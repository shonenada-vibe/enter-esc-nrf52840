#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/kernel.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/printk.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/uuid.h>

#define DEVICE_NAME                    CONFIG_BT_DEVICE_NAME
#define DEVICE_NAME_LEN                (sizeof(DEVICE_NAME) - 1)

#define HID_INFO_VERSION               0x0111
#define HID_FLAGS                      0x02

#define HIDS_INPUT                     0x01
#define HIDS_OUTPUT                    0x02

#define HIDS_PROTOCOL_BOOT             0x00
#define HIDS_PROTOCOL_REPORT           0x01

#define ENTER_KEYCODE                  0x28
#define ESC_KEYCODE                    0x29

#define KEY_REPORT_LEN                 8

#define KEY_TAP_HOLD_MS                30

#define BUTTON_DEBOUNCE_MS             15
#define CLICK_SEQUENCE_TIMEOUT_MS      300
#define RECORD_STATE_IDLE              0x00
#define RECORD_STATE_ACTIVE            0x01

#define TAP_SAMPLE_PERIOD_MS           10
#define TAP_SEQUENCE_TIMEOUT_MS        250
#define TAP_PEAK_DEADTIME_MS           80
#define TAP_MAG_HIGH_THRESHOLD_MM_S2   18000
#define TAP_MAG_LOW_THRESHOLD_MM_S2    13000

#define HAS_DIRECT_KEY_BUTTONS         (DT_HAS_ALIAS(sw0) && DT_HAS_ALIAS(sw1))
#define HAS_GESTURE_BUTTON_INPUT       (DT_HAS_ALIAS(sw0) && !DT_HAS_ALIAS(sw1))
#define HAS_TAP_SENSOR_INPUT           DT_HAS_COMPAT_STATUS_OKAY(st_lsm6dsl)
#define HAS_PULSE_KEY_OUTPUT           (HAS_GESTURE_BUTTON_INPUT || HAS_TAP_SENSOR_INPUT)
#define HAS_PRIMARY_BUTTON_INPUT       (HAS_DIRECT_KEY_BUTTONS || HAS_GESTURE_BUTTON_INPUT)
#define HAS_STATUS_LED                 (HAS_TAP_SENSOR_INPUT && DT_HAS_ALIAS(led2))
#define HAS_RECORD_BUTTON              DT_HAS_ALIAS(recordbtn)

#if !HAS_PRIMARY_BUTTON_INPUT && !HAS_TAP_SENSOR_INPUT
#error "No supported input source found for this board"
#endif

#if HAS_PRIMARY_BUTTON_INPUT
#define PRIMARY_BUTTON_NODE            DT_ALIAS(sw0)
#endif

#if HAS_DIRECT_KEY_BUTTONS
#define ESC_BUTTON_NODE                DT_ALIAS(sw1)
#endif

#define BT_UUID_RECORD_CTRL_SERVICE_VAL \
	BT_UUID_128_ENCODE(0x48f2d000, 0x7a15, 0x4b3f, 0x8d67, 0x60587f5d1001)
#define BT_UUID_RECORD_CTRL_STATE_VAL \
	BT_UUID_128_ENCODE(0x48f2d000, 0x7a15, 0x4b3f, 0x8d67, 0x60587f5d1002)

struct hids_info {
	uint16_t version;
	uint8_t code;
	uint8_t flags;
} __packed;

struct hids_report_ref {
	uint8_t id;
	uint8_t type;
} __packed;

#if HAS_PRIMARY_BUTTON_INPUT
static const struct gpio_dt_spec primary_button = GPIO_DT_SPEC_GET(PRIMARY_BUTTON_NODE, gpios);

static struct gpio_callback primary_button_cb_data;
static struct k_work_delayable primary_button_scan_work;
#endif

#if HAS_DIRECT_KEY_BUTTONS
static const struct gpio_dt_spec esc_button = GPIO_DT_SPEC_GET(ESC_BUTTON_NODE, gpios);

static struct gpio_callback esc_button_cb_data;
#endif

#if HAS_GESTURE_BUTTON_INPUT
static struct k_work_delayable gesture_sequence_work;
static bool primary_button_pressed;
static uint8_t gesture_click_count;
#endif

#if HAS_TAP_SENSOR_INPUT
static const struct device *const tap_sensor = DEVICE_DT_GET_ANY(st_lsm6dsl);

static struct k_work_delayable tap_sample_work;
static struct k_work_delayable tap_sequence_work;
#endif

#if HAS_PULSE_KEY_OUTPUT
static struct k_work_delayable key_release_work;
#endif

#if HAS_TAP_SENSOR_INPUT
static bool tap_detection_armed = true;
static uint8_t tap_sequence_count;
static int64_t last_tap_peak_ms;
#endif

#if HAS_STATUS_LED
static const struct gpio_dt_spec status_led = GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios);
#endif

#if HAS_RECORD_BUTTON
static const struct gpio_dt_spec record_button = GPIO_DT_SPEC_GET(DT_ALIAS(recordbtn), gpios);
static struct gpio_callback record_button_cb_data;
static struct k_work_delayable record_button_scan_work;

static struct bt_uuid_128 record_ctrl_service_uuid = BT_UUID_INIT_128(
	BT_UUID_RECORD_CTRL_SERVICE_VAL);
static struct bt_uuid_128 record_ctrl_state_uuid = BT_UUID_INIT_128(
	BT_UUID_RECORD_CTRL_STATE_VAL);
#endif

static struct bt_conn *active_conn;

static bool enter_pressed;
static bool esc_pressed;
static bool input_report_notify_enabled;
static bool boot_kb_notify_enabled;
static bool record_ctrl_notify_enabled;
static bool record_button_pressed;

static uint8_t protocol_mode = HIDS_PROTOCOL_REPORT;
static uint8_t ctrl_point;
static uint8_t output_report;
static uint8_t boot_kb_out_report;
static uint8_t record_ctrl_state = RECORD_STATE_IDLE;

static uint8_t input_report[KEY_REPORT_LEN];
static uint8_t boot_kb_in_report[KEY_REPORT_LEN];

static const struct hids_info hid_info = {
	.version = HID_INFO_VERSION,
	.code = 0x00,
	.flags = HID_FLAGS,
};

static const struct hids_report_ref input_report_ref = {
	.id = 0x00,
	.type = HIDS_INPUT,
};

static const struct hids_report_ref output_report_ref = {
	.id = 0x00,
	.type = HIDS_OUTPUT,
};

static const uint8_t report_map[] = {
	0x05, 0x01,
	0x09, 0x06,
	0xA1, 0x01,
	0x05, 0x07,
	0x19, 0xE0,
	0x29, 0xE7,
	0x15, 0x00,
	0x25, 0x01,
	0x75, 0x01,
	0x95, 0x08,
	0x81, 0x02,
	0x95, 0x01,
	0x75, 0x08,
	0x81, 0x01,
	0x95, 0x06,
	0x75, 0x08,
	0x15, 0x00,
	0x25, 0x65,
	0x05, 0x07,
	0x19, 0x00,
	0x29, 0x65,
	0x81, 0x00,
	0x95, 0x05,
	0x75, 0x01,
	0x05, 0x08,
	0x19, 0x01,
	0x29, 0x05,
	0x91, 0x02,
	0x95, 0x01,
	0x75, 0x03,
	0x91, 0x01,
	0xC0,
};

static const struct bt_data ad[] = {
	BT_DATA_BYTES(BT_DATA_GAP_APPEARANCE,
		      (CONFIG_BT_DEVICE_APPEARANCE >> 0) & 0xff,
		      (CONFIG_BT_DEVICE_APPEARANCE >> 8) & 0xff),
	BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
	BT_DATA_BYTES(BT_DATA_UUID16_ALL, BT_UUID_16_ENCODE(BT_UUID_HIDS_VAL)),
#if HAS_RECORD_BUTTON
	BT_DATA_BYTES(BT_DATA_UUID128_ALL, BT_UUID_RECORD_CTRL_SERVICE_VAL),
#endif
};

static const struct bt_data sd[] = {
	BT_DATA(BT_DATA_NAME_COMPLETE, DEVICE_NAME, DEVICE_NAME_LEN),
};

static void advertising_start(void)
{
	int err;

	err = bt_le_adv_start(BT_LE_ADV_CONN_FAST_1, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));
	if (err == -EALREADY) {
		return;
	}

	if (err) {
		printk("Advertising start failed (err %d)\n", err);
		return;
	}

	printk("Advertising started\n");
}

static ssize_t read_hid_info(struct bt_conn *conn,
			     const struct bt_gatt_attr *attr,
			     void *buf,
			     uint16_t len,
			     uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset,
				 attr->user_data, sizeof(hid_info));
}

static ssize_t read_report_map(struct bt_conn *conn,
			       const struct bt_gatt_attr *attr,
			       void *buf,
			       uint16_t len,
			       uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset,
				 report_map, sizeof(report_map));
}

static ssize_t read_protocol_mode(struct bt_conn *conn,
				  const struct bt_gatt_attr *attr,
				  void *buf,
				  uint16_t len,
				  uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset,
				 attr->user_data, sizeof(protocol_mode));
}

static ssize_t write_protocol_mode(struct bt_conn *conn,
				   const struct bt_gatt_attr *attr,
				   const void *buf,
				   uint16_t len,
				   uint16_t offset,
				   uint8_t flags)
{
	uint8_t *value = attr->user_data;

	ARG_UNUSED(conn);
	ARG_UNUSED(flags);

	if (offset != 0U || len != 1U) {
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
	}

	if ((((const uint8_t *)buf)[0] != HIDS_PROTOCOL_BOOT) &&
	    (((const uint8_t *)buf)[0] != HIDS_PROTOCOL_REPORT)) {
		return BT_GATT_ERR(BT_ATT_ERR_VALUE_NOT_ALLOWED);
	}

	*value = ((const uint8_t *)buf)[0];
	return len;
}

static ssize_t read_input_report(struct bt_conn *conn,
				 const struct bt_gatt_attr *attr,
				 void *buf,
				 uint16_t len,
				 uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset,
				 attr->user_data, KEY_REPORT_LEN);
}

static ssize_t read_report_ref(struct bt_conn *conn,
			       const struct bt_gatt_attr *attr,
			       void *buf,
			       uint16_t len,
			       uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset,
				 attr->user_data, sizeof(struct hids_report_ref));
}

static ssize_t read_output_report(struct bt_conn *conn,
				  const struct bt_gatt_attr *attr,
				  void *buf,
				  uint16_t len,
				  uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset,
				 attr->user_data, sizeof(output_report));
}

static ssize_t write_output_report(struct bt_conn *conn,
				   const struct bt_gatt_attr *attr,
				   const void *buf,
				   uint16_t len,
				   uint16_t offset,
				   uint8_t flags)
{
	uint8_t *value = attr->user_data;

	ARG_UNUSED(conn);
	ARG_UNUSED(flags);

	if (offset != 0U || len != 1U) {
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
	}

	*value = ((const uint8_t *)buf)[0];
	return len;
}

static ssize_t write_ctrl_point(struct bt_conn *conn,
				const struct bt_gatt_attr *attr,
				const void *buf,
				uint16_t len,
				uint16_t offset,
				uint8_t flags)
{
	uint8_t *value = attr->user_data;

	ARG_UNUSED(conn);
	ARG_UNUSED(flags);

	if (offset != 0U || len != 1U) {
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
	}

	*value = ((const uint8_t *)buf)[0];
	return len;
}

static void input_report_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
	ARG_UNUSED(attr);
	input_report_notify_enabled = (value == BT_GATT_CCC_NOTIFY);
}

static void boot_kb_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
	ARG_UNUSED(attr);
	boot_kb_notify_enabled = (value == BT_GATT_CCC_NOTIFY);
}

#if HAS_RECORD_BUTTON
static ssize_t read_record_ctrl_state(struct bt_conn *conn,
				      const struct bt_gatt_attr *attr,
				      void *buf,
				      uint16_t len,
				      uint16_t offset)
{
	return bt_gatt_attr_read(conn, attr, buf, len, offset,
				 attr->user_data, sizeof(record_ctrl_state));
}

static void record_ctrl_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
	ARG_UNUSED(attr);
	record_ctrl_notify_enabled = (value == BT_GATT_CCC_NOTIFY);
}

BT_GATT_SERVICE_DEFINE(record_ctrl_svc,
	BT_GATT_PRIMARY_SERVICE(&record_ctrl_service_uuid),
	BT_GATT_CHARACTERISTIC(&record_ctrl_state_uuid.uuid,
			       BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_READ,
			       read_record_ctrl_state, NULL, &record_ctrl_state),
	BT_GATT_CCC(record_ctrl_ccc_changed,
		    BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);
#endif

BT_GATT_SERVICE_DEFINE(hids_svc,
	BT_GATT_PRIMARY_SERVICE(BT_UUID_HIDS),
	BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_PROTOCOL_MODE,
			       BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
			       BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT,
			       read_protocol_mode, write_protocol_mode, &protocol_mode),
	BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_INFO,
			       BT_GATT_CHRC_READ,
			       BT_GATT_PERM_READ,
			       read_hid_info, NULL, (void *)&hid_info),
	BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_REPORT_MAP,
			       BT_GATT_CHRC_READ,
			       BT_GATT_PERM_READ,
			       read_report_map, NULL, NULL),
	BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_REPORT,
			       BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_READ_ENCRYPT,
			       read_input_report, NULL, input_report),
	BT_GATT_CCC(input_report_ccc_changed,
		    BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT),
	BT_GATT_DESCRIPTOR(BT_UUID_HIDS_REPORT_REF,
			   BT_GATT_PERM_READ_ENCRYPT,
			   read_report_ref, NULL, (void *)&input_report_ref),
	BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_BOOT_KB_IN_REPORT,
			       BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_READ_ENCRYPT,
			       read_input_report, NULL, boot_kb_in_report),
	BT_GATT_CCC(boot_kb_ccc_changed,
		    BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT),
	BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_REPORT,
			       BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
			       BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT,
			       read_output_report, write_output_report, &output_report),
	BT_GATT_DESCRIPTOR(BT_UUID_HIDS_REPORT_REF,
			   BT_GATT_PERM_READ_ENCRYPT,
			   read_report_ref, NULL, (void *)&output_report_ref),
	BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_BOOT_KB_OUT_REPORT,
			       BT_GATT_CHRC_READ | BT_GATT_CHRC_WRITE | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
			       BT_GATT_PERM_READ_ENCRYPT | BT_GATT_PERM_WRITE_ENCRYPT,
			       read_output_report, write_output_report, &boot_kb_out_report),
	BT_GATT_CHARACTERISTIC(BT_UUID_HIDS_CTRL_POINT,
			       BT_GATT_CHRC_WRITE_WITHOUT_RESP,
			       BT_GATT_PERM_WRITE_ENCRYPT,
			       NULL, write_ctrl_point, &ctrl_point),
);

static int send_key_report(void)
{
	int err;

	if (active_conn == NULL) {
		printk("Key change ignored: no BLE connection\n");
		return 0;
	}

	memcpy(boot_kb_in_report, input_report, sizeof(input_report));

	if (protocol_mode == HIDS_PROTOCOL_BOOT) {
		if (!boot_kb_notify_enabled) {
			printk("Key change ignored: boot keyboard notify not enabled\n");
			return 0;
		}

		err = bt_gatt_notify(active_conn, &hids_svc.attrs[12],
				     boot_kb_in_report, sizeof(boot_kb_in_report));
	} else {
		if (!input_report_notify_enabled) {
			printk("Key change ignored: input report notify not enabled\n");
			return 0;
		}

		err = bt_gatt_notify(active_conn, &hids_svc.attrs[8],
				     input_report, sizeof(input_report));
	}

	return err;
}

static void rebuild_report(void)
{
	memset(input_report, 0, sizeof(input_report));

	if (enter_pressed) {
		input_report[2] = ENTER_KEYCODE;
	}

	if (esc_pressed) {
		input_report[enter_pressed ? 3 : 2] = ESC_KEYCODE;
	}
}

static int apply_key_state(bool new_enter_pressed, bool new_esc_pressed)
{
	int err;

	if ((new_enter_pressed == enter_pressed) &&
	    (new_esc_pressed == esc_pressed)) {
		return 0;
	}

	enter_pressed = new_enter_pressed;
	esc_pressed = new_esc_pressed;
	rebuild_report();

	err = send_key_report();
	if (err) {
		printk("Key report send failed (err %d)\n", err);
	}

	return err;
}

#if HAS_PRIMARY_BUTTON_INPUT || HAS_RECORD_BUTTON
static bool gpio_button_is_pressed(const struct gpio_dt_spec *button)
{
	int value = gpio_pin_get_dt(button);

	if (value < 0) {
		printk("Button read failed on pin %u (err %d)\n", button->pin, value);
		return false;
	}

	return value != 0;
}
#endif

#if HAS_DIRECT_KEY_BUTTONS
static void primary_button_scan_work_handler(struct k_work *work)
{
	bool new_enter_pressed = gpio_button_is_pressed(&primary_button);
	bool new_esc_pressed = gpio_button_is_pressed(&esc_button);

	ARG_UNUSED(work);

	if (new_enter_pressed != enter_pressed) {
		printk("Enter %s\n", new_enter_pressed ? "pressed" : "released");
	}

	if (new_esc_pressed != esc_pressed) {
		printk("Esc %s\n", new_esc_pressed ? "pressed" : "released");
	}

	apply_key_state(new_enter_pressed, new_esc_pressed);
}

static void primary_button_gpio_handler(const struct device *port,
					struct gpio_callback *cb,
					uint32_t pins)
{
	ARG_UNUSED(port);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);

	k_work_reschedule(&primary_button_scan_work, K_MSEC(BUTTON_DEBOUNCE_MS));
}

static int buttons_init(void)
{
	int err;

	if (!gpio_is_ready_dt(&primary_button) || !gpio_is_ready_dt(&esc_button)) {
		printk("Button GPIO controller not ready\n");
		return -ENODEV;
	}

	err = gpio_pin_configure_dt(&primary_button, GPIO_INPUT);
	if (err) {
		printk("Enter button configure failed (err %d)\n", err);
		return err;
	}

	err = gpio_pin_configure_dt(&esc_button, GPIO_INPUT);
	if (err) {
		printk("Esc button configure failed (err %d)\n", err);
		return err;
	}

	gpio_init_callback(&primary_button_cb_data, primary_button_gpio_handler, BIT(primary_button.pin));
	err = gpio_add_callback(primary_button.port, &primary_button_cb_data);
	if (err) {
		printk("Enter button callback add failed (err %d)\n", err);
		return err;
	}

	gpio_init_callback(&esc_button_cb_data, primary_button_gpio_handler, BIT(esc_button.pin));
	err = gpio_add_callback(esc_button.port, &esc_button_cb_data);
	if (err) {
		printk("Esc button callback add failed (err %d)\n", err);
		return err;
	}

	err = gpio_pin_interrupt_configure_dt(&primary_button, GPIO_INT_EDGE_BOTH);
	if (err) {
		printk("Enter button IRQ configure failed (err %d)\n", err);
		return err;
	}

	err = gpio_pin_interrupt_configure_dt(&esc_button, GPIO_INT_EDGE_BOTH);
	if (err) {
		printk("Esc button IRQ configure failed (err %d)\n", err);
		return err;
	}

	enter_pressed = gpio_button_is_pressed(&primary_button);
	esc_pressed = gpio_button_is_pressed(&esc_button);
	rebuild_report();

	printk("Initial buttons: enter=%d esc=%d\n", enter_pressed, esc_pressed);
	return 0;
}
#endif

#if HAS_PULSE_KEY_OUTPUT
static void key_release_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	apply_key_state(false, false);
}

static void emit_key_tap(uint8_t keycode)
{
	switch (keycode) {
	case ENTER_KEYCODE:
		printk("Emit Enter\n");
		apply_key_state(true, false);
		break;
	case ESC_KEYCODE:
		printk("Emit Esc\n");
		apply_key_state(false, true);
		break;
	default:
		return;
	}

	k_work_reschedule(&key_release_work, K_MSEC(KEY_TAP_HOLD_MS));
}
#endif

#if HAS_GESTURE_BUTTON_INPUT
static void gesture_sequence_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	if (gesture_click_count == 2U) {
		printk("Double click detected\n");
		emit_key_tap(ENTER_KEYCODE);
	} else if (gesture_click_count == 1U) {
		printk("Single click ignored\n");
	}

	gesture_click_count = 0U;
}

static void primary_button_scan_work_handler(struct k_work *work)
{
	bool new_pressed = gpio_button_is_pressed(&primary_button);

	ARG_UNUSED(work);

	if (new_pressed == primary_button_pressed) {
		return;
	}

	primary_button_pressed = new_pressed;
	printk("Gesture button %s\n", new_pressed ? "pressed" : "released");

	if (new_pressed) {
		return;
	}

	gesture_click_count++;
	printk("Gesture click count=%u\n", gesture_click_count);

	if (gesture_click_count >= 3U) {
		gesture_click_count = 0U;
		k_work_cancel_delayable(&gesture_sequence_work);
		printk("Triple click detected\n");
		emit_key_tap(ESC_KEYCODE);
		return;
	}

	k_work_reschedule(&gesture_sequence_work, K_MSEC(CLICK_SEQUENCE_TIMEOUT_MS));
}

static void primary_button_gpio_handler(const struct device *port,
					struct gpio_callback *cb,
					uint32_t pins)
{
	ARG_UNUSED(port);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);

	k_work_reschedule(&primary_button_scan_work, K_MSEC(BUTTON_DEBOUNCE_MS));
}

static int gesture_button_init(void)
{
	int err;

	if (!gpio_is_ready_dt(&primary_button)) {
		printk("Gesture button GPIO controller not ready\n");
		return -ENODEV;
	}

	err = gpio_pin_configure_dt(&primary_button, GPIO_INPUT);
	if (err) {
		printk("Gesture button configure failed (err %d)\n", err);
		return err;
	}

	gpio_init_callback(&primary_button_cb_data, primary_button_gpio_handler, BIT(primary_button.pin));
	err = gpio_add_callback(primary_button.port, &primary_button_cb_data);
	if (err) {
		printk("Gesture button callback add failed (err %d)\n", err);
		return err;
	}

	err = gpio_pin_interrupt_configure_dt(&primary_button, GPIO_INT_EDGE_BOTH);
	if (err) {
		printk("Gesture button IRQ configure failed (err %d)\n", err);
		return err;
	}

	primary_button_pressed = gpio_button_is_pressed(&primary_button);
	printk("Initial gesture button=%d\n", primary_button_pressed);
	return 0;
}
#endif

#if HAS_RECORD_BUTTON
static int send_record_ctrl_state(void)
{
	int err;

	if (active_conn == NULL) {
		printk("Record control change ignored: no BLE connection\n");
		return 0;
	}

	if (!record_ctrl_notify_enabled) {
		printk("Record control change ignored: notify not enabled\n");
		return 0;
	}

	err = bt_gatt_notify(active_conn, &record_ctrl_svc.attrs[2],
			     &record_ctrl_state, sizeof(record_ctrl_state));
	if (err) {
		printk("Record control notify failed (err %d)\n", err);
	}

	return err;
}

static void apply_record_button_state(bool pressed)
{
	if (pressed == record_button_pressed) {
		return;
	}

	record_button_pressed = pressed;
	record_ctrl_state = pressed ? RECORD_STATE_ACTIVE : RECORD_STATE_IDLE;

	printk("Record control %s\n", pressed ? "start" : "stop");
	send_record_ctrl_state();
}

static void record_button_scan_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	apply_record_button_state(gpio_button_is_pressed(&record_button));
}

static void record_button_gpio_handler(const struct device *port,
				       struct gpio_callback *cb,
				       uint32_t pins)
{
	ARG_UNUSED(port);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);

	k_work_reschedule(&record_button_scan_work, K_MSEC(BUTTON_DEBOUNCE_MS));
}

static int record_button_init(void)
{
	int err;

	if (!gpio_is_ready_dt(&record_button)) {
		printk("Record button GPIO controller not ready\n");
		return -ENODEV;
	}

	err = gpio_pin_configure_dt(&record_button, GPIO_INPUT);
	if (err) {
		printk("Record button configure failed (err %d)\n", err);
		return err;
	}

	gpio_init_callback(&record_button_cb_data, record_button_gpio_handler, BIT(record_button.pin));
	err = gpio_add_callback(record_button.port, &record_button_cb_data);
	if (err) {
		printk("Record button callback add failed (err %d)\n", err);
		return err;
	}

	err = gpio_pin_interrupt_configure_dt(&record_button, GPIO_INT_EDGE_BOTH);
	if (err) {
		printk("Record button IRQ configure failed (err %d)\n", err);
		return err;
	}

	record_button_pressed = gpio_button_is_pressed(&record_button);
	record_ctrl_state = record_button_pressed ? RECORD_STATE_ACTIVE : RECORD_STATE_IDLE;
	printk("Initial record button=%d\n", record_button_pressed);

	return 0;
}
#endif

#if HAS_TAP_SENSOR_INPUT
static void tap_sequence_work_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	if (tap_sequence_count == 2U) {
		printk("Double tap detected\n");
		emit_key_tap(ENTER_KEYCODE);
	} else if (tap_sequence_count == 1U) {
		printk("Single tap ignored\n");
	}

	tap_sequence_count = 0U;
}

static void register_tap_peak(void)
{
	tap_sequence_count++;
	printk("Tap detected, count=%u\n", tap_sequence_count);

	if (tap_sequence_count >= 3U) {
		tap_sequence_count = 0U;
		k_work_cancel_delayable(&tap_sequence_work);
		printk("Triple tap detected\n");
		emit_key_tap(ESC_KEYCODE);
		return;
	}

	k_work_reschedule(&tap_sequence_work, K_MSEC(TAP_SEQUENCE_TIMEOUT_MS));
}

static void tap_sample_work_handler(struct k_work *work)
{
	int err;
	struct sensor_value accel[3];
	int64_t x_milli;
	int64_t y_milli;
	int64_t z_milli;
	int64_t mag_sq;
	int64_t now_ms = k_uptime_get();

	ARG_UNUSED(work);

	err = sensor_sample_fetch(tap_sensor);
	if (err) {
		printk("Tap sensor fetch failed (err %d)\n", err);
		goto reschedule;
	}

	err = sensor_channel_get(tap_sensor, SENSOR_CHAN_ACCEL_XYZ, accel);
	if (err) {
		printk("Tap sensor read failed (err %d)\n", err);
		goto reschedule;
	}

	x_milli = sensor_value_to_milli(&accel[0]);
	y_milli = sensor_value_to_milli(&accel[1]);
	z_milli = sensor_value_to_milli(&accel[2]);

	mag_sq = (x_milli * x_milli) + (y_milli * y_milli) + (z_milli * z_milli);

	if (mag_sq < ((int64_t)TAP_MAG_LOW_THRESHOLD_MM_S2 * TAP_MAG_LOW_THRESHOLD_MM_S2)) {
		tap_detection_armed = true;
	}

	if (tap_detection_armed &&
	    (mag_sq > ((int64_t)TAP_MAG_HIGH_THRESHOLD_MM_S2 * TAP_MAG_HIGH_THRESHOLD_MM_S2)) &&
	    ((now_ms - last_tap_peak_ms) >= TAP_PEAK_DEADTIME_MS)) {
		last_tap_peak_ms = now_ms;
		tap_detection_armed = false;
		register_tap_peak();
	}

reschedule:
	k_work_reschedule(&tap_sample_work, K_MSEC(TAP_SAMPLE_PERIOD_MS));
}

static int tap_sensor_init(void)
{
	int err;
	struct sensor_value accel_range;
	struct sensor_value accel_odr = { .val1 = 208, .val2 = 0 };

	if (tap_sensor == NULL || !device_is_ready(tap_sensor)) {
		printk("Tap sensor not ready\n");
		return -ENODEV;
	}

	sensor_g_to_ms2(16, &accel_range);

	err = sensor_attr_set(tap_sensor, SENSOR_CHAN_ACCEL_XYZ,
			      SENSOR_ATTR_FULL_SCALE, &accel_range);
	if (err) {
		printk("Tap sensor full-scale setup failed (err %d)\n", err);
	}

	err = sensor_attr_set(tap_sensor, SENSOR_CHAN_ACCEL_XYZ,
			      SENSOR_ATTR_SAMPLING_FREQUENCY, &accel_odr);
	if (err) {
		printk("Tap sensor ODR setup failed (err %d)\n", err);
	}

	printk("Tap sensor initialized\n");
	k_work_reschedule(&tap_sample_work, K_NO_WAIT);

	return 0;
}
#endif

static void connected(struct bt_conn *conn, uint8_t err)
{
	if (err) {
		printk("Connection failed: %s (err 0x%02x)\n", bt_conn_dst_str(conn), err);
		advertising_start();
		return;
	}

	printk("Connected: %s\n", bt_conn_dst_str(conn));

	if (active_conn == NULL) {
		active_conn = bt_conn_ref(conn);
	}

	err = bt_conn_set_security(conn, BT_SECURITY_L2);
	if (err) {
		printk("Security setup failed (err %d)\n", err);
	}

#if HAS_DIRECT_KEY_BUTTONS || HAS_GESTURE_BUTTON_INPUT
	k_work_reschedule(&primary_button_scan_work, K_NO_WAIT);
#endif

#if HAS_RECORD_BUTTON
	k_work_reschedule(&record_button_scan_work, K_NO_WAIT);
#endif
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	printk("Disconnected: %s (reason 0x%02x)\n", bt_conn_dst_str(conn), reason);

	if (active_conn == conn) {
		bt_conn_unref(active_conn);
		active_conn = NULL;
	}

	input_report_notify_enabled = false;
	boot_kb_notify_enabled = false;
	record_ctrl_notify_enabled = false;
	protocol_mode = HIDS_PROTOCOL_REPORT;

	advertising_start();
}

static void security_changed(struct bt_conn *conn,
			     bt_security_t level,
			     enum bt_security_err err)
{
	if (!err) {
		printk("Security changed: %s level %u\n", bt_conn_dst_str(conn), level);
		return;
	}

	printk("Security failed: %s level %u err %d\n", bt_conn_dst_str(conn), level, err);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.disconnected = disconnected,
	.security_changed = security_changed,
};

static void pairing_complete(struct bt_conn *conn, bool bonded)
{
	printk("Pairing complete: %s bonded=%d\n", bt_conn_dst_str(conn), bonded);
}

static void pairing_failed(struct bt_conn *conn, enum bt_security_err reason)
{
	printk("Pairing failed: %s reason=%d\n", bt_conn_dst_str(conn), reason);
}

static struct bt_conn_auth_info_cb auth_info_cb = {
	.pairing_complete = pairing_complete,
	.pairing_failed = pairing_failed,
};

static int status_led_init(void)
{
#if HAS_STATUS_LED
	int err;

	if (!gpio_is_ready_dt(&status_led)) {
		printk("Status LED controller not ready\n");
		return -ENODEV;
	}

	err = gpio_pin_configure_dt(&status_led, GPIO_OUTPUT_INACTIVE);
	if (err) {
		printk("Status LED configure failed (err %d)\n", err);
		return err;
	}

	err = gpio_pin_set_dt(&status_led, 1);
	if (err) {
		printk("Status LED set failed (err %d)\n", err);
		return err;
	}

	printk("Status LED on\n");
#endif

	return 0;
}

static int input_init(void)
{
	int err;

#if HAS_DIRECT_KEY_BUTTONS
	err = buttons_init();
	if (err) {
		return err;
	}
#endif

#if HAS_GESTURE_BUTTON_INPUT
	err = gesture_button_init();
	if (err) {
		return err;
	}
#endif

#if HAS_TAP_SENSOR_INPUT
	err = tap_sensor_init();
	if (err) {
		return err;
	}
#endif

#if HAS_RECORD_BUTTON
	err = record_button_init();
	if (err) {
		return err;
	}
#endif

	return 0;
}

int main(void)
{
	int err;

	printk("EnterEsc Keyboard starting\n");

	err = status_led_init();
	if (err) {
		return 0;
	}

#if HAS_DIRECT_KEY_BUTTONS || HAS_GESTURE_BUTTON_INPUT
	k_work_init_delayable(&primary_button_scan_work, primary_button_scan_work_handler);
#endif

#if HAS_TAP_SENSOR_INPUT
	k_work_init_delayable(&tap_sample_work, tap_sample_work_handler);
	k_work_init_delayable(&tap_sequence_work, tap_sequence_work_handler);
#endif

#if HAS_PULSE_KEY_OUTPUT
	k_work_init_delayable(&key_release_work, key_release_work_handler);
#endif

#if HAS_GESTURE_BUTTON_INPUT
	k_work_init_delayable(&gesture_sequence_work, gesture_sequence_work_handler);
#endif

#if HAS_RECORD_BUTTON
	k_work_init_delayable(&record_button_scan_work, record_button_scan_work_handler);
#endif

	err = input_init();
	if (err) {
		return 0;
	}

	err = bt_enable(NULL);
	if (err) {
		printk("Bluetooth init failed (err %d)\n", err);
		return 0;
	}

	printk("Bluetooth initialized\n");

	if (IS_ENABLED(CONFIG_SETTINGS)) {
		err = settings_load();
		if (err) {
			printk("settings_load failed (err %d)\n", err);
		}
	}

	err = bt_conn_auth_info_cb_register(&auth_info_cb);
	if (err) {
		printk("Auth info callback register failed (err %d)\n", err);
	}

	advertising_start();

	for (;;) {
		k_sleep(K_FOREVER);
	}
}

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/printk.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/uuid.h>

#define DEVICE_NAME             CONFIG_BT_DEVICE_NAME
#define DEVICE_NAME_LEN         (sizeof(DEVICE_NAME) - 1)

#define HID_INFO_VERSION        0x0111
#define HID_FLAGS               0x02

#define HIDS_INPUT              0x01
#define HIDS_OUTPUT             0x02

#define HIDS_PROTOCOL_BOOT      0x00
#define HIDS_PROTOCOL_REPORT    0x01

#define ENTER_KEYCODE           0x28
#define ESC_KEYCODE             0x29

#define KEY_REPORT_LEN          8
#define BUTTON_DEBOUNCE_MS      15

#define ENTER_BUTTON_NODE       DT_ALIAS(sw0)
#define ESC_BUTTON_NODE         DT_ALIAS(sw1)

#if !DT_NODE_HAS_STATUS(ENTER_BUTTON_NODE, okay)
#error "sw0 alias is not defined"
#endif

#if !DT_NODE_HAS_STATUS(ESC_BUTTON_NODE, okay)
#error "sw1 alias is not defined"
#endif

struct hids_info {
	uint16_t version;
	uint8_t code;
	uint8_t flags;
} __packed;

struct hids_report_ref {
	uint8_t id;
	uint8_t type;
} __packed;

static const struct gpio_dt_spec enter_button = GPIO_DT_SPEC_GET(ENTER_BUTTON_NODE, gpios);
static const struct gpio_dt_spec esc_button = GPIO_DT_SPEC_GET(ESC_BUTTON_NODE, gpios);

static struct gpio_callback enter_button_cb_data;
static struct gpio_callback esc_button_cb_data;
static struct k_work_delayable button_scan_work;

static struct bt_conn *active_conn;

static bool enter_pressed;
static bool esc_pressed;
static bool input_report_notify_enabled;
static bool boot_kb_notify_enabled;

static uint8_t protocol_mode = HIDS_PROTOCOL_REPORT;
static uint8_t ctrl_point;
static uint8_t output_report;
static uint8_t boot_kb_out_report;

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

static bool button_is_pressed(const struct gpio_dt_spec *button)
{
	int value = gpio_pin_get_dt(button);

	if (value < 0) {
		printk("Button read failed on pin %u (err %d)\n", button->pin, value);
		return false;
	}

	return value != 0;
}

static void rebuild_report_from_buttons(void)
{
	memset(input_report, 0, sizeof(input_report));

	if (enter_pressed) {
		input_report[2] = ENTER_KEYCODE;
	}

	if (esc_pressed) {
		input_report[enter_pressed ? 3 : 2] = ESC_KEYCODE;
	}
}

static void button_scan_work_handler(struct k_work *work)
{
	bool new_enter_pressed = button_is_pressed(&enter_button);
	bool new_esc_pressed = button_is_pressed(&esc_button);
	bool state_changed = false;
	int err;

	ARG_UNUSED(work);

	if (new_enter_pressed != enter_pressed) {
		enter_pressed = new_enter_pressed;
		printk("Enter %s\n", enter_pressed ? "pressed" : "released");
		state_changed = true;
	}

	if (new_esc_pressed != esc_pressed) {
		esc_pressed = new_esc_pressed;
		printk("Esc %s\n", esc_pressed ? "pressed" : "released");
		state_changed = true;
	}

	if (!state_changed) {
		return;
	}

	rebuild_report_from_buttons();

	err = send_key_report();
	if (err) {
		printk("Key report send failed (err %d)\n", err);
	}
}

static void button_gpio_handler(const struct device *port,
				struct gpio_callback *cb,
				uint32_t pins)
{
	ARG_UNUSED(port);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);

	k_work_reschedule(&button_scan_work, K_MSEC(BUTTON_DEBOUNCE_MS));
}

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

	k_work_reschedule(&button_scan_work, K_NO_WAIT);
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

static int buttons_init(void)
{
	int err;

	if (!gpio_is_ready_dt(&enter_button) || !gpio_is_ready_dt(&esc_button)) {
		printk("Button GPIO controller not ready\n");
		return -ENODEV;
	}

	err = gpio_pin_configure_dt(&enter_button, GPIO_INPUT);
	if (err) {
		printk("Enter button configure failed (err %d)\n", err);
		return err;
	}

	err = gpio_pin_configure_dt(&esc_button, GPIO_INPUT);
	if (err) {
		printk("Esc button configure failed (err %d)\n", err);
		return err;
	}

	gpio_init_callback(&enter_button_cb_data, button_gpio_handler, BIT(enter_button.pin));
	err = gpio_add_callback(enter_button.port, &enter_button_cb_data);
	if (err) {
		printk("Enter button callback add failed (err %d)\n", err);
		return err;
	}

	gpio_init_callback(&esc_button_cb_data, button_gpio_handler, BIT(esc_button.pin));
	err = gpio_add_callback(esc_button.port, &esc_button_cb_data);
	if (err) {
		printk("Esc button callback add failed (err %d)\n", err);
		return err;
	}

	err = gpio_pin_interrupt_configure_dt(&enter_button, GPIO_INT_EDGE_BOTH);
	if (err) {
		printk("Enter button IRQ configure failed (err %d)\n", err);
		return err;
	}

	err = gpio_pin_interrupt_configure_dt(&esc_button, GPIO_INT_EDGE_BOTH);
	if (err) {
		printk("Esc button IRQ configure failed (err %d)\n", err);
		return err;
	}

	enter_pressed = button_is_pressed(&enter_button);
	esc_pressed = button_is_pressed(&esc_button);
	rebuild_report_from_buttons();
	printk("Initial buttons: enter=%d esc=%d\n", enter_pressed, esc_pressed);

	return 0;
}

int main(void)
{
	int err;

	printk("EnterEsc Keyboard starting\n");

	k_work_init_delayable(&button_scan_work, button_scan_work_handler);

	err = buttons_init();
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

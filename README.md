# EnterEsc Keyboard

A standalone Zephyr app targeting `promicro_nrf52840` boards, including SuperMini nRF52840 / nice!nano style boards.

Features:

- BLE HID over GATT keyboard
- Device name: `EnterEsc Keyboard`
- `Enter` button sends HID keycode `0x28`
- `Esc` button sends HID keycode `0x29`
- Sends key down on press and release on button-up
- Supports bonding
- Automatically resumes advertising after disconnection

## Default Button Pins

The default overlay uses:

- Enter button: `D2 / P0.08`
- Esc button: `D3 / P0.06`

Expected button wiring:

- One side to the corresponding GPIO
- The other side to GND

If your buttons are wired to different pins, update the `gpios` entries in [boards/promicro_nrf52840_nrf52840_uf2.overlay](/path/to/enter-esc-nrf52840/boards/promicro_nrf52840_nrf52840_uf2.overlay).

## Build

```sh
west build -p auto -b promicro_nrf52840/nrf52840/uf2 .
```

## Flash

First put the board into UF2 bootloader mode, usually by double-tapping `RST`, then run:

```sh
west flash
```

Notes:

- The recommended target is `promicro_nrf52840/nrf52840/uf2`
- This target includes a bootloader/storage-friendly partition layout, which makes bonding persistence more straightforward

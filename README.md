# EnterEsc Keyboard

A standalone Zephyr BLE HID keyboard app with support for two nRF52840 board families:

- `promicro_nrf52840/nrf52840/uf2`
- `xiao_ble/nrf52840/sense`

Features:

- BLE HID over GATT keyboard
- Device name: `EnterEsc Keyboard`
- Supports bonding
- Automatically resumes advertising after disconnection

## Board Behaviors

### `promicro_nrf52840/nrf52840/uf2`

- BLE device name: `EnterEsc Keyboard`
- `D2 / P0.08` gesture button:
  single or double click sends `Enter` (`0x28`)
  triple click sends `Esc` (`0x29`)
- `D3 / P0.06` record-control button:
  press and hold sends `0x01`
  release sends `0x00`
- Red LED stays on while record control is active
- Supports bonding

Default button pins:

The default overlay uses:

- Gesture button: `D2 / P0.08`
- Record-control button: `D3 / P0.06`

Expected button wiring:

- One side to the corresponding GPIO
- The other side to GND

If your buttons are wired to different pins, update the `gpios` entries in [boards/promicro_nrf52840_nrf52840_uf2.overlay](/path/to/enter-esc-nrf52840/boards/promicro_nrf52840_nrf52840_uf2.overlay).

### `xiao_ble/nrf52840/sense`

- BLE device name: `EnterEsc Seeed`
- Double tap on the board sends `Enter` (`0x28`)
- Triple tap on the board sends `Esc` (`0x29`)
- Each gesture emits a short key press followed by release
- Tap detection is implemented in software from the onboard LSM6DS3TR-C accelerometer
- The onboard blue LED blinks while waiting for a BLE connection
- The onboard blue LED stays on after BLE connects
- The onboard red LED stays on while record control is active
- An external `Button B` on `D0 / P0.02` sends BLE record control events

Default `Button B` wiring:

- One side to `D0 / P0.02`
- The other side to `GND`

`Button B` BLE behavior:

- Press and hold: notify `0x01` (`record start`)
- Release: notify `0x00` (`record stop`)
- Custom service UUID: `48f2d000-7a15-4b3f-8d67-60587f5d1001`
- State characteristic UUID: `48f2d000-7a15-4b3f-8d67-60587f5d1002`

## Build

Build for Pro Micro style boards:

```sh
west build -p auto -b promicro_nrf52840/nrf52840/uf2 .
```

Build explicitly into a separate `promicro` build directory:

```sh
west build -p always -b promicro_nrf52840/nrf52840/uf2 \
  -s /path/to/enter-esc-nrf52840 \
  -d /path/to/enter-esc-nrf52840/build.promicro
```

Build for Seeed Studio XIAO BLE Sense:

```sh
west build -p auto -b xiao_ble/nrf52840/sense .
```

## Flash

First put the board into UF2 bootloader mode, usually by double-tapping `RST`, then run:

```sh
west flash
```

Flash the Pro Micro / SuperMini build explicitly:

```sh
west flash -d /path/to/enter-esc-nrf52840/build.promicro
```

Notes:

- `promicro_nrf52840/nrf52840/uf2` uses the board overlay in [boards/promicro_nrf52840_nrf52840_uf2.overlay](/path/to/enter-esc-nrf52840/boards/promicro_nrf52840_nrf52840_uf2.overlay)
- `promicro_nrf52840/nrf52840/uf2` includes a bootloader/storage-friendly partition layout, which makes bonding persistence more straightforward
- `xiao_ble/nrf52840/sense` uses the onboard accelerometer and does not require external buttons

## Mac Helper For Button B

The XIAO `Button B` record-control path is intended to work with a Mac-side helper:

- BLE device name: `EnterEsc Seeed`
- Record-control service UUID: `48f2d000-7a15-4b3f-8d67-60587f5d1001`
- State characteristic UUID: `48f2d000-7a15-4b3f-8d67-60587f5d1002`
- Press and hold sends `0x01`
- Release sends `0x00`

Install dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r host/requirements.txt
```

If you use Groq, set your API key:

```sh
export GROQ_API_KEY=your_groq_api_key
```

Run the Mac helper with Groq:

```sh
python host/mac_record_control.py
```

The helper now starts in a curses TUI by default. You can edit runtime settings live without restarting:

- Up / Down: select a field
- Enter: edit text / number values
- Space: toggle boolean values
- Left / Right: cycle choice fields such as `stt_provider`
- `i`: log available audio input devices
- `r`: reconnect BLE immediately with the current config
- `q`: quit

Connection-related changes such as BLE device name or characteristic UUID trigger a reconnect so the new settings take effect right away. Recording, STT, translation, and typing settings are picked up on the next action without restarting the script.
Runtime settings are now persisted to `~/Library/Application Support/EnterEscHost/config.json` by default, so TUI edits survive restarts.

If you want the old non-TUI mode:

```sh
python host/mac_record_control.py --no-tui
```

To use a different config file or ignore the saved one for a single launch:

```sh
python host/mac_record_control.py --config-file /path/to/config.json
python host/mac_record_control.py --reset-config
```

## macOS Menu Bar Shell

There is now an initial native macOS menu bar shell under `macos/EnterEscMenuBar`.

What it does today:

- launches as a menu bar app
- starts the Python host helper in background mode
- reads and writes the same persisted config file used by the Python helper
- lets you toggle:
  - `translate_to_en`
  - `press_return`
- lets you:
  - start the host
  - stop the host
  - restart the host
  - open the config file
  - open the config folder
  - open the host log

Build and run it with the full Xcode toolchain:

```sh
cd macos/EnterEscMenuBar
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift run
```

If you launch it from outside the repository, set the repo root explicitly so it can find `host/mac_record_control.py`:

```sh
ENTER_ESC_REPO_ROOT=/path/to/yesnoaudio \
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
swift run --package-path /path/to/yesnoaudio/macos/EnterEscMenuBar
```

Notes:

- The menu bar shell currently manages the existing Python runtime; it is not yet a full Swift rewrite of BLE/audio/STT behavior.
- The shell writes logs to `~/Library/Logs/EnterEscHost/menu-bar.log`.
- The Python helper config still lives at `~/Library/Application Support/EnterEscHost/config.json`.

To use the helper with the `promicro_nrf52840/nrf52840/uf2` target instead of XIAO:

```sh
python host/mac_record_control.py --device-name "EnterEsc Keyboard"
```

If you mainly speak Chinese, the helper already defaults to `--language zh`.
If you want a different input language, override it explicitly, for example:

```sh
python host/mac_record_control.py --language en
```

If you speak Chinese but want English typed into the active app, enable translation:

```sh
python host/mac_record_control.py --translate-to-en
```

This optional post-processing step uses the Groq API key in `GROQ_API_KEY` to translate the transcribed Chinese text into English before typing it.

Run the Mac helper with VAS:

```sh
python host/mac_record_control.py --stt-provider vas --vas-no-refine
```

VAS notes:

- The helper calls the Go demo client in `/path/to/vas/cmd/demo`
- It uses `go run . --json` and reads `final_text` from stdout
- Configure the service with `--vas-addr`, `--vas-access-token`, `--vas-model`, and `--vas-language`
- `--vas-no-refine` is recommended if you want plain STT text instead of VAS LLM post-processing

What it does:

- Connects to `EnterEsc Seeed` over BLE
- Subscribes to the record-control notify characteristic
- Starts recording from the Mac microphone on `0x01`
- Stops recording on `0x00`
- Sends the WAV file to the selected STT provider (`Groq` or `VAS`)
- Types the transcription into the active macOS app

Requirements and caveats:

- Grant microphone permission to the Python process running the helper
- Grant Accessibility permission so `osascript` / `System Events` can type text
- The typed text goes to the app that is frontmost when transcription completes

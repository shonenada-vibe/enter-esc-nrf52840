from __future__ import annotations

import contextlib
import curses
import textwrap
import threading
from dataclasses import dataclass

try:
    from .app_core import (
        AppState,
        AudioDeviceError,
        CONNECTION_FIELDS,
        LogBuffer,
        MASKED_FIELDS,
        RecordControlApp,
        RuntimeConfig,
        format_input_devices,
        install_signal_handlers,
        run_app_thread,
        state_path_for_config,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from app_core import (
        AppState,
        AudioDeviceError,
        CONNECTION_FIELDS,
        LogBuffer,
        MASKED_FIELDS,
        RecordControlApp,
        RuntimeConfig,
        format_input_devices,
        install_signal_handlers,
        run_app_thread,
        state_path_for_config,
    )


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: str
    choices: tuple[str, ...] = ()
    help_text: str = ""


FIELD_SPECS = [
    FieldSpec("device_name", "Device Name", "str", help_text="BLE peripheral name"),
    FieldSpec("service_uuid", "Service UUID", "str", help_text="For reference; reconnect field"),
    FieldSpec("char_uuid", "Char UUID", "str", help_text="Notify characteristic UUID"),
    FieldSpec("stt_provider", "STT Provider", "choice", choices=("groq", "vas", "whisper")),
    FieldSpec("model", "STT Model", "str"),
    FieldSpec("language", "STT Language", "str"),
    FieldSpec("translate_to_en", "Translate To EN", "bool"),
    FieldSpec("translation_model", "Translation Model", "str"),
    FieldSpec("whisper_api_url", "Whisper API URL", "str"),
    FieldSpec("whisper_api_key", "Whisper API Key", "str"),
    FieldSpec("vas_demo_dir", "VAS Demo Dir", "str"),
    FieldSpec("vas_addr", "VAS Addr", "str"),
    FieldSpec("vas_access_token", "VAS Access Token", "str"),
    FieldSpec("vas_model", "VAS Model", "str"),
    FieldSpec("vas_language", "VAS Language", "str"),
    FieldSpec("vas_no_refine", "VAS No Refine", "bool"),
    FieldSpec("sample_rate", "Sample Rate", "int"),
    FieldSpec("channels", "Channels", "int"),
    FieldSpec("input_device", "Input Device", "str", help_text="Use blank/default for PortAudio default"),
    FieldSpec("recordings_dir", "Recordings Dir", "str"),
    FieldSpec("scan_timeout", "Scan Timeout", "float"),
    FieldSpec("retry_delay", "Retry Delay", "float"),
    FieldSpec("press_return", "Press Return", "bool"),
]


class TerminalUi:
    def __init__(self, stdscr, config: RuntimeConfig, logger: LogBuffer, state: AppState, app: RecordControlApp):
        self.stdscr = stdscr
        self.config = config
        self.logger = logger
        self.state = state
        self.app = app
        self.selected_index = 0
        self.status_message = f"Ready | config: {self.config.path}"
        self.should_exit = False

    def run(self) -> None:
        self.stdscr.timeout(200)
        self.stdscr.keypad(True)
        with contextlib.suppress(curses.error):
            curses.curs_set(0)

        while not self.should_exit:
            self._draw()
            key = self.stdscr.getch()
            if key == -1:
                continue
            self._handle_key(key)

    def _draw(self) -> None:
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        top_height = min(len(FIELD_SPECS) + 7, max(12, height // 2))
        config_width = max(42, width // 2)
        visible_fields = max(1, top_height - 6)
        scroll_offset = min(
            max(0, self.selected_index - visible_fields + 1),
            max(0, len(FIELD_SPECS) - visible_fields),
        )

        cfg = self.config.snapshot()
        state = self.state.snapshot()

        self._safe_addstr(0, 0, "EnterEsc Host TUI", curses.A_BOLD)
        self._safe_addstr(
            1,
            0,
            f"Status: {state.status} | Connected: {state.connected_device or '-'} | Recording: {'yes' if state.recording else 'no'}",
        )
        self._safe_addstr(
            2,
            0,
            "Keys: Up/Down select  Left/Right cycle choice  Enter edit  Space toggle  i devices  r reconnect  q quit",
        )
        self._safe_addstr(3, 0, f"Config file: {self.config.path}"[: max(0, width - 1)])
        if state.last_error:
            self._safe_addstr(4, 0, f"Last error: {state.last_error}"[: max(0, width - 1)], curses.A_BOLD)
        if state.last_config_change:
            self._safe_addstr(5, 0, f"Last config change: {state.last_config_change}"[: max(0, width - 1)])

        config_title = (
            f"Config {scroll_offset + 1}-{min(len(FIELD_SPECS), scroll_offset + visible_fields)}/{len(FIELD_SPECS)}"
        )
        self._safe_addstr(6, 0, config_title, curses.A_UNDERLINE)
        for visible_index, spec in enumerate(FIELD_SPECS[scroll_offset: scroll_offset + visible_fields]):
            index = scroll_offset + visible_index
            row = 7 + visible_index
            value = getattr(cfg, spec.name)
            display = self._format_field_value(spec.name, value)
            line = f"{spec.label:<18} {display}"
            attr = curses.A_REVERSE if index == self.selected_index else curses.A_NORMAL
            self._safe_addstr(row, 0, line[: max(0, config_width - 1)], attr)

        self._safe_addstr(6, config_width + 1, "Logs", curses.A_UNDERLINE)
        log_lines = self.logger.lines()
        visible_log_lines = max(1, height - 7)
        start = max(0, len(log_lines) - visible_log_lines)
        log_width = max(10, width - config_width - 2)
        for offset, line in enumerate(log_lines[start:]):
            row = 7 + offset
            if row >= height - 2:
                break
            trimmed = textwrap.shorten(line, width=log_width - 1, placeholder="...")
            self._safe_addstr(row, config_width + 1, trimmed)

        self._safe_addstr(height - 1, 0, self.status_message[: max(0, width - 1)], curses.A_REVERSE)
        self.stdscr.refresh()

    def _handle_key(self, key: int) -> None:
        if key in (ord("q"), ord("Q")):
            self.status_message = "Shutting down..."
            self.app.request_shutdown()
            self.should_exit = True
            return

        if key == curses.KEY_UP:
            self.selected_index = (self.selected_index - 1) % len(FIELD_SPECS)
            return

        if key == curses.KEY_DOWN:
            self.selected_index = (self.selected_index + 1) % len(FIELD_SPECS)
            return

        if key in (ord("i"), ord("I")):
            try:
                for line in format_input_devices().splitlines():
                    self.logger.log(line)
                self.status_message = "Audio input devices logged"
            except AudioDeviceError as exc:
                self.logger.log(str(exc))
                self.status_message = str(exc)
            return

        if key in (ord("r"), ord("R")):
            self.app.request_reconnect()
            self.status_message = "Reconnect requested"
            return

        spec = FIELD_SPECS[self.selected_index]
        current_value = getattr(self.config.snapshot(), spec.name)

        if key == ord(" ") and spec.kind == "bool":
            self._apply_value(spec, not bool(current_value))
            return

        if key in (curses.KEY_LEFT, curses.KEY_RIGHT) and spec.kind == "choice":
            choices = list(spec.choices)
            idx = choices.index(current_value) if current_value in choices else 0
            if key == curses.KEY_LEFT:
                idx = (idx - 1) % len(choices)
            else:
                idx = (idx + 1) % len(choices)
            self._apply_value(spec, choices[idx])
            return

        if key in (10, 13, curses.KEY_ENTER):
            if spec.kind == "bool":
                self._apply_value(spec, not bool(current_value))
                return
            if spec.kind == "choice":
                choices = list(spec.choices)
                idx = choices.index(current_value) if current_value in choices else 0
                idx = (idx + 1) % len(choices)
                self._apply_value(spec, choices[idx])
                return

            raw_value = self._prompt_value(spec, current_value)
            if raw_value is None:
                self.status_message = "Edit cancelled"
                return

            try:
                parsed = self._parse_value(spec, raw_value, current_value)
            except ValueError as exc:
                self.status_message = str(exc)
                self.logger.log(f"Config update rejected for {spec.label}: {exc}")
                return

            self._apply_value(spec, parsed)

    def _apply_value(self, spec: FieldSpec, value) -> None:
        connection_changed = self.config.update_field(spec.name, value)
        display_value = self._format_field_value(spec.name, value)
        change_message = f"{spec.label} -> {display_value}"
        self.state.update(last_config_change=change_message)
        self.logger.log(f"Config updated: {change_message}")
        if connection_changed or spec.name in CONNECTION_FIELDS:
            self.app.request_reconnect()
            self.status_message = f"{spec.label} updated; reconnect requested"
        else:
            self.status_message = f"{spec.label} updated"

    def _prompt_value(self, spec: FieldSpec, current_value):
        height, width = self.stdscr.getmaxyx()
        prompt = f"{spec.label} (current: {self._format_field_value(spec.name, current_value)}). New value: "
        self.stdscr.move(height - 1, 0)
        self.stdscr.clrtoeol()
        self._safe_addstr(height - 1, 0, prompt[: max(0, width - 1)], curses.A_REVERSE)
        self.stdscr.refresh()

        with contextlib.suppress(curses.error):
            curses.curs_set(1)
        curses.echo()
        self.stdscr.timeout(-1)
        try:
            raw = self.stdscr.getstr(
                height - 1,
                min(len(prompt), max(0, width - 2)),
                max(1, width - len(prompt) - 1),
            )
        except KeyboardInterrupt:
            raw = None
        finally:
            curses.noecho()
            self.stdscr.timeout(200)
            with contextlib.suppress(curses.error):
                curses.curs_set(0)

        if raw is None:
            return None
        return raw.decode("utf-8").strip()

    def _parse_value(self, spec: FieldSpec, raw_value: str, current_value):
        if raw_value == "":
            if spec.name in {"input_device", "whisper_api_url"}:
                return None
            return current_value

        if spec.kind == "int":
            return int(raw_value)
        if spec.kind == "float":
            return float(raw_value)
        if spec.kind == "bool":
            normalized = raw_value.casefold()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError("Use true/false for boolean values")
        if spec.kind == "choice":
            if raw_value not in spec.choices:
                raise ValueError(f"Expected one of: {', '.join(spec.choices)}")
            return raw_value
        return raw_value

    def _format_field_value(self, field_name: str, value) -> str:
        if field_name in MASKED_FIELDS and value:
            return "*" * min(len(str(value)), 8)
        if value is None or value == "":
            return "<default>"
        if isinstance(value, bool):
            return "on" if value else "off"
        return str(value)

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.stdscr.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        clipped = text[: max(0, width - x - 1)]
        with contextlib.suppress(curses.error):
            self.stdscr.addstr(y, x, clipped, attr)


def run_tui(config: RuntimeConfig) -> int:
    logger = LogBuffer(echo_stdout=False)
    state = AppState(path=state_path_for_config(config.path))
    app = RecordControlApp(config=config, logger=logger, state=state)
    install_signal_handlers(app)

    result: dict[str, int] = {"code": 0}
    worker = threading.Thread(target=run_app_thread, args=(app, logger, result), daemon=True)
    worker.start()

    try:
        curses.wrapper(lambda stdscr: TerminalUi(stdscr, config, logger, state, app).run())
    finally:
        app.request_shutdown()
        worker.join(timeout=5)

    return result["code"]

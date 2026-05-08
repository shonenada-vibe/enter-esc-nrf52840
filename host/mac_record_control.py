#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

try:
    from .app_core import (
        DEFAULT_CONFIG_PATH,
        RuntimeConfig,
        RuntimeConfigSnapshot,
        AppState,
        AudioDeviceError,
        LogBuffer,
        RecordControlApp,
        STT_PROVIDER_CHOICES,
        apply_namespace_overrides,
        format_input_devices,
        install_signal_handlers,
        load_runtime_config,
        select_input_device,
    )
    from .tui_app import run_tui
except ImportError:  # pragma: no cover - direct script execution fallback
    from app_core import (
        DEFAULT_CONFIG_PATH,
        RuntimeConfig,
        RuntimeConfigSnapshot,
        AppState,
        AudioDeviceError,
        LogBuffer,
        RecordControlApp,
        STT_PROVIDER_CHOICES,
        apply_namespace_overrides,
        format_input_devices,
        install_signal_handlers,
        load_runtime_config,
        select_input_device,
    )
    from tui_app import run_tui


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BLE record-control host with a live-edit TUI for recording, STT, and translation settings."
    )
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_PATH,
                        help=f"Path to the persisted runtime config. Default: {DEFAULT_CONFIG_PATH}")
    parser.add_argument("--reset-config", action="store_true",
                        help="Ignore any existing config file and start from built-in defaults.")

    parser.add_argument("--device-name", default=argparse.SUPPRESS,
                        help="BLE peripheral name to scan for.")
    parser.add_argument("--service-uuid", default=argparse.SUPPRESS,
                        help="Custom record-control service UUID.")
    parser.add_argument("--char-uuid", default=argparse.SUPPRESS,
                        help="Record-control state characteristic UUID.")
    parser.add_argument("--stt-provider", choices=STT_PROVIDER_CHOICES, default=argparse.SUPPRESS,
                        help="STT provider to use.")
    parser.add_argument("--model", default=argparse.SUPPRESS,
                        help="Transcription model for Groq or local Whisper.")
    parser.add_argument("--language", default=argparse.SUPPRESS,
                        help="Optional ISO-639-1 language hint for Groq/local Whisper.")
    parser.add_argument("--translate-to-en", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                        help="Translate the final transcription to English before typing it.")
    parser.add_argument("--translation-model", default=argparse.SUPPRESS,
                        help="Groq text model used for translation.")
    parser.add_argument("--whisper-api-url", default=argparse.SUPPRESS,
                        help="OpenAI-compatible Whisper transcription endpoint.")
    parser.add_argument("--whisper-api-key", default=argparse.SUPPRESS,
                        help="Optional bearer token for the local Whisper endpoint.")
    parser.add_argument("--vas-demo-dir", default=argparse.SUPPRESS,
                        help="Path to the VAS demo Go client directory.")
    parser.add_argument("--vas-addr", default=argparse.SUPPRESS,
                        help="VAS gRPC server host or host:port.")
    parser.add_argument("--vas-access-token", default=argparse.SUPPRESS,
                        help="VAS access token.")
    parser.add_argument("--vas-model", default=argparse.SUPPRESS,
                        help="VAS ASR model: bigmodel|once|streaming.")
    parser.add_argument("--vas-language", default=argparse.SUPPRESS,
                        help="VAS language code.")
    parser.add_argument("--vas-no-refine", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                        help="Disable VAS LLM refine stage and use raw STT output only.")
    parser.add_argument("--sample-rate", type=int, default=argparse.SUPPRESS,
                        help="Microphone sample rate in Hz.")
    parser.add_argument("--channels", type=int, default=argparse.SUPPRESS,
                        help="Recorded channel count.")
    parser.add_argument("--input-device", default=argparse.SUPPRESS,
                        help="Optional sounddevice input device index or name fragment.")
    parser.add_argument("--list-input-devices", action="store_true",
                        help="List available sounddevice input devices and exit.")
    parser.add_argument("--recordings-dir", default=argparse.SUPPRESS,
                        help="Directory for temporary WAV files.")
    parser.add_argument("--scan-timeout", type=float, default=argparse.SUPPRESS,
                        help="Seconds to scan before retrying.")
    parser.add_argument("--retry-delay", type=float, default=argparse.SUPPRESS,
                        help="Seconds to wait before rescanning after disconnect/failure.")
    parser.add_argument("--press-return", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS,
                        help="Press Return after typing the transcription.")
    parser.add_argument("--no-tui", action="store_true",
                        help="Run without the interactive curses TUI.")
    return parser


def build_runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    if args.reset_config:
        snapshot = RuntimeConfigSnapshot()
    else:
        snapshot = load_runtime_config(args.config_file)

    config = RuntimeConfig(snapshot, path=args.config_file)
    apply_namespace_overrides(config, args)
    return config


async def main_async_cli(config: RuntimeConfig) -> int:
    logger = LogBuffer(echo_stdout=True)
    state = AppState()
    app = RecordControlApp(config=config, logger=logger, state=state)
    install_signal_handlers(app)
    return await app.run()


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.list_input_devices:
        try:
            print(format_input_devices(), flush=True)
        except AudioDeviceError as exc:
            print(exc, file=sys.stderr, flush=True)
            return 1
        return 0

    try:
        config = build_runtime_config(args)
    except RuntimeError as exc:
        print(exc, file=sys.stderr, flush=True)
        return 1

    if args.no_tui and config.snapshot().input_device is None:
        cfg = config.snapshot()
        try:
            selected = select_input_device(
                channels=cfg.channels,
                sample_rate=cfg.sample_rate,
            )
        except AudioDeviceError as exc:
            print(exc, file=sys.stderr, flush=True)
            return 1
        config.update_field("input_device", selected)

    try:
        if args.no_tui:
            return asyncio.run(main_async_cli(config))
        return run_tui(config)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

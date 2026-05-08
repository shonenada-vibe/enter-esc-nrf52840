#!/usr/bin/env python3
import argparse
import asyncio
import contextlib
import curses
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import uuid
import wave
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional
from urllib import error, request

import sounddevice as sd
from bleak import BleakClient, BleakScanner
from groq import Groq


DEFAULT_DEVICE_NAME = "EnterEsc Seeed"
DEFAULT_SERVICE_UUID = "48f2d000-7a15-4b3f-8d67-60587f5d1001"
DEFAULT_CHAR_UUID = "48f2d000-7a15-4b3f-8d67-60587f5d1002"
DEFAULT_STT_PROVIDER = "whisper" if os.environ.get("WHISPER_API_URL") else "groq"
DEFAULT_MODEL = "whisper-large-v3-turbo"
DEFAULT_TRANSLATION_MODEL = "llama-3.1-8b-instant"
DEFAULT_VAS_DEMO_DIR = "./"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECORD_STATE_IDLE = 0x00
RECORD_STATE_ACTIVE = 0x01

STT_PROVIDER_CHOICES = ("groq", "vas", "whisper")
CONNECTION_FIELDS = {"device_name", "service_uuid", "char_uuid", "scan_timeout", "retry_delay"}
MASKED_FIELDS = {"whisper_api_key", "vas_access_token"}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BLE record-control host with a live-edit TUI for recording, STT, and translation settings."
    )
    parser.add_argument("--device-name", default=DEFAULT_DEVICE_NAME,
                        help=f"BLE peripheral name to scan for. Default: {DEFAULT_DEVICE_NAME}")
    parser.add_argument("--service-uuid", default=DEFAULT_SERVICE_UUID,
                        help="Custom record-control service UUID.")
    parser.add_argument("--char-uuid", default=DEFAULT_CHAR_UUID,
                        help="Record-control state characteristic UUID.")
    parser.add_argument("--stt-provider", choices=STT_PROVIDER_CHOICES, default=DEFAULT_STT_PROVIDER,
                        help=f"STT provider to use. Default: {DEFAULT_STT_PROVIDER}")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Transcription model for Groq or local Whisper. Default: {DEFAULT_MODEL}")
    parser.add_argument("--language", default="zh",
                        help="Optional ISO-639-1 language hint for Groq/local Whisper. Default: zh")
    parser.add_argument("--translate-to-en", action="store_true",
                        help="Translate the final transcription to English before typing it.")
    parser.add_argument("--translation-model", default=DEFAULT_TRANSLATION_MODEL,
                        help=f"Groq text model used for translation. Default: {DEFAULT_TRANSLATION_MODEL}")
    parser.add_argument("--whisper-api-url", default=os.environ.get("WHISPER_API_URL"),
                        help="OpenAI-compatible Whisper transcription endpoint. Falls back to WHISPER_API_URL.")
    parser.add_argument("--whisper-api-key", default=os.environ.get("WHISPER_API_KEY", ""),
                        help="Optional bearer token for the local Whisper endpoint. Falls back to WHISPER_API_KEY.")
    parser.add_argument("--vas-demo-dir", default=DEFAULT_VAS_DEMO_DIR,
                        help=f"Path to the VAS demo Go client directory. Default: {DEFAULT_VAS_DEMO_DIR}")
    parser.add_argument("--vas-addr", default=os.environ.get("VAS_DEMO_ADDR", "106.53.30.28"),
                        help="VAS gRPC server host or host:port.")
    parser.add_argument("--vas-access-token", default=os.environ.get("VAS_DEMO_ACCESS_TOKEN", ""),
                        help="VAS access token. Falls back to VAS_DEMO_ACCESS_TOKEN.")
    parser.add_argument("--vas-model", default="bigmodel",
                        help="VAS ASR model: bigmodel|once|streaming. Default: bigmodel")
    parser.add_argument("--vas-language", default="zh-CN",
                        help="VAS language code. Default: zh-CN")
    parser.add_argument("--vas-no-refine", action="store_true",
                        help="Disable VAS LLM refine stage and use raw STT output only.")
    parser.add_argument("--sample-rate", type=int, default=16000,
                        help="Microphone sample rate in Hz. Default: 16000")
    parser.add_argument("--channels", type=int, default=1,
                        help="Recorded channel count. Default: 1")
    parser.add_argument("--input-device", default=None,
                        help="Optional sounddevice input device index or name fragment.")
    parser.add_argument("--list-input-devices", action="store_true",
                        help="List available sounddevice input devices and exit.")
    parser.add_argument("--recordings-dir", default=".cache/host_recordings",
                        help="Directory for temporary WAV files. Default: .cache/host_recordings")
    parser.add_argument("--scan-timeout", type=float, default=10.0,
                        help="Seconds to scan before retrying. Default: 10")
    parser.add_argument("--retry-delay", type=float, default=2.0,
                        help="Seconds to wait before rescanning after disconnect/failure. Default: 2")
    parser.add_argument("--press-return", action="store_true",
                        help="Press Return after typing the transcription.")
    parser.add_argument("--no-tui", action="store_true",
                        help="Run without the interactive curses TUI.")
    return parser


@dataclass
class RecordingResult:
    path: Path
    duration_s: float


@dataclass
class RuntimeConfigSnapshot:
    device_name: str = DEFAULT_DEVICE_NAME
    service_uuid: str = DEFAULT_SERVICE_UUID
    char_uuid: str = DEFAULT_CHAR_UUID
    stt_provider: str = DEFAULT_STT_PROVIDER
    model: str = DEFAULT_MODEL
    language: str = "zh"
    translate_to_en: bool = False
    translation_model: str = DEFAULT_TRANSLATION_MODEL
    whisper_api_url: Optional[str] = os.environ.get("WHISPER_API_URL")
    whisper_api_key: str = os.environ.get("WHISPER_API_KEY", "")
    vas_demo_dir: str = DEFAULT_VAS_DEMO_DIR
    vas_addr: str = os.environ.get("VAS_DEMO_ADDR", "106.53.30.28")
    vas_access_token: str = os.environ.get("VAS_DEMO_ACCESS_TOKEN", "")
    vas_model: str = "bigmodel"
    vas_language: str = "zh-CN"
    vas_no_refine: bool = False
    sample_rate: int = 16000
    channels: int = 1
    input_device: Optional[str] = None
    recordings_dir: str = ".cache/host_recordings"
    scan_timeout: float = 10.0
    retry_delay: float = 2.0
    press_return: bool = False


@dataclass
class AppStateSnapshot:
    status: str = "idle"
    connected_device: str = ""
    recording: bool = False
    last_config_change: str = ""
    last_error: str = ""


class AudioDeviceError(RuntimeError):
    pass


class LogBuffer:
    def __init__(self, max_lines: int = 500, echo_stdout: bool = True):
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()
        self.echo_stdout = echo_stdout

    def log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        with self._lock:
            self._lines.append(line)
        if self.echo_stdout:
            print(line, flush=True)

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)


class RuntimeConfig:
    def __init__(self, initial: RuntimeConfigSnapshot):
        self._snapshot = initial
        self._lock = threading.Lock()
        self._version = 0
        self._connection_version = 0

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "RuntimeConfig":
        return cls(
            RuntimeConfigSnapshot(
                device_name=args.device_name,
                service_uuid=args.service_uuid,
                char_uuid=args.char_uuid,
                stt_provider=args.stt_provider,
                model=args.model,
                language=args.language,
                translate_to_en=args.translate_to_en,
                translation_model=args.translation_model,
                whisper_api_url=args.whisper_api_url,
                whisper_api_key=args.whisper_api_key,
                vas_demo_dir=args.vas_demo_dir,
                vas_addr=args.vas_addr,
                vas_access_token=args.vas_access_token,
                vas_model=args.vas_model,
                vas_language=args.vas_language,
                vas_no_refine=args.vas_no_refine,
                sample_rate=args.sample_rate,
                channels=args.channels,
                input_device=args.input_device,
                recordings_dir=args.recordings_dir,
                scan_timeout=args.scan_timeout,
                retry_delay=args.retry_delay,
                press_return=args.press_return,
            )
        )

    def snapshot(self) -> RuntimeConfigSnapshot:
        with self._lock:
            return replace(self._snapshot)

    def update_field(self, field_name: str, value) -> bool:
        with self._lock:
            setattr(self._snapshot, field_name, value)
            self._version += 1
            connection_changed = field_name in CONNECTION_FIELDS
            if connection_changed:
                self._connection_version += 1
            return connection_changed

    def versions(self) -> tuple[int, int]:
        with self._lock:
            return self._version, self._connection_version


class AppState:
    def __init__(self):
        self._snapshot = AppStateSnapshot()
        self._lock = threading.Lock()

    def snapshot(self) -> AppStateSnapshot:
        with self._lock:
            return replace(self._snapshot)

    def update(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self._snapshot, key, value)


class Transcriber:
    def transcribe(self, path: Path) -> str:
        raise NotImplementedError


class TextTransformer:
    def transform(self, text: str) -> str:
        raise NotImplementedError


class IdentityTransformer(TextTransformer):
    def transform(self, text: str) -> str:
        return text


def _input_devices() -> list[tuple[int, dict]]:
    try:
        devices = sd.query_devices()
    except Exception as exc:
        raise AudioDeviceError(f"Unable to query audio input devices: {exc}") from exc

    return [
        (index, device)
        for index, device in enumerate(devices)
        if int(device.get("max_input_channels") or 0) > 0
    ]


def _default_input_device_index() -> Optional[int]:
    default_device = sd.default.device
    if isinstance(default_device, (list, tuple)):
        default_device = default_device[0] if default_device else None

    try:
        index = int(default_device)
    except (TypeError, ValueError):
        return None

    if index < 0:
        return None

    return index


def format_input_devices() -> str:
    default_index = _default_input_device_index()
    lines = ["Available sounddevice input devices:"]
    devices = _input_devices()
    if not devices:
        lines.append("  (none)")
        return "\n".join(lines)

    for index, device in devices:
        default_marker = " *" if index == default_index else "  "
        name = device.get("name") or "(unnamed)"
        channels = int(device.get("max_input_channels") or 0)
        sample_rate = int(float(device.get("default_samplerate") or 0))
        lines.append(f"{default_marker} {index}: {name} ({channels} input ch, default {sample_rate} Hz)")

    return "\n".join(lines)


def select_input_device(channels: int, sample_rate: int) -> str:
    devices = _input_devices()
    if not devices:
        raise AudioDeviceError(
            "No audio input devices are visible to PortAudio. Check the microphone connection "
            "and macOS Microphone permission for the terminal app running this script."
        )

    default_index = _default_input_device_index()
    if default_index not in {index for index, _device in devices} and len(devices) == 1:
        default_index = devices[0][0]

    print(format_input_devices(), flush=True)
    while True:
        default_hint = f" [{default_index}]" if default_index is not None else ""
        try:
            choice = input(f"Select input device index or name{default_hint}: ").strip()
        except EOFError as exc:
            raise AudioDeviceError(
                "No input device was selected. Run in an interactive terminal or pass "
                "`--input-device <index-or-name>`."
            ) from exc
        if not choice and default_index is not None:
            choice = str(default_index)
        if not choice:
            print("Enter an input device index or name.", flush=True)
            continue

        try:
            selected_index = resolve_input_device(choice, channels=channels, sample_rate=sample_rate)
        except AudioDeviceError as exc:
            print(f"Audio input error: {exc}", flush=True)
            continue

        selected_device = sd.query_devices(selected_index)
        selected_name = selected_device.get("name") or selected_index
        print(f"Selected input {selected_index} ({selected_name})", flush=True)
        return str(selected_index)


def resolve_input_device(input_device: Optional[str], channels: int, sample_rate: int) -> int:
    devices = _input_devices()
    if not devices:
        raise AudioDeviceError(
            "No audio input devices are visible to PortAudio. Check the microphone connection "
            "and macOS Microphone permission for the terminal app running this script."
        )

    if input_device in (None, "", "default"):
        default_index = _default_input_device_index()
        index = None
        for index, device in devices:
            if index == default_index:
                break
        else:
            index = None

        if index is None and len(devices) == 1:
            index = devices[0][0]

        if index is None:
            raise AudioDeviceError(
                "PortAudio has no valid default input device. Set one explicitly.\n" + format_input_devices()
            )
        device = sd.query_devices(index)
    else:
        try:
            index = int(input_device)
        except ValueError:
            name_fragment = input_device.casefold()
            matches = [
                (index, device)
                for index, device in devices
                if name_fragment in (device.get("name") or "").casefold()
            ]
            if not matches:
                raise AudioDeviceError(
                    f"No audio input device matches {input_device!r}.\n{format_input_devices()}"
                )
            if len(matches) > 1:
                matched = ", ".join(f"{index}: {device.get('name')}" for index, device in matches)
                raise AudioDeviceError(f"Audio input device name {input_device!r} is ambiguous: {matched}")
            index, device = matches[0]
        else:
            for device_index, device in devices:
                if device_index == index:
                    break
            else:
                raise AudioDeviceError(
                    f"Audio input device {input_device!r} is not an available input device.\n"
                    + format_input_devices()
                )

    try:
        sd.check_input_settings(
            device=index,
            channels=channels,
            dtype="int16",
            samplerate=sample_rate,
        )
    except Exception as exc:
        name = device.get("name") or index
        raise AudioDeviceError(
            f"Audio input device {index} ({name}) does not support "
            f"{channels} channel(s) at {sample_rate} Hz: {exc}"
        ) from exc

    return index


class AudioRecorder:
    def __init__(self, config: RuntimeConfig, logger: LogBuffer):
        self.config = config
        self.logger = logger
        self.stream: Optional[sd.RawInputStream] = None
        self.wave_file: Optional[wave.Wave_write] = None
        self.path: Optional[Path] = None
        self.started_at: Optional[float] = None
        self.lock = threading.Lock()

    def start(self) -> None:
        with self.lock:
            if self.stream is not None:
                self.logger.log("Recorder already active; ignoring duplicate start")
                return

            cfg = self.config.snapshot()
            device_index = resolve_input_device(
                cfg.input_device,
                channels=cfg.channels,
                sample_rate=cfg.sample_rate,
            )
            device_info = sd.query_devices(device_index)
            recordings_dir = Path(cfg.recordings_dir)
            recordings_dir.mkdir(parents=True, exist_ok=True)
            filename = time.strftime("recording-%Y%m%d-%H%M%S.wav")
            self.path = recordings_dir / filename
            self.wave_file = wave.open(str(self.path), "wb")
            self.wave_file.setnchannels(cfg.channels)
            self.wave_file.setsampwidth(2)
            self.wave_file.setframerate(cfg.sample_rate)

            try:
                self.stream = sd.RawInputStream(
                    samplerate=cfg.sample_rate,
                    channels=cfg.channels,
                    dtype="int16",
                    blocksize=0,
                    device=device_index,
                    callback=self._callback,
                )
                self.stream.start()
            except Exception:
                stream = self.stream
                wave_file = self.wave_file
                path = self.path
                self.stream = None
                self.wave_file = None
                self.path = None
                self.started_at = None
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()
                if wave_file is not None:
                    with contextlib.suppress(Exception):
                        wave_file.close()
                if path is not None:
                    with contextlib.suppress(Exception):
                        path.unlink()
                raise

            self.started_at = time.time()
            device_name = device_info.get("name") or device_index
            self.logger.log(f"Recording started from input {device_index} ({device_name}): {self.path}")

    def stop(self) -> Optional[RecordingResult]:
        with self.lock:
            if self.stream is None or self.wave_file is None or self.path is None:
                self.logger.log("Recorder already idle; ignoring duplicate stop")
                return None

            stream = self.stream
            wave_file = self.wave_file
            path = self.path
            started_at = self.started_at or time.time()

            self.stream = None
            self.wave_file = None
            self.path = None
            self.started_at = None

        with contextlib.suppress(Exception):
            stream.stop()
        with contextlib.suppress(Exception):
            stream.close()
        wave_file.close()

        duration_s = max(time.time() - started_at, 0.0)
        self.logger.log(f"Recording stopped: {path} ({duration_s:.2f}s)")
        return RecordingResult(path=path, duration_s=duration_s)

    def _callback(self, indata, frames, time_info, status) -> None:
        del frames
        del time_info
        if status:
            self.logger.log(f"Audio callback status: {status}")

        with self.lock:
            if self.wave_file is not None:
                self.wave_file.writeframes(indata)


class GroqTranscriber(Transcriber):
    def __init__(self, model: str, language: str, logger: LogBuffer):
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not set")

        self.client = Groq(api_key=api_key)
        self.model = model
        self.language = language
        self.logger = logger

    def transcribe(self, path: Path) -> str:
        with open(path, "rb") as handle:
            result = self.client.audio.transcriptions.create(
                file=(path.name, handle.read()),
                model=self.model,
                language=self.language,
                response_format="json",
                temperature=0.0,
            )

        text = (result.text or "").strip()
        self.logger.log(f"Groq transcription: {text!r}")
        return text


class LocalWhisperTranscriber(Transcriber):
    def __init__(self, api_url: Optional[str], api_key: str, model: str, language: str, logger: LogBuffer):
        if not api_url:
            raise RuntimeError(
                "WHISPER_API_URL is not set. Provide --whisper-api-url or configure it in the TUI."
            )

        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.language = language
        self.logger = logger

    def transcribe(self, path: Path) -> str:
        fields = {
            "model": self.model,
            "language": self.language,
            "response_format": "json",
            "temperature": "0",
        }
        body, content_type = self._multipart_body(fields, path)
        headers = {"Content-Type": content_type}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = request.Request(self.api_url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Local Whisper request failed with HTTP {exc.code}: {error_body}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Local Whisper request failed: {exc}") from exc

        text = self._extract_text(payload)
        self.logger.log(f"Local Whisper transcription: {text!r}")
        return text

    def _multipart_body(self, fields: dict[str, str], path: Path) -> tuple[bytes, str]:
        boundary = f"----enter-esc-{uuid.uuid4().hex}"
        parts = []
        for name, value in fields.items():
            parts.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode(),
                b"\r\n",
            ])

        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
            b"Content-Type: audio/wav\r\n\r\n",
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def _extract_text(self, payload) -> str:
        if isinstance(payload, dict):
            text = payload.get("text") or payload.get("transcription")
            if text is not None:
                return str(text).strip()

        raise RuntimeError(f"Local Whisper response did not include transcription text: {payload!r}")


class VASTranscriber(Transcriber):
    def __init__(
        self,
        demo_dir: Path,
        addr: str,
        access_token: str,
        model: str,
        language: str,
        no_refine: bool,
        logger: LogBuffer,
    ):
        self.demo_dir = demo_dir
        self.addr = addr
        self.access_token = access_token
        self.model = model
        self.language = language
        self.no_refine = no_refine
        self.logger = logger

        if not self.demo_dir.is_dir():
            raise RuntimeError(f"VAS demo directory does not exist: {self.demo_dir}")

    def transcribe(self, path: Path) -> str:
        cmd = [
            "./vas-cli",
            "--json",
            "--addr", self.addr,
            "--model", self.model,
            "--language", self.language,
            "--encoding", "raw",
        ]

        if self.access_token:
            cmd.extend(["--access-token", self.access_token])
        if self.no_refine:
            cmd.append("--no-refine")

        cmd.append(str(path))
        env = os.environ.copy()
        env.setdefault("GOCACHE", str(PROJECT_ROOT / ".cache/go-build"))

        self.logger.log(f"VAS transcription command: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            cwd=self.demo_dir,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )

        if result.stderr.strip():
            self.logger.log(result.stderr.strip())

        payload = json.loads(result.stdout)
        text = (payload.get("final_text") or "").strip()
        self.logger.log(f"VAS transcription: {text!r}")
        return text


class GroqEnglishTranslator(TextTransformer):
    def __init__(self, model: str, logger: LogBuffer):
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not set; required for translation")

        self.client = Groq(api_key=api_key)
        self.model = model
        self.logger = logger

    def transform(self, text: str) -> str:
        if not text:
            return text

        messages = [
            {
                "role": "system",
                "content": (
                    "You are translating raw speech transcription into English. "
                    "Translate the speaker's words faithfully and directly. "
                    "Do not answer the speaker, do not summarize, do not interpret intent, "
                    "and do not turn rhetorical questions into replies. "
                    "Preserve the original meaning, tone, and sentence form as much as possible. "
                    "If the input is already English, return it unchanged. "
                    "Return only the translated text."
                ),
            },
            {
                "role": "user",
                "content": f"Translate this transcript literally into English:\n\n{text}",
            },
        ]
        self.logger.log(
            f"Translation LLM request: {json.dumps({'model': self.model, 'messages': messages}, ensure_ascii=False)}"
        )

        result = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            messages=messages,
        )
        self.logger.log(f"Translation LLM response: {json.dumps(result.to_dict(), ensure_ascii=False)}")
        translated = (result.choices[0].message.content or "").strip()
        self.logger.log(f"English translation: {translated!r}")
        return translated


def build_transcriber(config: RuntimeConfigSnapshot, logger: LogBuffer) -> Transcriber:
    if config.stt_provider == "groq":
        return GroqTranscriber(model=config.model, language=config.language, logger=logger)

    if config.stt_provider == "whisper":
        return LocalWhisperTranscriber(
            api_url=config.whisper_api_url,
            api_key=config.whisper_api_key,
            model=config.model,
            language=config.language,
            logger=logger,
        )

    if config.stt_provider == "vas":
        return VASTranscriber(
            demo_dir=Path(config.vas_demo_dir),
            addr=config.vas_addr,
            access_token=config.vas_access_token,
            model=config.vas_model,
            language=config.vas_language,
            no_refine=config.vas_no_refine,
            logger=logger,
        )

    raise RuntimeError(f"Unsupported STT provider: {config.stt_provider}")


def build_text_transformer(config: RuntimeConfigSnapshot, logger: LogBuffer) -> TextTransformer:
    if config.translate_to_en:
        return GroqEnglishTranslator(model=config.translation_model, logger=logger)

    return IdentityTransformer()


def type_text_macos(text: str, press_return: bool, logger: LogBuffer) -> None:
    if not text:
        logger.log("Transcription empty; nothing to type")
        return

    script = """
on run argv
	set typedText to item 1 of argv
	set submitFlag to item 2 of argv
	set the clipboard to typedText
	tell application "System Events"
		keystroke "v" using command down
		if submitFlag is "1" then
			key code 36
		end if
	end tell
end run
"""

    subprocess.run(
        ["osascript", "-e", script, "--", text, "1" if press_return else "0"],
        check=True,
    )
    logger.log("Pasted transcription into the active macOS app")


class RecordControlApp:
    def __init__(self, config: RuntimeConfig, logger: LogBuffer, state: AppState):
        self.config = config
        self.logger = logger
        self.state = state
        self.recorder = AudioRecorder(config=config, logger=logger)
        self.recording_lock = asyncio.Lock()
        self.currently_recording = False

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown_event: Optional[asyncio.Event] = None
        self._reconnect_event: Optional[asyncio.Event] = None
        self._pending_shutdown = threading.Event()
        self._pending_reconnect = threading.Event()

    def request_shutdown(self) -> None:
        self._pending_shutdown.set()
        if self._loop is not None and self._shutdown_event is not None:
            self._loop.call_soon_threadsafe(self._shutdown_event.set)

    def request_reconnect(self) -> None:
        self._pending_reconnect.set()
        if self._loop is not None and self._reconnect_event is not None:
            self._loop.call_soon_threadsafe(self._reconnect_event.set)

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()
        self._reconnect_event = asyncio.Event()

        if self._pending_shutdown.is_set():
            self._shutdown_event.set()
        if self._pending_reconnect.is_set():
            self._reconnect_event.set()

        while not self._shutdown_event.is_set():
            cfg = self.config.snapshot()
            device = await self._find_device(cfg.device_name, cfg.scan_timeout)
            if device is None:
                await self._sleep_or_shutdown(self.config.snapshot().retry_delay)
                continue

            disconnected = asyncio.Event()

            def on_disconnect(_client: BleakClient) -> None:
                self.logger.log("BLE disconnected")
                self.state.update(status="disconnected", connected_device="", recording=False)
                disconnected.set()

            self._reconnect_event.clear()
            self._pending_reconnect.clear()

            try:
                async with BleakClient(device, disconnected_callback=on_disconnect) as client:
                    self.logger.log(f"Connected to {device.name or device.address}")
                    self.state.update(status="connected", connected_device=device.name or device.address)

                    notify_uuid = self.config.snapshot().char_uuid
                    await client.start_notify(notify_uuid, self._handle_notification)
                    self.logger.log(f"Subscribed to record-control notifications on {notify_uuid}")

                    wait_tasks = [
                        asyncio.create_task(disconnected.wait()),
                        asyncio.create_task(self._shutdown_event.wait()),
                        asyncio.create_task(self._reconnect_event.wait()),
                    ]
                    done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    for task in done:
                        with contextlib.suppress(asyncio.CancelledError):
                            task.result()

                    if self._reconnect_event.is_set():
                        self.logger.log("Runtime BLE config changed; reconnecting with new settings")

                    with contextlib.suppress(Exception):
                        await client.stop_notify(notify_uuid)
            except Exception as exc:
                self.logger.log(f"BLE session error: {exc}")
                self.state.update(status="error", last_error=str(exc), connected_device="", recording=False)

            if self._shutdown_event.is_set():
                break

            await self._sleep_or_shutdown(self.config.snapshot().retry_delay)

        self.state.update(status="stopped", connected_device="", recording=False)
        return 0

    async def _find_device(self, device_name: str, timeout: float):
        self.state.update(status="scanning", connected_device="")
        self.logger.log(f"Scanning for BLE device {device_name!r}...")
        devices = await BleakScanner.discover(timeout=timeout)
        for device in devices:
            if device.name == device_name:
                self.logger.log(f"Found device: {device.name} ({device.address})")
                return device

        self.logger.log("Device not found in this scan window")
        return None

    def _handle_notification(self, _sender: int, data: bytearray) -> None:
        if not data or self._loop is None:
            return

        value = data[0]
        if value == RECORD_STATE_ACTIVE:
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._start_recording()))
        elif value == RECORD_STATE_IDLE:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._stop_recording_and_transcribe())
            )
        else:
            self.logger.log(f"Ignoring unknown record-control value: {value}")

    async def _start_recording(self) -> None:
        async with self.recording_lock:
            if self.currently_recording:
                return

            try:
                self.recorder.start()
            except AudioDeviceError as exc:
                self.logger.log(f"Audio input error: {exc}")
                self.state.update(last_error=str(exc))
                return
            except Exception as exc:
                self.logger.log(f"Recording start error: {exc}")
                self.state.update(last_error=str(exc))
                return

            self.currently_recording = True
            self.state.update(recording=True)

    async def _stop_recording_and_transcribe(self) -> None:
        async with self.recording_lock:
            if not self.currently_recording:
                return

            result = self.recorder.stop()
            self.currently_recording = False
            self.state.update(recording=False)

        if result is None:
            return

        if result.duration_s < 0.15:
            self.logger.log("Recording too short; skipping transcription")
            return

        cfg = self.config.snapshot()
        try:
            transcriber = build_transcriber(cfg, self.logger)
            text = await asyncio.to_thread(transcriber.transcribe, result.path)
            transformer = build_text_transformer(cfg, self.logger)
            text = await asyncio.to_thread(transformer.transform, text)
            await asyncio.to_thread(type_text_macos, text, cfg.press_return, self.logger)
        except Exception as exc:
            self.logger.log(f"Transcription/type error: {exc}")
            self.state.update(last_error=str(exc))

    async def _sleep_or_shutdown(self, seconds: float) -> None:
        if seconds <= 0 or self._shutdown_event is None:
            return
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


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
    FieldSpec("stt_provider", "STT Provider", "choice", choices=STT_PROVIDER_CHOICES),
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
        self.status_message = "Ready"
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
        top_height = min(len(FIELD_SPECS) + 6, max(12, height // 2))
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
        if state.last_error:
            self._safe_addstr(3, 0, f"Last error: {state.last_error}"[: max(0, width - 1)], curses.A_BOLD)
        if state.last_config_change:
            self._safe_addstr(4, 0, f"Last config change: {state.last_config_change}"[: max(0, width - 1)])

        config_title = f"Config {scroll_offset + 1}-{min(len(FIELD_SPECS), scroll_offset + visible_fields)}/{len(FIELD_SPECS)}"
        self._safe_addstr(5, 0, config_title, curses.A_UNDERLINE)
        for visible_index, spec in enumerate(FIELD_SPECS[scroll_offset: scroll_offset + visible_fields]):
            index = scroll_offset + visible_index
            row = 6 + visible_index
            value = getattr(cfg, spec.name)
            display = self._format_field_value(spec.name, value)
            line = f"{spec.label:<18} {display}"
            attr = curses.A_REVERSE if index == self.selected_index else curses.A_NORMAL
            self._safe_addstr(row, 0, line[: max(0, config_width - 1)], attr)

        self._safe_addstr(5, config_width + 1, "Logs", curses.A_UNDERLINE)
        log_lines = self.logger.lines()
        visible_log_lines = max(1, height - 6)
        start = max(0, len(log_lines) - visible_log_lines)
        log_width = max(10, width - config_width - 2)
        for offset, line in enumerate(log_lines[start:]):
            row = 6 + offset
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
        if connection_changed:
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
            raw = self.stdscr.getstr(height - 1, min(len(prompt), max(0, width - 2)), max(1, width - len(prompt) - 1))
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
            if spec.name == "input_device":
                return None
            if spec.name == "whisper_api_url":
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


def install_signal_handlers(app: RecordControlApp) -> None:
    def handler(_signum, _frame) -> None:
        app.request_shutdown()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def run_app_thread(app: RecordControlApp, logger: LogBuffer, result: dict[str, int]) -> None:
    try:
        result["code"] = asyncio.run(app.run())
    except Exception as exc:
        logger.log(f"Background app crashed: {exc}")
        result["code"] = 1


def run_tui(config: RuntimeConfig) -> int:
    logger = LogBuffer(echo_stdout=False)
    state = AppState()
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

    if args.no_tui and args.input_device is None:
        try:
            args.input_device = select_input_device(
                channels=args.channels,
                sample_rate=args.sample_rate,
            )
        except AudioDeviceError as exc:
            print(exc, file=sys.stderr, flush=True)
            return 1

    config = RuntimeConfig.from_args(args)

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

#!/usr/bin/env python3
import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
import wave
from dataclasses import dataclass
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


def build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Subscribe to XIAO BLE record-control events, record from the Mac microphone, "
			    "transcribe with Groq Whisper, and type the result into the active app.",
	)
	parser.add_argument("--device-name", default=DEFAULT_DEVICE_NAME,
			    help=f"BLE peripheral name to scan for. Default: {DEFAULT_DEVICE_NAME}")
	parser.add_argument("--service-uuid", default=DEFAULT_SERVICE_UUID,
			    help="Custom record-control service UUID.")
	parser.add_argument("--char-uuid", default=DEFAULT_CHAR_UUID,
			    help="Record-control state characteristic UUID.")
	parser.add_argument("--stt-provider", choices=("groq", "vas", "whisper"), default=DEFAULT_STT_PROVIDER,
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
			    help="Optional sounddevice input device index or name fragment. Skips the startup prompt.")
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
	return parser


@dataclass
class RecordingResult:
	path: Path
	duration_s: float


class AudioDeviceError(RuntimeError):
	pass


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

	if input_device is None:
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
				"PortAudio has no valid default input device. Pass one explicitly with "
				"`--input-device <index-or-name>`.\n"
				+ format_input_devices()
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
				raise AudioDeviceError(
					f"Audio input device name {input_device!r} is ambiguous: {matched}"
				)
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
	def __init__(self, sample_rate: int, channels: int, input_device: Optional[str], recordings_dir: Path):
		self.sample_rate = sample_rate
		self.channels = channels
		self.input_device = input_device
		self.recordings_dir = recordings_dir
		self.stream: Optional[sd.RawInputStream] = None
		self.wave_file: Optional[wave.Wave_write] = None
		self.path: Optional[Path] = None
		self.started_at: Optional[float] = None
		self.lock = threading.Lock()

	def start(self) -> None:
		with self.lock:
			if self.stream is not None:
				print("Recorder already active; ignoring duplicate start", flush=True)
				return

			device_index = resolve_input_device(
				self.input_device,
				channels=self.channels,
				sample_rate=self.sample_rate,
			)
			device_info = sd.query_devices(device_index)
			self.recordings_dir.mkdir(parents=True, exist_ok=True)
			filename = time.strftime("recording-%Y%m%d-%H%M%S.wav")
			self.path = self.recordings_dir / filename
			self.wave_file = wave.open(str(self.path), "wb")
			self.wave_file.setnchannels(self.channels)
			self.wave_file.setsampwidth(2)
			self.wave_file.setframerate(self.sample_rate)

			try:
				self.stream = sd.RawInputStream(
					samplerate=self.sample_rate,
					channels=self.channels,
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
			print(f"Recording started from input {device_index} ({device_name}): {self.path}", flush=True)

	def stop(self) -> Optional[RecordingResult]:
		with self.lock:
			if self.stream is None or self.wave_file is None or self.path is None:
				print("Recorder already idle; ignoring duplicate stop", flush=True)
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
		print(f"Recording stopped: {path} ({duration_s:.2f}s)", flush=True)
		return RecordingResult(path=path, duration_s=duration_s)

	def _callback(self, indata, frames, time_info, status) -> None:
		del frames
		del time_info
		if status:
			print(f"Audio callback status: {status}", flush=True)

		with self.lock:
			if self.wave_file is not None:
				self.wave_file.writeframes(indata)


class GroqTranscriber(Transcriber):
	def __init__(self, model: str, language: str):
		api_key = os.environ.get("GROQ_API_KEY")
		if not api_key:
			raise RuntimeError("GROQ_API_KEY is not set")

		self.client = Groq(api_key=api_key)
		self.model = model
		self.language = language

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
		print(f"Groq transcription: {text!r}", flush=True)
		return text


class LocalWhisperTranscriber(Transcriber):
	def __init__(self, api_url: Optional[str], api_key: str, model: str, language: str):
		if not api_url:
			raise RuntimeError(
				"WHISPER_API_URL is not set. Provide --whisper-api-url or set WHISPER_API_URL."
			)

		self.api_url = api_url
		self.api_key = api_key
		self.model = model
		self.language = language

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
			raise RuntimeError(
				f"Local Whisper request failed with HTTP {exc.code}: {error_body}"
			) from exc
		except error.URLError as exc:
			raise RuntimeError(f"Local Whisper request failed: {exc}") from exc

		text = self._extract_text(payload)
		print(f"Local Whisper transcription: {text!r}", flush=True)
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
	):
		self.demo_dir = demo_dir
		self.addr = addr
		self.access_token = access_token
		self.model = model
		self.language = language
		self.no_refine = no_refine

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

		print(f"VAS transcription command: {' '.join(cmd)}", flush=True)
		result = subprocess.run(
			cmd,
			cwd=self.demo_dir,
			capture_output=True,
			text=True,
			check=True,
			env=env,
		)

		if result.stderr.strip():
			print(result.stderr.strip(), flush=True)

		payload = json.loads(result.stdout)
		text = (payload.get("final_text") or "").strip()
		print(f"VAS transcription: {text!r}", flush=True)
		return text


class GroqEnglishTranslator(TextTransformer):
	def __init__(self, model: str):
		api_key = os.environ.get("GROQ_API_KEY")
		if not api_key:
			raise RuntimeError("GROQ_API_KEY is not set; required for --translate-to-en")

		self.client = Groq(api_key=api_key)
		self.model = model

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
		print(
			f"Translation LLM request: {json.dumps({'model': self.model, 'messages': messages}, ensure_ascii=False)}",
			flush=True,
		)

		result = self.client.chat.completions.create(
			model=self.model,
			temperature=0,
			messages=messages,
		)
		print(
			f"Translation LLM response: {json.dumps(result.to_dict(), ensure_ascii=False)}",
			flush=True,
		)
		translated = (result.choices[0].message.content or "").strip()
		print(f"English translation: {translated!r}", flush=True)
		return translated


def build_transcriber(args: argparse.Namespace) -> Transcriber:
	if args.stt_provider == "groq":
		return GroqTranscriber(model=args.model, language=args.language)

	if args.stt_provider == "whisper":
		return LocalWhisperTranscriber(
			api_url=args.whisper_api_url,
			api_key=args.whisper_api_key,
			model=args.model,
			language=args.language,
		)

	if args.stt_provider == "vas":
		return VASTranscriber(
			demo_dir=Path(args.vas_demo_dir),
			addr=args.vas_addr,
			access_token=args.vas_access_token,
			model=args.vas_model,
			language=args.vas_language,
			no_refine=args.vas_no_refine,
		)

	raise RuntimeError(f"Unsupported STT provider: {args.stt_provider}")


def build_text_transformer(args: argparse.Namespace) -> TextTransformer:
	if args.translate_to_en:
		return GroqEnglishTranslator(model=args.translation_model)

	return IdentityTransformer()


def type_text_macos(text: str, press_return: bool) -> None:
	if not text:
		print("Transcription empty; nothing to type", flush=True)
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
	print("Pasted transcription into the active macOS app", flush=True)


class RecordControlApp:
	def __init__(self, args: argparse.Namespace):
		self.args = args
		self.recorder = AudioRecorder(
			sample_rate=args.sample_rate,
			channels=args.channels,
			input_device=args.input_device,
			recordings_dir=Path(args.recordings_dir),
		)
		self.transcriber = build_transcriber(args)
		self.text_transformer = build_text_transformer(args)
		self.recording_lock = asyncio.Lock()
		self.shutdown_event = asyncio.Event()
		self.currently_recording = False
		self.loop: Optional[asyncio.AbstractEventLoop] = None

	def request_shutdown(self) -> None:
		self.shutdown_event.set()

	async def run(self) -> None:
		self.loop = asyncio.get_running_loop()

		while not self.shutdown_event.is_set():
			device = await self._find_device()
			if device is None:
				await self._sleep_or_shutdown(self.args.retry_delay)
				continue

			disconnected = asyncio.Event()

			def on_disconnect(_client: BleakClient) -> None:
				print("BLE disconnected", flush=True)
				disconnected.set()

			try:
				async with BleakClient(device, disconnected_callback=on_disconnect) as client:
					print(f"Connected to {device.name or device.address}", flush=True)
					await client.start_notify(self.args.char_uuid, self._handle_notification)
					print("Subscribed to record-control notifications", flush=True)

					done, _ = await asyncio.wait(
						[
							asyncio.create_task(disconnected.wait()),
							asyncio.create_task(self.shutdown_event.wait()),
						],
						return_when=asyncio.FIRST_COMPLETED,
					)
					for task in done:
						task.result()

					with contextlib.suppress(Exception):
						await client.stop_notify(self.args.char_uuid)
			except Exception as exc:
				print(f"BLE session error: {exc}", flush=True)

			await self._sleep_or_shutdown(self.args.retry_delay)

	async def _find_device(self):
		print(f"Scanning for BLE device {self.args.device_name!r}...", flush=True)
		devices = await BleakScanner.discover(timeout=self.args.scan_timeout)
		for device in devices:
			if device.name == self.args.device_name:
				print(f"Found device: {device.name} ({device.address})", flush=True)
				return device

		print("Device not found in this scan window", flush=True)
		return None

	def _handle_notification(self, _sender: int, data: bytearray) -> None:
		if not data:
			return

		value = data[0]
		if self.loop is None:
			return

		if value == RECORD_STATE_ACTIVE:
			self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self._start_recording()))
		elif value == RECORD_STATE_IDLE:
			self.loop.call_soon_threadsafe(
				lambda: asyncio.create_task(self._stop_recording_and_transcribe())
			)
		else:
			print(f"Ignoring unknown record-control value: {value}", flush=True)

	async def _start_recording(self) -> None:
		async with self.recording_lock:
			if self.currently_recording:
				return

			try:
				self.recorder.start()
			except AudioDeviceError as exc:
				print(f"Audio input error: {exc}", flush=True)
				return
			except Exception as exc:
				print(f"Recording start error: {exc}", flush=True)
				return

			self.currently_recording = True

	async def _stop_recording_and_transcribe(self) -> None:
		async with self.recording_lock:
			if not self.currently_recording:
				return

			result = self.recorder.stop()
			self.currently_recording = False

		if result is None:
			return

		if result.duration_s < 0.15:
			print("Recording too short; skipping transcription", flush=True)
			return

		try:
			text = await asyncio.to_thread(self.transcriber.transcribe, result.path)
			text = await asyncio.to_thread(self.text_transformer.transform, text)
			await asyncio.to_thread(type_text_macos, text, self.args.press_return)
		except Exception as exc:
			print(f"Transcription/type error: {exc}", flush=True)

	async def _sleep_or_shutdown(self, seconds: float) -> None:
		if seconds <= 0:
			return
		try:
			await asyncio.wait_for(self.shutdown_event.wait(), timeout=seconds)
		except asyncio.TimeoutError:
			pass


def install_signal_handlers(app: RecordControlApp) -> None:
	def handler(_signum, _frame) -> None:
		app.request_shutdown()

	signal.signal(signal.SIGINT, handler)
	signal.signal(signal.SIGTERM, handler)


async def main_async() -> int:
	parser = build_arg_parser()
	args = parser.parse_args()

	if args.list_input_devices:
		try:
			print(format_input_devices(), flush=True)
		except AudioDeviceError as exc:
			print(exc, file=sys.stderr, flush=True)
			return 1
		return 0

	if args.input_device is None:
		try:
			args.input_device = select_input_device(
				channels=args.channels,
				sample_rate=args.sample_rate,
			)
		except AudioDeviceError as exc:
			print(exc, file=sys.stderr, flush=True)
			return 1

	app = RecordControlApp(args)
	install_signal_handlers(app)
	await app.run()
	return 0


def main() -> int:
	try:
		return asyncio.run(main_async())
	except KeyboardInterrupt:
		return 130
	except RuntimeError as exc:
		print(exc, file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())

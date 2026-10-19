#!/usr/bin/env python3
"""KN20A scheduled RTSP/RTMP recorder and image-based exposure controller.

FFmpeg stream-copies video/audio into MKV segments, then remuxes a completed
recording into one MP4. The optional exposure controller uses the substream.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import math
import mimetypes
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional


APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
CONFIG_PATH = APP_DIR / "config.json"
SCHEDULE_PATH = APP_DIR / "schedules.json"
PRESET_PATH = APP_DIR / "presets.json"
LOG_DIR = APP_DIR / "logs"
EXPOSURE_GUARD_PATH = APP_DIR / "data" / "exposure_guard.json"
JST = timezone(timedelta(hours=9), "JST")
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class WindowsKillOnCloseJob:
    """Own child processes so Windows kills them if this process disappears."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        pass

    _ExtendedLimitInformation._fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]

    def __init__(self) -> None:
        self.handle: Optional[int] = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = self._ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        success = kernel32.SetInformationJobObject(
            ctypes.c_void_p(handle),
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not success:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise ctypes.WinError(error)
        self.handle = int(handle)

    def assign(self, process: subprocess.Popen) -> None:
        if self.handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        success = kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(self.handle), ctypes.c_void_p(int(process._handle))
        )
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is not None:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(handle))


DEFAULT_CONFIG: dict[str, Any] = {
    "camera_ip": "192.0.2.10",
    "visca_port": 5678,
    "input_mode": "rtsp",
    "input_url": "rtsp://192.0.2.10:554/live/av0",
    "analysis_url": "rtsp://192.0.2.10:554/live/av1",
    "analysis_ws_url": "ws://192.0.2.10:8088/live/av1",
    "analysis_width": 320,
    "analysis_height": 180,
    "analysis_sample_seconds": 4,
    "listen_url": "rtmp://0.0.0.0:1935/live/av0",
    "ffmpeg_path": "ffmpeg",
    "output_dir": "recordings",
    "segment_seconds": 600,
    "postprocess_mp4": True,
    "keep_mkv_after_remux": False,
    "publisher_wait_seconds": 30,
    "rtsp_timeout_microseconds": 5000000,
    "retry_seconds": 5,
    "analysis_retry_max_seconds": 300,
    "analysis_stable_seconds": 120,
    "analysis_failure_limit": 8,
    "analysis_circuit_breaker_seconds": 900,
    "preset_prepare_seconds": 15,
    "preset_settle_seconds": 2,
    "estimated_bitrate_mbps": 3.5,
    "storage_safety_factor": 1.15,
    "minimum_free_bytes": 2147483648,
    "web_host": "127.0.0.1",
    "web_port": 8765,
}

MODE_LABELS = {
    0: "Auto（完全自動）",
    3: "Manual（手動）",
    10: "SAE（シャッター優先）",
    11: "AAE（絞り優先）",
    13: "Bright（明るさ優先）",
}

IRIS_LABELS = {
    1: "F11.0",
    2: "F9.6",
    3: "F8.0",
    4: "F6.8",
    5: "F5.6",
    6: "F4.8",
    7: "F4.0",
    8: "F3.4",
    9: "F2.8",
    10: "F2.4",
    11: "F2.0",
    12: "F1.8",
}


def now_jst() -> datetime:
    return datetime.now(JST)


def parse_datetime(value: str) -> datetime:
    """Parse an HTML datetime-local/ISO value and normalize it to JST."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("日時が空です")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("日時の形式が正しくありません") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    else:
        parsed = parsed.astimezone(JST)
    return parsed.replace(microsecond=0)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp_path, path)


def safe_title(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(value)).strip(" ._")
    value = re.sub(r"[\s_]+", "_", value)
    return value[:60] or "recording"


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config.json must contain an object")
        config.update(loaded)
    else:
        atomic_write_json(CONFIG_PATH, config)
    config["ffmpeg_path"] = resolve_executable(str(config["ffmpeg_path"]))
    output_dir = Path(config["output_dir"])
    if not output_dir.is_absolute():
        output_dir = APP_DIR / output_dir
    config["output_dir_resolved"] = str(output_dir.resolve())
    return config


def resolve_executable(value: str) -> str:
    """Accept an absolute/relative executable path or a command on PATH."""
    found = shutil.which(value)
    if found:
        return str(Path(found).resolve())
    return str(Path(value).expanduser())


def ffprobe_path(config: dict[str, Any]) -> Path:
    ffmpeg = Path(config["ffmpeg_path"])
    name = "ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe"
    sibling = ffmpeg.with_name(name)
    if sibling.is_file():
        return sibling
    return Path(resolve_executable(name))


def handle_termination(signum: int, frame: Any) -> None:
    """Let normal shutdown close recordings and reap analyzer children."""
    raise KeyboardInterrupt


class PresetStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.items: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        with self.lock:
            if not self.path.exists():
                self.save()
                return
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, list):
                raise ValueError("presets.json must contain an array")
            seen: set[int] = set()
            for item in value:
                if not isinstance(item, dict):
                    raise ValueError("presets.json contains a non-object item")
                number = int(item.get("number", -1))
                if not 0 <= number <= 254 or number in seen:
                    raise ValueError("presets.json contains an invalid preset number")
                seen.add(number)
                item["number"] = number
                item["name"] = str(item.get("name", "")).strip()[:60] or f"プリセット {number}"
            self.items = value

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, self.items)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(item) for item in sorted(self.items, key=lambda item: item["number"])]

    def get(self, number: int) -> Optional[dict[str, Any]]:
        with self.lock:
            return next((dict(item) for item in self.items if item["number"] == number), None)

    def upsert(self, number: int, name: str) -> dict[str, Any]:
        number = int(number)
        if not 0 <= number <= 254:
            raise ValueError("プリセット番号は0〜254で指定してください")
        clean_name = str(name).strip()[:60] or f"プリセット {number}"
        with self.lock:
            existing = next((item for item in self.items if item["number"] == number), None)
            values = {
                "number": number,
                "name": clean_name,
                "saved_at": now_jst().isoformat(),
            }
            if existing is None:
                self.items.append(values)
                result = values
            else:
                existing.update(values)
                result = existing
            self.save()
            return dict(result)

    def remove(self, number: int) -> bool:
        with self.lock:
            before = len(self.items)
            self.items = [item for item in self.items if item["number"] != int(number)]
            if len(self.items) == before:
                return False
            self.save()
            return True


class ScheduleStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.items: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        with self.lock:
            if self.path.exists():
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(value, list):
                    raise ValueError("schedules.json must contain an array")
                self.items = value
            else:
                self.items = []
                self.save()
            changed = False
            for item in self.items:
                if not isinstance(item, dict):
                    raise ValueError("schedules.json contains a non-object item")
                required = {"id", "title", "start", "end", "status"}
                if not required.issubset(item):
                    raise ValueError("schedules.json contains an item with missing fields")
                if not re.fullmatch(r"[0-9a-f]{12}", str(item["id"])):
                    raise ValueError("schedules.json contains an invalid id")
                start = parse_datetime(str(item["start"]))
                end = parse_datetime(str(item["end"]))
                if end <= start:
                    raise ValueError("schedules.json contains an invalid date range")
                if "preset_number" not in item:
                    item["preset_number"] = None
                    changed = True
                elif item["preset_number"] is not None:
                    item["preset_number"] = int(item["preset_number"])
                    if not 0 <= item["preset_number"] <= 254:
                        raise ValueError("schedules.json contains an invalid preset number")
                if item.get("status") in {"recording", "starting"}:
                    item["status"] = "pending"
                    changed = True
            if changed:
                self.save()

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, self.items)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(item) for item in sorted(self.items, key=lambda x: x["start"])]

    def get(self, schedule_id: str) -> Optional[dict[str, Any]]:
        with self.lock:
            return next((item for item in self.items if item["id"] == schedule_id), None)

    def create(
        self,
        title: str,
        start_value: str,
        end_value: str,
        preset_number: Optional[int] = None,
    ) -> dict[str, Any]:
        start = parse_datetime(start_value)
        end = parse_datetime(end_value)
        current = now_jst()
        if end <= start:
            raise ValueError("終了日時は開始日時より後にしてください")
        if end <= current:
            raise ValueError("終了日時がすでに過ぎています")
        if end - start > timedelta(days=7):
            raise ValueError("1件の録画は7日以内にしてください")
        if preset_number is not None:
            preset_number = int(preset_number)
            if not 0 <= preset_number <= 254:
                raise ValueError("プリセット番号は0〜254で指定してください")

        with self.lock:
            for item in self.items:
                if item.get("status") in {"cancelled", "completed", "missed", "failed"}:
                    continue
                other_start = parse_datetime(item["start"])
                other_end = parse_datetime(item["end"])
                if start < other_end and end > other_start:
                    raise ValueError(f"予約「{item['title']}」と時間が重なっています")
            item = {
                "id": uuid.uuid4().hex[:12],
                "title": str(title).strip()[:80] or "無題の録画",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "status": "pending",
                "preset_number": preset_number,
                "created_at": current.isoformat(),
                "attempts": 0,
                "last_error": "",
                "output_dir": "",
            }
            self.items.append(item)
            self.save()
            return dict(item)

    def update(self, schedule_id: str, **values: Any) -> Optional[dict[str, Any]]:
        with self.lock:
            item = self.get(schedule_id)
            if item is None:
                return None
            item.update(values)
            self.save()
            return dict(item)

    def cancel(self, schedule_id: str) -> Optional[dict[str, Any]]:
        with self.lock:
            item = self.get(schedule_id)
            if item is None or item.get("status") not in {"pending", "starting", "recording"}:
                return None
            return self.update(schedule_id, status="cancelled", cancelled_at=now_jst().isoformat())


@dataclass
class RecorderSnapshot:
    state: str = "idle"
    schedule_id: str = ""
    pid: Optional[int] = None
    attempt: int = 0
    output_dir: str = ""
    bytes_written: int = 0
    last_error: str = ""
    started_at: str = ""


class RecorderManager:
    def __init__(self, config: dict[str, Any], store: ScheduleStore, camera: ExposureClient):
        self.config = config
        self.store = store
        self.camera = camera
        self.lock = threading.RLock()
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.log_handle: Optional[Any] = None
        self.active_id = ""
        self.run_dir: Optional[Path] = None
        self.attempt = 0
        self.started_at = ""
        self.last_error = ""
        self.next_retry_at = 0.0
        self.next_preset_retry_at: dict[str, float] = {}
        self.prepared_presets: dict[str, int] = {}
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.finalize_queue: queue.Queue[str] = queue.Queue()
        self.queued_finalizations: set[str] = set()
        self.finalizing_id = ""
        self.finalizer_thread = threading.Thread(
            target=self._finalizer_loop, name="mp4-finalizer", daemon=True
        )

    def start(self) -> None:
        Path(self.config["output_dir_resolved"]).mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        for item in self.store.list():
            if item.get("status") in {"finalizing", "postprocess_failed"}:
                self._queue_finalization(item["id"])
        self.finalizer_thread.start()
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        self._stop_process(graceful=True)
        self.thread.join(timeout=5)
        self.finalizer_thread.join(timeout=5)

    def _queue_finalization(self, schedule_id: str) -> None:
        with self.lock:
            if schedule_id in self.queued_finalizations or self.finalizing_id == schedule_id:
                return
            self.queued_finalizations.add(schedule_id)
        self.store.update(schedule_id, status="finalizing", last_error="")
        self.finalize_queue.put(schedule_id)

    def _finalizer_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                schedule_id = self.finalize_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            with self.lock:
                self.queued_finalizations.discard(schedule_id)
                self.finalizing_id = schedule_id
            try:
                self._remux_schedule(schedule_id)
            except Exception as exc:
                logging.exception("MP4 remux failed schedule=%s", schedule_id)
                self.store.update(
                    schedule_id,
                    status="postprocess_failed",
                    last_error=f"MP4変換失敗: {exc}",
                )
            finally:
                with self.lock:
                    self.finalizing_id = ""
                self.finalize_queue.task_done()

    def _valid_mp4(self, path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        ffprobe = ffprobe_path(self.config)
        if not ffprobe.is_file():
            return False
        result = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=60,
            check=False,
        )
        try:
            return result.returncode == 0 and float(result.stdout.strip()) > 0
        except (TypeError, ValueError):
            return False

    def _remux_schedule(self, schedule_id: str) -> None:
        item = self.store.get(schedule_id)
        if item is None:
            raise RuntimeError("予約が見つかりません")
        output_dir = str(item.get("output_dir", "")).strip()
        if not output_dir:
            raise RuntimeError("録画フォルダが記録されていません")
        output_root = Path(self.config["output_dir_resolved"]).resolve()
        run_dir = Path(output_dir).resolve()
        if output_root not in run_dir.parents:
            raise RuntimeError("録画フォルダが保存先の外側です")
        if not run_dir.is_dir():
            raise RuntimeError("録画フォルダが見つかりません")
        mkv_sources = sorted(run_dir.glob("*.mkv"))
        legacy_parts = sorted(run_dir.glob("part_*.mp4"))
        target = run_dir / "recording.mp4"
        inputs = mkv_sources if mkv_sources else legacy_parts
        if self._valid_mp4(target):
            if not bool(self.config.get("keep_mkv_after_remux", False)):
                for source in mkv_sources:
                    source.unlink(missing_ok=True)
            for source in legacy_parts:
                source.unlink(missing_ok=True)
            self.store.update(
                schedule_id,
                status="completed",
                completed_at=now_jst().isoformat(),
                mp4_files=[str(target)],
                last_error="",
            )
            return
        if not inputs:
            raise RuntimeError("結合対象のMKV/MP4がありません")

        temporary = run_dir / "recording.mp4.partial"
        concat_list = run_dir / "recording.ffconcat"
        temporary.unlink(missing_ok=True)
        concat_list.unlink(missing_ok=True)
        ffmpeg_path = Path(self.config["ffmpeg_path"])
        log_path = LOG_DIR / f"remux_{schedule_id}.log"
        with log_path.open("ab", buffering=0) as log_handle:
            input_args: list[str]
            if len(inputs) == 1:
                input_args = ["-i", str(inputs[0])]
            else:
                concat_lines = ["ffconcat version 1.0"] + [
                    f"file '{source.name.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
                    for source in inputs
                ]
                concat_list.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
                input_args = ["-f", "concat", "-safe", "0", "-i", str(concat_list)]
            args = [
                str(ffmpeg_path),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "info",
                "-y",
                *input_args,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(temporary),
            ]
            try:
                result = subprocess.run(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log_handle,
                    cwd=str(APP_DIR),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            finally:
                concat_list.unlink(missing_ok=True)
            if result.returncode != 0 or not self._valid_mp4(temporary):
                temporary.unlink(missing_ok=True)
                raise RuntimeError("分割録画を1本のMP4へ結合できませんでした")
            os.replace(temporary, target)

        if not bool(self.config.get("keep_mkv_after_remux", False)):
            for source in mkv_sources:
                source.unlink(missing_ok=True)
        for source in legacy_parts:
            source.unlink(missing_ok=True)

        self.store.update(
            schedule_id,
            status="completed",
            completed_at=now_jst().isoformat(),
            mp4_files=[str(target)],
            last_error="",
        )

    def _active_schedule(self, current: datetime) -> Optional[dict[str, Any]]:
        for item in self.store.list():
            status = item.get("status")
            start = parse_datetime(item["start"])
            end = parse_datetime(item["end"])
            if status == "pending" and current >= end:
                self.store.update(item["id"], status="missed", last_error="終了時刻までサービスが起動していませんでした")
                continue
            if status in {"pending", "starting", "recording"} and start <= current < end:
                return item
        return None

    def _scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logging.exception("scheduler tick failed")
            self.stop_event.wait(1.0)

    def _upcoming_preset_schedule(self, current: datetime) -> Optional[dict[str, Any]]:
        prepare_seconds = int(self.config.get("preset_prepare_seconds", 15))
        for item in self.store.list():
            if item.get("status") != "pending" or item.get("preset_number") is None:
                continue
            start = parse_datetime(item["start"])
            if current < start <= current + timedelta(seconds=prepare_seconds):
                return item
        return None

    def _prepare_preset(self, item: dict[str, Any]) -> bool:
        preset = item.get("preset_number")
        if preset is None:
            return True
        preset = int(preset)
        if self.prepared_presets.get(item["id"]) == preset:
            return True
        if time.monotonic() < self.next_preset_retry_at.get(item["id"], 0.0):
            return False
        try:
            self.camera.recall_preset(preset)
            settle_seconds = float(self.config.get("preset_settle_seconds", 2))
            if self.stop_event.wait(max(0.0, settle_seconds)):
                return False
        except Exception as exc:
            self.last_error = f"プリセット{preset}の呼出待ち: {exc}"
            self.next_preset_retry_at[item["id"]] = time.monotonic() + int(self.config["retry_seconds"])
            self.store.update(item["id"], last_error=self.last_error)
            logging.warning("preset recall failed schedule=%s preset=%s: %s", item["id"], preset, exc)
            return False
        self.prepared_presets[item["id"]] = preset
        self.next_preset_retry_at.pop(item["id"], None)
        self.last_error = ""
        self.store.update(
            item["id"],
            last_error="",
            preset_recalled_at=now_jst().isoformat(),
        )
        logging.info("preset recalled schedule=%s preset=%s", item["id"], preset)
        return True

    def _tick(self) -> None:
        current = now_jst()
        active = self._active_schedule(current)
        if active is None:
            upcoming = self._upcoming_preset_schedule(current)
            if upcoming is not None:
                self._prepare_preset(upcoming)
        with self.lock:
            process = self.process
            active_id = self.active_id

        if process is not None:
            exit_code = process.poll()
            if exit_code is not None:
                self._finish_process(exit_code)
                process = None

        if active is None:
            if process is not None:
                self._stop_process(graceful=True)
            return

        if process is not None and active_id != active["id"]:
            self._stop_process(graceful=True)

        if self.process is None and time.monotonic() >= self.next_retry_at:
            if self._prepare_preset(active):
                self._start_process(active)
        elif self.process is not None:
            self._update_recording_state(active["id"])

    def _start_process(self, item: dict[str, Any]) -> None:
        ffmpeg_path = Path(self.config["ffmpeg_path"])
        if not ffmpeg_path.is_file():
            self.last_error = f"ffmpegが見つかりません: {ffmpeg_path}"
            self.store.update(item["id"], status="failed", last_error=self.last_error)
            return

        remaining_seconds = max(0, (parse_datetime(item["end"]) - now_jst()).total_seconds())
        output_root = Path(self.config["output_dir_resolved"])
        free_bytes = shutil.disk_usage(output_root).free
        estimated_bitrate, estimate_source = self._estimate_bitrate_mbps(output_root)
        estimated_media_bytes = int(remaining_seconds * estimated_bitrate * 1_000_000 / 8)
        safety_factor = float(self.config.get("storage_safety_factor", 1.15))
        estimated_bytes = int(estimated_media_bytes * safety_factor)
        reserve = int(self.config["minimum_free_bytes"])
        if free_bytes < estimated_bytes + reserve:
            self.last_error = (
                f"空き容量不足: 録画見込 {estimated_media_bytes / 1_000_000_000:.1f}GB "
                f"({estimated_bitrate:.2f}Mbps・{estimate_source}) + 安全余裕 "
                f"{round((safety_factor - 1) * 100):d}% + 予備 "
                f"{reserve / 1_000_000_000:.1f}GB"
            )
            self.store.update(
                item["id"],
                status="failed",
                last_error=self.last_error,
                estimated_bitrate_mbps=round(estimated_bitrate, 3),
                estimated_size_bytes=estimated_media_bytes,
                estimate_source=estimate_source,
            )
            return

        self.attempt = int(item.get("attempts", 0)) + 1
        start_stamp = now_jst().strftime("%Y%m%d_%H%M%S")
        folder_name = f"{parse_datetime(item['start']).strftime('%Y%m%d_%H%M')}_{safe_title(item['title'])}_{item['id']}"
        run_dir = Path(self.config["output_dir_resolved"]) / folder_name
        run_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = run_dir / f"part_{self.attempt:02d}_%Y%m%d_%H%M%S.mkv"
        log_path = LOG_DIR / f"ffmpeg_{item['id']}_{self.attempt:02d}_{start_stamp}.log"

        args = [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "info",
        ]
        if self.config.get("input_mode") == "rtsp":
            args.extend(
                [
                    "-rtsp_transport",
                    "tcp",
                    "-timeout",
                    str(int(self.config["rtsp_timeout_microseconds"])),
                    "-i",
                    str(self.config["input_url"]),
                ]
            )
        elif self.config.get("input_mode") == "rtmp-listen":
            args.extend(
                [
                    "-rtmp_listen",
                    "1",
                    "-timeout",
                    str(int(self.config["publisher_wait_seconds"])),
                    "-i",
                    str(self.config["listen_url"]),
                ]
            )
        else:
            raise ValueError("input_mode must be 'rtsp' or 'rtmp-listen'")
        args.extend([
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(int(self.config["segment_seconds"])),
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            "-y",
            str(output_pattern),
        ])

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.log_handle = log_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self.log_handle,
                cwd=str(APP_DIR),
                creationflags=creationflags,
            )
        except Exception as exc:
            self.log_handle.close()
            self.log_handle = None
            self.last_error = str(exc)
            self.next_retry_at = time.monotonic() + int(self.config["retry_seconds"])
            self.store.update(item["id"], status="starting", last_error=self.last_error)
            return

        with self.lock:
            self.process = process
            self.active_id = item["id"]
            self.run_dir = run_dir
            self.started_at = now_jst().isoformat()
            self.last_error = ""
        self.store.update(
            item["id"],
            status="starting",
            attempts=self.attempt,
            last_error="",
            output_dir=str(run_dir),
            started_at=self.started_at,
            estimated_bitrate_mbps=round(estimated_bitrate, 3),
            estimated_size_bytes=estimated_media_bytes,
            estimate_source=estimate_source,
        )

    def _probe_duration_seconds(self, path: Path) -> Optional[float]:
        ffprobe = ffprobe_path(self.config)
        if not ffprobe.is_file():
            return None
        try:
            result = subprocess.run(
                [
                    str(ffprobe),
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=30,
                check=False,
            )
            duration = float(result.stdout.strip())
            return duration if result.returncode == 0 and duration >= 10 else None
        except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
            return None

    def _estimate_bitrate_mbps(self, output_root: Path) -> tuple[float, str]:
        fallback = float(self.config.get("estimated_bitrate_mbps", 3.5))
        try:
            candidates = sorted(
                (
                    path
                    for path in output_root.rglob("*")
                    if path.is_file() and path.suffix.lower() in {".mkv", ".mp4"}
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return fallback, "設定値"
        measured: list[float] = []
        for path in candidates[:20]:
            duration = self._probe_duration_seconds(path)
            if duration is None:
                continue
            try:
                bitrate = path.stat().st_size * 8 / duration / 1_000_000
            except OSError:
                continue
            if 0.2 <= bitrate <= 200:
                measured.append(bitrate)
            if len(measured) >= 5:
                break
        if not measured:
            return fallback, "設定値"
        return float(statistics.median(measured)), f"直近実績{len(measured)}本"

    def _run_size(self) -> int:
        if self.run_dir is None or not self.run_dir.exists():
            return 0
        try:
            return sum(path.stat().st_size for path in self.run_dir.glob("*.mkv"))
        except OSError:
            return 0

    def _update_recording_state(self, schedule_id: str) -> None:
        size = self._run_size()
        item = self.store.get(schedule_id)
        if size > 0 and item and item.get("status") != "recording":
            self.store.update(schedule_id, status="recording")

    def _finish_process(self, exit_code: int) -> None:
        with self.lock:
            schedule_id = self.active_id
            self.process = None
            self.active_id = ""
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
        if schedule_id:
            self.prepared_presets.pop(schedule_id, None)
            self.next_preset_retry_at.pop(schedule_id, None)
            item = self.store.get(schedule_id)
            if item and item.get("status") != "cancelled":
                if parse_datetime(item["end"]) <= now_jst():
                    if self._run_size() > 0:
                        if bool(self.config.get("postprocess_mp4", True)):
                            self.store.update(
                                schedule_id,
                                recording_completed_at=now_jst().isoformat(),
                            )
                            self._queue_finalization(schedule_id)
                        else:
                            self.store.update(
                                schedule_id,
                                status="completed",
                                completed_at=now_jst().isoformat(),
                            )
                    else:
                        self.last_error = "映像を一度も受信できませんでした"
                        self.store.update(schedule_id, status="failed", last_error=self.last_error)
                else:
                    self.last_error = f"ffmpegが終了しました (code={exit_code})。再接続します"
                    self.store.update(schedule_id, status="starting", last_error=self.last_error)
                    base_retry = int(self.config["retry_seconds"])
                    retry_delay = min(60, base_retry * (2 ** min(max(self.attempt - 1, 0), 4)))
                    self.next_retry_at = time.monotonic() + retry_delay

    def _stop_process(self, graceful: bool) -> None:
        with self.lock:
            process = self.process
        if process is None:
            return
        if process.poll() is None and graceful and process.stdin:
            try:
                process.stdin.write(b"q\n")
                process.stdin.flush()
                process.wait(timeout=10)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._finish_process(process.returncode or 0)

    def cancel(self, schedule_id: str) -> bool:
        item = self.store.cancel(schedule_id)
        if item is None:
            return False
        with self.lock:
            is_active = self.active_id == schedule_id
        if is_active:
            self._stop_process(graceful=True)
        return True

    def stop_active_recording(self) -> Optional[dict[str, Any]]:
        """End the active schedule early, close MKV, then run normal MP4 finalization."""
        with self.lock:
            schedule_id = self.active_id
            process = self.process
        if not schedule_id or process is None:
            return None
        item = self.store.get(schedule_id)
        if item is None:
            return None
        stopped_at = now_jst()
        self.store.update(
            schedule_id,
            status="stopping",
            planned_end=item["end"],
            end=stopped_at.isoformat(),
            manually_stopped_at=stopped_at.isoformat(),
            last_error="",
        )
        self._stop_process(graceful=True)
        return self.store.get(schedule_id)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            process = self.process
            schedule_id = self.active_id
            run_dir = str(self.run_dir) if self.run_dir else ""
            started_at = self.started_at
            last_error = self.last_error
            attempt = self.attempt
            finalizing_id = self.finalizing_id
        size = self._run_size()
        state = "idle"
        if process and process.poll() is None:
            state = "recording" if size > 0 else "waiting_for_camera"
        elif finalizing_id:
            state = "finalizing"
            schedule_id = finalizing_id
        return RecorderSnapshot(
            state=state,
            schedule_id=schedule_id,
            pid=process.pid if process and process.poll() is None else None,
            attempt=attempt,
            output_dir=run_dir,
            bytes_written=size,
            last_error=last_error,
            started_at=started_at,
        ).__dict__


class ExposureClient:
    def __init__(self, camera_ip: str, visca_port: int):
        self.camera_ip = camera_ip
        self.visca_port = int(visca_port)
        self.lock = threading.RLock()
        # The Codex/runtime environment may define an outbound HTTP proxy.  A
        # private LAN camera must be contacted directly.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _http_parameters(self) -> dict[str, Any]:
        command = json.dumps(
            {"GetEnv": {"VideoParam": {"nChannel": -1}}}, separators=(",", ":")
        )
        url = f"http://{self.camera_ip}/ajaxcom?" + urllib.parse.urlencode({"szCmd": command})
        request = urllib.request.Request(url, headers={"User-Agent": "KN20A-Recorder/1.0"})
        with self.opener.open(request, timeout=4) as response:
            value = json.loads(response.read().decode("utf-8-sig"))
        if value.get("nRetVal") != 0 or not value.get("stValue"):
            raise RuntimeError(f"カメラ応答エラー: {value.get('nRetVal')}")
        return value["stValue"][0]

    @staticmethod
    def _named_value(container: dict[str, Any], list_key: str, value_key: str) -> dict[str, Any]:
        current = int(container[value_key])
        options = [
            {"value": int(entry["nValue"]), "label": str(entry["szName"])}
            for entry in container[list_key]
        ]
        label = next((entry["label"] for entry in options if entry["value"] == current), str(current))
        return {"value": current, "label": label, "options": options}

    def get(self) -> dict[str, Any]:
        with self.lock:
            params = self._http_parameters()
            exp = params["stExp"]
            mode_value = int(exp["stExpMode"]["emExpMode"])
            modes = [
                {"value": int(value), "label": MODE_LABELS.get(int(value), str(value))}
                for value in exp["stExpMode"]["byExpModeList"]
            ]
            return {
                "camera_ip": self.camera_ip,
                "mode": {
                    "value": mode_value,
                    "label": MODE_LABELS.get(mode_value, str(mode_value)),
                    "options": modes,
                },
                "iris": {
                    **self._named_value(exp["stIris"], "stIrisList", "nIris"),
                    "actual": mode_value in {3, 11},
                },
                "shutter": self._named_value(exp["stShutter"], "stShutterList", "nShutter"),
                "gain_limit": int(exp.get("gainLimit", 0)),
                "gain": int(exp.get("again", 0)),
                "drc": int(exp.get("drc", 0)),
                "antiflicker": int(exp.get("antiflicker", 0)),
            }

    def _visca(self, command: bytes, completion_timeout: float = 2.0) -> list[bytes]:
        frames: list[bytes] = []
        with socket.create_connection((self.camera_ip, self.visca_port), timeout=3) as sock:
            sock.settimeout(1.5)
            sock.sendall(command)
            buffer = bytearray()
            deadline = time.monotonic() + max(0.5, float(completion_timeout))
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(64)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buffer.extend(chunk)
                while 0xFF in buffer:
                    index = buffer.index(0xFF)
                    frames.append(bytes(buffer[: index + 1]))
                    del buffer[: index + 1]
                    if len(frames[-1]) > 1:
                        response_type = frames[-1][1] & 0xF0
                        if response_type == 0x60:
                            raise RuntimeError("カメラがVISCAコマンドを拒否しました")
                        if response_type == 0x50:
                            return frames
        if not frames:
            raise RuntimeError("VISCA応答がありません")
        raise RuntimeError("VISCA完了応答がありません")

    @staticmethod
    def _validate_preset(number: int) -> int:
        number = int(number)
        if not 0 <= number <= 254:
            raise ValueError("プリセット番号は0〜254で指定してください")
        return number

    def save_preset(self, number: int) -> None:
        number = self._validate_preset(number)
        with self.lock:
            self._visca(bytes([0x81, 0x01, 0x04, 0x3F, 0x01, number, 0xFF]), completion_timeout=5)

    def recall_preset(self, number: int) -> None:
        number = self._validate_preset(number)
        with self.lock:
            self._visca(bytes([0x81, 0x01, 0x04, 0x3F, 0x02, number, 0xFF]), completion_timeout=15)

    def _set_shutter_direct(self, shutter: int) -> None:
        self._visca(
            bytes([0x81, 0x01, 0x04, 0x4A, 0x00, 0x00, (shutter >> 4) & 0x0F, shutter & 0x0F, 0xFF])
        )

    def _set_gain_direct(self, gain: int) -> None:
        self._visca(bytes([0x81, 0x01, 0x04, 0x4C, 0x00, 0x00, 0x00, gain, 0xFF]))

    def set(
        self,
        mode: int,
        iris: Optional[int],
        shutter: Optional[int],
        gain_limit: Optional[int],
        gain: Optional[int] = None,
    ) -> dict[str, Any]:
        if mode not in MODE_LABELS:
            raise ValueError("未対応の露出モードです")
        if iris is not None and not 0 <= iris <= 12:
            raise ValueError("絞り値が範囲外です")
        if shutter is not None and not 0 <= shutter <= 33:
            raise ValueError("シャッター値が範囲外です")
        if gain_limit is not None and not 0 <= gain_limit <= 15:
            raise ValueError("ゲイン上限が範囲外です")
        if gain is not None and not 0 <= gain <= 15:
            raise ValueError("ゲイン値が範囲外です")

        with self.lock:
            before = self.get()
            valid_shutters = {int(item["value"]) for item in before["shutter"]["options"]}
            if shutter is not None and shutter not in valid_shutters:
                raise ValueError("カメラが対応していないシャッター値です")
            apply_fixed_iris = iris is not None and mode in {3, 11}
            apply_fixed_shutter = shutter is not None and mode in {3, 10}
            apply_fixed_gain = gain is not None and mode == 3
            self._visca(bytes([0x81, 0x01, 0x04, 0x39, mode, 0xFF]))
            if apply_fixed_iris:
                self._visca(bytes([0x81, 0x01, 0x04, 0x4B, 0x00, 0x00, 0x00, iris, 0xFF]))
            if apply_fixed_shutter:
                self._set_shutter_direct(shutter)
            if apply_fixed_gain:
                self._set_gain_direct(gain)
            if gain_limit is not None:
                self._visca(bytes([0x81, 0x01, 0x04, 0x2C, gain_limit, 0xFF]))
            time.sleep(0.25)
            after = self.get()
        if int(after["mode"]["value"]) != mode:
            raise RuntimeError("露出モードの変更を確認できませんでした")
        if apply_fixed_iris and int(after["iris"]["value"]) != iris:
            raise RuntimeError("絞り値の変更を確認できませんでした")
        if apply_fixed_shutter and int(after["shutter"]["value"]) != shutter:
            raise RuntimeError("シャッター値の変更を確認できませんでした")
        if gain_limit is not None and int(after["gain_limit"]) != gain_limit:
            raise RuntimeError("ゲイン上限の変更を確認できませんでした")
        if apply_fixed_gain and int(after["gain"]) != gain:
            raise RuntimeError("ゲイン値の変更を確認できませんでした")

        history = APP_DIR / "data" / "exposure_history.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"at": now_jst().isoformat(), "before": before, "after": after},
                    ensure_ascii=False,
                )
                + "\n"
            )
        return after

    def set_sae_shutter(self, shutter: int) -> dict[str, Any]:
        """Change only the SAE shutter and verify it from the camera."""
        with self.lock:
            before = self.get()
            if int(before["mode"]["value"]) != 10:
                raise RuntimeError("回折ガードにはSAE（シャッター優先）が必要です")
            valid = {int(item["value"]) for item in before["shutter"]["options"]}
            if shutter not in valid:
                raise ValueError("カメラが対応していないシャッター値です")
            self._set_shutter_direct(shutter)
            time.sleep(0.25)
            after = self.get()
            if int(after["shutter"]["value"]) != shutter:
                raise RuntimeError("シャッター値の変更を確認できませんでした")
            return after


def _histogram_percentile(histogram: list[int], sample_count: int, percentile: float) -> int:
    threshold = max(1, math.ceil(sample_count * percentile))
    accumulated = 0
    for value, count in enumerate(histogram):
        accumulated += count
        if accumulated >= threshold:
            return value
    return 255


def analyze_luma_frame(
    frame: bytes,
    previous: Optional[bytes] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> dict[str, Any]:
    """Calculate exposure-oriented metrics from one 8-bit grayscale frame."""
    if not frame:
        raise ValueError("空の解析フレームです")
    histogram = [0] * 256
    total = 0
    for value in frame:
        histogram[value] += 1
        total += value
    sample_count = len(frame)
    p10 = _histogram_percentile(histogram, sample_count, 0.10)
    p50 = _histogram_percentile(histogram, sample_count, 0.50)
    p90 = _histogram_percentile(histogram, sample_count, 0.90)
    p95 = _histogram_percentile(histogram, sample_count, 0.95)
    p99 = _histogram_percentile(histogram, sample_count, 0.99)
    black_fraction = sum(histogram[:17]) / sample_count
    white_fraction = sum(histogram[235:]) / sample_count
    metered_avg = total / sample_count
    metered_p90 = p90
    metered_white_fraction = white_fraction
    metered_tiles = 1
    if width and height and width * height == sample_count and width >= 4 and height >= 4:
        tiles_x = min(8, width)
        tiles_y = min(5, height)
        tile_count = tiles_x * tiles_y
        tile_totals = [0] * tile_count
        tile_samples = [0] * tile_count
        for index, value in enumerate(frame):
            y, x = divmod(index, width)
            tile = min(tiles_y - 1, y * tiles_y // height) * tiles_x + min(
                tiles_x - 1, x * tiles_x // width
            )
            tile_totals[tile] += value
            tile_samples[tile] += 1
        ranked = sorted(
            range(tile_count),
            key=lambda tile: tile_totals[tile] / max(1, tile_samples[tile]),
        )
        trim = tile_count // 10
        kept = set(ranked[trim : tile_count - trim] if trim else ranked)
        metered_histogram = [0] * 256
        metered_total = 0
        metered_count = 0
        for index, value in enumerate(frame):
            y, x = divmod(index, width)
            tile = min(tiles_y - 1, y * tiles_y // height) * tiles_x + min(
                tiles_x - 1, x * tiles_x // width
            )
            if tile in kept:
                metered_histogram[value] += 1
                metered_total += value
                metered_count += 1
        if metered_count:
            metered_avg = metered_total / metered_count
            metered_p90 = _histogram_percentile(metered_histogram, metered_count, 0.90)
            metered_white_fraction = sum(metered_histogram[235:]) / metered_count
            metered_tiles = len(kept)
    frame_difference = 0.0
    if previous is not None and len(previous) == sample_count:
        frame_difference = sum(abs(left - right) for left, right in zip(frame, previous)) / sample_count
    y_min = next(index for index, count in enumerate(histogram) if count)
    y_max = next(index for index in range(255, -1, -1) if histogram[index])
    # A genuinely dark night frame must remain controllable; treating it as a
    # signal fault prevents the guard from ever raising exposure.  Reject only
    # an exactly uniform digital black/white frame.  Decode/connectivity faults
    # are handled by the analyzer process and stale-frame checks.
    valid = not (y_min == y_max and y_min in {0, 255})
    return {
        "captured_at": now_jst().isoformat(),
        "monotonic_at": time.monotonic(),
        "sample_count": sample_count,
        "y_min": y_min,
        "y_p10": p10,
        "y_p50": p50,
        "y_avg": total / sample_count,
        "y_p90": p90,
        "y_p95": p95,
        "y_p99": p99,
        "y_max": y_max,
        "black_fraction": black_fraction,
        "white_fraction": white_fraction,
        "metered_yavg": metered_avg,
        "metered_yp90": metered_p90,
        "metered_white_fraction": metered_white_fraction,
        "metered_tiles": metered_tiles,
        "frame_difference": frame_difference,
        "valid": valid,
    }


class LumaAnalyzer:
    """Continuously decode a small centre crop from the camera preview stream.

    KN20A's RTSP endpoint can stall after the first decoded frame while its web
    preview remains healthy. The preview websocket carries fragmented MP4 and
    is therefore the preferred analysis source. RTSP remains as a fallback.
    """

    def __init__(self, config: dict[str, Any]):
        self.ffmpeg_path = str(config["ffmpeg_path"])
        self.url = str(config.get("analysis_url") or config["input_url"])
        self.ws_url = str(config.get("analysis_ws_url") or "")
        self.width = int(config.get("analysis_width", 320))
        self.height = int(config.get("analysis_height", 180))
        self.sample_seconds = max(1, int(config.get("analysis_sample_seconds", 4)))
        self.timeout = int(config.get("rtsp_timeout_microseconds", 5_000_000))
        self.retry_seconds = max(1, int(config.get("retry_seconds", 5)))
        self.retry_max_seconds = max(
            self.retry_seconds, int(config.get("analysis_retry_max_seconds", 300))
        )
        self.stable_seconds = max(
            self.sample_seconds * 3, int(config.get("analysis_stable_seconds", 120))
        )
        self.failure_limit = max(1, int(config.get("analysis_failure_limit", 8)))
        self.circuit_breaker_seconds = max(
            self.retry_max_seconds,
            int(config.get("analysis_circuit_breaker_seconds", 900)),
        )
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.process: Optional[subprocess.Popen] = None
        self.latest: Optional[dict[str, Any]] = None
        self.last_error = ""
        self.consecutive_failures = 0
        self.next_retry_seconds = 0
        self.circuit_open = False
        try:
            self.job = WindowsKillOnCloseJob()
        except Exception as exc:
            self.job = None
            logging.error("failed to create analyzer child-process job: %s", exc)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="luma-analyzer", daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        with self.lock:
            process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if self.thread:
            self.thread.join(timeout=10)
        if self.thread and self.thread.is_alive() and self.job is not None:
            logging.error("luma analyzer did not stop in time; closing child-process job")
            self.job.close()
            self.thread.join(timeout=5)
        elif self.job is not None:
            self.job.close()
        with self.lock:
            self.next_retry_seconds = 0
            self.circuit_open = False

    def snapshot(self) -> Optional[dict[str, Any]]:
        with self.lock:
            if self.latest is None:
                return None
            value = dict(self.latest)
            value["age_seconds"] = max(0.0, time.monotonic() - float(value["monotonic_at"]))
            value.pop("monotonic_at", None)
            return value

    def health(self) -> dict[str, Any]:
        with self.lock:
            process = self.process
            return {
                "process_pid": process.pid if process and process.poll() is None else None,
                "consecutive_failures": self.consecutive_failures,
                "next_retry_seconds": self.next_retry_seconds,
                "circuit_open": self.circuit_open,
            }

    @staticmethod
    def _read_exact(stream: Any, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = stream.read(size - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    def _command(self, websocket: bool = False) -> list[str]:
        if websocket:
            video_filter = (
                f"fps=1/{self.sample_seconds},"
                "crop=iw*0.8:ih*0.8:iw*0.1:ih*0.1,"
                f"scale={self.width}:{self.height}:flags=area,format=gray"
            )
            input_args = ["-f", "mp4", "-i", "pipe:0"]
        else:
            video_filter = (
                f"select='not(mod(n,{self.sample_seconds * 60}))',"
                "crop=iw*0.8:ih*0.8:iw*0.1:ih*0.1,"
                f"scale={self.width}:{self.height}:flags=area,format=gray"
            )
            input_args = [
                "-rtsp_transport",
                "tcp",
                "-timeout",
                str(self.timeout),
                "-allowed_media_types",
                "video",
                "-i",
                self.url,
            ]
        return [
            self.ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            *input_args,
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            video_filter,
            "-pix_fmt",
            "gray",
            "-fps_mode",
            "vfr",
            "-f",
            "rawvideo",
            "pipe:1",
        ]

    def _read_frames(self, process: subprocess.Popen) -> None:
        frame_size = self.width * self.height
        previous: Optional[bytes] = None
        assert process.stdout is not None
        while not self.stop_event.is_set():
            frame = self._read_exact(process.stdout, frame_size)
            if len(frame) != frame_size:
                break
            stats = analyze_luma_frame(frame, previous, self.width, self.height)
            previous = frame
            with self.lock:
                self.latest = stats
                self.last_error = ""
                self.next_retry_seconds = 0
                self.circuit_open = False

    async def _feed_websocket(self, process: subprocess.Popen) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Pythonのwebsocketsパッケージがありません") from exc

        timeout_seconds = max(2.0, self.timeout / 1_000_000)
        async with websockets.connect(
            self.ws_url,
            open_timeout=timeout_seconds,
            close_timeout=1,
            max_size=None,
            # The KN20A server is an older implementation.  It is stable with
            # the browser-compatible basic framing, but eventually emits a
            # protocol error when extension negotiation or Ping is enabled.
            compression=None,
            ping_interval=None,
        ) as websocket:
            await websocket.send("onlinelemon" + secrets.token_hex(8))
            assert process.stdin is not None
            while not self.stop_event.is_set() and process.poll() is None:
                try:
                    payload = await asyncio.wait_for(websocket.recv(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                if isinstance(payload, bytes) and payload:
                    process.stdin.write(payload)

    @staticmethod
    def _close_pipe(pipe: Any) -> None:
        if pipe is None:
            return
        try:
            pipe.close()
        except (OSError, ValueError):
            pass

    def _reap_process(
        self, process: subprocess.Popen, reader: Optional[threading.Thread]
    ) -> None:
        """Close pipes and synchronously reap ffmpeg on every exit path."""
        self._close_pipe(process.stdin)
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            # A wait after kill is mandatory on Windows; otherwise the process
            # and its console host can remain as unreaped kernel objects.
            process.wait(timeout=5)

        if reader is not None:
            reader.join(timeout=3)
            if reader.is_alive():
                self._close_pipe(process.stdout)
                reader.join(timeout=2)
            if reader.is_alive():
                logging.error("luma frame reader did not stop after ffmpeg exit")
        self._close_pipe(process.stdout)

    def _set_process(self, process: Optional[subprocess.Popen]) -> None:
        with self.lock:
            self.process = process

    def _run_process(self, log_handle: Any, websocket: bool) -> None:
        if not self.run_lock.acquire(blocking=False):
            raise RuntimeError("輝度解析ffmpegは既に起動中です")
        process: Optional[subprocess.Popen] = None
        reader: Optional[threading.Thread] = None
        try:
            process = subprocess.Popen(
                self._command(websocket),
                stdin=subprocess.PIPE if websocket else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=log_handle,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if self.job is not None:
                self.job.assign(process)
            self._set_process(process)
            reader = threading.Thread(
                target=self._read_frames,
                args=(process,),
                name="luma-frame-reader",
                daemon=True,
            )
            reader.start()
            if websocket:
                asyncio.run(self._feed_websocket(process))
            else:
                while not self.stop_event.wait(0.5) and process.poll() is None:
                    pass
            if not self.stop_event.is_set():
                raise RuntimeError(
                    f"輝度解析ffmpegが予期せず終了しました (code={process.poll()})"
                )
        finally:
            try:
                if process is not None:
                    self._reap_process(process, reader)
            finally:
                self._set_process(None)
                self.run_lock.release()

    def _loop(self) -> None:
        failures = 0
        while not self.stop_event.is_set():
            log_path = LOG_DIR / "exposure_analyzer_ffmpeg.log"
            started_at = time.monotonic()
            error_message = ""
            try:
                with log_path.open("ab") as log_handle:
                    self._run_process(log_handle, bool(self.ws_url))
            except Exception as exc:
                error_message = str(exc).strip() or type(exc).__name__
                logging.warning("luma analyzer: %s", error_message)
            if self.stop_event.is_set():
                break

            runtime = time.monotonic() - started_at
            failures = 1 if runtime >= self.stable_seconds else failures + 1
            if not error_message:
                error_message = "輝度解析ストリームが終了しました"

            circuit_open = failures >= self.failure_limit
            if circuit_open:
                delay = self.circuit_breaker_seconds
                error_message = (
                    f"{error_message}。連続{failures}回失敗したため"
                    f"{delay}秒間再試行を停止します"
                )
                logging.error("luma analyzer circuit breaker: %s", error_message)
            else:
                delay = min(
                    self.retry_seconds * (2 ** max(0, failures - 1)),
                    self.retry_max_seconds,
                )

            with self.lock:
                self.last_error = error_message
                self.consecutive_failures = failures
                self.next_retry_seconds = delay
                self.circuit_open = circuit_open
            if self.stop_event.wait(delay):
                break
            if circuit_open:
                failures = 0
            with self.lock:
                self.next_retry_seconds = 0
                self.circuit_open = False


def shutter_denominator(label: str) -> Optional[int]:
    match = re.fullmatch(r"1/(\d+)", str(label).strip())
    return int(match.group(1)) if match else None


def usable_shutters(
    exposure: dict[str, Any], slowest_denominator: int, fastest_denominator: int
) -> list[int]:
    current = int(exposure["shutter"]["value"])
    bank_is_60hz = current >= 17
    values: list[tuple[int, int]] = []
    for option in exposure["shutter"]["options"]:
        value = int(option["value"])
        denominator = shutter_denominator(option.get("label", ""))
        if (value >= 17) != bank_is_60hz or denominator is None:
            continue
        if slowest_denominator <= denominator <= fastest_denominator:
            values.append((denominator, value))
    return [value for _, value in sorted(values)]


def luma_exposure_score(stats: dict[str, Any], target_avg: float, target_high: float) -> float:
    average = float(stats.get("metered_yavg", stats["y_avg"]))
    high = float(stats.get("metered_yp90", stats["y_p90"]))
    return 0.65 * average / target_avg + 0.35 * high / target_high


NIGHT_POLICY_TARGET_SCALES = {
    "natural": 0.60,
    "balanced": 0.75,
    "visibility": 1.00,
}


def compensated_image_targets(
    target_avg: float, target_high: float, compensation_ev: float
) -> tuple[float, float, float]:
    """Apply photographic exposure compensation to image-analysis targets."""
    factor = 2.0 ** float(compensation_ev)
    return target_avg * factor, target_high * factor, factor


def effective_image_targets(
    stats: dict[str, Any],
    exposure: dict[str, Any],
    target_avg: float,
    target_high: float,
    deadband: float,
    night_policy: str,
    slowest_denominator: int,
    fastest_denominator: int,
) -> tuple[float, float, float]:
    """Roll the target down only after clean optical exposure is exhausted.

    The target follows the scene between the daytime target and the selected
    night floor while the camera is at 1/60, fully open and gain zero.  This
    deliberately preserves natural darkness instead of immediately chasing a
    daytime grey value with gain.  Once gain is in use, the stable night floor
    is used until gain has been unwound again.
    """
    floor = NIGHT_POLICY_TARGET_SCALES.get(str(night_policy), 0.60)
    if floor >= 1.0:
        return target_avg, target_high, 1.0
    values = usable_shutters(exposure, slowest_denominator, fastest_denominator)
    current = int(exposure["shutter"]["value"])
    iris = int(exposure["iris"]["value"])
    gain = int(exposure.get("gain", 0))
    at_clean_limit = bool(values) and current == values[0] and iris == max(IRIS_LABELS)
    if not at_clean_limit:
        return target_avg, target_high, 1.0
    if gain > 0:
        scale = floor
    else:
        day_score = luma_exposure_score(stats, target_avg, target_high)
        # Make the first transition land inside the lower half of the deadband,
        # avoiding a target change that itself requests an exposure reversal.
        scale = max(floor, min(1.0, day_score / max(0.5, 1.0 - deadband * 0.5)))
    return target_avg * scale, target_high * scale, scale


def iris_f_number(value: int) -> float:
    label = IRIS_LABELS.get(int(value), "")
    if not label.startswith("F"):
        raise ValueError("未対応の絞り値です")
    return float(label[1:])


def _candidate_improves(score: float, projected: float) -> bool:
    # Near digital black the measured score can be exactly zero, so a modelled
    # exposure increase also remains zero.  Continue brightening to reveal
    # sensor signal instead of getting stuck at the floor.
    if score < 0.02 and projected >= score:
        return True
    return abs(math.log2(max(projected, 0.001))) < abs(math.log2(max(score, 0.001)))


def choose_image_exposure_action(
    stats: dict[str, Any],
    exposure: dict[str, Any],
    target_avg: float,
    target_high: float,
    deadband: float,
    white_limit: float,
    slowest_denominator: int,
    fastest_denominator: int,
    iris_limit: int = 5,
    quality_gain_limit: Optional[int] = None,
    gain_step_db: float = 3.0,
) -> tuple[str, Optional[int]]:
    """Choose one Manual exposure step while never closing beyond ``iris_limit``.

    Brightening unwinds gain and aperture before speeding the shutter. Darkening
    uses shutter latitude first, then opens the iris, then adds gain. This keeps
    noise low and guarantees the configured diffraction limit at every stage.
    """
    if not stats.get("valid", False):
        return "invalid", None
    if float(stats.get("frame_difference", 0.0)) > 25.0:
        return "transient", None
    values = usable_shutters(exposure, slowest_denominator, fastest_denominator)
    current = int(exposure["shutter"]["value"])
    if not values or current not in values:
        return "external", None
    iris = int(exposure["iris"]["value"])
    gain = int(exposure.get("gain", 0))
    camera_gain_limit = int(exposure.get("gain_limit", 0))
    gain_limit = camera_gain_limit if quality_gain_limit is None else min(
        camera_gain_limit, max(0, int(quality_gain_limit))
    )
    if iris < iris_limit or iris not in IRIS_LABELS or iris_limit not in IRIS_LABELS:
        return "external", None
    index = values.index(current)
    score = luma_exposure_score(stats, target_avg, target_high)
    # Small bright areas (sky, lamps and reflections) must not override a dark
    # or otherwise correctly exposed outdoor scene.  Treat clipping as an
    # emergency only when it is both substantial and the body of the image is
    # not underexposed.  Normal highlight influence remains in y_p90.
    clipped = (
        float(stats.get("metered_white_fraction", stats["white_fraction"])) >= white_limit
        and score >= 1.0
        and float(stats.get("y_p50", stats["y_avg"])) >= target_avg * 0.65
    )
    too_bright = clipped or score > 1.0 + deadband
    too_dark = score < 1.0 - deadband
    if too_bright:
        if gain > 0:
            projected = score / (10 ** (gain_step_db / 20))
            if clipped or _candidate_improves(score, projected):
                return "gain_down", gain - 1
            return "hold", None
        if iris > iris_limit:
            candidate = iris - 1
            projected = score * (iris_f_number(iris) / iris_f_number(candidate)) ** 2
            if clipped or _candidate_improves(score, projected):
                return "iris_close", candidate
            return "hold", None
        if index < len(values) - 1:
            candidate = values[index + 1]
            current_denominator = shutter_denominator(exposure["shutter"].get("label", "")) or 1
            by_value = {int(item["value"]): item for item in exposure["shutter"]["options"]}
            candidate_denominator = shutter_denominator(by_value[candidate].get("label", "")) or 1
            projected = score * current_denominator / candidate_denominator
            if clipped or _candidate_improves(score, projected):
                return "faster", candidate
    if too_dark and index > 0:
        candidate = values[index - 1]
        current_denominator = shutter_denominator(exposure["shutter"].get("label", "")) or 1
        by_value = {int(item["value"]): item for item in exposure["shutter"]["options"]}
        candidate_denominator = shutter_denominator(by_value[candidate].get("label", "")) or 1
        projected = score * current_denominator / candidate_denominator
        if _candidate_improves(score, projected):
            return "slower", candidate
    if too_dark and index == 0:
        if iris < max(IRIS_LABELS):
            candidate = iris + 1
            projected = score * (iris_f_number(iris) / iris_f_number(candidate)) ** 2
            if _candidate_improves(score, projected):
                return "iris_open", candidate
        if iris == max(IRIS_LABELS) and gain < gain_limit:
            projected = score * (10 ** (gain_step_db / 20))
            if _candidate_improves(score, projected):
                return "gain_up", gain + 1
        if iris == max(IRIS_LABELS) and gain >= gain_limit:
            return "limit", None
    return "hold", None


def choose_guard_shutter(
    exposure: dict[str, Any],
    closed_iris: int,
    open_iris: int,
    slowest_shutter: int,
    fastest_shutter: int,
) -> Optional[int]:
    """Return the next shutter value, or None when inside the iris dead band."""
    current = int(exposure["shutter"]["value"])
    iris = int(exposure["iris"]["value"])
    gain = int(exposure.get("gain", 0))
    bank_is_60hz = current >= 17
    # The camera exposes two parallel code banks.  UI settings are stored in
    # the active 60 Hz bank, so translate the same positions if anti-flicker is
    # later changed to 50 Hz (and vice versa).
    if not bank_is_60hz and slowest_shutter >= 17:
        slowest_shutter -= 17
        fastest_shutter -= 17
    elif bank_is_60hz and fastest_shutter < 17:
        slowest_shutter += 17
        fastest_shutter += 17
    bank_values = sorted(
        {
            int(item["value"])
            for item in exposure["shutter"]["options"]
            if (int(item["value"]) >= 17) == bank_is_60hz
        }
    )
    values = [
        int(item["value"])
        for item in exposure["shutter"]["options"]
        if (int(item["value"]) >= 17) == bank_is_60hz
        and slowest_shutter <= int(item["value"]) <= fastest_shutter
    ]
    values = sorted(set(values))
    if not values or current not in bank_values:
        return None
    # Do not jump straight back into the configured range.  When the user has
    # manually selected an out-of-range shutter, only move toward the range if
    # the measured iris says that direction also improves exposure.
    if current not in values:
        bank_index = bank_values.index(current)
        if current > fastest_shutter and (iris > open_iris or gain > 0) and bank_index > 0:
            return bank_values[bank_index - 1]
        if current < slowest_shutter and iris < closed_iris and bank_index < len(bank_values) - 1:
            return bank_values[bank_index + 1]
        return None
    index = values.index(current)
    if iris < closed_iris and index < len(values) - 1:
        return values[index + 1]
    # Prefer a slower shutter over gain while there is still shutter latitude.
    # Some firmware revisions start adding gain before the iris has left the
    # dead band, so gain must also participate in the decision.
    if (iris > open_iris or gain > 0) and index > 0:
        return values[index - 1]
    return None


class ExposureGuard:
    """Manual image AE with a hard configurable diffraction aperture limit."""

    AVAILABLE = True
    UNAVAILABLE_REASON = ""
    DEFAULTS = {
        "settings_version": 2,
        "enabled": False,
        "phase": "off",
        "target_yavg": 97.0,
        "target_yhigh": 169.0,
        "exposure_compensation_ev": 0.0,
        "deadband": 0.10,
        "white_limit": 0.10,
        "manual_iris": 5,
        "slowest_denominator": 60,
        "fastest_denominator": 3000,
        "night_policy": "natural",
        "quality_gain_limit": 3,
        "gain_step_db": 3.0,
        "confirmations": 3,
        "settle_seconds": 5,
        "reversal_hold_seconds": 45,
    }

    PHASE_LABELS = {
        "off": "停止中",
        "day_shutter": "日中・SS制御",
        "twilight_iris": "夕方・絞り制御",
        "night_gain": "夜間・Gain制御",
        "night_natural": "夜間・自然な暗さを維持",
        "night_limited": "夜間・画質上限",
        "recovering": "カメラ復帰待ち",
        "paused_external": "外部変更を検知して停止",
        "fault": "安全停止",
    }

    def __init__(
        self, exposure: ExposureClient, path: Path, config: Optional[dict[str, Any]] = None
    ):
        self.exposure = exposure
        self.path = path
        self.analyzer = LumaAnalyzer(config or {**DEFAULT_CONFIG})
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.settings = dict(self.DEFAULTS)
        self.last_action = ""
        self.last_error = ""
        self.pending_candidate: Optional[str] = None
        self.pending_count = 0
        self.last_success_at = ""
        self.last_sample_id = ""
        self.expected_shutter: Optional[int] = None
        self.expected_iris: Optional[int] = None
        self.expected_gain: Optional[int] = None
        self.settle_until = 0.0
        self.resume_pending = False
        self.filtered_metrics: Optional[dict[str, Any]] = None
        self.effective_target_yavg = float(self.settings["target_yavg"])
        self.effective_target_yhigh = float(self.settings["target_yhigh"])
        self.target_scale = 1.0
        self.last_direction = ""
        self.last_direction_at = 0.0
        self.camera_error_count = 0
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.settings.update({key: loaded[key] for key in self.DEFAULTS if key in loaded})
                    if int(loaded.get("settings_version", 1)) < 2:
                        # Version 1 treated 1% clipped pixels as an emergency.
                        # Outdoor footage repeatedly oscillated on sky/clouds.
                        self.settings["white_limit"] = 0.10
                        self.settings["settings_version"] = 2
            except Exception:
                logging.exception("failed to load exposure guard settings")
        if not self.settings.get("enabled"):
            self.settings["phase"] = "off"
        else:
            self.settings["phase"] = "recovering"
            self.resume_pending = True
        self.effective_target_yavg, self.effective_target_yhigh, _ = compensated_image_targets(
            self.settings["target_yavg"],
            self.settings["target_yhigh"],
            self.settings["exposure_compensation_ev"],
        )

    def start(self) -> None:
        self.analyzer.start()
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="exposure-guard", daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.analyzer.shutdown()

    def snapshot(self) -> dict[str, Any]:
        metrics = self.analyzer.snapshot()
        with self.lock:
            phase = str(self.settings.get("phase"))
            phase_label = self.PHASE_LABELS.get(phase, phase)
            if phase in {"day_shutter", "twilight_iris"}:
                iris_label = IRIS_LABELS.get(int(self.settings["manual_iris"]), "?")
                phase_label = f"{phase_label}・回折上限{iris_label}"
            return {
                **self.settings,
                "phase_label": phase_label,
                "iris_options": [
                    {"value": value, "label": label} for value, label in IRIS_LABELS.items()
                ],
                "last_action": self.last_action,
                "last_error": self.last_error or self.analyzer.last_error,
                "analyzer_health": self.analyzer.health(),
                "last_success_at": self.last_success_at,
                "available": self.AVAILABLE,
                "unavailable_reason": "",
                "metrics": metrics,
                "effective_target_yavg": self.effective_target_yavg,
                "effective_target_yhigh": self.effective_target_yhigh,
                "target_scale": self.target_scale,
                "compensation_factor": 2.0
                ** float(self.settings["exposure_compensation_ev"]),
                "quality_limited": phase == "night_limited",
            }

    def _save(self) -> None:
        atomic_write_json(self.path, self.settings)

    def _record_event(self, event: str, details: dict[str, Any]) -> None:
        history = APP_DIR / "data" / "exposure_guard_history.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"at": now_jst().isoformat(), "event": event, **details},
                    ensure_ascii=False,
                )
                + "\n"
            )

    def configure(self, value: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            settings = dict(self.settings)
        previously_enabled = bool(settings.get("enabled"))
        for key in (
            "target_yavg",
            "target_yhigh",
            "exposure_compensation_ev",
            "deadband",
            "white_limit",
            "manual_iris",
            "slowest_denominator",
            "fastest_denominator",
            "night_policy",
            "quality_gain_limit",
            "gain_step_db",
            "confirmations",
            "settle_seconds",
            "reversal_hold_seconds",
        ):
            if key in value:
                settings[key] = value[key]
        settings["enabled"] = bool(value.get("enabled", False))
        settings["target_yavg"] = float(settings["target_yavg"])
        settings["target_yhigh"] = float(settings["target_yhigh"])
        settings["exposure_compensation_ev"] = round(
            float(settings["exposure_compensation_ev"]), 1
        )
        settings["deadband"] = float(settings["deadband"])
        settings["white_limit"] = float(settings["white_limit"])
        settings["manual_iris"] = int(settings["manual_iris"])
        settings["slowest_denominator"] = int(settings["slowest_denominator"])
        settings["fastest_denominator"] = int(settings["fastest_denominator"])
        settings["night_policy"] = str(settings["night_policy"])
        settings["quality_gain_limit"] = int(settings["quality_gain_limit"])
        settings["gain_step_db"] = float(settings["gain_step_db"])
        settings["confirmations"] = int(settings["confirmations"])
        settings["settle_seconds"] = int(settings["settle_seconds"])
        settings["reversal_hold_seconds"] = int(settings["reversal_hold_seconds"])
        settings["settings_version"] = 2
        if not 20 <= settings["target_yavg"] <= 220:
            raise ValueError("目標平均輝度が範囲外です")
        if not 30 <= settings["target_yhigh"] <= 245:
            raise ValueError("目標P90輝度が範囲外です")
        if settings["target_yavg"] >= settings["target_yhigh"]:
            raise ValueError("目標P90輝度は平均輝度より高くしてください")
        if not -2.0 <= settings["exposure_compensation_ev"] <= 1.0:
            raise ValueError("露出補正は-2.0〜+1.0 EVで指定してください")
        if not 0.03 <= settings["deadband"] <= 0.30:
            raise ValueError("輝度不感帯が範囲外です")
        if not 0.0001 <= settings["white_limit"] <= 0.20:
            raise ValueError("白飛び率が範囲外です")
        if not 1 <= settings["manual_iris"] <= 12:
            raise ValueError("固定絞りが範囲外です")
        if settings["slowest_denominator"] not in {30, 60}:
            raise ValueError("最遅SSは1/30または1/60で指定してください")
        if not 1 <= settings["slowest_denominator"] < settings["fastest_denominator"]:
            raise ValueError("シャッター範囲が不正です")
        if settings["night_policy"] not in NIGHT_POLICY_TARGET_SCALES:
            raise ValueError("夜間方針が不正です")
        if not 0 <= settings["quality_gain_limit"] <= 15:
            raise ValueError("画質Gain上限が範囲外です")
        if not 0.5 <= settings["gain_step_db"] <= 6.0:
            raise ValueError("Gainステップ幅が範囲外です")
        if not 2 <= settings["confirmations"] <= 20:
            raise ValueError("連続判定回数が範囲外です")
        if not 0 <= settings["reversal_hold_seconds"] <= 300:
            raise ValueError("反転抑制時間が範囲外です")

        if settings["enabled"]:
            before = self.exposure.get()
            values = usable_shutters(
                before, settings["slowest_denominator"], settings["fastest_denominator"]
            )
            if not values:
                raise ValueError("指定範囲に使用可能なシャッターがありません")
            shutter = int(before["shutter"]["value"])
            if shutter not in values:
                current_denominator = shutter_denominator(before["shutter"].get("label", "")) or 0
                by_value = {int(item["value"]): item for item in before["shutter"]["options"]}
                shutter = min(
                    values,
                    key=lambda candidate: abs(
                        math.log2(
                            max(1, shutter_denominator(by_value[candidate].get("label", "")) or 1)
                            / max(1, current_denominator)
                        )
                    ),
                )
            after = self.exposure.set(
                3,
                settings["manual_iris"],
                shutter,
                settings["quality_gain_limit"],
                min(int(before.get("gain", 0)), settings["quality_gain_limit"])
                if previously_enabled
                else 0,
            )
            settings["phase"] = "day_shutter"
            action = (
                f"{now_jst().strftime('%H:%M:%S')} 画像AE開始: Manual / "
                f"{after['iris']['label']} / {after['shutter']['label']}"
            )
            self._record_event("enabled", {"before": before, "after": after})
            expected_shutter = int(after["shutter"]["value"])
            expected_iris = int(after["iris"]["value"])
            expected_gain = int(after["gain"])
        else:
            settings["phase"] = "off"
            action = f"{now_jst().strftime('%H:%M:%S')} 画像AEを停止"
            expected_shutter = None
            expected_iris = None
            expected_gain = None
            self._record_event("disabled", {})
        with self.lock:
            self.settings = settings
            self.last_action = action
            self.last_error = ""
            self.pending_candidate = None
            self.pending_count = 0
            self.expected_shutter = expected_shutter
            self.expected_iris = expected_iris
            self.expected_gain = expected_gain
            self.settle_until = time.monotonic() + settings["settle_seconds"]
            self.resume_pending = False
            self.filtered_metrics = None
            (
                self.effective_target_yavg,
                self.effective_target_yhigh,
                _,
            ) = compensated_image_targets(
                settings["target_yavg"],
                settings["target_yhigh"],
                settings["exposure_compensation_ev"],
            )
            self.target_scale = 1.0
            self.last_direction = ""
            self.last_direction_at = 0.0
            self._save()
        return self.snapshot()

    def calibrate(self) -> dict[str, Any]:
        metrics = self.analyzer.snapshot()
        if metrics is None or float(metrics.get("age_seconds", 999)) > self.analyzer.sample_seconds * 3:
            raise RuntimeError("新しい解析フレームがまだありません")
        if not metrics.get("valid", False):
            raise RuntimeError("異常な暗転または白画面のため基準にできません")
        with self.lock:
            self.settings["target_yavg"] = round(
                float(metrics.get("metered_yavg", metrics["y_avg"])), 2
            )
            self.settings["target_yhigh"] = float(
                metrics.get("metered_yp90", metrics["y_p90"])
            )
            self.last_action = f"{now_jst().strftime('%H:%M:%S')} 現在映像を輝度基準に登録"
            self._save()
        self._record_event("calibrated", {"metrics": metrics})
        return self.snapshot()

    def stop_for_external_change(self) -> None:
        with self.lock:
            if not self.settings.get("enabled"):
                return
            self.settings["enabled"] = False
            self.settings["phase"] = "paused_external"
            self.last_error = "手動の露出変更を優先して画像AEを停止しました"
            self.pending_candidate = None
            self.pending_count = 0
            self.expected_shutter = None
            self.expected_iris = None
            self.expected_gain = None
            self.resume_pending = False
            self._save()
        self._record_event("external_stop", {})

    def _confirm_candidate(self, value: Optional[Any], required: int = 2) -> bool:
        with self.lock:
            if value is None:
                self.pending_candidate = None
                self.pending_count = 0
                return False
            candidate = str(value)
            if self.pending_candidate == candidate:
                self.pending_count += 1
            else:
                self.pending_candidate = candidate
                self.pending_count = 1
            return self.pending_count >= required

    def _safe_stop(self, message: str, phase: str = "fault") -> None:
        with self.lock:
            self.settings["enabled"] = False
            self.settings["phase"] = phase
            self.last_error = message
            self.pending_candidate = None
            self.pending_count = 0
            self.expected_shutter = None
            self.expected_iris = None
            self.expected_gain = None
            self.resume_pending = False
            self._save()
        self._record_event("safety_stop", {"reason": message, "phase": phase})

    def _smooth_metrics(self, metrics: dict[str, Any]) -> dict[str, Any]:
        """Apply gentle temporal smoothing without hiding invalid/transient frames."""
        keys = (
            "y_avg",
            "y_p50",
            "y_p90",
            "white_fraction",
            "black_fraction",
            "metered_yavg",
            "metered_yp90",
            "metered_white_fraction",
        )
        if self.filtered_metrics is None:
            self.filtered_metrics = {key: float(metrics.get(key, 0.0)) for key in keys}
        else:
            alpha = 0.35
            for key in keys:
                current = float(metrics.get(key, 0.0))
                previous = float(self.filtered_metrics.get(key, current))
                self.filtered_metrics[key] = previous + alpha * (current - previous)
        result = dict(metrics)
        result.update(self.filtered_metrics)
        return result

    @staticmethod
    def _action_direction(action: str) -> str:
        if action in {"slower", "iris_open", "gain_up"}:
            return "brighter"
        if action in {"faster", "iris_close", "gain_down"}:
            return "darker"
        return ""

    @staticmethod
    def _phase_for(
        exposure: dict[str, Any], settings: dict[str, Any], target_scale: float, action: str
    ) -> str:
        values = usable_shutters(
            exposure, settings["slowest_denominator"], settings["fastest_denominator"]
        )
        if not values:
            return "fault"
        shutter = int(exposure["shutter"]["value"])
        iris = int(exposure["iris"]["value"])
        gain = int(exposure.get("gain", 0))
        if shutter != values[0]:
            return "day_shutter"
        if iris < max(IRIS_LABELS):
            return "twilight_iris"
        if action == "limit":
            return "night_limited"
        if gain > 0 or action == "gain_up":
            return "night_gain"
        if target_scale < 0.995:
            return "night_natural"
        return "twilight_iris"

    def _resume_camera(self, settings: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
        """Re-arm persisted AE after a PC/camera power recovery."""
        values = usable_shutters(
            before, settings["slowest_denominator"], settings["fastest_denominator"]
        )
        if not values:
            raise RuntimeError("復帰時に使用可能なシャッターがありません")
        current = int(before["shutter"]["value"])
        if current not in values:
            by_value = {int(item["value"]): item for item in before["shutter"]["options"]}
            current_denominator = shutter_denominator(before["shutter"].get("label", "")) or 0
            current = min(
                values,
                key=lambda candidate: abs(
                    math.log2(
                        max(1, shutter_denominator(by_value[candidate].get("label", "")) or 1)
                        / max(1, current_denominator)
                    )
                ),
            )
        after = self.exposure.set(
            3,
            int(settings["manual_iris"]),
            current,
            int(settings["quality_gain_limit"]),
            0,
        )
        with self.lock:
            self.expected_shutter = int(after["shutter"]["value"])
            self.expected_iris = int(after["iris"]["value"])
            self.expected_gain = int(after["gain"])
            self.resume_pending = False
            self.camera_error_count = 0
            self.settings["phase"] = "day_shutter"
            self.last_action = (
                f"{now_jst().strftime('%H:%M:%S')} 電源復帰後に画像AEを再開: "
                f"{after['iris']['label']} / {after['shutter']['label']} / Gain {after['gain']}"
            )
            self.settle_until = time.monotonic() + int(settings["settle_seconds"])
            self._save()
        self._record_event("resumed", {"before": before, "after": after})
        return after

    def _loop(self) -> None:
        while not self.stop_event.wait(1):
            with self.lock:
                settings = dict(self.settings)
                expected_shutter = self.expected_shutter
                expected_iris = self.expected_iris
                expected_gain = self.expected_gain
                settle_until = self.settle_until
                resume_pending = self.resume_pending
            if not settings["enabled"]:
                continue
            metrics = self.analyzer.snapshot()
            if metrics is None:
                with self.lock:
                    self.last_error = "映像解析フレームを待っています"
                continue
            if float(metrics.get("age_seconds", 999)) > self.analyzer.sample_seconds * 3:
                with self.lock:
                    self.last_error = "映像解析フレームが更新されないため操作を保留中です"
                continue
            sample_id = str(metrics["captured_at"])
            if sample_id == self.last_sample_id or time.monotonic() < settle_until:
                continue
            self.last_sample_id = sample_id
            try:
                exposure = self.exposure.get()
                with self.lock:
                    self.camera_error_count = 0
                if resume_pending:
                    self._resume_camera(settings, exposure)
                    continue
                if int(exposure["mode"]["value"]) != 3 or exposure["iris"].get("actual") is not True:
                    self._safe_stop(
                        "Manual露出が外部で変更されたため停止しました",
                        "paused_external",
                    )
                    continue
                control_metrics = self._smooth_metrics(metrics)
                current_shutter = int(exposure["shutter"]["value"])
                current_iris = int(exposure["iris"]["value"])
                current_gain = int(exposure["gain"])
                if current_iris < int(settings["manual_iris"]):
                    self._safe_stop(
                        f"回折上限{IRIS_LABELS[int(settings['manual_iris'])]}を越える絞りを検知したため停止しました",
                        "paused_external",
                    )
                    continue
                if expected_shutter is None or expected_iris is None or expected_gain is None:
                    with self.lock:
                        self.expected_shutter = current_shutter
                        self.expected_iris = current_iris
                        self.expected_gain = current_gain
                elif (
                    current_shutter != expected_shutter
                    or current_iris != expected_iris
                    or current_gain != expected_gain
                ):
                    self._safe_stop(
                        "露出が外部で変更されたため停止しました",
                        "paused_external",
                    )
                    continue
                compensated_avg, compensated_high, _ = compensated_image_targets(
                    settings["target_yavg"],
                    settings["target_yhigh"],
                    settings["exposure_compensation_ev"],
                )
                effective_avg, effective_high, target_scale = effective_image_targets(
                    control_metrics,
                    exposure,
                    compensated_avg,
                    compensated_high,
                    settings["deadband"],
                    settings["night_policy"],
                    settings["slowest_denominator"],
                    settings["fastest_denominator"],
                )
                action, next_value = choose_image_exposure_action(
                    control_metrics,
                    exposure,
                    effective_avg,
                    effective_high,
                    settings["deadband"],
                    settings["white_limit"],
                    settings["slowest_denominator"],
                    settings["fastest_denominator"],
                    settings["manual_iris"],
                    settings["quality_gain_limit"],
                    settings["gain_step_db"],
                )
                direction = self._action_direction(action)
                opposite = (
                    direction
                    and self.last_direction
                    and direction != self.last_direction
                    and time.monotonic() - self.last_direction_at
                    < int(settings["reversal_hold_seconds"])
                )
                emergency_clip = float(
                    control_metrics.get(
                        "metered_white_fraction", control_metrics.get("white_fraction", 0.0)
                    )
                ) >= max(
                    0.25, float(settings["white_limit"]) * 2
                )
                if opposite and not emergency_clip:
                    action, next_value = "hold", None
                phase = self._phase_for(exposure, settings, target_scale, action)
                with self.lock:
                    self.settings["phase"] = phase
                    self.effective_target_yavg = effective_avg
                    self.effective_target_yhigh = effective_high
                    self.target_scale = target_scale
                if action in {"invalid", "transient"}:
                    self._confirm_candidate(None)
                    with self.lock:
                        self.last_error = "異常フレームまたは大きな動体を検知したため操作を保留中です"
                    continue
                if action == "external":
                    self._safe_stop("現在の露出が画像AEの制御範囲外です", "paused_external")
                    continue
                if action in {"hold", "limit"}:
                    self._confirm_candidate(None)
                elif next_value is not None and self._confirm_candidate(
                    f"{action}:{next_value}", int(settings["confirmations"])
                ):
                    next_shutter = current_shutter
                    next_iris = current_iris
                    next_gain = current_gain
                    if action in {"faster", "slower"}:
                        next_shutter = next_value
                    elif action in {"iris_open", "iris_close"}:
                        next_iris = next_value
                    elif action in {"gain_up", "gain_down"}:
                        next_gain = next_value
                    after = self.exposure.set(
                        3,
                        next_iris,
                        next_shutter,
                        int(exposure["gain_limit"]),
                        next_gain,
                    )
                    message = (
                        f"{now_jst().strftime('%H:%M:%S')} {exposure['iris']['label']} / "
                        f"{exposure['shutter']['label']} / Gain {current_gain} → "
                        f"{after['iris']['label']} / {after['shutter']['label']} / "
                        f"Gain {after['gain']}（画像輝度 {float(metrics['y_avg']):.1f}）"
                    )
                    with self.lock:
                        self.expected_shutter = int(after["shutter"]["value"])
                        self.expected_iris = int(after["iris"]["value"])
                        self.expected_gain = int(after["gain"])
                        self.last_action = message
                        self.pending_candidate = None
                        self.pending_count = 0
                        self.settle_until = time.monotonic() + settings["settle_seconds"]
                        self.last_direction = direction
                        self.last_direction_at = time.monotonic()
                        self.settings["phase"] = self._phase_for(
                            after, settings, target_scale, action
                        )
                    self._record_event(
                        action,
                        {
                            "metrics": metrics,
                            "control_metrics": control_metrics,
                            "effective_target": {
                                "y_avg": effective_avg,
                                "y_p90": effective_high,
                                "scale": target_scale,
                            },
                            "after": after,
                        },
                    )
                    logging.info("exposure guard: %s", message)
                with self.lock:
                    self.last_error = ""
                    self.last_success_at = now_jst().isoformat()
            except Exception as exc:
                with self.lock:
                    self.last_error = str(exc)
                    self.pending_candidate = None
                    self.pending_count = 0
                    self.camera_error_count += 1
                    if self.camera_error_count >= 3:
                        self.resume_pending = True
                        self.settings["phase"] = "recovering"
                logging.warning("exposure guard: %s", exc)


class AppState:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.store = ScheduleStore(SCHEDULE_PATH)
        self.presets = PresetStore(PRESET_PATH)
        self.exposure = ExposureClient(config["camera_ip"], config["visca_port"])
        self.recorder = RecorderManager(config, self.store, self.exposure)
        self.exposure_guard = ExposureGuard(self.exposure, EXPOSURE_GUARD_PATH, config)
        self.token = secrets.token_urlsafe(32)

    def status(self) -> dict[str, Any]:
        output_dir = Path(self.config["output_dir_resolved"])
        output_dir.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(output_dir)
        schedules = self.store.list()
        current = now_jst()
        next_item = next(
            (
                item
                for item in schedules
                if item.get("status") == "pending" and parse_datetime(item["end"]) > current
            ),
            None,
        )
        return {
            "now": current.isoformat(),
            "recorder": self.recorder.snapshot(),
            "schedules": schedules,
            "presets": self.presets.list(),
            "next_schedule": next_item,
            "exposure_guard": self.exposure_guard.snapshot(),
            "disk": {"free": disk.free, "total": disk.total, "used": disk.used},
            "config": {
                "camera_ip": self.config["camera_ip"],
                "input_mode": self.config["input_mode"],
                "input_url": self.config["input_url"],
                "listen_url": self.config["listen_url"],
                "output_dir": self.config["output_dir_resolved"],
                "segment_seconds": self.config["segment_seconds"],
            },
        }


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KN20ARecorder/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            if self.path == "/api/state":
                return
            logging.info("http %s - %s", self.address_string(), fmt % args)

        def _json(self, value: Any, status: int = 200) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, message: str, status: int = 400) -> None:
            self._json({"ok": False, "error": message}, status)

        def _require_token(self) -> bool:
            if self.command not in MUTATING_METHODS:
                return True
            if self.headers.get("X-KN20A-Token", "") != state.token:
                self._error("操作トークンが不正です", HTTPStatus.FORBIDDEN)
                return False
            return True

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("リクエスト本文が不正です")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSONオブジェクトが必要です")
            return value

        def _static(self, relative: str) -> None:
            file_path = (WEB_DIR / relative).resolve()
            if WEB_DIR.resolve() not in file_path.parents or not file_path.is_file():
                self.send_error(404)
                return
            payload = file_path.read_bytes()
            if relative == "index.html":
                text = payload.decode("utf-8").replace("__KN20A_TOKEN__", state.token)
                payload = text.encode("utf-8")
            mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; connect-src 'self'; img-src 'self' data:")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            try:
                if path == "/api/state":
                    self._json({"ok": True, **state.status()})
                elif path == "/api/exposure":
                    self._json({"ok": True, "exposure": state.exposure.get()})
                elif path == "/api/exposure-guard":
                    self._json({"ok": True, "guard": state.exposure_guard.snapshot()})
                elif path == "/api/presets":
                    self._json({"ok": True, "presets": state.presets.list()})
                elif path in {"/", "/index.html"}:
                    self._static("index.html")
                elif path == "/app.js":
                    self._static("app.js")
                elif path == "/style.css":
                    self._static("style.css")
                else:
                    self.send_error(404)
            except Exception as exc:
                logging.exception("GET failed")
                self._error(str(exc), 500)

        def do_POST(self) -> None:
            if not self._require_token():
                return
            path = urllib.parse.urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/schedules":
                    raw_preset = body.get("preset_number")
                    preset_number = None if raw_preset in {None, ""} else int(raw_preset)
                    item = state.store.create(
                        body.get("title", ""),
                        body.get("start", ""),
                        body.get("end", ""),
                        preset_number,
                    )
                    self._json({"ok": True, "schedule": item}, 201)
                    return
                if path == "/api/quick-test":
                    duration = int(body.get("duration_seconds", 30))
                    if not 10 <= duration <= 300:
                        raise ValueError("テスト録画は10〜300秒で指定してください")
                    start = now_jst() + timedelta(seconds=2)
                    end = start + timedelta(seconds=duration)
                    raw_preset = body.get("preset_number")
                    preset_number = None if raw_preset in {None, ""} else int(raw_preset)
                    item = state.store.create(
                        "受信テスト", start.isoformat(), end.isoformat(), preset_number
                    )
                    self._json({"ok": True, "schedule": item}, 201)
                    return
                if path == "/api/recording/stop":
                    item = state.recorder.stop_active_recording()
                    if item is None:
                        self._error("停止できる録画はありません", 409)
                    else:
                        self._json({"ok": True, "schedule": item})
                    return
                if path == "/api/presets":
                    number = int(body["number"])
                    state.exposure.save_preset(number)
                    preset = state.presets.upsert(number, body.get("name", ""))
                    self._json({"ok": True, "preset": preset}, 201)
                    return
                recall_match = re.fullmatch(r"/api/presets/(\d{1,3})/recall", path)
                if recall_match:
                    number = int(recall_match.group(1))
                    if state.presets.get(number) is None:
                        raise ValueError("プリセットが登録されていません")
                    state.exposure.recall_preset(number)
                    self._json({"ok": True})
                    return
                remove_match = re.fullmatch(r"/api/presets/(\d{1,3})/remove", path)
                if remove_match:
                    number = int(remove_match.group(1))
                    if not state.presets.remove(number):
                        self._error("プリセットが登録されていません", 404)
                    else:
                        self._json({"ok": True})
                    return
                cancel_match = re.fullmatch(r"/api/schedules/([0-9a-f]{12})/cancel", path)
                if cancel_match:
                    if not state.recorder.cancel(cancel_match.group(1)):
                        self._error("予約が見つかりません", 404)
                    else:
                        self._json({"ok": True})
                    return
                if path == "/api/exposure":
                    mode = int(body["mode"])
                    iris = int(body["iris"]) if body.get("iris") is not None else None
                    shutter = int(body["shutter"]) if body.get("shutter") is not None else None
                    gain_limit = int(body["gain_limit"]) if body.get("gain_limit") is not None else None
                    state.exposure_guard.stop_for_external_change()
                    exposure = state.exposure.set(mode, iris, shutter, gain_limit)
                    self._json({"ok": True, "exposure": exposure})
                    return
                if path == "/api/exposure-guard":
                    guard = state.exposure_guard.configure(body)
                    self._json({"ok": True, "guard": guard})
                    return
                if path == "/api/exposure-guard/calibrate":
                    guard = state.exposure_guard.calibrate()
                    self._json({"ok": True, "guard": guard})
                    return
                self.send_error(404)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                self._error(str(exc), 400)
            except Exception as exc:
                logging.exception("POST failed")
                self._error(str(exc), 500)

    return Handler


def validate_install(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    ffmpeg_path = Path(config["ffmpeg_path"])
    if not ffmpeg_path.is_file():
        errors.append(f"ffmpeg not found: {ffmpeg_path}")
    if not ffprobe_path(config).is_file():
        errors.append("ffprobe not found: install it alongside ffmpeg or add it to PATH")
    if config.get("input_mode") not in {"rtsp", "rtmp-listen"}:
        errors.append("input_mode must be 'rtsp' or 'rtmp-listen'")
    if not isinstance(config.get("input_url"), str) or not config.get("input_url"):
        errors.append("input_url must be a non-empty string")
    for key in (
        "segment_seconds",
        "publisher_wait_seconds",
        "rtsp_timeout_microseconds",
        "retry_seconds",
        "preset_prepare_seconds",
        "preset_settle_seconds",
        "analysis_width",
        "analysis_height",
        "analysis_sample_seconds",
        "web_port",
        "minimum_free_bytes",
    ):
        try:
            if int(config[key]) <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            errors.append(f"{key} must be a positive integer")
    try:
        if float(config["estimated_bitrate_mbps"]) <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        errors.append("estimated_bitrate_mbps must be a positive number")
    try:
        if not 1.0 <= float(config.get("storage_safety_factor", 1.15)) <= 2.0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("storage_safety_factor must be between 1.0 and 2.0")
    if not WEB_DIR.joinpath("index.html").is_file():
        errors.append("web/index.html not found")
    try:
        ScheduleStore(SCHEDULE_PATH)
    except Exception as exc:
        errors.append(f"schedule store: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="KN20A scheduled recorder")
    parser.add_argument("--no-browser", action="store_true", help="do not open the dashboard")
    parser.add_argument("--check", action="store_true", help="validate files and exit")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "service.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    config = load_config()
    errors = validate_install(config)
    if args.check:
        if errors:
            for error in errors:
                print("ERROR:", error)
            return 1
        print("OK: configuration and required files are valid")
        return 0
    if errors:
        raise RuntimeError("; ".join(errors))

    app_state = AppState(config)
    # Bind the dashboard before starting the scheduler.  If the port is already
    # occupied, no background recorder/ffmpeg process is left behind.
    server = ThreadingHTTPServer(
        (str(config["web_host"]), int(config["web_port"])), make_handler(app_state)
    )
    url = f"http://{config['web_host']}:{config['web_port']}/"
    recorder_started = False
    previous_sigterm = signal.signal(signal.SIGTERM, handle_termination)
    try:
        app_state.recorder.start()
        app_state.exposure_guard.start()
        recorder_started = True
        logging.info("dashboard: %s", url)
        if not args.no_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logging.info("shutdown requested")
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        server.shutdown()
        server.server_close()
        if recorder_started:
            app_state.exposure_guard.shutdown()
            app_state.recorder.shutdown()
        signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        logging.exception("fatal recorder error")
        raise

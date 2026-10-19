#!/usr/bin/env python3
"""Cross-platform recorder supervisor (systemd may supervise it directly on Linux)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
LOG_DIR = APP_DIR / "logs"
LOCK_PATH = DATA_DIR / "supervisor.lock"
LOG_PATH = LOG_DIR / "supervisor.log"
CHILD_LOG_PATH = LOG_DIR / "supervisor_child.log"
RECORDER_PATH = APP_DIR / "recorder.py"
CHECK_HOST = "127.0.0.1"
CHECK_PORT = 8765


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now().astimezone().isoformat()} {message}\n")


def port_is_open() -> bool:
    try:
        with socket.create_connection((CHECK_HOST, CHECK_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def acquire_single_instance_lock():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+b")
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> int:
    lock_handle = acquire_single_instance_lock()
    if lock_handle is None:
        log("another supervisor is already running; exit")
        return 0

    log(f"supervisor started pid={os_getpid()}")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = None
    previous_sigterm = signal.signal(signal.SIGTERM, handle_termination)
    try:
        while True:
            if port_is_open():
                time.sleep(5)
                continue
            with CHILD_LOG_PATH.open("ab", buffering=0) as child_log:
                process = subprocess.Popen(
                    [sys.executable, str(RECORDER_PATH), "--no-browser"],
                    cwd=str(APP_DIR),
                    stdin=subprocess.DEVNULL,
                    stdout=child_log,
                    stderr=child_log,
                    creationflags=creationflags,
                )
                log(f"recorder started pid={process.pid}")
                try:
                    exit_code = process.wait()
                except KeyboardInterrupt:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    raise
                log(f"recorder exited code={exit_code}; restart in 5 seconds")
                process = None
            time.sleep(5)
    except KeyboardInterrupt:
        log("supervisor stopped")
        return 0
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        lock_handle.close()
        signal.signal(signal.SIGTERM, previous_sigterm)


def handle_termination(signum, frame):
    raise KeyboardInterrupt


def os_getpid() -> int:
    # Kept as a tiny wrapper so tests can import this module without side effects.
    import os

    return os.getpid()


if __name__ == "__main__":
    raise SystemExit(main())

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recorder
import supervisor


class PlatformTests(unittest.TestCase):
    def test_ffmpeg_command_resolves_from_path(self):
        with mock.patch.object(recorder.shutil, "which", return_value=sys.executable):
            self.assertEqual(recorder.resolve_executable("ffmpeg"), str(Path(sys.executable).resolve()))

    def test_ffprobe_sibling_uses_platform_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            for suffix in ("", ".exe"):
                probe = Path(directory) / ("ffprobe" + suffix)
                probe.touch()
                self.assertEqual(
                    recorder.ffprobe_path({"ffmpeg_path": str(Path(directory) / ("ffmpeg" + suffix))}),
                    probe,
                )

    def test_missing_probe_does_not_accept_arbitrary_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "recording.mp4"
            media.write_bytes(b"not an mp4")
            with mock.patch.object(recorder, "ffprobe_path", return_value=Path(directory) / "missing"):
                self.assertFalse(recorder.RecorderManager._valid_mp4(mock.Mock(config={}), media))

    def test_supervisor_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            supervisor, "DATA_DIR", Path(directory)
        ), mock.patch.object(supervisor, "LOCK_PATH", Path(directory) / "supervisor.lock"):
            first = supervisor.acquire_single_instance_lock()
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(supervisor.acquire_single_instance_lock())
            finally:
                first.close()
            second = supervisor.acquire_single_instance_lock()
            self.assertIsNotNone(second)
            second.close()

    def test_sigterm_enters_normal_shutdown(self):
        with self.assertRaises(KeyboardInterrupt):
            recorder.handle_termination(signal.SIGTERM, None)

    @unittest.skipUnless(os.name == "posix" and shutil.which("systemd-analyze"), "Requires systemd")
    def test_systemd_unit_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = (recorder.APP_DIR / "systemd" / "kn20a-recorder.service").read_text(encoding="utf-8")
            unit = unit.replace("%h/kn20a-recorder/.venv/bin/python", sys.executable)
            unit = unit.replace("%h/kn20a-recorder", str(root))
            path = root / "kn20a-recorder.service"
            path.write_text(unit, encoding="utf-8")
            result = subprocess.run(["systemd-analyze", "verify", str(path)],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    @unittest.skipUnless(os.name == "posix" and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "Requires Linux and FFmpeg/FFprobe")
    def test_linux_server_stops_cleanly_on_sigterm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(recorder.__file__, root / "recorder.py")
            shutil.copytree(recorder.APP_DIR / "recorder_app", root / "recorder_app")
            shutil.copytree(recorder.WEB_DIR, root / "web")
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            (root / "config.json").write_text(json.dumps({
                "ffmpeg_path": "ffmpeg", "web_port": port,
            }), encoding="utf-8")
            process = subprocess.Popen([sys.executable, str(root / "recorder.py"), "--no-browser"],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(process.communicate()[0].decode(errors="replace"))
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("dashboard startup timed out")
                process.terminate()
                output, _ = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg/FFprobe")
    def test_real_ffmpeg_remux_validates_mp4(self):
        from datetime import timedelta

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "recordings" / "test"
            run.mkdir(parents=True)
            for number in range(2):
                subprocess.run([
                    shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "color=c=black:s=64x64:r=10", "-t", "0.5", "-c:v", "mpeg4",
                    "-threads", "1", str(run / f"part_{number}.mkv"),
                ], check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            store = recorder.ScheduleStore(root / "schedules.json")
            start = recorder.now_jst() + timedelta(hours=1)
            item = store.create("test", start.isoformat(), (start + timedelta(minutes=1)).isoformat())
            store.update(item["id"], output_dir=str(run), status="finalizing")
            manager = recorder.RecorderManager({
                "output_dir_resolved": str(root / "recordings"),
                "ffmpeg_path": shutil.which("ffmpeg"),
                "keep_mkv_after_remux": False,
            }, store, object())
            with mock.patch.object(recorder, "LOG_DIR", root):
                manager._remux_schedule(item["id"])
            self.assertTrue(manager._valid_mp4(run / "recording.mp4"))
            self.assertFalse(list(run.glob("*.mkv")))
            self.assertEqual(store.get(item["id"])["status"], "completed")


if __name__ == "__main__":
    unittest.main()

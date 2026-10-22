"""Safety, recovery, capability and dispatch-contract regressions for Issue #1."""
from contextlib import ExitStack
from contextlib import nullcontext
from datetime import timedelta
import json
import io
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from unittest import mock

import recorder
from recorder_app.artifacts import Finalizer, digest, persist, frame_shapes
from recorder_app.configuration import normalize, to_v2
from recorder_app.controls import make_controls
from recorder_app.inputs import input_adapter
from recorder_app.preparation import PresetCoordinator, ControlConflict


class Contracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = recorder.now_jst()
        self.store = recorder.ScheduleStore(self.root / "schedules.json")

    def reservation(self, **kwargs):
        return self.store.create("test", (self.now + timedelta(seconds=20)).isoformat(),
                                 (self.now + timedelta(minutes=5)).isoformat(), **kwargs)

    def test_v1_migration_preserves_kn20a_and_legacy_policy(self):
        old = dict(recorder.DEFAULT_CONFIG)
        normalized = normalize(old)
        self.assertEqual(normalized["controls"]["presets"]["driver"], "kn20a")
        self.assertEqual(normalized["preset_failure_policy"], "legacy")
        migrated = normalize({**recorder.DEFAULT_CONFIG, **to_v2(normalized)})
        self.assertEqual(migrated["preset_failure_policy"], "legacy")
        self.assertEqual(migrated["input_url"], old["input_url"])
        self.assertNotIn("controls", old)

    def test_frame_metadata_field_order_is_not_assumed(self):
        data = io.BytesIO(json.dumps({"frames":[{"width":64,"height":96,"pix_fmt":"yuv420p","side_data_list":[{"side_data_type":"unregistered"}]},
                     {"pix_fmt":"yuv420p","height":96,"width":64}]}).encode())
        self.assertEqual(frame_shapes(data),{("64","96","yuv420p")})

    def test_generic_constructs_no_kn20a_clients(self):
        config = normalize({**recorder.DEFAULT_CONFIG, "schema_version": 2,
                            "input": {"type": "rtsp", "url": "rtsp://127.0.0.1/test"}})
        factory = mock.Mock(side_effect=AssertionError("KN20A must not be constructed"))
        ptz, exposure, capabilities = make_controls(config, factory)
        self.assertIsNone(ptz)
        self.assertIsNone(exposure)
        self.assertFalse(capabilities["analysis_ae"]["supported"])
        factory.assert_not_called()

    def test_roles_can_use_different_control_endpoints(self):
        config = normalize({**recorder.DEFAULT_CONFIG, "schema_version": 2,
            "input": {"type":"rtsp", "url":"rtsp://127.0.0.1/test"},
            "controls": {"presets":{"driver":"kn20a", "host":"192.0.2.1"},
                         "exposure":{"driver":"kn20a", "host":"192.0.2.2"}}})
        factory = mock.Mock(side_effect=lambda host, port: host)
        ptz, exposure, _ = make_controls(config, factory)
        self.assertNotEqual(ptz, exposure)

    def test_unsupported_driver_is_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            normalize({**recorder.DEFAULT_CONFIG, "schema_version": 2,
                "input":{"type":"rtsp", "url":"rtsp://127.0.0.1/test"},
                "controls":{"presets":{"driver":"onvif"}}})

    def test_protocol_timeout_units_are_independent(self):
        tcp = input_adapter({**recorder.DEFAULT_CONFIG, "rtsp_timeout_microseconds": 3000000}).arguments()
        rtmp = input_adapter({**recorder.DEFAULT_CONFIG, "input_mode":"rtmp-listen", "publisher_wait_seconds":7}).arguments()
        self.assertEqual(tcp[tcp.index("-timeout") + 1], "3000000")
        self.assertEqual(rtmp[rtmp.index("-timeout") + 1], "7")

    def test_old_reservation_does_not_inherit_new_defaults(self):
        item = self.reservation(preset_number=2)
        raw = self.store.get(item["id"])
        raw.pop("preset_failure_policy")
        self.store.save()
        loaded = recorder.ScheduleStore(self.store.path, default_policy="continue")
        self.assertEqual(loaded.get(item["id"])["preset_failure_policy"], "legacy")

    def test_new_reservation_snapshots_policy_and_deadline(self):
        self.store.default_policy = "require"
        self.store.default_deadline = 8
        item = self.reservation()
        self.store.default_policy = "continue"
        self.store.default_deadline = 2
        loaded = recorder.ScheduleStore(self.store.path, default_policy="continue")
        self.assertEqual(loaded.get(item["id"])["preset_failure_policy"], "require")
        self.assertEqual(loaded.get(item["id"])["preset_deadline_seconds"], 8)

    def test_next_preset_is_not_dispatched_while_previous_capture_owns_camera(self):
        item = self.reservation(preset_number=2)
        camera = mock.Mock()
        coordinator = PresetCoordinator(camera, self.store, {}, lambda:self.now)
        coordinator.set_owner("previous-recording")
        self.assertFalse(coordinator.ready(item))
        camera.recall_preset.assert_not_called()
        self.assertEqual(coordinator.jobs, {})

    def test_cancel_while_connecting_fences_actual_send(self):
        item = self.reservation(preset_number=2)
        connecting, release, finished = threading.Event(), threading.Event(), threading.Event()
        sent = []
        class Camera:
            def recall_preset(self, number, authorize):
                connecting.set()
                try:
                    release.wait(3)
                    with authorize(): sent.append(number)
                finally:
                    finished.set()
        coordinator = PresetCoordinator(Camera(), self.store, {"preset_settle_seconds":0}, lambda:self.now)
        coordinator.ready(item)
        self.assertTrue(connecting.wait(2))
        self.store.cancel(item["id"])
        coordinator.invalidate(item["id"])
        release.set()
        self.assertTrue(finished.wait(2))
        self.assertEqual(sent, [])
        coordinator.shutdown()

    def test_expired_require_is_failed_not_waited_forever(self):
        item = self.reservation(preset_number=2, preset_failure_policy="require", preset_deadline_seconds=5)
        late = self.now + timedelta(seconds=30)
        coordinator = PresetCoordinator(mock.Mock(), self.store, {}, lambda:late)
        self.assertFalse(coordinator.ready(item))
        self.assertEqual(self.store.get(item["id"])["status"], "failed")

    def test_continue_records_without_dispatching_late_preset(self):
        item = self.reservation(preset_number=2, preset_failure_policy="continue")
        current = self.now + timedelta(seconds=30)
        camera = mock.Mock()
        coordinator = PresetCoordinator(None, self.store, {}, lambda:current)
        self.assertTrue(coordinator.ready(item))
        self.assertIn("非対応", self.store.get(item["id"])["control_warning"])
        camera.assert_not_called()

    def test_manual_preset_is_blocked_during_capture(self):
        coordinator = PresetCoordinator(None, self.store, {}, lambda:self.now)
        coordinator.set_owner("capture")
        with self.assertRaises(ControlConflict): coordinator.manual(lambda:None)

    def test_in_flight_control_uncertainty_survives_restart(self):
        item = self.reservation(preset_number=2)
        done = threading.Event()
        class Camera:
            def recall_preset(self, number, authorize):
                with authorize(): pass
                done.set()
                raise TimeoutError("sent but no completion")
        coordinator = PresetCoordinator(Camera(),self.store,{"preset_settle_seconds":0},lambda:self.now)
        coordinator.ready(item)
        self.assertTrue(done.wait(2))
        self.assertTrue(coordinator.jobs[item["id"]]["done"].wait(3))
        for _ in range(100):
            if coordinator.uncertain: break
            time.sleep(0.01)
        self.assertTrue(coordinator.uncertain)
        restored = PresetCoordinator(Camera(),self.store,{},lambda:self.now)
        self.assertTrue(restored.uncertain)
        restored.acknowledge()
        self.assertFalse(self.store.get(item["id"])["control_uncertain"])

    def restore_coordinator(self, camera=None):
        return PresetCoordinator(camera, recorder.ScheduleStore(self.store.path),
                                 {"preset_settle_seconds": 0}, lambda: self.now)

    def test_manual_timeout_survives_restart_and_blocks_automatic_send(self):
        item = self.reservation(preset_number=2)
        coordinator = self.restore_coordinator()
        with self.assertRaises(TimeoutError):
            coordinator.manual(mock.Mock(side_effect=TimeoutError("sent, no completion")))
        camera = mock.Mock()
        restored = self.restore_coordinator(camera)
        self.assertTrue(restored.uncertain)
        self.assertFalse(restored.ready(item))
        self.assertEqual(restored.jobs, {})
        camera.recall_preset.assert_not_called()
        with self.assertRaises(ControlConflict): restored.manual(lambda: None)

    def test_manual_process_exit_after_dispatch_requires_acknowledgement(self):
        # Real process termination skips exception handlers and finally blocks.
        script = """
import os, sys
from pathlib import Path
import recorder
from recorder_app.preparation import PresetCoordinator
store = recorder.ScheduleStore(Path(sys.argv[1]))
coordinator = PresetCoordinator(None, store, {}, recorder.now_jst)
coordinator.manual(lambda: os._exit(73))
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.store.path)],
                                cwd=Path(recorder.__file__).parent, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))
        state = json.loads(self.store.path.with_name("schedules.control.json").read_text())
        self.assertTrue(state["in_flight"])
        restored = self.restore_coordinator()
        self.assertTrue(restored.uncertain)
        with self.assertRaises(ControlConflict): restored.manual(lambda: None)
        restored.acknowledge()
        self.assertFalse(self.restore_coordinator().uncertain)
        self.assertEqual(restored.manual(lambda: "confirmed"), "confirmed")

    def test_manual_success_does_not_leave_restart_hold(self):
        coordinator = self.restore_coordinator()
        self.assertEqual(coordinator.manual(lambda: "completed"), "completed")
        self.assertFalse(self.restore_coordinator().uncertain)

    def test_manual_journal_failure_prevents_dispatch(self):
        coordinator = self.restore_coordinator()
        operation = mock.Mock()
        with mock.patch("recorder_app.preparation.persist", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): coordinator.manual(operation)
        operation.assert_not_called()

    def test_manual_completion_persist_failure_keeps_restart_hold(self):
        coordinator = self.restore_coordinator()
        durable_persist = persist
        def fail_completion(path, state):
            if not state["in_flight"]: raise OSError("completion save failed")
            durable_persist(path, state)
        with mock.patch("recorder_app.preparation.persist", side_effect=fail_completion):
            with self.assertRaises(OSError): coordinator.manual(lambda: None)
        self.assertTrue(coordinator.uncertain)
        self.assertTrue(self.restore_coordinator().uncertain)

    def test_manual_failed_acknowledgement_does_not_release_hold(self):
        coordinator = self.restore_coordinator()
        with self.assertRaises(TimeoutError):
            coordinator.manual(mock.Mock(side_effect=TimeoutError()))
        with mock.patch("recorder_app.preparation.persist", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): coordinator.acknowledge()
        self.assertTrue(coordinator.uncertain)
        self.assertTrue(self.restore_coordinator().uncertain)

    def test_manual_acknowledgement_reenables_automatic_preparation(self):
        item = self.reservation(preset_number=2)
        coordinator = self.restore_coordinator()
        with self.assertRaises(TimeoutError):
            coordinator.manual(mock.Mock(side_effect=TimeoutError()))
        restored = self.restore_coordinator()
        restored.acknowledge()
        class Camera:
            def recall_preset(self, number, authorize):
                with authorize(): pass
        resumed = self.restore_coordinator(Camera())
        resumed.ready(item)
        self.assertTrue(resumed.jobs[item["id"]]["done"].wait(3))
        self.assertEqual(resumed.jobs[item["id"]]["state"], "success")

    def test_revision_change_before_send_is_fenced(self):
        item = self.reservation(preset_number=2)
        coordinator = PresetCoordinator(None,self.store,{},lambda:self.now)
        job = {"revision":0,"sent":False}
        coordinator.jobs[item["id"]] = job
        self.store.update(item["id"],revision=1)
        with self.assertRaises(ControlConflict):
            with coordinator.authorize(item,job): self.fail("must not dispatch")

    def test_stale_worker_cannot_send_after_deadline(self):
        item = self.reservation(preset_number=2,preset_failure_policy="require",preset_deadline_seconds=5)
        coordinator = PresetCoordinator(None,self.store,{},lambda:self.now+timedelta(seconds=30))
        job = {"revision":0,"sent":False}
        coordinator.jobs[item["id"]] = job
        with self.assertRaises(ControlConflict):
            with coordinator.authorize(item,job): self.fail("must not dispatch")

    def test_reconnection_budget_is_persisted_and_not_reset_by_restart(self):
        item = self.reservation(preset_number=2, preset_failure_policy="require", preset_deadline_seconds=5)
        self.store.update(item["id"], start=(self.now - timedelta(minutes=1)).isoformat(), status="reconnecting")
        coordinator = PresetCoordinator(None, self.store, {}, lambda: self.now)
        self.assertFalse(coordinator.ready(self.store.get(item["id"])))
        current = self.store.get(item["id"])
        self.assertEqual(current["status"], "reconnecting")
        self.assertEqual(current["preset_reconnect_started_at"], self.now.isoformat())
        later = self.now + timedelta(seconds=6)
        loaded = recorder.ScheduleStore(self.store.path)
        restored = PresetCoordinator(None, loaded, {}, lambda: later)
        self.assertFalse(restored.ready(loaded.get(item["id"])))
        self.assertEqual(loaded.get(item["id"])["status"], "failed")
        job = {"revision": 0, "sent": False}
        restored.jobs[item["id"]] = job
        loaded.update(item["id"], status="reconnecting")
        with self.assertRaises(ControlConflict):
            with restored.authorize(loaded.get(item["id"]), job): self.fail("late reconnect send")

    def test_restart_of_active_capture_requires_fresh_preset_confirmation(self):
        item = self.reservation(preset_number=2, preset_failure_policy="require")
        self.store.update(item["id"], start=(self.now-timedelta(seconds=30)).isoformat(), status="recording")
        manager = recorder.RecorderManager({**recorder.DEFAULT_CONFIG, "output_dir_resolved": str(self.root)}, self.store)
        with mock.patch.object(recorder, "LOG_DIR", self.root), mock.patch.object(manager.thread, "start"), mock.patch.object(manager.finalizer_thread, "start"):
            manager.start()
        restored = self.store.get(item["id"])
        self.assertEqual(restored["status"], "reconnecting")
        self.assertFalse(manager._prepare_preset(restored))
        self.assertEqual(self.store.get(item["id"])["status"], "reconnecting")

    def test_stream_urls_are_fully_redacted_before_log_write(self):
        urls = [
            "rtsp://camera.example/live?access_token=secret123&signature=xyz",
            "rtsps://user:basic-secret@camera.example/live",
            "rtmp://camera.example/live/path-secret?arbitrary=other-secret",
            "RTMPS://camera.example/live?token=rtmps-secret",
            "https://storage.example/path-secret?X-Amz-Credential=cred-secret&X-Amz-Signature=signed-secret",
            "http://user:http-secret@camera.example/path",
            "wss://camera.example/live?auth=ws-secret",
        ]
        manager = recorder.RecorderManager({**recorder.DEFAULT_CONFIG, "output_dir_resolved": str(self.root)}, self.store)
        manager.log_handle = io.BytesIO()
        manager.process = mock.Mock(stderr=io.BytesIO(("open " + " ".join(urls) + " failed\n").encode()))
        manager._read_recording_log(manager.process)
        saved = manager.log_handle.getvalue().decode()
        self.assertEqual(saved.count("[STREAM_URL_REDACTED]"), len(urls))
        self.assertTrue(saved.startswith("open "))
        self.assertTrue(saved.endswith(" failed\n"))
        for secret in ("secret", "secret123", "xyz", "camera.example", "storage.example"):
            self.assertNotIn(secret, saved)

    def test_require_resume_failure_respects_disabled_postprocessing(self):
        item = self.reservation(preset_number=2, preset_failure_policy="require", preset_deadline_seconds=5)
        folder = self.root / "recordings" / "run"
        folder.mkdir(parents=True)
        source = folder / "part_01.mkv"
        source.write_bytes(b"previously captured media")
        epoch = (self.now - timedelta(seconds=10)).isoformat()
        self.store.update(item["id"], status="reconnecting", start=(self.now-timedelta(minutes=1)).isoformat(),
                          output_dir=str(folder), preset_reconnect_started_at=epoch)
        manager = recorder.RecorderManager({**recorder.DEFAULT_CONFIG, "postprocess_mp4": False,
            "output_dir_resolved": str(folder.parent)}, self.store)
        manager.preparation.set_owner(item["id"])
        with mock.patch.object(recorder, "LOG_DIR", self.root), mock.patch.object(manager.thread, "start"), mock.patch.object(manager.finalizer_thread, "start"):
            manager.start()
        self.assertEqual(self.store.get(item["id"])["preset_reconnect_started_at"], epoch)
        with mock.patch.object(manager, "_queue_finalization") as queue:
            manager._tick()
        queue.assert_not_called()
        self.assertEqual(self.store.get(item["id"])["status"], "completed")
        self.assertEqual(self.store.get(item["id"])["outcome"], "partial")
        self.assertTrue(source.exists())
        self.assertIsNone(manager.preparation.owner)

    def test_recovery_after_end_queues_existing_mkv_before_missed(self):
        item = self.reservation()
        folder = self.root / "recordings" / "run"
        folder.mkdir(parents=True)
        (folder / "part_01.mkv").write_bytes(b"interrupted")
        self.store.update(item["id"], status="recording", end=(self.now-timedelta(seconds=1)).isoformat(), output_dir=str(folder))
        manager = recorder.RecorderManager({**recorder.DEFAULT_CONFIG, "output_dir_resolved":str(folder.parent)}, self.store)
        with mock.patch.object(recorder, "LOG_DIR", self.root), mock.patch.object(manager.thread,"start"), mock.patch.object(manager.finalizer_thread,"start"), mock.patch.object(manager,"_queue_finalization") as queue:
            manager.start()
        queue.assert_called_once_with(item["id"])
        self.assertTrue((folder / "part_01.mkv").exists())
        self.assertNotEqual(self.store.get(item["id"])["status"], "missed")

    def test_stale_owner_released_after_recording_start_failure(self):
        item = self.reservation()
        self.store.update(item["id"], start=(self.now-timedelta(seconds=1)).isoformat())
        manager = recorder.RecorderManager({**recorder.DEFAULT_CONFIG, "ffmpeg_path":"missing", "output_dir_resolved":str(self.root)}, self.store)
        manager._tick()
        self.assertIsNone(manager.preparation.owner)

    def test_reservation_end_during_reconnect_finalizes_without_restart(self):
        item = self.reservation()
        folder = self.root/"recordings"/"run"
        folder.mkdir(parents=True)
        (folder/"part_01.mkv").write_bytes(b"interrupted")
        self.store.update(item["id"],status="reconnecting",end=(self.now-timedelta(seconds=1)).isoformat(),output_dir=str(folder))
        manager = recorder.RecorderManager({**recorder.DEFAULT_CONFIG,"output_dir_resolved":str(folder.parent)},self.store)
        manager.preparation.set_owner(item["id"])
        with mock.patch.object(manager,"_queue_finalization") as queue:
            manager._tick()
        queue.assert_called_once_with(item["id"])
        self.assertIsNone(manager.preparation.owner)


    def test_reconnect_expiry_retains_sources_when_postprocessing_disabled(self):
        item = self.reservation()
        folder = self.root / "recordings" / "reconnect"
        folder.mkdir(parents=True)
        sources = [folder / "part_00000001_000000000.mkv",
                   folder / "part_00000001_000000001.mkv"]
        for source in sources:
            source.write_bytes(b"previously captured media")
        self.store.update(
            item["id"], status="reconnecting",
            start=(self.now - timedelta(minutes=2)).isoformat(),
            end=(self.now - timedelta(seconds=1)).isoformat(),
            output_dir=str(folder), gaps=[{"reason": "stream_exit"}],
        )
        manager = recorder.RecorderManager({
            **recorder.DEFAULT_CONFIG, "output_dir_resolved": str(folder.parent),
            "postprocess_mp4": False, "keep_mkv_after_remux": False,
        }, self.store)
        manager.preparation.set_owner(item["id"])
        with mock.patch.object(manager, "_queue_finalization") as queue:
            manager._tick()
        queue.assert_not_called()
        self.assertTrue(manager.finalize_queue.empty())
        self.assertEqual(self.store.get(item["id"])["status"], "completed")
        self.assertEqual(self.store.get(item["id"])["outcome"], "partial")
        self.assertTrue(all(source.exists() for source in sources))
        self.assertEqual(list(folder.glob("*.mp4")), [])
        self.assertIsNone(manager.preparation.owner)

    def test_restart_after_end_retains_sources_when_postprocessing_disabled(self):
        item = self.reservation()
        folder = self.root / "recordings" / "restart"
        folder.mkdir(parents=True)
        sources = [folder / "part_00000001_000000000.mkv",
                   folder / "part_00000001_000000001.mkv"]
        for source in sources:
            source.write_bytes(b"previously captured media")
        self.store.update(
            item["id"], status="recording",
            start=(self.now - timedelta(minutes=2)).isoformat(),
            end=(self.now - timedelta(seconds=1)).isoformat(),
            output_dir=str(folder),
        )
        manager = recorder.RecorderManager({
            **recorder.DEFAULT_CONFIG, "output_dir_resolved": str(folder.parent),
            "postprocess_mp4": False, "keep_mkv_after_remux": False,
        }, self.store)
        with (mock.patch.object(recorder, "LOG_DIR", self.root),
              mock.patch.object(manager.thread, "start"),
              mock.patch.object(manager.finalizer_thread, "start"),
              mock.patch.object(manager, "_queue_finalization") as queue):
            manager.start()
        queue.assert_not_called()
        self.assertTrue(manager.finalize_queue.empty())
        self.assertEqual(self.store.get(item["id"])["status"], "completed")
        self.assertEqual(self.store.get(item["id"])["outcome"], "partial")
        self.assertTrue(all(source.exists() for source in sources))
        self.assertEqual(list(folder.glob("*.mp4")), [])

    def test_packet_frame_and_decode_scans_use_configured_validation_timeout(self):
        finalizer = Finalizer({"ffmpeg_path": "ffmpeg", "validation_timeout_seconds": 601})
        calls = []
        metadata = {"streams": [{
            "codec_type": "video", "codec_name": "h264",
            "nb_read_packets": "1", "width": 64, "height": 64,
            "pix_fmt": "yuv420p",
        }], "format": {"duration": "1.0"}}

        def run(command, **kwargs):
            calls.append(kwargs["timeout"])
            if kwargs["timeout"] <= 300:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if "-count_packets" in command:
                return mock.Mock(returncode=0, stderr=b"", stdout=json.dumps(metadata).encode())
            if "-show_frames" in command:
                kwargs["stdout"].write(b'{"frames":[{"width":64,"height":64,"pix_fmt":"yuv420p"}]}')
                return mock.Mock(returncode=0, stderr=b"")
            return mock.Mock(returncode=0, stderr=b"")

        with mock.patch("recorder_app.artifacts.subprocess.run", side_effect=run):
            info = finalizer.inspect(self.root / "part_01.mkv")
            finalizer.validate(self.root / "recording.mp4", [{"media": info}])
        self.assertEqual(calls, [601.0] * 5)

    def test_cancel_during_bitrate_estimate_prevents_late_ffmpeg_start(self):
        item = self.reservation()
        self.store.update(item["id"], start=(self.now - timedelta(seconds=1)).isoformat())
        executable = self.root / "ffmpeg"
        executable.write_bytes(b"fake executable")
        manager = recorder.RecorderManager({
            **recorder.DEFAULT_CONFIG, "ffmpeg_path": str(executable),
            "output_dir_resolved": str(self.root), "minimum_free_bytes": 1,
        }, self.store)
        estimating, resume = threading.Event(), threading.Event()

        def estimate(_root):
            estimating.set()
            if not resume.wait(5):
                raise TimeoutError("test estimate gate did not resume")
            return 3.5, "mock"

        errors = []
        def start():
            try:
                manager._start_process(self.store.get(item["id"]))
            except Exception as exc:
                errors.append(exc)

        with (mock.patch.object(manager, "_estimate_bitrate_mbps", side_effect=estimate),
              mock.patch.object(recorder, "LOG_DIR", self.root),
              mock.patch.object(recorder.shutil, "disk_usage", return_value=mock.Mock(free=10 ** 12)),
              mock.patch.object(recorder.subprocess, "Popen") as popen):
            worker = threading.Thread(target=start)
            worker.start()
            try:
                self.assertTrue(estimating.wait(3))
                self.assertTrue(manager.cancel(item["id"]))
            finally:
                resume.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            popen.assert_not_called()
        self.assertEqual(self.store.get(item["id"])["status"], "cancelled")
        self.assertIsNone(manager.process)

    def test_cancel_during_spawn_stops_process_without_reverting_status(self):
        item = self.reservation()
        self.store.update(item["id"], start=(self.now - timedelta(seconds=1)).isoformat())
        executable = self.root / "ffmpeg"
        executable.write_bytes(b"fake executable")
        manager = recorder.RecorderManager({
            **recorder.DEFAULT_CONFIG, "ffmpeg_path": str(executable),
            "output_dir_resolved": str(self.root), "minimum_free_bytes": 1,
        }, self.store)
        spawning, release = threading.Event(), threading.Event()
        process = mock.Mock()
        process.returncode = None
        process.poll.side_effect = lambda: process.returncode
        def stop(timeout=None):
            process.returncode = 0
            return 0
        process.wait.side_effect = stop
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()

        def spawn(*args, **kwargs):
            spawning.set()
            if not release.wait(5):
                raise TimeoutError("test spawn gate did not resume")
            return process

        errors = []
        def start():
            try:
                manager._start_process(self.store.get(item["id"]))
            except Exception as exc:
                errors.append(exc)

        with (mock.patch.object(manager, "_estimate_bitrate_mbps", return_value=(3.5, "mock")),
              mock.patch.object(recorder, "LOG_DIR", self.root),
              mock.patch.object(recorder.shutil, "disk_usage", return_value=mock.Mock(free=10 ** 12)),
              mock.patch.object(recorder.subprocess, "Popen", side_effect=spawn)):
            starter = threading.Thread(target=start)
            starter.start()
            try:
                self.assertTrue(spawning.wait(3))
                cancel_result = []
                canceller = threading.Thread(target=lambda: cancel_result.append(manager.cancel(item["id"])))
                canceller.start()
            finally:
                release.set()
            starter.join(5)
            canceller.join(5)
            self.assertFalse(starter.is_alive())
            self.assertFalse(canceller.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(cancel_result, [True])
        self.assertEqual(self.store.get(item["id"])["status"], "cancelled")
        self.assertIsNone(manager.process)
        self.assertEqual(manager.active_id, "")


class GenericAPI(unittest.TestCase):
    def test_generic_api_refuses_control_and_creates_no_analysis(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            for name, file in (("SCHEDULE_PATH","schedules.json"),("PRESET_PATH","presets.json")):
                stack.enter_context(mock.patch.object(recorder,name,root/file))
            exposure = stack.enter_context(mock.patch.object(recorder,"ExposureClient",side_effect=AssertionError("Generic control")))
            guard = stack.enter_context(mock.patch.object(recorder,"ExposureGuard",side_effect=AssertionError("Generic analysis")))
            state = recorder.AppState({**recorder.DEFAULT_CONFIG, "schema_version":2,
                "input":{"type":"rtsp","url":"rtsp://127.0.0.1/test"}, "output_dir_resolved":str(root)})
            server = recorder.ThreadingHTTPServer(("127.0.0.1",0),recorder.make_handler(state))
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                for endpoint in ("/api/exposure","/api/presets"):
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(base+endpoint,timeout=3)
                    self.assertEqual(error.exception.code,422)
                    self.assertEqual(json.loads(error.exception.read())["code"],"unsupported_capability")
                payload = {"title":"test","start":(recorder.now_jst()+timedelta(minutes=1)).isoformat(),
                           "end":(recorder.now_jst()+timedelta(minutes=2)).isoformat(),"preset_number":2}
                request = urllib.request.Request(base+"/api/schedules",data=json.dumps(payload).encode(),
                    headers={"Content-Type":"application/json","X-KN20A-Token":state.token})
                with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request,timeout=3)
                self.assertEqual(error.exception.code,422)
                payload["preset_number"] = None
                request.data = json.dumps(payload).encode()
                with urllib.request.urlopen(request,timeout=3) as response:
                    self.assertEqual(response.status,201)
                    self.assertEqual(json.load(response)["schedule"]["preset_failure_policy"],"continue")
                exposure.assert_not_called()
                guard.assert_not_called()
            finally:
                server.shutdown(); server.server_close(); thread.join(3)


class KN20AAdapterIntegration(unittest.TestCase):
    def test_mock_http_and_visca_preserve_existing_contract(self):
        from recorder_app.kn20a import ExposureClient
        commands = []
        parameters = {"stExp": {
            "stExpMode":{"emExpMode":3,"byExpModeList":[3,10]},
            "stIris":{"nIris":12,"stIrisList":[{"nValue":12,"szName":"F1.8"}]},
            "stShutter":{"nShutter":18,"stShutterList":[{"nValue":18,"szName":"1/60"}]},
            "gainLimit":6,"again":0,"drc":3,"antiflicker":0}}
        class HTTP(recorder.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({"nRetVal":0,"stValue":[parameters]}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(payload)
            def log_message(self,*args): pass
        class VISCA(socketserver.BaseRequestHandler):
            def handle(self):
                commands.append(self.request.recv(64))
                self.request.sendall(b"\x90\x41\xff\x90\x51\xff")
        http = recorder.ThreadingHTTPServer(("127.0.0.1",0),HTTP)
        tcp = socketserver.TCPServer(("127.0.0.1",0),VISCA)
        threads = [threading.Thread(target=s.serve_forever,daemon=True) for s in (http,tcp)]
        for thread in threads: thread.start()
        try:
            client = ExposureClient("127.0.0.1",tcp.server_address[1])
            real = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def local(request,timeout):
                url = request.full_url.replace("http://127.0.0.1/",f"http://127.0.0.1:{http.server_port}/")
                return real.open(url,timeout=timeout)
            client.opener.open = local
            self.assertEqual(client.get()["iris"]["label"],"F1.8")
            client.recall_preset(2,authorize=lambda:nullcontext())
            client.save_preset(2)
            self.assertEqual(commands,[b"\x81\x01\x04\x3f\x02\x02\xff",b"\x81\x01\x04\x3f\x01\x02\xff"])
        finally:
            for server in (http,tcp): server.shutdown(); server.server_close()
            for thread in threads: thread.join(3)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg/FFprobe")
class RealArtifactSafety(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / "run"
        self.run.mkdir()
        self.config = {"ffmpeg_path":shutil.which("ffmpeg"),"output_dir_resolved":str(self.root),
                       "keep_mkv_after_remux":False,"minimum_free_bytes":1}
        self.finalizer = Finalizer(self.config)

    def segment(self,name="part_01.mkv",size="64x64",audio=None):
        path = self.run/name
        args = [shutil.which("ffmpeg"),"-v","error","-f","lavfi","-i",f"testsrc2=s={size}:r=10"]
        if audio: args += ["-f","lavfi","-i","sine=sample_rate=48000"]
        args += ["-t","1","-c:v","libx264","-preset","ultrafast","-g","10","-threads","1"]
        if audio: args += ["-c:a",audio]
        subprocess.run(args+[str(path)],capture_output=True,check=True,timeout=20)
        return path

    def require_reconnect_scenario(self, fail_recall=False, no_camera=False):
        clock = [recorder.now_jst()]
        start = clock[0] + timedelta(seconds=2)
        end = start + timedelta(minutes=2)
        store = recorder.ScheduleStore(self.root / "schedules.json")
        item = store.create("require reconnect", start.isoformat(), end.isoformat(),
                            preset_number=2, preset_failure_policy="require", preset_deadline_seconds=10)
        calls = []
        class Camera:
            def recall_preset(self, number, authorize):
                with authorize(): calls.append(number)
                if fail_recall and len(calls) == 2: raise TimeoutError("no reconnect completion")
        with mock.patch.object(recorder, "now_jst", side_effect=lambda: clock[0]):
            manager = recorder.RecorderManager({**recorder.DEFAULT_CONFIG, **self.config,
                "preset_settle_seconds": 0, "retry_seconds": 0}, store, Camera())
            captures = []
            def capture(current):
                captures.append(current["id"])
                self.segment(f"part_{len(captures):02d}.mkv")
                manager.process = mock.Mock()
                manager.process.poll.return_value = None
                manager.active_id = current["id"]
                manager.run_dir = self.run
                manager.last_progress_at = time.monotonic()
                manager.progress_started = True
                store.update(current["id"], status="recording", output_dir=str(self.run))
            with mock.patch.object(manager, "_start_process", side_effect=capture):
                manager._prepare_preset(item)
                self.assertTrue(manager.preparation.jobs[item["id"]]["done"].wait(3))
                clock[0] = start
                manager._tick()
                self.assertEqual(store.get(item["id"])["status"], "recording")
                clock[0] = start + timedelta(seconds=20)
                manager.process.poll.return_value = 1
                if no_camera: manager.preparation.camera = None
                manager._tick()  # Includes finish, fresh snapshot, and preparation.
                self.assertEqual(store.get(item["id"])["status"], "reconnecting")
                if not no_camera:
                    self.assertTrue(manager.preparation.jobs[item["id"]]["done"].wait(3))
                if fail_recall or no_camera:
                    clock[0] += timedelta(seconds=11)
                    manager._tick()
                    self.assertEqual(len(captures), 1)
                    self.assertIn("期限", store.get(item["id"])["capture_stop_reason"])
                else:
                    manager._tick()
                    self.assertEqual(len(captures), 2)
                    self.assertEqual(calls, [2, 2])
                    clock[0] = end
                    manager.process.poll.return_value = 0
                    manager._tick()
                self.assertEqual(store.get(item["id"])["status"], "finalizing")
                self.assertIsNone(manager.preparation.owner)
                self.assertEqual(manager.finalize_queue.get_nowait(), item["id"])
                manager._remux_schedule(item["id"])
        result = store.get(item["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "partial")
        if fail_recall or no_camera:
            self.assertIn("録画再開を中止", result["control_warning"])
        self.assertEqual(len(result["mp4_files"]), 1)
        self.assertTrue(Path(result["mp4_files"][0]).is_file())
        self.assertEqual(list(self.run.glob("part_*.mkv")), [])

    def test_require_after_twenty_seconds_disconnect_reconnect_and_finalize(self):
        self.require_reconnect_scenario()

    def test_require_reconnect_timeout_finalizes_existing_media(self):
        self.require_reconnect_scenario(fail_recall=True)

    def test_require_reconnect_without_control_finalizes_existing_media(self):
        self.require_reconnect_scenario(no_camera=True)

    def test_untrusted_existing_output_does_not_authorize_cleanup(self):
        source = self.segment()
        (self.run/"recording.mp4").write_bytes(b"broken")
        with mock.patch.object(self.finalizer,"validate",side_effect=RuntimeError("validation failed")):
            with self.assertRaises(RuntimeError): self.finalizer.finalize(self.run)
        self.assertTrue(source.exists())
        self.assertTrue(list(self.run.glob("unverified_*.mp4")))

    def test_audio_only_output_is_not_verified(self):
        source = self.segment()
        target = self.run/"audio_only.mp4"
        subprocess.run([shutil.which("ffmpeg"),"-v","error","-f","lavfi","-i","sine=sample_rate=48000",
                        "-t","0.5","-c:a","aac",str(target)],capture_output=True,check=True,timeout=20)
        with self.assertRaises(RuntimeError): self.finalizer.inspect(target)
        self.assertTrue(source.exists())

    def test_short_output_does_not_cover_source(self):
        source = self.segment()
        target = self.run/"short.mp4"
        subprocess.run([shutil.which("ffmpeg"),"-v","error","-i",str(source),"-t","0.2","-c","copy",str(target)],
                       capture_output=True,check=True,timeout=20)
        info = self.finalizer.inspect(source)
        with self.assertRaises(RuntimeError): self.finalizer.validate(target,[{"media":info}])
        self.assertTrue(source.exists())

    def test_audio_layout_change_is_grouped_not_blindly_concatenated(self):
        self.segment("part_01.mkv")
        self.segment("part_02.mkv",audio="aac")
        outputs,_ = self.finalizer.finalize(self.run)
        self.assertEqual(len(outputs),2)
        self.assertEqual([len(self.finalizer.inspect(Path(p))["fingerprint"]) for p in outputs],[1,2])

    def test_two_digit_attempts_have_numeric_chronology(self):
        self.segment("part_10.mkv")
        self.segment("part_2.mkv")
        self.config["keep_mkv_after_remux"] = True
        self.finalizer.finalize(self.run)
        manifest = json.loads((self.run/"manifest.json").read_text())
        self.assertEqual([s["name"] for s in manifest["sources"]],["part_2.mkv","part_10.mkv"])

    def test_legacy_part_remains_when_verification_fails(self):
        source = self.segment("part_01.mp4")
        with mock.patch.object(self.finalizer,"validate",side_effect=RuntimeError("broken")):
            with self.assertRaises(RuntimeError): self.finalizer.finalize(self.run)
        self.assertTrue(source.exists())

    def test_legacy_parts_honor_retention(self):
        self.segment("part_01.mp4")
        self.config["keep_mkv_after_remux"] = True
        outputs,state = self.finalizer.finalize(self.run)
        self.assertEqual(state,"verified")
        self.assertTrue((self.run/"part_01.mp4").exists())
        self.assertEqual(len(outputs),1)

    def test_verified_receipt_allows_idempotent_cleanup(self):
        source = self.segment()
        outputs,_ = self.finalizer.finalize(self.run)
        self.assertFalse(source.exists())
        again,_ = self.finalizer.finalize(self.run)
        self.assertEqual(outputs,again)

    def test_crash_before_unlink_is_recoverable(self):
        source = self.segment()
        original = Path.unlink
        def crash(path,*args,**kwargs):
            if path == source: raise OSError("simulated crash")
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,"unlink",crash):
            with self.assertRaises(OSError): self.finalizer.finalize(self.run)
        manifest = json.loads((self.run/"manifest.json").read_text())
        self.assertEqual(manifest["phase"],"verified")
        self.assertTrue(manifest["sources"][0]["deletion_authorized"])
        self.assertTrue(source.exists())
        self.finalizer.finalize(self.run)
        self.assertFalse(source.exists())

    def test_changed_output_prevents_resume_cleanup(self):
        source = self.segment()
        self.config["keep_mkv_after_remux"] = True
        outputs,_ = self.finalizer.finalize(self.run)
        Path(outputs[0]).write_bytes(b"corrupt")
        self.config["keep_mkv_after_remux"] = False
        with self.assertRaises(RuntimeError): self.finalizer.finalize(self.run)
        self.assertTrue(source.exists())

    def test_changed_resolution_is_separate_group(self):
        self.segment("part_01.mkv",size="64x64")
        self.segment("part_02.mkv",size="96x96")
        outputs,_ = self.finalizer.finalize(self.run)
        self.assertEqual(len(outputs),2)
        self.assertEqual([self.finalizer.inspect(Path(p))["fingerprint"][0]["width"] for p in outputs],[64,96])

    def partial_groups(self):
        self.segment("part_01.mkv", size="64x64")
        self.segment("part_02.mkv", size="96x96")
        def fail_second(output, sources):
            if output.name == "recording_002.mp4.partial":
                raise RuntimeError("second group failed")
            return Finalizer.validate(self.finalizer, output, sources)
        return fail_second

    def test_partial_success_is_reused_across_repeated_restarts(self):
        fail_second = self.partial_groups()
        completed = self.run / "recording_001.mp4"
        for attempt in range(3):
            self.finalizer = Finalizer(self.config)
            with mock.patch.object(self.finalizer, "validate", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "second group failed"):
                    self.finalizer.finalize(self.run)
            current = (digest(completed), completed.stat().st_mtime_ns)
            if attempt == 0: original = current
            self.assertEqual(current, original)
            self.assertEqual(len(list(self.run.glob("*.mp4"))), 1)
            self.assertEqual(list(self.run.glob("unverified_*.mp4")), [])
            manifest = json.loads((self.run / "manifest.json").read_text())
            self.assertEqual(manifest["phase"], "preparing")
            self.assertEqual(len(manifest["groups"]), 1)
            self.assertEqual(len(list(self.run.glob("part_*.mkv"))), 2)
        outputs, state = Finalizer(self.config).finalize(self.run)
        self.assertEqual(state, "verified")
        self.assertEqual(len(outputs), 2)
        self.assertEqual((digest(completed), completed.stat().st_mtime_ns), original)
        self.assertEqual(list(self.run.glob("part_*.mkv")), [])

    def test_changed_partial_source_prevents_reuse_and_cleanup(self):
        fail_second = self.partial_groups()
        with mock.patch.object(self.finalizer, "validate", side_effect=fail_second):
            with self.assertRaises(RuntimeError): self.finalizer.finalize(self.run)
        receipt = (self.run / "manifest.json").read_bytes()
        completed_hash = digest(self.run / "recording_001.mp4")
        (self.run / "part_01.mkv").write_bytes(b"modified original")
        resumed = Finalizer(self.config)
        with mock.patch.object(resumed, "validate") as validate:
            with self.assertRaisesRegex(RuntimeError, "Sources changed since preparation"):
                resumed.finalize(self.run)
        validate.assert_not_called()
        self.assertEqual((self.run / "manifest.json").read_bytes(), receipt)
        self.assertEqual(digest(self.run / "recording_001.mp4"), completed_hash)
        self.assertEqual(len(list(self.run.glob("part_*.mkv"))), 2)

    def test_changed_partial_output_is_regenerated_once_not_blindly_reused(self):
        fail_second = self.partial_groups()
        with mock.patch.object(self.finalizer, "validate", side_effect=fail_second):
            with self.assertRaises(RuntimeError): self.finalizer.finalize(self.run)
        completed = self.run / "recording_001.mp4"
        completed.write_bytes(b"corrupted output")
        for _ in range(2):
            resumed = Finalizer(self.config)
            with mock.patch.object(resumed, "validate", side_effect=fail_second):
                with self.assertRaises(RuntimeError): resumed.finalize(self.run)
            self.assertNotEqual(completed.read_bytes(), b"corrupted output")
            self.assertEqual(len(list(self.run.glob("unverified_*.mp4"))), 1)
            self.assertEqual(len(list(self.run.glob("part_*.mkv"))), 2)

    def test_g711_is_kept_without_silent_audio_conversion(self):
        source = self.segment(audio="pcm_alaw")
        outputs,state = self.finalizer.finalize(self.run)
        self.assertEqual((outputs,state),([],"kept_mkv"))
        self.assertTrue(source.exists())

    def test_corrupt_source_is_retained(self):
        source = self.segment()
        source.write_bytes(source.read_bytes()[:200])
        with self.assertRaises(RuntimeError): self.finalizer.finalize(self.run)
        self.assertTrue(source.exists())

    def test_source_change_after_verification_is_not_deleted(self):
        source = self.segment()
        self.config["keep_mkv_after_remux"] = True
        self.finalizer.finalize(self.run)
        source.write_bytes(b"replaced")
        self.config["keep_mkv_after_remux"] = False
        with self.assertRaises(RuntimeError): self.finalizer.finalize(self.run)
        self.assertTrue(source.exists())

    def test_receipt_with_missing_coverage_is_rejected(self):
        source = self.segment()
        self.config["keep_mkv_after_remux"] = True
        self.finalizer.finalize(self.run)
        receipt = self.run/"manifest.json"
        manifest = json.loads(receipt.read_text())
        manifest["groups"] = []
        persist(receipt,manifest)
        self.config["keep_mkv_after_remux"] = False
        with self.assertRaises(RuntimeError): self.finalizer.finalize(self.run)
        self.assertTrue(source.exists())


if __name__ == "__main__": unittest.main()

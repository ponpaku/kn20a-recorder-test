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

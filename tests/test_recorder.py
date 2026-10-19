import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import timedelta
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "recorder.py"
SPEC = importlib.util.spec_from_file_location("kn20a_recorder", MODULE_PATH)
recorder = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = recorder
SPEC.loader.exec_module(recorder)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        log_patch = mock.patch.object(recorder, "LOG_DIR", Path(directory.name))
        log_patch.start()
        self.addCleanup(log_patch.stop)

    @staticmethod
    def exposure(iris=6, shutter=28, gain=0):
        return {
            "iris": {"value": iris},
            "gain": gain,
            "shutter": {
                "value": shutter,
                "options": [{"value": value} for value in range(17, 34)],
            },
        }

    def test_parse_naive_datetime_as_jst(self):
        value = recorder.parse_datetime("2026-09-10T18:30:00")
        self.assertEqual(value.utcoffset(), timedelta(hours=9))
        self.assertEqual(value.hour, 18)

    def test_parse_offset_datetime_normalized_to_jst(self):
        value = recorder.parse_datetime("2026-09-10T09:30:00+00:00")
        self.assertEqual(value.hour, 18)

    def test_safe_title(self):
        self.assertEqual(recorder.safe_title('夕景:夜景 / test?'), "夕景_夜景_test")

    def test_schedule_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            store = recorder.ScheduleStore(Path(directory) / "schedules.json")
            base = recorder.now_jst() + timedelta(hours=1)
            store.create("A", base.isoformat(), (base + timedelta(hours=1)).isoformat())
            with self.assertRaisesRegex(ValueError, "重なって"):
                store.create(
                    "B",
                    (base + timedelta(minutes=30)).isoformat(),
                    (base + timedelta(hours=2)).isoformat(),
                )

    def test_schedule_accepts_touching_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            store = recorder.ScheduleStore(Path(directory) / "schedules.json")
            base = recorder.now_jst() + timedelta(hours=1)
            store.create("A", base.isoformat(), (base + timedelta(hours=1)).isoformat())
            second = store.create(
                "B",
                (base + timedelta(hours=1)).isoformat(),
                (base + timedelta(hours=2)).isoformat(),
            )
            self.assertEqual(second["status"], "pending")

    def test_schedule_keeps_selected_preset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = recorder.ScheduleStore(Path(directory) / "schedules.json")
            base = recorder.now_jst() + timedelta(hours=1)
            item = store.create("A", base.isoformat(), (base + timedelta(hours=1)).isoformat(), 0)
            self.assertEqual(item["preset_number"], 0)

    def test_preset_store_supports_zero_and_254(self):
        with tempfile.TemporaryDirectory() as directory:
            store = recorder.PresetStore(Path(directory) / "presets.json")
            store.upsert(0, "復帰位置")
            store.upsert(254, "端位置")
            self.assertEqual([item["number"] for item in store.list()], [0, 254])

    def test_preset_store_rejects_out_of_range_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = recorder.PresetStore(Path(directory) / "presets.json")
            with self.assertRaises(ValueError):
                store.upsert(-1, "invalid")
            with self.assertRaises(ValueError):
                store.upsert(255, "invalid")

    def test_preset_prepare_recalls_once_and_memoizes(self):
        class FakeCamera:
            def __init__(self):
                self.calls = []

            def recall_preset(self, number):
                self.calls.append(number)

        with tempfile.TemporaryDirectory() as directory:
            store = recorder.ScheduleStore(Path(directory) / "schedules.json")
            base = recorder.now_jst() + timedelta(hours=1)
            item = store.create("A", base.isoformat(), (base + timedelta(hours=1)).isoformat(), 7)
            camera = FakeCamera()
            manager = recorder.RecorderManager(
                {"output_dir_resolved": directory, "preset_settle_seconds": 0, "retry_seconds": 1},
                store,
                camera,
            )
            self.assertTrue(manager._prepare_preset(item))
            self.assertTrue(manager._prepare_preset(item))
            self.assertEqual(camera.calls, [7])
            manager.active_id = item["id"]
            manager._finish_process(1)
            self.assertNotIn(item["id"], manager.prepared_presets)
            manager.next_preset_retry_at.pop(item["id"], None)
            self.assertTrue(manager._prepare_preset(item))
            self.assertEqual(camera.calls, [7, 7])

    def test_completed_schedule_cannot_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            store = recorder.ScheduleStore(Path(directory) / "schedules.json")
            base = recorder.now_jst() + timedelta(hours=1)
            item = store.create("A", base.isoformat(), (base + timedelta(hours=1)).isoformat())
            store.update(item["id"], status="completed")
            self.assertIsNone(store.cancel(item["id"]))
            self.assertEqual(store.get(item["id"])["status"], "completed")

    def test_remux_schedule_replaces_verified_mkv_with_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recordings"
            run_dir = root / "recording"
            run_dir.mkdir(parents=True)
            source = run_dir / "part_01.mkv"
            source.write_bytes(b"mkv")
            store = recorder.ScheduleStore(Path(directory) / "schedules.json")
            base = recorder.now_jst() + timedelta(hours=1)
            item = store.create("A", base.isoformat(), (base + timedelta(hours=1)).isoformat())
            store.update(item["id"], status="finalizing", output_dir=str(run_dir))
            manager = recorder.RecorderManager(
                {
                    "output_dir_resolved": str(root),
                    "ffmpeg_path": "ffmpeg.exe",
                    "keep_mkv_after_remux": False,
                },
                store,
                object(),
            )

            def fake_run(args, **kwargs):
                Path(args[-1]).write_bytes(b"mp4")
                return mock.Mock(returncode=0)

            with mock.patch.object(recorder.subprocess, "run", side_effect=fake_run), mock.patch.object(
                manager, "_valid_mp4", side_effect=lambda path: path.is_file() and path.stat().st_size > 0
            ):
                manager._remux_schedule(item["id"])

            result = store.get(item["id"])
            self.assertEqual(result["status"], "completed")
            self.assertFalse(source.exists())
            self.assertTrue(run_dir.joinpath("recording.mp4").exists())
            self.assertEqual(result["mp4_files"], [str(run_dir / "recording.mp4")])

    def test_remux_schedule_combines_legacy_mp4_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recordings"
            run_dir = root / "recording"
            run_dir.mkdir(parents=True)
            parts = [run_dir / "part_01.mp4", run_dir / "part_02.mp4"]
            for part in parts:
                part.write_bytes(b"mp4")
            store = recorder.ScheduleStore(Path(directory) / "schedules.json")
            base = recorder.now_jst() + timedelta(hours=1)
            item = store.create("A", base.isoformat(), (base + timedelta(hours=1)).isoformat())
            store.update(item["id"], status="finalizing", output_dir=str(run_dir))
            manager = recorder.RecorderManager(
                {
                    "output_dir_resolved": str(root),
                    "ffmpeg_path": "ffmpeg.exe",
                    "keep_mkv_after_remux": False,
                },
                store,
                object(),
            )

            def fake_run(args, **kwargs):
                self.assertIn("concat", args)
                Path(args[-1]).write_bytes(b"combined")
                return mock.Mock(returncode=0)

            with mock.patch.object(recorder.subprocess, "run", side_effect=fake_run), mock.patch.object(
                manager, "_valid_mp4", side_effect=lambda path: path.is_file() and path.stat().st_size > 3
            ):
                manager._remux_schedule(item["id"])
            self.assertTrue(run_dir.joinpath("recording.mp4").exists())
            self.assertTrue(all(not part.exists() for part in parts))
            self.assertEqual(store.get(item["id"])["mp4_files"], [str(run_dir / "recording.mp4")])

    def test_remux_failure_keeps_original_mkv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recordings"
            run_dir = root / "recording"
            run_dir.mkdir(parents=True)
            source = run_dir / "part_01.mkv"
            source.write_bytes(b"mkv")
            store = recorder.ScheduleStore(Path(directory) / "schedules.json")
            base = recorder.now_jst() + timedelta(hours=1)
            item = store.create("A", base.isoformat(), (base + timedelta(hours=1)).isoformat())
            store.update(item["id"], status="finalizing", output_dir=str(run_dir))
            manager = recorder.RecorderManager(
                {
                    "output_dir_resolved": str(root),
                    "ffmpeg_path": "ffmpeg.exe",
                    "keep_mkv_after_remux": False,
                },
                store,
                object(),
            )
            with mock.patch.object(
                recorder.subprocess, "run", return_value=mock.Mock(returncode=1)
            ), mock.patch.object(manager, "_valid_mp4", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "MP4"):
                    manager._remux_schedule(item["id"])
            self.assertTrue(source.exists())

    def test_storage_estimate_uses_recent_median_bitrate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recordings"
            root.mkdir()
            for index, bitrate in enumerate((3.0, 4.0, 20.0)):
                path = root / f"sample_{index}.mp4"
                size = int(bitrate * 100 * 1_000_000 / 8)
                with path.open("wb") as handle:
                    handle.seek(size - 1)
                    handle.write(b"0")
            store = recorder.ScheduleStore(Path(directory) / "schedules.json")
            manager = recorder.RecorderManager(
                {
                    "output_dir_resolved": str(root),
                    "ffmpeg_path": "ffmpeg.exe",
                    "estimated_bitrate_mbps": 3.5,
                },
                store,
                object(),
            )
            with mock.patch.object(manager, "_probe_duration_seconds", return_value=100.0):
                bitrate, source = manager._estimate_bitrate_mbps(root)
            self.assertAlmostEqual(bitrate, 4.0, places=2)
            self.assertEqual(source, "直近実績3本")

    def test_manual_stop_shortens_schedule_and_preserves_planned_end(self):
        with tempfile.TemporaryDirectory() as directory:
            store = recorder.ScheduleStore(Path(directory) / "schedules.json")
            start = recorder.now_jst() - timedelta(minutes=5)
            planned_end = recorder.now_jst() + timedelta(hours=1)
            item = store.create("A", start.isoformat(), planned_end.isoformat())
            store.update(item["id"], status="recording")
            manager = recorder.RecorderManager(
                {"output_dir_resolved": directory}, store, object()
            )
            manager.active_id = item["id"]
            manager.process = object()
            with mock.patch.object(manager, "_stop_process") as stop:
                result = manager.stop_active_recording()
            stop.assert_called_once_with(graceful=True)
            self.assertEqual(result["status"], "stopping")
            self.assertEqual(result["planned_end"], item["end"])
            self.assertLess(recorder.parse_datetime(result["end"]), planned_end)
            self.assertIn("manually_stopped_at", result)

    def test_guard_speeds_up_when_iris_is_too_closed(self):
        value = recorder.choose_guard_shutter(self.exposure(iris=5, shutter=28), 6, 9, 18, 33)
        self.assertEqual(value, 29)

    def test_guard_slows_down_when_iris_is_too_open(self):
        value = recorder.choose_guard_shutter(self.exposure(iris=10, shutter=28), 6, 9, 18, 33)
        self.assertEqual(value, 27)

    def test_guard_holds_inside_dead_band(self):
        value = recorder.choose_guard_shutter(self.exposure(iris=7, shutter=28), 6, 9, 18, 33)
        self.assertIsNone(value)

    def test_guard_respects_shutter_limits(self):
        self.assertIsNone(recorder.choose_guard_shutter(self.exposure(iris=4, shutter=33), 6, 9, 18, 33))
        self.assertIsNone(recorder.choose_guard_shutter(self.exposure(iris=10, shutter=18), 6, 9, 18, 33))

    def test_guard_prefers_slower_shutter_over_gain(self):
        value = recorder.choose_guard_shutter(self.exposure(iris=5, shutter=25, gain=1), 5, 6, 18, 28)
        self.assertEqual(value, 24)

    def test_guard_does_not_force_unsafe_jump_from_faster_manual_shutter(self):
        value = recorder.choose_guard_shutter(self.exposure(iris=4, shutter=33), 5, 6, 18, 28)
        self.assertIsNone(value)

    def test_guard_returns_from_faster_manual_shutter_only_when_scene_is_dark(self):
        value = recorder.choose_guard_shutter(self.exposure(iris=8, shutter=33), 5, 6, 18, 28)
        self.assertEqual(value, 32)

    def test_guard_returns_from_faster_manual_shutter_when_gain_is_active(self):
        value = recorder.choose_guard_shutter(self.exposure(iris=5, shutter=33, gain=1), 5, 6, 18, 28)
        self.assertEqual(value, 32)

    def test_guard_translates_range_for_50hz_bank(self):
        exposure = {
            "iris": {"value": 7},
            "gain": 0,
            "shutter": {
                "value": 11,
                "options": [{"value": value} for value in range(17)],
            },
        }
        value = recorder.choose_guard_shutter(exposure, 5, 6, 18, 28)
        self.assertEqual(value, 10)

    def test_guard_requires_two_matching_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = recorder.ExposureGuard(object(), Path(directory) / "guard.json")
            self.assertFalse(guard._confirm_candidate(27))
            self.assertTrue(guard._confirm_candidate(27))
            self.assertFalse(guard._confirm_candidate(None))
            self.assertFalse(guard._confirm_candidate(26))

    def test_luma_frame_statistics(self):
        stats = recorder.analyze_luma_frame(bytes([0, 10, 100, 200, 240, 255]))
        self.assertAlmostEqual(stats["y_avg"], 134.1666, places=3)
        self.assertEqual(stats["y_p50"], 100)
        self.assertEqual(stats["y_p90"], 255)
        self.assertAlmostEqual(stats["white_fraction"], 2 / 6)
        self.assertTrue(stats["valid"])

    def test_tiled_metering_reduces_small_extreme_regions(self):
        frame = bytes([255 if x < 2 else 80 for _y in range(10) for x in range(20)])
        stats = recorder.analyze_luma_frame(frame, width=20, height=10)
        self.assertLess(stats["metered_yavg"], stats["y_avg"])
        self.assertLess(stats["metered_white_fraction"], stats["white_fraction"])

    @staticmethod
    def image_exposure(shutter=28, iris=5, gain=0, gain_limit=6):
        labels = {
            17: "1/30", 18: "1/60", 19: "1/90", 20: "1/100", 21: "1/125",
            22: "1/180", 23: "1/250", 24: "1/350", 25: "1/500",
            26: "1/725", 27: "1/1000", 28: "1/1500", 29: "1/2000",
            30: "1/3000",
        }
        return {
            "iris": {"value": iris, "label": recorder.IRIS_LABELS[iris], "actual": True},
            "gain": gain,
            "gain_limit": gain_limit,
            "shutter": {
                "value": shutter,
                "label": labels[shutter],
                "options": [
                    {"value": value, "label": label} for value, label in labels.items()
                ],
            }
        }

    @staticmethod
    def luma(avg=96, high=163, white=0.0, valid=True, difference=1.0):
        return {
            "y_avg": avg,
            "y_p90": high,
            "white_fraction": white,
            "valid": valid,
            "frame_difference": difference,
        }

    def test_image_controller_slows_shutter_when_dark(self):
        action, value = recorder.choose_image_exposure_action(
            self.luma(avg=70, high=120), self.image_exposure(28),
            96, 163, 0.10, 0.01, 60, 3000,
        )
        self.assertEqual((action, value), ("slower", 27))

    def test_image_controller_speeds_shutter_for_highlight_clipping(self):
        action, value = recorder.choose_image_exposure_action(
            self.luma(white=0.02), self.image_exposure(28),
            96, 163, 0.10, 0.01, 60, 3000,
        )
        self.assertEqual((action, value), ("faster", 29))

    def test_image_controller_ignores_small_highlight_in_dark_scene(self):
        action, value = recorder.choose_image_exposure_action(
            self.luma(avg=65, high=105, white=0.03), self.image_exposure(28),
            96, 163, 0.10, 0.01, 60, 3000,
        )
        self.assertEqual((action, value), ("slower", 27))

    def test_image_controller_opens_iris_after_slowest_shutter(self):
        action, value = recorder.choose_image_exposure_action(
            self.luma(avg=50, high=90), self.image_exposure(18),
            96, 163, 0.10, 0.01, 60, 3000,
        )
        self.assertEqual((action, value), ("iris_open", 6))

    def test_image_controller_never_closes_beyond_iris_limit(self):
        action, value = recorder.choose_image_exposure_action(
            self.luma(avg=130, high=210), self.image_exposure(18, iris=5),
            96, 163, 0.10, 0.01, 60, 3000, 5,
        )
        self.assertEqual((action, value), ("faster", 19))

    def test_image_controller_adds_gain_only_at_open_iris_and_slowest_shutter(self):
        action, value = recorder.choose_image_exposure_action(
            self.luma(avg=50, high=90), self.image_exposure(18, iris=12, gain=0),
            96, 163, 0.10, 0.01, 60, 3000, 5,
        )
        self.assertEqual((action, value), ("gain_up", 1))

    def test_image_controller_respects_quality_gain_limit(self):
        action, value = recorder.choose_image_exposure_action(
            self.luma(avg=25, high=45), self.image_exposure(18, iris=12, gain=3),
            96, 163, 0.10, 0.10, 60, 3000, 5, 3,
        )
        self.assertEqual((action, value), ("limit", None))

    def test_natural_night_rolls_target_without_requesting_gain(self):
        exposure = self.image_exposure(18, iris=12, gain=0)
        stats = self.luma(avg=75, high=127)
        avg, high, scale = recorder.effective_image_targets(
            stats, exposure, 96, 163, 0.10, "natural", 60, 3000
        )
        self.assertGreater(scale, 0.60)
        self.assertLess(scale, 1.0)
        action, value = recorder.choose_image_exposure_action(
            stats, exposure, avg, high, 0.10, 0.10, 60, 3000, 5, 3
        )
        self.assertEqual((action, value), ("hold", None))

    def test_natural_night_uses_gain_below_floor(self):
        exposure = self.image_exposure(18, iris=12, gain=0)
        stats = self.luma(avg=35, high=60)
        avg, high, scale = recorder.effective_image_targets(
            stats, exposure, 96, 163, 0.10, "natural", 60, 3000
        )
        self.assertEqual(scale, 0.60)
        action, value = recorder.choose_image_exposure_action(
            stats, exposure, avg, high, 0.10, 0.10, 60, 3000, 5, 3
        )
        self.assertEqual((action, value), ("gain_up", 1))

    def test_visibility_policy_keeps_day_target(self):
        avg, high, scale = recorder.effective_image_targets(
            self.luma(avg=35, high=60), self.image_exposure(18, iris=12),
            96, 163, 0.10, "visibility", 60, 3000
        )
        self.assertEqual((avg, high, scale), (96, 163, 1.0))

    def test_exposure_compensation_uses_photographic_ev_scale(self):
        avg, high, factor = recorder.compensated_image_targets(96, 160, -0.7)
        self.assertAlmostEqual(factor, 2 ** -0.7)
        self.assertAlmostEqual(avg, 96 * (2 ** -0.7))
        self.assertAlmostEqual(high, 160 * (2 ** -0.7))

    def test_nearly_black_night_frame_remains_valid_for_brightening(self):
        frame = bytes([0] * 57500 + [1] * 100)
        stats = recorder.analyze_luma_frame(frame, width=320, height=180)
        self.assertTrue(stats["valid"])
        action, value = recorder.choose_image_exposure_action(
            stats,
            self.image_exposure(17, iris=12, gain=6, gain_limit=10),
            58,
            101,
            0.10,
            0.10,
            30,
            3000,
            7,
            10,
        )
        self.assertEqual((action, value), ("gain_up", 7))

    def test_uniform_digital_black_frame_is_rejected(self):
        stats = recorder.analyze_luma_frame(bytes([0] * 57600), width=320, height=180)
        self.assertFalse(stats["valid"])

    def test_image_controller_removes_gain_before_closing_iris(self):
        action, value = recorder.choose_image_exposure_action(
            self.luma(avg=140, high=220), self.image_exposure(18, iris=12, gain=2),
            96, 163, 0.10, 0.01, 60, 3000, 5,
        )
        self.assertEqual((action, value), ("gain_down", 1))

    def test_image_controller_does_not_reverse_a_better_coarse_step(self):
        action, value = recorder.choose_image_exposure_action(
            self.luma(avg=106.1, high=180), self.image_exposure(18),
            96, 163, 0.10, 0.01, 60, 3000,
        )
        self.assertEqual((action, value), ("hold", None))

    def test_guard_is_available_but_starts_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = recorder.ExposureGuard(object(), Path(directory) / "guard.json")
            snapshot = guard.snapshot()
            self.assertTrue(snapshot["available"])
            self.assertFalse(snapshot["enabled"])

    def test_guard_rejects_highlight_target_below_average(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = recorder.ExposureGuard(object(), Path(directory) / "guard.json")
            with self.assertRaisesRegex(ValueError, "P90"):
                guard.configure({"enabled": False, "target_yavg": 180, "target_yhigh": 170})

    def test_guard_validates_and_rounds_exposure_compensation(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = recorder.ExposureGuard(object(), Path(directory) / "guard.json")
            snapshot = guard.configure(
                {"enabled": False, "exposure_compensation_ev": -0.66}
            )
            self.assertEqual(snapshot["exposure_compensation_ev"], -0.7)
            self.assertAlmostEqual(snapshot["effective_target_yavg"], 97 * (2 ** -0.7))
            with self.assertRaisesRegex(ValueError, "露出補正"):
                guard.configure({"enabled": False, "exposure_compensation_ev": 1.1})

    def test_guard_syncs_camera_gain_limit_and_preserves_current_gain(self):
        class FakeExposure:
            def __init__(self):
                self.calls = []
                self.current = {
                    "mode": {"value": 3, "label": "Manual", "options": []},
                    "iris": {"value": 7, "label": "F4.0", "options": []},
                    "shutter": {
                        "value": 18,
                        "label": "1/60",
                        "options": [
                            {"value": 17, "label": "1/30"},
                            {"value": 18, "label": "1/60"},
                            {"value": 19, "label": "1/90"},
                            {"value": 33, "label": "1/3000"},
                        ],
                    },
                    "gain": 6,
                    "gain_limit": 6,
                    "drc": 3,
                }

            def get(self):
                return self.current

            def set(self, mode, iris, shutter, gain_limit, gain=0):
                self.calls.append((mode, iris, shutter, gain_limit, gain))
                self.current["iris"] = {**self.current["iris"], "value": iris, "label": recorder.IRIS_LABELS[iris]}
                label = next(item["label"] for item in self.current["shutter"]["options"] if item["value"] == shutter)
                self.current["shutter"] = {**self.current["shutter"], "value": shutter, "label": label}
                self.current["gain_limit"] = gain_limit
                self.current["gain"] = gain
                return self.current

        with tempfile.TemporaryDirectory() as directory:
            exposure = FakeExposure()
            guard = recorder.ExposureGuard(exposure, Path(directory) / "guard.json")
            guard.settings["enabled"] = True
            snapshot = guard.configure(
                {
                    "enabled": True,
                    "manual_iris": 7,
                    "slowest_denominator": 30,
                    "quality_gain_limit": 10,
                }
            )
            self.assertEqual(exposure.calls[-1], (3, 7, 18, 10, 6))
            self.assertEqual(snapshot["slowest_denominator"], 30)
            self.assertEqual(snapshot["quality_gain_limit"], 10)

    def test_guard_migrates_old_white_limit_and_marks_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guard.json"
            path.write_text('{"enabled":true,"phase":"day_manual","white_limit":0.01}', encoding="utf-8")
            guard = recorder.ExposureGuard(object(), path)
            snapshot = guard.snapshot()
            self.assertEqual(snapshot["white_limit"], 0.10)
            self.assertEqual(snapshot["phase"], "recovering")
            self.assertTrue(guard.resume_pending)


class LumaAnalyzerProcessTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self, timeout_on_terminate=False):
            self.pid = 43210
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.returncode = None
            self.terminate_called = False
            self.kill_called = False
            self.wait_calls = 0
            self.timeout_on_terminate = timeout_on_terminate
            self._handle = 123

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_called = True

        def kill(self):
            self.kill_called = True
            self.returncode = -9

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.timeout_on_terminate and not self.kill_called:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            if self.returncode is None:
                self.returncode = -15
            return self.returncode

    def make_analyzer(self, **overrides):
        config = {
            **recorder.DEFAULT_CONFIG,
            "ffmpeg_path": "ffmpeg.exe",
            "analysis_ws_url": "ws://camera/live",
            **overrides,
        }
        job = mock.Mock()
        with mock.patch.object(recorder, "WindowsKillOnCloseJob", return_value=job):
            analyzer = recorder.LumaAnalyzer(config)
        return analyzer, job

    def test_websocket_exception_always_reaps_ffmpeg(self):
        analyzer, job = self.make_analyzer()
        process = self.FakeProcess()
        with (
            mock.patch.object(recorder.subprocess, "Popen", return_value=process),
            mock.patch.object(
                analyzer,
                "_feed_websocket",
                mock.AsyncMock(side_effect=RuntimeError("websocket failed")),
            ),
            self.assertRaisesRegex(RuntimeError, "websocket failed"),
        ):
            analyzer._run_process(io.BytesIO(), True)
        self.assertTrue(process.terminate_called)
        self.assertGreaterEqual(process.wait_calls, 1)
        self.assertIsNone(analyzer.process)
        job.assign.assert_called_once_with(process)

    def test_terminate_timeout_forces_kill_and_wait(self):
        analyzer, _ = self.make_analyzer()
        process = self.FakeProcess(timeout_on_terminate=True)
        analyzer._reap_process(process, None)
        self.assertTrue(process.terminate_called)
        self.assertTrue(process.kill_called)
        self.assertEqual(process.wait_calls, 2)

    def test_second_ffmpeg_cannot_start_concurrently(self):
        analyzer, _ = self.make_analyzer()
        analyzer.run_lock.acquire()
        try:
            with (
                mock.patch.object(recorder.subprocess, "Popen") as popen,
                self.assertRaisesRegex(RuntimeError, "既に起動中"),
            ):
                analyzer._run_process(io.BytesIO(), True)
            popen.assert_not_called()
        finally:
            analyzer.run_lock.release()

    def test_cleanup_error_still_releases_single_instance_lock(self):
        analyzer, _ = self.make_analyzer()
        process = self.FakeProcess()
        with (
            mock.patch.object(recorder.subprocess, "Popen", return_value=process),
            mock.patch.object(
                analyzer,
                "_feed_websocket",
                mock.AsyncMock(side_effect=RuntimeError("websocket failed")),
            ),
            mock.patch.object(analyzer, "_reap_process", side_effect=RuntimeError("cleanup failed")),
            self.assertRaisesRegex(RuntimeError, "cleanup failed"),
        ):
            analyzer._run_process(io.BytesIO(), True)
        self.assertIsNone(analyzer.process)
        self.assertTrue(analyzer.run_lock.acquire(blocking=False))
        analyzer.run_lock.release()

    def test_retry_uses_exponential_backoff(self):
        analyzer, _ = self.make_analyzer(
            retry_seconds=5,
            analysis_retry_max_seconds=300,
            analysis_failure_limit=8,
        )
        delays = []

        def fake_wait(delay):
            delays.append(delay)
            if len(delays) == 3:
                analyzer.stop_event.set()
                return True
            return False

        analyzer.stop_event.wait = fake_wait
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(recorder, "LOG_DIR", Path(directory)),
            mock.patch.object(analyzer, "_run_process", side_effect=RuntimeError("bad stream")),
        ):
            analyzer._loop()
        self.assertEqual(delays, [5, 10, 20])

    def test_failure_limit_opens_circuit_breaker(self):
        analyzer, _ = self.make_analyzer(
            retry_seconds=1,
            analysis_retry_max_seconds=4,
            analysis_failure_limit=3,
            analysis_circuit_breaker_seconds=20,
        )
        observations = []

        def fake_wait(delay):
            observations.append((delay, analyzer.circuit_open))
            if len(observations) == 3:
                analyzer.stop_event.set()
                return True
            return False

        analyzer.stop_event.wait = fake_wait
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(recorder, "LOG_DIR", Path(directory)),
            mock.patch.object(analyzer, "_run_process", side_effect=RuntimeError("bad stream")),
        ):
            analyzer._loop()
        self.assertEqual(observations, [(1, False), (2, False), (20, True)])
        self.assertIn("再試行を停止", analyzer.last_error)

    def test_shutdown_closes_kill_on_close_job(self):
        analyzer, job = self.make_analyzer()
        process = self.FakeProcess()
        analyzer.process = process
        analyzer.shutdown()
        self.assertTrue(process.terminate_called)
        self.assertEqual(analyzer.next_retry_seconds, 0)
        job.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

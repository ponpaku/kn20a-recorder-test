"""Reproducible Issue #1 investigation; never contacts a physical camera.

Requires Linux FFmpeg/FFprobe and an explicitly supplied MediaMTX binary.
Creates temporary loopback-only servers and reaps every child on exit.
The current working tree is imported. Results are JSON on stdout.
"""
import argparse
from datetime import timedelta
import json
import logging
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recorder


def run(args, timeout=30):
    value = subprocess.run(args, capture_output=True, timeout=timeout)
    return {"code": value.returncode, "stderr": value.stderr.decode(errors="replace")[-1000:]}


def probe(path):
    value = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                            "-show_data_hash", "sha256", "-of", "json", str(path)],
                           capture_output=True, timeout=15)
    return json.loads(value.stdout) if value.returncode == 0 else {}


def stop(process):
    if process is not None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def generate(path, video="libx264", audio="aac", size="128x96"):
    args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=10"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    args += ["-t", "2", "-c:v", video, "-threads", "1", "-g", "10", "-bf", "0"]
    if video == "libx264":
        args += ["-preset", "ultrafast"]
    if video == "libx265":
        args += ["-x265-params", "pools=1:frame-threads=1:log-level=error"]
    if audio:
        args += ["-c:a", audio]
    result = run(args + ["-y", str(path)])
    if result["code"]:
        raise RuntimeError(result)


class NoControl:
    calls = 0

    def recall_preset(self, number):
        self.calls += 1
        raise RuntimeError("control unavailable")


def control_dependency(root):
    store = recorder.ScheduleStore(root / "dependency.json")
    start = recorder.now_jst() - timedelta(seconds=1)
    item = store.create("dependency", start.isoformat(), (start + timedelta(seconds=30)).isoformat(), 2)
    camera = NoControl()
    manager = recorder.RecorderManager({**recorder.DEFAULT_CONFIG, "preset_settle_seconds": 0}, store, camera)
    with mock.patch.object(manager, "_start_process") as spawn:
        manager._tick()
        blocked = not spawn.called
        store.update(item["id"], preset_number=None)
        manager._tick()
        without_preset = spawn.called
    return {"preset_failure_blocks_recording": blocked,
            "no_preset_starts_without_control": without_preset, "camera_calls": camera.calls}


def expired_recording_recovery(root):
    """R02: simulate a persisted recording whose reservation has expired."""
    run_dir = root / "recovery" / "recordings" / "test"
    run_dir.mkdir(parents=True)
    source = run_dir / "part_01.mkv"
    shutil.copy(root / "h264_aac.mkv", source)
    now = recorder.now_jst()
    path = root / "recovery" / "schedules.json"
    recorder.atomic_write_json(path, [{"id": "012345abcdef", "title": "recovery",
        "status": "recording", "start": (now - timedelta(minutes=2)).isoformat(),
        "end": (now - timedelta(minutes=1)).isoformat(), "output_dir": str(run_dir),
        "preset_number": None}])
    store = recorder.ScheduleStore(path)
    after_load = store.get("012345abcdef")["status"]
    manager = recorder.RecorderManager({**recorder.DEFAULT_CONFIG,
        "output_dir_resolved": str(run_dir.parent)}, store, NoControl())
    with mock.patch.object(recorder, "LOG_DIR", root / "recovery"), \
            mock.patch.object(manager.thread, "start"), \
            mock.patch.object(manager.finalizer_thread, "start"), \
            mock.patch.object(manager, "_queue_finalization") as queue:
        manager.start()
        manager._active_schedule(now)
        queued = queue.called
    return {"status_after_load": after_load, "status_after_scheduler": store.get("012345abcdef")["status"],
            "finalization_queued": queued, "mkv_still_present": source.exists()}


def media_cases(root):
    report = {}
    for name, video, audio in [("h264_aac", "libx264", "aac"),
                               ("h264_no_audio", "libx264", None),
                               ("h265_aac", "libx265", "aac"),
                               ("h264_g711", "libx264", "pcm_alaw"),
                               ("h264_pcm", "libx264", "pcm_s16le")]:
        source = root / (name + ".mkv")
        generate(source, video, audio)
        target = root / (name + ".mp4")
        result = run(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
                      "-c", "copy", "-movflags", "+faststart", "-y", str(target)])
        report[name] = {**result, "source_retained": source.exists(),
                        "output_codecs": [s["codec_name"] for s in probe(target).get("streams", [])]}
    changed = root / "changed.mkv"
    generate(changed, size="160x120")
    listing = root / "changed.ffconcat"
    listing.write_text("ffconcat version 1.0\nfile 'h264_aac.mkv'\nfile 'changed.mkv'\n", encoding="utf-8")
    target = root / "changed.mp4"
    result = run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                  "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-y", str(target)])
    report["resolution_change_concat"] = {**result,
        "source_widths": [probe(root / name)["streams"][0]["width"] for name in ("h264_aac.mkv", "changed.mkv")],
        "output_header_width": probe(target).get("streams", [{}])[0].get("width"),
        "decode": run(["ffmpeg", "-v", "error", "-xerror", "-i", str(target), "-f", "null", "-"])}
    audio = root / "audio_only.mp4"
    run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", "1", "-c:a", "aac", str(audio)])
    manager = recorder.RecorderManager({"ffmpeg_path": shutil.which("ffmpeg")}, recorder.ScheduleStore(root / "media-schedules.json"), None)
    report["audio_only_validation"] = {"accepted_by_current_validator": manager._valid_mp4(audio)}
    data = (root / "h264_aac.mp4").read_bytes()
    truncated = root / "truncated.mp4"
    truncated.write_bytes(data[:int(len(data) * 0.7)])
    report["truncated_validation"] = {"accepted_by_current_validator": manager._valid_mp4(truncated),
        "decode": run(["ffmpeg", "-v", "error", "-xerror", "-i", str(truncated), "-f", "null", "-"])}
    retention_root = root / "retention"
    retention_run = retention_root / "run"
    retention_run.mkdir(parents=True)
    retained_source = retention_run / "part_01.mkv"
    shutil.copy(root / "h264_aac.mkv", retained_source)
    shutil.copy(truncated, retention_run / "recording.mp4")
    store = mock.Mock()
    store.list.return_value = []
    store.get.return_value = {"id": "test", "output_dir": str(retention_run)}
    retention_manager = recorder.RecorderManager({"ffmpeg_path": shutil.which("ffmpeg"),
        "output_dir_resolved": str(retention_root), "keep_mkv_after_remux": False}, store, None)
    retention_manager._remux_schedule("test")
    report["existing_truncated_target"] = {"source_deleted_by_current_finalizer": not retained_source.exists()}
    report["existing_truncated_target"]["replacement_output_valid"] = retention_manager._valid_mp4(retention_run / "recording.mp4")
    report["existing_truncated_target"]["unverified_output_quarantined"] = bool(list(retention_run.glob("unverified_*.mp4")))
    mixed = root / "mixed.ffconcat"
    mixed.write_text("ffconcat version 1.0\nfile 'h264_aac.mkv'\nfile 'h265_aac.mkv'\n", encoding="utf-8")
    mixed_target = root / "mixed.mp4"
    report["codec_change_concat"] = run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", str(mixed), "-c", "copy", "-y", str(mixed_target)])
    if mixed_target.exists():
        report["codec_change_concat"]["decode"] = run(["ffmpeg", "-v", "error", "-xerror", "-i",
            str(mixed_target), "-f", "null", "-"])
    return report


def network_case(root, binary, source, mode, disconnect=False):
    root.mkdir()
    listener = port()
    server = publisher = None
    log = (root / "fixture.log").open("wb")
    url = f"rtsp://127.0.0.1:{listener}/test" if mode == "rtsp" else f"rtmp://127.0.0.1:{listener}/live/test"
    config = {**recorder.DEFAULT_CONFIG, "input_mode": mode, "input_url": url, "listen_url": url,
              "ffmpeg_path": shutil.which("ffmpeg"), "output_dir_resolved": str(root / "recordings"),
              "segment_seconds": 2, "retry_seconds": 1, "minimum_free_bytes": 1,
              "preset_settle_seconds": 0, "estimated_bitrate_mbps": 1,
              "rtsp_timeout_microseconds": 1500000, "keep_mkv_after_remux": True}
    store = recorder.ScheduleStore(root / "schedules.json")
    camera = NoControl()
    manager = recorder.RecorderManager(config, store, camera)
    started = False

    def publish():
        args = ["ffmpeg", "-v", "error", "-re", "-stream_loop", "-1", "-i", str(source), "-c", "copy"]
        args += ["-f", "rtsp", "-rtsp_transport", "tcp"] if mode == "rtsp" else ["-f", "flv"]
        return subprocess.Popen(args + [url], stdin=subprocess.DEVNULL, stdout=log, stderr=log)

    try:
        if mode == "rtsp":
            settings = root / "mediamtx.yml"
            settings.write_text(f"rtspAddress: 127.0.0.1:{listener}\nrtspTransports: [tcp]\n"
                                "rtmp: no\nhls: no\nwebrtc: no\nsrt: no\npaths:\n  all_others:\n", encoding="utf-8")
            server = subprocess.Popen([binary, str(settings)], cwd=str(root), stdout=log, stderr=log)
            time.sleep(0.5)
            if server.poll() is not None:
                raise RuntimeError("MediaMTX startup failed")
            publisher = publish()
            time.sleep(1)
        start = recorder.now_jst() + timedelta(seconds=1)
        duration = 15 if disconnect else 8
        item = store.create("fixture", start.isoformat(), (start + timedelta(seconds=duration)).isoformat())
        with mock.patch.object(recorder, "LOG_DIR", root):
            manager.start()
            started = True
            begin = time.monotonic()
            dropped = resumed = False
            while time.monotonic() - begin < duration + 20:
                elapsed = time.monotonic() - begin
                if mode == "rtmp-listen" and publisher is None and elapsed > 2:
                    publisher = publish()
                if disconnect and not dropped and elapsed > 5:
                    stop(publisher)
                    dropped = True
                if disconnect and dropped and not resumed and elapsed > 8:
                    publisher = publish()
                    resumed = True
                final = store.get(item["id"])
                if final["status"] in {"completed", "failed", "postprocess_failed", "missed"}:
                    break
                time.sleep(0.2)
            final = store.get(item["id"])
            manager.shutdown()
            started = False
        folder = Path(final.get("output_dir", root))
        output = folder / "recording.mp4"
        return {"status": final["status"], "attempts": final.get("attempts", 0),
                "camera_calls": camera.calls, "mkv_count": len(list(folder.glob("*.mkv"))),
                "output_duration": probe(output).get("format", {}).get("duration"),
                "last_error": final.get("last_error", ""),
                "decode": run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"]) if output.exists() else None}
    finally:
        if started:
            manager.shutdown()
        stop(publisher)
        stop(server)
        log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mediamtx", required=True)
    parser.add_argument("--media-only", action="store_true", help="skip the network fixtures")
    parser.add_argument("--assert-network", action="store_true", help="fail if any network acceptance scenario fails")
    args = parser.parse_args()
    args.mediamtx = str(Path(args.mediamtx).resolve())
    logging.basicConfig(level=logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="kn20a-issue1-") as directory:
        root = Path(directory)
        report = {"ffmpeg": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
                  "mediamtx": subprocess.check_output([args.mediamtx, "--version"], text=True).strip(),
                  "control": control_dependency(root), "media": media_cases(root)}
        report["expired_recording_recovery"] = expired_recording_recovery(root)
        for mode, disconnect in [("rtsp", False), ("rtsp", True), ("rtmp-listen", False), ("rtmp-listen", True)]:
            if args.media_only:
                break
            name = mode + ("_reconnect" if disconnect else "")
            report[name] = network_case(root / name, args.mediamtx, root / "h264_aac.mkv", mode, disconnect)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.assert_network:
            for name in ("rtsp", "rtsp_reconnect", "rtmp-listen", "rtmp-listen_reconnect"):
                result = report[name]
                if name.endswith("reconnect") and result["status"] == "postprocess_failed" and result["mkv_count"] > 0 and result["camera_calls"] == 0:
                    continue  # Abrupt loss may damage a segment; safe retention is an accepted outcome.
                if result["status"] != "completed" or result["camera_calls"] or not result.get("decode") or result["decode"]["code"] or result["decode"]["stderr"]:
                    raise SystemExit(f"Network acceptance failed: {name}")


if __name__ == "__main__":
    main()

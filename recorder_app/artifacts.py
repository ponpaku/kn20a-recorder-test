"""Transactional finalization. No source is deleted without a durable receipt."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def frame_shapes(handle):
    """Read frame JSON incrementally: bounded memory even for long recordings."""
    decoder = json.JSONDecoder()
    buffer = ""
    started = False
    shapes = set()
    while True:
        chunk = handle.read(65536)
        if not chunk:
            raise RuntimeError("Incomplete frame metadata")
        buffer += chunk.decode("utf-8", errors="replace")
        if not started:
            index = buffer.find("[")
            if index < 0:
                continue
            buffer = buffer[index + 1:]
            started = True
        while True:
            buffer = buffer.lstrip(" \r\n\t,")
            if buffer.startswith("]"):
                return shapes
            try:
                frame, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                break
            if all(key in frame for key in ("width", "height", "pix_fmt")):
                shapes.add((str(frame["width"]), str(frame["height"]), frame["pix_fmt"]))
            buffer = buffer[end:]


def persist(path, value):
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def check_segment_sequence(names):
    """Validate only the new attempt/segment naming scheme; legacy stays supported."""
    attempts = {}
    for name in names:
        match = re.fullmatch(r"part_(\d{8})_(\d{9})\.mkv", name)
        if match:
            attempt, segment = (int(value) for value in match.groups())
            attempts.setdefault(attempt, []).append(segment)
    for attempt, segments in attempts.items():
        indices = sorted(segments)
        missing = 0 if indices[0] != 0 else next(
            (left + 1 for left, right in zip(indices, indices[1:]) if right != left + 1),
            None,
        )
        if missing is not None:
            raise RuntimeError(
                f"Missing segment part_{attempt:08d}_{missing:09d}.mkv; "
                "originals must be retained"
            )


class Finalizer:
    def __init__(self, config):
        self.config = config
        self.ffmpeg = str(config["ffmpeg_path"])
        name = "ffprobe.exe" if Path(self.ffmpeg).suffix == ".exe" else "ffprobe"
        sibling = Path(self.ffmpeg).with_name(name)
        self.ffprobe = str(sibling) if sibling.is_file() else shutil.which(name) or name

    def inspect(self, path):
        result = subprocess.run([self.ffprobe, "-v", "error", "-count_packets", "-show_streams",
            "-show_format", "-show_data_hash", "sha256", "-of", "json", str(path)],
            capture_output=True, timeout=float(self.config.get("validation_timeout_seconds", 21600)))
        if result.returncode or result.stderr.strip():
            raise RuntimeError(f"Media inspection failed: {path.name}")
        data = json.loads(result.stdout)
        selected = [s for s in data.get("streams", []) if s["codec_type"] in ("video", "audio")]
        if not selected or selected[0]["codec_type"] != "video" or sum(s["codec_type"] == "video" for s in selected) != 1:
            raise RuntimeError(f"Expected exactly one video stream: {path.name}")
        if any(int(s.get("nb_read_packets", 0)) <= 0 for s in selected):
            raise RuntimeError(f"Missing media packets: {path.name}")
        # Header metadata alone misses a resolution/pixel-format switch inside a segment.
        with tempfile.TemporaryFile() as frames:
            scan = subprocess.run([self.ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_frames", "-show_entries", "frame=width,height,pix_fmt", "-of", "json", str(path)],
                stdout=frames, stderr=subprocess.PIPE,
                timeout=float(self.config.get("validation_timeout_seconds", 21600)))
            if scan.returncode or scan.stderr.strip():
                raise RuntimeError(f"Source frame scan failed: {path.name}")
            frames.seek(0)
            shapes = frame_shapes(frames)
            expected_shape = (str(selected[0].get("width")), str(selected[0].get("height")), selected[0].get("pix_fmt"))
            if shapes != {expected_shape}:
                raise RuntimeError(f"In-segment format change or missing frames: {path.name}; expected={expected_shape!r}; decoded={shapes!r}")
        fields = ("codec_type", "codec_name", "profile", "level", "width", "height", "pix_fmt",
                  "sample_aspect_ratio", "time_base", "sample_rate", "channels", "channel_layout", "extradata_hash")
        return {"fingerprint": [{k: s.get(k) for k in fields} for s in selected],
                "packets": [int(s["nb_read_packets"]) for s in selected],
                "duration": float(data.get("format", {}).get("duration", 0))}

    def validate(self, output, sources):
        info = self.inspect(output)
        expected = [sum(source["media"]["packets"][i] for source in sources)
                    for i in range(len(sources[0]["media"]["packets"]))]
        if info["packets"] != expected:
            raise RuntimeError("Output packet coverage does not match its sources")
        for actual, source in zip(info["fingerprint"], sources[0]["media"]["fingerprint"]):
            for field in ("codec_type", "codec_name", "width", "height", "sample_rate", "channels"):
                if actual.get(field) != source.get(field):
                    raise RuntimeError("Output stream structure changed")
        duration = sum(source["media"]["duration"] for source in sources)
        if info["duration"] <= 0 or abs(info["duration"] - duration) > max(0.3, len(sources) * 0.15):
            raise RuntimeError("Output duration does not cover its sources")
        # Scan every frame, not just the header; FFmpeg can log errors with exit 0.
        result = subprocess.run([self.ffmpeg, "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
            "-i", str(output), "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=float(self.config.get("validation_timeout_seconds", 21600)))
        if result.returncode or result.stderr.strip():
            detail = result.stderr.decode(errors="replace")[-600:]
            raise RuntimeError("Full output decode failed; sources retained: " + detail)

    def finalize(self, run_dir):
        root = Path(self.config["output_dir_resolved"]).resolve()
        run_dir = Path(run_dir).resolve()
        if root not in run_dir.parents:
            raise RuntimeError("Recording directory is outside output root")
        receipt = run_dir / "manifest.json"
        if receipt.is_symlink():
            raise RuntimeError("Untrusted manifest path")
        previous = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else None
        def sequence(path):
            match = re.fullmatch(r"part_(\d+)(.*)", path.name)
            if not match:
                raise RuntimeError("Unrecognized segment filename")
            return int(match.group(1)), match.group(2)
        files = sorted(list(run_dir.glob("part_*.mkv")) + list(run_dir.glob("part_*.mp4")), key=sequence)
        # Never interpret arbitrary external symlinks as recording artifacts.
        if any(path.is_symlink() or path.resolve().parent != run_dir for path in files):
            raise RuntimeError("Untrusted recording artifact path")
        if previous and previous.get("phase") == "verified":
            names = [s["name"] for s in previous["sources"]]
            check_segment_sequence(names)
            outputs = [g["output"] for g in previous["groups"]]
            covered = [name for g in previous["groups"] for name in g["sources"]]
            if (not names or not outputs or len(names) != len(set(names)) or sorted(covered) != sorted(names)
                    or any(Path(name).name != name or "/" in name or "\\" in name for name in names + outputs)
                    or any((run_dir / name).is_symlink() for name in names + outputs)):
                raise RuntimeError("Invalid verification receipt")
            known = {s["name"]: s for s in previous["sources"]}
            if any(path.name not in known or digest(path) != known[path.name]["sha256"] for path in files):
                raise RuntimeError("Sources changed since verification; no cleanup performed")
            for source in previous["sources"]:
                if not (run_dir / source["name"]).exists() and not source.get("deletion_authorized"):
                    raise RuntimeError("Unexplained missing source")
            for group in previous["groups"]:
                output = run_dir / group["output"]
                if not output.is_file() or digest(output) != group["sha256"]:
                    raise RuntimeError("Verified output changed; no cleanup performed")
                self.validate(output, [known[name] for name in group["sources"]])
            self.cleanup(run_dir, receipt, previous)
            return [str(run_dir / g["output"]) for g in previous["groups"]], "verified"
        if not files:
            raise RuntimeError("No original MKV/MP4 segments found")
        if any(run_dir.glob("part_*.mkv")) and any(run_dir.glob("part_*.mp4")):
            raise RuntimeError("Mixed legacy and MKV sources require manual reconciliation")
        check_segment_sequence([path.name for path in files])
        if previous and previous.get("phase") == "preparing":
            recorded = {s["name"]: s["sha256"] for s in previous["sources"]}
            if len(recorded) != len(previous["sources"]) or {p.name: digest(p) for p in files} != recorded:
                raise RuntimeError("Sources changed since preparation; no reuse or cleanup performed")
        sources = [{"name": path.name, "sha256": digest(path), "size": path.stat().st_size,
                    "media": self.inspect(path)} for path in files]
        groups = []
        for source in sources:
            if not groups or groups[-1][0]["media"]["fingerprint"] != source["media"]["fingerprint"]:
                groups.append([])
            groups[-1].append(source)
        manifest = {"version": 1, "phase": "preparing", "input_id": self.config.get("input_id"), "sources": sources, "groups": []}
        targets = ["recording.mp4" if len(groups) == 1 else f"recording_{i + 1:03d}.mp4" for i in range(len(groups))]
        reusable = {}
        if previous and previous.get("phase") == "preparing":
            planned = {name: group for name, group in zip(targets, groups)}
            for completed in previous.get("groups", []):
                name = completed["output"]
                if (name not in planned or name in reusable or
                        completed["sources"] != [s["name"] for s in planned[name]]):
                    raise RuntimeError("Invalid partial verification receipt")
                output = run_dir / name
                if output.is_symlink():
                    raise RuntimeError("Untrusted recording output path")
                if output.is_file() and digest(output) == completed["sha256"]:
                    try:
                        self.validate(output, planned[name])
                    except RuntimeError:
                        continue
                    reusable[name] = completed
            # Keep every validated group durable even if a different group fails.
            manifest["groups"] = list(reusable.values())
        persist(receipt, manifest)
        supported = all(s["media"]["fingerprint"][0]["codec_name"] in ("h264", "hevc", "mpeg4")
                        and all(a["codec_name"] == "aac" for a in s["media"]["fingerprint"][1:]) for s in sources)
        if not supported:
            manifest["phase"] = "kept_mkv"
            persist(receipt, manifest)
            return [], "kept_mkv"
        needed = sum(s["size"] for name, group in zip(targets, groups) if name not in reusable
                     for s in group) * 1.15 + int(self.config.get("minimum_free_bytes", 1))
        if shutil.disk_usage(run_dir).free < needed:
            raise RuntimeError("Insufficient finalization space; sources retained")
        for index, group in enumerate(groups):
            target = run_dir / targets[index]
            if target.name in reusable:
                continue
            if target.is_symlink():
                raise RuntimeError("Untrusted recording output path")
            if target.exists():
                # A receipt-less old output is evidence, never an authorization to delete.
                os.replace(target, run_dir / f"unverified_{uuid.uuid4().hex}.mp4")
            listing = run_dir / "recording.ffconcat"
            listing.write_text("ffconcat version 1.0\n" + "\n".join(
                "file '" + s["name"].replace("'", "'\\''") + "'" for s in group) + "\n", encoding="utf-8")
            temporary = run_dir / (target.name + ".partial")
            result = subprocess.run([self.ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                "-i", str(listing), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart",
                "-f", "mp4", str(temporary)], capture_output=True,
                timeout=float(self.config.get("validation_timeout_seconds", 21600)))
            if result.returncode or result.stderr.strip():
                raise RuntimeError("MP4 remux failed; all sources retained")
            self.validate(temporary, group)
            if any(digest(run_dir / s["name"]) != s["sha256"] for s in sources):
                raise RuntimeError("Source changed during finalization")
            with temporary.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            manifest["groups"].append({"output": target.name, "sha256": digest(target),
                                       "sources": [s["name"] for s in group]})
            manifest["groups"].sort(key=lambda g: targets.index(g["output"]))
            persist(receipt, manifest)
        if any(digest(run_dir / s["name"]) != s["sha256"] for s in sources):
            raise RuntimeError("Source changed during finalization")
        manifest["phase"] = "verified"
        persist(receipt, manifest)
        self.cleanup(run_dir, receipt, manifest)
        return [str(run_dir / g["output"]) for g in manifest["groups"]], "verified"

    def cleanup(self, run_dir, receipt, manifest):
        if self.config.get("keep_mkv_after_remux", True):
            return
        for source in manifest["sources"]:
            path = run_dir / source["name"]
            if path.exists():
                if path.is_symlink() or path.resolve().parent != run_dir or digest(path) != source["sha256"]:
                    raise RuntimeError("Source changed before cleanup")
                # Commit authorization BEFORE unlink: restart can explain a missing file.
                source["deletion_authorized"] = True
                persist(receipt, manifest)
                path.unlink()
            source["deleted"] = True
            persist(receipt, manifest)

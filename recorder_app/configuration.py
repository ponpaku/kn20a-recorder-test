"""Non-destructive v1/v2 configuration normalization."""
import copy
import hashlib


def normalize(config):
    result = copy.deepcopy(config)
    version = int(result.get("schema_version", 1))
    if version not in (1, 2):
        raise ValueError("Unsupported schema_version")
    if version == 2:
        source = result.get("input", {})
        if source.get("type") not in ("rtsp", "rtmp-listen"):
            raise ValueError("input.type must be rtsp or rtmp-listen")
        result["input_mode"] = source["type"]
        result["listen_url" if source["type"] == "rtmp-listen" else "input_url"] = source["url"]
        if source.get("transport", "tcp") != "tcp":
            raise ValueError("Only RTSP TCP is implemented")
        result["rtsp_timeout_microseconds"] = int(float(source.get("read_timeout_seconds", 5)) * 1_000_000)
        result["publisher_wait_seconds"] = int(source.get("connect_timeout_seconds", 30))
        recording = result.get("recording", {})
        for key in ("output_dir", "segment_seconds", "preset_failure_policy", "preset_deadline_seconds"):
            if key in recording:
                result[key] = recording[key]
        result["keep_mkv_after_remux"] = recording.get("retain_sources", True)
        result.setdefault("preset_failure_policy", "continue")
        controls = result.get("controls", {})
        result["controls"] = controls
        for role in ("presets", "exposure"):
            settings = controls.get(role, {"driver": "none"})
            if settings.get("driver", "none") not in ("none", "kn20a"):
                raise ValueError(f"controls.{role}.driver must be none or kn20a")
    else:
        result["controls"] = {
            role: {"driver": "kn20a", "host": result["camera_ip"], "port": result.get("visca_port", 5678)}
            for role in ("presets", "exposure")
        }
        result["preset_failure_policy"] = "legacy"
    if result.get("preset_failure_policy") not in ("continue", "require", "legacy"):
        raise ValueError("Invalid preset_failure_policy")
    result.setdefault("preset_deadline_seconds", 10)
    source_url = result.get("listen_url") if result.get("input_mode") == "rtmp-listen" else result.get("input_url")
    result["input_id"] = hashlib.sha256((str(result.get("input_mode")) + str(source_url)).encode()).hexdigest()
    if not 0 < float(result["preset_deadline_seconds"]) <= 60:
        raise ValueError("preset_deadline_seconds must be in (0, 60]")
    return result


def to_v2(config):
    result = copy.deepcopy(config)
    result["schema_version"] = 2
    result["input"] = {"type": config["input_mode"], "url": config["listen_url"] if config["input_mode"] == "rtmp-listen" else config["input_url"],
                       "transport": "tcp", "read_timeout_seconds": config["rtsp_timeout_microseconds"] / 1_000_000,
                       "connect_timeout_seconds": config["publisher_wait_seconds"]}
    result["recording"] = {"output_dir": config["output_dir"], "segment_seconds": config["segment_seconds"],
        "retain_sources": config.get("keep_mkv_after_remux", True),
        "preset_failure_policy": config["preset_failure_policy"], "preset_deadline_seconds": config["preset_deadline_seconds"]}
    for key in ("output_dir_resolved", "input_id", "input_mode", "input_url", "listen_url", "output_dir", "segment_seconds",
                "preset_failure_policy", "preset_deadline_seconds", "keep_mkv_after_remux"):
        result.pop(key, None)
    return result

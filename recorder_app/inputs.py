"""Input arguments: no camera control dependencies."""
from dataclasses import dataclass
import json
import subprocess
import threading


@dataclass(frozen=True)
class RTSPInput:
    url: str
    timeout_us: int

    def arguments(self):
        return ["-rtsp_transport", "tcp", "-timeout", str(self.timeout_us), "-i", self.url]


@dataclass(frozen=True)
class RTMPListenInput:
    url: str
    wait_seconds: int

    def arguments(self):
        return ["-rtmp_listen", "1", "-timeout", str(self.wait_seconds), "-i", self.url]


def input_adapter(config):
    if config["input_mode"] == "rtsp":
        return RTSPInput(config["input_url"], int(config["rtsp_timeout_microseconds"]))
    if config["input_mode"] == "rtmp-listen":
        return RTMPListenInput(config["listen_url"], int(config["publisher_wait_seconds"]))
    raise ValueError("input_mode must be 'rtsp' or 'rtmp-listen'")


class InputInspector:
    """Optional bounded probe; capture takes priority and cancels it."""
    def __init__(self, config, ffprobe, manager):
        self.config, self.ffprobe, self.manager = config, str(ffprobe), manager
        self.process = None
        self.generation = 0
        self.result = {"state": "not_checked"}

    def start(self):
        with self.manager.lock:
            if self.manager.process is not None or self.manager.preparation.owner:
                raise RuntimeError("録画中・接続中は入力検査を実行できません")
            if self.process is not None:
                return self.result
            if self.config.get("input_mode") == "rtmp-listen":
                self.result = {"state":"deferred", "message":"RTMPは録画受信後のファイルで検査します"}
                return self.result
            if self.config.get("input", {}).get("probe") == "off":
                self.result = {"state":"disabled"}
                return self.result
            self.generation += 1
            generation = self.generation
            self.result = {"state":"checking"}
            process = subprocess.Popen([self.ffprobe, "-v", "error", *input_adapter(self.config).arguments(),
                "-show_streams", "-of", "json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.process = process
            threading.Thread(target=self._read,args=(process,generation),daemon=True).start()
            return self.result

    def _read(self, process, generation):
        try:
            output, _ = process.communicate(timeout=10)
            data = json.loads(output) if process.returncode == 0 else {}
            streams = [{k:s.get(k) for k in ("codec_type","codec_name","width","height","sample_rate","channels")}
                       for s in data.get("streams",[])]
            result = {"state":"ok" if any(s.get("codec_type")=="video" for s in streams) else "unavailable", "streams":streams}
        except Exception:
            if process.poll() is None:
                process.kill()
            process.wait()
            result = {"state":"unavailable"}
        with self.manager.lock:
            if self.generation == generation:
                self.result, self.process = result, None

    def cancel(self):
        with self.manager.lock:
            process = self.process
            self.generation += 1
            self.process = None
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
                self.result = {"state":"cancelled", "message":"録画を優先して検査を中断しました"}

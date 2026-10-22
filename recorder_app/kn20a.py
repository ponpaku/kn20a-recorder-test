"""KN20A HTTP/VISCA adapter; not constructed for Generic inputs."""
from __future__ import annotations
import json
from contextlib import nullcontext
import socket
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timezone, timedelta

APP_DIR = Path(__file__).resolve().parents[1]
MODE_LABELS = {0: "Auto（完全自動）", 3: "Manual（手動）", 10: "SAE（シャッター優先）", 11: "AAE（絞り優先）", 13: "Bright（明るさ優先）"}

def now_jst():
    return datetime.now(timezone(timedelta(hours=9)))

class ExposureClient:
    def __init__(self, camera_ip: str, visca_port: int):
        self.camera_ip = camera_ip
        self.visca_port = int(visca_port)
        self.lock = threading.RLock()
        # Reach private LAN cameras directly, independent of outbound proxy settings.
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

    def _visca(self, command: bytes, completion_timeout: float = 2.0, authorize=None) -> list[bytes]:
        frames: list[bytes] = []
        with socket.create_connection((self.camera_ip, self.visca_port), timeout=3) as sock:
            sock.settimeout(1.5)
            with authorize() if authorize else nullcontext():
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

    def recall_preset(self, number: int, authorize=None) -> None:
        number = self._validate_preset(number)
        with self.lock:
            self._visca(bytes([0x81, 0x01, 0x04, 0x3F, 0x02, number, 0xFF]), completion_timeout=15, authorize=authorize)

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

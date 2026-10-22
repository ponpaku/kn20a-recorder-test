"""Camera capabilities and optional KN20A adapter factory."""
from typing import Protocol


class PresetControl(Protocol):
    def recall_preset(self, number: int) -> None: ...
    def save_preset(self, number: int) -> None: ...


class UnsupportedCapability(ValueError):
    def __init__(self, capability):
        self.capability = capability
        super().__init__(f"Unsupported capability: {capability}")


class DisabledGuard:
    def start(self): pass
    def shutdown(self): pass
    def stop_for_external_change(self): pass
    def snapshot(self):
        return {"available": False, "enabled": False, "phase": "unsupported",
                "phase_label": "非対応", "unavailable_reason": "この制御方式では夕景AEは非対応です"}


def make_controls(config, kn20a_factory):
    cache = {}
    def build(role):
        setting = config["controls"].get(role, {})
        if setting.get("driver", "none") == "none":
            return None
        host = setting.get("host", config["camera_ip"])
        port = int(setting.get("port", config["visca_port"]))
        key = (host, port)
        if key not in cache:
            cache[key] = kn20a_factory(host, port)
        return cache[key]
    presets, exposure = build("presets"), build("exposure")
    capabilities = {"presets": {"recall": presets is not None, "save": presets is not None,
                                "list": presets is not None},
                    "exposure": {"read": exposure is not None, "write": exposure is not None},
                    "analysis_ae": {"supported": exposure is not None}}
    return presets, exposure, capabilities

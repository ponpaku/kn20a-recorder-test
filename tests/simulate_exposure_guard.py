"""Deterministic sunset simulation for the image-based exposure controller."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
SPEC = importlib.util.spec_from_file_location("kn20a_recorder_sim", APP_DIR / "recorder.py")
recorder = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = recorder
SPEC.loader.exec_module(recorder)

SHUTTERS = {
    18: 60, 19: 90, 20: 100, 21: 125, 22: 180, 23: 250,
    24: 350, 25: 500, 26: 725, 27: 1000, 28: 1500,
    29: 2000, 30: 3000,
}


def exposure(shutter: int, iris: int, gain: int) -> dict:
    return {
        "iris": {"value": iris, "label": recorder.IRIS_LABELS[iris], "actual": True},
        "gain": gain,
        "gain_limit": 6,
        "shutter": {
            "value": shutter,
            "label": f"1/{SHUTTERS[shutter]}",
            "options": [
                {"value": value, "label": f"1/{denominator}"}
                for value, denominator in SHUTTERS.items()
            ],
        }
    }


def camera_image(scene_stops: float, shutter: int, iris: int, gain: int) -> dict:
    """Approximate decoded Y relative to the measured good 1/1500 daylight frame."""
    f_number = recorder.iris_f_number(iris)
    brightness = (
        (2 ** scene_stops)
        * 1500 / SHUTTERS[shutter]
        * (5.6 / f_number) ** 2
        * (10 ** (gain * 3 / 20))
    )
    y_avg = min(250.0, 96.0 * brightness)
    y_p90 = min(255.0, 163.0 * brightness)
    white_fraction = max(0.0, min(1.0, (y_p90 - 230.0) / 25.0 * 0.08))
    return {
        "y_avg": y_avg,
        "y_p90": y_p90,
        "white_fraction": white_fraction,
        "valid": True,
        "frame_difference": 0.5,
    }


def run() -> None:
    shutter = 28
    iris = 5
    gain = 0
    print("SUNSET: Manual, diffraction limit F5.6, natural-night, quality Gain <= 3")
    print("SceneEV  Iris  Shutter  Gain  Target  YAVG   P90  Decision")
    sunset = [value / 2 for value in range(2, -27, -1)]
    sunrise = [value / 2 for value in range(-25, 3)]
    for scene_stops in sunset + sunrise:
        decision = "hold"
        for _ in range(80):
            stats = camera_image(scene_stops, shutter, iris, gain)
            effective_avg, effective_high, target_scale = recorder.effective_image_targets(
                stats, exposure(shutter, iris, gain), 96, 163, 0.10,
                "natural", 60, 3000,
            )
            decision, value = recorder.choose_image_exposure_action(
                stats, exposure(shutter, iris, gain), effective_avg, effective_high,
                0.10, 0.10, 60, 3000, 5, 3, 3.0,
            )
            if decision in {"faster", "slower"} and value is not None:
                shutter = value
                continue
            if decision in {"iris_open", "iris_close"} and value is not None:
                iris = value
                continue
            if decision in {"gain_up", "gain_down"} and value is not None:
                gain = value
                continue
            break
        assert iris >= 5, "diffraction limit was exceeded"
        assert gain <= 3, "quality gain limit was exceeded"
        stats = camera_image(scene_stops, shutter, iris, gain)
        _, _, target_scale = recorder.effective_image_targets(
            stats, exposure(shutter, iris, gain), 96, 163, 0.10,
            "natural", 60, 3000,
        )
        print(
            f"{scene_stops:>+7.1f}  {recorder.IRIS_LABELS[iris]:<5}  "
            f"1/{SHUTTERS[shutter]:<5}  {gain:>4}  {target_scale:>5.2f}  "
            f"{stats['y_avg']:>5.1f}  {stats['y_p90']:>4.0f}  {decision}"
        )


if __name__ == "__main__":
    run()

from __future__ import annotations


THREAD_WEIGHT_PRESETS = {
    "40wt": {
        "label": "40 wt polyester/rayon",
        "spacing_mm": 0.20,
        "min_spacing_mm": 0.16,
        "diameter_mm": 0.20,
    },
    "30wt": {
        "label": "30 wt thicker thread",
        "spacing_mm": 0.35,
        "min_spacing_mm": 0.30,
        "diameter_mm": 0.28,
    },
    "60wt": {
        "label": "60 wt fine thread",
        "spacing_mm": 0.15,
        "min_spacing_mm": 0.12,
        "diameter_mm": 0.13,
    },
}

DEFAULT_THREAD_WEIGHT = "40wt"


def normalize_thread_weight(value: str | None) -> str:
    key = (value or DEFAULT_THREAD_WEIGHT).strip().lower()
    return key if key in THREAD_WEIGHT_PRESETS else DEFAULT_THREAD_WEIGHT


def thread_weight_label(value: str | None) -> str:
    key = normalize_thread_weight(value)
    return THREAD_WEIGHT_PRESETS[key]["label"]


def recommended_fill_spacing(value: str | None) -> float:
    key = normalize_thread_weight(value)
    return float(THREAD_WEIGHT_PRESETS[key]["spacing_mm"])


def minimum_fill_spacing(value: str | None) -> float:
    key = normalize_thread_weight(value)
    return float(THREAD_WEIGHT_PRESETS[key]["min_spacing_mm"])


def thread_diameter_mm(value: str | None) -> float:
    key = normalize_thread_weight(value)
    return float(THREAD_WEIGHT_PRESETS[key]["diameter_mm"])

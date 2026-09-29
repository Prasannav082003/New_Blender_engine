"""
Guardrails between the model and your renderer.

A structured-output schema guarantees the SHAPE of the response. It
guarantees nothing about whether the numbers are sane. A model that
places a 4000-lumen spot outside the room bounds, or sets a 12000K
colour temp, will produce a render you throw away - and a 4K path-traced
render is expensive enough that you do not want to find out afterwards.

Everything the LLM proposes passes through here first. Values are
clamped, out-of-bounds lights are dropped, and every correction is
recorded so you can see what the model got wrong.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from .schema import COLOR_MOODS, LIGHT_KINDS, LIGHTING_PROFILES, LIMITS

log = logging.getLogger(__name__)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class ValidationReport:
    def __init__(self) -> None:
        self.corrections: List[str] = []
        self.dropped: List[str] = []

    def correct(self, msg: str) -> None:
        self.corrections.append(msg)
        log.info("[validate] corrected: %s", msg)

    def drop(self, msg: str) -> None:
        self.dropped.append(msg)
        log.warning("[validate] dropped: %s", msg)

    @property
    def clean(self) -> bool:
        return not self.corrections and not self.dropped

    def as_dict(self) -> Dict[str, Any]:
        return {"corrections": self.corrections, "dropped": self.dropped}


def validate_decision(
    decision: Dict[str, Any],
    room: Dict[str, Any],
) -> Tuple[Dict[str, Any], ValidationReport]:
    """
    `room` is the "room" block from the Godot lighting report:
        {"min_x","max_x","min_y","max_y","ceiling_height","floor_area_m2", ...}
    all in PLAN units (the same units the LLM was given).
    """
    rep = ValidationReport()
    out: Dict[str, Any] = {}

    # ---------------- lightingSettings ----------------
    ls = dict(decision.get("lightingSettings") or {})

    profile = str(ls.get("lighting_profile", "day")).lower()
    if profile not in LIGHTING_PROFILES:
        rep.correct(f"lighting_profile '{profile}' -> 'day'")
        profile = "day"

    mood = str(ls.get("colorMood", "day")).lower()
    if mood not in COLOR_MOODS:
        rep.correct(f"colorMood '{mood}' -> 'day'")
        mood = "day"

    lo, hi = LIMITS["illuminationLevel"]
    try:
        level = int(ls.get("illuminationLevel", 5))
    except (TypeError, ValueError):
        level = 5
        rep.correct("illuminationLevel unparseable -> 5")
    if not lo <= level <= hi:
        rep.correct(f"illuminationLevel {level} -> {int(_clamp(level, lo, hi))}")
        level = int(_clamp(level, lo, hi))

    # A night render with a bright daylight mood is almost always a model
    # error rather than an artistic choice.
    if profile == "night" and mood == "day":
        rep.correct("night profile with 'day' mood -> 'warm'")
        mood = "warm"

    out["lightingSettings"] = {
        "lighting_profile": profile,
        "colorMood": mood,
        "illuminationLevel": level,
    }

    # ---------------- addLights ----------------
    #
    # UNITS: the model works entirely in METRES. Everything it is told -
    # floor area, ceiling height, "inset 1.0-1.5m from walls" - is metric,
    # so demanding plan-unit coordinates guarantees it answers in metres
    # and every light lands 100x outside the room. Convert here, once, at
    # the boundary. Storage stays in plan units because that is what
    # _build_custom_lights() consumes.
    min_x = float(room.get("min_x", -1e9))
    max_x = float(room.get("max_x", 1e9))
    min_y = float(room.get("min_y", -1e9))
    max_y = float(room.get("max_y", 1e9))
    ceiling = float(room.get("ceiling_height", 0.0)) or None

    unit_scale = float(room.get("unit_scale", 1.0) or 1.0)
    if unit_scale <= 0:
        unit_scale = 1.0

    def m_to_plan(metres: float) -> float:
        return metres / unit_scale

    accepted: Dict[str, Dict[str, Any]] = {}
    proposed = decision.get("addLights") or []

    if len(proposed) > LIMITS["max_added_lights"]:
        rep.correct(
            f"{len(proposed)} lights proposed, keeping first {LIMITS['max_added_lights']}"
        )
        proposed = proposed[: LIMITS["max_added_lights"]]

    for raw in proposed:
        if not isinstance(raw, dict):
            continue

        lid = str(raw.get("id", "")).strip()
        if not lid:
            rep.drop("light with no id")
            continue
        # Namespace every LLM light so they are trivially identifiable and
        # removable in the scene tree and in the JSON.
        if not lid.startswith("llm_"):
            lid = "llm_" + lid

        try:
            x_m = float(raw["x"])
            y_m = float(raw["y"])
            alt_m = float(raw["altitude"])
        except (KeyError, TypeError, ValueError):
            rep.drop(f"{lid}: non-numeric position")
            continue

        x = m_to_plan(x_m)
        y = m_to_plan(y_m)
        alt = m_to_plan(alt_m)

        # Reject lights placed outside the room footprint outright. Nudging
        # them to the boundary produces worse results than not having them.
        if not (min_x <= x <= max_x and min_y <= y <= max_y):
            rep.drop(
                f"{lid}: position ({x_m:.2f}m, {y_m:.2f}m) is outside the room "
                f"({min_x * unit_scale:.2f}-{max_x * unit_scale:.2f}m x "
                f"{min_y * unit_scale:.2f}-{max_y * unit_scale:.2f}m)"
            )
            continue

        if ceiling:
            a_lo, a_hi = LIMITS["altitude_frac"]
            lo_alt, hi_alt = ceiling * a_lo, ceiling * a_hi
            if not lo_alt <= alt <= hi_alt:
                new_alt = _clamp(alt, lo_alt, hi_alt)
                rep.correct(
                    f"{lid}: altitude {alt_m:.2f}m -> "
                    f"{new_alt * unit_scale:.2f}m"
                )
                alt = new_alt

        kind = str(raw.get("lightKind", "point")).lower()
        if kind not in LIGHT_KINDS:
            rep.correct(f"{lid}: lightKind '{kind}' -> 'point'")
            kind = "point"

        light: Dict[str, Any] = {
            "lightKind": kind,
            "x": x,
            "y": y,
            "altitude": alt,
            "isOn": bool(raw.get("isOn", True)),
            "castShadow": bool(raw.get("castShadow", False)),
            "_llm_purpose": str(raw.get("purpose", "fill")),
            "_llm_rationale": str(raw.get("rationale", ""))[:300],
        }

        for key in ("colorTemp", "intensityMultiplier", "coneAngle", "tiltAngle"):
            if key not in raw or raw[key] is None:
                continue
            try:
                val = float(raw[key])
            except (TypeError, ValueError):
                continue
            lo_k, hi_k = LIMITS[key]
            clamped = _clamp(val, lo_k, hi_k)
            if abs(clamped - val) > 1e-6:
                rep.correct(f"{lid}: {key} {val:g} -> {clamped:g}")
            light[key] = clamped

        if kind == "spot" and "coneAngle" not in light:
            light["coneAngle"] = 45.0
        if "intensityMultiplier" not in light:
            light["intensityMultiplier"] = 1.0
        if "rotationY" in raw:
            try:
                light["rotationY"] = float(raw["rotationY"])
            except (TypeError, ValueError):
                pass

        accepted[lid] = light

    out["customLights"] = accepted
    out["verdict"] = str(decision.get("verdict", "acceptable"))
    out["issues"] = [str(i) for i in (decision.get("issues") or [])][:12]
    out["reasoning"] = str(decision.get("reasoning", ""))[:1200]
    out["sunThroughWindows"] = bool(decision.get("sunThroughWindows", False))

    # Sun elevation: clamp to sane range if provided
    sun_elev = decision.get("sunElevation")
    if sun_elev is not None:
        try:
            sun_elev = float(sun_elev)
            sun_elev = _clamp(sun_elev, 5.0, 60.0)
            out["sunElevation"] = sun_elev
        except (TypeError, ValueError):
            rep.correct("sunElevation unparseable, using engine default")

    return out, rep


def validate_exposure(decision: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp the post-render exposure correction."""
    try:
        delta = int(decision.get("illuminationLevelDelta", 0))
    except (TypeError, ValueError):
        delta = 0
    delta = int(_clamp(delta, -2, 2))

    try:
        scale = float(decision.get("tonemapExposureScale", 1.0))
    except (TypeError, ValueError):
        scale = 1.0
    scale = _clamp(scale, 0.6, 1.6)

    return {
        "verdict": str(decision.get("verdict", "acceptable")),
        "problem": str(decision.get("problem", "none")),
        "illuminationLevelDelta": delta,
        "tonemapExposureScale": scale,
        "shouldRerender": bool(decision.get("shouldRerender", False)),
        "reasoning": str(decision.get("reasoning", ""))[:800],
    }

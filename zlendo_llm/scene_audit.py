"""
Derive the lighting audit from the scene JSON, without launching Godot.

WHY THIS EXISTS
---------------
`RenderSession.analyze_scene()` was built to get its numbers by booting the
scene headlessly and having `lighting_audit.gd` measure the result. That hook
is gone from this tree - there is no `godot_project/lighting_audit.gd`, and
`image_glb_creation.gd` never calls `LightingAudit.emit(...)`. With the hook
missing the session raised, and the whole AI lighting pass was skipped on every
render.

Everything the agent actually reads, though, is present in the scene JSON that
gets handed to Godot in the first place: room polygons, ceiling heights, wall
lines, window openings, authored lights and furniture. So this module computes
the same report shape directly, in milliseconds, with no second Godot boot and
no GPU time.

MEASURED vs DERIVED
-------------------
These numbers describe the plan as authored, not the scene as built. For the
decisions the lighting agent makes - is this room under-lit for its area, is
there enough glazing for a day render, are there already fixtures here - the
two are equivalent, because the builder derives the geometry from exactly this
data. Where they could diverge is if the builder silently drops or merges
something; a restored `lighting_audit.gd` would catch that and this will not.
The report is tagged `"source": "derived"` so a consumer can tell.

STAYING IN STEP WITH THE BUILDER
--------------------------------
Every resolver below mirrors its counterpart in `image_glb_creation.gd`
(`_resolve_scene_unit_scale`, `_resolve_lighting_profile`, `_resolve_color_mood`,
`_resolve_illumination_scale`, `_is_light_fixture_item`). If one of those
changes, change it here too - a report that disagrees with the builder is worse
than no report, because it is confidently wrong.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Mirrors _is_light_fixture_item() in image_glb_creation.gd. Keep in sync.
_LIGHT_KEYWORDS = (
    "light", "lamp", "pendant", "pendent", "chandelier",
    "lantern", "sconce", "ceilinglamp", "ceiling_light",
    "wallmount", "streetlamp", "lighting fixture",
)

# Hole types that admit daylight. "sliding" covers sliding glass doors, which
# are glazing for lighting purposes even though the editor calls them doors.
_GLAZED_HOLE_TYPES = ("window", "sliding")


# ----------------------------------------------------------------------
# Plan parsing
# ----------------------------------------------------------------------
def _parse_floor_plan(scene: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """floor_plan_data, parsed if it arrived as a JSON string."""
    fp = scene.get("floor_plan_data")
    if isinstance(fp, str):
        try:
            fp = json.loads(fp)
        except (json.JSONDecodeError, TypeError):
            log.warning("floor_plan_data is not parseable JSON")
            return None
    return fp if isinstance(fp, dict) else None


def _active_layer(floor_plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The selected layer, or the first one with geometry on it."""
    layers = floor_plan.get("layers")
    if not isinstance(layers, dict) or not layers:
        return None

    selected = floor_plan.get("selectedLayer")
    if selected and isinstance(layers.get(selected), dict):
        return layers[selected]

    for layer in layers.values():
        if isinstance(layer, dict) and layer.get("areas"):
            return layer
    first = next(iter(layers.values()), None)
    return first if isinstance(first, dict) else None


def _unit_scale(scene: Dict[str, Any], floor_plan: Optional[Dict[str, Any]]) -> float:
    """Metres per plan unit. Mirrors _resolve_scene_unit_scale()."""
    version = ""
    for src in (floor_plan, scene):
        if isinstance(src, dict) and src.get("version") is not None:
            version = str(src.get("version", "")).strip()
            break
    # 2.0.0 stores measurements in millimetres; older plans in centimetres.
    return 0.001 if version == "2.0.0" else 0.01


def _length(container: Any, key: str) -> Optional[float]:
    """Read the editor's {"key": {"length": N}} shape, tolerating a bare N."""
    if not isinstance(container, dict):
        return None
    val = container.get(key)
    if isinstance(val, dict):
        val = val.get("length")
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _hole_length(hole: Dict[str, Any], key: str) -> Optional[float]:
    """Dimension of an opening, preferring `properties` over the top level.

    The two genuinely disagree in real plans - one window in the test scene
    carries properties.altitude=1310 alongside a top-level altitude=900, and
    another has width 914.4 vs 1800. `properties` is authoritative; that is the
    same precedence the wall builder settled on (the floating-window defect came
    from reading the top-level copy). The top level is only a fallback for holes
    that omit the key entirely.
    """
    val = _length(hole.get("properties"), key)
    if val is not None:
        return val
    return _length(hole, key)


def _unsign_zero(v: float) -> float:
    """-0.0 prints as "-0.0" and reads as a direction. It is not one."""
    return 0.0 if v == 0 else v


# ----------------------------------------------------------------------
# Profile - mirrors the three GDScript resolvers
# ----------------------------------------------------------------------
def _lighting_profile(scene: Dict[str, Any]) -> str:
    string_keys = ("lighting_profile", "time_of_day", "timeOfDay",
                   "render_time", "environment_preset")
    sources: List[Dict[str, Any]] = [scene]
    ls = scene.get("_lightingSettings")
    if isinstance(ls, dict):
        sources.append(ls)

    for source in sources:
        for key in string_keys:
            if key in source:
                value = str(source[key]).lower().strip()
                if any(w in value for w in ("sunset", "dusk", "evening")):
                    return "sunset"
                if "night" in value:
                    return "night"
                if any(w in value for w in ("day", "morning", "afternoon")):
                    return "day"

    if scene.get("sunset_render"):
        return "sunset"
    if scene.get("night_render"):
        return "night"
    if "day_render" in scene:
        return "day" if scene["day_render"] else "night"
    return "day"


def _color_mood(scene: Dict[str, Any]) -> str:
    candidate = ""
    if "colorMood" in scene:
        candidate = str(scene["colorMood"])
    elif "color_mood" in scene:
        candidate = str(scene["color_mood"])
    else:
        ls = scene.get("_lightingSettings")
        if isinstance(ls, dict) and "colorMood" in ls:
            candidate = str(ls["colorMood"])
    return candidate.lower().strip() or "day"


def _illumination_level(scene: Dict[str, Any]) -> int:
    level = 5
    for key in ("illuminationLevel", "illumination_level"):
        if key in scene:
            level = scene[key]
            break
    else:
        ls = scene.get("_lightingSettings")
        if isinstance(ls, dict):
            for key in ("illuminationLevel", "illumination_level"):
                if key in ls:
                    level = ls[key]
                    break
    try:
        return max(1, min(5, int(level)))
    except (TypeError, ValueError):
        return 5


# ----------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------
def _polygon_area(points: List[Tuple[float, float]]) -> float:
    """Shoelace area, always positive - winding order is not guaranteed."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _area_polygon(area: Dict[str, Any],
                  vertices: Dict[str, Any]) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    for vid in area.get("vertices", []) or []:
        v = vertices.get(str(vid))
        if not isinstance(v, dict):
            continue
        try:
            pts.append((float(v.get("x", 0.0)), float(v.get("y", 0.0))))
        except (TypeError, ValueError):
            continue
    return pts


def _room_metrics(layer: Dict[str, Any], us: float) -> Dict[str, Any]:
    """Floor area, ceiling height and plan bounds across every room."""
    vertices = layer.get("vertices") or {}
    areas = layer.get("areas") or {}
    if not isinstance(vertices, dict) or not isinstance(areas, dict):
        return {}

    total_plan_area = 0.0
    heights: List[float] = []
    xs: List[float] = []
    ys: List[float] = []
    rooms: List[Dict[str, Any]] = []

    for area_id, area in areas.items():
        if not isinstance(area, dict) or area.get("visible") is False:
            continue
        pts = _area_polygon(area, vertices)
        if len(pts) < 3:
            continue

        plan_area = _polygon_area(pts)
        total_plan_area += plan_area
        xs.extend(p[0] for p in pts)
        ys.extend(p[1] for p in pts)

        props = area.get("properties") or {}
        h = _length(props, "height")
        if h and h > 0:
            heights.append(h * us)

        rooms.append({
            "id": area_id,
            "label": (props.get("label") if isinstance(props, dict) else None) or "",
            "floor_area_m2": round(plan_area * us * us, 2),
        })

    if not rooms:
        return {}

    # Ceiling height is per-room, but the agent asks for one number. The tallest
    # governs how far light has to travel, which is the lighting-relevant one.
    ceiling = round(max(heights), 3) if heights else 0.0

    return {
        "floor_area_m2": round(total_plan_area * us * us, 2),
        "ceiling_height": ceiling,
        # Same value under the name integration.py's record block reads.
        "ceiling_height_m": ceiling,
        "unit_scale": us,
        # Bounds stay in PLAN units: lighting_agent multiplies them by
        # unit_scale itself when it prints "ROOM BOUNDS (metres)".
        "min_x": round(min(xs), 2),
        "max_x": round(max(xs), 2),
        "min_y": round(min(ys), 2),
        "max_y": round(max(ys), 2),
        "room_count": len(rooms),
        "rooms": rooms,
    }


def _window_metrics(layer: Dict[str, Any], us: float) -> Dict[str, Any]:
    """Glazing count, area and per-opening detail including wall facing."""
    holes = layer.get("holes") or {}
    lines = layer.get("lines") or {}
    vertices = layer.get("vertices") or {}
    if not isinstance(holes, dict):
        return {"count": 0, "total_area_plan2": 0.0, "window_details": []}

    total_plan2 = 0.0
    details: List[Dict[str, Any]] = []

    for hole_id, hole in holes.items():
        if not isinstance(hole, dict):
            continue
        if str(hole.get("type", "")).lower() not in _GLAZED_HOLE_TYPES:
            continue

        w = _hole_length(hole, "width") or 0.0
        h = _hole_length(hole, "height") or 0.0
        if w <= 0 or h <= 0:
            continue
        total_plan2 += w * h

        detail: Dict[str, Any] = {
            "id": hole_id,
            "type": str(hole.get("type", "")).lower(),
            "name": hole.get("name") or "",
            # Metres - the agent prints these raw, and "2.4x2.4" reads as a
            # window where "2438.4x2360.0" reads as noise.
            "width": round(w * us, 2),
            "height": round(h * us, 2),
            "sill_height": round((_hole_length(hole, "altitude") or 0.0) * us, 2),
        }

        # Wall midpoint and outward normal, so the model can reason about
        # which side of the room daylight arrives from.
        wall_id = hole.get("wallId") or hole.get("line")
        wall = lines.get(str(wall_id)) if isinstance(lines, dict) else None
        if isinstance(wall, dict):
            wv = wall.get("vertices") or []
            if len(wv) >= 2:
                a = vertices.get(str(wv[0])) if isinstance(vertices, dict) else None
                b = vertices.get(str(wv[1])) if isinstance(vertices, dict) else None
                if isinstance(a, dict) and isinstance(b, dict):
                    try:
                        ax, ay = float(a.get("x", 0)), float(a.get("y", 0))
                        bx, by = float(b.get("x", 0)), float(b.get("y", 0))
                        detail["wall_mid_x"] = round((ax + bx) / 2 * us, 2)
                        detail["wall_mid_y"] = round((ay + by) / 2 * us, 2)
                        dx, dy = bx - ax, by - ay
                        length = math.hypot(dx, dy)
                        if length > 1e-9:
                            # Normal to the wall run; sign is arbitrary without
                            # knowing which side is outside, so this gives the
                            # ORIENTATION of the opening, not a compass bearing.
                            detail["facing_nx"] = _unsign_zero(round(-dy / length, 3))
                            detail["facing_ny"] = _unsign_zero(round(dx / length, 3))
                    except (TypeError, ValueError):
                        pass

        details.append(detail)

    return {
        "count": len(details),
        "total_area_plan2": round(total_plan2, 2),
        "window_details": details,
    }


def _is_light_fixture(item: Dict[str, Any]) -> bool:
    """Mirrors _is_light_fixture_item() in image_glb_creation.gd."""
    urls = item.get("asset_urls") or {}
    model_hint = ""
    if isinstance(urls, dict):
        model_hint = str(urls.get("GLB_File_URL") or urls.get("glb_Url") or "")
    full = " ".join((
        str(item.get("type", "")),
        str(item.get("name", "")),
        str(item.get("mounting_type", "")),
        model_hint,
    )).lower()
    return any(kw in full for kw in _LIGHT_KEYWORDS)


def _light_metrics(layer: Dict[str, Any]) -> Dict[str, Any]:
    """Authored lights and light-fixture props, split the way the agent asks."""
    scene_lights = layer.get("sceneLights") or {}
    items = layer.get("items") or {}

    custom = 0
    kinds: Dict[str, int] = {}
    if isinstance(scene_lights, dict):
        for light in scene_lights.values():
            if not isinstance(light, dict):
                continue
            if light.get("isOn") is False or light.get("visible") is False:
                continue
            custom += 1
            kind = str(light.get("lightKind", "unknown")).lower()
            kinds[kind] = kinds.get(kind, 0) + 1

    fixture_items = 0
    ceiling_fixtures = 0
    if isinstance(items, dict):
        for item in items.values():
            if not isinstance(item, dict) or item.get("visible") is False:
                continue
            if not _is_light_fixture(item):
                continue
            fixture_items += 1
            mounting = str(item.get("mounting_type", "")).lower()
            name = str(item.get("name", "")).lower()
            if ("ceiling" in mounting
                    or any(w in name for w in ("ceiling", "pendant", "chandelier"))):
                ceiling_fixtures += 1

    return {
        "ceiling_fixture_count": ceiling_fixtures,
        "custom_light_count": custom,
        "fixture_item_count": fixture_items,
        "custom_light_kinds": kinds,
    }


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def derive_audit(scene: Dict[str, Any]) -> Dict[str, Any]:
    """Build the lighting-audit report from the scene JSON alone.

    Returns the same shape `lighting_audit.gd` was expected to emit, before
    RenderSession._enrich() adds total_area_m2 and glazing_to_floor_pct.
    Never raises: a partial report is more useful to the agent than none, and
    the caller is on the render path.
    """
    report: Dict[str, Any] = {
        "source": "derived",
        "source_note": (
            "Computed from the scene JSON, not measured from the built scene "
            "(godot_project/lighting_audit.gd is not present in this install)."
        ),
        "room": {},
        "windows": {"count": 0, "total_area_plan2": 0.0, "window_details": []},
        "lights": {
            "ceiling_fixture_count": 0,
            "custom_light_count": 0,
            "fixture_item_count": 0,
        },
        "profile": {
            "lighting_profile": _lighting_profile(scene),
            "color_mood": _color_mood(scene),
            "illumination_level": _illumination_level(scene),
        },
    }

    try:
        floor_plan = _parse_floor_plan(scene)
        us = _unit_scale(scene, floor_plan)
        report["room"]["unit_scale"] = us

        if floor_plan is None:
            report["source_note"] += " floor_plan_data was missing or unparseable."
            return report

        layer = _active_layer(floor_plan)
        if layer is None:
            report["source_note"] += " No usable layer found in floor_plan_data."
            return report

        room = _room_metrics(layer, us)
        if room:
            report["room"] = room
        report["windows"] = _window_metrics(layer, us)
        report["lights"] = _light_metrics(layer)
    except Exception as exc:  # noqa: BLE001
        log.exception("Deriving the lighting audit failed")
        report["source_note"] += f" Derivation failed part-way: {exc}"

    return report

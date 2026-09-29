"""
The tool registry - ONE definition, two consumers.

Both the MCP server (Claude Desktop, any MCP client) and the internal
Qwen agent loop are built from this list.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List

from .schema import COLOR_MOODS, LIGHT_KINDS, LIGHTING_PROFILES
from .session import RenderSession

log = logging.getLogger(__name__)


class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[[RenderSession, Dict[str, Any]], Any],
        expensive: bool = False,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler
        self.expensive = expensive

    def as_ollama(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def __call__(self, session: RenderSession, args: Dict[str, Any]) -> Any:
        return self.handler(session, args)


def _obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def _us(room: Dict[str, Any]) -> float:
    """Metres per plan unit; 1.0 if the audit could not determine it."""
    v = float(room.get("unit_scale", 1.0) or 1.0)
    return v if v > 0 else 1.0


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------
def _h_analyze(s: RenderSession, a: Dict[str, Any]) -> Any:
    report = s.analyze_scene(force=bool(a.get("force", False)))
    room = report.get("room", {})
    lights = report.get("lights", {})
    win = report.get("windows", {})
    prof = report.get("profile", {})

    area = room.get("floor_area_m2", 0.0)
    n_lights = lights.get("ceiling_fixture_count", 0)

    out = {
        "floor_area_m2": round(area, 1),
        "ceiling_height_m": round(room.get("ceiling_height_m", 0.0), 2),
        "room_bounds_m": {
            "min_x": round(room.get("min_x", 0.0) * _us(room), 2),
            "max_x": round(room.get("max_x", 0.0) * _us(room), 2),
            "min_y": round(room.get("min_y", 0.0) * _us(room), 2),
            "max_y": round(room.get("max_y", 0.0) * _us(room), 2),
        },
        "ceiling_fixture_altitude_m": round(
            room.get("ceiling_height", 0.0) * _us(room) * 0.9, 2),
        "window_count": win.get("count", 0),
        "window_details": win.get("window_details", []),
        "door_count": win.get("door_count", 0),
        "existing_light_count": n_lights,
        "light_fixture_props": lights.get("fixture_item_count", 0),
        "requested_profile": prof.get("lighting_profile"),
        "requested_mood": prof.get("color_mood"),
        "requested_illumination": prof.get("illumination_level"),
        "lights_per_100m2": round(n_lights / area * 100.0, 1) if area > 0 else None,
        "renders_remaining": s.renders_remaining,
    }

    # These are runtime findings the model has to act on, not editorial advice.
    # They stay concrete: a warning that does not say what the consequence is
    # gets ignored by a small model.
    warnings = []
    p = str(prof.get("lighting_profile", "")).lower()
    if p in ("night", "sunset") and n_lights == 0:
        warnings.append(
            "CRITICAL: low-daylight render with zero interior lights - "
            "this will render almost black."
        )
    if p == "day" and win.get("count", 0) == 0:
        warnings.append(
            "Day render with no windows - daylight cannot enter."
        )
    if area > 0 and n_lights > 0 and (n_lights / area * 100.0) > 40:
        warnings.append("Light density is very high for the floor area.")
    n_win = win.get("count", 0)
    if p in ("day", "sunset") and n_win > 0:
        warnings.append(
            f"{n_win} window(s) detected."
        )
    out["warnings"] = warnings
    return out


def _h_set_lighting(s: RenderSession, a: Dict[str, Any]) -> Any:
    return s.set_lighting(
        lighting_profile=a.get("lighting_profile"),
        color_mood=a.get("color_mood"),
        illumination_level=a.get("illumination_level"),
    )


def _h_add_light(s: RenderSession, a: Dict[str, Any]) -> Any:
    return s.add_light({
        "id": a.get("id", "light"),
        "lightKind": a.get("light_kind", "point"),
        "purpose": a.get("purpose", "fill"),
        "x": a.get("x"),
        "y": a.get("y"),
        "altitude": a.get("altitude"),
        "colorTemp": a.get("color_temp", 3000),
        "intensityMultiplier": a.get("intensity", 1.0),
        "coneAngle": a.get("cone_angle"),
        "tiltAngle": a.get("tilt_angle"),
        "castShadow": a.get("cast_shadow", False),
        "isOn": True,
        "rationale": a.get("rationale", ""),
    })


def _h_remove_light(s: RenderSession, a: Dict[str, Any]) -> Any:
    return s.remove_light(str(a.get("id", "")))


def _h_list_lights(s: RenderSession, a: Dict[str, Any]) -> Any:
    return s.list_lights()


def _h_reset_lights(s: RenderSession, a: Dict[str, Any]) -> Any:
    return s.reset_lights()


def _h_render(s: RenderSession, a: Dict[str, Any]) -> Any:
    result = s.render()
    if not result.get("rendered"):
        return result
    m = result["metrics"]
    return {
        "rendered": True,
        "render_index": result["index"],
        "seconds": result["seconds"],
        "renders_remaining": result["renders_remaining"],
        "quality_score": s.score(m),
        "score_note": "lower is better; below ~0.35 is good",
        "mean_luminance": m["mean_luminance"],
        "p50_luminance": m["p50_luminance"],
        "clipped_highlight_pct": m["clipped_highlight_pct"],
        "crushed_shadow_pct": m["crushed_shadow_pct"],
        "dynamic_range_stops": m["dynamic_range_stops"],
        "log_contrast": m["log_contrast"],
        "colour_cast_rgb": m["colour_cast_rgb"],
        "problems": m["heuristic_flags"],
    }


def _h_history(s: RenderSession, a: Dict[str, Any]) -> Any:
    return {
        "renders": [
            {
                "index": r.index,
                "score": s.score(r.metrics),
                "settings": r.settings,
                "problems": r.metrics.get("heuristic_flags"),
            }
            for r in s.renders
        ],
        "best": s.best_render_summary(),
        "renders_remaining": s.renders_remaining,
    }


def _h_finish(s: RenderSession, a: Dict[str, Any]) -> Any:
    return {
        "done": True,
        "summary": a.get("summary", ""),
        "best": s.best_render_summary(),
    }


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------
TOOLS: List[Tool] = [
    Tool(
        "analyze_scene",
        "Build the 3D scene and measure it: room size, ceiling height, window "
        "count, existing lights, and the requested lighting profile. Call this "
        "FIRST, before changing anything.",
        _obj({"force": {
            "type": "boolean",
            "description": "Re-analyse even if a cached analysis exists.",
        }}, []),
        _h_analyze,
    ),
    Tool(
        "set_lighting",
        "Set the three global lighting controls: lighting_profile (time of "
        "day), color_mood (colour temperature family) and illumination_level "
        "(1-5 multiplier on all light energy). Values are clamped to valid "
        "ranges automatically. Cheap - does not render.",
        _obj({
            "lighting_profile": {"type": "string", "enum": LIGHTING_PROFILES,
                                 "description": "Time of day."},
            "color_mood": {"type": "string", "enum": COLOR_MOODS},
            "illumination_level": {"type": "integer",
                                   "description": "1 (moody) to 5 (bright)."},
        }, []),
        _h_set_lighting,
    ),
    Tool(
        "add_light",
        "Place one light in the room. ALL COORDINATES ARE IN METRES and must "
        "fall inside room_bounds_m from analyze_scene.",
        _obj({
            "id": {"type": "string", "description": "Short unique name."},
            "light_kind": {"type": "string", "enum": LIGHT_KINDS},
            "purpose": {"type": "string",
                        "enum": ["key", "fill", "ambient", "accent", "wall_wash", "practical"]},
            "x": {"type": "number",
                  "description": "X in METRES, within room_bounds_m."},
            "y": {"type": "number",
                  "description": "Y in METRES, within room_bounds_m."},
            "altitude": {"type": "number",
                         "description": "Height above floor in METRES."},
            "color_temp": {"type": "number", "description": "Kelvin 1800-7500."},
            "intensity": {"type": "number", "description": "0.05-4.0, 1.0 = engine default."},
            "cone_angle": {"type": "number", "description": "Spot only, degrees 10-150."},
            "tilt_angle": {"type": "number", "description": "Downward tilt, degrees."},
            "cast_shadow": {"type": "boolean"},
            "rationale": {"type": "string", "description": "One sentence: why this light."},
        }, ["id", "light_kind", "x", "y", "altitude"]),
        _h_add_light,
    ),
    Tool(
        "remove_light",
        "Remove a light you previously added, by its id. Author-placed lights "
        "cannot be removed. Returns the updated light list.",
        _obj({"id": {"type": "string"}}, ["id"]),
        _h_remove_light,
    ),
    Tool(
        "list_lights",
        "List the lights you have added, the author-placed lights already in "
        "the scene, and the current global lighting settings.",
        _obj({}, []),
        _h_list_lights,
    ),
    Tool(
        "reset_lights",
        "Remove every light you have added and start the lighting plan over. "
        "Author-placed lights are left untouched.",
        _obj({}, []),
        _h_reset_lights,
    ),
    Tool(
        "render",
        "EXPENSIVE. Fire a full 4K path-traced render with the current "
        "settings and measure the result.",
        _obj({}, []),
        _h_render,
        expensive=True,
    ),
    Tool(
        "get_render_history",
        "Review every render so far with its quality score and the settings "
        "that produced it. Use this to check whether a change actually helped.",
        _obj({}, []),
        _h_history,
    ),
    Tool(
        "finish",
        "Declare the work complete. Call this once you have a render you are "
        "satisfied with, or when the render budget is exhausted.",
        _obj({"summary": {
            "type": "string",
            "description": "2-3 sentences: what was wrong and what you changed.",
        }}, ["summary"]),
        _h_finish,
    ),
]

TOOLS_BY_NAME: Dict[str, Tool] = {t.name: t for t in TOOLS}


def ollama_tool_specs() -> List[Dict[str, Any]]:
    return [t.as_ollama() for t in TOOLS]


def dispatch(session: RenderSession, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool by name."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {"error": f"Unknown tool '{name}'. Available: {list(TOOLS_BY_NAME)}"}
    try:
        result = tool(session, args or {})
        return result if isinstance(result, dict) else {"result": result}
    except Exception as exc:  # noqa: BLE001
        log.exception("Tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


def to_text(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)

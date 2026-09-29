"""
Surface + light inventory for material-aware lighting decisions.

The lighting agent currently decides illumination levels, colour
temperature, and custom light placement based ONLY on room geometry
(area, ceiling height, window count). It is blind to what is on the
surfaces and what lights are already in the scene.

A dark blue wall absorbs warm light and makes the room look dimmer than
the illumination level suggests. A high-gloss white floor bounces
overhead spotlights into distracting specular reflections. Four existing
4000K point lights already cover the centre, so adding three more there
creates a blown-out hotspot.

This module extracts everything the lighting agent needs to account for
those effects:

  1. Surface materials  - colour (hex + luminance), roughness, metallic,
                          texture type (from filename keywords), for every
                          wall, floor, and ceiling.
  2. Light inventory    - every scene light by type, position, intensity,
                          colour temperature, IES/fixture info.
  3. Asset context      - furniture names so the model knows whether
                          this is a living room, kitchen, or bedroom.
  4. Derived metrics    - average room reflectivity, colour warmth bias,
                          total light coverage estimate, coverage gaps.

The output is a plain dict that _summarise_report() splices into the
prompt so the LLM has the full picture.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── colour helpers ────────────────────────────────────────────────────

def _hex_to_rgb(hex_str: str) -> Tuple[int, int, int]:
    """'#0f58eb' -> (15, 88, 235). Returns (200,200,200) on bad input."""
    h = hex_str.lstrip("#").strip()
    if len(h) == 3:
        h = h[0]*2 + h[1]*2 + h[2]*2
    if len(h) != 6:
        return (200, 200, 200)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (200, 200, 200)


def _relative_luminance(r: int, g: int, b: int) -> float:
    """
    sRGB relative luminance (0.0 = black, 1.0 = white).
    Used to estimate how much light a surface reflects back into the room.
    """
    def _lin(v: int) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _colour_warmth(r: int, g: int, b: int) -> str:
    """
    Rough warm/neutral/cool classification based on R-B channel bias.
    Not scientifically rigorous, but enough to flag "cool blue walls
    paired with warm lighting" conflicts.
    """
    if r == g == b:
        return "neutral"
    diff = r - b
    if diff > 40:
        return "warm"
    elif diff < -40:
        return "cool"
    return "neutral"


def _colour_name(r: int, g: int, b: int) -> str:
    """Human-readable colour descriptor for the prompt."""
    lum = _relative_luminance(r, g, b)
    if lum > 0.85:
        return "white"
    if lum < 0.05:
        return "near-black"

    # dominant channel
    mx = max(r, g, b)
    mn = min(r, g, b)
    sat = (mx - mn) / mx if mx > 0 else 0

    if sat < 0.15:
        if lum > 0.5:
            return "light grey"
        return "dark grey"

    # rough hue buckets
    if r >= g and r >= b:
        if g > b + 30:
            return "warm yellow" if lum > 0.4 else "olive"
        return "warm red" if lum > 0.15 else "dark red"
    if g >= r and g >= b:
        return "green" if lum > 0.15 else "dark green"
    # blue dominant
    if r > 100 and g < 100:
        return "purple"
    return "cool blue" if lum > 0.15 else "dark blue"


# ── texture type from filename ────────────────────────────────────────

_TEXTURE_MATERIAL_PATTERNS = {
    "marble":    re.compile(r"marble|travertine|onyx", re.I),
    "wood":      re.compile(r"wood|plank|parquet|oak|walnut|birch|pine|teak|bamboo", re.I),
    "tile":      re.compile(r"tile|ceramic|porcelain|terrazzo|mosaic", re.I),
    "brick":     re.compile(r"brick|masonry", re.I),
    "concrete":  re.compile(r"concrete|cement|stucco|plaster", re.I),
    "stone":     re.compile(r"stone|slate|granite|limestone|sandstone", re.I),
    "metal":     re.compile(r"metal|steel|iron|copper|brass|aluminum|chrome", re.I),
    "fabric":    re.compile(r"fabric|textile|linen|cotton|velvet|silk|wool|carpet", re.I),
    "wallpaper": re.compile(r"wallpaper|damask|pattern", re.I),
    "paint":     re.compile(r"paint|matte|satin|eggshell|gloss", re.I),
}


def _guess_texture_type(map_url: str) -> Optional[str]:
    """Infer material type from texture filename keywords."""
    if not map_url:
        return None
    for mat_type, pattern in _TEXTURE_MATERIAL_PATTERNS.items():
        if pattern.search(map_url):
            return mat_type
    return None


# ── floor plan parsing ────────────────────────────────────────────────

def _parse_floor_plan(godot_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Extract the floor plan data. It may be a JSON string or already a dict.
    """
    fp = godot_input.get("floor_plan_data")
    if fp is None:
        return None
    if isinstance(fp, str):
        try:
            return json.loads(fp)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(fp, dict):
        return fp
    return None


def _get_active_layer(floor_plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the selected layer, or the first layer if none selected."""
    layers = floor_plan.get("layers")
    if not isinstance(layers, dict) or not layers:
        return None

    selected = floor_plan.get("selectedLayer")
    if selected and selected in layers:
        return layers[selected]

    # fall back to first layer
    return next(iter(layers.values()))


# ── surface extraction ────────────────────────────────────────────────

def _extract_surface(
    mat: Dict[str, Any],
    label: str,
) -> Dict[str, Any]:
    """
    Build a surface descriptor from a material dict.
    Works for wall inner/outer, floor, and ceiling materials.
    """
    color_hex = str(mat.get("color", "#ffffff"))
    r, g, b = _hex_to_rgb(color_hex)
    luminance = _relative_luminance(r, g, b)

    roughness = 0.8
    try:
        roughness = float(mat.get("roughness", 0.8))
    except (TypeError, ValueError):
        pass

    metallic = 0.0
    try:
        # The JSON uses "metalness" but render_rules.py calls it "metallic"
        metallic = float(mat.get("metalness", mat.get("metallic", 0.0)))
    except (TypeError, ValueError):
        pass

    map_url = str(mat.get("mapUrl", "") or "")
    tex_type = _guess_texture_type(map_url)

    repeat_x = 0
    repeat_y = 0
    try:
        repeat_x = float(mat.get("repeatX", 0))
        repeat_y = float(mat.get("repeatY", 0))
    except (TypeError, ValueError):
        pass

    return {
        "label": label,
        "color": color_hex,
        "color_name": _colour_name(r, g, b),
        "luminance": round(luminance, 3),
        "warmth": _colour_warmth(r, g, b),
        "roughness": round(roughness, 2),
        "metallic": round(metallic, 2),
        "texture_type": tex_type,
        "has_texture": bool(map_url),
        "repeat_x_cm": round(repeat_x / 10, 1) if repeat_x > 0 else 0,
        "repeat_y_cm": round(repeat_y / 10, 1) if repeat_y > 0 else 0,
    }


def _extract_wall_surfaces(layer: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract inner (room-facing) wall surface materials."""
    surfaces = []
    lines = layer.get("lines")
    if not isinstance(lines, dict):
        return surfaces

    for line_id, line in lines.items():
        if not isinstance(line, dict):
            continue
        if line.get("type") != "wall":
            continue

        inner = line.get("inner_properties", {})
        if isinstance(inner, dict):
            mat = inner.get("material", {})
            if isinstance(mat, dict):
                surfaces.append(_extract_surface(mat, f"wall_{line_id[:8]}"))

    return surfaces


def _extract_floor_ceiling(layer: Dict[str, Any]) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]]
]:
    """Extract floor and ceiling materials from areas."""
    floors = []
    ceilings = []
    areas = layer.get("areas")
    if not isinstance(areas, dict):
        return floors, ceilings

    for area_id, area in areas.items():
        if not isinstance(area, dict):
            continue

        floor_props = area.get("floor_properties", {})
        if isinstance(floor_props, dict):
            mat = floor_props.get("material", {})
            if isinstance(mat, dict):
                floors.append(_extract_surface(mat, f"floor_{area_id[:8]}"))

        ceil_props = area.get("ceiling_properties", {})
        if isinstance(ceil_props, dict):
            mat = ceil_props.get("material", {})
            if isinstance(mat, dict):
                ceilings.append(_extract_surface(mat, f"ceiling_{area_id[:8]}"))

    return floors, ceilings


# ── light inventory ───────────────────────────────────────────────────

def _extract_scene_lights(layer: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Catalog every light placed in the scene.
    This is separate from custom lights the LLM adds (llm_* prefix).
    """
    lights = []

    # ── Light FIXTURES placed as assets ───────────────────────────────────
    #
    # These were invisible to this function, and the consequence was not
    # cosmetic. `sceneLights` holds only lights authored directly in the
    # editor's lighting tool, and in a normal room that dict is EMPTY - the
    # actual lights are furniture assets carrying `light_meta`, sitting in
    # layer["items"].
    #
    # So a six-lamp living room was reported to the lighting agent as
    # `existing_lights: 0, total_intensity: null`. Its prompt tells it that
    # "if existing lights already provide adequate coverage (total intensity
    # >= 3.0), reduce the global illuminationLevel to avoid overexposure" -
    # a rule that could never fire. Instead it concluded "existing lights
    # are insufficient" and ADDED TWO MORE rect lights to a room whose six
    # fixtures already summed to 5.78, on top of a sun already being
    # multiplied by 1.2. That is a large part of why the renders came back
    # washed out.
    for item_id, item in (layer.get("items") or {}).items():
        if not isinstance(item, dict):
            continue
        meta = item.get("light_meta")
        if not isinstance(meta, dict):
            continue

        try:
            fixture_intensity = float(meta.get("intensity", 0.0))
        except (TypeError, ValueError):
            fixture_intensity = 0.0
        try:
            fixture_temp = int(float(meta.get("temperature", 4000)))
        except (TypeError, ValueError):
            fixture_temp = 4000

        mounting = str(item.get("mounting_type", "general")).lower()
        entry = {
            "id": str(item_id)[:12],
            "kind": "fixture",
            "name": str(item.get("name", ""))[:40],
            "is_on": fixture_intensity > 0.0,
            "color_temp_k": fixture_temp,
            "intensity": round(fixture_intensity, 2),
            "cast_shadow": False,
            "mounting_type": mounting,
            "from_asset": True,
        }
        for coord, key in (("x", "x"), ("y", "y")):
            try:
                entry[coord] = round(float(item.get(key, 0)) / 1000.0, 2)
            except (TypeError, ValueError):
                entry[coord] = 0.0
        try:
            entry["altitude"] = round(
                float((item.get("properties") or {}).get("altitude", {}).get("length", 0)) / 1000.0, 2
            )
        except (TypeError, ValueError, AttributeError):
            entry["altitude"] = 0.0
        lights.append(entry)

    # ── Lights authored in the editor's lighting tool ─────────────────────
    scene_lights = layer.get("sceneLights")
    if not isinstance(scene_lights, dict):
        return lights

    for light_id, light in scene_lights.items():
        if not isinstance(light, dict):
            continue

        kind = str(light.get("lightKind", "point")).lower()
        # Normalise area_rect -> rect for consistency with schema.py
        if kind == "area_rect":
            kind = "rect"

        is_on = light.get("isOn", True)
        if isinstance(is_on, str):
            is_on = is_on.lower() in ("true", "1", "yes")

        color_temp = 4000
        try:
            color_temp = int(float(light.get("colorTemp", 4000)))
        except (TypeError, ValueError):
            pass

        intensity = 1.0
        try:
            intensity = float(light.get("intensityMultiplier", 1.0))
        except (TypeError, ValueError):
            pass

        entry: Dict[str, Any] = {
            "id": str(light_id)[:12],
            "kind": kind,
            "is_on": bool(is_on),
            "color_temp_k": color_temp,
            "intensity": round(intensity, 2),
            "cast_shadow": bool(light.get("castShadow", False)),
        }

        # Position (in plan units from the editor)
        for coord in ("x", "y", "altitude"):
            try:
                entry[coord] = round(float(light.get(coord, 0)), 1)
            except (TypeError, ValueError):
                entry[coord] = 0.0

        # IES profile
        photo_type = str(light.get("photometricType", "none")).lower()
        if photo_type == "ies":
            entry["ies"] = True
            fixture_id = light.get("fixtureId", "")
            if fixture_id:
                entry["fixture_id"] = str(fixture_id)

        # Spot-specific
        if kind == "spot":
            try:
                entry["cone_angle"] = round(float(light.get("coneAngle", 45)), 1)
            except (TypeError, ValueError):
                pass

        # Rect-specific
        if kind == "rect":
            try:
                entry["width"] = round(float(light.get("width", 1000)), 0)
                entry["height"] = round(float(light.get("height", 500)), 0)
            except (TypeError, ValueError):
                pass

        lights.append(entry)

    return lights


# ── asset context ─────────────────────────────────────────────────────

_ROOM_TYPE_KEYWORDS = {
    "kitchen":   re.compile(r"kitchen|oven|stove|dishwasher|refrigerator|fridge|microwave|cabinet.*kitchen|sink.*kitchen", re.I),
    "bathroom":  re.compile(r"bathroom|toilet|shower|bathtub|basin|vanity|bidet", re.I),
    "bedroom":   re.compile(r"bed\b|nightstand|bedside|wardrobe|dresser|headboard", re.I),
    "dining":    re.compile(r"dining|dinner.*table", re.I),
    "living":    re.compile(r"sofa|couch|armchair|tv\b|television|coffee.*table|living", re.I),
    "office":    re.compile(r"desk|office.*chair|bookshelf|monitor\b|computer", re.I),
}


def _extract_asset_context(layer: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull asset names and infer room type from them.
    We don't need full asset details - just names for room-type inference
    and so the model knows what furniture is present.
    """
    items = layer.get("items")
    if not isinstance(items, dict):
        return {"asset_names": [], "inferred_room_type": None}

    names = []
    for item_id, item in items.items():
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if name:
                names.append(name)

    # Infer room type from asset names
    combined = " ".join(names)
    scores: Dict[str, int] = {}
    for room_type, pattern in _ROOM_TYPE_KEYWORDS.items():
        matches = pattern.findall(combined)
        if matches:
            scores[room_type] = len(matches)

    inferred = None
    if scores:
        inferred = max(scores, key=scores.get)

    return {
        "asset_names": names[:20],  # cap to keep prompt size sane
        "inferred_room_type": inferred,
    }


# ── derived metrics ───────────────────────────────────────────────────

def _compute_metrics(
    walls: List[Dict[str, Any]],
    floors: List[Dict[str, Any]],
    ceilings: List[Dict[str, Any]],
    lights: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compute aggregate metrics that help the LLM make better decisions.
    """
    metrics: Dict[str, Any] = {}

    # ── Average room reflectivity ──
    # Weight: ceiling ~40% of reflected light, walls ~40%, floor ~20%
    # (ceiling matters most because overhead lights bounce off it first)
    all_surfaces = []
    for s in ceilings:
        all_surfaces.append((s["luminance"], 0.4))
    for s in walls:
        all_surfaces.append((s["luminance"], 0.4 / max(len(walls), 1)))
    for s in floors:
        all_surfaces.append((s["luminance"], 0.2))

    if all_surfaces:
        total_weight = sum(w for _, w in all_surfaces)
        avg_reflectivity = sum(l * w for l, w in all_surfaces) / total_weight if total_weight > 0 else 0.5
        metrics["avg_reflectivity"] = round(avg_reflectivity, 3)

        if avg_reflectivity < 0.15:
            metrics["reflectivity_note"] = "very dark surfaces - room absorbs most light, increase illumination level"
        elif avg_reflectivity < 0.3:
            metrics["reflectivity_note"] = "dark surfaces - room absorbs significant light"
        elif avg_reflectivity > 0.75:
            metrics["reflectivity_note"] = "very bright surfaces - light bounces strongly, risk of overexposure with high illumination"
        else:
            metrics["reflectivity_note"] = "moderate reflectivity"

    # ── Surface warmth consensus ──
    warmth_counts = {"warm": 0, "neutral": 0, "cool": 0}
    for s in walls + floors + ceilings:
        w = s.get("warmth", "neutral")
        warmth_counts[w] = warmth_counts.get(w, 0) + 1
    dominant = max(warmth_counts, key=warmth_counts.get)
    metrics["surface_warmth_bias"] = dominant

    # Detect warm/cool conflicts
    if warmth_counts["warm"] > 0 and warmth_counts["cool"] > 0:
        metrics["warmth_conflict"] = True
        metrics["warmth_note"] = "mixed warm and cool surfaces - use neutral colour temperature (3500-4500K) to avoid colour cast"
    else:
        metrics["warmth_conflict"] = False

    # ── Existing light analysis ──
    active_lights = [l for l in lights if l.get("is_on", True)]
    metrics["total_lights"] = len(lights)
    metrics["active_lights"] = len(active_lights)

    if active_lights:
        # Intensity stats
        intensities = [l["intensity"] for l in active_lights]
        metrics["avg_intensity"] = round(sum(intensities) / len(intensities), 2)
        metrics["max_intensity"] = round(max(intensities), 2)
        metrics["total_intensity"] = round(sum(intensities), 2)

        # Colour temp distribution
        temps = [l["color_temp_k"] for l in active_lights]
        metrics["avg_color_temp_k"] = round(sum(temps) / len(temps))
        metrics["color_temp_range"] = f"{min(temps)}-{max(temps)}K"

        # Temp spread warning
        if max(temps) - min(temps) > 2000:
            metrics["color_temp_note"] = "wide colour temperature spread across existing lights - may cause visible colour banding"

        # Type breakdown
        type_counts: Dict[str, int] = {}
        for l in active_lights:
            k = l["kind"]
            type_counts[k] = type_counts.get(k, 0) + 1
        metrics["light_types"] = type_counts

        # IES light count
        ies_count = sum(1 for l in active_lights if l.get("ies"))
        if ies_count > 0:
            metrics["ies_lights"] = ies_count

        # Highlight high-intensity lights that dominate the scene
        dominant_lights = [l for l in active_lights if l["intensity"] >= 2.0]
        if dominant_lights:
            metrics["dominant_lights_note"] = (
                f"{len(dominant_lights)} light(s) at intensity >= 2.0 - "
                f"these dominate the scene, avoid adding competing lights nearby"
            )

    # ── Specular risk ──
    # Low roughness + high metallic = mirror-like surface
    # These create harsh reflections from direct overhead lights
    specular_surfaces = []
    for s in floors + walls:
        if s["roughness"] < 0.4 or s["metallic"] > 0.5:
            specular_surfaces.append(s["label"])
    if specular_surfaces:
        metrics["specular_risk"] = specular_surfaces[:5]
        metrics["specular_note"] = (
            "glossy/metallic surfaces detected - avoid direct overhead spotlights "
            "pointing at these surfaces, prefer indirect/diffuse lighting"
        )

    return metrics


# ── main entry point ──────────────────────────────────────────────────

def analyze_scene_materials(
    godot_input: Dict[str, Any],
    unit_scale: float = 1.0,
) -> Dict[str, Any]:
    """
    Extract a complete material and light inventory from the scene JSON.

    Returns a dict with:
        surfaces     - wall/floor/ceiling materials
        lights       - existing scene light inventory
        assets       - furniture names + inferred room type
        metrics      - derived aggregates (reflectivity, warmth, coverage)

    This dict is designed to be passed to the lighting agent's prompt
    builder, NOT to the validator or the patch applicator.
    """
    result: Dict[str, Any] = {
        "surfaces": {"walls": [], "floors": [], "ceilings": []},
        "lights": [],
        "assets": {"asset_names": [], "inferred_room_type": None},
        "metrics": {},
    }

    floor_plan = _parse_floor_plan(godot_input)
    if floor_plan is None:
        log.warning("No floor_plan_data found in scene JSON")
        return result

    layer = _get_active_layer(floor_plan)
    if layer is None:
        log.warning("No active layer found in floor plan")
        return result

    # ── extract surfaces ──
    walls = _extract_wall_surfaces(layer)
    floors, ceilings = _extract_floor_ceiling(layer)

    result["surfaces"]["walls"] = walls
    result["surfaces"]["floors"] = floors
    result["surfaces"]["ceilings"] = ceilings

    # ── extract lights ──
    lights = _extract_scene_lights(layer)

    # Convert positions from plan units to metres for the prompt
    if unit_scale > 0:
        for light in lights:
            for coord in ("x", "y", "altitude"):
                if coord in light:
                    light[f"{coord}_m"] = round(light[coord] * unit_scale, 2)

    result["lights"] = lights

    # ── extract assets ──
    result["assets"] = _extract_asset_context(layer)

    # ── compute metrics ──
    result["metrics"] = _compute_metrics(walls, floors, ceilings, lights)

    log.info(
        "[material-analyzer] %d walls, %d floors, %d ceilings, "
        "%d lights, %d assets, room_type=%s",
        len(walls), len(floors), len(ceilings),
        len(lights), len(result["assets"]["asset_names"]),
        result["assets"]["inferred_room_type"],
    )

    return result


def format_for_prompt(analysis: Dict[str, Any]) -> str:
    """
    Format the analysis as compact text for the LLM system prompt.

    Keeps it concise - the model has limited context and the full JSON
    analysis is already large. Focus on actionable information.
    """
    lines: List[str] = []

    # ── Room type ──
    room_type = analysis["assets"].get("inferred_room_type")
    if room_type:
        lines.append(f"ROOM TYPE (inferred from furniture): {room_type}")
    assets = analysis["assets"].get("asset_names", [])
    if assets:
        lines.append(f"Furniture: {', '.join(assets[:10])}")

    # ── Surface materials ──
    lines.append("")
    lines.append("SURFACE MATERIALS:")

    for surface in analysis["surfaces"].get("walls", []):
        tex = f", texture={surface['texture_type']}" if surface["texture_type"] else ""
        lines.append(
            f"  {surface['label']}: {surface['color_name']} ({surface['color']}), "
            f"luminance={surface['luminance']}, roughness={surface['roughness']}, "
            f"metallic={surface['metallic']}{tex}"
        )

    for surface in analysis["surfaces"].get("floors", []):
        tex = f", texture={surface['texture_type']}" if surface["texture_type"] else ""
        lines.append(
            f"  {surface['label']}: {surface['color_name']} ({surface['color']}), "
            f"luminance={surface['luminance']}, roughness={surface['roughness']}{tex}"
        )

    for surface in analysis["surfaces"].get("ceilings", []):
        lines.append(
            f"  {surface['label']}: {surface['color_name']} ({surface['color']}), "
            f"luminance={surface['luminance']}, roughness={surface['roughness']}"
        )

    # ── Metrics ──
    metrics = analysis.get("metrics", {})
    if metrics:
        lines.append("")
        lines.append("SURFACE ANALYSIS:")
        if "avg_reflectivity" in metrics:
            lines.append(f"  Average reflectivity: {metrics['avg_reflectivity']:.2f}")
        if "reflectivity_note" in metrics:
            lines.append(f"  -> {metrics['reflectivity_note']}")
        if "surface_warmth_bias" in metrics:
            lines.append(f"  Surface warmth bias: {metrics['surface_warmth_bias']}")
        if "warmth_conflict" in metrics and metrics["warmth_conflict"]:
            lines.append(f"  WARNING: {metrics['warmth_note']}")
        if "specular_note" in metrics:
            lines.append(f"  WARNING: {metrics['specular_note']}")
            lines.append(f"    Glossy surfaces: {', '.join(metrics.get('specular_risk', []))}")

    # ── Existing lights ──
    existing = analysis.get("lights", [])
    if existing:
        lines.append("")
        lines.append(f"EXISTING SCENE LIGHTS ({len(existing)} total):")
        active = [l for l in existing if l.get("is_on", True)]
        if not active:
            lines.append("  (all lights are OFF)")
        else:
            for light in active:
                ies_tag = " [IES]" if light.get("ies") else ""
                shadow_tag = " [shadow]" if light.get("cast_shadow") else ""
                pos = ""
                if "x_m" in light:
                    pos = f" at ({light['x_m']:.1f}, {light['y_m']:.1f}, alt={light['altitude_m']:.1f})m"
                cone = ""
                if "cone_angle" in light:
                    cone = f", cone={light['cone_angle']}deg"

                lines.append(
                    f"  {light['kind']}: {light['color_temp_k']}K, "
                    f"intensity={light['intensity']}{cone}{ies_tag}{shadow_tag}{pos}"
                )

        if "avg_color_temp_k" in metrics:
            lines.append(
                f"  Average colour temp: {metrics['avg_color_temp_k']}K "
                f"(range: {metrics.get('color_temp_range', '?')})"
            )
        if "total_intensity" in metrics:
            lines.append(f"  Total intensity: {metrics['total_intensity']}")
        if "color_temp_note" in metrics:
            lines.append(f"  WARNING: {metrics['color_temp_note']}")
        if "dominant_lights_note" in metrics:
            lines.append(f"  NOTE: {metrics['dominant_lights_note']}")

    lines.append("")
    return "\n".join(lines)

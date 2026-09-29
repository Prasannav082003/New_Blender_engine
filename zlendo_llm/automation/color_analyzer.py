"""
Extract and analyse surface colours from the scene JSON.

Works on the CULLED scene so only rooms/walls the camera can actually see
are considered.  This keeps the LLM token budget low and the lighting
adjustments relevant to the rendered view.

Colour classification:
    luminance < 0.15  -> very_dark    (e.g. #281b1b, deep navy)
    luminance < 0.35  -> dark         (e.g. #0f58eb dark blue wall)
    luminance < 0.65  -> medium
    luminance < 0.85  -> light
    luminance >= 0.85 -> very_light   (e.g. #ffffff white)

The relative luminance formula is sRGB-aware (linearises before weighting).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ── Colour helpers ────────────────────────────────────────────────────────

def _hex_to_rgb(hex_color: str) -> Optional[Tuple[float, float, float]]:
    """Parse '#RRGGBB' or '#RGB' to (r, g, b) in [0, 1]."""
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = h[0] * 2 + h[1] * 2 + h[2] * 2
    if len(h) != 6:
        return None
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return r / 255.0, g / 255.0, b / 255.0
    except ValueError:
        return None


def _srgb_to_linear(c: float) -> float:
    """sRGB gamma-expand a single channel."""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(r: float, g: float, b: float) -> float:
    """WCAG relative luminance (0 = black, 1 = white)."""
    return (
        0.2126 * _srgb_to_linear(r)
        + 0.7152 * _srgb_to_linear(g)
        + 0.0722 * _srgb_to_linear(b)
    )


def classify_luminance(lum: float) -> str:
    if lum < 0.15:
        return "very_dark"
    if lum < 0.35:
        return "dark"
    if lum < 0.65:
        return "medium"
    if lum < 0.85:
        return "light"
    return "very_light"


# ── Data structures ───────────────────────────────────────────────────────

@dataclass
class SurfaceColor:
    """One surface (wall side, floor, ceiling) with its resolved colour."""
    surface_type: str       # "wall_inner", "wall_outer", "floor", "ceiling"
    area_id: str            # which room / area
    hex_color: str
    luminance: float
    classification: str     # very_dark / dark / medium / light / very_light
    has_texture: bool       # True if a texture map overrides the base colour
    texture_url: str = ""


@dataclass
class ColorAnalysis:
    """Full colour analysis of the visible scene."""
    surfaces: List[SurfaceColor] = field(default_factory=list)
    avg_wall_luminance: float = 0.0
    avg_floor_luminance: float = 0.0
    avg_ceiling_luminance: float = 0.0
    overall_luminance: float = 0.0
    overall_classification: str = "medium"
    dominant_mood: str = "neutral"       # warm / cool / neutral
    day_render: bool = True
    room_count: int = 0

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Compact the surfaces list for the LLM prompt
        d["surfaces"] = [
            {
                "type": s["surface_type"],
                "area": s["area_id"],
                "color": s["hex_color"],
                "luminance": round(s["luminance"], 3),
                "class": s["classification"],
                "textured": s["has_texture"],
            }
            for s in d["surfaces"]
        ]
        return d

    def summary_for_llm(self) -> str:
        """Compact text summary for the LLM prompt — saves tokens."""
        lines = [
            f"Scene colour analysis ({self.room_count} visible room(s), "
            f"{'day' if self.day_render else 'night'} render):",
            f"  Overall luminance: {self.overall_luminance:.3f} "
            f"({self.overall_classification})",
            f"  Avg wall luminance:    {self.avg_wall_luminance:.3f}",
            f"  Avg floor luminance:   {self.avg_floor_luminance:.3f}",
            f"  Avg ceiling luminance: {self.avg_ceiling_luminance:.3f}",
            f"  Dominant colour mood:  {self.dominant_mood}",
            "",
            "Surface details:",
        ]
        for s in self.surfaces:
            tex = " (textured)" if s.has_texture else ""
            lines.append(
                f"  {s.surface_type:12s}  {s.hex_color}  "
                f"lum={s.luminance:.3f}  {s.classification}{tex}"
            )
        return "\n".join(lines)


# ── Extraction from floor_plan_data ───────────────────────────────────────

def _extract_color(material: Dict[str, Any]) -> Tuple[str, bool, str]:
    """Return (hex_color, has_texture, texture_url) from a material dict."""
    color = str(material.get("color", "#ffffff")).strip()
    if not color.startswith("#"):
        color = "#ffffff"
    map_url = str(material.get("mapUrl", "") or "")
    has_texture = bool(map_url.strip())
    return color, has_texture, map_url


def _guess_texture_luminance(texture_url: str) -> Optional[float]:
    """
    Rough luminance guess from texture filename keywords.

    When a texture is present, the base colour is often just #ffffff
    placeholder.  The filename gives a better signal:
      - "dark", "black", "charcoal", "ebony" -> low
      - "white", "light", "cream", "ivory"   -> high
      - "wood", "oak", "walnut", "mahogany"  -> medium-dark
      - "marble", "granite", "concrete"      -> medium
    """
    url_lower = texture_url.lower()
    dark_kw = ("dark", "black", "charcoal", "ebony", "anthracite", "night")
    light_kw = ("white", "light", "cream", "ivory", "snow", "bright")
    warm_dark_kw = ("walnut", "mahogany", "cherry", "espresso", "wenge")
    warm_med_kw = ("wood", "oak", "teak", "pine", "bamboo", "birch")
    cool_med_kw = ("marble", "granite", "concrete", "slate", "stone", "cement")

    for kw in dark_kw:
        if kw in url_lower:
            return 0.10
    for kw in warm_dark_kw:
        if kw in url_lower:
            return 0.20
    for kw in light_kw:
        if kw in url_lower:
            return 0.85
    for kw in warm_med_kw:
        if kw in url_lower:
            return 0.40
    for kw in cool_med_kw:
        if kw in url_lower:
            return 0.45
    return None


def _color_temperature(r: float, g: float, b: float) -> str:
    """Classify (r,g,b) as warm / cool / neutral."""
    if r > b + 0.15:
        return "warm"
    if b > r + 0.15:
        return "cool"
    return "neutral"


def analyze_scene_colors(scene: Dict[str, Any]) -> ColorAnalysis:
    """
    Walk the (culled) scene JSON and extract every visible surface colour.

    Expects the floor_plan_data to be already parsed (dict), or a JSON
    string that will be parsed here.
    """
    analysis = ColorAnalysis()
    analysis.day_render = bool(scene.get("day_render", True))

    # Parse floor_plan_data
    fpd = scene.get("floor_plan_data")
    if isinstance(fpd, str):
        try:
            fpd = json.loads(fpd)
        except (json.JSONDecodeError, TypeError):
            log.warning("Could not parse floor_plan_data as JSON")
            return analysis
    if not isinstance(fpd, dict):
        log.warning("floor_plan_data is not a dict")
        return analysis

    wall_lums: List[float] = []
    floor_lums: List[float] = []
    ceiling_lums: List[float] = []
    temp_votes: List[str] = []

    layers = fpd.get("layers", {})
    if not isinstance(layers, dict):
        return analysis

    for layer_id, layer in layers.items():
        if not isinstance(layer, dict):
            continue

        # ── Walls ────────────────────────────────────────────────
        lines = layer.get("lines", {})
        if isinstance(lines, dict):
            for wall_id, wall in lines.items():
                if not isinstance(wall, dict):
                    continue
                # Inner face (what the camera usually sees)
                inner = wall.get("inner_properties", {})
                if isinstance(inner, dict):
                    mat = inner.get("material", {}) or {}
                    color, has_tex, tex_url = _extract_color(mat)
                    rgb = _hex_to_rgb(color)
                    if rgb:
                        lum = relative_luminance(*rgb)
                        # If textured and base color is white placeholder,
                        # use the texture keyword guess instead
                        if has_tex and lum > 0.8:
                            tex_lum = _guess_texture_luminance(tex_url)
                            if tex_lum is not None:
                                lum = tex_lum
                        analysis.surfaces.append(SurfaceColor(
                            surface_type="wall_inner",
                            area_id=wall_id[:8],
                            hex_color=color,
                            luminance=round(lum, 4),
                            classification=classify_luminance(lum),
                            has_texture=has_tex,
                            texture_url=tex_url,
                        ))
                        wall_lums.append(lum)
                        temp_votes.append(_color_temperature(*rgb))

        # ── Areas (floors + ceilings) ────────────────────────────
        areas = layer.get("areas", {})
        if isinstance(areas, dict):
            analysis.room_count += len(areas)
            for area_id, area in areas.items():
                if not isinstance(area, dict):
                    continue

                # Floor
                fp = area.get("floor_properties", {})
                if isinstance(fp, dict):
                    mat = fp.get("material", {}) or {}
                    color, has_tex, tex_url = _extract_color(mat)
                    rgb = _hex_to_rgb(color)
                    if rgb:
                        lum = relative_luminance(*rgb)
                        if has_tex and lum > 0.8:
                            tex_lum = _guess_texture_luminance(tex_url)
                            if tex_lum is not None:
                                lum = tex_lum
                        analysis.surfaces.append(SurfaceColor(
                            surface_type="floor",
                            area_id=area_id[:8],
                            hex_color=color,
                            luminance=round(lum, 4),
                            classification=classify_luminance(lum),
                            has_texture=has_tex,
                            texture_url=tex_url,
                        ))
                        floor_lums.append(lum)
                        temp_votes.append(_color_temperature(*rgb))

                # Ceiling
                cp = area.get("ceiling_properties", {})
                if isinstance(cp, dict):
                    mat = cp.get("material", {}) or {}
                    color, has_tex, tex_url = _extract_color(mat)
                    rgb = _hex_to_rgb(color)
                    if rgb:
                        lum = relative_luminance(*rgb)
                        if has_tex and lum > 0.8:
                            tex_lum = _guess_texture_luminance(tex_url)
                            if tex_lum is not None:
                                lum = tex_lum
                        analysis.surfaces.append(SurfaceColor(
                            surface_type="ceiling",
                            area_id=area_id[:8],
                            hex_color=color,
                            luminance=round(lum, 4),
                            classification=classify_luminance(lum),
                            has_texture=has_tex,
                            texture_url=tex_url,
                        ))
                        ceiling_lums.append(lum)

                # Room area colour (the "areaColor" field)
                props = area.get("properties", {})
                if isinstance(props, dict):
                    area_color = props.get("areaColor", "")
                    if area_color and isinstance(area_color, str):
                        rgb = _hex_to_rgb(area_color)
                        if rgb:
                            temp_votes.append(_color_temperature(*rgb))

    # ── Aggregates ────────────────────────────────────────────────
    def _avg(vals: List[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.5

    analysis.avg_wall_luminance = round(_avg(wall_lums), 3)
    analysis.avg_floor_luminance = round(_avg(floor_lums), 3)
    analysis.avg_ceiling_luminance = round(_avg(ceiling_lums), 3)

    all_lums = wall_lums + floor_lums + ceiling_lums
    # Walls matter more than ceiling for perceived brightness
    weighted = wall_lums * 2 + floor_lums + ceiling_lums
    analysis.overall_luminance = round(_avg(weighted), 3)
    analysis.overall_classification = classify_luminance(analysis.overall_luminance)

    # Dominant mood from colour temperatures
    if temp_votes:
        from collections import Counter
        counts = Counter(temp_votes)
        analysis.dominant_mood = counts.most_common(1)[0][0]

    return analysis

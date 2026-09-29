"""
The lighting agent: reads Godot's scene audit, decides what is wrong,
patches the scene JSON.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from .ollama_client import OllamaClient
from .schema import EXPOSURE_DECISION_SCHEMA, LIGHTING_DECISION_SCHEMA
from .validate import validate_decision, validate_exposure

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are a lighting technical director for an architectural visualisation \
renderer. You review an automated analysis of a 3D interior scene and \
decide how it should be lit.

You are NOT writing code and NOT placing raw engine lights. You choose \
values for a lighting system that is already built and already tuned. \
Your output is consumed directly by that system.

The three global controls:

- lighting_profile: "day" | "sunset" | "night".

- colorMood: "warm" | "soft" | "neutral" | "cool" | "day".

- illuminationLevel: integer 1-5.

You may also add lights when the analysis shows a real deficit.

- sunThroughWindows: boolean. Set true when the scene has windows.

- sunElevation: degrees (5-60).

ALL COORDINATES ARE IN METRES. The analysis gives you the room's exact \
x and y range in metres - every light you place must fall inside it. \
altitude is height above the floor in metres. A light outside the stated \
range is discarded, so check the numbers before you answer.

Judge the CURRENT state in "verdict" - that is your assessment BEFORE \
your changes. Be concise and concrete in "issues".

MATERIAL-AWARE LIGHTING RULES:

You will receive a surface material analysis covering wall, floor, and \
ceiling colours, roughness, and texture types, plus an inventory of \
every light already in the scene. Use this to make better decisions:

1. REFLECTIVITY: Dark surfaces (luminance < 0.2) absorb light. Increase \
   illuminationLevel by 1 relative to what geometry alone would suggest. \
   Bright surfaces (luminance > 0.7) bounce light aggressively - reduce \
   illuminationLevel or use softer, more diffuse lights.

2. COLOUR TEMPERATURE: Match lighting warmth to the surface palette. \
   Warm-toned surfaces (wood, terracotta, beige) pair with 2700-3500K. \
   Cool-toned surfaces (blue, grey, white marble) pair with 4000-5500K. \
   Mixed warm/cool surfaces need a neutral 3500-4500K to avoid colour cast.

3. EXISTING LIGHTS: The scene may already have point, spot, rect, or \
   IES lights placed by the user. Account for their intensity and \
   colour temperature when setting illuminationLevel and colorMood. \
   If existing lights already provide adequate coverage (total intensity \
   >= 3.0), reduce the global illuminationLevel to avoid overexposure. \
   Match your addLights colour temperature to within 500K of the \
   existing lights unless there is a deliberate reason to contrast.

4. SPECULAR SURFACES: Glossy floors (roughness < 0.3) or metallic \
   surfaces create mirror-like reflections. Never place a spot or point \
   light directly above a glossy floor - use rect lights or indirect \
   bounce lighting instead. If you must add overhead lights, increase \
   their altitude and reduce intensity.

5. ROOM TYPE: A living room with a sofa wants warm ambient lighting. \
   A kitchen wants bright, even illumination at 4000-5000K. A bedroom \
   wants low illumination with warm colour temperature. Use the \
   inferred room type and furniture list to inform your choices.

6. IES LIGHTS: If the scene contains IES-profiled lights, these have \
   carefully shaped beam patterns. Avoid placing competing lights at \
   similar positions - supplement, do not duplicate.
"""


def _summarise_report(
    report: Dict[str, Any],
    material_context: Optional[str] = None,
) -> str:
    """Turn Godot's audit into compact prose plus JSON."""
    room = report.get("room", {}) or {}
    lights = report.get("lights", {}) or {}
    windows = report.get("windows", {}) or {}
    profile = report.get("profile", {}) or {}

    area = room.get("floor_area_m2")
    ceiling = room.get("ceiling_height")
    n_fix = lights.get("ceiling_fixture_count", 0)
    n_custom = lights.get("custom_light_count", 0)
    n_items = lights.get("fixture_item_count", 0)
    n_win = windows.get("count", 0)
    win_area = windows.get("total_area_m2")

    lines: List[str] = []
    lines.append(
        f"Room: {area:.1f} m2 floor area, {ceiling:.2f} m ceiling."
        if isinstance(area, (int, float)) and isinstance(ceiling, (int, float))
        else "Room dimensions unavailable."
    )
    lines.append(
        f"Requested render: profile='{profile.get('lighting_profile', '?')}', "
        f"mood='{profile.get('color_mood', '?')}', "
        f"illuminationLevel={profile.get('illumination_level', '?')}."
    )
    lines.append(
        f"Windows: {n_win}"
        + (f", {win_area:.1f} m2 total glazing."
           if isinstance(win_area, (int, float)) and win_area > 0 else ".")
    )

    win_details = windows.get("window_details", [])
    if win_details:
        for wd in win_details:
            wid = wd.get("id", "?")
            facing = ""
            if "facing_nx" in wd and "facing_ny" in wd:
                facing = f", facing direction=({wd['facing_nx']}, {wd['facing_ny']})"
            mid = ""
            if "wall_mid_x" in wd and "wall_mid_y" in wd:
                mid = f", wall midpoint=({wd['wall_mid_x']:.1f}, {wd['wall_mid_y']:.1f})"
            lines.append(
                f"  Window '{wid}': {wd.get('width', 0):.1f}x{wd.get('height', 0):.1f}"
                f"{mid}{facing}"
            )

    us = room.get("unit_scale")
    if us and all(k in room for k in ("min_x", "max_x", "min_y", "max_y")):
        lines.append(
            f"ROOM BOUNDS (metres): x from {room['min_x'] * us:.2f} to "
            f"{room['max_x'] * us:.2f}, y from {room['min_y'] * us:.2f} to "
            f"{room['max_y'] * us:.2f}."
        )
    if isinstance(ceiling, (int, float)) and ceiling > 0:
        lines.append(
            f"Ceiling height: {ceiling:.2f} m."
        )
    lines.append(
        f"Existing lights: {n_fix} ceiling fixtures, {n_custom} authored custom lights, "
        f"{n_items} light-fixture props."
    )

    if isinstance(area, (int, float)) and area > 0:
        density = (n_fix + n_custom) / area * 100.0
        lines.append(f"Light density: {density:.1f} lights per 100 m2.")

    prof = str(profile.get("lighting_profile", "")).lower()
    if prof in ("night", "sunset") and (n_fix + n_custom) == 0:
        lines.append(
            "WARNING: low-daylight render with no interior lights."
        )
    if prof == "day" and n_win == 0:
        lines.append(
            "WARNING: day render with no windows detected."
        )

    # ── Material + light inventory context ──
    if material_context:
        lines.append("")
        lines.append("─── MATERIAL & LIGHT ANALYSIS ───")
        lines.append(material_context)

    return "\n".join(lines) + "\n\nFull analysis:\n" + json.dumps(
        report, indent=2, ensure_ascii=False
    )


class LightingAgent:
    def __init__(self, client: OllamaClient):
        self.client = client

    # ------------------------------------------------------------------
    def review(
        self,
        report: Dict[str, Any],
        material_context: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Any]:
        """
        Given Godot's lighting audit, return a validated patch.

        If `material_context` is provided (from scene_material_analyzer),
        it is spliced into the user prompt so the model accounts for
        surface colours, textures, and existing light fixtures.

        Returns (patch, validation_report).
        """
        user = _summarise_report(report, material_context=material_context)
        log.info("Requesting lighting decision from %s ...", self.client.cfg.model)

        raw = self.client.chat_json(
            system=SYSTEM_PROMPT,
            user=user,
            schema=LIGHTING_DECISION_SCHEMA,
        )

        room = report.get("room", {}) or {}
        patch, validation = validate_decision(raw, room)

        log.info(
            "Verdict=%s  added=%d lights  issues=%d",
            patch["verdict"], len(patch["customLights"]), len(patch["issues"]),
        )
        for issue in patch["issues"]:
            log.info("  issue: %s", issue)
        if not validation.clean:
            log.warning("Validation adjusted the model's output: %s", validation.as_dict())

        return patch, validation

    # ------------------------------------------------------------------
    def review_exposure(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        """
        Post-render pass. `metrics` comes from render_metrics.py.
        """
        user = (
            "A 4K path-traced interior render just completed. Below are measured "
            "photometric statistics of the output image. Decide whether the "
            "exposure is acceptable or needs correction.\n\n"
            + json.dumps(metrics, indent=2)
        )

        raw = self.client.chat_json(
            system=(
                "You are a render technical director assessing exposure from "
                "measured image statistics. Be conservative: only recommend a "
                "re-render when the image is genuinely unusable, because each "
                "4K path-traced render is expensive."
            ),
            user=user,
            schema=EXPOSURE_DECISION_SCHEMA,
        )
        return validate_exposure(raw)


# ----------------------------------------------------------------------
# Merging the patch back into the scene JSON
# ----------------------------------------------------------------------
def apply_patch(
    scene: Dict[str, Any],
    patch: Dict[str, Any],
    target_layer: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Merge the agent's decision into the scene JSON that Godot will read.
    Returns a NEW dict; the input is not mutated.
    """
    import copy

    out = copy.deepcopy(scene)

    # --- global settings ---
    settings = out.setdefault("_lightingSettings", {})
    if not isinstance(settings, dict):
        settings = {}
        out["_lightingSettings"] = settings
    settings.update(patch["lightingSettings"])

    for key in ("lighting_profile", "colorMood", "illuminationLevel"):
        if key in patch["lightingSettings"]:
            out[key] = patch["lightingSettings"][key]
    prof = patch["lightingSettings"]["lighting_profile"]
    out["day_render"] = prof == "day"
    out["night_render"] = prof == "night"
    out["sunset_render"] = prof == "sunset"

    # --- added lights ---
    new_lights = patch.get("customLights") or {}
    if new_lights:
        container = out
        if target_layer:
            layers = out.get("layers")
            if isinstance(layers, dict) and target_layer in layers:
                container = layers[target_layer]

        existing = container.get("customLights")
        if not isinstance(existing, dict):
            existing = {}
        existing = {k: v for k, v in existing.items() if not str(k).startswith("llm_")}
        existing.update(new_lights)
        container["customLights"] = existing

    # --- sun through windows ---
    if patch.get("sunThroughWindows", False):
        out.pop("directional_light", None)
        ls = out.get("_lightingSettings", {})
        if isinstance(ls, dict):
            ls.pop("directionalPosition", None)
            ls.pop("directionalTarget", None)
        out["_sunThroughWindows"] = True

        sun_elev = patch.get("sunElevation")
        if sun_elev is not None:
            try:
                sun_elev = float(sun_elev)
                sun_elev = max(5.0, min(60.0, sun_elev))
                ls = out.setdefault("_lightingSettings", {})
                ls["sunElevation"] = sun_elev
                log.info("sunElevation=%.1f degrees", sun_elev)
            except (TypeError, ValueError):
                pass

        log.info("sunThroughWindows=true: cleared directional_light pose")

    # --- provenance ---
    out["_llmLightingAudit"] = {
        "verdict": patch.get("verdict"),
        "issues": patch.get("issues", []),
        "reasoning": patch.get("reasoning", ""),
        "lights_added": len(new_lights),
        "sun_through_windows": patch.get("sunThroughWindows", False),
    }
    return out

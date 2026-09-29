"""
Colour-aware lighting adjuster.

Reads the ColorAnalysis of the culled scene and asks the LLM to choose
lighting parameters that compensate for surface colours:
    - dark walls/floors  -> brighter lights, higher illumination
    - light walls/floors -> lower light energy to avoid washout
    - warm tones         -> slightly cool lighting to balance
    - cool tones         -> slightly warm lighting to balance

The output is a structured JSON patch applied directly to the scene's
_lightingSettings and root keys before the existing AI lighting pass
(if enabled) runs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from .color_analyzer import ColorAnalysis

log = logging.getLogger(__name__)


# ── Structured output schema ─────────────────────────────────────────────

LIGHTING_ADJUSTMENT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "illuminationLevel": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": (
                "Overall brightness 1-5.  Raise for dark surfaces that "
                "absorb light; lower for bright surfaces that reflect it."
            ),
        },
        "colorMood": {
            "type": "string",
            "enum": ["warm", "soft", "neutral", "cool", "day"],
            "description": (
                "Colour mood.  Use to balance the dominant surface tones: "
                "warm surfaces -> prefer 'neutral' or 'cool'; "
                "cool surfaces -> prefer 'warm' or 'neutral'."
            ),
        },
        "ambientBoost": {
            "type": "number",
            "minimum": 0.5,
            "maximum": 2.5,
            "description": (
                "Multiplier on ambient light energy (1.0 = no change). "
                "Raise above 1.0 for rooms with dark surfaces to "
                "prevent shadowed areas from going black.  Very dark "
                "walls (luminance < 0.15) need 1.8-2.2."
            ),
        },
        "sunEnergyMultiplier": {
            "type": "number",
            "minimum": 0.5,
            "maximum": 2.5,
            "description": (
                "Multiplier on sun/directional energy (1.0 = no change). "
                "Raise for dark rooms to push more bounce light.  Lower "
                "for very bright rooms to avoid blown highlights."
            ),
        },
        "exposureScale": {
            "type": "number",
            "minimum": 0.6,
            "maximum": 1.6,
            "description": (
                "Global exposure trim (1.0 = no change). This is the whole-image "
                "brightness control: it scales the final tonemapped result rather "
                "than any single light, so it changes overall exposure without "
                "altering the balance between the sun, the sky fill and the "
                "fixtures. Below 1.0 for a room whose surfaces are bright enough "
                "to push the image toward washout; leave at 1.0 when the light "
                "balance itself is what needs changing."
            ),
        },
        "reasoning": {
            "type": "string",
            "description": "One sentence explaining the adjustment.",
        },
    },
    "required": [
        "illuminationLevel",
        "colorMood",
        "ambientBoost",
        "sunEnergyMultiplier",
        "exposureScale",
        "reasoning",
    ],
}


SYSTEM_PROMPT = """\
You are a lighting automation agent for an architectural renderer. You are \
given a colour analysis of the room surfaces (walls, floors, ceilings) that \
are visible to the camera in THIS render.

Your job: choose lighting parameters that make the render look natural and \
appealing given the surface colours.

Rules:
1. DARK surfaces absorb light.  A room with dark walls needs MORE light to \
   look bright enough - raise illuminationLevel and ambientBoost.
2. LIGHT surfaces reflect light.  A bright white room needs LESS light or \
   it washes out - lower illuminationLevel or keep ambientBoost at 1.0.
3. WARM-toned surfaces (reds, oranges, wood) look best with neutral or \
   slightly cool lighting to avoid looking too orange.
4. COOL-toned surfaces (blues, grays) look best with neutral or slightly \
   warm lighting to avoid looking cold and clinical.
5. For day renders, keep colorMood as "day" unless the surfaces strongly \
   call for something else.
6. IMPORTANT: dark walls absorb most of the light energy.  A room with \
   very dark walls (luminance < 0.15) needs aggressive compensation even \
   if the ceiling is bright.  Use the AVG WALL LUMINANCE as your primary \
   guide, not overall luminance.

7. WHAT illuminationLevel ACTUALLY DOES. On a day render it scales the
   AMBIENT sky fill - light arriving from every direction at once, with no
   position and casting no shadows. It does not touch the sun.

   That makes it a CONTRAST control as much as a brightness one. Ambient
   fill lands on an object and on the wall behind it equally, so when it is
   strong relative to the sun the two sit at the same value and the render
   reads as flat shapes pasted together rather than objects in a room. When
   it is weaker than the sun, directional shading and shadow gradients
   return and objects separate from their background.

   At the room's directional energy the ratios are roughly:

     level 5 -> ambient 2.0x the sun    flat, fully filled
     level 3 -> ambient 1.2x the sun    soft
     level 2 -> ambient 0.8x the sun    sun-led, shadows read
     level 1 -> ambient 0.7x the sun    strongest form and contrast

   A bright room wants a LOW level: its own walls already bounce plenty of
   fill, so adding sky fill on top only removes contrast.

Quick reference (use avg_wall_luminance as the key driver):
  wall_luminance < 0.15 (very dark):  illumination 5, ambientBoost 1.8-2.2
  wall_luminance 0.15-0.35 (dark):    illumination 5, ambientBoost 1.5-1.8
  wall_luminance 0.35-0.65 (medium):  illumination 3-4, ambientBoost 1.0
  wall_luminance 0.65-0.85 (light):   illumination 2,   ambientBoost 0.75-0.9
  wall_luminance > 0.85 (very light): illumination 1,   ambientBoost 0.6-0.8

Respond ONLY with the JSON object matching the schema.
"""


class AutoLightingAdjuster:
    """Send the colour analysis to the LLM and get a lighting patch."""

    def __init__(self, client):
        """client is an OllamaClient instance."""
        self.client = client

    def adjust(
        self,
        analysis: ColorAnalysis,
    ) -> Dict[str, Any]:
        """
        Returns a validated adjustment dict.

        If the LLM call fails, returns safe defaults (no change).
        """
        user_prompt = analysis.summary_for_llm()

        log.info(
            "Requesting colour-aware lighting adjustment (overall_lum=%.3f, "
            "class=%s, mood=%s)",
            analysis.overall_luminance,
            analysis.overall_classification,
            analysis.dominant_mood,
        )

        try:
            raw = self.client.chat_json(
                system=SYSTEM_PROMPT,
                user=user_prompt,
                schema=LIGHTING_ADJUSTMENT_SCHEMA,
            )
        except Exception as exc:
            log.warning(
                "LLM colour adjustment failed (%s) - using fallback", exc
            )
            raw = _fallback_adjustment(analysis)

        return _validate(raw, analysis)


def _fallback_adjustment(analysis: ColorAnalysis) -> Dict[str, Any]:
    """Deterministic fallback when the LLM is unreachable.

    Uses wall luminance as the primary driver because walls dominate
    what the eye perceives.  A bright ceiling can reflect light back,
    but dark walls absorb most of the energy and the room *looks* dark.
    """
    # Walls are what you see — use wall luminance, not overall average
    # which gets inflated by bright ceilings.
    lum = (
        analysis.avg_wall_luminance
        if analysis.avg_wall_luminance > 0
        else analysis.overall_luminance
    )

    if lum < 0.15:
        level, boost, sun = 5, 2.0, 1.6
    elif lum < 0.35:
        level, boost, sun = 5, 1.6, 1.4
    elif lum < 0.65:
        level, boost, sun = 3, 1.0, 1.0
    elif lum < 0.85:
        level, boost, sun = 2, 0.80, 0.90
    else:
        # Bright end recalibrated after illuminationLevel was fixed.
        #
        # These numbers were set while illuminationLevel reached NOTHING on
        # a day render, so ambientBoost was the only damping a bright room
        # ever received and 0.8 had to carry it alone. Now the level scales
        # the ambient too, and a very light room at level 1 lands on the
        # 0.35 floor - the LLM's illumination control is already bottomed
        # out and has no headroom left. This reference is what gives it the
        # remaining authority, so it moves down with it.
        level, boost, sun = 1, 0.70, 0.85

    mood = "day" if analysis.day_render else "neutral"
    if not analysis.day_render:
        if analysis.dominant_mood == "warm":
            mood = "neutral"
        elif analysis.dominant_mood == "cool":
            mood = "warm"

    # Global exposure trim by surface brightness.
    #
    # Measured against the reference render, a room with walls at luminance
    # 1.000 came out 1.22x too bright, uniformly across ceiling, walls and
    # floor. Uniform across every surface is the signature of an exposure
    # difference rather than a lighting one, and exposure is the correct
    # control for it - it moves the whole image without disturbing the
    # balance between sun, sky fill and fixtures that the other three values
    # have just been set to achieve.
    #
    # 0.85 rather than 1/1.22 = 0.82 because the tonemapper compresses:
    # a given change in scene exposure produces a slightly larger change in
    # output midtones.
    if lum >= 0.85:
        exposure = 0.85
    elif lum >= 0.65:
        exposure = 0.93
    else:
        exposure = 1.0

    return {
        "illuminationLevel": level,
        "colorMood": mood,
        "ambientBoost": boost,
        "sunEnergyMultiplier": sun,
        "exposureScale": exposure,
        "reasoning": (
            f"Fallback: overall luminance {lum:.2f} "
            f"({analysis.overall_classification}), "
            f"surfaces are {analysis.dominant_mood}-toned."
        ),
    }


def _validate(
    raw: Dict[str, Any],
    analysis: ColorAnalysis,
) -> Dict[str, Any]:
    """Clamp values to safe ranges."""
    out = {}
    out["illuminationLevel"] = max(1, min(5, int(raw.get("illuminationLevel", 3))))
    out["colorMood"] = raw.get("colorMood", "day")
    if out["colorMood"] not in ("warm", "soft", "neutral", "cool", "day"):
        out["colorMood"] = "day" if analysis.day_render else "neutral"

    # Bounded against the DETERMINISTIC value for this room, not just the
    # global [0.5, 2.5] range.
    #
    # On a room with wall luminance 1.000 the model returned
    # sunEnergyMultiplier=1.2 - a 20% sun BOOST - while its own reasoning
    # said "the room has very light walls... the lighting should be kept low
    # to avoid washing out the surfaces", and while it correctly set
    # ambientBoost to 0.8. The reasoning did not mention the sun at all. The
    # deterministic rule for that luminance is 0.85, so the model overshot
    # by 41% in the direction its own analysis said was wrong, and the flat
    # [0.5, 2.5] clamp let it through.
    #
    # The fallback table encodes the luminance->gain relationship that the
    # model is being asked to apply. Letting the model move within a band
    # around it keeps its judgement while making it impossible to contradict
    # the room's measured brightness. A wide band (x0.75 to x1.35) because
    # this is a guard against inversions, not a straitjacket.
    reference = _fallback_adjustment(analysis)
    for key in ("ambientBoost", "sunEnergyMultiplier", "exposureScale"):
        try:
            value = float(raw.get(key, 1.0))
        except (TypeError, ValueError):
            value = float(reference[key])
        ref = float(reference[key])
        lower = ref * 0.75
        upper = ref * 1.35
        # No inversions. When the measurement says this room needs DAMPING
        # (reference below 1.0), the model may argue about how much, but it
        # may not turn the gain into a boost - and vice versa for a dark
        # room. Without this the band alone still permitted 1.15 on a room
        # whose walls measure 1.000, which is the same mistake in a smaller
        # size.
        if ref <= 1.0:
            upper = min(upper, 1.0)
        if ref >= 1.0:
            lower = max(lower, 1.0)
        bounded = max(lower, min(upper, value))
        if abs(bounded - value) > 0.01:
            log.warning(
                "%s=%.2f contradicts the measured room (wall luminance %.2f "
                "implies %.2f); pulled to %.2f",
                key, value, analysis.avg_wall_luminance, ref, bounded,
            )
        out[key] = max(0.6, min(1.6, bounded)) if key == "exposureScale" \
            else max(0.5, min(2.5, bounded))
    out["reasoning"] = str(raw.get("reasoning", ""))[:500]
    return out

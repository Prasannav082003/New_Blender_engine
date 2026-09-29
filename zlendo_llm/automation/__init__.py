"""
Automation pipeline: colour-aware lighting adjustment and per-fixture
emission control.

Both run AFTER scene culling so only visible rooms are analysed, keeping
token usage minimal and the adjustments accurate.

Two passes, two questions, two flags:

    auto_adjust_lighting   how bright is the ROOM?
                           illuminationLevel, colorMood, ambientBoost,
                           sunEnergyMultiplier.
                           Gated by AUTOMATION_LLM_ADJUSTMENT.

    auto_adjust_emission   what does each light ASSET emit?
                           light_meta.intensity, light_meta.temperature,
                           per fixture.
                           Gated by AUTOMATION_LLM_EMISSION.

Order matters: emission reads the colorMood the colour pass sets, so it
runs second.
"""

from .color_analyzer import ColorAnalysis, analyze_scene_colors
from .lighting_adjuster import AutoLightingAdjuster, LIGHTING_ADJUSTMENT_SCHEMA
from .integration import auto_adjust_lighting, auto_adjust_emission
from .emission_analyzer import (
    EmissionAnalysis,
    EmissionFixture,
    analyze_scene_emission,
)
from .emission_agent import (
    EmissionAgent,
    EMISSION_DECISION_SCHEMA,
    EMISSION_FIXTURE_SCHEMA,
)

__all__ = [
    "ColorAnalysis",
    "analyze_scene_colors",
    "AutoLightingAdjuster",
    "LIGHTING_ADJUSTMENT_SCHEMA",
    "auto_adjust_lighting",
    "EmissionAnalysis",
    "EmissionFixture",
    "analyze_scene_emission",
    "EmissionAgent",
    "EMISSION_DECISION_SCHEMA",
    "EMISSION_FIXTURE_SCHEMA",
    "auto_adjust_emission",
]

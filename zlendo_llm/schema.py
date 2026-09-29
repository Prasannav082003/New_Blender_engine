"""
The contract between the LLM and your Godot lighting code.

Every field here maps onto something image_glb_creation.gd ALREADY reads.
"""

from __future__ import annotations

from typing import Any, Dict

LIGHTING_PROFILES = ["day", "sunset", "night"]
COLOR_MOODS = ["warm", "soft", "neutral", "cool", "day"]
LIGHT_KINDS = ["point", "spot", "rect"]

LIMITS = {
    "illuminationLevel": (1, 5),
    "colorTemp": (1800.0, 7500.0),
    "intensityMultiplier": (0.05, 4.0),
    "coneAngle": (10.0, 150.0),
    "tiltAngle": (-90.0, 90.0),
    "altitude_frac": (0.05, 0.98),
    "max_added_lights": 8,
}


CUSTOM_LIGHT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "Unique id, must start with 'llm_'.",
        },
        "lightKind": {"type": "string", "enum": LIGHT_KINDS},
        "purpose": {
            "type": "string",
            "enum": ["key", "fill", "ambient", "accent", "wall_wash", "practical"],
            "description": "Role this light plays in the composition.",
        },
        "x": {"type": "number",
              "description": "X position in METRES, inside the room's x range."},
        "y": {"type": "number",
              "description": "Y position in METRES, inside the room's y range."},
        "altitude": {
            "type": "number",
            "description": "Height above the floor in METRES.",
        },
        "colorTemp": {
            "type": "number",
            "description": "Kelvin, 1800-7500.",
        },
        "intensityMultiplier": {
            "type": "number",
            "description": "Relative brightness, 1.0 = engine default. Range 0.05-4.0.",
        },
        "coneAngle": {"type": "number", "description": "Spot only, degrees."},
        "tiltAngle": {"type": "number", "description": "Downward tilt, degrees."},
        "rotationY": {"type": "number", "description": "Yaw in radians."},
        "castShadow": {"type": "boolean"},
        "isOn": {"type": "boolean"},
        "rationale": {"type": "string", "description": "One sentence: why this light."},
    },
    "required": [
        "id", "lightKind", "purpose", "x", "y", "altitude",
        "colorTemp", "intensityMultiplier", "castShadow", "isOn", "rationale",
    ],
}


LIGHTING_DECISION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["good", "acceptable", "poor"],
            "description": "Assessment of the CURRENT lighting before your changes.",
        },
        "issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete problems found.",
        },
        "lightingSettings": {
            "type": "object",
            "properties": {
                "lighting_profile": {"type": "string", "enum": LIGHTING_PROFILES},
                "colorMood": {"type": "string", "enum": COLOR_MOODS},
                "illuminationLevel": {
                    "type": "integer",
                    "description": "1 (dim) to 5 (bright).",
                },
            },
            "required": ["lighting_profile", "colorMood", "illuminationLevel"],
        },
        "sunThroughWindows": {
            "type": "boolean",
            "description": "Set true when the scene has windows.",
        },
        "sunElevation": {
            "type": "number",
            "description": "Sun elevation angle in degrees (5-60).",
        },
        "addLights": {
            "type": "array",
            "items": CUSTOM_LIGHT_SCHEMA,
            "description": "New lights to place. Empty array if none needed.",
        },
        "reasoning": {
            "type": "string",
            "description": "2-4 sentences explaining the overall plan.",
        },
    },
    "required": ["verdict", "issues", "lightingSettings", "sunThroughWindows", "addLights", "reasoning"],
}


EXPOSURE_DECISION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["good", "acceptable", "poor"]},
        "problem": {
            "type": "string",
            "enum": [
                "none", "underexposed", "overexposed",
                "low_contrast", "blown_highlights", "crushed_shadows",
                "colour_cast",
            ],
        },
        "illuminationLevelDelta": {
            "type": "integer",
            "description": "Change to illuminationLevel: -2..+2. 0 = leave alone.",
        },
        "tonemapExposureScale": {
            "type": "number",
            "description": "Multiplier on tonemap exposure, 0.6-1.6. 1.0 = leave alone.",
        },
        "shouldRerender": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "verdict", "problem", "illuminationLevelDelta",
        "tonemapExposureScale", "shouldRerender", "reasoning",
    ],
}

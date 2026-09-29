"""
The JSON -> render conversion rules, as data.

This is the single source of truth for "what does changing this key actually do
to the picture". Everything else derives from it:

  * `prompt_fragment()` emits a compact subset into the LLM's system prompt, so
    the model knows the consequence of an edit rather than guessing.
  * `to_markdown()` generates RENDER_RULES.md for humans.
  * `check_against_scene()` verifies the documented paths still exist in a real
    scene file, so the rules cannot silently rot as image_glb_creation.gd moves.

Rules are DATA, not prose, for one reason: a markdown file drifts from the code
the week after it is written and nobody notices, because nothing reads it. This
file is read on every prompt pass, and `--check` fails loudly when a documented
path no longer resolves.

Every `effect` below was read out of godot_project/image_glb_creation.gd, not
inferred. Where the JSON and the engine disagree - and they do, see `metallic` -
the rule says so, because that mismatch is invisible until a render looks wrong.

Regenerate the doc after editing:

    python zlendo_llm/render_rules.py --markdown -o RENDER_RULES.md

Verify against a real scene:

    python zlendo_llm/render_rules.py --check image.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --- categories --------------------------------------------------------
CAT_MATERIAL = "material"
CAT_LIGHTING = "lighting"
CAT_GEOMETRY = "geometry"
CAT_CAMERA = "camera"

CATEGORY_ORDER = (CAT_MATERIAL, CAT_LIGHTING, CAT_GEOMETRY, CAT_CAMERA)
CATEGORY_TITLE = {
    CAT_MATERIAL: "Materials — colour and texture",
    CAT_LIGHTING: "Lighting",
    CAT_GEOMETRY: "Geometry and visibility",
    CAT_CAMERA: "Camera and render settings",
}

# --- who may write the key --------------------------------------------
# Honest about capability. A rule marked REFERENCE documents real engine
# behaviour but nothing in the pipeline edits it yet; saying so stops a future
# reader assuming a prompt can reach it.
ST_PROMPT = "prompt"        # the prompt material pass writes this
ST_LIGHTING = "lighting"    # the lighting pass writes this
ST_REFERENCE = "reference"  # documented, not currently written by any pass

STATUS_LABEL = {
    ST_PROMPT: "prompt pass",
    ST_LIGHTING: "lighting pass",
    ST_REFERENCE: "reference only",
}


@dataclass(frozen=True)
class Rule:
    key: str
    category: str
    status: str
    paths: Tuple[str, ...]
    accepts: str
    effect: str
    gotcha: str = ""
    example: str = ""
    surfaces: Tuple[str, ...] = ()
    engine_ref: str = ""

    def one_line(self) -> str:
        """Compact form for the system prompt."""
        line = f"- {self.key}: {self.accepts} -> {self.effect}"
        if self.gotcha:
            line += f"\n  ! {self.gotcha}"
        return line


# ======================================================================
# MATERIALS
# ======================================================================
_SURFACES_ARCH = ("floor", "ceiling", "wall_inner", "wall_outer")
_SURFACES_ALL = _SURFACES_ARCH + ("item",)

MATERIAL_RULES: Tuple[Rule, ...] = (
    Rule(
        key="color",
        category=CAT_MATERIAL,
        status=ST_PROMPT,
        paths=(
            "layers.<layer>.areas.<area>.floor_properties.material.color",
            "layers.<layer>.areas.<area>.ceiling_properties.material.color",
            "layers.<layer>.lines.<line>.inner_properties.material.color",
            "layers.<layer>.lines.<line>.outer_properties.material.color",
            "layers.<layer>.items.<item>.materials.<slot>.color",
        ),
        accepts='"#rrggbb" hex string (a leading # is added if missing)',
        effect="StandardMaterial3D.albedo_color — the flat colour of the surface",
        gotcha=(
            "A colour under an existing mapUrl is invisible: albedo_color "
            "multiplies the texture. To make a colour actually show, remove "
            "mapUrl in the same edit."
        ),
        example='"color": "#c0392b"',
        surfaces=_SURFACES_ALL,
        engine_ref="create_material() L5618-5630",
    ),
    Rule(
        key="mapUrl",
        category=CAT_MATERIAL,
        status=ST_PROMPT,
        paths=(
            "…floor_properties.material.mapUrl",
            "…ceiling_properties.material.mapUrl",
            "…inner_properties.material.mapUrl / outer_properties.material.mapUrl",
            "…items.<item>.materials.<slot>.mapUrl",
            "layers.<layer>.lines.<line>.asset_urls.<inner|outer>.mapUrl",
        ),
        accepts=(
            'relative path "textures/…" (preferred), or an absolute blob URL'
        ),
        effect="albedo_texture — the image painted on the surface",
        gotcha=(
            "Write the RELATIVE form. main.py prefixes the blob host when "
            "collecting and swaps in the downloaded local path afterwards; an "
            "absolute URL works but does not match author-written JSON. "
            "When mapUrl is set and no explicit colour is given, the engine "
            "forces albedo_color to white so the texture shows true colour."
        ),
        example='"mapUrl": "textures/LowResolution/Stone/Marble012A_4K-PNG_LR_Color.jpg"',
        surfaces=_SURFACES_ALL,
        engine_ref="create_material() L5632-5643",
    ),
    Rule(
        key="normalUrl",
        category=CAT_MATERIAL,
        status=ST_PROMPT,
        paths=("…material.normalUrl",),
        accepts='relative path "textures/…"',
        effect="normal_texture, and sets normal_enabled — surface relief",
        gotcha="Optional. A set with only a normal map and no mapUrl looks unchanged.",
        surfaces=_SURFACES_ALL,
        engine_ref="create_material() L5645-5649",
    ),
    Rule(
        key="roughnessUrl",
        category=CAT_MATERIAL,
        status=ST_PROMPT,
        paths=("…material.roughnessUrl",),
        accepts='relative path "textures/…"',
        effect="roughness_texture, read from the GREEN channel",
        surfaces=_SURFACES_ALL,
        engine_ref="create_material() L5651-5655",
    ),
    Rule(
        key="roughness",
        category=CAT_MATERIAL,
        status=ST_PROMPT,
        paths=("…material.roughness",),
        accepts="float 0.0–1.0 (engine default 0.65 when absent)",
        effect="StandardMaterial3D.roughness — 0 is mirror-sharp, 1 is fully matte",
        example='"roughness": 0.2',
        surfaces=_SURFACES_ALL,
        engine_ref="create_material() L5612",
    ),
    Rule(
        key="metallic",
        category=CAT_MATERIAL,
        status=ST_PROMPT,
        paths=("…material.metallic",),
        accepts="float 0.0–1.0 (engine default 0.0)",
        effect="StandardMaterial3D.metallic",
        gotcha=(
            'MISMATCH: the frontend writes "metalness" but create_material() '
            'reads "metallic". Every "metalness" value in the scene JSON is '
            "silently ignored. Write \"metallic\" to have any effect."
        ),
        surfaces=_SURFACES_ALL,
        engine_ref="create_material() L5613",
    ),
    Rule(
        key="repeatX / repeatY",
        category=CAT_MATERIAL,
        status=ST_PROMPT,
        paths=("…material.repeatX", "…material.repeatY"),
        accepts="physical tile size in SOURCE units (304.8 = 1 ft)",
        effect=(
            "on floors/walls/ceilings: UV repeat = surface_span / (tile × unit_scale), "
            "then × 2.0 density boost"
        ),
        gotcha=(
            "0 or absent means NO tiling is computed — one texture tile is "
            "stretched across the whole surface. Every wall in image.json ships "
            "with repeatX/repeatY = 0, so any texture applied to a wall must set "
            "them. On ITEMS these are read as a direct UV multiplier instead, not "
            "a physical size."
        ),
        example='"repeatX": 304.8, "repeatY": 304.8',
        surfaces=_SURFACES_ALL,
        engine_ref="_apply_architecture_texture_scale() L5271-5320; create_material() L5660-5689",
    ),
    Rule(
        key="texture_urls",
        category=CAT_MATERIAL,
        status=ST_REFERENCE,
        paths=("…material.texture_urls", "…asset_urls.<face>.texture_urls"),
        accepts="array of paths",
        effect="fallback source for mapUrl — element [0] is used when mapUrl is absent",
        surfaces=_SURFACES_ALL,
        engine_ref="create_material() L5635-5636",
    ),
    Rule(
        key="fallback_color",
        category=CAT_MATERIAL,
        status=ST_REFERENCE,
        paths=("layers.<layer>.lines.<line>.asset_urls.<inner|outer>.fallback_color",),
        accepts='"#rrggbb"',
        effect="becomes material.color for that wall face when the asset_urls block is used",
        surfaces=("wall_inner", "wall_outer"),
        engine_ref="_resolve_wall_face_material() L3920-3925",
    ),
    Rule(
        key="(texture filename keywords)",
        category=CAT_MATERIAL,
        status=ST_REFERENCE,
        paths=("…material.mapUrl",),
        accepts="n/a — read from the chosen filename",
        effect=(
            "the reflection assist inspects the mapUrl text: on a FLOOR the words "
            "tile/marble/granite/polished/porcelain/gloss/reflect/metal/chrome/glass "
            "force roughness ≤ 0.25 and enable clearcoat; on walls/items "
            "glass/mirror force near-mirror, and metal/chrome/gold force metallic = 1.0"
        ),
        gotcha=(
            "Choosing a marble texture therefore also makes the floor glossy, "
            "whether or not that was asked for. Set roughness explicitly to "
            "override the look."
        ),
        surfaces=_SURFACES_ALL,
        engine_ref="_material_is_reflective_candidate() / _apply_reflection_response_assist() L1691-1750",
    ),
    Rule(
        key="code / materialType",
        category=CAT_MATERIAL,
        status=ST_REFERENCE,
        paths=("…material.code", "…floor_properties.materialType"),
        accepts="string",
        effect="carried through for the frontend; create_material() does not read either",
        gotcha="Changing materialType does NOT change the render. Change the material dict.",
        surfaces=_SURFACES_ALL,
    ),
)


# ======================================================================
# LIGHTING
# ======================================================================
LIGHTING_RULES: Tuple[Rule, ...] = (
    Rule(
        key="lighting_profile",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=("<root>.lighting_profile", "_lightingSettings.lighting_profile"),
        accepts='"day" | "sunset" | "night"',
        effect="sun energy, sky HDRI, ambient floor, tonemapping",
        gotcha=(
            "The ROOT dict wins over _lightingSettings — a stale root key silently "
            "overrides the one you set. Also matched by substring on "
            "time_of_day / timeOfDay / render_time / environment_preset, where "
            "dusk|evening→sunset and morning|afternoon→day."
        ),
        engine_ref="_resolve_lighting_profile() L1311-1333",
    ),
    Rule(
        key="day_render / night_render / sunset_render",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=("<root>.day_render", "<root>.night_render", "<root>.sunset_render"),
        accepts="boolean",
        effect="fallback profile selection when no string key is present",
        gotcha=(
            "Checked only after the string keys. Leaving a contradictory pair "
            "(day_render=true beside lighting_profile='night') is harmless but "
            "confusing — the lighting pass rewrites all three together."
        ),
        engine_ref="_resolve_lighting_profile() L1327-1333",
    ),
    Rule(
        key="colorMood",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=("<root>.colorMood", "<root>.color_mood", "_lightingSettings.colorMood"),
        accepts='"warm" | "soft" | "neutral" | "cool" | "day"',
        effect=(
            "tints ambient, fixture, fill and directional light: "
            "warm≈2700K, soft≈3000K, neutral≈4000K, cool≈5500K, day≈6500K"
        ),
        gotcha='Unrecognised values fall back to "day", not to the current value.',
        engine_ref="_resolve_color_mood() L1336-1367",
    ),
    Rule(
        key="illuminationLevel",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=(
            "<root>.illuminationLevel",
            "<root>.illumination_level",
            "_lightingSettings.illuminationLevel",
        ),
        accepts="integer 1–5, clamped",
        effect="multiplier on ALL light energy: scale = level × 0.10, so 5 → 0.5, 1 → 0.1",
        gotcha=(
            "Not a 1.0 baseline. Level 5 is 0.5, and the cinematic math downstream "
            "is written as t = scale / 0.5. Absent means level 5."
        ),
        engine_ref="_resolve_illumination_scale() L1369-1388",
    ),
    Rule(
        key="customLights / sceneLights",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=(
            "<root>.customLights.<id>",
            "<root>.sceneLights.<id>",
            "layers.<layer>.customLights.<id>",
            "layers.<layer>.sceneLights.<id>",
        ),
        accepts="dict keyed by light id",
        effect="each entry becomes an OmniLight3D or SpotLight3D in the scene",
        gotcha=(
            "Collected from root AND every layer; later sources overwrite earlier "
            "ids. The AI prefixes its own ids with 'llm_' so repeat passes replace "
            "rather than accumulate, and author lights are never touched."
        ),
        engine_ref="_collect_custom_lights_from_source() L4339; _build_custom_lights()",
    ),
    Rule(
        key="customLights.<id>.x / .y / .altitude",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=("customLights.<id>.x", "customLights.<id>.y", "customLights.<id>.altitude"),
        accepts="numbers in PLAN units (not metres)",
        effect="light position; all three are multiplied by _scene_unit_scale",
        gotcha=(
            "The MODEL is asked for metres and validate.py converts to plan units. "
            "Writing metres straight into the JSON puts the light in the wrong "
            "place by a factor of unit_scale."
        ),
        engine_ref="_build_custom_lights() L~4425; _plan_point_3d() L~2390",
    ),
    Rule(
        key="customLights.<id>.lightKind",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=("customLights.<id>.lightKind", "customLights.<id>.lightType"),
        accepts='"point" | "spot" | "rect"',
        effect=(
            "point→OmniLight3D; spot→SpotLight3D using coneAngle; "
            "rect→SpotLight3D at 180° with width/height as the emitter size"
        ),
        gotcha=(
            "A light carrying an IES profile is built as an OMNI regardless of "
            "lightKind — the photometry already encodes the beam shape."
        ),
        engine_ref="_build_custom_lights() L~4356-4375",
    ),
    Rule(
        key="customLights.<id>.colorTemp",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=("customLights.<id>.colorTemp",),
        accepts="Kelvin, 1800–7500",
        effect="light colour via _kelvin_to_rgb()",
        gotcha='Takes precedence over "color". A colorTemp of 0 falls back to the hex colour.',
        engine_ref="_build_custom_lights() L~4390",
    ),
    Rule(
        key="customLights.<id>.intensityMultiplier",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=("customLights.<id>.intensityMultiplier", "customLights.<id>.brightness"),
        accepts="float, 1.0 = engine default (brightness is a 0–100 percentage)",
        effect=(
            "light_energy = multiplier × base × illumination_scale, where base is "
            "5.0 in day renders and 15.0 otherwise, ×4 for spots, ÷divisor for rect"
        ),
        engine_ref="_build_custom_lights() L~4396-4414",
    ),
    Rule(
        key="customLights.<id>.castShadow",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=("customLights.<id>.castShadow",),
        accepts="boolean",
        effect="enables a soft interior shadow for that fixture",
        gotcha=(
            "The only non-directional shadow source permitted in a day render, and "
            "it costs real render time. Use on one or two key lights at most."
        ),
        engine_ref="_build_custom_lights() L~4417-4421",
    ),
    Rule(
        key="customLights.<id>.isOn",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=("customLights.<id>.isOn",),
        accepts="boolean (default true)",
        effect="false skips the fixture entirely — it is never added to the scene",
        engine_ref="_build_custom_lights() L~4352",
    ),
    Rule(
        key="_sunThroughWindows / sunElevation",
        category=CAT_LIGHTING,
        status=ST_LIGHTING,
        paths=("<root>._sunThroughWindows", "_lightingSettings.sunElevation"),
        accepts="boolean; elevation in degrees 5–60",
        effect=(
            "lets the engine aim the sun through detected windows, producing rays "
            "and window-shaped floor shadows; lower elevation = longer shadows"
        ),
        gotcha=(
            "Only takes effect once the authored directional_light pose is REMOVED. "
            "apply_patch() pops directional_light plus directionalPosition/"
            "directionalTarget; leaving any of them in place keeps the old sun."
        ),
        engine_ref="_orient_directional_light_from_windows(); lighting_agent.apply_patch()",
    ),
    Rule(
        key="directional_light",
        category=CAT_LIGHTING,
        status=ST_REFERENCE,
        paths=("<root>.directional_light", "_lightingSettings.directionalPosition"),
        accepts="{intensity, position, target, color, cast_shadow, …}",
        effect="explicit sun pose and energy",
        gotcha=(
            "In DAY renders the engine overrides the JSON energy and colour with a "
            "tuned warm golden white — setting a pure white here has no effect."
        ),
        engine_ref="setup_lighting() L494-549",
    ),
    Rule(
        key="iesProfiles / iesFile",
        category=CAT_LIGHTING,
        status=ST_REFERENCE,
        paths=("floor_plan_data.iesProfiles", "customLights.<id>.iesFile"),
        accepts="profile id → path map; per-light resolved path",
        effect="real photometric distribution; the fixture is built as an omni",
        gotcha="IES energy is scaled differently (×0.3, not base_e) — do not re-tune against base_e.",
        engine_ref="_apply_ies_profile(); _ies_path_for_light()",
    ),
)


# ======================================================================
# GEOMETRY AND VISIBILITY
# ======================================================================
GEOMETRY_RULES: Tuple[Rule, ...] = (
    Rule(
        key="lines.<line>.properties.height",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=("layers.<layer>.lines.<line>.properties.height",),
        accepts="{length: n} in source units, or a bare number",
        effect="wall height; default 240 source units",
        engine_ref="_build_layer_geometry() L2866-2873",
    ),
    Rule(
        key="lines.<line>.properties.thickness",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=("layers.<layer>.lines.<line>.properties.thickness",),
        accepts="{length: n} in source units, or a bare number (default 20)",
        effect="wall depth, and the offset of the inner/outer overlay panels",
        engine_ref="_build_layer_geometry() L2844-2878",
    ),
    Rule(
        key="lines.<line>.properties.alignment",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=("layers.<layer>.lines.<line>.properties.alignment",),
        accepts='"center" (default) | "left" | "right"',
        effect="shifts the wall centreline by half the thickness so it matches the 2D editor",
        engine_ref="_build_layer_geometry() L2839-2858",
    ),
    Rule(
        key="lines.<line>.properties.customHeight",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=("layers.<layer>.lines.<line>.properties.customHeight",),
        accepts="{left, right, center, centerX}",
        effect="peaked or sloped wall profile instead of a rectangle",
        engine_ref="_build_layer_geometry() L2890-2910",
    ),
    Rule(
        key="visible / isvisible / isVisible",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=(
            "layers.<layer>.lines.<line>.visible",
            "…areas.<area>.floor_properties.isvisible",
            "…areas.<area>.ceiling_properties.isvisible",
        ),
        accepts="boolean",
        effect="omits the wall / floor slab / ceiling slab from the build entirely",
        gotcha=(
            "Ceilings differ: with showAllFloors=false a ceiling is hidden by "
            "DEFAULT and kept only when isvisible=true, and it is hidden again if "
            "the camera sits above it. Floors have no such rule."
        ),
        engine_ref="_build_layer_geometry() L3548-3554, L3730-3775",
    ),
    Rule(
        key="floor_properties.thickness / ceiling_properties.thickness",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=(
            "…areas.<area>.floor_properties.thickness",
            "…areas.<area>.ceiling_properties.thickness",
        ),
        accepts="number in source units",
        effect="slab depth; floor default 0.1 m, ceiling default 0.1 m with a 0.05 m floor",
        engine_ref="_build_layer_geometry() L3559-3562, L3778-3783",
    ),
    Rule(
        key="areas.<area>.properties.height",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=("layers.<layer>.areas.<area>.properties.height",),
        accepts="{length: n} or number, in source units",
        effect="per-room ceiling elevation, capped at the tallest wall",
        gotcha=(
            "Values below 1.5 m are IGNORED on purpose — curbs, outdoor half-walls "
            "and unit typos used to collapse the ceiling onto the floor."
        ),
        engine_ref="_build_layer_geometry() L3746-3763",
    ),
    Rule(
        key="floor_properties.paving",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=("…areas.<area>.floor_properties.paving",),
        accepts='{pattern: "none"|…, gapColor: "#rrggbb", gapColorMode: "material"|…}',
        effect=(
            "when pattern is not 'none', the base slab becomes flat grout colour and "
            "per-tile slabs carrying the real material are built 2 mm above it"
        ),
        gotcha=(
            "With paving on, floor_properties.material is applied to the TILES, not "
            "the slab. Setting only the slab colour changes the grout, not the floor."
        ),
        engine_ref="_build_layer_geometry() L3572-3601, _build_paving_overlay()",
    ),
    Rule(
        key="customShapes",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=(
            "…floor_properties.customShapes[]",
            "…ceiling_properties.customShapes[]",
        ),
        accepts="[{points:[{x,y}], thickness, material}]",
        effect="inlaid floor panels 5 mm above the slab; ceiling panels hanging below it",
        gotcha=(
            "Floor shapes are STANDALONE scene nodes, deliberately not children of "
            "floor_csg — parenting them there made CSG booleans carve black pits "
            "into the floor."
        ),
        engine_ref="_build_layer_geometry() L3673-3728, L3815-3867",
    ),
    Rule(
        key="holes",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=("layers.<layer>.holes.<hole>", "layers.<layer>.lines.<line>.holes[]"),
        accepts="{width, height, altitude, offset, type, asset_urls}",
        effect="subtracts a door/window opening from the wall and both overlay panels, then loads the GLB",
        gotcha="offset > 1.0 is read as a distance in source units; ≤ 1.0 as a 0–1 ratio along the wall.",
        engine_ref="_build_layer_geometry() L3069-3157",
    ),
    Rule(
        key="structures",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=("layers.<layer>.structures.<id>",),
        accepts='{structure_type: "partitionwall"|"squarecolumn"|"staircase"|…, width, depth, height, …}',
        effect="procedural CSG geometry — columns, beams, ramps, stairs, false ceilings, sunshades",
        engine_ref="build_structures() L3970-4073",
    ),
    Rule(
        key="items.<item>",
        category=CAT_GEOMETRY,
        status=ST_REFERENCE,
        paths=("layers.<layer>.items.<item>",),
        accepts="{x, y, altitude, rotation, width, depth, height, flipX, flipZ, asset_urls, materials}",
        effect="places a downloaded GLB; materials overrides its surfaces",
        gotcha=(
            "Only items that already declare a `materials` dict are editable by the "
            "prompt pass — a slot name invented for an unopened GLB would never "
            "match a real surface, i.e. a silent no-op."
        ),
        engine_ref="_apply_item_materials() L5826",
    ),
)


# ======================================================================
# CAMERA AND RENDER SETTINGS
# ======================================================================
CAMERA_RULES: Tuple[Rule, ...] = (
    Rule(
        key="threejs_camera",
        category=CAT_CAMERA,
        status=ST_REFERENCE,
        paths=("<root>.threejs_camera",),
        accepts="{position:{x,y,z}, target:{x,y,z}, fov, …}",
        effect="camera pose; also drives scene culling and the sun-through-windows aim",
        gotcha=(
            "Moving the camera changes which rooms survive SceneOptimizer.cull_scene(), "
            "so it is not a purely cosmetic edit."
        ),
    ),
    Rule(
        key="fov",
        category=CAT_CAMERA,
        status=ST_REFERENCE,
        paths=("<root>.fov",),
        accepts="degrees (default 45.0)",
        effect="vertical field of view",
    ),
    Rule(
        key="aspect_ratio",
        category=CAT_CAMERA,
        status=ST_REFERENCE,
        paths=("<root>.aspect_ratio",),
        accepts='"16:9", "4:3", a float, …',
        effect="output frame shape; combined with render_quality to pick the pixel size",
        engine_ref="_calculate_image_render_size() in main.py",
    ),
    Rule(
        key="render_quality",
        category=CAT_CAMERA,
        status=ST_REFERENCE,
        paths=("<root>.render_quality",),
        accepts='"12K" | "8K" | "6K" | "4K" | "2K" | "QUAD HD" | "1080P" | "FHD" | "HD"',
        effect="output resolution via QUALITY_RESOLUTION_MAP",
        gotcha="Drives render cost directly. 4K = 3840×2160.",
        engine_ref="QUALITY_RESOLUTION_MAP in main.py L5888",
    ),
    Rule(
        key="environment_url / hdri_filename",
        category=CAT_CAMERA,
        status=ST_REFERENCE,
        paths=("<root>.environment_url", "<root>.hdri_filename"),
        accepts="URL to an .exr, or a filename",
        effect="PanoramaSkyMaterial for the sky; falls back to a procedural sky when absent",
        engine_ref="setup_lighting() L572-598",
    ),
    Rule(
        key="render_mode",
        category=CAT_CAMERA,
        status=ST_REFERENCE,
        paths=("<root>.render_mode", "layers.<layer>.render_mode"),
        accepts='"INTERIOR" (default) | "EXTERIOR"',
        effect=(
            "INTERIOR puts the inner material on the structural wall and adds both "
            "overlay panels; EXTERIOR uses the outer material on both sides and "
            "builds no panels"
        ),
        engine_ref="_build_layer_geometry() L2969-2990",
    ),
    Rule(
        key="showAllFloors / selectedLayer",
        category=CAT_CAMERA,
        status=ST_REFERENCE,
        paths=("floor_plan_data.showAllFloors", "floor_plan_data.selectedLayer"),
        accepts="boolean; layer id",
        effect="which layers are built, and each layer's altitude offset",
        gotcha="showAllFloors=false also flips the ceiling default to hidden.",
    ),
    Rule(
        key="unit",
        category=CAT_CAMERA,
        status=ST_REFERENCE,
        paths=("floor_plan_data.unit",),
        accepts='"in", "cm", "mm", …',
        effect="sets _scene_unit_scale, the metres-per-source-unit factor used everywhere",
        gotcha=(
            "THE conversion point. Model-facing values are metres; stored geometry "
            "and light positions are source units. Mixing them is the single most "
            "common cause of objects landing outside the room."
        ),
        engine_ref="_resolve_scene_unit_scale()",
    ),
)


ALL_RULES: Tuple[Rule, ...] = (
    MATERIAL_RULES + LIGHTING_RULES + GEOMETRY_RULES + CAMERA_RULES
)

RULES_BY_CATEGORY: Dict[str, Tuple[Rule, ...]] = {
    CAT_MATERIAL: MATERIAL_RULES,
    CAT_LIGHTING: LIGHTING_RULES,
    CAT_GEOMETRY: GEOMETRY_RULES,
    CAT_CAMERA: CAMERA_RULES,
}


# ======================================================================
# Queries
# ======================================================================
def rules_for(
    categories: Optional[Sequence[str]] = None,
    statuses: Optional[Sequence[str]] = None,
    surfaces: Optional[Sequence[str]] = None,
) -> List[Rule]:
    """Filter the rule set."""
    out: List[Rule] = []
    for rule in ALL_RULES:
        if categories and rule.category not in categories:
            continue
        if statuses and rule.status not in statuses:
            continue
        if surfaces and rule.surfaces and not set(surfaces) & set(rule.surfaces):
            continue
        out.append(rule)
    return out


def prompt_fragment(
    categories: Sequence[str] = (CAT_MATERIAL,),
    statuses: Sequence[str] = (ST_PROMPT,),
    surfaces: Optional[Sequence[str]] = None,
) -> str:
    """
    The compact form injected into a model's system prompt.

    Deliberately only the writable rules by default. Telling a model about keys
    it is not allowed to touch invites it to touch them, and every extra line
    here competes with the scene data for the model's attention - which is the
    thing that actually determines whether the edit is correct.
    """
    selected = rules_for(categories, statuses, surfaces)
    if not selected:
        return ""
    lines = ["WHAT EACH FIELD DOES IN THE RENDER:"]
    lines.extend(rule.one_line() for rule in selected)
    return "\n".join(lines)


# ======================================================================
# Documentation
# ======================================================================
def to_markdown() -> str:
    """Generate RENDER_RULES.md from the rule data."""
    out: List[str] = []
    out.append("# JSON → render conversion rules\n")
    out.append(
        "**Generated from `zlendo_llm/render_rules.py` — do not edit by hand.**\n"
        "Regenerate with `python zlendo_llm/render_rules.py --markdown -o RENDER_RULES.md`.\n"
    )
    out.append(
        "Every effect below was read out of `godot_project/image_glb_creation.gd`. "
        "Where the scene JSON and the engine disagree, the rule says so — those "
        "mismatches are invisible until a render looks wrong.\n"
    )

    out.append("## Who writes what\n")
    out.append("| Marker | Meaning |")
    out.append("|---|---|")
    out.append("| **prompt pass** | `payload.prompt` can change this today |")
    out.append("| **lighting pass** | the LLM lighting audit changes this |")
    out.append("| *reference only* | documented engine behaviour; no pass writes it yet |")
    out.append("")

    counts = {st: 0 for st in STATUS_LABEL}
    for r in ALL_RULES:
        counts[r.status] += 1
    out.append(
        f"{len(ALL_RULES)} rules: {counts[ST_PROMPT]} writable by the prompt pass, "
        f"{counts[ST_LIGHTING]} by the lighting pass, "
        f"{counts[ST_REFERENCE]} reference only.\n"
    )

    for cat in CATEGORY_ORDER:
        rules = RULES_BY_CATEGORY[cat]
        if not rules:
            continue
        out.append(f"## {CATEGORY_TITLE[cat]}\n")
        for rule in rules:
            label = STATUS_LABEL[rule.status]
            marker = f"**{label}**" if rule.status != ST_REFERENCE else f"*{label}*"
            out.append(f"### `{rule.key}` — {marker}\n")
            out.append(f"**Accepts:** {rule.accepts}\n")
            out.append(f"**Effect:** {rule.effect}\n")
            if rule.gotcha:
                out.append(f"> ⚠️ {rule.gotcha}\n")
            if rule.example:
                out.append(f"```json\n{rule.example}\n```\n")
            out.append("**Where:**\n")
            for p in rule.paths:
                out.append(f"- `{p}`")
            out.append("")
            if rule.surfaces:
                out.append(f"**Surfaces:** {', '.join(rule.surfaces)}\n")
            if rule.engine_ref:
                out.append(f"<sub>Engine: {rule.engine_ref}</sub>\n")

    out.append("## The order things happen in\n")
    out.append("```")
    out.append("payload.floor_plan_data")
    out.append("  1a  apply_prompt_edits()          material edits from payload.prompt")
    out.append("  1   _download_and_localize_assets()  fetches every texture/GLB referenced")
    out.append("      _localize_ies_profiles()")
    out.append("      _repair_open_wall_loops()")
    out.append("  2.5 SceneOptimizer.cull_scene()   drops rooms/items the camera cannot see")
    out.append("  2.6 ai_upgrade_lighting()         lighting profile, mood, level, lights")
    out.append("  3   write <job>_godot_input.json")
    out.append("  4   Godot: build_scene() -> setup_lighting() -> path trace")
    out.append("```\n")
    out.append(
        "Material edits run **before** localization so a newly chosen texture is "
        "downloaded like any other. Lighting edits run **after** culling so the "
        "audit sees the geometry that will actually be rendered.\n"
    )

    out.append("## Units\n")
    out.append(
        "- Stored geometry, `customLights.x/y/altitude`, `repeatX/repeatY` and all "
        "`{length: n}` values are in **source units**.\n"
        "- `_scene_unit_scale` (from `floor_plan_data.unit`) converts source units "
        "to **metres**.\n"
        "- Anything shown to an LLM is in **metres**; `validate.py` converts back "
        "on the way in. Mixing the two is the most common cause of an object "
        "landing outside the room.\n"
    )
    return "\n".join(out)


# ======================================================================
# Self-check against a real scene
# ======================================================================
def _walk_keys(node: Any, found: set, depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(str(k))
            _walk_keys(v, found, depth + 1)
    elif isinstance(node, list):
        for v in node[:50]:
            _walk_keys(v, found, depth + 1)


def check_against_scene(scene: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    Verify documented keys still appear in a real scene file.

    Returns (present, missing). A missing key is NOT automatically a bug - a
    scene with no paving legitimately has no `paving` key - so this is a
    drift signal to read, not a hard gate. What it reliably catches is a key
    RENAMED in the engine, because then it disappears from every scene at once.
    """
    keys: set = set()
    _walk_keys(scene, keys)

    present: List[str] = []
    missing: List[str] = []
    for rule in ALL_RULES:
        # Take the last path segment that is not a placeholder.
        names = []
        for part in rule.key.replace(" / ", "/").split("/"):
            leaf = part.strip().split(".")[-1].strip()
            if leaf and not leaf.startswith("(") and not leaf.startswith("<"):
                names.append(leaf)
        if not names:
            continue
        if any(n in keys for n in names):
            present.append(rule.key)
        else:
            missing.append(rule.key)
    return present, missing


# ======================================================================
# CLI
# ======================================================================
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="JSON -> render conversion rules: generate docs or check drift."
    )
    ap.add_argument("--markdown", action="store_true", help="Print the full reference.")
    ap.add_argument("-o", "--out", help="Write the markdown to this file.")
    ap.add_argument("--fragment", action="store_true",
                    help="Print the compact system-prompt fragment.")
    ap.add_argument("--category", action="append",
                    choices=list(CATEGORY_ORDER),
                    help="Restrict --fragment to a category (repeatable).")
    ap.add_argument("--all-statuses", action="store_true",
                    help="Include reference-only rules in --fragment.")
    ap.add_argument("--check", metavar="SCENE.json",
                    help="Verify documented keys still exist in a real scene.")
    args = ap.parse_args(argv)

    if args.check:
        with open(args.check, encoding="utf-8") as fh:
            data = json.load(fh)
        plan = data.get("floor_plan_data", data) if isinstance(data, dict) else data
        if isinstance(plan, str):
            plan = json.loads(plan)
        scene = data if isinstance(data, dict) else {}
        merged = {"_root": scene, "_plan": plan}
        present, missing = check_against_scene(merged)
        print(f"{len(present)}/{len(present) + len(missing)} documented keys present "
              f"in {args.check}")
        if missing:
            print("\nNot present in this scene (expected for optional features,\n"
                  "but a renamed engine key would show up here too):")
            for k in missing:
                print(f"  - {k}")
        return 0

    if args.fragment:
        cats = args.category or [CAT_MATERIAL]
        statuses = None if args.all_statuses else (ST_PROMPT,)
        print(prompt_fragment(cats, statuses or (ST_PROMPT, ST_LIGHTING, ST_REFERENCE)))
        return 0

    text = to_markdown()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {args.out} ({len(ALL_RULES)} rules)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

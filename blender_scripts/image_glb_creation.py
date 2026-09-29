"""
image_glb_creation.py — Blender (bpy) port of godot_project/image_glb_creation.gd.

Builds the whole scene from the same *_godot_input.json that main.py writes for Godot:
    build_scene(data)
      ├─ setup_lighting()        sun (DirectionalLight3D) + HDRI world (PanoramaSky + ambient)
      ├─ build_architecture()    walls (+alignment, sloped tops, per-face materials),
      │                          door/window holes + their GLBs, wall custom shapes,
      │                          floors / ceilings / external areas (+ custom shapes)
      ├─ build_structures()      structure.py
      ├─ load_assets()           furniture GLBs: same placement / auto-scale / flip rules
      ├─ build_custom_lights()   sceneLights / customLights (point, spot, area_rect, IES)
      └─ fixture lights          _add_light_fixture_effect (emissive + analytic light)

Coordinate handling
-------------------
All placement maths below is the GDScript maths, in Godot/Three.js Y-up space, so it can
be diffed against the .gd file. Values are converted to Blender Z-up ONLY through coords.py:
    point      (x, y, z)_three  ->  (x, -z, y)_blender
    transform  M_godot          ->  C @ M_godot @ C^-1   (GLB content)
                                    C @ M_godot          (cameras / lights)

Godot-only rasteriser workarounds (reflection probes, VoxelGI/SDFGI/SSIL, window portal
fill lights, DLSS, shadow atlas sizes) are intentionally NOT ported: Cycles path traces
GI, reflections and shadows natively.
"""

import os
import json
import math

import bpy
from mathutils import Vector, Matrix

from coords import (C3, C4, C4_INV, three_to_blender, blender_to_three, godot_trs,
                    godot_matrix_to_blender_world, godot_matrix_to_blender_object, look_at_matrix)
from blender_utils import (log, get_collection, link_object, extrude_polygon_godot, box_godot,
                           mesh_object, apply_boolean_difference, planar_uvs, polygon_signed_area,
                           parse_html_color, srgb_to_linear, kelvin_to_rgb)
from materials import (MaterialFactory, arch_uv_scale, custom_shape_uv_scale, apply_item_materials,
                       convert_glb_alpha_glass, set_emission, material_is_emissive, _item_uv_scale)
from structure import StructureBuilder

ENABLE_DUPLICATION = True

# ---------------------------------------------------------------------------
# Godot energy -> Cycles units. Godot's light_energy is unitless; Cycles uses
# Watts (point/spot/area) and W/m^2 (sun). These are the calibration knobs —
# override any of them per job with a "_blender" dict in the input JSON,
# e.g.  "_blender": {"SUN_STRENGTH_PER_GODOT": 5.0}
# ---------------------------------------------------------------------------
CALIBRATION = {
    "SUN_STRENGTH_PER_GODOT": 4.0,      # sun W/m^2 per Godot directional energy unit
    "SUN_ANGLE_DEG": 0.8,               # Godot light_angular_distance (day)
    "WORLD_LIGHT_PER_GODOT_AMBIENT": 0.1,  # HDRI lighting strength per Godot ambient_light_energy
    "WORLD_BACKGROUND_STRENGTH": 1.0,   # HDRI as seen directly by the camera (Godot bg energy 1.0)
    "ENV_ROTATION_Z_DEG": -90.0,        # aligns Blender equirect mapping with Godot PanoramaSky (-Z = centre)
    "LIGHT_W_PER_GODOT": 60.0,          # point / spot Watts per Godot light_energy
    "AREA_W_PER_GODOT": 60.0,           # area_rect Watts per Godot light_energy
    "IES_W_PER_GODOT": 300.0,           # IES fixture Watts per Godot light_energy
    "FIXTURE_W_PER_GODOT": 60.0,        # item-fixture analytic light Watts per Godot light_energy
    "FIXTURE_EMISSION_SCALE": 1.0,      # Emission Strength per Godot emission_energy
    "POINT_RADIUS": 0.05,               # soft-shadow radius for analytic point lights (m)
}


class SceneBuilder:
    def __init__(self, data, asset_search_dirs=None, image_render_only=True):
        self.data = data
        self.cal = dict(CALIBRATION)
        if isinstance(data.get("_blender"), dict):
            self.cal.update({k: v for k, v in data["_blender"].items() if k in CALIBRATION})
        self.image_render_only = image_render_only
        self.materials = MaterialFactory(asset_search_dirs)
        self.asset_search_dirs = [d for d in (asset_search_dirs or []) if d]
        self.structures = StructureBuilder(self)
        self.tracked_assets = []
        self.glb_cache = {}

        # collections
        self.col_root = get_collection("ZlendoScene")
        self.col_arch = get_collection("Architecture", self.col_root)
        self.col_items = get_collection("Items", self.col_root)
        self.col_lights = get_collection("Lights", self.col_root)
        self.col_cutters = get_collection("_Cutters", self.col_root, hide_render=True)
        self.col_protos = get_collection("_GLBPrototypes", self.col_root, hide_render=True)
        vl = bpy.context.view_layer.layer_collection
        for lc in vl.children:
            if lc.name == "ZlendoScene":
                for c in lc.children:
                    if c.name in ("_GLBPrototypes", "_Cutters"):
                        c.exclude = False
                        c.hide_viewport = True

        # parse floor plan (floor_plan_data may be a JSON string)
        geom = data
        fp = data.get("floor_plan_data")
        if isinstance(fp, str):
            try:
                geom = json.loads(fp)
                log("Parsed floor_plan_data from string.")
            except Exception as e:
                log("Error parsing floor_plan_data string:", e)
        elif isinstance(fp, dict):
            geom = fp
        self.geom = geom if isinstance(geom, dict) else {}

        version = str(self.geom.get("version", data.get("version", ""))).strip()
        self.unit_scale = 0.001 if version == "2.0.0" else 0.01
        self.rotation_is_radians = version == "2.0.0"
        if version == "2.0.0":
            self.plan_rotation = 0.0
        else:
            self.plan_rotation = -math.radians(float(self.geom.get("directionangle", data.get("directionangle", 0.0)) or 0.0))
        self.plan_pivot = self._resolve_plan_pivot()

        self.lighting_profile = self._resolve_lighting_profile(data)
        self.day_render = self.lighting_profile != "night"
        self.color_mood = "day" if self.day_render else self._resolve_color_mood(data)
        self.illumination_scale = self._resolve_illumination_scale(data)
        self.night_light_on = bool(data.get("nightLightOn", (data.get("_lightingSettings") or {}).get("nightLightOn", False)))

        cam = self.extract_camera_pose(data)
        self.camera_pos_g, self.camera_target_g = cam["position"], cam["target"]
        self.camera_height_m = float(self.camera_pos_g.y)
        self.ceiling_fixture_count = max(self._count_fixtures(ceiling_only=True), 1)

        log("Using scene unit scale:", self.unit_scale, "m per source unit")
        log("Using radians for plan rotations:", self.rotation_is_radians)
        log("Using plan rotation (radians):", self.plan_rotation, " pivot:", tuple(self.plan_pivot))
        log("Lighting profile:", self.lighting_profile, " mood:", self.color_mood, " illum scale:", self.illumination_scale)

    # =====================================================================
    # Plan-space helpers (_plan_point_2d, _plan_point_3d, ...)
    # =====================================================================
    def _resolve_plan_pivot(self):
        src = self.geom
        bounds = [math.inf, math.inf, -math.inf, -math.inf]
        found = False

        def acc(v):
            nonlocal found
            if isinstance(v, dict) and "x" in v and "y" in v:
                bounds[0] = min(bounds[0], float(v["x"])); bounds[1] = min(bounds[1], float(v["y"]))
                bounds[2] = max(bounds[2], float(v["x"])); bounds[3] = max(bounds[3], float(v["y"]))
                found = True

        layers = src.get("layers") if isinstance(src.get("layers"), dict) else {}
        sel = str(src.get("selectedLayer", "")).strip()
        ids = [sel] if sel and sel in layers else list(layers.keys())
        for lid in ids:
            layer = layers.get(lid) or {}
            verts = layer.get("vertices") or src.get("vertices") or {}
            for area in (layer.get("areas") or {}).values():
                for vid in area.get("vertices", []):
                    acc(verts.get(str(vid)))
            if found:
                break
        if not found:
            for v in (src.get("vertices") or {}).values():
                acc(v)
        if not found:
            return Vector((0.0, 0.0))
        return Vector(((bounds[0] + bounds[2]) * 0.5, (bounds[1] + bounds[3]) * 0.5))

    def plan_point_2d(self, x, y):
        p = Vector((float(x), float(y))) - self.plan_pivot
        if abs(self.plan_rotation) > 1e-6:
            c, s = math.cos(self.plan_rotation), math.sin(self.plan_rotation)
            p = Vector((p.x * c - p.y * s, p.x * s + p.y * c))
        return p + self.plan_pivot

    def plan_point_3d(self, x, y, world_y=0.0):
        p = self.plan_point_2d(x, y)
        return Vector((p.x * self.unit_scale, world_y, p.y * self.unit_scale))

    def transform_plan_point_meters(self, point):
        if abs(self.plan_rotation) < 1e-6:
            return Vector(point)
        s = max(self.unit_scale, 1e-6)
        t = self.plan_point_2d(point[0] / s, point[2] / s)
        return Vector((t.x * s, point[1], t.y * s))

    def rotation_to_radians(self, raw):
        return float(raw) if self.rotation_is_radians else math.radians(float(raw))

    def world_yaw_from_source_rotation(self, raw):
        return -self.rotation_to_radians(raw)

    @staticmethod
    def get_dimension_value(v, default=100.0):
        if isinstance(v, list) and v:
            return float(v[0])
        if isinstance(v, dict):
            return float(v.get("length", default))
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    def structure_uv_scale(self, mat_data):
        # Godot structures use uv1_triplanar with uv1_scale = repeat -> per-metre density ~ 1/tile
        tu = float((mat_data or {}).get("repeatX", 0) or 0)
        tv = float((mat_data or {}).get("repeatY", 0) or 0)
        if tu > 0 and tv > 0:
            return (1.0 / (tu * self.unit_scale), 1.0 / (tv * self.unit_scale))
        return (1.0, 1.0)

    # =====================================================================
    # Lighting profile resolution (_resolve_lighting_profile etc.)
    # =====================================================================
    @staticmethod
    def _resolve_lighting_profile(data):
        keys = ["lighting_profile", "time_of_day", "timeOfDay", "render_time", "environment_preset"]
        sources = [data]
        if isinstance(data.get("_lightingSettings"), dict):
            sources.append(data["_lightingSettings"])
        for src in sources:
            for k in keys:
                if k in src:
                    v = str(src[k]).lower().strip()
                    if "sunset" in v or "dusk" in v or "evening" in v:
                        return "sunset"
                    if "night" in v:
                        return "night"
                    if "day" in v or "morning" in v or "afternoon" in v:
                        return "day"
        if data.get("sunset_render"):
            return "sunset"
        if data.get("night_render"):
            return "night"
        if "day_render" in data:
            return "day" if data["day_render"] else "night"
        return "day"

    @staticmethod
    def _resolve_color_mood(data):
        c = str(data.get("colorMood", data.get("color_mood", (data.get("_lightingSettings") or {}).get("colorMood", "")))).lower().strip()
        for m in ("warm", "soft", "neutral", "cool", "day"):
            if m in c:
                return m
        return "neutral" if "nuetral" in c else "day"

    @staticmethod
    def _resolve_illumination_scale(data):
        lvl = data.get("illuminationLevel", data.get("illumination_level"))
        if lvl is None and isinstance(data.get("_lightingSettings"), dict):
            ls = data["_lightingSettings"]
            lvl = ls.get("illuminationLevel", ls.get("illumination_level"))
        lvl = 5 if lvl is None else int(float(lvl))
        return float(min(max(lvl, 1), 5)) * 0.10

    def mood_color(self, role):
        # _get_color_mood_light_color: day renders are neutral; night moods tint lights.
        if self.day_render:
            return (1.0, 1.0, 1.0)
        return {"warm": (1.0, 0.82, 0.62), "soft": (1.0, 0.92, 0.82), "cool": (0.82, 0.9, 1.0)}.get(self.color_mood, (1.0, 0.95, 0.88))

    # =====================================================================
    # Camera pose (_extract_camera_pose) — Godot space
    # =====================================================================
    def extract_camera_pose(self, data):
        pos, tgt = Vector((0, 1.6, 6)), Vector((0, 0, 0))
        found = found_t = False
        transform = str(self.geom.get("version", "")).strip() != "2.0.0"
        tc = data.get("threejs_camera")
        if isinstance(tc, dict):
            if isinstance(tc.get("position"), dict):
                p = tc["position"]; pos = Vector((float(p.get("x", 0)), float(p.get("y", 0)), float(p.get("z", 0)))); found = True
            if isinstance(tc.get("target"), dict):
                t = tc["target"]; tgt = Vector((float(t.get("x", 0)), float(t.get("y", 0)), float(t.get("z", 0)))); found_t = True
        elif isinstance(data.get("blender_camera"), dict):
            loc = data["blender_camera"].get("location")
            if isinstance(loc, list) and len(loc) >= 3:
                pos = blender_to_three(loc); found = True
            bt = data.get("blender_target") or {}
            if isinstance(bt.get("location"), list) and len(bt["location"]) >= 3:
                tgt = blender_to_three(bt["location"]); found_t = True
        if found and transform:
            pos = self.transform_plan_point_meters(pos)
        if found_t and transform:
            tgt = self.transform_plan_point_meters(tgt)
        if (tgt - pos).length < 0.001:
            tgt = pos + Vector((0, 0, -1))
        return {"position": pos, "target": tgt}

    # =====================================================================
    # MAIN ENTRY
    # =====================================================================
    def build_scene(self):
        self.setup_lighting()
        self.build_architecture()
        self.build_structures()
        self.load_assets()
        self.build_custom_lights()
        log(f"Scene built: {len(self.col_arch.objects)} architecture objects, "
            f"{len(self.col_items.all_objects)} item objects, {len(self.col_lights.objects)} lights")

    # =====================================================================
    # LIGHTING (setup_lighting)
    # =====================================================================
    def setup_lighting(self):
        data = self.data
        cal = self.cal
        auto_ambient = min(max(float(data.get("_automationAmbientBoost", 1.0)), 0.5), 2.5)
        auto_sun = min(max(float(data.get("_automationSunMultiplier", 1.0)), 0.5), 2.5)
        self.auto_exposure_scale = min(max(float(data.get("_tonemapExposureScale", 1.0)), 0.6), 1.6)

        prof = {
            "night": dict(dir_energy=0.0, dir_color=(0.55, 0.62, 0.90), ambient_color=(0.29, 0.21, 0.16), ambient_energy=0.18, exposure=0.42),
            "sunset": dict(dir_energy=1.65, dir_color=(1.0, 0.63, 0.38), ambient_color=(0.62, 0.50, 0.42), ambient_energy=0.28, exposure=0.78 * 1.30),
        }.get(self.lighting_profile, dict(dir_energy=3.5, dir_color=(1.0, 0.92, 0.78), ambient_color=(1.0, 0.835, 0.5), ambient_energy=0.35, exposure=0.90))

        # ---- Sun --------------------------------------------------------
        if self.day_render:
            dir_energy, dir_color = 1.0 * auto_sun, (1.0, 1.0, 1.0)   # render.gd: day ignores JSON energy/colour
        else:
            dir_energy, dir_color = prof["dir_energy"] * self.illumination_scale, prof["dir_color"]
        sun_data = bpy.data.lights.new("Sun", "SUN")
        sun_data.energy = dir_energy * cal["SUN_STRENGTH_PER_GODOT"]
        sun_data.color = dir_color
        sun_data.angle = math.radians(cal["SUN_ANGLE_DEG"] if self.day_render else 0.5)
        sun = bpy.data.objects.new("Sun", sun_data)
        self.col_lights.objects.link(sun)
        self._orient_sun(sun)
        if not self.day_render:
            sun.hide_render = True
        log(f"[Sun] strength={sun_data.energy:.2f} W/m2 (godot energy {dir_energy:.2f})")

        # ---- World / HDRI (PanoramaSkyMaterial + ambient) ---------------
        if self.day_render:
            illum_factor = min(max(self.illumination_scale / 0.5, 0.35), 1.0)
            ambient_energy = 10.0 * auto_ambient * illum_factor
            exposure = 1.3
            if auto_ambient > 1.0:
                exposure *= 1.0 + (auto_ambient - 1.0) * 0.25
            exposure *= 1.18  # AgX contrast term folded into exposure (tonemap_agx_contrast)
        else:
            ambient_energy = prof["ambient_energy"] * self.illumination_scale * auto_ambient * (1.2 if self.color_mood == "warm" else 1.0)
            exposure = prof["exposure"] * (1.08 if self.color_mood == "warm" else 1.0)
        exposure *= self.auto_exposure_scale
        self.godot_tonemap_exposure = exposure
        self._setup_world(ambient_energy, prof)
        log(f"[Lighting] ambient(godot)={ambient_energy:.2f} tonemap_exposure(godot)={exposure:.3f}")

    def _orient_sun(self, sun):
        """_apply_directional_light_pose_from_json -> Blender. Position/target are Three.js space."""
        data = self.data
        ls = data.get("_lightingSettings") if isinstance(data.get("_lightingSettings"), dict) else {}
        ld = data.get("directional_light") if isinstance(data.get("directional_light"), dict) else {}

        def v3(d):
            return Vector((float(d.get("x", 0)), float(d.get("y", 0)), float(d.get("z", 0))))
        pos = v3(ld["position"]) if isinstance(ld.get("position"), dict) else (v3(ls["directionalPosition"]) if isinstance(ls.get("directionalPosition"), dict) else None)
        tgt = v3(ld["target"]) if isinstance(ld.get("target"), dict) else (v3(ls["directionalTarget"]) if isinstance(ls.get("directionalTarget"), dict) else None)
        if pos is not None and tgt is not None and (pos - tgt).length > 1e-3:
            pos_g, tgt_g = self.transform_plan_point_meters(pos), self.transform_plan_point_meters(tgt)
            pb, tb = three_to_blender(pos_g), three_to_blender(tgt_g)
            sun.matrix_world = Matrix.Translation(pb) @ look_at_matrix(pb, tb).to_4x4()
            log(f"[Sun] from JSON: three pos={tuple(round(x,2) for x in pos)} -> blender {tuple(round(x,2) for x in pb)}")
            return
        # fallback: azimuth / altitude (degrees) from _lightingSettings
        az = math.radians(float(ls.get("sunAzimuth", 135.0)))
        alt = math.radians(float(ls.get("sunAltitude", 45.0)))
        # direction FROM the scene TO the sun in Three.js space (y-up), azimuth measured on XZ
        to_sun_g = Vector((math.cos(alt) * math.sin(az), math.sin(alt), math.cos(alt) * math.cos(az)))
        to_sun_b = C3 @ to_sun_g
        sun.matrix_world = look_at_matrix(Vector((0, 0, 0)), -to_sun_b).to_4x4()
        log("[Sun] oriented from sunAzimuth/sunAltitude")

    def _setup_world(self, ambient_energy, prof):
        cal = self.cal
        world = bpy.data.worlds.new("ZlendoWorld")
        bpy.context.scene.world = world
        try:
            world.use_nodes = True
        except Exception:
            pass
        nt = world.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputWorld")
        light_path = nt.nodes.new("ShaderNodeLightPath")
        bg_cam = nt.nodes.new("ShaderNodeBackground")
        bg_light = nt.nodes.new("ShaderNodeBackground")
        mix = nt.nodes.new("ShaderNodeMixShader")

        exr = str(self.data.get("environment_url") or "")
        env_img = None
        if exr and os.path.isfile(exr):
            try:
                env_img = bpy.data.images.load(exr, check_existing=True)
            except Exception as e:
                log("Warning: failed to load EXR:", exr, e)
        else:
            # Godot fallback: res://day.exr next to the scripts
            here = os.path.dirname(os.path.abspath(__file__))
            cand = os.path.join(here, "day.exr" if self.lighting_profile != "night" else "night.exr")
            if os.path.isfile(cand):
                env_img = bpy.data.images.load(cand, check_existing=True)
            else:
                log("Warning: EXR sky file not found:", exr or cand)

        if env_img is not None:
            tc = nt.nodes.new("ShaderNodeTexCoord")
            mp = nt.nodes.new("ShaderNodeMapping")
            mp.inputs["Rotation"].default_value[2] = math.radians(cal["ENV_ROTATION_Z_DEG"])
            env = nt.nodes.new("ShaderNodeTexEnvironment")
            env.image = env_img
            nt.links.new(tc.outputs["Generated"], mp.inputs["Vector"])
            nt.links.new(mp.outputs["Vector"], env.inputs["Vector"])
            nt.links.new(env.outputs["Color"], bg_cam.inputs["Color"])
            nt.links.new(env.outputs["Color"], bg_light.inputs["Color"])
            log("Loaded EXR sky background:", env_img.filepath)
        else:
            sky = (0.25, 0.40, 0.78, 1.0) if self.day_render else (0.01, 0.02, 0.05, 1.0)
            bg_cam.inputs["Color"].default_value = sky
            bg_light.inputs["Color"].default_value = sky
            log("Using fallback procedural sky colour")

        bg_cam.inputs["Strength"].default_value = cal["WORLD_BACKGROUND_STRENGTH"]
        light_strength = ambient_energy * cal["WORLD_LIGHT_PER_GODOT_AMBIENT"]
        if not self.day_render:
            # Godot night: COLOR ambient (mood tinted) instead of sky
            mc = self.mood_color("ambient")
            c = prof["ambient_color"]
            bg_light.inputs["Color"].default_value = (c[0] * mc[0], c[1] * mc[1], c[2] * mc[2], 1.0)
            for l in list(bg_light.inputs["Color"].links):
                nt.links.remove(l)
        bg_light.inputs["Strength"].default_value = light_strength
        # camera rays see the sky at background strength; every other ray gets the ambient strength
        nt.links.new(light_path.outputs["Is Camera Ray"], mix.inputs["Fac"])
        nt.links.new(bg_light.outputs["Background"], mix.inputs[1])
        nt.links.new(bg_cam.outputs["Background"], mix.inputs[2])
        nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
        log(f"[World] lighting strength={light_strength:.3f}, background strength={bg_cam.inputs['Strength'].default_value}")

    # =====================================================================
    # ARCHITECTURE
    # =====================================================================
    def _layer_altitude(self, layer):
        a = layer.get("altitude", 0.0)
        return (float(a.get("length", 0)) if isinstance(a, dict) else float(a or 0)) * self.unit_scale

    def _resolve_camera_layer_id(self):
        layers = self.geom.get("layers") if isinstance(self.geom.get("layers"), dict) else {}

        def span(layer):
            alt = self._layer_altitude(layer)
            h = 3.0
            for line in (layer.get("lines") or {}).values():
                hp = (line.get("properties") or {}).get("height") if isinstance(line, dict) else None
                if hp is None:
                    continue
                wh = (float(hp.get("length", 0)) if isinstance(hp, dict) else float(hp)) * self.unit_scale
                h = max(h, wh)
            return alt, alt + h
        sel = str(self.geom.get("selectedLayer", "")).strip()
        if sel and isinstance(layers.get(sel), dict):
            lo, hi = span(layers[sel])
            if lo <= self.camera_height_m < hi:
                return sel
        for lid, layer in layers.items():
            if isinstance(layer, dict):
                lo, hi = span(layer)
                if lo <= self.camera_height_m < hi:
                    return str(lid)
        return ""

    def _active_layers(self):
        """Layer selection shared by build_architecture / build_structures / load_assets."""
        data = self.geom
        layers = data.get("layers") if isinstance(data.get("layers"), dict) else {}
        show_all = bool(data.get("showAllFloors", True))
        selected = str(data.get("selectedLayer", ""))
        keep_all = show_all
        cam_layer = self._resolve_camera_layer_id() if (show_all or selected == "") else ""
        if cam_layer:
            mode = str(layers.get(cam_layer, {}).get("render_mode", "INTERIOR")).upper()
            if mode == "INTERIOR":
                show_all, keep_all, selected = False, False, cam_layer
            else:
                show_all = keep_all = True
        out = []
        for lid, layer in layers.items():
            if not keep_all and selected and str(lid) != selected:
                continue
            alt = self._layer_altitude(layer) if show_all else 0.0
            out.append((str(lid), layer, alt, show_all))
        return out

    def build_architecture(self):
        log("Building Architecture...")
        data = self.geom
        use_showall = bool(data.get("use_showall", True))
        if isinstance(data.get("layers"), dict):
            for lid, layer, alt, show_all in self._active_layers():
                if "lines" in layer and "vertices" in layer:
                    mode = str(layer.get("render_mode", data.get("render_mode", "INTERIOR"))).upper()
                    log("Processing layer:", lid, "render_mode:", mode, "altitude:", alt)
                    self._build_layer_geometry(layer, alt, show_all, mode, use_showall)
        elif "lines" in data and "vertices" in data:
            self._build_layer_geometry(data, 0.0, bool(data.get("showAllFloors", True)),
                                       str(data.get("render_mode", "INTERIOR")).upper(), use_showall)
        else:
            log("Warning: No lines or vertices found in floor plan data.")

    # ---------------------------------------------------------------------
    @staticmethod
    def _is_point_in_polygon(pt, poly):
        inside = False
        n = len(poly)
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > pt[1]) != (yj > pt[1])) and (pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _closest_point_on_segment(p, a, b):
        ab = b - a
        L = ab.length_squared
        if L < 1e-12:
            return a
        t = max(0.0, min(1.0, (p - a).dot(ab) / L))
        return a + ab * t

    def _get_wall_facing_direction(self, line, vertices, areas):
        v1, v2 = str(line["vertices"][0]), str(line["vertices"][1])
        if v1 not in vertices or v2 not in vertices:
            return ""
        a = self.plan_point_2d(float(vertices[v1]["x"]), float(vertices[v1]["y"]))
        b = self.plan_point_2d(float(vertices[v2]["x"]), float(vertices[v2]["y"]))
        d = (b - a)
        if d.length < 1e-9:
            return ""
        d.normalize()
        left_n, right_n = Vector((-d.y, d.x)), Vector((d.y, -d.x))
        mid = (a + b) * 0.5
        off = 0.05 / max(self.unit_scale, 1e-6)
        ls, rs = mid + left_n * off, mid + right_n * off
        snap = 0.05 / max(self.unit_scale, 1e-6)
        best, best_id = "", ""
        for aid, area in areas.items():
            poly = [self.plan_point_2d(float(vertices[str(v)]["x"]), float(vertices[str(v)]["y"]))
                    for v in area.get("vertices", []) if str(v) in vertices]
            if len(poly) < 3:
                continue
            on1 = on2 = False
            for i in range(len(poly)):
                ea, eb = poly[i], poly[(i + 1) % len(poly)]
                if not on1 and (self._closest_point_on_segment(a, ea, eb) - a).length <= snap:
                    on1 = True
                if not on2 and (self._closest_point_on_segment(b, ea, eb) - b).length <= snap:
                    on2 = True
            if not (on1 and on2):
                continue
            li, ri = self._is_point_in_polygon(ls, poly), self._is_point_in_polygon(rs, poly)
            res = "inner-left" if (li and not ri) else ("inner-right" if (ri and not li) else "")
            if not res:
                continue
            if best == "" or str(aid) < best_id:
                best, best_id = res, str(aid)
        return best

    def _resolve_wall_face_material(self, line, face, wall_facing=""):
        lookup = face
        if wall_facing == "inner-left":
            lookup = "outer" if face == "inner" else "inner"
        pk = lookup + "_properties"
        ok = ("outer" if lookup == "inner" else "inner") + "_properties"
        has_face = isinstance(line.get(pk), dict) and isinstance(line[pk].get("material"), dict)
        if not has_face and isinstance(line.get(ok), dict):
            has_face = isinstance(line[ok].get("material"), dict)
        au = line.get("asset_urls")
        if isinstance(au, dict) and isinstance(au.get(lookup), dict):
            blk = au[lookup]
            md = {}
            if blk.get("mapUrl"):
                md["mapUrl"] = blk["mapUrl"]
            elif isinstance(blk.get("texture_urls"), list) and blk["texture_urls"]:
                md["mapUrl"] = blk["texture_urls"][0]
            if blk.get("normalUrl"):
                md["normalUrl"] = blk["normalUrl"]
            if blk.get("roughnessUrl"):
                md["roughnessUrl"] = blk["roughnessUrl"]
            fc = blk.get("fallback_color") or ""
            if str(fc).strip():
                md["color"] = str(fc)
            if float(blk.get("texture_scale_x", 0) or 0) > 0 and float(blk.get("texture_scale_y", 0) or 0) > 0:
                md["scale"] = [float(blk["texture_scale_x"]), float(blk["texture_scale_y"])]
            rx, ry = float(blk.get("repeatX", 0) or 0), float(blk.get("repeatY", 0) or 0)
            if rx > 0 or ry > 0:
                md["repeatX"], md["repeatY"] = rx, ry
            elif isinstance(blk.get("repeat"), list):
                md["repeat"] = blk["repeat"]
            if md:
                has_tex = any(k in md for k in ("mapUrl", "normalUrl", "roughnessUrl"))
                if has_tex or ("color" in md and not has_face):
                    return md
        if isinstance(line.get(pk), dict) and isinstance(line[pk].get("material"), dict):
            return line[pk]["material"]
        if isinstance(line.get(ok), dict) and isinstance(line[ok].get("material"), dict):
            return line[ok]["material"]
        return None

    def _hole_dims(self, hole):
        s = self.unit_scale
        gd = self.get_dimension_value
        w = gd(hole.get("width", 0)) * s
        h = gd(hole.get("height", 0)) * s
        a = gd(hole.get("altitude", 0)) * s
        props = hole.get("properties") if isinstance(hole.get("properties"), dict) else {}
        if "width" in props:
            w = gd(props["width"]) * s
        if "height" in props:
            h = gd(props["height"]) * s
        if "altitude" in props:
            pa = gd(props["altitude"]) * s
            if abs(pa - a) > 0.001:
                log(f"[WARN] Hole '{hole.get('id')}': top-level altitude={a:.3f} differs from properties.altitude={pa:.3f} — using properties value")
            a = pa
        raw = float(hole.get("offset", 0.0) or 0.0)
        if "offset" in props:
            raw = float(props["offset"])
        return w, h, a, raw

    # ---------------------------------------------------------------------
    def _build_layer_geometry(self, layer, layer_alt, show_all_floors=True, render_mode="INTERIOR", use_showall=True):
        s = self.unit_scale
        lines = layer.get("lines") or {}
        vertices = layer.get("vertices") or {}
        all_holes = dict(layer.get("holes") or {})
        for hid, h in (self.geom.get("holes") or {}).items():
            if hid not in all_holes:
                all_holes[hid] = h
                log(f"[WARN] Hole '{hid}' sourced from root-level fallback")
        areas = layer.get("areas") or {}
        structures = layer.get("structures") if isinstance(layer.get("structures"), dict) else {}

        centroid, cnt = Vector((0, 0, 0)), 0
        for area in areas.values():
            for vid in area.get("vertices", []):
                if str(vid) in vertices:
                    v = vertices[str(vid)]
                    centroid += self.plan_point_3d(float(v["x"]), float(v["y"]))
                    cnt += 1
        if cnt:
            centroid /= cnt

        # ---------------- 1. Walls ----------------
        for line_id, line in lines.items():
            vis = line.get("visible", line.get("isvisible", line.get("isVisible", True)))
            if not vis or len(line.get("vertices", [])) < 2:
                continue
            try:
                self._build_wall(str(line_id), line, vertices, areas, all_holes, layer_alt, render_mode,
                                 centroid if cnt else None)
            except Exception as e:
                log(f"[ERROR] wall {line_id} failed: {e}")
                import traceback; traceback.print_exc()

        # ---------------- 2. Floors / ceilings ----------------
        max_wall_h, max_wall_t = 0.0, 0.2 * s
        for l in lines.values():
            p = l.get("properties") or {}
            if "height" in p:
                hp = p["height"]
                max_wall_h = max(max_wall_h, (float(hp.get("length", 0)) if isinstance(hp, dict) else float(hp)) * s)
            if "thickness" in p:
                max_wall_t = max(max_wall_t, self.get_dimension_value(p["thickness"], 20.0) * s)
        if max_wall_h < 0.1:
            max_wall_h = 280.0 * s
        force_uniform = bool(layer.get("force_uniform_ceiling_height", False))

        for aid, ext in (layer.get("externalAreas") or {}).items():
            if isinstance(ext, dict):
                self._build_external_area_floor(str(aid), ext, vertices, layer_alt)

        for aid, area in areas.items():
            if len(area.get("vertices", [])) < 3:
                continue
            poly = [self.plan_point_2d(float(vertices[str(v)]["x"]), float(vertices[str(v)]["y"])) * s
                    for v in area["vertices"] if str(v) in vertices]
            if len(poly) < 3:
                continue
            if polygon_signed_area(poly) < 0:
                poly.reverse()
            span = self._span(poly)
            self._build_floor(str(aid), area, poly, span, layer_alt, structures)
            self._build_ceiling(str(aid), area, poly, span, layer_alt, max_wall_h, force_uniform, show_all_floors, use_showall)

    @staticmethod
    def _span(poly):
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        return (max(0.001, max(xs) - min(xs)), max(0.001, max(ys) - min(ys)))

    # ---------------------------------------------------------------------
    def _build_wall(self, line_id, line, vertices, areas, all_holes, layer_alt, render_mode, centroid):
        s = self.unit_scale
        v1, v2 = str(line["vertices"][0]), str(line["vertices"][1])
        if v1 not in vertices or v2 not in vertices:
            return
        props = line.get("properties") or {}
        x1, y1 = float(vertices[v1]["x"]), float(vertices[v1]["y"])
        x2, y2 = float(vertices[v2]["x"]), float(vertices[v2]["y"])
        alignment = str(props.get("alignment", "center")).lower().strip()
        t_src = self.get_dimension_value(props.get("thickness", 20.0), 20.0) if "thickness" in props else 20.0
        d2 = Vector((x2 - x1, y2 - y1))
        if d2.length < 1e-9:
            return
        d2.normalize()
        left_n, right_n = Vector((d2.y, -d2.x)), Vector((-d2.y, d2.x))
        off = Vector((0, 0))
        if alignment == "left":
            off = right_n * (t_src / 2.0)
        elif alignment == "right":
            off = left_n * (t_src / 2.0)
        p1 = self.plan_point_3d(x1 + off.x, y1 + off.y)
        p2 = self.plan_point_3d(x2 + off.x, y2 + off.y)
        diff = p2 - p1
        length = diff.length
        center = (p1 + p2) / 2.0
        height = 240.0 * s
        if "height" in props:
            hp = props["height"]
            height = (float(hp.get("length", 240)) if isinstance(hp, dict) else float(hp)) * s
        thick = self.get_dimension_value(props.get("thickness", 20.0), 20.0) * s if "thickness" in props else 0.2
        wall_angle = -math.atan2(diff.z, diff.x)
        wall_pos = Vector((center.x, layer_alt, center.z))
        corner_len = max(length - 0.001, 0.05)
        half = corner_len / 2.0

        ch = props.get("customHeight")
        if isinstance(ch, dict) and length > 0.001:
            lh = float(ch.get("left", height / s)) * s
            rh = float(ch.get("right", height / s)) * s
            chh = float(ch.get("center", height / s)) * s
            cxr = float(ch["centerX"]) * s / length if "centerX" in ch else 0.5
            cxr = min(max(cxr, 0.001), 0.999)
            acx = -half + corner_len * cxr
            poly = [(-half, 0), (half, 0), (half, rh), (acx, chh), (-half, lh)]
            max_h = max(lh, rh, chh)
        else:
            poly = [(-half, 0), (half, 0), (half, height), (-half, height)]
            max_h = height
        poly_span = (corner_len, max_h)

        facing = self._get_wall_facing_direction(line, vertices, areas)
        wall_basis = godot_trs((0, 0, 0), wall_angle).to_3x3()
        if facing == "inner-left":
            inner_sign = 1.0
        elif facing == "inner-right":
            inner_sign = -1.0
        else:
            inner_sign = 1.0
            if centroid is not None:
                to_c = centroid - center
                to_c.y = 0
                if to_c.dot(wall_basis @ Vector((0, 0, 1))) < 0:
                    inner_sign = -1.0
            facing = "inner-left" if inner_sign > 0 else "inner-right"

        inner_md = self._resolve_wall_face_material(line, "inner", facing)
        outer_md = self._resolve_wall_face_material(line, "outer", facing)
        inner_mat = self.materials.create_material(inner_md or {"color": "#d1d1d1"}, "wall", arch_uv_scale(inner_md, poly_span, s))
        outer_mat = self.materials.create_material(outer_md or {"color": "#d1d1d1"}, "wall", arch_uv_scale(outer_md, poly_span, s))

        # structural wall: CSGPolygon3D centred on the wall line
        m_wall = godot_trs(wall_pos + wall_basis @ Vector((0, 0, thick / 2.0)), wall_angle)
        verts, faces = extrude_polygon_godot(poly, thick, m_wall)
        wall = mesh_object(f"Wall_{line_id}", verts, faces, self.col_arch, [inner_mat, outer_mat])

        cutters = []
        shape_cutters = []  # thicker cutters used to clip wall custom shapes around openings
        for hid in line.get("holes", []) or []:
            hole = all_holes.get(str(hid))
            if not isinstance(hole, dict):
                continue
            hw, hh, ha, raw = self._hole_dims(hole)
            if hw < 0.01 or hh < 0.01:
                continue
            if raw > 1.0:
                ratio = min(max((raw * s) / length, 0.0), 1.0) if length > 1e-4 else 0.5
            else:
                ratio = raw
            hpos = p1.lerp(p2, ratio)
            hc = Vector((hpos.x, layer_alt + ha + hh / 2.0, hpos.z))
            cm = godot_trs(hc, wall_angle)
            cv, cf = box_godot((hw, hh, thick + 0.2), cm)
            cutters.append(mesh_object(f"HoleCut_{hid}", cv, cf, self.col_cutters))
            sv, sf = box_godot((hw, hh, thick + 1.0), cm)
            shape_cutters.append((sv, sf))
            if hole.get("is_exterior_black"):
                bv, bf = box_godot((hw + 0.02, hh + 0.02, 0.01), cm)
                mesh_object(f"HoleBlack_{hid}", bv, bf, self.col_arch, [self.materials.solid((0, 0, 0))])
            self._load_hole_asset(hole, hc, hw, hh, ha, thick, wall_angle, layer_alt)

        # wall custom shapes (panels / openings)
        inner_key = "outer_properties" if facing == "inner-left" else "inner_properties"
        outer_key = "inner_properties" if facing == "inner-left" else "outer_properties"
        faces_to_check = []
        if isinstance(line.get(inner_key), dict) and "customShapes" in line[inner_key]:
            faces_to_check.append((line[inner_key]["customShapes"] or [], inner_sign))
        if isinstance(line.get(outer_key), dict) and "customShapes" in line[outer_key]:
            faces_to_check.append((line[outer_key]["customShapes"] or [], -inner_sign))
        for shapes, side in faces_to_check:
            for idx, shape in enumerate(shapes):
                self._build_wall_shape(line_id, shape, idx, side, p1, length, height, thick, wall_angle,
                                       wall_basis, layer_alt, cutters, shape_cutters)

        apply_boolean_difference(wall, cutters)

        # per-face materials (replaces Godot's 3 mm inner/outer overlay panels)
        inner_dir = C3 @ (wall_basis @ Vector((0, 0, inner_sign)))
        me = wall.data
        for p in me.polygons:
            dot = p.normal.dot(inner_dir)
            if render_mode == "INTERIOR":
                p.material_index = 1 if dot < -0.7 else 0
            else:
                p.material_index = 1
        u_axis = three_to_blender(diff.normalized()) if length > 0 else Vector((1, 0, 0))
        planar_uvs(wall, u_axis, (0, 0, 1), C3 @ (wall_basis @ Vector((0, 0, 1))), origin=three_to_blender(Vector((p1.x, layer_alt, p1.z))))

    def _build_wall_shape(self, line_id, shape, idx, side, p1, length, height, thick, wall_angle,
                          wall_basis, layer_alt, wall_cutters, shape_cutters):
        s = self.unit_scale
        if str(shape.get("structure_type", "")).lower() == "custom_shape":
            log(f"[CustomShape] wall-surface structure custom_shape not yet ported (wall {line_id})")
            return
        pts = shape.get("points")
        if not isinstance(pts, list) or len(pts) < 3:
            return
        poly = []
        for pt in pts:
            px = float(pt.get("x", 0)) * s
            if side < 0:
                px = length - px
            py = height - float(pt.get("y", 0)) * s  # frontend Y=0 is the ceiling
            poly.append((px, py))
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        span = (max(xs) - min(xs), max(ys) - min(ys))
        v = shape.get("createOpening", False)
        is_opening = v if isinstance(v, bool) else str(v).lower() == "true"
        depth = self.get_dimension_value(shape["thickness"], 20.0) * s if "thickness" in shape else 0.02
        local_z = wall_basis @ Vector((0, 0, 1))
        base = Vector((p1.x, layer_alt, p1.z))
        if is_opening:
            depth = thick + 0.2
            pos = base + local_z * (depth / 2.0)
            verts, faces = extrude_polygon_godot(poly, depth, godot_trs(pos, wall_angle))
            wall_cutters.append(mesh_object(f"ShapeCut_{line_id}_{idx}", verts, faces, self.col_cutters))
            return
        if side > 0:
            pos = base + local_z * (thick / 2.0 + depth + 0.001 * idx)
        else:
            # Godot places the outer panel one extra `depth` off the face; sit it flush instead.
            pos = base + local_z * (-thick / 2.0 - 0.001 * idx)
        verts, faces = extrude_polygon_godot(poly, depth, godot_trs(pos, wall_angle))
        md = shape.get("material") if isinstance(shape.get("material"), dict) else {}
        mat = self.materials.create_material(md, "wall", arch_uv_scale(md, span, s))
        obj = mesh_object(f"WallShape_{line_id}_{idx}", verts, faces, self.col_arch, [mat])
        cuts = [mesh_object("ShapeHoleCut", sv, sf, self.col_cutters) for sv, sf in shape_cutters]
        apply_boolean_difference(obj, cuts)
        planar_uvs(obj, three_to_blender((wall_basis @ Vector((1, 0, 0)))), (0, 0, 1),
                   C3 @ local_z, origin=three_to_blender(Vector((p1.x, layer_alt, p1.z))))

    # ---------------------------------------------------------------------
    def _floor_flags(self, props, keys=("visible", "isvisible", "isVisible"), default=True):
        for k in keys:
            if k in props:
                return bool(props[k])
        return default

    def _build_floor(self, aid, area, poly, span, layer_alt, structures):
        s = self.unit_scale
        fp = area.get("floor_properties") or {}
        if not self._floor_flags(fp):
            return
        depth = float(fp.get("thickness", 0) or 0) * s if "thickness" in fp else 0.1
        if depth <= 0.001:
            depth = 0.1
        pav = fp.get("paving") if isinstance(fp.get("paving"), dict) else None
        if pav and str(pav.get("pattern", "none")).lower() not in ("", "none"):
            log(f"[Paving] pattern '{pav.get('pattern')}' on area {aid}: paving overlay not ported yet — using floor material")
        md = fp.get("material") if isinstance(fp.get("material"), dict) else None
        mat = self.materials.create_material(md or {"color": "#cccccc"}, "floor", arch_uv_scale(md, span, s))
        m = godot_trs((0, layer_alt, 0), rot_x=math.pi / 2)
        verts, faces = extrude_polygon_godot(poly, depth, m)
        obj = mesh_object(f"Floor_{aid}", verts, faces, self.col_arch, [mat])
        cuts = []
        for cm, size in self.structures.floor_opening_cutters(structures, aid, layer_alt, depth):
            cv, cf = box_godot(size, cm)
            cuts.append(mesh_object("FloorOpeningCut", cv, cf, self.col_cutters))
        apply_boolean_difference(obj, cuts)
        planar_uvs(obj, (1, 0, 0), (0, -1, 0), (0, 0, 1))

        for idx, shape in enumerate(fp.get("customShapes") or []):
            pts = shape.get("points")
            if not isinstance(pts, list) or len(pts) < 3:
                continue
            sp = [self.plan_point_2d(float(p.get("x", 0)), float(p.get("y", 0))) * s for p in pts]
            sd = self.get_dimension_value(shape["thickness"], 10.0) * s if "thickness" in shape else 0.010
            y = layer_alt + 0.005 + 0.001 * idx
            v, f = extrude_polygon_godot(sp, sd, godot_trs((0, y, 0), rot_x=math.pi / 2))
            smd = shape.get("material") if isinstance(shape.get("material"), dict) else {}
            smat = self.materials.create_material(smd, "floor", arch_uv_scale(smd, self._span(sp), s))
            so = mesh_object(f"FloorShape_{aid}_{idx}", v, f, self.col_arch, [smat])
            planar_uvs(so, (1, 0, 0), (0, -1, 0), (0, 0, 1))

    def _build_ceiling(self, aid, area, poly, span, layer_alt, max_wall_h, force_uniform, show_all, use_showall):
        s = self.unit_scale
        cp = area.get("ceiling_properties") or {}
        has_vis = any(k in cp for k in ("visible", "isvisible", "isVisible"))
        vis = self._floor_flags(cp) if has_vis else show_all
        if not vis:
            return
        ceil_h = max_wall_h
        if not force_uniform:
            ap = area.get("properties") or {}
            if "height" in ap:
                hp = ap["height"]
                c = (float(hp.get("length", 0)) if isinstance(hp, dict) else float(hp)) * s
                if c >= 1.5:
                    ceil_h = min(c, max_wall_h)
            elif "height" in cp:
                c = float(cp["height"]) * s
                if c >= 1.5:
                    ceil_h = min(c, max_wall_h)
        cy = layer_alt + ceil_h
        if use_showall and not show_all and self.camera_height_m > cy + 0.1:
            log(f"[VISIBILITY] Hiding ceiling {aid}: camera above ceiling")
            return
        depth = float(cp.get("thickness", 0) or 0) * s if "thickness" in cp else 0.1
        if depth <= 0.001:
            depth = 0.1
        depth = max(depth, 0.05)
        md = cp.get("material") if isinstance(cp.get("material"), dict) else None
        mat = self.materials.create_material(md or {"color": "#d1d1d1"}, "ceiling", arch_uv_scale(md, span, s))
        v, f = extrude_polygon_godot(poly, depth, godot_trs((0, cy - depth, 0), rot_x=math.pi / 2))
        obj = mesh_object(f"Ceiling_{aid}", v, f, self.col_arch, [mat])
        planar_uvs(obj, (1, 0, 0), (0, -1, 0), (0, 0, 1))
        for idx, shape in enumerate(cp.get("customShapes") or []):
            pts = shape.get("points")
            if not isinstance(pts, list) or len(pts) < 3:
                continue
            sp = [self.plan_point_2d(float(p.get("x", 0)), float(p.get("y", 0))) * s for p in pts]
            sd = self.get_dimension_value(shape["thickness"], 5.0) * s if "thickness" in shape else 0.005
            y = (cy - depth) - sd - 0.001 * idx
            v2, f2 = extrude_polygon_godot(sp, sd + 0.010, godot_trs((0, y, 0), rot_x=math.pi / 2))
            smd = shape.get("material") if isinstance(shape.get("material"), dict) else {}
            smat = self.materials.create_material(smd, "ceiling", arch_uv_scale(smd, self._span(sp), s))
            so = mesh_object(f"CeilingShape_{aid}_{idx}", v2, f2, self.col_arch, [smat])
            planar_uvs(so, (1, 0, 0), (0, -1, 0), (0, 0, 1))
        log(f"[INFO] Created ceiling for area {aid} at Y={cy:.3f}")

    def _build_external_area_floor(self, aid, area, vertices, layer_alt):
        s = self.unit_scale
        poly = [self.plan_point_2d(float(vertices[str(v)]["x"]), float(vertices[str(v)]["y"])) * s
                for v in area.get("vertices", []) if str(v) in vertices]
        if len(poly) < 3:
            return
        if polygon_signed_area(poly) < 0:
            poly.reverse()
        fp = area.get("floor_properties") if isinstance(area.get("floor_properties"), dict) else {}
        vis = area.get("visible", fp.get("visible", fp.get("isvisible", fp.get("isVisible", True))))
        if not vis:
            return
        depth = 0.1
        ap = area.get("properties") or {}
        if "thickness" in ap:
            t = self.get_dimension_value(ap["thickness"], 20.0) * s
            depth = t if t > 0.001 else depth
        elif "thickness" in fp:
            t = self.get_dimension_value(fp["thickness"], 20.0) * s
            depth = t if t > 0.001 else depth
        md = fp.get("material") if isinstance(fp.get("material"), dict) else (area.get("material") if isinstance(area.get("material"), dict) else None)
        mat = self.materials.create_material(md or {"color": "#4c994c"}, "floor", arch_uv_scale(md, self._span(poly), s))
        v, f = extrude_polygon_godot(poly, depth, godot_trs((0, layer_alt - depth - 0.01, 0), rot_x=math.pi / 2))
        obj = mesh_object(f"ExternalArea_{aid}", v, f, self.col_arch, [mat])
        planar_uvs(obj, (1, 0, 0), (0, -1, 0), (0, 0, 1))

    # =====================================================================
    # STRUCTURES
    # =====================================================================
    def build_structures(self):
        if isinstance(self.geom.get("layers"), dict):
            for lid, layer, alt, _ in self._active_layers():
                st = layer.get("structures")
                for sid, sdata in (st.items() if isinstance(st, dict) else enumerate(st or [])):
                    if isinstance(sdata, dict) and sdata.get("visible", True) is not False:
                        try:
                            self.structures.build(sdata, alt)
                        except Exception as e:
                            log(f"[ERROR] structure {sid} failed: {e}")
        for sdata in (self.geom.get("structures") or {}).values() if isinstance(self.geom.get("structures"), dict) else []:
            if isinstance(sdata, dict):
                self.structures.build(sdata, 0.0)

    # =====================================================================
    # GLB loading
    # =====================================================================
    def resolve_local_model_path(self, path):
        if not path or str(path) in ("null", "None"):
            return ""
        norm = str(path).replace("\\", "/")
        if os.path.isfile(norm):
            return norm
        fname = os.path.basename(norm)
        if not fname:
            return ""
        dirs = self.asset_search_dirs + ["glb-assets", "assets", "."]
        for d in dirs:
            c = os.path.join(d, fname)
            if os.path.isfile(c):
                return c
        base = os.path.splitext(fname)[0].lower()
        hints = [h for h in [os.path.basename(os.path.dirname(norm)).lower(), base,
                             base[:-3] if base.endswith(("_lp", "_hp")) else "", base[:-4] if base.endswith("_low") else ""] if h]
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for e in os.listdir(d):
                if e.lower().endswith(".glb") and any(h in e.lower() for h in hints):
                    return os.path.join(d, e)
        return ""

    def _import_glb_proto(self, path):
        """Import a GLB once into the hidden prototype collection. Returns (proto_empty, aabb_min, aabb_max)
        with the AABB in glTF/Godot (Y-up) local space — _get_hierarchy_aabb."""
        if ENABLE_DUPLICATION and path in self.glb_cache:
            return self.glb_cache[path]
        before = set(bpy.data.objects)
        lc = bpy.context.view_layer.layer_collection
        target_lc = None
        for c in lc.children:
            if c.name == "ZlendoScene":
                for cc in c.children:
                    if cc.name == "_GLBPrototypes":
                        target_lc = cc
        if target_lc:
            bpy.context.view_layer.active_layer_collection = target_lc
        try:
            bpy.ops.import_scene.gltf(filepath=path)
        except Exception as e:
            log("Failed to load GLTF:", path, e)
            self.glb_cache[path] = None
            return None
        new_objs = [o for o in bpy.data.objects if o not in before]
        # _sanitize_glb_lights: strip lights/cameras embedded in the asset
        for o in list(new_objs):
            if o.type in ("LIGHT", "CAMERA"):
                new_objs.remove(o)
                bpy.data.objects.remove(o, do_unlink=True)
        proto = bpy.data.objects.new("GLB_" + os.path.basename(path), None)
        self.col_protos.objects.link(proto)
        for o in new_objs:
            if o.name not in self.col_protos.objects:
                link_object(o, self.col_protos)
            if o.parent is None:
                o.parent = proto
        bpy.context.view_layer.update()
        mn = Vector((math.inf,) * 3)
        mx = Vector((-math.inf,) * 3)
        meshes = [o for o in new_objs if o.type == "MESH"]
        for o in meshes:
            for c in o.bound_box:
                g = blender_to_three(o.matrix_world @ Vector(c))
                mn = Vector((min(mn.x, g.x), min(mn.y, g.y), min(mn.z, g.z)))
                mx = Vector((max(mx.x, g.x), max(mx.y, g.y), max(mx.z, g.z)))
        if not meshes:
            mn, mx = Vector((0, 0, 0)), Vector((1, 1, 1))
        convert_glb_alpha_glass(new_objs, os.path.basename(path))
        res = (proto, mn, mx)
        self.glb_cache[path] = res
        return res

    def _instance(self, proto, name):
        """Duplicate a prototype hierarchy (mesh data shared, like Godot node.duplicate())."""
        mapping = {}

        def rec(src, parent):
            dst = src.copy()
            dst.name = f"{name}:{src.name}"[:63]
            self.col_items.objects.link(dst)
            dst.parent = parent
            if parent is not None:
                dst.matrix_parent_inverse = src.matrix_parent_inverse.copy()
            mapping[src] = dst
            for ch in src.children:
                rec(ch, dst)
        rec(proto, None)
        root = mapping[proto]
        return root, list(mapping.values())

    def _load_hole_asset(self, hole, hc, hw, hh, ha, thick, wall_angle, layer_alt):
        urls = hole.get("asset_urls") or {}
        mp = str(urls.get("GLB_File_URL") or urls.get("glb_Url") or "")
        if not mp:
            return
        path = self.resolve_local_model_path(mp)
        status = "failed"
        if path:
            res = self._import_glb_proto(path)
            if res:
                proto, mn, mx = res
                dims = mx - mn
                dims = Vector((dims.x or 1.0, dims.y or 1.0, dims.z or 1.0))
                sx, sy = hw / dims.x, hh / dims.y
                draw = hole.get("thickness", hole.get("depth", thick / self.unit_scale))
                p = hole.get("properties") or {}
                if "thickness" in p:
                    draw = p["thickness"]
                elif "depth" in p:
                    draw = p["depth"]
                sz = self.get_dimension_value(draw) * self.unit_scale / dims.z
                pos = Vector((hc.x, layer_alt + ha - mn.y * sy, hc.z))
                cx = (mn.x + dims.x / 2.0) * sx
                cz = (mn.z + dims.z / 2.0) * sz
                rb = godot_trs((0, 0, 0), wall_angle).to_3x3()
                pos += rb @ Vector((-cx, 0, -cz))
                fx = -1.0 if hole.get("flipX") else 1.0
                fz = -1.0 if hole.get("flipZ") else 1.0
                root, objs = self._instance(proto, f"Hole_{hole.get('name', hole.get('id', ''))}")
                root.matrix_world = godot_matrix_to_blender_world(godot_trs(pos, wall_angle, (sx * fx, sy, sz * fz)))
                apply_item_materials(objs, hole, self.materials)
                status = "loaded"
        self.tracked_assets.append({"type": "hole_asset", "id": hole.get("id"), "name": hole.get("name"),
                                    "path": path or mp, "status": status})

    # =====================================================================
    # ITEMS (load_assets / _load_layer_items)
    # =====================================================================
    def load_assets(self):
        if isinstance(self.geom.get("layers"), dict):
            for lid, layer, alt, _ in self._active_layers():
                for key in ("items", "assets"):
                    if key in layer:
                        self._load_layer_items(layer[key], alt, lid)
        for key in ("assets", "items"):
            if key in self.geom:
                self._load_layer_items(self.geom[key], 0.0, "root")

    def _load_layer_items(self, items, layer_alt, layer_id):
        values = items.values() if isinstance(items, dict) else (items or [])
        for item in values:
            if not isinstance(item, dict):
                continue
            try:
                self._load_item(item, layer_alt, layer_id)
            except Exception as e:
                log(f"[ERROR] item {item.get('name')} failed: {e}")
                import traceback; traceback.print_exc()

    def _load_item(self, item, layer_alt, layer_id):
        s = self.unit_scale
        mp = item.get("local_glb_path") or item.get("local_path") or ""
        if not mp and isinstance(item.get("asset_urls"), dict):
            u = item["asset_urls"]
            mp = str(u.get("GLB_File_URL") or u.get("glb_Url") or "")
        track = {"type": "item_asset", "id": item.get("id"), "name": item.get("name"), "layer_id": layer_id,
                 "requested_path": mp, "status": "failed", "reason": ""}
        self.tracked_assets.append(track)
        if not mp:
            track["reason"] = "missing_model_path"
            return
        path = self.resolve_local_model_path(mp)
        if not path:
            track["reason"] = "file_not_found"
            log("GLB not found (skipped):", mp)
            return
        res = self._import_glb_proto(path)
        if not res:
            track["reason"] = "import_failed"
            return
        proto, mn, mx = res
        track["path"] = path

        xy = self.plan_point_2d(float(item.get("x", 0)), float(item.get("y", 0)))
        px, pz_plan = xy.x * s, xy.y * s
        props = item.get("properties") if isinstance(item.get("properties"), dict) else {}
        alt_raw = props["altitude"] if "altitude" in props else item.get("altitude", 0.0)
        py = self.get_dimension_value(alt_raw, 0.0) * s + layer_alt
        rot = float(item.get("rotation", props.get("rotation", 0.0)) or 0.0)
        yaw = self.world_yaw_from_source_rotation(rot)

        tw = self.get_dimension_value(props.get("width", 100)) * s
        td = self.get_dimension_value(props.get("depth", 100)) * s
        th = self.get_dimension_value(props.get("height", 100)) * s
        dw = dh = dd = 100 * s
        resized = abs(tw - dw) > 0.001 or abs(th - dh) > 0.001 or abs(td - dd) > 0.001
        dims = mx - mn
        dims = Vector((dims.x or 1.0, dims.y or 1.0, dims.z or 1.0))
        sx, sy, sz = tw / dims.x, th / dims.y, td / dims.z
        sxs, szs = tw / dims.z, td / dims.x
        if abs(sxs - szs) < abs(sx - sz) and abs(sxs - szs) < 0.3:
            fs = Vector((sxs, sy, szs))
        else:
            avg = (sx + sz) / 2.0
            desc = (str(item.get("type", "")) + " " + str(item.get("name", ""))).lower()
            is_ceiling_light = any(k in desc for k in ("hanging", "chandelier", "ceiling_light", "ceilinglight", "lamp", "light")) \
                and str(item.get("mounting_type", "")).lower() == "ceiling_mount"
            is_thin = any(k in desc for k in ("frame", "picture", "art", "painting", "mirror", "curtain", "drape", "tv", "television", "monitor", "screen"))
            is_tall = any(k in desc for k in ("fridge", "refrigerat", "freezer", "shower", "bathtub", "bath_tub"))
            if not resized and not is_ceiling_light and not is_thin and not is_tall and avg > 0:
                if abs(sy - avg) / avg > 0.2 or sy > 1.5:
                    sy = avg
            fs = Vector((sx, sy, sz))
        final_scale = fs.copy()
        if self.image_render_only:
            if item.get("flipX"):
                fs.x = -abs(fs.x)
            if item.get("flipY", item.get("fipy", False)):
                fs.y = -abs(fs.y)
            if item.get("flipZ"):
                fs.z = -abs(fs.z)
        pos_y = py - mn.y * final_scale.y
        basis = godot_trs((0, 0, 0), yaw, tuple(fs)).to_3x3()
        lc = Vector((mn.x + (mx.x - mn.x) / 2.0, 0.0, mn.z + (mx.z - mn.z) / 2.0))
        wo = basis @ lc
        pos = Vector((px - wo.x, pos_y, pz_plan - wo.z))
        m_godot = godot_trs(pos, yaw, tuple(fs))

        root, objs = self._instance(proto, f"Item_{item.get('name', item.get('id', ''))}")
        root.matrix_world = godot_matrix_to_blender_world(m_godot)

        if item.get("is_exterior_black"):
            black = self.materials.solid((0, 0, 0))
            for o in objs:
                if o.type == "MESH":
                    for slot in o.material_slots:
                        slot.link = "OBJECT"
                        slot.material = black
        apply_item_materials(objs, item, self.materials)
        self._add_light_fixture_effect(item, objs, m_godot, mn, mx)
        track["status"] = "loaded"
        log(f"Loaded asset: {os.path.basename(path)} scale={tuple(round(v, 3) for v in fs)}")

    # =====================================================================
    # FIXTURE LIGHTS (_add_light_fixture_effect, simplified to one analytic light)
    # =====================================================================
    @staticmethod
    def _model_hint(item):
        u = item.get("asset_urls") if isinstance(item.get("asset_urls"), dict) else {}
        return str(u.get("GLB_File_URL") or u.get("glb_Url") or "").lower()

    def _is_light_fixture_item(self, item):
        desc = " ".join([str(item.get("type", "")), str(item.get("name", "")), str(item.get("mounting_type", "")), self._model_hint(item)]).lower()
        return any(k in desc for k in ["light", "lamp", "pendant", "pendent", "chandelier", "lantern", "sconce",
                                       "ceilinglamp", "ceiling_light", "wallmount", "streetlamp", "lighting fixture"])

    def _count_fixtures(self, ceiling_only=False):
        n = 0
        for layer in (self.geom.get("layers") or {}).values():
            items = (layer or {}).get("items") or {}
            for it in (items.values() if isinstance(items, dict) else items):
                if isinstance(it, dict) and self._is_light_fixture_item(it):
                    if not ceiling_only or str(it.get("mounting_type", "")).lower() == "ceiling_mount":
                        n += 1
        return n

    def _add_light_fixture_effect(self, item, objs, m_godot, mn, mx):
        if item.get("is_exterior_black") or not self._is_light_fixture_item(item):
            return
        mount = str(item.get("mounting_type", "")).lower()
        desc = " ".join([str(item.get("type", "")), str(item.get("name", "")), mount, self._model_hint(item)]).lower()
        size = mx - mn
        e = Vector((mn.x + size.x * 0.5, 0.0, mn.z + size.z * 0.5))
        if mount == "ceiling_mount":
            e.y = mn.y + size.y * 0.18
        elif "wall" in desc:
            e.y = mn.y + size.y * 0.45
            e.z = mn.z + size.z * 0.22
        elif mount == "floor_mount":
            e.y = mn.y + size.y * 0.82
        else:
            e.y = mn.y + size.y * 0.72
        street = "street" in desc or "exterior" in desc
        night = self.lighting_profile == "night"
        sunset = self.lighting_profile == "sunset"
        kelvin = 2200.0 if street else (2500.0 if sunset else (2700.0 if night else 3200.0))

        le, ee = 1.45, 5.5
        if sunset:
            le, ee = 3.2, 8.5
        elif night:
            le, ee = 5.2, 11.0
        if mount == "ceiling_mount":
            le, ee = (5.5, 18.0) if night else ((4.2, 14.0) if sunset else (2.0, 10.0))
        elif "wall" in desc:
            le, ee = (4.6, 9.8) if night else ((2.9, 8.1) if sunset else (1.2, 10.0))
        elif mount == "floor_mount":
            if street:
                le, ee = (7.2, 12.0) if night else ((4.2, 9.6) if sunset else (2.0, 6.0))
            else:
                le, ee = (5.0, 10.0) if night else ((3.1, 8.4) if sunset else (1.35, 5.2))

        meta = item.get("light_meta") if isinstance(item.get("light_meta"), dict) else None
        skip_norm = False
        if self.day_render and self.night_light_on and meta:
            base = float(meta.get("intensity", le))
            le = base * 0.15
            kelvin = float(meta.get("temperature", kelvin))
            ee = min(max(base * 0.7, 0.36), 1.10)
            skip_norm = True
        elif not self.day_render or self.night_light_on:
            il = float(item.get("illumination_level", 5.0))
            le = self.illumination_scale * (il / 5.0) * 2.2
            ee *= self.illumination_scale * (il / 6.0) * 2.2
            if self.day_render and self.night_light_on:
                le *= 0.35
                ee = 0.6
            if meta:
                kelvin = float(meta.get("temperature", kelvin))
        else:
            le, ee = 0.0, 0.3
        if mount == "ceiling_mount" and not skip_norm:
            n = math.sqrt(float(min(max(self.ceiling_fixture_count, 1), 16)))
            le /= n
            ee /= n

        kc = kelvin_to_rgb(kelvin)
        mc = self.mood_color("fixture")
        col = (min(kc[0] * mc[0], 1), min(kc[1] * mc[1], 1), min(kc[2] * mc[2], 1))

        # emissive surfaces of the fixture (bulb / shade)
        lin = tuple(srgb_to_linear(c) for c in mc)
        emissive_found = False
        for o in objs:
            if o.type != "MESH":
                continue
            for slot in o.material_slots:
                m = slot.material
                if m and (material_is_emissive(m) or any(k in m.name.lower() for k in ("bulb", "emissive", "emission", "glow", "light"))):
                    nm = m.copy()
                    set_emission(nm, lin, ee * self.cal["FIXTURE_EMISSION_SCALE"])
                    slot.link = "OBJECT"
                    slot.material = nm
                    emissive_found = True

        if le <= 0.0:
            return
        world_g = m_godot @ e
        ld = bpy.data.lights.new(f"Fixture_{item.get('id', 'light')}", "POINT")
        ld.energy = le * self.cal["FIXTURE_W_PER_GODOT"]
        ld.color = col
        ld.shadow_soft_size = self.cal["POINT_RADIUS"]
        lo = bpy.data.objects.new(ld.name, ld)
        self.col_lights.objects.link(lo)
        lo.location = three_to_blender(world_g)
        log(f"[LightFixture] {item.get('name')}: {ld.energy:.1f} W @ {kelvin:.0f}K (emissive mesh: {emissive_found})")

    # =====================================================================
    # CUSTOM LIGHTS (_build_custom_lights)
    # =====================================================================
    def _collect_custom_lights(self, src, out):
        if not isinstance(src, dict):
            return
        for key in ("customLights", "sceneLights"):
            if isinstance(src.get(key), dict):
                out.update({str(k): v for k, v in src[key].items()})
        layers = src.get("layers") if isinstance(src.get("layers"), dict) else {}
        sel = str(src.get("selectedLayer", "")).strip()
        ids = [sel] if sel and sel in layers else list(layers.keys())
        for lid in ids:
            for key in ("customLights", "sceneLights"):
                if isinstance((layers[lid] or {}).get(key), dict):
                    out.update({str(k): v for k, v in layers[lid][key].items()})
        if not out:
            for lid, layer in layers.items():
                if lid == sel:
                    continue
                for key in ("customLights", "sceneLights"):
                    if isinstance((layer or {}).get(key), dict):
                        out.update({str(k): v for k, v in layer[key].items()})

    def _ies_path(self, ld):
        p = str(ld.get("iesFile") or "")
        if p and os.path.isfile(p):
            return p
        pid = str(ld.get("iesProfileId") or "")
        prof = (self.geom.get("iesProfiles") or {}).get(pid) if pid else None
        if isinstance(prof, dict):
            for k in ("localPath", "local_path", "iesFile", "path", "url"):
                if prof.get(k) and os.path.isfile(str(prof[k])):
                    return str(prof[k])
        if p:
            name = os.path.basename(p.replace("\\", "/"))
            for d in self.asset_search_dirs:
                if os.path.isfile(os.path.join(d, name)):
                    return os.path.join(d, name)
        return ""

    def build_custom_lights(self):
        lights = {}
        self._collect_custom_lights(self.geom, lights)
        if not lights:
            return
        log(f"Building {len(lights)} custom lights.")
        cal = self.cal
        for lid, ld in lights.items():
            if not isinstance(ld, dict) or not ld.get("isOn", True):
                continue
            kind = str(ld.get("lightKind", ld.get("lightType", "point"))).lower()
            ies = self._ies_path(ld)
            k = float(ld.get("colorTemp", 0) or 0)
            color = kelvin_to_rgb(k) if k > 0 else (parse_html_color(ld.get("color", "#ffffff")) or (1, 1, 1))[:3]
            mult = float(ld["intensityMultiplier"]) if "intensityMultiplier" in ld else float(ld.get("brightness", 100.0)) / 100.0
            base_e = 5.0 if self.day_render else 15.0
            if kind == "spot" and not ies:
                base_e *= 4.0
            energy_g = mult * base_e * self.illumination_scale

            if ies:
                data = bpy.data.lights.new(f"CustomLight_{lid}", "POINT")
                data.energy = mult * self.illumination_scale * 0.3 * cal["IES_W_PER_GODOT"]
                data.shadow_soft_size = cal["POINT_RADIUS"]
                self._attach_ies(data, ies, max(mult, 0.01))
            elif kind == "spot":
                data = bpy.data.lights.new(f"CustomLight_{lid}", "SPOT")
                data.spot_size = min(math.radians(float(ld.get("coneAngle", 45.0))) * 2.0, math.pi)
                data.spot_blend = 0.15
                data.energy = energy_g * cal["LIGHT_W_PER_GODOT"]
                data.shadow_soft_size = cal["POINT_RADIUS"]
            elif kind in ("rect", "area", "area_rect"):
                data = bpy.data.lights.new(f"CustomLight_{lid}", "AREA")
                data.shape = "RECTANGLE"
                data.size = max(float(ld.get("width", 100.0)) * self.unit_scale, 0.01)
                data.size_y = max(float(ld.get("height", 100.0)) * self.unit_scale, 0.01)
                data.energy = energy_g * cal["AREA_W_PER_GODOT"]
            else:
                data = bpy.data.lights.new(f"CustomLight_{lid}", "POINT")
                data.energy = energy_g * cal["LIGHT_W_PER_GODOT"]
                data.shadow_soft_size = cal["POINT_RADIUS"]
            data.color = color
            try:
                data.use_shadow = True  # Cycles always traces shadows; castShadow only matters for Godot raster
            except Exception:
                pass
            obj = bpy.data.objects.new(data.name, data)
            self.col_lights.objects.link(obj)
            alt = float(ld.get("altitude", 0.0)) * self.unit_scale
            pos_g = self.plan_point_3d(float(ld.get("x", 0.0)), float(ld.get("y", 0.0)), alt)
            rot_y = float(ld.get("rotationY", 0.0))
            tilt = math.radians(float(ld.get("tiltAngle", 0.0)))
            # Godot: spot/area/IES aim down local -Z after rotation.x = tilt - PI/2.
            # (Godot rotates IES omni profiles to nadir itself; Blender IES follows the light's -Z,
            #  so IES lights get the same downward aim as spots.)
            m_g = godot_trs(pos_g, rot_y, rot_x=(tilt - math.pi / 2) if (kind != "point" or ies) else 0.0)
            obj.matrix_world = godot_matrix_to_blender_object(m_g)
            log(f"  light {lid}: {kind}{' +IES' if ies else ''} {data.energy:.1f} W at blender {tuple(round(v, 2) for v in obj.location)}")

    @staticmethod
    def _attach_ies(light_data, ies_path, strength):
        try:
            try:
                light_data.use_nodes = True
            except Exception:
                pass
            nt = light_data.node_tree
            if nt is None:
                log("  [IES] light node tree unavailable in this Blender build — IES ignored")
                return
            emission = next((n for n in nt.nodes if n.type == "EMISSION"), None) or nt.nodes.new("ShaderNodeEmission")
            out = next((n for n in nt.nodes if n.type == "OUTPUT_LIGHT"), None) or nt.nodes.new("ShaderNodeOutputLight")
            ies = nt.nodes.new("ShaderNodeTexIES")
            ies.mode = "EXTERNAL"
            ies.filepath = ies_path
            ies.inputs["Strength"].default_value = strength
            nt.links.new(ies.outputs["Fac"], emission.inputs["Strength"])
            nt.links.new(emission.outputs["Emission"], out.inputs["Surface"])
            log("  [IES] profile:", ies_path)
        except Exception as e:
            log("  [IES] failed to attach profile:", e)

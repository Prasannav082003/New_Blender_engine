"""
materials.py — Blender port of the material code in image_glb_creation.gd
(create_material, _apply_architecture_texture_scale, _apply_custom_shape_texture_scale,
_apply_item_materials, load_texture_from_path, _resolve_local_texture, _convert_glb_alpha_glass)
and mo_image.gd (MOImage.optimize_material).

Godot StandardMaterial3D  ->  Blender Principled BSDF node tree.
Godot uv1_scale           ->  Mapping node scale on world-metre UVs (see arch_uv_scale).
"""

import os
import json
import hashlib
import bpy

from blender_utils import log, parse_html_color, srgb_to_linear

# Same switches as image_glb_creation.gd
USE_MATERIAL_OPTIMIZER = False
FLOOR_CEIL_TEXTURE_DENSITY_BOOST = 2.0
ARCHITECTURAL_MAX_ALBEDO = 0.85
NORMAL_STRENGTH = 1.0

GLB_GLASS_MATERIAL_KEYWORDS = ["glass", "glazing", "glaze", "pane", "crystal", "transparent", "window_glass", "windowglass"]
GLB_WATER_MATERIAL_KEYWORDS = ["water", "aquarium", "pool"]
GLB_CONVERTED_GLASS_IOR = 1.2  # matches the Godot constant


class MaterialFactory:
    def __init__(self, asset_search_dirs=None):
        self._tex_cache = {}
        self._mat_cache = {}
        self.asset_search_dirs = [d for d in (asset_search_dirs or []) if d]

    # ------------------------------------------------------------------
    # Textures (load_texture_from_path / _resolve_local_texture)
    # ------------------------------------------------------------------
    def resolve_local_texture(self, url):
        if not url or str(url) in ("null", "None"):
            return ""
        norm = str(url).replace("\\", "/")
        if os.path.isfile(norm):
            return norm
        if norm.startswith("http"):
            return ""
        filename = os.path.basename(norm)
        if not filename:
            return ""
        dirs = self.asset_search_dirs + ["textures", "downloaded_textures", "assets/textures", "."]
        for d in dirs:
            cand = os.path.join(d, filename)
            if os.path.isfile(cand):
                return cand
        if "_" in filename:
            suffix = filename[filename.find("_"):]
            for d in dirs:
                if not os.path.isdir(d):
                    continue
                for f in os.listdir(d):
                    if f.endswith(suffix):
                        log("Fuzzy matched texture:", filename, "->", f)
                        return os.path.join(d, f)
        return ""

    def load_image(self, path, non_color=False):
        if not path:
            return None
        key = (str(path).replace("\\", "/"), non_color)
        if key in self._tex_cache:
            return self._tex_cache[key]
        local = self.resolve_local_texture(path)
        if not local:
            log("Texture not found (skipped):", path)
            self._tex_cache[key] = None
            return None
        try:
            img = bpy.data.images.load(local, check_existing=not non_color)
            if non_color:
                img = img if img.users <= 1 else img.copy()
                img.colorspace_settings.name = "Non-Color"
        except Exception as e:  # wrong extension / unreadable file
            log("Failed to load texture", local, e)
            img = None
        self._tex_cache[key] = img
        return img

    # ------------------------------------------------------------------
    # create_material(mat_data, surface_type)
    # ------------------------------------------------------------------
    def create_material(self, mat_data, surface_type="item", uv_scale=(1.0, 1.0), name_hint=""):
        mat_data = mat_data if isinstance(mat_data, dict) else {}
        key = hashlib.md5((json.dumps(mat_data, sort_keys=True, default=str) + surface_type +
                           "%.5f,%.5f" % tuple(uv_scale)).encode()).hexdigest()[:12]
        if key in self._mat_cache:
            return self._mat_cache[key]

        code = str(mat_data.get("code", "") or name_hint or surface_type)
        mat = bpy.data.materials.new(f"{surface_type}_{code}_{key}"[:60])
        try:
            mat.use_nodes = True  # no-op/deprecated on Blender 5.x (always node based)
        except Exception:
            pass
        nt = mat.node_tree
        nodes, links = nt.nodes, nt.links
        bsdf = nodes.get("Principled BSDF") or nodes.new("ShaderNodeBsdfPrincipled")
        out = nodes.get("Material Output") or nodes.new("ShaderNodeOutputMaterial")
        if not bsdf.outputs["BSDF"].links:
            links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

        roughness = float(mat_data.get("roughness", 0.65))
        metallic = float(mat_data.get("metallic", 0.0))  # Godot reads "metallic" only
        bsdf.inputs["Roughness"].default_value = roughness
        bsdf.inputs["Metallic"].default_value = metallic

        # --- colour (with the Godot physical albedo ceiling for architecture) ---
        base = (0.82, 0.82, 0.82, 1.0)
        has_explicit_color = False
        if "color" in mat_data:
            c = str(mat_data.get("color", "")).strip()
            if len(c) >= 3 and not c.startswith("#"):
                c = "#" + c
            parsed = parse_html_color(c)
            if parsed:
                base = parsed
                has_explicit_color = True
        if has_explicit_color and surface_type in ("wall", "ceiling", "floor"):
            lum = 0.2126 * base[0] + 0.7152 * base[1] + 0.0722 * base[2]
            if lum > ARCHITECTURAL_MAX_ALBEDO:
                k = ARCHITECTURAL_MAX_ALBEDO / lum
                base = (base[0] * k, base[1] * k, base[2] * k, base[3])
        lin = (srgb_to_linear(base[0]), srgb_to_linear(base[1]), srgb_to_linear(base[2]), 1.0)

        map_url = ""
        if mat_data.get("mapUrl"):
            map_url = str(mat_data["mapUrl"])
        elif isinstance(mat_data.get("texture_urls"), list) and mat_data["texture_urls"]:
            map_url = str(mat_data["texture_urls"][0])

        tex_coord = nodes.new("ShaderNodeTexCoord")
        mapping = nodes.new("ShaderNodeMapping")
        mapping.inputs["Scale"].default_value = (float(uv_scale[0]), float(uv_scale[1]), 1.0)
        links.new(tex_coord.outputs["UV"], mapping.inputs["Vector"])

        albedo_img = self.load_image(map_url) if map_url else None
        if albedo_img:
            t = nodes.new("ShaderNodeTexImage")
            t.image = albedo_img
            t.interpolation = "Linear"  # Cycles: Smart/Cubic blur minified 4K textures
            links.new(mapping.outputs["Vector"], t.inputs["Vector"])
            if has_explicit_color and any(abs(x - 1.0) > 1e-3 for x in lin[:3]):
                mix = nodes.new("ShaderNodeMix")
                mix.data_type = "RGBA"
                mix.blend_type = "MULTIPLY"
                mix.inputs["Factor"].default_value = 1.0
                links.new(t.outputs["Color"], mix.inputs["A"])
                mix.inputs["B"].default_value = lin
                links.new(mix.outputs["Result"], bsdf.inputs["Base Color"])
            else:
                links.new(t.outputs["Color"], bsdf.inputs["Base Color"])
        else:
            bsdf.inputs["Base Color"].default_value = lin
        if base[3] < 0.999:
            bsdf.inputs["Alpha"].default_value = base[3]

        if mat_data.get("normalUrl"):
            nimg = self.load_image(str(mat_data["normalUrl"]), non_color=True)
            if nimg:
                t = nodes.new("ShaderNodeTexImage")
                t.image = nimg
                links.new(mapping.outputs["Vector"], t.inputs["Vector"])
                nm = nodes.new("ShaderNodeNormalMap")
                nm.inputs["Strength"].default_value = NORMAL_STRENGTH
                links.new(t.outputs["Color"], nm.inputs["Color"])
                links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])

        if mat_data.get("roughnessUrl"):
            rimg = self.load_image(str(mat_data["roughnessUrl"]), non_color=True)
            if rimg:
                t = nodes.new("ShaderNodeTexImage")
                t.image = rimg
                links.new(mapping.outputs["Vector"], t.inputs["Vector"])
                sep = nodes.new("ShaderNodeSeparateColor")
                links.new(t.outputs["Color"], sep.inputs["Color"])
                # Godot: roughness_texture_channel = GREEN, multiplied by the scalar roughness
                mul = nodes.new("ShaderNodeMath")
                mul.operation = "MULTIPLY"
                mul.inputs[1].default_value = roughness
                links.new(sep.outputs["Green"], mul.inputs[0])
                links.new(mul.outputs["Value"], bsdf.inputs["Roughness"])

        if USE_MATERIAL_OPTIMIZER:
            optimize_material(bsdf, mat_data, surface_type, map_url)

        self._mat_cache[key] = mat
        return mat

    def solid(self, rgb_srgb, roughness=0.9, name="solid"):
        return self.create_material({"color": "#%02x%02x%02x" % tuple(int(max(0, min(1, c)) * 255) for c in rgb_srgb[:3]),
                                     "roughness": roughness}, "item", name_hint=name)


# ---------------------------------------------------------------------------
# Texture density (Godot uv1_scale -> Mapping scale on metre UVs)
# ---------------------------------------------------------------------------
def _tile_sizes(mat_data):
    tu = float(mat_data.get("repeatX", mat_data.get("texture_scale_x", 0.0)) or 0.0)
    tv = float(mat_data.get("repeatY", mat_data.get("texture_scale_y", 0.0)) or 0.0)
    return tu, tv


def arch_uv_scale(mat_data, surface_span, unit_scale, boost=FLOOR_CEIL_TEXTURE_DENSITY_BOOST):
    """
    _apply_architecture_texture_scale ported to metre UVs.
    Godot: UV normalised over the surface span, uv1_scale = span / tile_m * boost
    => repeats per metre = boost / tile_m.
    Legacy 'repeat' array: uv1_scale = repeat * span  => repeats per metre = repeat.
    Nothing set: one image over the whole span        => 1 / span.
    """
    w = max(float(surface_span[0]), 0.001)
    h = max(float(surface_span[1]), 0.001)
    if not isinstance(mat_data, dict):
        return (1.0 / w, 1.0 / h)
    tu, tv = _tile_sizes(mat_data)
    if tu > 0 and tv > 0:
        return (boost / max(tu * unit_scale, 1e-6), boost / max(tv * unit_scale, 1e-6))
    r = mat_data.get("repeat")
    if isinstance(r, list) and len(r) >= 2 and (float(r[0]) > 0 or float(r[1]) > 0):
        ru = float(r[0]) if float(r[0]) > 0 else 1.0 / w
        rv = float(r[1]) if float(r[1]) > 0 else 1.0 / h
        return (ru, rv)
    return (1.0 / w, 1.0 / h)


def custom_shape_uv_scale(mat_data, surface_span, unit_scale):
    """_apply_custom_shape_texture_scale: same maths without the density boost."""
    return arch_uv_scale(mat_data, surface_span, unit_scale, boost=1.0)


# ---------------------------------------------------------------------------
# MOImage.optimize_material (mo_image.gd) — off by default, like the Godot build
# ---------------------------------------------------------------------------
def optimize_material(bsdf, mat_data, surface_type, map_url):
    path = str(map_url or "").lower()
    I = bsdf.inputs

    def setv(n, v):
        if n in I:
            I[n].default_value = v

    if surface_type == "ceiling":
        setv("Metallic", 0.0); setv("Roughness", 0.95); return
    if surface_type == "wall":
        setv("Metallic", 0.0); setv("Roughness", 0.85)
    elif surface_type == "floor":
        setv("Metallic", 0.1); setv("Roughness", 0.15); setv("Coat Weight", 1.0); setv("Coat Roughness", 0.1)
    else:
        setv("Metallic", 0.0); setv("Roughness", 0.6)
    if any(k in path for k in ("marble", "tile", "granite", "polished", "porcelain")):
        setv("Roughness", 0.12); setv("Coat Weight", 0.2); setv("Coat Roughness", 0.05)
    elif any(k in path for k in ("wood", "parquet", "plank", "floor")):
        setv("Roughness", 0.25 if surface_type == "floor" else 0.32)
    elif any(k in path for k in ("fabric", "rug", "cloth", "textile", "upholster")):
        setv("Roughness", 0.95); setv("Sheen Weight", 0.5)
    elif "mirror" in path:
        setv("Roughness", 0.0); setv("Metallic", 1.0)
    elif any(k in path for k in ("glass", "glaz", "window")):
        setv("Roughness", 0.01); setv("Transmission Weight", 1.0)


# ---------------------------------------------------------------------------
# GLB post-processing
# ---------------------------------------------------------------------------
def _principled(mat):
    if not mat or not mat.node_tree:
        return None
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    return None


def convert_glb_alpha_glass(objects, asset_label=""):
    """_convert_glb_alpha_glass: alpha-blended glass/water in a GLB -> transmissive Cycles glass."""
    done = set()
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if not mat or mat.name in done:
                continue
            ctx = (mat.name + " " + obj.name + " " + asset_label).lower()
            is_glass = any(k in ctx for k in GLB_GLASS_MATERIAL_KEYWORDS)
            is_water = any(k in ctx for k in GLB_WATER_MATERIAL_KEYWORDS)
            if not (is_glass or is_water):
                continue
            b = _principled(mat)
            if b is None:
                continue
            alpha_linked = bool(b.inputs["Alpha"].links)
            alpha = b.inputs["Alpha"].default_value
            blended = getattr(mat, "surface_render_method", "") == "BLENDED" or alpha < 0.999 or alpha_linked
            if not blended and not is_glass:
                continue
            for l in list(b.inputs["Alpha"].links):
                mat.node_tree.links.remove(l)
            b.inputs["Alpha"].default_value = 1.0
            b.inputs["Transmission Weight"].default_value = 1.0
            b.inputs["Roughness"].default_value = min(b.inputs["Roughness"].default_value, 0.05)
            b.inputs["IOR"].default_value = GLB_CONVERTED_GLASS_IOR if is_glass else 1.33
            if not b.inputs["Base Color"].links:
                c = b.inputs["Base Color"].default_value
                b.inputs["Base Color"].default_value = (max(c[0], 0.9), max(c[1], 0.9), max(c[2], 0.9), 1.0)
            done.add(mat.name)
            log("  [Glass] converted", mat.name, "->", "transmissive" if is_glass else "water")


def _norm_label(s):
    return "".join(ch for ch in str(s).strip().lower() if ch in "abcdefghijklmnopqrstuvwxyz0123456789_")


GENERIC_KEYS = {"wood", "material", "materials", "default", "surface", "surfaces", "mat", "texture"}


def apply_item_materials(objects, item, factory: MaterialFactory):
    """_apply_item_materials: per-item material overrides keyed by exact (normalised) GLB material
    or mesh name. Overrides are object-linked so other instances of the same GLB keep theirs."""
    item_mats = item.get("materials")
    if not isinstance(item_mats, dict) or not item_mats:
        return
    limit_single_generic = len(item_mats) == 1 and _norm_label(next(iter(item_mats))) in GENERIC_KEYS
    applied = set()
    meshes = [o for o in objects if o.type == "MESH"]
    for obj in meshes:
        n_slots = len(obj.material_slots)
        for si, slot in enumerate(obj.material_slots):
            mat = slot.material
            if mat is None:
                continue
            matched = ""
            for key in item_mats:
                kn = _norm_label(key)
                if kn and (kn == _norm_label(mat.name) or kn == _norm_label(obj.data.name)):
                    matched = key
                    break
            if not matched and len(item_mats) == 1 and n_slots == 1:
                matched = next(iter(item_mats))
            if not matched or not isinstance(item_mats[matched], dict):
                continue
            opt = dict(item_mats[matched])
            if item.get("is_exterior_black"):
                opt = {"color": "#000000"}
            new_mat = factory.create_material(opt, "item", _item_uv_scale(opt), name_hint=mat.name)
            slot.link = "OBJECT"
            slot.material = new_mat
            applied.add(matched)
            if limit_single_generic:
                break
        if limit_single_generic and applied:
            break


def _item_uv_scale(opt):
    su, sv = 1.0, 1.0
    r = opt.get("repeat")
    if isinstance(r, list) and len(r) >= 2:
        su = float(r[0]) if float(r[0]) > 0 else su
        sv = float(r[1]) if float(r[1]) > 0 else sv
    if float(opt.get("repeatX", 0) or 0) > 0:
        su = float(opt["repeatX"])
    if float(opt.get("repeatY", 0) or 0) > 0:
        sv = float(opt["repeatY"])
    s = opt.get("scale")
    if isinstance(s, list) and len(s) >= 2:
        su = float(s[0]) if float(s[0]) > 0 else su
        sv = float(s[1]) if float(s[1]) > 0 else sv
    elif isinstance(s, (int, float)) and s > 0:
        su = sv = float(s)
    return (su, sv)


def set_emission(mat, rgb_linear, strength):
    b = _principled(mat)
    if b is None:
        return False
    b.inputs["Emission Color"].default_value = (rgb_linear[0], rgb_linear[1], rgb_linear[2], 1.0)
    b.inputs["Emission Strength"].default_value = strength
    return True


def material_is_emissive(mat):
    b = _principled(mat)
    if b is None:
        return False
    if b.inputs["Emission Color"].links:
        return True
    c = b.inputs["Emission Color"].default_value
    return (c[0] + c[1] + c[2]) > 0.01 and b.inputs["Emission Strength"].default_value > 0.0

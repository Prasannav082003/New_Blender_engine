"""
blender_utils.py — small bpy helpers shared by the Blender port of the Godot pipeline.

Geometry convention used everywhere in the port:
  * Scene maths is done in Godot/Three.js space (Y-up), exactly like image_glb_creation.gd,
    so the GDScript logic can be ported line-for-line.
  * Every vertex / transform is converted to Blender space (Z-up) with coords.py at the
    moment it is written into a bpy datablock. Nothing else converts coordinates.
"""

import math
import bpy
import bmesh
from mathutils import Vector, Matrix

from coords import C4, three_to_blender


_LOG_FILE = None


def set_log_file(path):
    """Mirror every log() line into a text file so a failed run is never silent."""
    global _LOG_FILE
    try:
        _LOG_FILE = open(path, "a", encoding="utf-8")
    except Exception as e:
        print("[Blender] could not open log file", path, e, flush=True)


def log(*args):
    line = "[Blender] " + " ".join(str(a) for a in args)
    print(line, flush=True)
    if _LOG_FILE:
        try:
            _LOG_FILE.write(line + "\n")
            _LOG_FILE.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------
def get_collection(name, parent=None, hide_render=False):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(col)
    col.hide_render = hide_render
    return col


def link_object(obj, collection):
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    collection.objects.link(obj)


# ---------------------------------------------------------------------------
# Mesh building
# ---------------------------------------------------------------------------
def polygon_signed_area(poly):
    a = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a * 0.5


def extrude_polygon_godot(poly2d, depth, m_godot):
    """
    Port of Godot CSGPolygon3D (MODE_DEPTH): the polygon lives in the node's local XY plane
    and is extruded from z=0 to z=-depth. `m_godot` is the node's Godot-space transform.
    Returns (verts_blender, faces) with front cap, back cap and side quads.
    """
    n = len(poly2d)
    local = [Vector((p[0], p[1], 0.0)) for p in poly2d] + \
            [Vector((p[0], p[1], -depth)) for p in poly2d]
    verts = []
    for v in local:
        g = m_godot @ v
        verts.append(three_to_blender(g))
    ccw = polygon_signed_area(poly2d) > 0
    front = list(range(n)) if ccw else list(reversed(range(n)))
    back = [i + n for i in reversed(front)]
    faces = [front, back]
    for i in range(n):
        j = (i + 1) % n
        if ccw:
            faces.append([i, i + n, j + n, j])
        else:
            faces.append([j, j + n, i + n, i])
    return verts, faces


def box_godot(size, m_godot):
    """Axis box of `size` centred at the local origin, transformed by m_godot (Y-up)."""
    sx, sy, sz = size[0] * 0.5, size[1] * 0.5, size[2] * 0.5
    corners = [(-sx, -sy, -sz), (sx, -sy, -sz), (sx, sy, -sz), (-sx, sy, -sz),
               (-sx, -sy, sz), (sx, -sy, sz), (sx, sy, sz), (-sx, sy, sz)]
    verts = [three_to_blender(m_godot @ Vector(c)) for c in corners]
    faces = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [2, 3, 7, 6], [1, 2, 6, 5], [0, 4, 7, 3]]
    return verts, faces


def mesh_object(name, verts, faces, collection, materials=None, face_mat_index=None):
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in verts], [], faces)
    me.validate(clean_customdata=False)
    me.update()
    obj = bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    for m in (materials or []):
        me.materials.append(m)
    if face_mat_index is not None:
        for p in me.polygons:
            p.material_index = face_mat_index(p)
    recalc_normals(obj)
    return obj


def recalc_normals(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def apply_boolean_difference(target, cutters):
    """CSG subtraction (Godot CSG OPERATION_SUBTRACTION) using Blender's Exact boolean, then bake."""
    if not cutters:
        return target
    for i, c in enumerate(cutters):
        mod = target.modifiers.new(f"cut_{i}", "BOOLEAN")
        mod.operation = "DIFFERENCE"
        mod.solver = "EXACT"
        mod.object = c
        c.hide_render = True
        c.hide_viewport = True
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    new_me = bpy.data.meshes.new_from_object(target.evaluated_get(dg))
    old = target.data
    target.modifiers.clear()
    target.data = new_me
    # keep material slots
    if len(new_me.materials) == 0:
        for m in old.materials:
            new_me.materials.append(m)
    bpy.data.meshes.remove(old)
    for c in cutters:
        me = c.data
        bpy.data.objects.remove(c, do_unlink=True)
        if me and me.users == 0:
            bpy.data.meshes.remove(me)
    return target


def planar_uvs(obj, u_axis, v_axis, n_axis, origin=Vector((0, 0, 0))):
    """
    World-space metre UVs (Blender axes). Faces facing n_axis get (u, v); faces facing u_axis
    get (n, v); faces facing v_axis get (u, n). Texture density is then set by the material's
    Mapping node, which reproduces Godot's uv1_scale maths (see materials.arch_uv_scale).
    """
    me = obj.data
    if not me.uv_layers:
        me.uv_layers.new(name="UVMap")
    uv = me.uv_layers.active.data
    mw = obj.matrix_world
    u_axis, v_axis, n_axis = Vector(u_axis).normalized(), Vector(v_axis).normalized(), Vector(n_axis).normalized()
    rot = mw.to_3x3()
    for p in me.polygons:
        n = (rot @ p.normal).normalized()
        du, dv, dn = abs(n.dot(u_axis)), abs(n.dot(v_axis)), abs(n.dot(n_axis))
        for li in p.loop_indices:
            co = mw @ me.vertices[me.loops[li].vertex_index].co - origin
            if dn >= du and dn >= dv:
                uv[li].uv = (co.dot(u_axis), co.dot(v_axis))
            elif du >= dv:
                uv[li].uv = (co.dot(n_axis), co.dot(v_axis))
            else:
                uv[li].uv = (co.dot(u_axis), co.dot(n_axis))


def srgb_to_linear(c):
    c = max(0.0, float(c))
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def parse_html_color(value, default=None):
    """Godot Color(html) equivalent -> (r,g,b,a) in sRGB 0..1, or default."""
    if value is None:
        return default
    if isinstance(value, dict):
        return (float(value.get("r", 1)), float(value.get("g", 1)), float(value.get("b", 1)), float(value.get("a", 1)))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (float(value[0]), float(value[1]), float(value[2]), float(value[3]) if len(value) > 3 else 1.0)
    s = str(value).strip()
    if s.startswith("#"):
        s = s[1:]
    try:
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) == 6:
            return (int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0, 1.0)
        if len(s) == 8:  # Godot html is RRGGBBAA
            return (int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0, int(s[6:8], 16) / 255.0)
    except ValueError:
        pass
    return default


def kelvin_to_rgb(kelvin):
    """Port of image_glb_creation.gd::_kelvin_to_rgb."""
    temp = kelvin / 100.0
    if temp <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(max(temp, 1e-6)) - 161.1195681661
        b = 0.0 if temp <= 19 else 138.5177312231 * math.log(temp - 10) - 305.0447927307
    else:
        r = 329.698727446 * math.pow(temp - 60, -0.1332047592)
        g = 288.1221695283 * math.pow(temp - 60, -0.0755148492)
        b = 255.0
    return tuple(min(max(x / 255.0, 0.0), 1.0) for x in (r, g, b))

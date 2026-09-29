"""
coords.py — Three.js / Godot (right-handed, Y-up) -> Blender (right-handed, Z-up).

THIS is the file that actually converts coordinates. The frontend JSON carries
`coordinate_system: right_handed_y_up` and `target_coordinate_system:
right_handed_z_up`; those two strings are only labels. Every conversion in the
Blender pipeline goes through the functions below.

Point rule (the only rule — everything else is derived from it):

    blender_x =  three_x
    blender_y = -three_z
    blender_z =  three_y

As a matrix this is C = Rx(+90deg):

        | 1  0  0 |
    C = | 0  0 -1 |        C^-1 = C^T
        | 0  1  0 |

Godot uses the same Y-up space as Three.js (render.gd feeds threejs_camera
straight into Camera3D with no conversion), so every Godot-space value that
image_glb_creation.gd computes is converted with the same C.

Orientation rule: for a camera/light whose Three.js world rotation matrix is
R_three (Euler XYZ), the Blender world rotation is

    R_blender = C @ R_three

Both engines' cameras look down local -Z with local +Y up, so no extra local
correction is needed. For an object transform authored in Y-up space
(T_godot) and applied to Y-up local data that Blender's glTF importer already
converted to Z-up, the Blender parent matrix is C @ T_godot @ C^-1.
"""

import math
from mathutils import Matrix, Vector, Euler

# 4x4 conversion matrices
C4 = Matrix.Rotation(math.radians(90.0), 4, 'X')   # Y-up -> Z-up
C4_INV = C4.inverted()
C3 = C4.to_3x3()


# ---------------------------------------------------------------------------
# Points / vectors
# ---------------------------------------------------------------------------
def three_to_blender(x, y=None, z=None):
    """Three.js/Godot point -> Blender point. Accepts (x,y,z), a sequence or a {'x','y','z'} dict."""
    if y is None:
        v = x
        if isinstance(v, dict):
            x, y, z = float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))
        else:
            x, y, z = float(v[0]), float(v[1]), float(v[2])
    return Vector((float(x), -float(z), float(y)))


def blender_to_three(x, y=None, z=None):
    """Inverse: Blender point -> Three.js/Godot point."""
    if y is None:
        x, y, z = x[0], x[1], x[2]
    return Vector((float(x), float(z), -float(y)))


# ---------------------------------------------------------------------------
# Rotations
# ---------------------------------------------------------------------------
def three_euler_to_blender_matrix(rx, ry, rz, order="XYZ"):
    """Three.js Euler (radians, default order 'XYZ') -> Blender 3x3 world rotation.

    Careful: Three.js order 'XYZ' builds R = Rx @ Ry @ Rz (same as Godot's EULER_ORDER_XYZ),
    while mathutils/Blender 'XYZ' builds R = Rz @ Ry @ Rx. The mathutils order string is
    therefore the REVERSE of the Three.js one.
    """
    r_three = Euler((float(rx), float(ry), float(rz)), str(order)[::-1]).to_matrix()
    return C3 @ r_three


def three_euler_to_blender_euler(rx, ry, rz, order="XYZ"):
    """Three.js Euler -> Blender rotation_euler (mode XYZ)."""
    return three_euler_to_blender_matrix(rx, ry, rz, order).to_euler("XYZ")


def look_at_matrix(eye_b, target_b):
    """Blender-space look-at rotation for a camera/light (local -Z forward, local +Y up)."""
    direction = Vector(target_b) - Vector(eye_b)
    if direction.length < 1e-6:
        direction = Vector((0.0, 1.0, 0.0))
    return direction.to_track_quat("-Z", "Y").to_matrix()


# ---------------------------------------------------------------------------
# Whole transforms
# ---------------------------------------------------------------------------
def godot_trs(position=(0, 0, 0), rot_y=0.0, scale=(1, 1, 1), rot_x=0.0, rot_z=0.0):
    """Build a Godot-space (Y-up) 4x4 transform. Node3D rotation uses Godot's default YXZ order,
    i.e. R = Ry @ Rx @ Rz, which is mathutils order 'ZXY'."""
    t = Matrix.Translation(Vector(position))
    r = Euler((rot_x, rot_y, rot_z), "ZXY").to_matrix().to_4x4()
    s = Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))
    return t @ r @ s


def godot_matrix_to_blender_world(m_godot):
    """Transform authored in Y-up space, applied to Y-up local geometry -> Blender world matrix
    for that same geometry expressed in Blender (Z-up) local coordinates."""
    return C4 @ m_godot @ C4_INV


def godot_matrix_to_blender_object(m_godot):
    """Transform for an object (camera / light) whose *local* axes are defined identically in
    both engines (look -Z, up +Y): world_blender = C @ M_godot."""
    return C4 @ m_godot


# ---------------------------------------------------------------------------
# Camera helpers (used by render.py and exposed for the camera script)
# ---------------------------------------------------------------------------
def threejs_camera_to_blender(tc, plan_transform=None):
    """
    tc: the `threejs_camera` dict from the input JSON.
    plan_transform: optional callable(Vector3 godot) -> Vector3 godot (directionangle rotation).

    Returns dict with Blender location, rotation_euler, target and vertical FOV (radians).
    Rotation priority mirrors render.gd.setup_camera(): explicit rotation first, else look-at target.
    """
    pos = tc.get("position", {"x": 0, "y": 1.5, "z": 5})
    p_g = Vector((float(pos.get("x", 0)), float(pos.get("y", 0)), float(pos.get("z", 0))))
    t_g = None
    if isinstance(tc.get("target"), dict):
        t = tc["target"]
        t_g = Vector((float(t.get("x", 0)), float(t.get("y", 0)), float(t.get("z", 0))))
    if plan_transform:
        p_g = plan_transform(p_g)
        if t_g is not None:
            t_g = plan_transform(t_g)

    loc_b = three_to_blender(p_g)
    tgt_b = three_to_blender(t_g) if t_g is not None else None

    rot_source = "default"
    if isinstance(tc.get("rotation"), dict) and not plan_transform:
        r = tc["rotation"]
        rot_m = three_euler_to_blender_matrix(r.get("x", 0), r.get("y", 0), r.get("z", 0),
                                              str(r.get("order", "XYZ")))
        rot_source = "threejs_rotation"
    elif tgt_b is not None:
        rot_m = look_at_matrix(loc_b, tgt_b)
        rot_source = "threejs_target"
    else:
        rot_m = look_at_matrix(loc_b, loc_b + Vector((0, 1, 0)))

    fov_deg = float(tc.get("fov", 45.0))
    return {
        "location": loc_b,
        "rotation_matrix": rot_m,
        "rotation_euler": rot_m.to_euler("XYZ"),
        "target": tgt_b,
        "fov_vertical_rad": math.radians(fov_deg),
        "fov_deg": fov_deg,
        "near": float(tc.get("near", 0.1)),
        "far": float(tc.get("far", 500.0)),
        "rotation_source": rot_source,
    }


def blender_camera_block_to_blender(bc, bt=None):
    """
    Uses the `blender_camera` / `blender_target` blocks as-is (already Z-up).
    NOTE: the frontend's blender_camera.rotation_euler is NOT a valid Blender camera
    orientation (e.g. X=-0.53 would point the camera at the floor/behind); the frontend's
    own script only works because a Track-To constraint overrides it. So when a target
    exists we aim at the target, exactly like that Track-To constraint does.
    """
    loc = Vector([float(v) for v in bc.get("location", [0, -5, 1.6])][:3])
    tgt = None
    if bt and isinstance(bt.get("location"), (list, tuple)) and len(bt["location"]) >= 3:
        tgt = Vector([float(v) for v in bt["location"][:3]])
    if tgt is not None:
        rot_m = look_at_matrix(loc, tgt)
        src = "blender_target"
    else:
        r = bc.get("rotation_euler", [math.pi / 2, 0, 0])
        rot_m = Euler([float(v) for v in r[:3]], "XYZ").to_matrix()
        src = "blender_rotation_euler"
    fov_deg = float(bc.get("lens", 45.0)) if str(bc.get("lens_unit", "FOV")).upper() == "FOV" else None
    return {
        "location": loc,
        "rotation_matrix": rot_m,
        "rotation_euler": rot_m.to_euler("XYZ"),
        "target": tgt,
        "fov_vertical_rad": math.radians(fov_deg) if fov_deg else None,
        "fov_deg": fov_deg,
        "lens_mm": None if fov_deg else float(bc.get("lens", 50.0)),
        "near": float(bc.get("clip_start", 0.1)),
        "far": float(bc.get("clip_end", 500.0)),
        "rotation_source": src,
    }

"""
threejs_to_blender_camera.py — the missing Three.js -> Blender camera conversion step.

extract_camera_coordinates.py only *reads* `coordinate_system` / `target_coordinate_system`
and copies the keyframes unchanged. This script actually converts them. Plain Python
(no bpy needed), so main.py or any tool can call it.

    python threejs_to_blender_camera.py image.json                 # prints blender_camera / blender_target
    python threejs_to_blender_camera.py image.json --write out.json  # writes them (+ camera_script) into a copy

Rule (right-handed Y-up -> right-handed Z-up):
    blender = (three.x, -three.z, three.y)
    R_blender = Rx(+90deg) @ R_three          (both cameras look down local -Z, up +Y)
    fov: Three.js fov is VERTICAL -> Blender sensor_fit = 'VERTICAL'
"""

import sys
import json
import math


def three_to_blender(p):
    x, y, z = (p["x"], p["y"], p["z"]) if isinstance(p, dict) else p
    return [float(x), -float(z), float(y)]


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _rx(a):
    c, s = math.cos(a), math.sin(a)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def _ry(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def _rz(a):
    c, s = math.cos(a), math.sin(a)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def euler_xyz_to_matrix(x, y, z):
    # Three.js Euler order 'XYZ' (and Godot EULER_ORDER_XYZ): R = Rx @ Ry @ Rz
    return _mat_mul(_mat_mul(_rx(x), _ry(y)), _rz(z))


def matrix_to_euler_xyz(m):
    # Blender rotation_mode 'XYZ' means R = Rz @ Ry @ Rx  -> decompose that
    sy = max(-1.0, min(1.0, -m[2][0]))
    y = math.asin(sy)
    if abs(sy) < 0.9999999:
        x = math.atan2(m[2][1], m[2][2])
        z = math.atan2(m[1][0], m[0][0])
    else:
        x = math.atan2(-m[1][2], m[1][1])
        z = 0.0
    return [x, y, z]


def look_at_euler(eye, target):
    """Blender camera Euler XYZ that points local -Z at target with world +Z up (== Track To -Z / Y)."""
    d = [target[i] - eye[i] for i in range(3)]
    L = math.sqrt(sum(v * v for v in d)) or 1.0
    d = [v / L for v in d]
    pitch = math.acos(max(-1.0, min(1.0, -d[2])))      # 0 = looking straight down
    yaw = math.atan2(d[1], d[0]) - math.pi / 2.0        # camera -Z projected on XY
    return [pitch, 0.0, yaw]


C = _rx(math.pi / 2.0)  # Y-up -> Z-up


def convert(data):
    tc = data["threejs_camera"]
    loc = three_to_blender(tc["position"])
    tgt = three_to_blender(tc["target"]) if isinstance(tc.get("target"), dict) else None
    if isinstance(tc.get("rotation"), dict):
        r = tc["rotation"]
        rot = matrix_to_euler_xyz(_mat_mul(C, euler_xyz_to_matrix(r.get("x", 0), r.get("y", 0), r.get("z", 0))))
        src = "threejs_rotation"
    elif tgt:
        rot = look_at_euler(loc, tgt)
        src = "threejs_target"
    else:
        rot = [math.pi / 2, 0.0, 0.0]
        src = "default"
    fov = float(tc.get("fov", 45.0))
    cam = {
        "location": loc,
        "rotation_euler": rot,
        "rotation_mode": "XYZ",
        "rotation_source": src,
        "type": "PERSP",
        "lens_unit": "FOV",
        "angle_y_deg": fov,
        "sensor_fit": "VERTICAL",
        "clip_start": float(tc.get("near", 0.1)),
        "clip_end": float(tc.get("far", 500.0)),
        "shift_x": 0,
        "shift_y": 0,
    }
    target = {"location": tgt, "rotation_euler": [0, 0, 0], "rotation_mode": "XYZ", "type": "EMPTY",
              "empty_display_type": "PLAIN_AXES", "empty_display_size": 1} if tgt else None
    return cam, target


def camera_script(cam, target):
    lines = [
        "import bpy, math",
        "scene = bpy.context.scene",
        "cam_data = bpy.data.cameras.new('Camera')",
        "cam_data.lens_unit = 'FOV'",
        "cam_data.sensor_fit = 'VERTICAL'   # Three.js fov is vertical",
        f"cam_data.angle_y = math.radians({cam['angle_y_deg']})",
        f"cam_data.clip_start = {cam['clip_start']}",
        f"cam_data.clip_end = {cam['clip_end']}",
        "camera = bpy.data.objects.new('Camera', cam_data)",
        "scene.collection.objects.link(camera)",
        f"camera.location = {tuple(cam['location'])}",
        "camera.rotation_mode = 'XYZ'",
        f"camera.rotation_euler = {tuple(cam['rotation_euler'])}",
        "scene.camera = camera",
    ]
    if target:
        lines += [
            "tgt = bpy.data.objects.new('CameraTarget', None)",
            "scene.collection.objects.link(tgt)",
            f"tgt.location = {tuple(target['location'])}",
        ]
    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
    cam, target = convert(data)
    old = data.get("blender_camera")
    if isinstance(old, dict):
        print("frontend blender_camera.rotation_euler :", old.get("rotation_euler"))
    print("converted  blender_camera.rotation_euler:", [round(v, 6) for v in cam["rotation_euler"]])
    print(json.dumps({"blender_camera": cam, "blender_target": target}, indent=2))
    if "--write" in sys.argv:
        out = sys.argv[sys.argv.index("--write") + 1]
        data["blender_camera"], data["blender_target"] = cam, target
        data["blender_script"] = camera_script(cam, target)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print("written:", out)


if __name__ == "__main__":
    main()

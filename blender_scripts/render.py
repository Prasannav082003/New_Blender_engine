"""
render.py — Blender port of godot_project/render.gd (+ renderer.gd wrapper). IMAGE renders.

Run (same argument contract as Godot: <input.json> <output.png>):

    blender.exe --background --factory-startup --python blender_scripts/render.py -- <input.json> <output.png>

Pipeline (mirrors render.gd::_ready):
    parse args -> load JSON -> set_resolution() -> build_scene() (image_glb_creation.py)
    -> setup_camera() (Three.js -> Blender conversion in coords.py) -> render_image()

Outputs, same as Godot:
    <output>.png            16-bit PNG (compression 0)
    <output>_thumb.webp     thumbnail used by main.py (_get_thumbnail_path)
    <output>_blender_log.json  tracked assets + camera conversion report
Optional: "_blender": {"save_blend": true} in the JSON also writes <output>.blend for debugging.
"""

import os
import sys
import json
import math
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bpy  # noqa: E402
from mathutils import Matrix, Vector  # noqa: E402

from blender_utils import log, set_log_file  # noqa: E402
from coords import threejs_camera_to_blender, blender_camera_block_to_blender  # noqa: E402
from image_glb_creation import SceneBuilder  # noqa: E402

# renderer.gd defaults
USE_THREEJS = True            # prefer threejs_camera over blender_camera
PNG16_COMPRESSION = 0

QUALITY_MAP = {
    "12K": (12288, 6642), "8K": (7680, 4320), "6K": (6144, 3321), "4K": (3840, 2160),
    "2K": (2560, 1440), "QUAD HD": (2048, 1080), "1080P": (1920, 1080), "FHD": (1920, 1080),
    "FULL HD": (1920, 1080), "HD": (1280, 720),
}

# Cycles quality per job type. draft_render=true -> fast preview.
SAMPLES_FINAL = int(os.getenv("BLENDER_SAMPLES", "256"))
SAMPLES_DRAFT = int(os.getenv("BLENDER_SAMPLES_DRAFT", "64"))


# ---------------------------------------------------------------------------
DEFAULT_INPUT = os.path.join(os.path.dirname(HERE), "image.json")
DEFAULT_OUTPUT = os.path.join(os.path.dirname(HERE), "output_renders", "blender_test.png")


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    inp = out = ""
    for a in argv:
        if a.lower().endswith(".json"):
            inp = a
        elif a.lower().endswith((".png", ".jpg", ".mp4")) or a == "video_output":
            out = a
    if (not inp or not out) and len(argv) >= 2:
        inp, out = argv[-2], argv[-1]
    return inp, out


def parse_aspect_ratio(text):
    t = str(text or "").strip()
    if not t:
        return -1.0
    for sep in (":", "/"):
        if sep in t:
            a, b = t.split(sep, 1)
            try:
                a, b = float(a), float(b)
                return a / b if a > 0 and b > 0 else -1.0
            except ValueError:
                return -1.0
    try:
        v = float(t)
        return v if v > 0 else -1.0
    except ValueError:
        return -1.0


def set_resolution(data, scene):
    w, h = 1920, 1080
    q = str(data.get("render_quality", "")).upper().strip()
    if q in QUALITY_MAP:
        w, h = QUALITY_MAP[q]
    if "width" in data:
        w = int(data["width"])
    if "height" in data:
        h = int(data["height"])
    ar = parse_aspect_ratio(data.get("aspect_ratio", "16:9"))
    if ar > 0 and abs(ar - w / max(h, 1)) > 0.001:
        long_side = max(w, h)
        if ar >= 1.0:
            w, h = long_side, int(round(long_side / ar))
        else:
            w, h = int(round(long_side * ar)), long_side
    scene.render.resolution_x = max(w, 1)
    scene.render.resolution_y = max(h, 1)
    scene.render.resolution_percentage = 100
    scale = float((data.get("_blender") or {}).get("resolution_scale", os.getenv("BLENDER_RESOLUTION_SCALE", "1.0")))
    if scale != 1.0:
        scene.render.resolution_percentage = max(1, int(round(scale * 100)))
    log(f"Setting Resolution to: {w}x{h} ({scene.render.resolution_percentage}%)")
    return w, h


# ---------------------------------------------------------------------------
def setup_camera(data, builder, scene):
    """render.gd::setup_camera, with the Three.js -> Blender conversion made explicit."""
    prefer_three = bool(data.get("use_threejs", USE_THREEJS))
    plan_tf = builder.transform_plan_point_meters if abs(builder.plan_rotation) > 1e-6 else None
    if prefer_three and isinstance(data.get("threejs_camera"), dict):
        cam_info = threejs_camera_to_blender(data["threejs_camera"], plan_tf)
        source = "threejs_camera"
    elif isinstance(data.get("blender_camera"), dict):
        cam_info = blender_camera_block_to_blender(data["blender_camera"], data.get("blender_target"))
        source = "blender_camera"
    else:
        from coords import three_to_blender, look_at_matrix
        loc = three_to_blender((0, 1.5, 5))
        cam_info = {"location": loc, "rotation_matrix": look_at_matrix(loc, three_to_blender((0, 1.0, 0))),
                    "fov_vertical_rad": math.radians(45), "fov_deg": 45, "near": 0.05, "far": 500, "target": None,
                    "rotation_source": "default"}
        source = "default"

    cam_data = bpy.data.cameras.new("MainCamera")
    cam_data.type = "PERSP"
    if cam_info.get("fov_vertical_rad"):
        # Three.js / Godot fov is the VERTICAL field of view -> lock Blender's sensor fit to vertical.
        cam_data.lens_unit = "FOV"
        cam_data.sensor_fit = "VERTICAL"
        cam_data.angle_y = cam_info["fov_vertical_rad"]
    else:
        cam_data.lens = cam_info.get("lens_mm", 50.0)
    cam_data.clip_start = max(float(cam_info.get("near", 0.1)), 0.01)
    cam_data.clip_end = max(float(cam_info.get("far", 500.0)), 100.0)
    cam = bpy.data.objects.new("MainCamera", cam_data)
    scene.collection.objects.link(cam)
    cam.matrix_world = Matrix.Translation(cam_info["location"]) @ cam_info["rotation_matrix"].to_4x4()
    scene.camera = cam

    report = {
        "source": source,
        "rotation_source": cam_info["rotation_source"],
        "blender_location": [round(v, 6) for v in cam.location],
        "blender_rotation_euler_xyz": [round(v, 6) for v in cam.matrix_world.to_euler("XYZ")],
        "blender_target": [round(v, 6) for v in cam_info["target"]] if cam_info.get("target") is not None else None,
        "fov_vertical_deg": cam_info.get("fov_deg"),
        "rule": "blender(x, y, z) = three(x, -z, y); R_blender = Rx(90deg) @ R_three",
    }
    if source == "threejs_camera":
        tc = data["threejs_camera"]
        report["threejs_position"] = tc.get("position")
        report["threejs_rotation"] = tc.get("rotation")
        report["threejs_target"] = tc.get("target")
        if isinstance(data.get("blender_camera"), dict):
            fl = data["blender_camera"].get("location")
            if isinstance(fl, list) and len(fl) >= 3:
                d = (Vector(fl[:3]) - cam.location).length
                report["frontend_blender_camera_location_delta_m"] = round(d, 6)
    log("[Camera]", json.dumps(report))
    return cam, report


# ---------------------------------------------------------------------------
def configure_cycles(scene, data, builder):
    scene.render.engine = "CYCLES"
    draft = str(data.get("draft_render", False)).lower() in ("true", "1")
    cy = scene.cycles
    cy.samples = SAMPLES_DRAFT if draft else SAMPLES_FINAL
    cy.use_adaptive_sampling = True
    cy.adaptive_threshold = 0.02 if draft else 0.01
    cy.use_denoising = True
    # Keep texture detail through the denoiser: albedo+normal guides, accurate prefilter, high quality.
    for attr, val in (("denoiser", "OPENIMAGEDENOISE"), ("denoising_input_passes", "RGB_ALBEDO_NORMAL"),
                      ("denoising_prefilter", "ACCURATE"), ("denoising_quality", "HIGH"),
                      ("denoising_use_gpu", True),
                      # Blender's default 1.5 px Blackman-Harris filter softens 4K noticeably;
                      # 1.0 px is closer to Godot's unfiltered DLSS output.
                      ("filter_width", float(os.getenv("BLENDER_FILTER_WIDTH", "1.0")))):
        try:
            setattr(cy, attr, val)
        except Exception as e:
            log(f"[Cycles] could not set {attr}={val}: {e}")
    cy.max_bounces = 12
    cy.diffuse_bounces = 6
    cy.glossy_bounces = 6
    cy.transmission_bounces = 12
    cy.transparent_max_bounces = 16
    cy.sample_clamp_indirect = 10.0
    cy.caustics_reflective = False
    cy.caustics_refractive = False

    # GPU if the build has a backend. A self-built Blender only has GPU kernels when it was
    # configured with WITH_CYCLES_CUDA_BINARIES=ON (needs the CUDA Toolkit) and, for OptiX,
    # OPTIX_ROOT_DIR pointing at the OptiX SDK. Otherwise no GPU devices are listed.
    # CYCLES_DEVICE=OPTIX|CUDA|HIP|ONEAPI|CPU forces a backend; BLENDER_REQUIRE_GPU=true fails
    # the job instead of silently rendering on the CPU.
    device = "CPU"
    wanted = os.getenv("CYCLES_DEVICE", "").strip().upper()
    order = [wanted] if wanted and wanted != "CPU" else ["OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"]
    report = {}
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for backend in order:
            try:
                prefs.compute_device_type = backend
            except TypeError:
                report[backend] = "not compiled into this Blender build"
                continue
            try:
                prefs.refresh_devices()
            except Exception:
                prefs.get_devices()
            gpus = [d for d in prefs.devices if d.type == backend]
            if not gpus:
                report[backend] = "compiled, but no usable device (missing GPU kernels / driver)"
                continue
            for d in prefs.devices:
                d.use = d.type == backend
            report[backend] = "OK: " + ", ".join(d.name for d in gpus)
            device = backend
            break
    except Exception as e:
        report["error"] = str(e)
    if wanted == "CPU":
        device = "CPU"
    for k, v in report.items():
        log(f"[Cycles] backend {k}: {v}")
    if device == "CPU":
        log("[Cycles] WARNING: rendering on CPU. This Blender build has no GPU kernels - rebuild with "
            "build_blender_gpu.bat (CUDA Toolkit + OptiX SDK) or set BLENDER_RENDER_EXE to a GPU-enabled Blender.")
        if os.getenv("BLENDER_REQUIRE_GPU", "").lower() in ("1", "true", "yes"):
            raise RuntimeError("BLENDER_REQUIRE_GPU is set but no GPU backend is available")
    cy.device = "GPU" if device != "CPU" else "CPU"
    log(f"[Cycles] device={device} samples={cy.samples} draft={draft}")

    # Colour management — Godot uses AgX tonemap with exposure multiplier
    vs = scene.view_settings
    try:
        vs.view_transform = "AgX"
        vs.look = "None"
    except TypeError:
        vs.view_transform = "Filmic"
    vs.exposure = math.log2(max(builder.godot_tonemap_exposure, 1e-3))
    log(f"[ColorMgmt] view={vs.view_transform} exposure={vs.exposure:.3f} EV (godot {builder.godot_tonemap_exposure:.3f}x)")


def render_image(scene, output_path):
    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)
    s = scene.render.image_settings
    try:
        s.media_type = "IMAGE"  # Blender 5.x
    except Exception:
        pass
    s.file_format = "PNG"
    s.color_mode = "RGB"
    s.color_depth = "16"
    s.compression = PNG16_COMPRESSION
    scene.render.filepath = os.path.abspath(output_path)
    scene.render.use_file_extension = False
    r = scene.render
    log(f"[Render] output size {int(r.resolution_x * r.resolution_percentage / 100)}x"
        f"{int(r.resolution_y * r.resolution_percentage / 100)} px, 16-bit PNG")
    t0 = time.time()
    bpy.ops.render.render(write_still=True)
    log(f"[Render] finished in {time.time() - t0:.1f}s")
    if not os.path.isfile(output_path):
        raise RuntimeError(f"Render reported OK but no file exists at {output_path}")
    log("Image saved to:", output_path)
    save_thumbnail_webp(output_path)


def save_thumbnail_webp(output_path, long_side=640):
    base = os.path.splitext(os.path.abspath(output_path))[0]
    thumb = base + "_thumb.webp"
    try:
        img = bpy.data.images.load(output_path)
        w, h = img.size
        k = long_side / max(w, h)
        img.scale(max(1, int(w * k)), max(1, int(h * k)))
        img.filepath_raw = thumb
        img.file_format = "WEBP"
        img.save()
        log("Thumbnail saved:", thumb)
    except Exception as e:
        log("Thumbnail failed:", e)


def save_logs(data, output_path, builder, cam_report, error=None):
    log_path = os.path.splitext(os.path.abspath(output_path))[0] + "_blender_log.json"
    payload = {
        "engine": "blender",
        "blender_version": bpy.app.version_string,
        "output": output_path,
        "error": error,
        "camera": cam_report,
        "tracked_assets": builder.tracked_assets if builder else [],
        "calibration": builder.cal if builder else {},
    }
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
    except Exception as e:
        log("Could not write log:", e)


# ---------------------------------------------------------------------------
def main():
    log("Blender Renderer Started (port of render.gd)")
    inp, out = parse_args()
    if not inp:
        inp = DEFAULT_INPUT
        log("No input JSON given - using default:", inp)
    if not out:
        out = DEFAULT_OUTPUT
        log("No output path given - using default:", out)
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    set_log_file(os.path.splitext(out)[0] + "_blender_run.log")
    log("Blender", bpy.app.version_string, "| python", sys.version.split()[0])
    log("Input JSON:", inp)
    log("Output Path:", out)
    if not os.path.isfile(inp):
        log("Error: Input file does not exist.")
        sys.exit(1)
    with open(inp, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data.get("video_animation"), dict) and data["video_animation"]:
        log("video_animation present — video rendering is phase 2 (not ported yet). Rendering the first frame as a still.")

    # start from an empty scene (--factory-startup gives the default cube/camera/light)
    scene = bpy.context.scene
    for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.lights, bpy.data.cameras,
                 bpy.data.materials, bpy.data.worlds):
        for block in list(coll):
            try:
                coll.remove(block)
            except Exception:
                pass

    asset_dirs = [os.getenv("ASSET_DOWNLOAD_DIR", ""), os.path.join(os.path.dirname(HERE), "asset_downloads")]
    builder = None
    cam_report = None
    try:
        set_resolution(data, scene)
        builder = SceneBuilder(data, asset_dirs, image_render_only=True)
        builder.build_scene()
        _, cam_report = setup_camera(data, builder, scene)
        configure_cycles(scene, data, builder)
        blender_opts = data.get("_blender") or {}
        if blender_opts.get("save_blend") or os.getenv("BLENDER_SAVE_BLEND", "").lower() == "true":
            blend = os.path.splitext(os.path.abspath(out))[0] + ".blend"
            bpy.ops.wm.save_as_mainfile(filepath=blend, check_existing=False)
            log("Saved debug .blend:", blend)
        if blender_opts.get("skip_render"):
            log("skip_render set — scene built, no render.")
        else:
            render_image(scene, out)
        save_logs(data, out, builder, cam_report)
    except Exception as e:
        tb = traceback.format_exc()
        print(tb, flush=True)
        log("TRACEBACK:\n" + tb)
        save_logs(data, out, builder, cam_report, error=str(e))
        log("[Render] FATAL:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()

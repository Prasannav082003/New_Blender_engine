import json
import math
import os
import time
from typing import Dict, List, Any, Set, Tuple, Optional

# Configuration
# Plan coordinates are in CM. Camera coordinates are in Meters.
SCALE_METERS_TO_CM = 100.0

# Culling logs folder
CULLING_LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "culling_logs")
os.makedirs(CULLING_LOGS_DIR, exist_ok=True)

# Floor-related items always kept
FLOOR_ITEM_KEYWORDS = ["floor", "slab", "ground", "terrain", "level"]

# Exterior-only items — kept ONLY in EXTERIOR mode, removed in INTERIOR mode
# These are identified by name/type keywords (case-insensitive substring match)
EXTERIOR_ITEM_KEYWORDS = [
    "roof", "grill", "balcony", "exterior", "facade", "chimney", "antenna",
    "gutter", "downspout", "tree", "plant", "garden", "pool", "outdoor", "landscape",
    "terrain", "road", "street", "car", "gate", "fence", "wall_cladding",
    "pergola", "gazebo", "patio", "deck", "driveway", "pathway",
]

# Elevation-type items — NEVER removed in ANY render mode (interior or exterior).
# These are part of the building shell/facade and must always be present.
ELEVATION_ITEM_KEYWORDS = ["elevation"]

# Keywords that mark an item as definitively INDOOR — prevents false-positive
# exterior keyword matches (e.g. "Indoor Snake Plant Pot" matching "plant").
INDOOR_ITEM_KEYWORDS = [
    "indoor", "interior", "ceiling", "sofa", "couch", "bed ", "wardrobe",
    "cupboard", "dresser", "toilet", "shower", "washbasin", "fridge",
    "washing machine", "curtain", "rug", "carpet", "bookshelf",
    "bookcase", "desk", "ottoman", "chandelier",
    "mirror", "cabinet", "shelf", "table", "stool", "frame", "rack",
]

# GLB path prefix that marks an item as definitively exterior-facing even when
# placed inside a room polygon (e.g. facade wall-mount lights, security cameras).
EXTERIOR_GLB_PATH_PREFIXES = ["glb-assets/exterior/", "asset-library/exterior/"]

# Item proximity rescue tolerance (cm) — catches items slightly outside polygon
ITEM_RESCUE_TOLERANCE_CM = 85.0

# Extra visibility slack for items.  Partially visible furniture should stay.
ITEM_VIEW_MARGIN_DEG = 15.0

# Image renders should be noticeably stricter than the generic item rule.
# This trims away furnishings that sit inside the same room polygon but are
# not meaningfully present in the camera's view.
STRICT_IMAGE_ITEM_VIEW_MARGIN_DEG = 0.0
STRICT_IMAGE_ITEM_RESCUE_TOLERANCE_CM = 20.0

# Image renders should also be stricter about cross-room expansion.
# Keep the best portal-connected neighboring rooms, but cap the total so we
# still avoid exploding into every room reachable from the active room.
STRICT_IMAGE_PORTAL_ROOM_LIMIT = 20

# Assets (furniture, decor) are only kept for rooms within this many portal
# hops from the active (camera) room. Rooms beyond this depth have their
# geometry created (so openings don't show a void) but no furnishings loaded.
ASSET_DEPTH_LIMIT = 3

# Tolerance for matching vertex positions between rooms (cm)
POSITION_TOLERANCE_CM = 1.0

# ── Interior precision-culling constants ──────────────────────────────────────
# Default FOV half-angle.  Read from threejs_camera.fov
# when available.  30° = 60° full FOV, typical architectural camera.
DEFAULT_FOV_HALF_DEG = 30.0

# Maximum sightline distance (cm).  Rooms whose portals (door/window centres)
# are farther than this are never created.  30 m covers most indoor sightlines.
DEFAULT_VIEW_DISTANCE_CM = 4500.0

# Ceiling override switch.
# True  -> use the current showAllFloors / camera-height ceiling logic.
# False -> force all ceilings to stay visible, regardless of camera position.
use_showall = False

# Portal angle margin added on top of the camera FOV half-angle.
# A door that sits just outside the nominal FOV can still be partially visible
# if it is wide.  20° extra margin prevents edge-portal rooms from being
# culled when the opening is only slightly off-axis.
PORTAL_ANGLE_MARGIN_DEG = 20.0

# A portal must have at least this fraction of its width visible inside the
# FOV cone for the room behind it to be created (0.0 = any overlap keeps it).
PORTAL_VISIBLE_FRACTION_MIN = 0.0


def _source_length_to_m(raw_value: Any, meters_per_unit: float) -> float:
    """Convert a source length value into metres."""
    if isinstance(raw_value, dict):
        raw_value = raw_value.get("length", 0)
    return float(raw_value or 0.0) * meters_per_unit


def _source_length_to_cm(raw_value: Any, cm_per_unit: float) -> float:
    """Convert a source length value into centimetres."""
    if isinstance(raw_value, dict):
        raw_value = raw_value.get("length", 0)
    return float(raw_value or 0.0) * cm_per_unit


def _resolve_scene_meters_per_unit(fp_data: dict) -> float:
    """
    Resolve the source plan unit scale.

    Legacy plans are treated as centimetre-based, which matches the existing
    culling behaviour. Version 2.0.0 plans use millimetres.
    """
    version = str(fp_data.get("version", "")).strip() if isinstance(fp_data, dict) else ""
    unit = str(fp_data.get("unit", "")).strip().lower() if isinstance(fp_data, dict) else ""
    if version == "2.0.0" or unit == "mm":
        return 0.001
    return 0.01


def _resolve_scene_cm_per_unit(fp_data: dict) -> float:
    return _resolve_scene_meters_per_unit(fp_data) * 100.0


def _resolve_scene_rotation_radians(fp_data: dict) -> float:
    """
    Resolve the plan rotation used by the renderer.

    The source JSON stores directionangle in degrees, and the scene uses the
    inverse rotation when mapping plan coordinates into world space.
    """
    if not isinstance(fp_data, dict):
        return 0.0
    if str(fp_data.get("version", "")).strip() == "2.0.0":
        return 0.0
    if "directionangle" not in fp_data:
        return 0.0
    return -math.radians(float(fp_data.get("directionangle", 0.0)))


def _parse_aspect_ratio(raw_value: Any) -> Optional[float]:
    """Parse an aspect ratio as width / height from common encodings."""
    if raw_value is None:
        return None

    if isinstance(raw_value, (int, float)):
        value = float(raw_value)
        return value if value > 0.0 else None

    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return None
        if ":" in text:
            parts = text.split(":")
            if len(parts) == 2:
                try:
                    width = float(parts[0].strip())
                    height = float(parts[1].strip())
                    if width > 0.0 and height > 0.0:
                        return width / height
                except Exception:
                    return None
        try:
            value = float(text)
            return value if value > 0.0 else None
        except Exception:
            return None

    if isinstance(raw_value, dict):
        width = raw_value.get("width", raw_value.get("w"))
        height = raw_value.get("height", raw_value.get("h"))
        if width is not None and height is not None:
            try:
                width_f = float(width)
                height_f = float(height)
                if width_f > 0.0 and height_f > 0.0:
                    return width_f / height_f
            except Exception:
                pass
        if "x" in raw_value and "y" in raw_value:
            try:
                width_f = float(raw_value.get("x"))
                height_f = float(raw_value.get("y"))
                if width_f > 0.0 and height_f > 0.0:
                    return width_f / height_f
            except Exception:
                pass

    return None


def _resolve_scene_aspect_ratio(payload: dict) -> Optional[float]:
    """Resolve the render aspect ratio from payload fields when available."""
    if not isinstance(payload, dict):
        return None

    candidates = [payload.get("aspect_ratio"), payload.get("render_aspect_ratio")]
    threejs_camera = payload.get("threejs_camera", {})
    if isinstance(threejs_camera, dict):
        candidates.extend(
            [
                threejs_camera.get("aspect_ratio"),
                threejs_camera.get("aspect"),
                threejs_camera.get("aspectRatio"),
            ]
        )

    for raw_value in candidates:
        aspect_ratio = _parse_aspect_ratio(raw_value)
        if aspect_ratio:
            return aspect_ratio
    return None


def _resolve_camera_plan_half_fov_deg(payload: dict) -> Tuple[float, Optional[float], Optional[float]]:
    """
    Resolve the plan-view half FOV used by the image culler.

    Three.js stores vertical FOV, while the optimizer culls in plan view.
    Convert to the horizontal plan cone using the render aspect ratio when
    possible so we do not over-cull objects that are still inside the frame.
    """
    if not isinstance(payload, dict):
        return DEFAULT_FOV_HALF_DEG, None, None

    aspect_ratio = _resolve_scene_aspect_ratio(payload)
    threejs_camera = payload.get("threejs_camera", {})
    tj_fov = threejs_camera.get("fov") if isinstance(threejs_camera, dict) else None

    if "interior_fov_half_deg" in payload:
        return float(payload["interior_fov_half_deg"]), None, aspect_ratio

    if tj_fov:
        vertical_fov_deg = float(tj_fov)
        vertical_half_deg = vertical_fov_deg / 2.0
        if aspect_ratio and aspect_ratio > 0.0:
            horizontal_half_rad = math.atan(
                math.tan(math.radians(vertical_half_deg)) * aspect_ratio
            )
            return math.degrees(horizontal_half_rad), vertical_fov_deg, aspect_ratio
        return vertical_half_deg, vertical_fov_deg, aspect_ratio

    return DEFAULT_FOV_HALF_DEG, None, aspect_ratio


def _item_dimension_cm(
    item: dict,
    key: str,
    default_cm: float = 100.0,
    cm_per_unit: float = 1.0,
) -> float:
    """Read an item's dimension in centimetres from top-level or properties."""
    raw = item.get(key, None)
    if raw is None:
        props = item.get("properties")
        if isinstance(props, dict):
            raw = props.get(key, None)

    if isinstance(raw, dict):
        if "length" in raw:
            try:
                return float(raw.get("length", default_cm)) * cm_per_unit
            except Exception:
                return default_cm
        if "value" in raw:
            try:
                return float(raw.get("value", default_cm)) * cm_per_unit
            except Exception:
                return default_cm

    try:
        if raw is not None:
            return float(raw) * cm_per_unit
    except Exception:
        pass
    return default_cm


def _item_footprint_radius_cm(item: dict, cm_per_unit: float = 1.0) -> float:
    """
    Approximate an item's footprint as a circle in plan view.
    Half the diagonal gives a conservative keep radius.
    The stored item dimensions follow the same source units as the plan data,
    so we convert them to centimetres before using them in visibility tests.
    """
    width = max(_item_dimension_cm(item, "width", 100.0, cm_per_unit), 1.0)
    depth = max(_item_dimension_cm(item, "depth", 100.0, cm_per_unit), 1.0)
    return 0.5 * math.hypot(width, depth)


def _item_plan_sample_points_cm(item: dict, cm_per_unit: float = 1.0) -> List[Tuple[float, float]]:
    """
    Build a small set of plan-view footprint samples for an item.

    We treat the item origin as the center of a conservative footprint box.
    That is intentionally generous for items whose authored origin is off-center:
    if any sampled part overlaps the active room or camera cone, we keep the item.
    """
    center_x = float(item.get("x", 0.0))
    center_y = float(item.get("y", 0.0))
    rotation = float(item.get("rotation", 0.0) or 0.0)

    width_cm = max(_item_dimension_cm(item, "width", 100.0, cm_per_unit), 1.0)
    depth_cm = max(_item_dimension_cm(item, "depth", 100.0, cm_per_unit), 1.0)
    half_w = max(width_cm * 0.5, 10.0)
    half_d = max(depth_cm * 0.5, 10.0)

    local_samples = [
        (0.0, 0.0),
        (half_w, 0.0),
        (-half_w, 0.0),
        (0.0, half_d),
        (0.0, -half_d),
        (half_w, half_d),
        (half_w, -half_d),
        (-half_w, half_d),
        (-half_w, -half_d),
    ]

    samples: List[Tuple[float, float]] = []
    for lx, ly in local_samples:
        sx, sy = _rotate_point_around_pivot(
            center_x + lx,
            center_y + ly,
            rotation,
            center_x,
            center_y,
        )
        samples.append((sx, sy))
    return samples


def _point_visible_from_sample(
    sample: Dict[str, Any],
    point_x: float,
    point_y: float,
    max_view_dist_cm: float,
    view_margin_deg: float = 0.0,
) -> bool:
    """Return True when a plan-point lies inside the camera cone."""
    cam_x = float(sample["cam_x"])
    cam_y = float(sample["cam_y"])
    cam_dir_x = float(sample["cam_dir_x"])
    cam_dir_y = float(sample["cam_dir_y"])
    fov_half_deg = float(sample["fov_half_deg"])

    dist_cm = math.hypot(point_x - cam_x, point_y - cam_y)
    if dist_cm > max_view_dist_cm:
        return False

    angle_deg = math.degrees(
        _angle_to_point(cam_x, cam_y, cam_dir_x, cam_dir_y, point_x, point_y)
    )
    return angle_deg <= (fov_half_deg + view_margin_deg)


def _item_visible_from_sample(
    item: dict,
    sample: Dict[str, Any],
    item_x: float,
    item_y: float,
    max_view_dist_cm: float,
    cm_per_unit: float = 1.0,
    view_margin_deg: float = ITEM_VIEW_MARGIN_DEG,
) -> bool:
    """Return True when the item's footprint overlaps the camera cone."""
    radius_cm = _item_footprint_radius_cm(item, cm_per_unit)
    cam_x = float(sample["cam_x"])
    cam_y = float(sample["cam_y"])
    dist_cm = math.hypot(item_x - cam_x, item_y - cam_y)
    if max(0.0, dist_cm - radius_cm) > max_view_dist_cm:
        return False

    if not _point_visible_from_sample(sample, item_x, item_y, max_view_dist_cm, view_margin_deg):
        return False
    return True


def _rotate_point_around_pivot(
    x: float,
    y: float,
    rotation_radians: float,
    pivot_x: float,
    pivot_y: float,
) -> Tuple[float, float]:
    """Rotate a 2-D point around a pivot."""
    if abs(rotation_radians) < 1e-8:
        return x, y
    dx = x - pivot_x
    dy = y - pivot_y
    cos_r = math.cos(rotation_radians)
    sin_r = math.sin(rotation_radians)
    return (
        pivot_x + (dx * cos_r) - (dy * sin_r),
        pivot_y + (dx * sin_r) + (dy * cos_r),
    )


def _transform_plan_point_cm(
    x: float,
    y: float,
    cm_per_unit: float,
    rotation_radians: float,
    pivot: Tuple[float, float],
) -> Tuple[float, float]:
    """Convert source plan coordinates to culling coordinates in cm."""
    px = float(x) * cm_per_unit
    py = float(y) * cm_per_unit
    return _rotate_point_around_pivot(px, py, rotation_radians, pivot[0], pivot[1])


def _transform_camera_point_cm(
    x_m: float,
    z_m: float,
    rotation_radians: float,
    pivot: Tuple[float, float],
) -> Tuple[float, float]:
    """Convert camera world meters into culling coordinates in cm."""
    return _rotate_point_around_pivot(
        float(x_m) * 100.0,
        float(z_m) * 100.0,
        rotation_radians,
        pivot[0],
        pivot[1],
    )


def _point_to_segment_distance_2d(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> float:
    """Return the shortest 2-D distance from a point to a line segment."""
    dx = bx - ax
    dy = by - ay
    seg_len_sq = (dx * dx) + (dy * dy)
    if seg_len_sq < 1e-12:
        return math.hypot(px - ax, py - ay)

    t = (((px - ax) * dx) + ((py - ay) * dy)) / seg_len_sq
    t = max(0.0, min(1.0, t))
    cx = ax + (t * dx)
    cy = ay + (t * dy)
    return math.hypot(px - cx, py - cy)


def _segment_intersection_2d(
    a1: Tuple[float, float],
    a2: Tuple[float, float],
    b1: Tuple[float, float],
    b2: Tuple[float, float],
) -> Optional[Tuple[float, float]]:
    """Return the intersection point of two segments, or None."""
    r_x = a2[0] - a1[0]
    r_y = a2[1] - a1[1]
    s_x = b2[0] - b1[0]
    s_y = b2[1] - b1[1]
    denom = (r_x * s_y) - (r_y * s_x)
    if abs(denom) < 1e-9:
        return None

    qp_x = b1[0] - a1[0]
    qp_y = b1[1] - a1[1]
    t = ((qp_x * s_y) - (qp_y * s_x)) / denom
    u = ((qp_x * r_y) - (qp_y * r_x)) / denom
    if t < 0.0 or t > 1.0 or u < 0.0 or u > 1.0:
        return None
    return (a1[0] + (t * r_x), a1[1] + (t * r_y))


def _segment_distance_2d(
    a1: Tuple[float, float],
    a2: Tuple[float, float],
    b1: Tuple[float, float],
    b2: Tuple[float, float],
) -> float:
    """Shortest 2-D distance between two line segments."""
    if _segment_intersection_2d(a1, a2, b1, b2) is not None:
        return 0.0
    return min(
        _point_to_segment_distance_2d(a1[0], a1[1], b1[0], b1[1], b2[0], b2[1]),
        _point_to_segment_distance_2d(a2[0], a2[1], b1[0], b1[1], b2[0], b2[1]),
        _point_to_segment_distance_2d(b1[0], b1[1], a1[0], a1[1], a2[0], a2[1]),
        _point_to_segment_distance_2d(b2[0], b2[1], a1[0], a1[1], a2[0], a2[1]),
    )


def _camera_view_segment_2d(
    cam_x: float,
    cam_y: float,
    cam_dir_x: float,
    cam_dir_y: float,
    cam_target_x: Optional[float] = None,
    cam_target_y: Optional[float] = None,
    max_view_dist_cm: float = DEFAULT_VIEW_DISTANCE_CM,
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Build the camera's plan-view segment used for blocker culling."""
    start = (float(cam_x), float(cam_y))
    if cam_target_x is not None and cam_target_y is not None:
        end = (float(cam_target_x), float(cam_target_y))
        if math.hypot(end[0] - start[0], end[1] - start[1]) > 1e-6:
            return start, end
    end = (
        float(cam_x) + (float(cam_dir_x) * max_view_dist_cm),
        float(cam_y) + (float(cam_dir_y) * max_view_dist_cm),
    )
    return start, end


def _resolve_scene_plan_pivot_cm(fp_data: dict, cm_per_unit: float) -> Tuple[float, float]:
    """
    Compute a rotation pivot from the plan bounds.

    This mirrors the Godot renderer: prefer the selected layer if available,
    otherwise use the first layer with geometry, and fall back to all vertices.
    """
    if not isinstance(fp_data, dict):
        return (0.0, 0.0)

    source = fp_data
    if fp_data.get("layers") and isinstance(fp_data.get("layers"), dict):
        source = fp_data.get("layers")

    bounds = [float("inf"), float("inf"), float("-inf"), float("-inf")]
    found = False

    def _accumulate_from_vertices(vertices: dict) -> None:
        nonlocal found
        for raw_vertex in vertices.values():
            if isinstance(raw_vertex, dict) and "x" in raw_vertex and "y" in raw_vertex:
                vx = float(raw_vertex.get("x", 0.0)) * cm_per_unit
                vy = float(raw_vertex.get("y", 0.0)) * cm_per_unit
                bounds[0] = min(bounds[0], vx)
                bounds[1] = min(bounds[1], vy)
                bounds[2] = max(bounds[2], vx)
                bounds[3] = max(bounds[3], vy)
                found = True

    if isinstance(fp_data.get("layers"), dict):
        layers = fp_data.get("layers", {})
        selected = str(fp_data.get("selectedLayer", "")).strip()
        layer_ids: List[str] = []
        if selected and selected in layers:
            layer_ids.append(selected)
        else:
            layer_ids.extend(list(layers.keys()))

        for layer_id in layer_ids:
            layer = layers.get(layer_id, {})
            if not isinstance(layer, dict):
                continue
            if isinstance(layer.get("vertices"), dict) and layer.get("vertices"):
                _accumulate_from_vertices(layer["vertices"])
                if found:
                    break

    if not found and isinstance(fp_data.get("vertices"), dict):
        _accumulate_from_vertices(fp_data.get("vertices", {}))

    if not found:
        return (0.0, 0.0)

    return ((bounds[0] + bounds[2]) * 0.5, (bounds[1] + bounds[3]) * 0.5)


# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _point_in_polygon(x: float, y: float, polygon: List[Tuple[float, float]]) -> bool:
    """Ray-casting algorithm: returns True if (x,y) is inside the polygon."""
    inside = False
    n = len(polygon)
    p1x, p1y = polygon[0]
    for i in range(1, n + 1):
        p2x, p2y = polygon[i % n]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside


def _point_in_any_area(x: float, y: float, areas: dict, vertices: dict) -> bool:
    """Returns True if (x,y) is inside ANY area polygon."""
    for area_id, area in areas.items():
        poly_verts = []
        for v_id in area.get("vertices", []):
            v = vertices.get(v_id)
            if v:
                poly_verts.append((v["x"], v["y"]))
        if poly_verts and _point_in_polygon(x, y, poly_verts):
            return True
    return False


def _min_dist_to_polygon(ix: float, iy: float, poly_verts: List[Tuple[float, float]]) -> float:
    """Minimum distance from point (ix, iy) to the edges of a polygon."""
    min_dist = float("inf")
    n = len(poly_verts)
    for i in range(n):
        x1, y1 = poly_verts[i]
        x2, y2 = poly_verts[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            dist = math.hypot(ix - x1, iy - y1)
        else:
            t = ((ix - x1) * dx + (iy - y1) * dy) / (dx * dx + dy * dy)
            t = max(0.0, min(1.0, t))
            cx, cy = x1 + t * dx, y1 + t * dy
            dist = math.hypot(ix - cx, iy - cy)
        if dist < min_dist:
            min_dist = dist
    return min_dist


def _camera_forward_collision_push_cm(
    cam_x: float,
    cam_y: float,
    cam_dir_x: float,
    cam_dir_y: float,
    cam_height_m: float,
    layers: dict,
    transformed_layers: dict,
    meters_per_unit: float,
    cm_per_unit: float,
    camera_layer_id: Optional[str] = None,
    is_top_view: bool = False,
) -> Tuple[float, Optional[str]]:
    """
    Return a small forward push for the cull camera when it overlaps a wall.

    The authored camera position is left untouched; we only move the culling sample
    forward so a camera embedded in geometry still keeps the nearby scene visible.
    """
    if is_top_view:
        return 0.0, None

    if abs(cam_dir_x) < 1e-6 and abs(cam_dir_y) < 1e-6:
        return 0.0, None

    candidate_layer_ids: List[str] = []
    if camera_layer_id and camera_layer_id in layers:
        candidate_layer_ids.append(camera_layer_id)
    else:
        for lid, layer in layers.items():
            if not isinstance(layer, dict):
                continue
            layer_alt_m = _source_length_to_m(layer.get("altitude", 0), meters_per_unit)
            wall_heights_m: List[float] = []
            for wall in layer.get("lines", {}).values():
                wall_h_m = _source_length_to_m(
                    wall.get("properties", {}).get("height", {}),
                    meters_per_unit,
                )
                if wall_h_m > 0.1:
                    wall_heights_m.append(wall_h_m)
            layer_top_m = layer_alt_m + (max(wall_heights_m) if wall_heights_m else 3.0)
            if layer_alt_m <= cam_height_m < layer_top_m:
                candidate_layer_ids.append(lid)

    if not candidate_layer_ids:
        return 0.0, None

    def _sample_collision_label(sample_x: float, sample_y: float) -> Optional[str]:
        for lid in candidate_layer_ids:
            layer = layers.get(lid)
            transformed = transformed_layers.get(lid, {})
            if not isinstance(layer, dict) or not isinstance(transformed, dict):
                continue

            verts = transformed.get("vertices", {})
            transformed_items = transformed.get("items", {})

            for line_id, line in layer.get("lines", {}).items():
                v_ids = line.get("vertices", [])
                if len(v_ids) < 2:
                    continue
                v1 = verts.get(str(v_ids[0]))
                v2 = verts.get(str(v_ids[1]))
                if not v1 or not v2:
                    continue
                p1x, p1y = float(v1["x"]), float(v1["y"])
                p2x, p2y = float(v2["x"]), float(v2["y"])
                wall_thickness_cm = _source_length_to_cm(
                    line.get("properties", {}).get("thickness", 20.0),
                    cm_per_unit,
                )
                # Use the full wall thickness as the collision band.  Half-thickness
                # was too tight and missed cameras that were sitting just outside the
                # room face, which incorrectly kept them in EXTERIOR mode.
                wall_block_tol_cm = max(20.0, wall_thickness_cm)
                dist_to_wall = _point_to_segment_distance_2d(
                    sample_x,
                    sample_y,
                    p1x,
                    p1y,
                    p2x,
                    p2y,
                )
                if dist_to_wall <= wall_block_tol_cm:
                    return f"wall '{line.get('name', line_id)}' in layer '{lid}'"

        return None

    collision_label = _sample_collision_label(cam_x, cam_y)
    if not collision_label:
        return 0.0, None

    for push_cm in (20.0, 40.0, 60.0, 80.0, 100.0, 120.0, 140.0, 160.0):
        test_x = cam_x + (cam_dir_x * push_cm)
        test_y = cam_y + (cam_dir_y * push_cm)
        if _sample_collision_label(test_x, test_y) is None:
            return push_cm, collision_label

    return 160.0, collision_label


def _resolve_selected_layer_hint(
    selected_layer_raw: str,
    layers: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve a selectedLayer hint to a concrete layer id.

    The hint may be a layer key, the layer id field, or the layer name.
    Returns (layer_id, match_reason) or (None, None) if no match exists.
    """
    hint = str(selected_layer_raw or "").strip()
    if not hint:
        return None, None

    def _norm(value: Any) -> str:
        return "".join(ch for ch in str(value).lower() if ch.isalnum())

    hint_norm = _norm(hint)

    if hint in layers:
        return hint, "exact key"

    for layer_id, layer in layers.items():
        candidates = (layer_id, layer.get("id"), layer.get("name"))
        for candidate in candidates:
            if candidate is None:
                continue
            candidate_text = str(candidate).strip()
            if not candidate_text:
                continue
            if candidate_text == hint:
                return layer_id, "exact id/name"
            if _norm(candidate_text) == hint_norm:
                return layer_id, "normalized id/name"

    for layer_id, layer in layers.items():
        candidates = (layer_id, layer.get("id"), layer.get("name"))
        for candidate in candidates:
            candidate_text = str(candidate or "").strip()
            if not candidate_text:
                continue
            candidate_norm = _norm(candidate_text)
            if not candidate_norm:
                continue
            if hint_norm.startswith(candidate_norm) or candidate_norm.startswith(hint_norm):
                return layer_id, "prefix match"

    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# PORTAL VISIBILITY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _angle_to_point(cam_x: float, cam_y: float,
                    dir_x: float, dir_y: float,
                    px: float, py: float) -> float:
    """Signed angle (radians) from camera forward direction to point (px,py).
    Returns float('inf') if point is behind camera (dist ~ 0)."""
    dx, dy = px - cam_x, py - cam_y
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return 0.0
    cos_a = max(-1.0, min(1.0, (dx * dir_x + dy * dir_y) / dist))
    return math.acos(cos_a)


def _portal_visible_in_fov(
    cam_x: float, cam_y: float,
    dir_x: float, dir_y: float,
    fov_half_rad: float,
    p1x: float, p1y: float,
    p2x: float, p2y: float,
    n_samples: int = 5,
) -> bool:
    """
    Returns True if ANY part of the portal segment [p1→p2] falls inside the
    camera's 2-D view cone (half-angle = fov_half_rad from dir).

    Samples n_samples evenly spaced points along the portal width and tests
    each.  This handles wide doorways correctly — a door that straddles the
    FOV edge is still detected.
    """
    for i in range(n_samples):
        t = i / max(1, n_samples - 1)
        sx = p1x + t * (p2x - p1x)
        sy = p1y + t * (p2y - p1y)
        if _angle_to_point(cam_x, cam_y, dir_x, dir_y, sx, sy) <= fov_half_rad:
            return True
    return False


def _portal_world_endpoints(
    line: dict,
    hole: dict,
    vertices: dict,
    cm_per_unit: float = 1.0,
) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """
    Compute the world (plan) endpoints of a portal (hole) on a wall (line).

    Returns ((x1,y1),(x2,y2)) — the two corners of the door/window opening
    in plan-space (cm), or None if the wall vertices are missing.

    The portal runs along the wall direction, centred at the hole offset,
    spanning ±half_width either side.
    """
    v_ids = line.get("vertices", [])
    if len(v_ids) < 2:
        return None
    v1 = vertices.get(str(v_ids[0]))
    v2 = vertices.get(str(v_ids[1]))
    if not v1 or not v2:
        return None

    ax, ay = float(v1["x"]), float(v1["y"])
    bx, by = float(v2["x"]), float(v2["y"])
    wall_len = math.hypot(bx - ax, by - ay)
    if wall_len < 1e-6:
        return None

    # Unit vector along wall
    ux = (bx - ax) / wall_len
    uy = (by - ay) / wall_len

    # Portal centre position along wall
    raw_offset = float(hole.get("offset", 0.5))
    # Older plans often store the opening offset as a wall fraction (0..1).
    # Newer plans store the absolute source distance along the wall.
    if abs(raw_offset) <= 1.5:
        offset_cm = raw_offset * wall_len
    else:
        offset_cm = raw_offset * cm_per_unit
    cx = ax + offset_cm * ux
    cy = ay + offset_cm * uy

    # Portal half-width — try properties dict first, then top-level
    raw_w = hole.get("properties", {}).get("width", hole.get("width", 100))
    half_w = _source_length_to_cm(raw_w, cm_per_unit) / 2.0

    p1x, p1y = cx - half_w * ux, cy - half_w * uy
    p2x, p2y = cx + half_w * ux, cy + half_w * uy
    return (p1x, p1y), (p2x, p2y)


def _rooms_beyond_portal(
    line: dict,
    active_area_id: str,
    areas: dict,
    vertices: dict,
) -> Set[str]:
    """
    Given a wall (line) that has a portal visible to the camera, return the
    set of area IDs that lie on the OTHER side of that wall from the active room.

    Strategy: collect all areas whose vertex list shares any vertex with this
    wall.  Exclude the active room itself.
    """
    v_ids = [str(v) for v in line.get("vertices", [])[:2]]
    if len(v_ids) < 2:
        return set()

    # Build position set for the two wall vertices (for loose matching)
    wall_positions: Set[Tuple] = set()
    for vid in v_ids:
        v = vertices.get(vid)
        if v:
            wall_positions.add((round(float(v["x"]), 0), round(float(v["y"]), 0)))

    beyond: Set[str] = set()
    for aid, area in areas.items():
        if aid == active_area_id:
            continue
        area_verts = [str(v) for v in area.get("vertices", [])]
        # Direct vertex-ID match
        if any(av in v_ids for av in area_verts):
            beyond.add(aid)
            continue
        # Position-based match (handles id aliasing after culling)
        for av in area_verts:
            av_data = vertices.get(av)
            if av_data:
                key = (round(float(av_data["x"]), 0), round(float(av_data["y"]), 0))
                if key in wall_positions:
                    beyond.add(aid)
                    break

    return beyond


def _collect_portal_visible_rooms(
    active_area_id: str,
    areas: dict,
    lines: dict,
    holes: dict,
    vertices: dict,
    cm_per_unit: float,
    cam_x: float,
    cam_y: float,
    dir_x: float,
    dir_y: float,
    fov_half_rad: float,
    max_dist_cm: float,
    log_fn,
    strict_room_limit: int = 0,
) -> Set[str]:
    """
    Finds all rooms that should be created in INTERIOR mode by doing
    PORTAL-based visibility from the active room outward.

    Algorithm (single-depth — only rooms directly visible through openings
    in the active room's walls):

      For each wall that belongs to the active room:
        For each hole (door/window) on that wall:
          1. Compute the portal's world endpoints (width-aware, not just centre)
          2. Check if ANY part of the portal falls within the camera FOV
          3. Check if the portal centre is within max_dist_cm of the camera
          4. If visible → find the room(s) on the other side → add to keep set

    Returns set of area IDs to keep (NOT including active_area_id — caller adds it).
    """
    log_fn(f"  🔭 Portal-visibility sweep from active room {active_area_id} ...")

    # Collect the active room's vertex IDs and positions
    active_area = areas[active_area_id]
    active_verts: Set[str] = set(str(v) for v in active_area.get("vertices", []))
    
    active_positions: Set[Tuple[float, float]] = set()
    for vid in active_verts:
        v = vertices.get(vid)
        if v:
            active_positions.add((round(float(v["x"]), 0), round(float(v["y"]), 0)))

    def _area_centroid(a_id: str) -> Optional[Tuple[float, float]]:
        area = areas.get(a_id)
        if not area:
            return None
        pts: List[Tuple[float, float]] = []
        for v_id in area.get("vertices", []):
            v = vertices.get(str(v_id))
            if v:
                pts.append((float(v["x"]), float(v["y"])))
        if not pts:
            return None
        return (
            sum(x for x, _ in pts) / len(pts),
            sum(y for _, y in pts) / len(pts),
        )

    active_room_centroid = _area_centroid(active_area_id)

    def _segment_side(
        ax: float,
        ay: float,
        bx: float,
        by: float,
        px: float,
        py: float,
    ) -> float:
        """Signed distance proxy: positive and negative values mean opposite sides."""
        return ((bx - ax) * (py - ay)) - ((by - ay) * (px - ax))

    def _is_on_opposite_side(
        room_centroid: Optional[Tuple[float, float]],
        wall_p1: Tuple[float, float],
        wall_p2: Tuple[float, float],
    ) -> bool:
        """Keep only rooms that sit on the far side of the wall from the active room."""
        if not active_room_centroid or room_centroid is None:
            return True

        ref_side = _segment_side(
            wall_p1[0], wall_p1[1], wall_p2[0], wall_p2[1],
            active_room_centroid[0], active_room_centroid[1],
        )
        room_side = _segment_side(
            wall_p1[0], wall_p1[1], wall_p2[0], wall_p2[1],
            room_centroid[0], room_centroid[1],
        )
        if abs(ref_side) < 1e-6 or abs(room_side) < 1e-6:
            return True
        return (ref_side * room_side) < 0.0

    visible_rooms: Set[str] = set()
    portal_room_scores: Dict[str, Tuple[float, float, float]] = {}
    fallback_room_scores: Dict[str, Tuple[float, float, float]] = {}

    def _register_room_candidate(
        room_id: str,
        kind_bias: float,
        room_centroid: Optional[Tuple[float, float]],
        portal_dist_cm: float,
        source: str,
    ) -> None:
        if room_id == active_area_id or room_centroid is None:
            return
        room_angle_deg = math.degrees(
            _angle_to_point(cam_x, cam_y, dir_x, dir_y, room_centroid[0], room_centroid[1])
        )
        score = (kind_bias, abs(room_angle_deg), portal_dist_cm)
        score_map = portal_room_scores if source == "portal" else fallback_room_scores
        current = score_map.get(room_id)
        if current is None or score < current:
            score_map[room_id] = score

    portal_margin_rad = math.radians(PORTAL_ANGLE_MARGIN_DEG)
    effective_fov = fov_half_rad + portal_margin_rad
    facing_wall_room_margin_rad = math.radians(20.0)

    for lid, line in lines.items():
        v_ids = [str(v) for v in line.get("vertices", [])[:2]]
        if len(v_ids) < 2:
            continue

        # Wall must touch active room (check IDs first, then positions)
        touches_active = any(vid in active_verts for vid in v_ids)
        if not touches_active:
            for vid in v_ids:
                v = vertices.get(vid)
                if v:
                    vpos = (round(float(v["x"]), 0), round(float(v["y"]), 0))
                    if vpos in active_positions:
                        touches_active = True
                        break
        
        if not touches_active:
            continue

        line_holes = line.get("holes", [])
        is_hidden  = line.get("visible") is False
        
        # (a) Handle regular portals (doors/windows)
        for hole_id in line_holes:
            hid = str(hole_id)
            hole = holes.get(hid)
            if not hole:
                continue

            # Portal endpoints (width-aware)
            endpoints = _portal_world_endpoints(line, hole, vertices, cm_per_unit)
            if not endpoints:
                continue
            (ep1x, ep1y), (ep2x, ep2y) = endpoints
            portal_cx = (ep1x + ep2x) / 2.0
            portal_cy = (ep1y + ep2y) / 2.0

            # Distance check
            dist = math.hypot(portal_cx - cam_x, portal_cy - cam_y)
            if dist > max_dist_cm:
                log_fn(
                    f"    ↩ Portal '{hole.get('name', hid)}' on wall {lid} — "
                    f"dist {dist:.0f}cm > max {max_dist_cm:.0f}cm → skip"
                )
                continue

            # FOV check — sample across full portal width
            if not _portal_visible_in_fov(
                cam_x, cam_y, dir_x, dir_y, effective_fov,
                ep1x, ep1y, ep2x, ep2y
            ):
                portal_angle = math.degrees(_angle_to_point(
                    cam_x, cam_y, dir_x, dir_y, portal_cx, portal_cy
                ))
                log_fn(
                    f"    ↩ Portal '{hole.get('name', hid)}' angle={portal_angle:.1f}° "
                    f"> FOV ±{math.degrees(effective_fov):.0f}° → culled"
                )
                continue

            # Portal IS visible — find rooms beyond
            beyond = _rooms_beyond_portal(line, active_area_id, areas, vertices)
            portal_angle = math.degrees(_angle_to_point(
                cam_x, cam_y, dir_x, dir_y, portal_cx, portal_cy
            ))
            if beyond:
                log_fn(
                    f"    ✅ Portal '{hole.get('name', hid)}' on wall {lid} "
                    f"angle={portal_angle:.1f}° dist={dist:.0f}cm → "
                    f"reveal: {[areas[a].get('name', a) for a in beyond if a in areas]}"
                )
                for aid in beyond:
                    centroid = _area_centroid(aid)
                    if centroid is not None and not _is_on_opposite_side(centroid, (ep1x, ep1y), (ep2x, ep2y)):
                        # Room is vertex-adjacent (confirmed by _rooms_beyond_portal) but
                        # centroid is on the same side — common with L-shaped / shared-boundary
                        # rooms.  Log the skip but still register as a lower-priority fallback
                        # so it is not silently dropped.
                        log_fn(
                            f"      ⚠️  Room '{areas.get(aid, {}).get('name', aid)}' centroid same-side "
                            f"— registering as fallback candidate (vertex-adjacent)"
                        )
                        _register_room_candidate(aid, 0.05, centroid, dist, "fallback")
                        continue
                    _register_room_candidate(aid, 0.0, centroid, dist, "portal")
            else:
                log_fn(
                    f"    ⚠️  Portal '{hole.get('name', hid)}' on wall {lid} "
                    f"angle={portal_angle:.1f}° — no room found beyond"
                )

        # (b) Handle hidden walls (treat entire wall as a portal)
        if is_hidden:
            vd1 = vertices.get(v_ids[0])
            vd2 = vertices.get(v_ids[1])
            if vd1 and vd2:
                p1x, p1y = float(vd1["x"]), float(vd1["y"])
                p2x, p2y = float(vd2["x"]), float(vd2["y"])
                mid_x, mid_y = (p1x + p2x) / 2.0, (p1y + p2y) / 2.0
                dist = math.hypot(mid_x - cam_x, mid_y - cam_y)
                if dist <= max_dist_cm:
                    if _portal_visible_in_fov(cam_x, cam_y, dir_x, dir_y, effective_fov, p1x, p1y, p2x, p2y):
                        beyond = _rooms_beyond_portal(line, active_area_id, areas, vertices)
                        if beyond:
                            portal_angle = math.degrees(_angle_to_point(cam_x, cam_y, dir_x, dir_y, mid_x, mid_y))
                            log_fn(
                                f"    ✅ Hidden Wall {lid} angle={portal_angle:.1f}° dist={dist:.0f}cm → "
                                f"reveal: {[areas[a].get('name', a) for a in beyond if a in areas]}"
                            )
                            for aid in beyond:
                                centroid = _area_centroid(aid)
                                if centroid is not None and not _is_on_opposite_side(centroid, (p1x, p1y), (p2x, p2y)):
                                    log_fn(
                                        f"      ⚠️  Room '{areas.get(aid, {}).get('name', aid)}' centroid same-side "
                                        f"(hidden wall) — registering as fallback candidate"
                                    )
                                    _register_room_candidate(aid, 0.15, centroid, dist, "fallback")
                                    continue
                                _register_room_candidate(aid, 0.1, centroid, dist, "portal")
        elif line_holes:
            # Portals already handled above. This branch keeps the function
            # structure explicit and avoids treating them as plain walls.
            pass
        else:
            # Image-only fallback: some new plans export the next room boundary
            # as a plain visible wall. If the wall faces the camera and the
            # neighboring room sits on the far side of that wall, keep it.
            vd1 = vertices.get(v_ids[0])
            vd2 = vertices.get(v_ids[1])
            if vd1 and vd2:
                p1x, p1y = float(vd1["x"]), float(vd1["y"])
                p2x, p2y = float(vd2["x"]), float(vd2["y"])
                mid_x, mid_y = (p1x + p2x) / 2.0, (p1y + p2y) / 2.0
                dist = math.hypot(mid_x - cam_x, mid_y - cam_y)
                if dist <= max_dist_cm and _portal_visible_in_fov(
                    cam_x, cam_y, dir_x, dir_y, effective_fov, p1x, p1y, p2x, p2y
                ):
                    beyond = _rooms_beyond_portal(line, active_area_id, areas, vertices)
                    keep_rooms: Set[str] = set()
                    for aid in beyond:
                        if aid == active_area_id:
                            continue
                        centroid = _area_centroid(aid)
                        if not centroid:
                            continue
                        # Keep only rooms that are on the far side of the wall.
                        if (
                            ((centroid[0] - mid_x) * (cam_x - mid_x))
                            + ((centroid[1] - mid_y) * (cam_y - mid_y))
                        ) < 0.0:
                            room_angle = _angle_to_point(
                                cam_x, cam_y, dir_x, dir_y, centroid[0], centroid[1]
                            )
                            if room_angle <= (effective_fov + facing_wall_room_margin_rad):
                                keep_rooms.add(aid)

                    if keep_rooms:
                        for aid in keep_rooms:
                            centroid = _area_centroid(aid)
                            if centroid is not None and not _is_on_opposite_side(centroid, (p1x, p1y), (p2x, p2y)):
                                continue
                            _register_room_candidate(aid, 0.2, centroid, dist, "fallback")
                        log_fn(
                            f"    ✅ Facing Wall {lid} angle={math.degrees(_angle_to_point(cam_x, cam_y, dir_x, dir_y, mid_x, mid_y)):.1f}° "
                            f"dist={dist:.0f}cm → reveal: {[areas[a].get('name', a) for a in keep_rooms if a in areas]}"
                        )

    portal_limit = strict_room_limit if strict_room_limit > 0 else STRICT_IMAGE_PORTAL_ROOM_LIMIT
    if portal_room_scores or fallback_room_scores:
        ordered_rooms: List[Tuple[str, Tuple[float, float, float]]] = []

        if portal_room_scores:
            ordered_portal_rooms = sorted(portal_room_scores.items(), key=lambda kv: kv[1])
            ordered_rooms.extend(ordered_portal_rooms[:portal_limit])

        remaining_slots = max(0, portal_limit - len(ordered_rooms))
        if remaining_slots > 0 and fallback_room_scores:
            ordered_fallback_rooms = sorted(fallback_room_scores.items(), key=lambda kv: kv[1])
            ordered_rooms.extend(ordered_fallback_rooms[:remaining_slots])

        visible_rooms = {room_id for room_id, _ in ordered_rooms}
        combined_count = len(portal_room_scores) + len(fallback_room_scores)
        log_fn(
            f"  🔒 Image room clamp: keeping {len(visible_rooms)} portal-neighbour room(s) "
            f"out of {combined_count} candidate(s)"
        )
        for room_id, score in ordered_rooms:
            log_fn(
                f"     • {areas.get(room_id, {}).get('name', room_id)} ({room_id}) "
                f"score={score[0]:.2f}/{score[1]:.1f}/{score[2]:.0f}"
            )

    return visible_rooms


# ─────────────────────────────────────────────────────────────────────────────
# BFS FLOOD-FILL  (kept for exterior / top-view modes)
# ─────────────────────────────────────────────────────────────────────────────

def _flood_fill_connected_rooms(
    seed_ids: Set[str],
    areas: dict,
    lines: dict,
    vertices: dict,
    log_fn,
) -> Set[str]:
    """
    Starting from seed_ids, expand kept rooms via:
      (a) Shared vertex positions (adjacent rooms share a wall corner)
      (b) Walls that contain holes (door/window openings) or are hidden/invisible
          — these mark passable boundaries between rooms
    Returns the full set of kept area IDs.
    """
    # Pre-compute each area's vertex positions for fast neighbour checks
    area_positions: Dict[str, List[Tuple[float, float]]] = {}
    for a_id, area in areas.items():
        positions = []
        for v_id in area.get("vertices", []):
            v = vertices.get(v_id)
            if v:
                positions.append((v["x"], v["y"]))
        area_positions[a_id] = positions

    def areas_are_neighbors(aid1: str, aid2: str) -> bool:
        for (x1, y1) in area_positions.get(aid1, []):
            for (x2, y2) in area_positions.get(aid2, []):
                if (
                    abs(x1 - x2) < POSITION_TOLERANCE_CM
                    and abs(y1 - y2) < POSITION_TOLERANCE_CM
                ):
                    return True
        return False

    kept: Set[str] = set(seed_ids)
    changed = True

    while changed:
        changed = False

        # (a) Position-based adjacency
        for a_id in list(areas.keys()):
            if a_id in kept:
                continue
            for kept_id in list(kept):
                if areas_are_neighbors(kept_id, a_id):
                    kept.add(a_id)
                    changed = True
                    log_fn(
                        f"  ➕ Adding Neighbour Room (Position Match): "
                        f"'{areas[a_id].get('name')}' ({a_id})"
                    )
                    break

        # (b) Hole / hidden-wall connections
        all_kept_verts: Set[str] = set()
        for aid in kept:
            all_kept_verts.update(areas[aid].get("vertices", []))

        for lid, line in lines.items():
            line_holes = line.get("holes", [])
            line_hidden = line.get("visible") is False
            if not line_holes and not line_hidden:
                continue

            v_ids = line.get("vertices", [])
            if len(v_ids) < 2:
                continue
            v1, v2 = v_ids[0], v_ids[1]

            v1_pos = (vertices[v1]["x"], vertices[v1]["y"]) if v1 in vertices else None
            v2_pos = (vertices[v2]["x"], vertices[v2]["y"]) if v2 in vertices else None

            touches_kept = v1 in all_kept_verts or v2 in all_kept_verts
            if not touches_kept:
                for aid in kept:
                    for (px, py) in area_positions.get(aid, []):
                        if v1_pos and abs(v1_pos[0] - px) < POSITION_TOLERANCE_CM and abs(v1_pos[1] - py) < POSITION_TOLERANCE_CM:
                            touches_kept = True
                            break
                        if v2_pos and abs(v2_pos[0] - px) < POSITION_TOLERANCE_CM and abs(v2_pos[1] - py) < POSITION_TOLERANCE_CM:
                            touches_kept = True
                            break
                    if touches_kept:
                        break

            if touches_kept:
                for a_id, area in areas.items():
                    if a_id in kept:
                        continue
                    area_verts = set(area.get("vertices", []))
                    if v1 in area_verts or v2 in area_verts:
                        kept.add(a_id)
                        changed = True
                        reason = "Hole-Connected" if line_holes else "Hidden-Wall-Connected"
                        log_fn(
                            f"  ➕ Adding Neighbour Room ({reason}): "
                            f"'{area.get('name')}' ({a_id})"
                        )

    return kept


# ─────────────────────────────────────────────────────────────────────────────
# INTERIOR CULLING
# ─────────────────────────────────────────────────────────────────────────────

def _is_elevation_asset(item: dict) -> bool:
    """
    Check if an item is an elevation-type asset.
    Elevation assets are part of the building shell/facade and must NEVER be
    removed in any render mode — neither INTERIOR nor EXTERIOR.
    """
    item_type = str(item.get("type", "")).lower()
    item_name = str(item.get("name", "")).lower()
    full_desc = item_type + " " + item_name
    return any(kw in full_desc for kw in ELEVATION_ITEM_KEYWORDS)


def _is_exterior_asset(item: dict) -> bool:
    """
    Check if an item is an exterior-only asset by its name/type keywords.

    Guards against false positives:
      • If the name/type contains an INDOOR keyword (e.g. "Indoor Snake Plant"),
        it is NOT treated as exterior even if it matches an exterior keyword.
      • If the item's GLB path starts with a known exterior path prefix it IS
        exterior, regardless of indoor keyword match.
    """
    item_type = str(item.get("type", "")).lower()
    item_name = str(item.get("name", "")).lower()
    full_desc = item_type + " " + item_name

    # Check GLB path — items under Exterior/ folders are always exterior
    glb_url = str(item.get("asset_urls", {}).get("GLB_File_URL", "")).lower()
    if any(glb_url.startswith(pfx) for pfx in EXTERIOR_GLB_PATH_PREFIXES):
        return True

    # If it matches an indoor keyword, reject the exterior match
    if any(kw in full_desc for kw in INDOOR_ITEM_KEYWORDS):
        return False

    return any(kw in full_desc for kw in EXTERIOR_ITEM_KEYWORDS)


def _is_interior_furniture_asset(item: dict) -> bool:
    """
    Heuristic for indoor furniture/decor items that should stay in image renders
    even when their center point sits outside the camera's 2-D FOV cone.
    """
    item_type = str(item.get("type", "")).lower()
    item_name = str(item.get("name", "")).lower()
    full_desc = item_type + " " + item_name
    if _is_exterior_asset(item):
        return False
    return any(kw in full_desc for kw in INDOOR_ITEM_KEYWORDS)


def _item_altitude_m(item: dict, meters_per_unit: float = 0.01) -> float:
    """Return the item's altitude in metres (default 0)."""
    raw = item.get("properties", {}).get("altitude", item.get("altitude", 0))
    if isinstance(raw, dict):
        return float(raw.get("length", 0)) * meters_per_unit
    return float(raw) * meters_per_unit


# ─────────────────────────────────────────────────────────────────────────────
#  WALL TOPOLOGY REPAIR
# ─────────────────────────────────────────────────────────────────────────────
#  Some plans arrive with a wall that terminates on a *hosted* / T-junction
#  vertex sitting one wall-thickness away from the real room corner, instead of
#  on the corner vertex itself:
#
#      corner b263f7ee (7500, 2500)   ← area corner, junctionType "end", degree 1
#      stray  f3c37e4b (7728.6, 2500) ← isJunction:true, junctionType:"t",
#                                        hostedOn: {hostLineId: <west wall>}
#
#  The room polygon is then an open C-shape: three walls chain corner→corner and
#  the fourth dead-ends on the stray vertex.  Godot builds wall boxes straight
#  from `line["vertices"]` (it ignores `geometry.cleanedPolygon`), so that wall
#  is drawn short and the room is left open on that side — the camera sees
#  straight out into the HDRI.  The stray vertex is also the one vertex the
#  culler has to carry by hand, which is where the mm→cm unit slip used to bite.
#
#  This pass welds those dangling endpoints onto the corner they belong to and
#  rebuilds the affected wall polygons, so a plan shaped like the above renders
#  closed without anyone hand-editing the JSON.
# ─────────────────────────────────────────────────────────────────────────────

#: A dangling endpoint is welded only if it sits within this multiple of the
#: wall thickness of a real room corner.  1.6 covers the exact-one-thickness
#: case plus editor rounding, while staying far below any real wall length.
_WELD_THICKNESS_FACTOR = 1.6
#: Absolute floor for the weld radius, in source units, when a wall carries no
#: usable thickness (source units are mm for v2.0.0 plans, cm for older ones).
_WELD_MIN_RADIUS = 1.0


def _vertex_xy(vertex: Any) -> Optional[Tuple[float, float]]:
    if not isinstance(vertex, dict):
        return None
    try:
        return float(vertex.get("x", 0.0)), float(vertex.get("y", 0.0))
    except (TypeError, ValueError):
        return None


def _wall_thickness_source_units(line: dict) -> float:
    raw = (line.get("properties") or {}).get("thickness", 0.0)
    if isinstance(raw, dict):
        raw = raw.get("length", 0.0)
    try:
        return abs(float(raw))
    except (TypeError, ValueError):
        return 0.0


def _rebuild_wall_polygon_from_area(
    line: dict,
    v1_id: str,
    v2_id: str,
    areas: dict,
    vertices: dict,
) -> bool:
    """Rebuild a wall's raw/cleaned polygons from its parent area's polygons.

    The editor already stores the correctly mitred corner points on the area as
    `geometry.outerPolygon` / `geometry.innerPolygon`, index-aligned with
    `area["vertices"]`.  Every healthy wall in a plan follows the convention

        cleanedPolygon = [outer[i1], inner[i1], inner[i2], outer[i2]]

    so we reuse those points rather than re-deriving mitres ourselves.
    Returns True when the polygons were rebuilt.
    """
    for area in areas.values():
        if not isinstance(area, dict):
            continue
        area_vids = [str(v) for v in area.get("vertices", [])]
        if v1_id not in area_vids or v2_id not in area_vids:
            continue

        geo = area.get("geometry") or {}
        outer = geo.get("outerPolygon")
        inner = geo.get("innerPolygon")
        if not (isinstance(outer, list) and isinstance(inner, list)):
            continue
        if len(outer) != len(area_vids) or len(inner) != len(area_vids):
            continue

        i1 = area_vids.index(v1_id)
        i2 = area_vids.index(v2_id)

        # Only trust the index alignment if outerPolygon[i] really is vertex i.
        aligned = True
        for idx, vid in ((i1, v1_id), (i2, v2_id)):
            vxy = _vertex_xy(vertices.get(vid))
            oxy = _vertex_xy(outer[idx])
            if vxy is None or oxy is None or math.hypot(vxy[0] - oxy[0], vxy[1] - oxy[1]) > 1.0:
                aligned = False
                break
        if not aligned:
            continue

        o1, n1 = dict(outer[i1]), dict(inner[i1])
        o2, n2 = dict(outer[i2]), dict(inner[i2])

        # Un-mitred rectangle: offset the outer edge toward the room interior.
        thickness = _wall_thickness_source_units(line)
        dx, dy = o2["x"] - o1["x"], o2["y"] - o1["y"]
        seg = math.hypot(dx, dy)
        if seg > 1e-9 and thickness > 0.0:
            nx, ny = -dy / seg, dx / seg
            # Point the normal at the interior, using the mitred inner corner.
            if ((n1["x"] - o1["x"]) * nx + (n1["y"] - o1["y"]) * ny) < 0.0:
                nx, ny = -nx, -ny
            raw = [
                {"x": o1["x"], "y": o1["y"]},
                {"x": o1["x"] + nx * thickness, "y": o1["y"] + ny * thickness},
                {"x": o2["x"] + nx * thickness, "y": o2["y"] + ny * thickness},
                {"x": o2["x"], "y": o2["y"]},
            ]
        else:
            raw = [o1, dict(n1), dict(n2), o2]

        geometry = line.setdefault("geometry", {})
        geometry["rawPolygon"] = raw
        geometry["cleanedPolygon"] = [o1, n1, n2, o2]
        geometry["dirty"] = False
        return True

    # No parent area to derive from — let the consumer recompute.
    line.setdefault("geometry", {})["dirty"] = True
    return False


def _repair_wall_topology(fp_data: dict, log_fn=None) -> int:
    """Weld dangling wall endpoints onto the room corner they belong to.

    Mutates ``fp_data`` in place.  Returns the number of endpoints welded.
    Safe to run on healthy plans: if no endpoint dangles, nothing changes.
    """
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    layers = fp_data.get("layers")
    if not isinstance(layers, dict):
        return 0

    welded_total = 0

    for layer_id, layer in layers.items():
        if not isinstance(layer, dict):
            continue
        vertices = layer.get("vertices")
        lines = layer.get("lines")
        if not isinstance(vertices, dict) or not isinstance(lines, dict):
            continue
        areas = layer.get("areas") if isinstance(layer.get("areas"), dict) else {}
        external_areas = (
            layer.get("externalAreas") if isinstance(layer.get("externalAreas"), dict) else {}
        )

        # Corner vertices are the ones an area polygon is actually built from.
        corner_ids: Set[str] = set()
        for bucket in (areas, external_areas):
            for area in bucket.values():
                if isinstance(area, dict):
                    corner_ids.update(str(v) for v in area.get("vertices", []))
        corner_ids = {vid for vid in corner_ids if vid in vertices}
        if not corner_ids:
            continue

        # Endpoint vertices that are not corners are weld candidates.
        endpoint_lines: Dict[str, List[str]] = {}
        for lid, line in lines.items():
            if not isinstance(line, dict):
                continue
            v_ids = line.get("vertices") or []
            if len(v_ids) < 2:
                continue
            for vid in (str(v_ids[0]), str(v_ids[1])):
                endpoint_lines.setdefault(vid, []).append(lid)

        remap: Dict[str, str] = {}
        for vid, owning_lines in endpoint_lines.items():
            if vid in corner_ids or vid not in vertices:
                continue
            stray_xy = _vertex_xy(vertices.get(vid))
            if stray_xy is None:
                continue

            radius = _WELD_MIN_RADIUS
            for lid in owning_lines:
                t = _wall_thickness_source_units(lines.get(lid, {}))
                if t > 0.0:
                    radius = max(radius, t * _WELD_THICKNESS_FACTOR)

            best_id, best_dist = None, radius
            for cid in corner_ids:
                cxy = _vertex_xy(vertices.get(cid))
                if cxy is None:
                    continue
                d = math.hypot(stray_xy[0] - cxy[0], stray_xy[1] - cxy[1])
                if d <= best_dist:
                    best_id, best_dist = cid, d
            if best_id is None:
                continue

            # Never collapse a wall onto itself.
            collapses = False
            for lid in owning_lines:
                other = [str(v) for v in lines[lid].get("vertices", [])[:2]]
                if len(other) == 2 and best_id in other:
                    collapses = True
                    break
            if collapses:
                continue

            remap[vid] = best_id
            _log(
                f"  🔧 Wall topology: endpoint {vid} at "
                f"({stray_xy[0]:.1f}, {stray_xy[1]:.1f}) dangles "
                f"{best_dist:.1f} source-units off corner {best_id} — welding."
            )

        if not remap:
            continue

        touched_lines: Set[str] = set()
        for lid, line in lines.items():
            v_ids = line.get("vertices") or []
            if len(v_ids) < 2:
                continue
            new_v = [remap.get(str(v_ids[0]), str(v_ids[0])), remap.get(str(v_ids[1]), str(v_ids[1]))]
            if new_v != [str(v_ids[0]), str(v_ids[1])]:
                line["vertices"] = new_v + list(v_ids[2:])
                touched_lines.add(lid)

        # Any wall meeting a welded corner needs its polygons re-derived, not
        # just the wall that moved — the neighbour was mitred against the old,
        # square-cut corner.
        welded_corners = set(remap.values())
        for lid, line in lines.items():
            v_ids = [str(v) for v in (line.get("vertices") or [])[:2]]
            if len(v_ids) == 2 and (welded_corners & set(v_ids)):
                touched_lines.add(lid)

        for lid in touched_lines:
            line = lines[lid]
            v_ids = [str(v) for v in (line.get("vertices") or [])[:2]]
            if len(v_ids) == 2:
                _rebuild_wall_polygon_from_area(line, v_ids[0], v_ids[1], areas, vertices)

        # Drop the now-orphaned stray vertices and re-classify the corners they
        # merged into (a corner that was degree-1 "end" is now a real corner).
        referenced: Set[str] = set(corner_ids)
        for line in lines.values():
            for v in (line.get("vertices") or [])[:2]:
                referenced.add(str(v))
        for vid in list(remap):
            if vid not in referenced:
                vertices.pop(vid, None)

        for cid in welded_corners:
            degree = sum(
                1
                for line in lines.values()
                for v in (line.get("vertices") or [])[:2]
                if str(v) == cid
            )
            cv = vertices.get(cid)
            if isinstance(cv, dict) and degree == 2 and cv.get("junctionType") == "end":
                cv["junctionType"] = "l"
                cv["isJunction"] = False

        welded_total += len(remap)
        _log(
            f"  ✅ Wall topology repaired on layer {layer_id}: "
            f"{len(remap)} endpoint(s) welded, {len(touched_lines)} wall polygon(s) rebuilt."
        )

    return welded_total


def _cull_interior(
    layer_id: str,
    active_area_id: str,
    vertices: dict,
    lines: dict,
    areas: dict,
    items: dict,
    holes: dict,
    log_fn,
    cam_x: float = 0.0,
    cam_y: float = 0.0,
    cam_dir_x: float = 1.0,
    cam_dir_y: float = 0.0,
    cam_target_x: Optional[float] = None,
    cam_target_y: Optional[float] = None,
    cam_height_m: float = 0.0,
    fov_half_deg: float = DEFAULT_FOV_HALF_DEG,
    max_view_dist_cm: float = DEFAULT_VIEW_DISTANCE_CM,
    meters_per_unit: float = 0.01,
    cm_per_unit: float = 1.0,
    source_vertices: Optional[dict] = None,
    source_items: Optional[dict] = None,
    strict_room_limit: int = 0,
    external_areas: Optional[dict] = None,
) -> Tuple[dict, dict, dict, dict, dict]:
    """
    INTERIOR MODE — camera is inside a room.  Portal-based precision culling.

    ┌─────────────────────────────────────────────────────────────────────┐
    │  RULE: Only create what the camera can literally see through         │
    │  the geometry.                                                       │
    │                                                                      │
    │  • Active room  →  ALWAYS created (camera is inside it)             │
    │  • Any other room  →  created ONLY if there is a door/window        │
    │    opening in the active room's walls whose opening falls inside     │
    │    the camera FOV and is within max_view_dist_cm                     │
    │  • No opening in FOV  →  only the active room is created            │
    │  • No openings at all  →  only the active room is created           │
    └─────────────────────────────────────────────────────────────────────┘

    This replaces the old BFS flood-fill which kept ALL connected rooms
    regardless of camera direction, generating far too much geometry.

    Keep:
      • Active room walls, floor, ceiling, items
      • ONLY the rooms directly behind portals (doors/windows) that are
        inside the camera's FOV cone
      • Walls shared between active room and a kept neighbour room
      • Holes (door/window assets) on kept walls
      • Items inside kept rooms + 85 cm rescue tolerance

    Remove:
      • Every room not directly visible through an in-FOV portal
      • All walls, holes, items belonging to removed rooms
      • Exterior-only items (roof, grill, balcony, etc.)
    """
    log_fn(f"  ── Interior Culling for {layer_id} [PORTAL MODE] ──")
    log_fn(
        f"  📷 Pos=({cam_x:.0f},{cam_y:.0f})cm  "
        f"Dir=({cam_dir_x:.3f},{cam_dir_y:.3f})  "
        f"FOV=±{fov_half_deg:.0f}°+{PORTAL_ANGLE_MARGIN_DEG:.0f}°margin  "
        f"MaxDist={max_view_dist_cm:.0f}cm"
    )

    fov_half_rad = math.radians(fov_half_deg)
    camera_segment = _camera_view_segment_2d(
        cam_x,
        cam_y,
        cam_dir_x,
        cam_dir_y,
        cam_target_x,
        cam_target_y,
        max_view_dist_cm,
    )
    log_fn(
        f"  👁️ Camera blocker segment: "
        f"({camera_segment[0][0]:.1f}, {camera_segment[0][1]:.1f}) -> "
        f"({camera_segment[1][0]:.1f}, {camera_segment[1][1]:.1f})"
    )

    # ── Step 1: Portal sweep — find rooms visible through active room's openings ─
    neighbour_ids = _collect_portal_visible_rooms(
        active_area_id, areas, lines, holes, vertices,
        cm_per_unit,
        cam_x, cam_y, cam_dir_x, cam_dir_y,
        fov_half_rad, max_view_dist_cm,
        log_fn,
        strict_room_limit=strict_room_limit or STRICT_IMAGE_PORTAL_ROOM_LIMIT,
    )

    # ── Step 1b: Second-depth sweep — rooms visible through neighbour portals ──
    # Sweep from each first-depth neighbour using a wider FOV margin so that
    # rooms beyond a corridor or hallway are also created (geometry-only).
    # We use the camera position as the sweep origin so distance limits are
    # still camera-relative, but we sweep from the neighbour room's vertex set
    # so the portal-touch check succeeds for its walls.
    depth2_ids: Set[str] = set()
    for n_aid in neighbour_ids:
        # Compute the neighbour room's centroid as a proxy view direction
        n_area = areas.get(n_aid, {})
        n_verts = [vertices.get(str(v)) for v in n_area.get("vertices", []) if vertices.get(str(v))]
        if not n_verts:
            continue
        n_cx = sum(float(v["x"]) for v in n_verts) / len(n_verts)
        n_cy = sum(float(v["y"]) for v in n_verts) / len(n_verts)
        # Direction from camera toward that room's centroid
        n_dx = n_cx - cam_x
        n_dy = n_cy - cam_y
        n_dist = math.hypot(n_dx, n_dy)
        if n_dist < 1e-6:
            continue
        n_dir_x = n_dx / n_dist
        n_dir_y = n_dy / n_dist

        d2 = _collect_portal_visible_rooms(
            n_aid, areas, lines, holes, vertices,
            cm_per_unit,
            cam_x, cam_y, n_dir_x, n_dir_y,
            fov_half_rad, max_view_dist_cm,
            log_fn,
            strict_room_limit=strict_room_limit or STRICT_IMAGE_PORTAL_ROOM_LIMIT,
        )
        # Only add rooms not already kept; exclude the active room itself
        new_d2 = d2 - {active_area_id} - neighbour_ids
        if new_d2:
            log_fn(f"  🔭 Depth-2 sweep from '{n_area.get('name', n_aid)}' found: "
                   f"{[areas.get(a, {}).get('name', a) for a in new_d2]}")
        depth2_ids.update(new_d2)

    all_neighbour_ids = neighbour_ids | depth2_ids

    # Active room is ALWAYS kept
    kept_area_ids: Set[str] = {active_area_id} | all_neighbour_ids

    # ── Asset depth limit ─────────────────────────────────────────────────────
    # ALL portal-visible rooms get geometry (walls/floors) so openings are not
    # rendered as voids. But assets (furniture, decor) are only loaded for the
    # active room plus the nearest ASSET_DEPTH_LIMIT portal-neighbours.
    # Neighbours are ordered by angle to camera direction (most centred first).
    asset_area_ids: Set[str] = {active_area_id}
    if neighbour_ids:
        def _neighbour_angle(aid: str) -> float:
            area = areas.get(aid, {})
            verts = [
                vertices.get(str(v)) for v in area.get("vertices", [])
                if vertices.get(str(v))
            ]
            if not verts:
                return 180.0
            cx = sum(float(v["x"]) for v in verts) / len(verts)
            cy = sum(float(v["y"]) for v in verts) / len(verts)
            return math.degrees(_angle_to_point(cam_x, cam_y, cam_dir_x, cam_dir_y, cx, cy))

        sorted_neighbours = sorted(neighbour_ids, key=_neighbour_angle)
        asset_depth = ASSET_DEPTH_LIMIT
        asset_area_ids.update(sorted_neighbours[:asset_depth])
        
        # Always load assets for outdoor-style portal-connected areas (like balconies)
        # because they are visually important from windows and usually have very few items.
        for nid in sorted_neighbours:
            n_area = areas.get(nid, {})
            n_label = str(n_area.get("properties", {}).get("label", "")).lower()
            n_name = str(n_area.get("name", "")).lower()
            if any(kw in n_label or kw in n_name for kw in ["balcony", "patio", "terrace", "deck", "outdoor", "porch", "loggia"]):
                asset_area_ids.add(nid)

    log_fn(f"  🎯 Kept rooms on {layer_id}: {len(kept_area_ids)} "
           f"(active + {len(neighbour_ids)} portal-visible, assets in {len(asset_area_ids)})")
    for aid in kept_area_ids:
        has_assets = aid in asset_area_ids
        tag = "📍ACTIVE" if aid == active_area_id else ("🚪PORTAL+ASSETS" if has_assets else "🚪PORTAL-SHELL")
        log_fn(f"     {tag} {areas[aid].get('name', 'Unknown')} ({aid})")

    culled_room_count = len(areas) - len(kept_area_ids)
    if culled_room_count > 0:
        log_fn(f"  🗑️  Culled {culled_room_count} rooms not visible from camera")


    # ── Step 2: Filter areas ─────────────────────────────────────────────────
    new_areas = {aid: areas[aid] for aid in kept_area_ids}

    # ── Step 3: Compute kept vertex set ──────────────────────────────────────
    kept_vertex_ids: Set[str] = set()
    for area in new_areas.values():
        kept_vertex_ids.update(str(v) for v in area.get("vertices", []))
    if external_areas:
        for ex_area in external_areas.values():
            kept_vertex_ids.update(str(v) for v in ex_area.get("vertices", []))

    kept_positions: Set[Tuple] = set()
    for vid in kept_vertex_ids:
        v = vertices.get(vid)
        if v:
            kept_positions.add((round(float(v["x"]), 0), round(float(v["y"]), 0)))

    def vertex_is_near_kept(vid: str) -> bool:
        if str(vid) in kept_vertex_ids:
            return True
        v = vertices.get(str(vid))
        if v:
            vx, vy = float(v["x"]), float(v["y"])
            if (round(vx, 0), round(vy, 0)) in kept_positions:
                return True
            # Also check if it's within a reasonable wall thickness tolerance (1000 units = 100cm)
            for kx, ky in kept_positions:
                if math.hypot(vx - kx, vy - ky) <= 1000.0:
                    return True
        return False

    vertex_source = source_vertices if isinstance(source_vertices, dict) else vertices
    new_vertices = {vid: vertex_source[vid] for vid in kept_vertex_ids if vid in vertex_source}

    def _keep_vertices(*vertex_ids: str) -> None:
        """Add wall endpoints to the surviving vertex set.

        UNITS: `vertices` here is the CULLING copy — `_transform_layer_for_culling()`
        has already multiplied it by `cm_per_unit` and applied the plan rotation.
        The optimised plan we hand back to Godot must stay in SOURCE units, so
        every vertex we emit has to come from `vertex_source`, never from
        `vertices`.

        Copying from `vertices` here silently divided mm-based (version 2.0.0)
        plans by 10.  It only ever hit vertices that are NOT area corners —
        T-junction / `hostedOn` vertices — because corner vertices are already
        seeded into `new_vertices` from `vertex_source` above.  Symptom: the one
        wall that ends on a T-junction is drawn running off toward the plan
        origin, leaving the room open on that side.
        """
        for vid in vertex_ids:
            if vid in new_vertices:
                continue
            src = vertex_source.get(vid)
            if src is None:
                continue
            new_vertices[vid] = src
            kept_vertex_ids.add(vid)

    # ── Step 4: Filter lines (walls) ─────────────────────────────────────────
    # Keep the active room shell intact. Non-active walls can still be culled
    # by distance / FOV so we do not keep the whole building when the camera
    # only sees part of it.
    new_lines: Dict[str, Any] = {}
    culled_walls: List[str] = []
    
    # Use a slightly wider margin for walls than for items to ensure corners 
    # and door frames don't clip at the edges of the view.
    wall_fov_margin_rad = math.radians(fov_half_deg + 20.0)

    active_area = areas.get(active_area_id)
    active_room_vertex_ids: Set[str] = set()
    active_room_positions: Set[Tuple[float, float]] = set()
    # Pre-build the ordered list of active-room corner positions for edge queries.
    # T-junction vertices (junctions that sit ON an edge but are not corners) will
    # be matched by _point_to_segment_distance_2d against these edges.
    active_room_edges: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    if active_area:
        active_room_vertex_ids = set(str(v) for v in active_area.get("vertices", []))
        for vid in active_room_vertex_ids:
            v = vertices.get(vid)
            if v:
                active_room_positions.add((round(float(v["x"]), 0), round(float(v["y"]), 0)))
        # Build corner list in order and derive edges
        corner_vids = [str(v) for v in active_area.get("vertices", [])]
        corner_pts = []
        for vid in corner_vids:
            v = vertices.get(vid)
            if v:
                corner_pts.append((float(v["x"]), float(v["y"])))
        if len(corner_pts) >= 2:
            for i in range(len(corner_pts)):
                active_room_edges.append((corner_pts[i], corner_pts[(i + 1) % len(corner_pts)]))

    # Pre-build set of hole line IDs for quick lookup
    hole_line_ids: Set[str] = set()
    for h in holes.values():
        ref = str(h.get("line", ""))
        if ref:
            hole_line_ids.add(ref)

    # Tolerance in source units: T-junction vertices lie exactly on an edge,
    # but allow 1 cm of rounding slack to be safe.
    _EDGE_SNAP_TOL = 1.0 / cm_per_unit  # 1 cm in source units

    def wall_belongs_to_active_room(v1_id: str, v2_id: str) -> bool:
        """True only when BOTH endpoints are active-room vertices (or lie on
        an active-room boundary edge — handles T-junction points).

        A wall where only ONE endpoint is in the active room is an exterior
        stub that hangs off a corner (e.g. the short wall beside a door
        opening that extends outward).  Including it always creates a phantom
        wall visible in the render.  We deliberately exclude those stubs here;
        they are re-evaluated below by the normal distance/FOV path, where
        they will be kept only if they have a hole on them.
        """
        def _in_room(vid: str) -> bool:
            if vid in active_room_vertex_ids:
                return True
            v = vertices.get(vid)
            if v:
                vx, vy = float(v["x"]), float(v["y"])
                if (round(vx, 0), round(vy, 0)) in active_room_positions:
                    return True
                # T-junction check: vertex lies ON one of the active room's boundary edges
                for (ax, ay), (bx, by) in active_room_edges:
                    if _point_to_segment_distance_2d(vx, vy, ax, ay, bx, by) <= _EDGE_SNAP_TOL:
                        return True
                # Tolerance check for thick walls where the endpoint extends slightly past the room corner
                for rx, ry in active_room_positions:
                    if math.hypot(vx - rx, vy - ry) <= 30.0:
                        return True
            return False
        return _in_room(v1_id) and _in_room(v2_id)

    for lid, line in lines.items():
        v_ids = line.get("vertices", [])
        if len(v_ids) < 2:
            continue
        v1h, v2h = str(v_ids[0]), str(v_ids[1])
        
        # The active room shell must always survive culling.  If we let the
        # camera-ray blocker run first, it can delete the wall the viewer is
        # actually looking at, which leaves the room open and visually broken.
        if wall_belongs_to_active_room(v1h, v2h):
            new_lines[lid] = line
            _keep_vertices(v1h, v2h)
            continue

        # A one-sided stub (only one vertex in active room) that has a hole
        # (door/window) on it is part of the doorframe and must be kept.
        # Without it the opening asset floats with no surrounding wall.
        v1_in = (v1h in active_room_vertex_ids) or (
            (round(float(vertices[v1h]["x"]), 0), round(float(vertices[v1h]["y"]), 0))
            in active_room_positions
            if vertices.get(v1h) else False
        )
        v2_in = (v2h in active_room_vertex_ids) or (
            (round(float(vertices[v2h]["x"]), 0), round(float(vertices[v2h]["y"]), 0))
            in active_room_positions
            if vertices.get(v2h) else False
        )
        if (v1_in or v2_in) and lid in hole_line_ids:
            # Stub wall that carries a door/window — keep it (door frame)
            new_lines[lid] = line
            _keep_vertices(v1h, v2h)
            continue

        # Stub wall with no hole — treat as exterior, fall through to normal
        # distance/FOV culling below (will almost certainly be dropped).

        # Check if wall touches a kept room
        if not (vertex_is_near_kept(v1h) and vertex_is_near_kept(v2h)):
            culled_walls.append(f"Wall {lid} [not touching kept rooms]")
            continue
            
        # Get vertex positions
        vd1 = vertices.get(v1h)
        vd2 = vertices.get(v2h)
        if not vd1 or not vd2:
            continue
            
        p1x, p1y = float(vd1["x"]), float(vd1["y"])
        p2x, p2y = float(vd2["x"]), float(vd2["y"])

        wall_thickness_cm = _source_length_to_cm(
            line.get("properties", {}).get("thickness", 20.0),
            cm_per_unit,
        )
        wall_block_tol_cm = max(8.0, wall_thickness_cm * 0.5)
        # Fix: only cull if the camera point itself is too close to the wall (stuck inside)
        dist_to_cam = _point_to_segment_distance_2d(cam_x, cam_y, p1x, p1y, p2x, p2y)
        if dist_to_cam <= wall_block_tol_cm:
            culled_walls.append(
                f"Wall {lid} [camera is stuck inside wall (dist {dist_to_cam:.1f}cm)]"
            )
            continue

        # Midpoint for distance check
        mx, my = (p1x + p2x) / 2.0, (p1y + p2y) / 2.0
        dist = math.hypot(mx - cam_x, my - cam_y)
        
        if dist > max_view_dist_cm:
            culled_walls.append(f"Wall {lid} [dist {dist:.0f}cm > {max_view_dist_cm:.0f}cm]")
            continue

        # FOV check using segment-sampling helper
        if not _portal_visible_in_fov(
            cam_x, cam_y, cam_dir_x, cam_dir_y, 
            wall_fov_margin_rad,
            p1x, p1y, p2x, p2y
        ):
            angle_deg = math.degrees(_angle_to_point(cam_x, cam_y, cam_dir_x, cam_dir_y, mx, my))
            culled_walls.append(f"Wall {lid} [outside FOV sector: {angle_deg:.1f}°]")
            continue

        # Wall IS visible and connected
        new_lines[lid] = line
        _keep_vertices(v1h, v2h)

    if culled_walls:
        log_fn(f"\n  🗑️  Culled {len(culled_walls)} walls from {layer_id}:")
        for cw in culled_walls:
            log_fn(f"     - {cw}")

    # ── Step 5: Filter holes ─────────────────────────────────────────────────
    new_holes: Dict[str, Any] = {}
    culled_holes_count = 0
    for hid, hole in holes.items():
        line_ref = str(hole.get("line"))
        if line_ref in new_lines:
            new_holes[hid] = hole
        else:
            culled_holes_count += 1
    
    if culled_holes_count:
        log_fn(f"  🗑️  Culled {culled_holes_count} holes (doors/windows) due to wall removal.")

    # ── Step 6: Filter items ─────────────────────────────────────────────────
    new_items: Dict[str, Any] = {}
    culled_items: List[str] = []
    image_item_view_margin_deg = STRICT_IMAGE_ITEM_VIEW_MARGIN_DEG
    image_item_rescue_tolerance_cm = STRICT_IMAGE_ITEM_RESCUE_TOLERANCE_CM

    for iid, item in items.items():
        item_type = str(item.get("type", "")).lower()
        item_name = str(item.get("name", "")).lower()

        # Always keep floor/slab assets regardless of room
        if any(kw in item_type or kw in item_name for kw in FLOOR_ITEM_KEYWORDS):
            new_items[iid] = item
            log_fn(f"  🛡️  Floor asset kept: '{item.get('name')}' ({iid})")
            continue

        # Always keep elevation assets — building shell, never culled in any mode
        if _is_elevation_asset(item):
            new_items[iid] = item
            log_fn(f"  🏛️  Elevation asset kept (never culled): '{item.get('name')}' ({iid})")
            continue

        # Remove exterior-only assets unless use_showall is True in interior mode
        # Exception: if the exterior asset is explicitly placed inside an active/portal room, keep it.
        if _is_exterior_asset(item) and not use_showall:
            ix, iy = float(item.get("x", 0)), float(item.get("y", 0))
            assigned_room = None
            for aid in asset_area_ids:
                area = areas.get(aid, {})
                poly = []
                for v_id in area.get("vertices", []):
                    v = vertices.get(str(v_id))
                    if v:
                        poly.append((float(v["x"]), float(v["y"])))
                if len(poly) >= 3 and _point_in_polygon(ix, iy, poly):
                    assigned_room = aid
                    break
            
            if assigned_room:
                pass # Keep exterior item because it is placed inside a rendered room
            else:
                culled_items.append(f"{item.get('name', 'Unknown')} ({iid}) [exterior]")
                continue

        ix, iy = float(item.get("x", 0)), float(item.get("y", 0))
        item_samples = _item_plan_sample_points_cm(item, cm_per_unit)
        camera_sample = {
            "cam_x": cam_x,
            "cam_y": cam_y,
            "cam_dir_x": cam_dir_x,
            "cam_dir_y": cam_dir_y,
            "fov_half_deg": fov_half_deg,
        }
        visible_samples = [
            (sx, sy)
            for sx, sy in item_samples
            if _point_visible_from_sample(
                camera_sample,
                sx,
                sy,
                max_view_dist_cm,
                image_item_view_margin_deg,
            )
        ]

        item_alt_m = _item_altitude_m(item, meters_per_unit)
        item_radius_cm = max(_item_footprint_radius_cm(item, cm_per_unit), 25.0)
        dist_to_cam = math.hypot(ix - cam_x, iy - cam_y)

        # If the camera sits inside the item's footprint, only cull it when
        # none of the sampled footprint points still fall inside the view cone.
        if item_alt_m <= cam_height_m + 1.25 and dist_to_cam <= item_radius_cm and not visible_samples:
            culled_items.append(
                f"{item.get('name', 'Unknown')} ({iid}) [camera is stuck inside item (dist {dist_to_cam:.1f}cm)]"
            )
            continue

        # Keep items when any sampled point from their footprint overlaps the
        # camera cone. This avoids dropping large sofas/carpets when the item
        # origin sits just outside the active room polygon.
        if not visible_samples:
            dist = math.hypot(ix - cam_x, iy - cam_y)
            angle_deg = math.degrees(
                _angle_to_point(cam_x, cam_y, cam_dir_x, cam_dir_y, ix, iy)
            )
            if dist > max_view_dist_cm:
                culled_items.append(
                    f"{item.get('name', 'Unknown')} ({iid}) [out of {max_view_dist_cm/100:.0f}m range: {dist:.0f}cm]"
                )
            else:
                culled_items.append(
                    f"{item.get('name', 'Unknown')} ({iid}) [outside FOV sector: {angle_deg:.1f}°]"
                )
            continue

        # Pass 1 — footprint-aware room containment
        # Use asset_area_ids (not kept_area_ids) so assets are only kept for rooms
        # within ASSET_DEPTH_LIMIT portal hops. Rooms beyond get geometry but no items.
        kept = False
        rescue_tol_cm = min(image_item_rescue_tolerance_cm, ITEM_RESCUE_TOLERANCE_CM)
        for aid in asset_area_ids:
            area = areas.get(aid)
            if not area:
                continue
            poly = [(float(vertices[str(v)]["x"]), float(vertices[str(v)]["y"]))
                    for v in area.get("vertices", []) if str(v) in vertices]
            if not poly:
                continue

            sample_in_poly = any(_point_in_polygon(sx, sy, poly) for sx, sy in item_samples)
            sample_near_poly = any(_min_dist_to_polygon(sx, sy, poly) <= rescue_tol_cm for sx, sy in item_samples)

            if aid == active_area_id:
                if sample_in_poly or sample_near_poly:
                    kept = True
                    log_fn(f"  ✓ Item in active room: '{item.get('name', 'Unknown')}' ({iid})")
                    break
            else:
                if sample_in_poly:
                    kept = True
                    log_fn(f"  ✓ Item in room: '{item.get('name', 'Unknown')}' ({iid})")
                    break

        if kept:
            new_items[iid] = item
            continue

        # Pass 2 — proximity rescue (items clipped into walls, ≤85 cm)
        # Also uses asset_area_ids to maintain depth-limited asset loading.
        rescued = False
        for aid in asset_area_ids:
            area = areas.get(aid)
            if not area:
                continue
            poly = [(float(vertices[str(v)]["x"]), float(vertices[str(v)]["y"]))
                    for v in area.get("vertices", []) if str(v) in vertices]
            if not poly:
                continue
            d = min((_min_dist_to_polygon(sx, sy, poly) for sx, sy in item_samples), default=float("inf"))
            if d <= rescue_tol_cm:
                log_fn(f"  ✨ Rescue item '{item.get('name', 'Unknown')}' ({iid}) dist={d:.0f}cm")
                new_items[iid] = item
                rescued = True
                break

        if not rescued:
            culled_items.append(f"{item.get('name', 'Unknown')} ({iid})")

    if culled_items:
        log_fn(f"\n  🗑️  Culled {len(culled_items)} items from {layer_id}:")
        for ci in culled_items:
            log_fn(f"     - {ci}")

    item_source = source_items if isinstance(source_items, dict) else items
    if isinstance(item_source, dict):
        final_items = {
            iid: item_source[iid]
            for iid in new_items.keys()
            if iid in item_source
        }
    else:
        final_items = new_items

    return new_vertices, new_lines, new_areas, final_items, new_holes


# ─────────────────────────────────────────────────────────────────────────────
# EXTERIOR CULLING
# ─────────────────────────────────────────────────────────────────────────────

def _cull_exterior(
    layer_id: str,
    vertices: dict,
    lines: dict,
    areas: dict,
    items: dict,
    holes: dict,
    log_fn,
    layer_alt_m: float = 0.0,
    layer_top_m: float = 999.0,
    meters_per_unit: float = 0.01,
    source_vertices: Optional[dict] = None,
    source_items: Optional[dict] = None,
    external_areas: Optional[dict] = None,
) -> Tuple[dict, dict, dict, dict, dict]:
    """
    EXTERIOR MODE — camera is completely outside all rooms.

    Keep:
      • ALL walls (building shell visible from outside)
      • ALL areas (floor/ceiling geometry needed for structural mesh)
      • ALL holes (doors/windows) — marked with is_exterior_black=true
        so the renderer fills openings with BLACK (no interior visible)
      • Elevation-type items (always kept, never removed in any mode)
      • Exterior-named items (roof, grill, balcony, etc.) — by keyword match,
        with indoor-keyword exclusion guard and GLB path check
      • Floor assets always

    Remove:
      • ALL non-exterior items, including anything inside a room polygon and
        anything outside room polygons if it is not explicitly exterior
        (sofas, fridges, lights, frames, pictures, mirrors, carpets, cabinets)
      • Altitude-aware: items whose altitude places them outside this layer's
        vertical range are NOT used for polygon culling (they belong elsewhere)

    All holes (doors/windows/openings) are flagged is_exterior_black=true
    so the Godot renderer places a black blocker in the opening. This is
    purely geometry-based — no name matching is used for holes.
    """
    log_fn(f"  ── Exterior Culling for {layer_id} "
           f"[alt={layer_alt_m:.2f}m .. {layer_top_m:.2f}m] ──")

    # Keep all architectural geometry
    new_areas    = dict(areas)
    new_lines    = dict(lines)
    vertex_source = source_vertices if isinstance(source_vertices, dict) else vertices
    new_vertices = dict(vertex_source)

    log_fn(f"  ✓ Keeping ALL {len(new_areas)} areas  (structural mesh)")
    log_fn(f"  ✓ Keeping ALL {len(new_lines)} walls  (building shell)")

    for aid, area in new_areas.items():
        log_fn(f"     ✓ Area: {area.get('name', 'Unknown')} ({aid})")

    # ── Mark ALL holes as exterior-black ──────────────────────────────────────
    new_holes = {}
    for hid, hole in holes.items():
        hole_copy = dict(hole)
        hole_copy["is_exterior_black"] = True
        new_holes[hid] = hole_copy
        log_fn(f"     ⚫ Hole marked BLACK: ({hole.get('type','?')}) {hole.get('name', hid)}")

    log_fn(f"  ⚫ Marked ALL {len(new_holes)} holes as is_exterior_black=true")

    # ── Filter items ─────────────────────────────────────────────────────────
    new_items:           Dict[str, Any] = {}
    culled_items:        List[str]      = []
    kept_exterior_items: List[str]      = []

    for iid, item in items.items():
        item_type = str(item.get("type", "")).lower()
        item_name = str(item.get("name", "")).lower()
        ix, iy    = item.get("x", 0), item.get("y", 0)

        # Always keep floor/slab assets
        if any(kw in item_type or kw in item_name for kw in FLOOR_ITEM_KEYWORDS):
            new_items[iid] = item
            log_fn(f"  🛡️ Preserving Floor Asset: '{item.get('name')}' ({iid})")
            continue

        # Always keep elevation assets — building shell, never culled
        if _is_elevation_asset(item):
            new_items[iid] = item
            log_fn(f"  🏛️ Preserving Elevation Asset (never culled): '{item.get('name')}' ({iid})")
            continue

        # Keep exterior-named assets (roof, grill, balcony, etc.)
        # Indoor-keyword guard and GLB-path check are inside _is_exterior_asset
        # ── Altitude-aware inside-room test ───────────────────────────────────
        # Items whose altitude places them ABOVE this layer's ceiling do NOT
        # belong to this layer's rooms — treat them as outside (keep them).
        # This prevents upper-floor items being culled by ground-floor polygons
        # when showAllFloors=True causes all layers to process simultaneously.
        item_alt_m = _item_altitude_m(item, meters_per_unit)
        # Items placed above the layer ceiling clearly don't belong here
        layer_ceiling_m = layer_top_m - layer_alt_m  # relative ceiling height
        if item_alt_m > layer_ceiling_m + 0.1:
            new_items[iid] = item
            kept_exterior_items.append(
                f"{item.get('name', 'Unknown')} ({iid}) [altitude {item_alt_m:.2f}m > layer ceiling]"
            )
            log_fn(
                f"  📐 Keeping item above layer ceiling: "
                f"'{item.get('name', 'Unknown')}' ({iid}) alt={item_alt_m:.2f}m"
            )
            continue

        cm_per_unit = meters_per_unit * 100.0
        item_samples = _item_plan_sample_points_cm(item, cm_per_unit)
        inside_room = any(_point_in_any_area(sx, sy, areas, vertices) for sx, sy in item_samples)
        inside_external_area = False
        if external_areas and isinstance(external_areas, dict):
            inside_external_area = any(
                _point_in_any_area(sx, sy, external_areas, vertices)
                for sx, sy in item_samples
            )

        # 1. Cull if it's inside a room and not explicitly an exterior asset.
        # (This prevents indoor items from being kept just because a lawn polygon overlaps the house).
        if inside_room and not _is_exterior_asset(item):
            culled_items.append(f"{item.get('name', 'Unknown')} ({iid}) [inside room, non-exterior]")
            log_fn(f"  🗑️ Culling inside-room item: '{item.get('name', 'Unknown')}' ({iid})")
            continue

        # 2. Cull explicit interior items (e.g. sofas, beds) placed in neutral space.
        # We KEEP them if they are explicitly inside an external area (e.g. patio furniture on a patio).
        if _is_interior_furniture_asset(item) and not inside_external_area:
            culled_items.append(f"{item.get('name', 'Unknown')} ({iid}) [interior keyword]")
            log_fn(f"  🗑️ Culling explicit interior item: '{item.get('name', 'Unknown')}' ({iid})")
            continue

        new_items[iid] = item
        kept_exterior_items.append(f"{item.get('name', 'Unknown')} ({iid}) [neutral/exterior]")
        log_fn(f"  ✓ Keeping exterior/architectural item: '{item.get('name', 'Unknown')}' ({iid})")

    if kept_exterior_items:
        log_fn(f"\n  🌳 Kept {len(kept_exterior_items)} exterior items:")
        for ei in kept_exterior_items:
            log_fn(f"     + {ei}")

    if culled_items:
        log_fn(f"\n  🗑️  Culled {len(culled_items)} interior items from {layer_id}:")
        for ci in culled_items:
            log_fn(f"     - {ci}")

    item_source = source_items if isinstance(source_items, dict) else items
    if item_source is not items:
        final_items = {iid: item_source[iid] for iid in new_items.keys() if iid in item_source}
    else:
        final_items = new_items

    return new_vertices, new_lines, new_areas, final_items, new_holes


# ─────────────────────────────────────────────────────────────────────────────
# TOP-VIEW CULLING  (show_all=False — preserve interior assets)
# ─────────────────────────────────────────────────────────────────────────────

def _cull_top_view_keep_interior(
    layer_id: str,
    vertices: dict,
    lines: dict,
    areas: dict,
    items: dict,
    holes: dict,
    log_fn,
) -> Tuple[dict, dict, dict, dict, dict]:
    """
    TOP VIEW MODE with show_all=False.

    Keep:
      • ALL walls, areas, vertices, holes  (full building shell from above)
      • ALL interior items — sofas, fridges, furniture, etc. are NOT removed
      • Exterior-named items (roof, grill, balcony, etc.) are also kept
      • Floor assets always kept

    This mode is used when a top-down / bird's-eye render is requested but
    the caller explicitly wants interior furnishings to remain visible (e.g.
    a floorplan overview render).  No culling of items is performed.
    """
    log_fn(f"  ── Top-View (show_all=False) Culling for {layer_id} ──")

    # Keep all architectural geometry unchanged
    new_areas    = dict(areas)
    new_lines    = dict(lines)
    new_vertices = dict(vertices)

    log_fn(f"  ✓ Keeping ALL {len(new_areas)} areas  (structural mesh)")
    log_fn(f"  ✓ Keeping ALL {len(new_lines)} walls  (building shell)")

    # Keep all holes as-is (no black-blocker flag needed — camera is above)
    new_holes = dict(holes)
    log_fn(f"  ✓ Keeping ALL {len(new_holes)} holes  (no is_exterior_black flag)")

    # Keep ALL items — do not remove interior assets
    new_items = dict(items)
    log_fn(f"  ✓ Keeping ALL {len(new_items)} items  (show_all=False: interior assets preserved)")

    for iid, item in new_items.items():
        log_fn(f"     ✓ Item: '{item.get('name', 'Unknown')}' ({iid})")

    item_source = source_items if isinstance(source_items, dict) else items
    if item_source is not items:
        final_items = {iid: item_source[iid] for iid in new_items.keys() if iid in item_source}
    else:
        final_items = new_items

    return new_vertices, new_lines, new_areas, final_items, new_holes


# ─────────────────────────────────────────────────────────────────────────────
# MAIN CLASS
# ─────────────────────────────────────────────────────────────────────────────

class SceneOptimizer:
    def __init__(self, log_path: Optional[str] = None):
        """
        Initialize SceneOptimizer.

        Args:
            log_path: Full path for the plain-text culling log (.txt).
                      A matching *_details.json structured log is saved
                      alongside it automatically.
                      If None, uses default path inside culling_logs/.
        """
        if log_path is None:
            log_path = os.path.join(CULLING_LOGS_DIR, "culling_log.txt")

        self.log_path      = log_path
        self.json_log_path = os.path.splitext(log_path)[0] + "_details.json"

        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

        try:
            with open(self.log_path, "w", encoding="utf-8") as f:
                f.write("--- Scene Culling Log ---\n")
        except Exception:
            pass

        self._log_data: Dict[str, Any] = {
            "timestamp":         time.strftime("%Y-%m-%dT%H:%M:%S"),
            "log_txt_file":      self.log_path,
            "log_json_file":     self.json_log_path,
            "camera":            {},
            "render_mode":       "UNKNOWN",
            "show_all_floors":   None,
            "use_showall":       None,
            "floor_decisions":   [],
            "ceiling_decisions": [],
            "layers":            {},
            "summary":           {},
            "errors":            [],
        }

    # ── Logging ───────────────────────────────────────────────────────────────

    def log(self, msg: str) -> None:
        safe_msg = f"[SceneOptimizer] {msg}".encode("ascii", "backslashreplace").decode("ascii")
        print(safe_msg)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{msg}\n")
        except Exception:
            pass

    def _save_json_log(self) -> None:
        try:
            with open(self.json_log_path, "w", encoding="utf-8") as f:
                json.dump(self._log_data, f, indent=2, ensure_ascii=False)
            self.log(f"📄 Structured log saved → {self.json_log_path}")
        except Exception as exc:
            self.log(f"⚠️  Could not save JSON log: {exc}")

    # ── Ceiling Visibility ────────────────────────────────────────────────────

    def _apply_floor_visibility(
        self,
        fp_data: Dict[str, Any],
    ) -> None:
        """
        Force area floors to stay renderable.

        If a source floor is marked hidden, we still keep it visible so the
        layer above can be rendered as an occluding surface.
        """
        self.log("🏠 _apply_floor_visibility -> forcing floor visibility on ...")
        for layer_id, layer in fp_data.get("layers", {}).items():
            for area_id, area in layer.get("areas", {}).items():
                area_name = area.get("name", area_id)
                floor_props = area.get("floor_properties")
                if not isinstance(floor_props, dict):
                    floor_props = {}
                    area["floor_properties"] = floor_props

                source_visible = None
                if "visible" in floor_props:
                    source_visible = bool(floor_props.get("visible"))
                elif "isvisible" in floor_props:
                    source_visible = bool(floor_props.get("isvisible"))
                elif "isVisible" in floor_props:
                    source_visible = bool(floor_props.get("isVisible"))

                floor_props["visible"] = True
                floor_props["isvisible"] = True
                floor_props["isVisible"] = True

                self._log_data["floor_decisions"].append({
                    "layer_id": layer_id,
                    "area_id": area_id,
                    "area_name": area_name,
                    "visible": True,
                    "source_visible": source_visible,
                    "reason": "forced renderable floor",
                })
                self.log(
                    f"  ✅ Area '{area_name}' ({layer_id}) - floor forced visible "
                    f"(source was {source_visible})"
                )

    def _apply_ceiling_visibility(
        self,
        fp_data: Dict[str, Any],
        cam_height_m: float,
        show_all_floors: bool,
    ) -> None:
        """
        Tags per-area ceiling visibility in fp_data (in-place) by setting
        ceiling_properties.isvisible.

        Respects explicit `isvisible` or `isVisible` properties if provided in the JSON.
        Defaults to True if not explicitly specified.
        """
        self.log(
            "🏠 _apply_ceiling_visibility → processing ceiling visibility "
            "(respecting explicit isvisible=false) ..."
        )
        for layer_id, layer in fp_data.get("layers", {}).items():
            for area_id, area in layer.get("areas", {}).items():
                cp        = area.get("ceiling_properties")
                area_name = area.get("name", area_id)

                if cp is None:
                    area["ceiling_properties"] = {"isvisible": True}
                    self._log_data["ceiling_decisions"].append({
                        "layer_id":  layer_id,
                        "area_id":   area_id,
                        "area_name": area_name,
                        "isvisible": True,
                        "reason":    "no ceiling_properties → defaulted to True",
                    })
                    self.log(f"  ✅ Area '{area_name}' ({layer_id}) — ceiling defaulted to visible")
                else:
                    # Respect explicit visibility if provided
                    if "visible" in cp:
                        is_vis = bool(cp["visible"])
                    elif "isvisible" in cp:
                        is_vis = bool(cp["isvisible"])
                    elif "isVisible" in cp:
                        is_vis = bool(cp["isVisible"])
                    else:
                        is_vis = True
                        cp["isvisible"] = True
                    
                    self._log_data["ceiling_decisions"].append({
                        "layer_id":  layer_id,
                        "area_id":   area_id,
                        "area_name": area_name,
                        "isvisible": is_vis,
                        "reason":    "explicit JSON property" if "isvisible" in cp or "isVisible" in cp or "visible" in cp else "defaulted to True",
                    })
                    self.log(f"  ✅ Area '{area_name}' ({layer_id}) — ceiling visibility resolved to {is_vis}")

    # ── Main Entry Point ──────────────────────────────────────────────────────

    def _normalize_external_area_materials(self, fp_data: Dict[str, Any]) -> None:
        """
        Promote external-area texture metadata into a consistent top-level
        `material` block so downstream renderers can always resolve textures
        and repeat values the same way.
        """
        layers = fp_data.get("layers", {})
        if not isinstance(layers, dict):
            return

        for layer in layers.values():
            external_areas = layer.get("externalAreas", {})
            if not isinstance(external_areas, dict):
                continue

            for area in external_areas.values():
                if not isinstance(area, dict):
                    continue

                props = area.get("properties", {})
                if not isinstance(props, dict):
                    props = {}

                material = area.get("material")
                if not isinstance(material, dict):
                    material = {}

                props_material = props.get("material")
                if isinstance(props_material, dict):
                    for key, value in props_material.items():
                        if key not in material:
                            material[key] = value

                if "mapUrl" not in material or str(material.get("mapUrl", "")).strip() == "":
                    texture_url = str(props.get("texture", "")).strip()
                    if texture_url and texture_url.lower() != "none":
                        material["mapUrl"] = texture_url

                if material:
                    area["material"] = material

    def cull_scene(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Optimise the render payload using three-mode culling:

        INTERIOR (camera inside a room)
        ─────────────────────────────────
        • Keep only the active room + rooms reachable via openings / hidden walls
        • Remove all exterior walls, areas, and items outside kept rooms
        • Textures: interior materials applied normally

        EXTERIOR (camera outside all rooms)
        ─────────────────────────────────────
        • Keep all walls, areas, and holes (building shell)
        • Window/door openings render BLACK — no interior geometry behind them
        • Remove ALL interior furniture items (sofa, fridge, decorations, etc.)
        • Keep only: items outside room polygons + architectural wall fittings
        • Textures: exterior-facing materials applied (handled by renderer)

        TOP VIEW (payload contains is_top_view=true)
        ─────────────────────────────────────────────
        • Camera XY is overridden to the centroid of all home rooms (directly above)
        • All walls, areas, and holes are kept (building shell from above)
        • If show_all=false → interior assets are NOT removed (kept as-is)
        • If show_all=true  → interior items inside rooms are culled (exterior mode)

        Returns a new payload with optimised floor_plan_data.
        Also saves a plain-text log and a structured *_details.json log.
        """
        try:
            # ── 1. Parse floor_plan_data ──────────────────────────────────────
            fp_str = payload.get("floor_plan_data")
            if not fp_str:
                self.log("⚠️ No floor_plan_data found.")
                return payload

            fp_data = json.loads(fp_str) if isinstance(fp_str, str) else fp_str
            if isinstance(fp_data, dict) and "showAllFloors" not in fp_data and "showAllFloors" in payload:
                fp_data["showAllFloors"] = payload["showAllFloors"]
            if isinstance(fp_data, dict) and "selectedLayer" not in fp_data and "selectedLayer" in payload:
                fp_data["selectedLayer"] = payload["selectedLayer"]
            if isinstance(fp_data, dict):
                self._normalize_external_area_materials(fp_data)

            # ── 1a. Repair wall topology BEFORE anything reads the walls ──────
            # Welds wall endpoints that dangle on a hosted/T-junction vertex
            # onto the real room corner, so the room polygon is closed for
            # culling, for the mitre rebuild, and for Godot's wall boxes.
            if isinstance(fp_data, dict):
                _welded = _repair_wall_topology(fp_data, self.log)
                if _welded:
                    self.log(
                        f"🔧 Wall topology repair: welded {_welded} dangling "
                        f"wall endpoint(s) onto room corners."
                    )
            scene_version = str(fp_data.get("version", "")).strip() if isinstance(fp_data, dict) else ""

            # ── 1b. Detect top_view mode ──────────────────────────────────────
            meters_per_unit = _resolve_scene_meters_per_unit(fp_data)
            cm_per_unit = meters_per_unit * 100.0
            plan_rotation_radians = _resolve_scene_rotation_radians(fp_data)
            plan_pivot_cm = _resolve_scene_plan_pivot_cm(fp_data, cm_per_unit)
            self.log(
                f"📐 Plan units: {meters_per_unit:.4f}m/source unit "
                f"({cm_per_unit:.2f}cm/source unit)"
            )
            self.log(
                f"📐 Plan rotation: {math.degrees(plan_rotation_radians):.1f}° "
                f"pivot={tuple(round(v, 2) for v in plan_pivot_cm)}"
            )

            is_top_view = bool(payload.get("is_top_view", False))
            show_all    = bool(payload.get("show_all", payload.get("showall", False)))  # False = isolate camera layer; True = render all layers (top-view override)
            self._log_data["is_top_view"] = is_top_view
            self._log_data["show_all"]    = show_all

            if is_top_view:
                self.log(f"🔭 TOP VIEW mode detected | show_all={show_all}")

            # ── 2. Extract camera position ────────────────────────────────────
            cam_x = cam_y = cam_height_m = 0.0
            cam_source = "none"

            if "threejs_camera" in payload and "position" in payload["threejs_camera"]:
                tj = payload.get("threejs_camera", {})
                pos = tj.get("position")
                tgt = tj.get("target")

                # ── 2a. Keep the authored camera position intact here ────────
                # Collision-aware camera nudging is applied later only when the
                # camera actually overlaps walls or assets.  Do not apply an
                # unconditional forward offset, otherwise the render can shift
                # even when the camera is already clear of geometry.
                cull_pos = dict(pos) if isinstance(pos, dict) else None

                cull_x = float(pos.get("x", 0.0)) if isinstance(pos, dict) else 0.0
                cull_z = float(pos.get("z", 0.0)) if isinstance(pos, dict) else 0.0
                if isinstance(cull_pos, dict):
                    cull_x = float(cull_pos.get("x", cull_x))
                    cull_z = float(cull_pos.get("z", cull_z))

                cam_x, cam_y = _transform_camera_point_cm(
                    cull_x,
                    cull_z,
                    plan_rotation_radians,
                    plan_pivot_cm,
                )
                cam_height_m = float(pos.get("y", 0))
                cam_source   = "threejs_camera"
                self.log(
                    f"📍 Camera (ThreeJS): {pos} → "
                    f"Plan XZ: ({cam_x:.1f}, {cam_y:.1f}) cm | Height: {cam_height_m:.3f} m"
                )

            else:
                self.log("⚠️ No camera data found. Skipping optimization.")
                return payload

            # ── 2b. Extract camera forward direction ──────────────────────────
            # Priority: threejs_camera.target → fallback
            cam_dir_x, cam_dir_y = 1.0, 0.0
            dir_source = "fallback"

            tj = payload.get("threejs_camera", {})
            tgt = tj.get("target")
            if isinstance(tgt, dict) and "x" in tgt and "z" in tgt:
                tx, ty = _transform_camera_point_cm(
                    float(tgt.get("x", 0.0)),
                    float(tgt.get("z", 0.0)),
                    plan_rotation_radians,
                    plan_pivot_cm,
                )
                fdx, fdy = tx - cam_x, ty - cam_y
                mag = math.hypot(fdx, fdy)
                if mag > 1e-6:
                    cam_dir_x, cam_dir_y = fdx / mag, fdy / mag
                    dir_source = "threejs_target"

            self.log(
                f"📐 Camera dir: ({cam_dir_x:.3f},{cam_dir_y:.3f})  "
                f"angle={math.degrees(math.atan2(cam_dir_y, cam_dir_x)):.1f}°  "
                f"source={dir_source}"
            )

            cam_view_dist_cm = float(payload.get("interior_view_dist_cm", DEFAULT_VIEW_DISTANCE_CM))
            cam_fov_half_deg, cam_vertical_fov_deg, cam_aspect_ratio = _resolve_camera_plan_half_fov_deg(payload)
            if "interior_fov_half_deg" in payload:
                self.log(f"📐 FOV: payload override ±{cam_fov_half_deg:.0f}°")
            elif cam_vertical_fov_deg and cam_aspect_ratio:
                self.log(
                    f"📐 FOV: threejs fov={cam_vertical_fov_deg:.0f}° "
                    f"aspect={cam_aspect_ratio:.3f} → plan ±{cam_fov_half_deg:.0f}°"
                )
            elif cam_vertical_fov_deg:
                self.log(f"📐 FOV: threejs fov={cam_vertical_fov_deg:.0f}° → ±{cam_fov_half_deg:.0f}°")
            else:
                self.log(f"📐 FOV: default ±{cam_fov_half_deg:.0f}°")

            cam_target_x: Optional[float] = None
            cam_target_y: Optional[float] = None
            cam_target_source = "fallback"
            if isinstance(tgt, dict) and "x" in tgt and "z" in tgt:
                cam_target_x, cam_target_y = _transform_camera_point_cm(
                    float(tgt.get("x", 0.0)),
                    float(tgt.get("z", 0.0)),
                    plan_rotation_radians,
                    plan_pivot_cm,
                )
                cam_target_source = "threejs_target"
            else:
                cam_target_x = cam_x + (cam_dir_x * cam_view_dist_cm)
                cam_target_y = cam_y + (cam_dir_y * cam_view_dist_cm)
            self.log(
                f"📐 Camera target: ({cam_target_x:.1f}, {cam_target_y:.1f}) cm "
                f"source={cam_target_source}"
            )

            layers = fp_data.get("layers", {})

            def _transform_layer_for_culling(layer_data: dict) -> Tuple[dict, dict]:
                transformed_vertices: Dict[str, Any] = {}
                transformed_items: Dict[str, Any] = {}

                for vid, vertex in layer_data.get("vertices", {}).items():
                    if not isinstance(vertex, dict):
                        continue
                    tx, ty = _transform_plan_point_cm(
                        float(vertex.get("x", 0.0)),
                        float(vertex.get("y", 0.0)),
                        cm_per_unit,
                        plan_rotation_radians,
                        plan_pivot_cm,
                    )
                    v_copy = dict(vertex)
                    v_copy["x"] = tx
                    v_copy["y"] = ty
                    transformed_vertices[vid] = v_copy

                for iid, item in layer_data.get("items", {}).items():
                    if not isinstance(item, dict):
                        continue
                    i_copy = dict(item)
                    if "x" in i_copy and "y" in i_copy:
                        tx, ty = _transform_plan_point_cm(
                            float(i_copy.get("x", 0.0)),
                            float(i_copy.get("y", 0.0)),
                            cm_per_unit,
                            plan_rotation_radians,
                            plan_pivot_cm,
                        )
                        i_copy["x"] = tx
                        i_copy["y"] = ty
                    transformed_items[iid] = i_copy

                return transformed_vertices, transformed_items

            transformed_layers: Dict[str, Dict[str, dict]] = {}
            for _lid, _layer in layers.items():
                if isinstance(_layer, dict):
                    _tv, _ti = _transform_layer_for_culling(_layer)
                    transformed_layers[_lid] = {"vertices": _tv, "items": _ti}

            # ── 2b. Top-view: override camera XY to home centroid ─────────────
            if is_top_view:
                all_xs: List[float] = []
                all_ys: List[float] = []
                for layer in transformed_layers.values():
                    _verts = layer.get("vertices", {})
                    for _area in layer.get("areas", {}).values():
                        for _vid in _area.get("vertices", []):
                            _v = _verts.get(_vid)
                            if _v:
                                all_xs.append(_v["x"])
                                all_ys.append(_v["y"])
                if all_xs and all_ys:
                    orig_cam_x, orig_cam_y = cam_x, cam_y
                    cam_x = (min(all_xs) + max(all_xs)) / 2.0
                    cam_y = (min(all_ys) + max(all_ys)) / 2.0
                    self.log(
                        f"🔭 TOP VIEW: Camera XY overridden from "
                        f"({orig_cam_x:.1f}, {orig_cam_y:.1f}) → centroid "
                        f"({cam_x:.1f}, {cam_y:.1f}) cm  [directly above home]"
                    )
                else:
                    self.log("⚠️ TOP VIEW: No vertices found to compute centroid — camera XY unchanged.")

            self._log_data["camera"] = {
                "source":         cam_source,
                "dir_source":     dir_source,
                "plan_x_cm":      round(cam_x, 2),
                "plan_y_cm":      round(cam_y, 2),
                "height_m":       round(cam_height_m, 4),
                "dir_x":          round(cam_dir_x, 4),
                "dir_y":          round(cam_dir_y, 4),
                "fov_half_deg":   cam_fov_half_deg,
                "view_dist_cm":   cam_view_dist_cm,
                "raw_threejs":    payload.get("threejs_camera", {}).get("position"),
                "is_top_view":    is_top_view,
                "show_all":       show_all,
            }

            # ── 3. showAllFloors / ceiling pass ───────────────────────────────
            show_all_floors = fp_data.get("showAllFloors", True)
            self._log_data["show_all_floors"] = show_all_floors
            self._log_data["use_showall"] = use_showall
            self.log(f"🏗️  use_showall = {use_showall}")
            self.log(f"🏗️  showAllFloors = {show_all_floors}")
            # Stamp use_showall into fp_data so the Godot renderer (image_glb_creation.gd)
            # can read it and skip the camera-height ceiling override when use_showall=False.
            fp_data["use_showall"] = use_showall
            self._apply_floor_visibility(fp_data)
            self._apply_ceiling_visibility(fp_data, cam_height_m, show_all_floors)
            # keep_all_layers: True when the payload explicitly wants all floors,
            # or when we are in top-view mode.
            keep_all_layers = show_all or show_all_floors or is_top_view

            # ── 4. Per-layer culling ──────────────────────────────────────────
            layers = fp_data.get("layers", {})
            if not layers:
                self.log("⚠️ No layers found in floor plan.")
                new_payload = payload.copy()
                new_payload["floor_plan_data"] = (
                    json.dumps(fp_data) if isinstance(fp_str, str) else fp_data
                )
                self._save_json_log()
                return new_payload

            # ── 4a. Pre-scan: find which layer the camera is in ───────────────
            # camera_layer_id is resolved regardless of showAllFloors so that:
            #   • Cross-floor detection always has the camera's home layer.
            #   • showAllFloors=true + INTERIOR: layer isolation still runs
            #     (only that floor is rendered, matching the focused camera).
            #   • showAllFloors=true + EXTERIOR: camera_layer_id stays None →
            #     _do_layer_isolation=False → all floors are rendered (correct).
            #   • showAllFloors=false: selectedLayer / spatial scan → isolate.
            camera_layer_id: Optional[str] = None
            _selected_layer_raw: str = str(fp_data.get("selectedLayer", "")).strip()
            # True when camera_layer_id was set from the selectedLayer hint (not spatial scan).
            # Cross-floor detection is fully disabled in this case — the caller explicitly
            # named the layer they want rendered; we must not pull in neighbours even if
            # the camera's physical height is near the selected layer's altitude boundary.
            _camera_layer_from_hint: bool = False

            if _selected_layer_raw and not is_top_view and not show_all_floors:
                _resolved_layer_id, _resolve_reason = _resolve_selected_layer_hint(
                    _selected_layer_raw,
                    layers,
                )
                if _resolved_layer_id:
                    camera_layer_id = _resolved_layer_id
                    _camera_layer_from_hint = True
                    self.log(
                        f"  ✅ selectedLayer='{_selected_layer_raw}' resolved via {_resolve_reason} "
                        f"→ camera_layer='{camera_layer_id}'"
                    )
                else:
                    self.log(
                        f"  ⚠️ selectedLayer='{_selected_layer_raw}' did not match any layer; "
                        "falling back to spatial pre-scan."
                    )

            if camera_layer_id:
                _hint_note = " Cross-floor detection DISABLED (selectedLayer is authoritative)." if _camera_layer_from_hint else ""
                self.log(
                    f"  📌 Using selectedLayer='{_selected_layer_raw}' "
                    f"(resolved to '{camera_layer_id}') — skipping spatial pre-scan.{_hint_note}"
                )

            # Spatial pre-scan only runs when no explicit selectedLayer was resolved.
            # This keeps camera height from overriding a user-selected floor.
            if not camera_layer_id and not is_top_view:
                if show_all_floors:
                    self.log("  🏗️ showAllFloors=true: running spatial pre-scan — isolation applies if camera is INTERIOR, skipped if EXTERIOR.")
                for _lid, _layer in layers.items():
                    _raw_alt = _layer.get("altitude", 0)
                    _layer_alt_m = _source_length_to_m(_raw_alt, meters_per_unit)
                    _wall_heights = []
                    for _wl in _layer.get("lines", {}).values():
                        _wh = _wl.get("properties", {}).get("height", {})
                        _wh_m = _source_length_to_m(_wh, meters_per_unit)
                        if _wh_m > 0.1:
                            _wall_heights.append(_wh_m)
                    _floor_height_m = max(_wall_heights) if _wall_heights else 3.0
                    _layer_top_m    = _layer_alt_m + _floor_height_m

                    if not (_layer_alt_m <= cam_height_m < _layer_top_m):
                        self.log(
                            f"  ↩ Pre-scan skip '{_lid}': cam_height={cam_height_m:.3f}m "
                            f"not in layer Z range [{_layer_alt_m:.3f}m .. {_layer_top_m:.3f}m]"
                        )
                        continue

                    _verts = transformed_layers.get(_lid, {}).get("vertices", _layer.get("vertices", {}))
                    for _area in _layer.get("areas", {}).values():
                        _poly = []
                        for _vid in _area.get("vertices", []):
                            _v = _verts.get(_vid)
                            if _v:
                                _poly.append((_v["x"], _v["y"]))
                        if not _poly:
                            continue
                        _xs = [p[0] for p in _poly]
                        _ys = [p[1] for p in _poly]
                        if min(_xs) <= cam_x <= max(_xs) and min(_ys) <= cam_y <= max(_ys):
                            if _point_in_polygon(cam_x, cam_y, _poly):
                                camera_layer_id = _lid
                                self.log(
                                    f"  ✅ Pre-scan: camera height={cam_height_m:.3f}m "
                                    f"∈ layer '{_lid}' Z [{_layer_alt_m:.3f}m .. {_layer_top_m:.3f}m] "
                                    f"AND inside 2-D polygon → camera_layer='{_lid}'"
                                )
                                break
                    if camera_layer_id:
                        break

            if _camera_layer_from_hint:
                self.log(
                    f"  🚫 Cross-floor detection SKIPPED — selectedLayer='{_selected_layer_raw}' "
                    f"is an explicit directive. Only layer '{camera_layer_id}' will be rendered."
                )

            # ── Cross-floor transition detection ─────────────────────────────
            # When the camera is genuinely near a floor boundary (e.g. looking from
            # ground floor up toward first floor), include the adjacent layer so the
            # transition zone is fully visible.
            #
            # DISABLED when selectedLayer is the source (showAllFloors=false):
            #   The caller explicitly named the layer they want — we render ONLY that
            #   layer, no matter what.  Cross-floor must not override an explicit
            #   selectedLayer directive.  This is the root cause of the bug where
            #   selectedLayer=layer-2 but layer-1 was also rendered because the
            #   camera's physical height (1.345 m) happened to be < layer-2 alt
            #   (3 m) + boundary_zone (0.6 m).
            #
            # ENABLED only when camera_layer_id was found by the SPATIAL SCAN —
            #   meaning the camera is genuinely inside that layer's geometry.
            cross_floor_layer_ids: Set[str] = set()
            if camera_layer_id and not is_top_view and not _camera_layer_from_hint:
                _cam_layer     = layers[camera_layer_id]
                _raw_alt       = _cam_layer.get("altitude", 0)
                _cam_alt_m     = _source_length_to_m(_raw_alt, meters_per_unit)
                _cam_wh        = []
                for _wl in _cam_layer.get("lines", {}).values():
                    _wh = _wl.get("properties", {}).get("height", {})
                    _wh_m = _source_length_to_m(_wh, meters_per_unit)
                    if _wh_m > 0.1:
                        _cam_wh.append(_wh_m)
                _cam_floor_h   = max(_cam_wh) if _cam_wh else 3.0
                _cam_top_m     = _cam_alt_m + _cam_floor_h
                _boundary_zone = _cam_floor_h * 0.20

                # ── Cross-floor guard: camera must be within the layer's Z range ──
                _cam_in_layer_range = _cam_alt_m <= cam_height_m < _cam_top_m
                if not _cam_in_layer_range:
                    self.log(
                        f"  ↩ Cross-floor detection skipped: camera height={cam_height_m:.3f}m "
                        f"is outside layer '{camera_layer_id}' Z range "
                        f"[{_cam_alt_m:.3f}m .. {_cam_top_m:.3f}m]. "
                        f"(camera is physically on a different floor.)"
                    )

                near_top    = _cam_in_layer_range and cam_height_m >= (_cam_top_m - _boundary_zone)
                near_bottom = _cam_in_layer_range and cam_height_m <= (_cam_alt_m + _boundary_zone)

                if near_top or near_bottom:
                    for _lid, _layer in layers.items():
                        if _lid == camera_layer_id:
                            continue
                        _raw_alt2    = _layer.get("altitude", 0)
                        _other_alt_m = _source_length_to_m(_raw_alt2, meters_per_unit)
                        _other_wh = []
                        for _wl in _layer.get("lines", {}).values():
                            _wh = _wl.get("properties", {}).get("height", {})
                            _wh_m2 = _source_length_to_m(_wh, meters_per_unit)
                            if _wh_m2 > 0.1:
                                _other_wh.append(_wh_m2)
                        _other_floor_h = max(_other_wh) if _other_wh else 3.0
                        _other_top_m   = _other_alt_m + _other_floor_h
                        is_directly_above = abs(_other_alt_m - _cam_top_m) < 0.5
                        is_directly_below = abs(_other_top_m - _cam_alt_m) < 0.5
                        if (near_top and is_directly_above) or (near_bottom and is_directly_below):
                            cross_floor_layer_ids.add(_lid)
                            self.log(
                                f"  🔀 CROSS-FLOOR: camera near "
                                f"{'top' if near_top else 'bottom'} of layer '{camera_layer_id}' "
                                f"→ also rendering adjacent layer '{_lid}'"
                            )
                if cross_floor_layer_ids:
                    self.log(f"  🔀 Cross-floor layers: {list(cross_floor_layer_ids)}")

            # ── Layer isolation decision ──────────────────────────────────────
            #
            # LOGIC:
            #   showAllFloors=true
            #     → render ALL floors. The camera may still resolve a home layer,
            #       but we keep every layer eligible so upper slabs stay present.
            #
            #   showAllFloors=false  (camera_layer_id is set from selectedLayer)
            #     → isolate to the selectedLayer only (classic single-floor render).
            #
            #   show_all=True or is_top_view
            #     → always render all layers regardless (existing override).
            _do_layer_isolation = (
                camera_layer_id is not None
                and not keep_all_layers   # False when show_all=True or is_top_view
                # Isolate only when we are intentionally narrowing to a single floor.
            )

            if _do_layer_isolation:
                _isolation_reason = (
                    f"showAllFloors={'true' if show_all_floors else 'false'} + "
                    f"{'selectedLayer' if _camera_layer_from_hint else 'INTERIOR'}"
                )
                self.log(
                    f"\n🎯 LAYER ISOLATION [{_isolation_reason}]: "
                    f"Camera is inside layer '{camera_layer_id}' "
                    f"— only this layer (+ cross-floor neighbours) will be rendered."
                )
            elif is_top_view:
                self.log(f"\n🔭 TOP VIEW: Processing all {len(layers)} layers.")
            elif show_all:
                self.log(f"\n🌍 show_all=True: Processing all {len(layers)} layers.")
            elif show_all_floors:
                self.log(
                    f"\n🏗️ showAllFloors=true: processing all {len(layers)} layers "
                    f"(multi-floor render)."
                )
            else:
                self.log(
                    f"\n🌍 EXTERIOR: Camera not found inside any layer — "
                    f"processing all {len(layers)} layers."
                )

            totals = dict(
                areas_before=0, areas_after=0,
                items_before=0, items_after=0,
                lines_before=0, lines_after=0,
                holes_before=0, holes_after=0,
            )
            render_mode = "UNKNOWN"

            collision_push_cm, collision_label = _camera_forward_collision_push_cm(
                cam_x=cam_x,
                cam_y=cam_y,
                cam_dir_x=cam_dir_x,
                cam_dir_y=cam_dir_y,
                cam_height_m=cam_height_m,
                layers=layers,
                transformed_layers=transformed_layers,
                meters_per_unit=meters_per_unit,
                cm_per_unit=cm_per_unit,
                camera_layer_id=camera_layer_id,
                is_top_view=is_top_view,
            )
            if collision_push_cm > 0.0:
                camera_log = self._log_data.get("camera") if isinstance(self._log_data.get("camera"), dict) else None
                orig_world_pos = dict(pos) if isinstance(pos, dict) else None
                original_plan_x_cm = cam_x
                original_plan_y_cm = cam_y
                orig_cam_x, orig_cam_y = cam_x, cam_y
                cam_x += cam_dir_x * collision_push_cm
                cam_y += cam_dir_y * collision_push_cm
                if cam_target_x is not None and cam_target_y is not None:
                    cam_target_x += cam_dir_x * collision_push_cm
                    cam_target_y += cam_dir_y * collision_push_cm

                push_m = collision_push_cm / 100.0
                if isinstance(pos, dict):
                    px = float(orig_world_pos.get("x", 0.0)) if orig_world_pos else float(pos.get("x", 0.0))
                    pz = float(orig_world_pos.get("z", 0.0)) if orig_world_pos else float(pos.get("z", 0.0))
                    pos["x"] = px + (cam_dir_x * push_m)
                    pos["z"] = pz + (cam_dir_y * push_m)

                if isinstance(camera_log, dict):
                    camera_log["camera_move_applied"] = True
                    camera_log["camera_move_reason"] = collision_label
                    camera_log["camera_move_cm"] = round(collision_push_cm, 2)
                    camera_log["original_plan_x_cm"] = round(original_plan_x_cm, 2)
                    camera_log["original_plan_y_cm"] = round(original_plan_y_cm, 2)
                    camera_log["moved_plan_x_cm"] = round(cam_x, 2)
                    camera_log["moved_plan_y_cm"] = round(cam_y, 2)
                    camera_log["plan_x_cm"] = round(cam_x, 2)
                    camera_log["plan_y_cm"] = round(cam_y, 2)
                    if orig_world_pos:
                        camera_log["camera_move_original_threejs"] = {
                            "x": round(float(orig_world_pos.get("x", 0.0)), 6),
                            "y": round(float(orig_world_pos.get("y", 0.0)), 6),
                            "z": round(float(orig_world_pos.get("z", 0.0)), 6),
                        }
                    if isinstance(pos, dict):
                        camera_log["camera_move_adjusted_threejs"] = {
                            "x": round(float(pos.get("x", 0.0)), 6),
                            "y": round(float(pos.get("y", 0.0)), 6),
                            "z": round(float(pos.get("z", 0.0)), 6),
                        }
                self.log(
                    f"  🚧 Camera collision detected with {collision_label}; "
                    f"moved camera forward by {collision_push_cm:.0f}cm "
                    f"from plan ({orig_cam_x:.1f}, {orig_cam_y:.1f}) cm "
                    f"to ({cam_x:.1f}, {cam_y:.1f}) cm."
                )
                if orig_world_pos and isinstance(pos, dict):
                    self.log(
                        f"     ThreeJS camera: ({orig_world_pos['x']:.3f}, {orig_world_pos['y']:.3f}, {orig_world_pos['z']:.3f}) "
                        f"→ ({pos['x']:.3f}, {pos['y']:.3f}, {pos['z']:.3f})"
                    )
            elif isinstance(self._log_data.get("camera"), dict):
                camera_log = self._log_data["camera"]
                camera_log["camera_move_applied"] = False
                camera_log["camera_move_reason"] = None
                camera_log["camera_move_cm"] = 0.0
                camera_log["original_plan_x_cm"] = round(cam_x, 2)
                camera_log["original_plan_y_cm"] = round(cam_y, 2)
                camera_log["moved_plan_x_cm"] = round(cam_x, 2)
                camera_log["moved_plan_y_cm"] = round(cam_y, 2)
                if isinstance(pos, dict):
                    camera_log["camera_move_original_threejs"] = {
                        "x": round(float(pos.get("x", 0.0)), 6),
                        "y": round(float(pos.get("y", 0.0)), 6),
                        "z": round(float(pos.get("z", 0.0)), 6),
                    }
                    camera_log["camera_move_adjusted_threejs"] = {
                        "x": round(float(pos.get("x", 0.0)), 6),
                        "y": round(float(pos.get("y", 0.0)), 6),
                        "z": round(float(pos.get("z", 0.0)), 6),
                    }

            for layer_id, layer in layers.items():
                self.log(f"\n{'─'*60}")
                self.log(f"🔍 Inspecting Layer: {layer_id}")
                self.log(f"{'─'*60}")

                # ── LAYER SKIP ────────────────────────────────────────────────
                # Only blank a layer when layer isolation is active AND this
                # layer is neither the camera layer nor a cross-floor neighbour.
                if _do_layer_isolation and layer_id != camera_layer_id \
                        and layer_id not in cross_floor_layer_ids:
                    self.log(
                        f"  ⏭️  SKIPPED — camera is in layer '{camera_layer_id}', "
                        f"not '{layer_id}'. Removing entire layer from output."
                    )
                    # Blank the layer so the renderer ignores it completely
                    layer["vertices"] = {}
                    layer["lines"]    = {}
                    layer["areas"]    = {}
                    layer["items"]    = {}
                    layer["holes"]    = {}
                    layer["render_mode"] = "SKIPPED"
                    self._log_data["layers"][layer_id] = {
                        "render_mode":  "SKIPPED",
                        "active_room":  None,
                        "reason":       f"camera is in layer '{camera_layer_id}'",
                        "stats_before": {"areas": 0, "lines": 0, "holes": 0, "items": 0},
                        "stats_after":  {"areas": 0, "lines": 0, "holes": 0, "items": 0},
                    }
                    continue

                source_vertices = layer.get("vertices", {})
                source_items    = layer.get("items", {})
                vertices = transformed_layers.get(layer_id, {}).get("vertices", source_vertices)
                lines    = layer.get("lines",    {})
                areas    = layer.get("areas",    {})
                items    = transformed_layers.get(layer_id, {}).get("items", source_items)
                holes    = layer.get("holes",    {})

                # ── Compute this layer's vertical range ───────────────────────
                _raw_layer_alt = layer.get("altitude", 0)
                _layer_alt_m   = _source_length_to_m(_raw_layer_alt, meters_per_unit)
                _layer_wh = []
                for _wl in lines.values():
                    _wh = _wl.get("properties", {}).get("height", {})
                    _wh_m = _source_length_to_m(_wh, meters_per_unit)
                    if _wh_m > 0.1:
                        _layer_wh.append(_wh_m)
                _layer_floor_h = max(_layer_wh) if _layer_wh else 3.0
                _layer_top_m   = _layer_alt_m + _layer_floor_h

                totals["areas_before"] += len(areas)
                totals["items_before"] += len(items)
                totals["lines_before"] += len(lines)
                totals["holes_before"] += len(holes)

                self.log(f"  📊 Layer Statistics:")
                self.log(f"     Areas:    {len(areas)}")
                self.log(f"     Lines:    {len(lines)}")
                self.log(f"     Items:    {len(items)}")
                self.log(f"     Holes:    {len(holes)}")
                self.log(f"     Vertices: {len(vertices)}")

                # ── Determine active room ─────────────────────────────────────
                active_area_id = None

                for area_id, area in areas.items():
                    poly_verts = []
                    for v_id in area.get("vertices", []):
                        v = vertices.get(v_id)
                        if v:
                            poly_verts.append((v["x"], v["y"]))
                    if not poly_verts:
                        continue

                    xs = [p[0] for p in poly_verts]
                    ys = [p[1] for p in poly_verts]
                    if min(xs) <= cam_x <= max(xs) and min(ys) <= cam_y <= max(ys):
                        if _point_in_polygon(cam_x, cam_y, poly_verts):
                            active_area_id = area_id
                            self.log(
                                f"  ✅ Camera ({cam_x:.1f}, {cam_y:.1f}) in Active Room: "
                                f"'{area.get('name')}' ({area_id})"
                            )
                            break
                        else:
                            self.log(
                                f"  ❓ Camera inside bbox of '{area.get('name')}' "
                                "but failed polygon test."
                            )

                # ── Branch: Top-view / Interior / Exterior ────────────────────
                # ── showAllFloors=false: Strict Exterior Culling ──────────────
                # When use_showall=False, keep the current culling logic but
                # skip the "camera above the ceiling" fallback so ceilings
                # stay visible even for higher camera positions.
                if (
                    not active_area_id
                    and not show_all_floors
                    and scene_version == "2.0.0"
                    and str(fp_data.get("selectedLayer", "")).strip() != ""
                    and str(layer_id) == str(fp_data.get("selectedLayer", "")).strip()
                    and areas
                    and not is_top_view
                ):
                    # Only promote the selected layer to INTERIOR when there is a
                    # single unambiguous room. Picking the first area in a
                    # multi-room layer can incorrectly override a genuine exterior
                    # camera and force the whole scene into INTERIOR mode.
                    if len(areas) == 1:
                        active_area_id = next(iter(areas.keys()))
                        self.log(
                            f"  ✅ Version 2.0.0 fallback: treating selected layer '{layer_id}' "
                            f"as INTERIOR room '{areas[active_area_id].get('name', 'Unknown')}' "
                            "so room assets are preserved."
                        )
                    else:
                        self.log(
                            f"  ⚠️ Version 2.0.0 selectedLayer fallback skipped for '{layer_id}': "
                            f"camera is not inside any room polygon and the layer has {len(areas)} areas."
                        )

                if not show_all_floors:
                    is_above_ceiling = cam_height_m > (_layer_top_m + 0.5)
                    if is_above_ceiling:
                        self.log(
                            f"  🏗️ Camera above layer '{layer_id}' ceiling "
                            f"(cam_height={cam_height_m:.3f}m > top={_layer_top_m:.3f}m) "
                            "→ force exterior cleanup."
                        )

                    if active_area_id and not is_top_view and not is_above_ceiling:
                        # Eye-level interior shot: Keep room assets
                        render_mode = "INTERIOR"
                        if use_showall:
                            self.log(f"\n  🏠 MODE: INTERIOR (showAllFloors=false) — Camera inside '{areas[active_area_id].get('name')}': Interior assets PRESERVED.")
                        else:
                            self.log(f"\n  🏠 MODE: INTERIOR (use_showall=False override) — Camera inside '{areas[active_area_id].get('name')}': Ceiling visibility preserved.")
                        new_vertices, new_lines, new_areas, new_items, new_holes = _cull_interior(
                            layer_id, active_area_id,
                            vertices, lines, areas, items, holes,
                            self.log,
                            cam_x=cam_x, cam_y=cam_y,
                            cam_dir_x=cam_dir_x, cam_dir_y=cam_dir_y,
                            cam_target_x=cam_target_x,
                            cam_target_y=cam_target_y,
                            cam_height_m=cam_height_m,
                            fov_half_deg=cam_fov_half_deg,
                            max_view_dist_cm=cam_view_dist_cm,
                            meters_per_unit=meters_per_unit,
                            cm_per_unit=cm_per_unit,
                            source_vertices=source_vertices,
                            source_items=source_items,
                            strict_room_limit=STRICT_IMAGE_PORTAL_ROOM_LIMIT,
                            external_areas=layer.get("externalAreas", {}),
                        )
                    else:
                        # Top View or Exterior: Remove all interior assets
                        render_mode = "EXTERIOR_CLEAN"
                        mode_name = "TOP VIEW" if is_top_view else "EXTERIOR"
                        if use_showall:
                            self.log(f"\n  🌍 MODE: {mode_name} (showAllFloors=false) — Facade view: REMOVING all interior assets.")
                        else:
                            self.log(f"\n  🌍 MODE: {mode_name} (use_showall=False override) — Facade view: REMOVING all interior assets.")
                        new_vertices, new_lines, new_areas, new_items, new_holes = _cull_exterior(
                            layer_id,
                            vertices, lines, areas, items, holes,
                            self.log,
                            layer_alt_m=_layer_alt_m,
                            layer_top_m=_layer_top_m,
                            meters_per_unit=meters_per_unit,
                            source_vertices=source_vertices,
                            source_items=source_items,
                            external_areas=layer.get("externalAreas", {}),
                        )
                elif is_top_view:
                    render_mode = "TOP_VIEW"
                    self.log(f"\n  🔭 MODE: TOP VIEW RENDER (show_all={show_all})")
                    if not show_all:
                        # Top-view exterior: treat this as a strict facade pass.
                        # Interior furnishings are removed; only shell / elevation /
                        # explicit exterior assets survive.
                        render_mode = "EXTERIOR_CLEAN"
                        self.log("  🗑️ show_all=False → top-view exterior cleanup (removing all interior items)")
                        new_vertices, new_lines, new_areas, new_items, new_holes = _cull_exterior(
                            layer_id,
                            vertices, lines, areas, items, holes,
                            self.log,
                            layer_alt_m=_layer_alt_m,
                            layer_top_m=_layer_top_m,
                            meters_per_unit=meters_per_unit,
                            source_vertices=source_vertices,
                            source_items=source_items,
                            external_areas=layer.get("externalAreas", {}),
                        )
                    else:
                        # show_all=True → behave like exterior (cull interior items)
                        self.log("  🗑️  show_all=True → interior items culled (exterior-style)")
                        new_vertices, new_lines, new_areas, new_items, new_holes = _cull_exterior(
                            layer_id,
                            vertices, lines, areas, items, holes,
                            self.log,
                            layer_alt_m=_layer_alt_m,
                            layer_top_m=_layer_top_m,
                            meters_per_unit=meters_per_unit,
                            source_vertices=source_vertices,
                            source_items=source_items,
                            external_areas=layer.get("externalAreas", {}),
                        )
                elif active_area_id:
                    render_mode = "INTERIOR"
                    self.log(f"\n  🏠 MODE: INTERIOR RENDER (Camera inside room '{areas[active_area_id].get('name')}')")
                    new_vertices, new_lines, new_areas, new_items, new_holes = _cull_interior(
                        layer_id, active_area_id,
                        vertices, lines, areas, items, holes,
                        self.log,
                        cam_x=cam_x,
                        cam_y=cam_y,
                        cam_dir_x=cam_dir_x,
                        cam_dir_y=cam_dir_y,
                        cam_target_x=cam_target_x,
                        cam_target_y=cam_target_y,
                        cam_height_m=cam_height_m,
                        fov_half_deg=cam_fov_half_deg,
                        max_view_dist_cm=cam_view_dist_cm,
                        meters_per_unit=meters_per_unit,
                        cm_per_unit=cm_per_unit,
                        source_vertices=source_vertices,
                        source_items=source_items,
                        strict_room_limit=STRICT_IMAGE_PORTAL_ROOM_LIMIT,
                        external_areas=layer.get("externalAreas", {}),
                    )
                else:
                    render_mode = "EXTERIOR"
                    self.log(f"\n  🌍 MODE: EXTERIOR RENDER (Camera outside all rooms — windows will render BLACK)")
                    new_vertices, new_lines, new_areas, new_items, new_holes = _cull_exterior(
                        layer_id,
                        vertices, lines, areas, items, holes,
                        self.log,
                        layer_alt_m=_layer_alt_m,
                        layer_top_m=_layer_top_m,
                        meters_per_unit=meters_per_unit,
                        source_vertices=source_vertices,
                        source_items=source_items,
                        external_areas=layer.get("externalAreas", {}),
                    )

                self.log(f"\n  ✂️  Optimisation Results for {layer_id}:")
                self.log(f"     Areas:    {len(areas)} → {len(new_areas)}  (removed {len(areas) - len(new_areas)})")
                self.log(f"     Lines:    {len(lines)} → {len(new_lines)}  (removed {len(lines) - len(new_lines)})")
                self.log(f"     Holes:    {len(holes)} → {len(new_holes)}  (removed {len(holes) - len(new_holes)})")
                self.log(f"     Items:    {len(items)} → {len(new_items)}  (removed {len(items) - len(new_items)})")
                self.log(f"     Vertices: {len(vertices)} → {len(new_vertices)}")

                totals["areas_after"] += len(new_areas)
                totals["items_after"] += len(new_items)
                totals["lines_after"] += len(new_lines)
                totals["holes_after"] += len(new_holes)

                # Reconstruct layer
                layer["vertices"]    = new_vertices
                layer["lines"]       = new_lines
                layer["areas"]       = new_areas
                layer["items"]       = new_items
                layer["holes"]       = new_holes
                if "externalAreas" in layer:
                    layer["externalAreas"] = dict(layer.get("externalAreas", {}))
                if "structures" in layer:
                    layer["structures"] = dict(layer.get("structures", {}))
                if "customLights" in layer:
                    layer["customLights"] = dict(layer.get("customLights", {}))
                if "sceneLights" in layer:
                    layer["sceneLights"] = dict(layer.get("sceneLights", {}))
                layer["render_mode"] = render_mode  # Pass to Godot renderer

                self._log_data["layers"][layer_id] = {
                    "render_mode":   render_mode,
                    "active_room":   active_area_id,
                    "cam_dir":       (round(cam_dir_x, 4), round(cam_dir_y, 4)) if render_mode == "INTERIOR" else None,
                    "fov_half_deg":  cam_fov_half_deg if render_mode == "INTERIOR" else None,
                    "view_dist_cm":  cam_view_dist_cm if render_mode == "INTERIOR" else None,
                    "stats_before":  {
                        "areas": len(areas), "lines": len(lines),
                        "holes": len(holes), "items": len(items),
                    },
                    "stats_after":   {
                        "areas": len(new_areas), "lines": len(new_lines),
                        "holes": len(new_holes), "items": len(new_items),
                    },
                }

            # ── Summary ───────────────────────────────────────────────────────
            self._log_data["render_mode"] = render_mode
            self._log_data["summary"]     = totals

            self.log(f"\n{'='*60}")
            self.log(f"📊 CULLING SUMMARY  [{render_mode}]")
            self.log(f"{'='*60}")
            self.log(f"  Render Mode: {render_mode}")
            self.log(f"  Camera Plan: ({cam_x:.1f}, {cam_y:.1f}) cm")
            self.log(f"  Areas:  {totals['areas_before']} → {totals['areas_after']}  (removed {totals['areas_before'] - totals['areas_after']})")
            self.log(f"  Items:  {totals['items_before']} → {totals['items_after']}  (removed {totals['items_before'] - totals['items_after']})")
            self.log(f"  Lines:  {totals['lines_before']} → {totals['lines_after']}  (removed {totals['lines_before'] - totals['lines_after']})")
            self.log(f"  Holes:  {totals['holes_before']} → {totals['holes_after']}  (removed {totals['holes_before'] - totals['holes_after']})")
            if render_mode == "EXTERIOR":
                self.log("  ⚫ Window/door openings will render as BLACK (no interior backdrop)")
            if render_mode == "TOP_VIEW":
                self.log(f"  🔭 Top-view render | show_all={show_all} | Camera placed above home centroid")
                if not show_all:
                    self.log("  📦 Interior assets preserved (show_all=False)")
            self.log(f"{'='*60}")

            new_payload = payload.copy()
            new_payload["floor_plan_data"] = (
                json.dumps(fp_data) if isinstance(fp_str, str) else fp_data
            )

            self._save_json_log()
            return new_payload

        except Exception as exc:
            self.log(f"❌ Error during scene culling: {exc}")
            self._log_data["errors"].append(str(exc))
            import traceback
            traceback.print_exc()
            self._save_json_log()
            return payload  # Fail-safe: return original


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Running optimization test...")
    pass

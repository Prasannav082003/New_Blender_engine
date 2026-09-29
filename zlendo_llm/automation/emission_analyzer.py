"""
Per-fixture emission analysis.

The colour pass in `color_analyzer.py` answers "how bright should the ROOM
be".  This module answers the question underneath it: "what should EACH
light asset emit".  Those are different problems and they failed
differently.  A scene can have a perfectly chosen illuminationLevel and
still look wrong because the six lamps in it span 2000K-7500K, because the
ceiling pendant is dimmer than the floor lamps, or because one lamp's
`light_meta.intensity` is 0 while its shade is clearly modelled as lit.
No amount of global adjustment fixes any of those.

Everything here is deterministic - no model is contacted.  It reads the
(culled) scene and produces the compact context the emission agent needs,
plus the derived geometry the agent cannot compute reliably on its own:
emitter area, neighbour distances, ceiling clearance, window proximity and
camera visibility.  Asking a model to do trigonometry on raw millimetre
coordinates is how you get confident, wrong numbers.

Units: floor_plan_data is in MILLIMETRES.  Everything this module emits is
in METRES, because that is what the rest of zlendo_llm speaks and mixing
the two is the oldest bug in this pipeline.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .color_analyzer import (
    _hex_to_rgb,
    classify_luminance,
    relative_luminance,
    analyze_scene_colors,
)

log = logging.getLogger(__name__)

MM = 1000.0  # floor_plan_data millimetres -> metres

# Distance at which one neighbour contributes half a unit of crowding.
# Kept in step with CROWDING_RADIUS in emission_agent.py - the two
# measure the same thing, one for the prompt and one for the validator.
CROWDING_REFERENCE_DISTANCE = 2.5

# Fixtures within this distance of one another (3D) are treated as one
# group sharing a pool of light.
#
# Kept deliberately tight. Grouping is single-linkage, which CHAINS: at
# 2.0 m the reference scene collapsed into one cluster of six, because each
# lamp was within 2 m of the next one along even though the ends were 4 m
# apart. A "cluster" containing every fixture in the room says nothing and
# ladders the whole room down. At 1.3 m the same scene resolves into the
# two groups that are actually there - the row of three floor lamps, and
# the tripod-plus-side-table pair - and leaves the pendant standalone,
# which is correct: it hangs 1.5 m above them.
CLUSTER_DISTANCE = 1.3


# ── Fixture taxonomy ──────────────────────────────────────────────────────

# Matched against the GLB path and the asset name, most specific first.
# The category drives the default role, which drives the intensity
# hierarchy.  Getting this wrong is what produced "floor lamp brighter than
# the ceiling pendant" scenes.
#
# Patterns are matched against NORMALISED text (see _normalise below), so
# they are written with spaces: the asset library spells the same fixture
# "TwinRingPendantLight", "Twin_Ring_Pendant_Light" and "Twin Ring Pendant
# Light" in the GLB path, the SVG path and the display name respectively.
# Matching the raw strings caught roughly one spelling in three.
_CATEGORY_PATTERNS: List[Tuple[str, str]] = [
    ("pendant", r"hanging light|pendant|chandelier|suspended"),
    ("ceiling", r"ceiling light|flush mount|downlight|spot light|cove light"),
    ("wall", r"wall light|sconce|wall lamp|picture light"),
    ("table", r"table lamp|desk lamp|side.{0,8}table|bedside|table.{0,8}with lamp"),
    ("floor", r"floor lamp|standing lamp|stand light|tripod|torch"),
    ("lamp", r"\blamp\b|\blight\b"),
]

# Default role by category.  "key" is the room's primary source and must
# stay the brightest; everything else is measured against it.
_ROLE_BY_CATEGORY = {
    "pendant": "key",
    "ceiling": "key",
    "wall": "wall_wash",
    "table": "accent",
    "floor": "fill",
    "lamp": "fill",
    "unknown": "fill",
}

# Shades that only glow on the underside vs. shades that glow all over.
# A bare sphere at intensity 1.0 puts far more visible emission on screen
# than an opaque bell shade at the same value.
_OPEN_EMITTER_HINT = r"sphere|globe|orb|bulb|ring|tube|strip|neon|glow"
_SHADED_EMITTER_HINT = r"shade|bell|cone|drum|dome|hood"


def _normalise(*parts: str) -> str:
    """Lowercase, split CamelCase, collapse separators to single spaces.

    'glb-assets/.../WoodenSidesTablewithLamp_HP_low.glb' becomes
    '... wooden sides tablewith lamp hp low glb', which the space-separated
    patterns above can actually match. Without this step every asset whose
    name is CamelCase falls through to the generic 'lamp' bucket and gets
    the wrong role.
    """
    text = " ".join(str(p or "") for p in parts)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)   # CamelCase -> Camel Case
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _classify_fixture(
    name: str,
    glb_url: str,
    height: float = 0.0,
    altitude: float = 0.0,
    mounting_type: str = "general",
) -> str:
    """Category from the asset's names, with geometry as the tie-breaker.

    Names are unreliable - 'Modern tall Lamp' says nothing about whether it
    stands on the floor or on a side table. Its dimensions do: a 1.5 m
    fixture sitting at floor level is a floor lamp whatever it is called.
    Name first (it is specific when present), geometry second (it is always
    there).
    """
    if mounting_type == "ceiling_mount":
        blob = _normalise(glb_url, name)
        return "pendant" if re.search(r"hanging|pendant|chandelier", blob) else "ceiling"
    if mounting_type in ("wall_mount", "wall"):
        return "wall"

    blob = _normalise(glb_url, name)
    for category, pattern in _CATEGORY_PATTERNS:
        if category == "lamp":
            continue  # generic bucket, only after the geometry check below
        if re.search(pattern, blob):
            return category

    # Geometry fallback. Heights are metres.
    if altitude > 1.5:
        return "ceiling"
    if height >= 1.2:
        return "floor"
    if height > 0:
        return "table"
    return "lamp" if re.search(r"\blamp\b|\blight\b", blob) else "unknown"


def _emitter_openness(name: str, glb_url: str) -> str:
    """'open' (whole emitter visible), 'shaded' (partly occluded), or 'unknown'."""
    blob = _normalise(glb_url, name)
    if re.search(_SHADED_EMITTER_HINT, blob):
        return "shaded"
    if re.search(_OPEN_EMITTER_HINT, blob):
        return "open"
    return "unknown"


# ── Data structures ───────────────────────────────────────────────────────

@dataclass
class EmissionFixture:
    """One light asset with everything the agent needs to reason about it."""

    id: str
    name: str
    asset_id: str = ""
    category: str = "unknown"          # pendant / ceiling / wall / table / floor
    default_role: str = "fill"         # key / fill / accent / wall_wash
    mounting_type: str = "general"

    # Placement, metres
    x: float = 0.0
    y: float = 0.0
    altitude: float = 0.0              # base height above floor
    top_height: float = 0.0            # altitude + fixture height
    emitter_height: float = 0.0        # where the light actually comes from

    # Size, metres
    width: float = 0.0
    depth: float = 0.0
    height: float = 0.0
    emitter_area_m2: float = 0.0       # disc proxy, see _emitter_area()
    emitter_openness: str = "unknown"

    # Derived geometry
    ceiling_clearance: float = 0.0     # gap between fixture top and ceiling
    nearest_wall_dist: float = 0.0
    nearest_neighbour_dist: float = 0.0
    nearest_neighbour_id: str = ""
    crowding_index: float = 0.0     # how much company this fixture has
    cluster_id: int = -1            # -1 = standalone
    cluster_size: int = 1
    nearest_window_dist: float = 0.0
    in_camera_frame: bool = False
    camera_dist: float = 0.0

    # IES
    has_ies: bool = False
    ies_lumens: float = 0.0
    ies_watts: float = 0.0

    # Current state
    current_intensity: Optional[float] = None
    current_temperature: Optional[float] = None
    locked: bool = False               # user pinned these values; do not touch

    def summary_line(self) -> str:
        cur = (
            f"int={self.current_intensity:g} temp={self.current_temperature:g}K"
            if self.current_intensity is not None
            and self.current_temperature is not None
            else "no light_meta"
        )
        flags = []
        if self.in_camera_frame:
            flags.append("IN FRAME")
        if self.has_ies:
            flags.append(f"IES {self.ies_lumens:g}lm")
        if self.locked:
            flags.append("LOCKED")
        if self.ceiling_clearance and self.ceiling_clearance < 0.25:
            flags.append(f"ceiling gap {self.ceiling_clearance:.2f}m")
        if self.cluster_size > 1:
            flags.append(f"cluster #{self.cluster_id} of {self.cluster_size}")
        if self.emitter_openness == "shaded":
            flags.append("OPAQUE SHADE")
        flag_text = ("  [" + ", ".join(flags) + "]") if flags else ""
        return (
            f"  {self.id[:8]}  {self.name[:34]:34s} "
            f"{self.category:8s} role={self.default_role:9s} "
            f"pos=({self.x:.2f},{self.y:.2f}) alt={self.altitude:.2f}m "
            f"emitter={self.emitter_area_m2:.3f}m2({self.emitter_openness}) crowd={self.crowding_index:.2f} "
            f"{cur}{flag_text}"
        )


@dataclass
class EmissionAnalysis:
    """Scene-level context plus every light asset in it."""

    fixtures: List[EmissionFixture] = field(default_factory=list)

    # Render intent
    day_render: bool = True
    night_render: bool = False
    time_of_day: str = "day"
    color_mood: str = "neutral"
    illumination_level: int = 3
    environment_url: str = ""

    # Sun / sky
    sun_altitude: float = 45.0
    sun_azimuth: float = 0.0
    ambient_intensity: float = 0.3
    directional_intensity: float = 1.0

    # Room
    room_count: int = 0
    floor_area_m2: float = 0.0
    ceiling_height: float = 2.7
    volume_m3: float = 0.0
    min_x: float = 0.0
    max_x: float = 0.0
    min_y: float = 0.0
    max_y: float = 0.0

    # Surfaces (from color_analyzer, so the two passes cannot disagree)
    avg_wall_luminance: float = 0.5
    avg_floor_luminance: float = 0.5
    avg_ceiling_luminance: float = 0.5
    dominant_mood: str = "neutral"
    floor_is_warm: bool = False
    room_albedo: float = 0.5

    # Daylight
    window_count: int = 0
    window_area_m2: float = 0.0
    window_to_floor_ratio: float = 0.0

    # Camera
    camera_x: float = 0.0
    camera_y: float = 0.0
    camera_height: float = 1.5
    camera_fov: float = 45.0

    # Inference
    inferred_room_type: str = "living_room"
    furniture: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # ------------------------------------------------------------------
    def summary_for_llm(self) -> str:
        """Compact prose for the prompt.  Deliberately not raw JSON: the
        full scene is tens of thousands of tokens and almost none of it
        bears on emission."""
        mode = "NIGHT" if self.night_render else ("DAY" if self.day_render else "SUNSET")
        lines = [
            f"RENDER: {mode} render, timeOfDay={self.time_of_day}, "
            f"colorMood={self.color_mood}, illuminationLevel={self.illumination_level}",
            f"  Sun: altitude {self.sun_altitude:.0f} deg, azimuth {self.sun_azimuth:.0f} deg, "
            f"directional intensity {self.directional_intensity:g}, "
            f"ambient {self.ambient_intensity:g}",
            "",
            f"ROOM: {self.floor_area_m2:.1f} m2 floor, {self.ceiling_height:.2f} m ceiling, "
            f"{self.volume_m3:.0f} m3",
            f"  Bounds (m): x {self.min_x:.2f}..{self.max_x:.2f}, "
            f"y {self.min_y:.2f}..{self.max_y:.2f}",
            f"  Inferred type: {self.inferred_room_type}",
            f"  Wall luminance {self.avg_wall_luminance:.2f}, "
            f"floor {self.avg_floor_luminance:.2f}, "
            f"ceiling {self.avg_ceiling_luminance:.2f}",
            f"  Estimated room albedo {self.room_albedo:.2f} - "
            + (
                "bright room, bounces aggressively, needs LESS emission per fixture"
                if self.room_albedo > 0.6
                else "dark room, absorbs light, needs MORE emission per fixture"
                if self.room_albedo < 0.35
                else "mid-albedo room"
            ),
            f"  Dominant surface tone: {self.dominant_mood}"
            + (" (warm floor - cool light will read wrong on it)" if self.floor_is_warm else ""),
            "",
            f"DAYLIGHT: {self.window_count} window(s), {self.window_area_m2:.1f} m2 glazing, "
            f"window-to-floor ratio {self.window_to_floor_ratio * 100:.1f}%",
        ]
        if self.furniture:
            lines.append("")
            lines.append("FURNITURE: " + ", ".join(self.furniture[:12]))

        lines.append("")
        lines.append(
            f"CAMERA: at ({self.camera_x:.2f}, {self.camera_y:.2f}) "
            f"height {self.camera_height:.2f} m, fov {self.camera_fov:.0f} deg"
        )
        lines.append("")
        lines.append(f"LIGHT ASSETS ({len(self.fixtures)}):")
        for fx in self.fixtures:
            lines.append(fx.summary_line())

        # Call out the inconsistencies explicitly.  A model reading a table
        # of numbers will often miss a 5500K spread; a sentence naming it
        # never gets missed.
        problems = self.detect_problems()
        if problems:
            lines.append("")
            lines.append("DETECTED PROBLEMS IN CURRENT VALUES:")
            for p in problems:
                lines.append(f"  - {p}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    def detect_problems(self) -> List[str]:
        """Deterministic problem detection.  Also used for the job log so
        an operator can see what the pass reacted to without re-rendering."""
        out: List[str] = []
        # Crowding only means something relative to the rest of the room: in
        # a six-lamp room every fixture has a high absolute index, and
        # reporting all six as "crowded" is noise, not a finding.
        crowds = sorted(f.crowding_index for f in self.fixtures)
        crowd_ref = crowds[len(crowds) // 2] if crowds else 0.0
        lit = [f for f in self.fixtures if f.current_temperature]
        if len(lit) >= 2:
            temps = [f.current_temperature for f in lit]
            spread = max(temps) - min(temps)
            if spread > 800:
                out.append(
                    f"Colour temperature spread is {spread:.0f}K "
                    f"({min(temps):.0f}-{max(temps):.0f}K) across {len(lit)} fixtures - "
                    f"a single room should stay within ~400K."
                )

        keys = [f for f in self.fixtures if f.default_role == "key"]
        others = [f for f in self.fixtures if f.default_role != "key"]
        if keys and others:
            key_max = max((f.current_intensity or 0) for f in keys)
            other_max = max((f.current_intensity or 0) for f in others)
            if other_max > key_max:
                out.append(
                    f"Inverted hierarchy: a non-key fixture is at {other_max:g} "
                    f"while the ceiling/key fixture is at {key_max:g}."
                )

        for f in self.fixtures:
            if f.current_intensity is not None and f.current_intensity <= 0.0:
                out.append(
                    f"'{f.name}' has intensity 0 - it will render as an unlit "
                    f"prop even though it is modelled as a light."
                )
            if f.ceiling_clearance and f.ceiling_clearance < 0.20:
                out.append(
                    f"'{f.name}' sits {f.ceiling_clearance:.2f} m below the ceiling - "
                    f"hot-spot risk, cap its intensity."
                )

        seen: set = set()
        for f in self.fixtures:
            if f.cluster_size > 1 and f.cluster_id not in seen:
                seen.add(f.cluster_id)
                members = [
                    g for g in self.fixtures if g.cluster_id == f.cluster_id
                ]
                lit = [g for g in members if (g.current_intensity or 0) > 0]
                if len(lit) >= 2:
                    vals = ", ".join(
                        f"{g.name}={g.current_intensity:g}" for g in lit
                    )
                    spread = max(g.current_intensity for g in lit) / max(
                        1e-6, min(g.current_intensity for g in lit)
                    )
                    if spread < 1.4:
                        out.append(
                            f"Cluster #{f.cluster_id}: {len(members)} fixtures "
                            f"light the same volume at near-identical levels "
                            f"({vals}) - they will read as one blown patch. One "
                            f"should lead and the rest step down."
                        )
        return out


# ── Geometry helpers ──────────────────────────────────────────────────────

def _emitter_area(width: float, depth: float, openness: str) -> float:
    """Rough emissive surface area in m2.

    A proxy, not a measurement - we do not have the GLB here.  The fixture
    footprint is treated as a disc of the mean plan dimension.  A shaded
    fixture only radiates through its aperture, so it gets a fraction.

    This exists so intensity can be normalised by area.  Treating
    `intensity` as brightness rather than radiance is why a 950 mm twin-ring
    pendant and a 420 mm sphere at the same value look nothing alike.
    """
    d = max(0.01, (width + depth) / 2.0)
    area = math.pi * (d / 2.0) ** 2
    if openness == "shaded":
        area *= 0.45
    return round(area, 4)


def _point_in_frame(
    cam_x: float, cam_y: float, tgt_x: float, tgt_y: float,
    px: float, py: float, hfov_deg: float, margin_deg: float = 8.0,
) -> bool:
    """Plan-view frustum test.  Ignores vertical extent deliberately: a
    fixture just outside the vertical frame still bleeds bloom into it."""
    vx, vy = tgt_x - cam_x, tgt_y - cam_y
    px_, py_ = px - cam_x, py - cam_y
    vlen = math.hypot(vx, vy)
    plen = math.hypot(px_, py_)
    if vlen < 1e-6 or plen < 1e-6:
        return True
    cos_a = max(-1.0, min(1.0, (vx * px_ + vy * py_) / (vlen * plen)))
    angle = math.degrees(math.acos(cos_a))
    return angle <= (hfov_deg / 2.0 + margin_deg)


def _horizontal_fov(vfov_deg: float, aspect: float = 16.0 / 9.0) -> float:
    v = math.radians(max(1.0, min(179.0, vfov_deg)))
    return math.degrees(2.0 * math.atan(math.tan(v / 2.0) * aspect))


def _infer_room_type(furniture: List[str]) -> str:
    blob = " ".join(furniture).lower()
    if re.search(r"\bbed\b|headboard|nightstand|wardrobe", blob):
        return "bedroom"
    if re.search(r"kitchen|cabinet|countertop|refrigerator|cooktop|sink", blob):
        return "kitchen"
    if re.search(r"desk|office chair|bookshelf|workstation", blob):
        return "office"
    if re.search(r"dining|dinner table", blob):
        return "dining_room"
    if re.search(r"bath|shower|vanity|toilet", blob):
        return "bathroom"
    if re.search(r"sofa|sectional|couch|coffee table|tv unit|armchair", blob):
        return "living_room"
    return "living_room"


def _assign_clusters(a: "EmissionAnalysis") -> None:
    """Group fixtures that light the same volume, by single linkage in 3D.

    Single linkage on purpose: a row of three lamps each 1.5 m from the next
    is one group of three, not two overlapping pairs, even though the ends
    are 3 m apart. They form a continuous bright band in the render, and
    that band is what has to be broken up.

    Grouping is what makes "one of these leads and the rest step down"
    expressible at all. A per-fixture crowding score says how much company
    a fixture has, but it cannot say WHICH of the neighbours should be the
    bright one - that needs the set.
    """
    parent = {f.id: f.id for f in a.fixtures}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, f in enumerate(a.fixtures):
        for g in a.fixtures[i + 1:]:
            d = math.sqrt(
                (f.x - g.x) ** 2
                + (f.y - g.y) ** 2
                + (f.emitter_height - g.emitter_height) ** 2
            )
            if d <= CLUSTER_DISTANCE:
                ra, rb = find(f.id), find(g.id)
                if ra != rb:
                    parent[ra] = rb

    groups: Dict[str, List[str]] = {}
    for f in a.fixtures:
        groups.setdefault(find(f.id), []).append(f.id)

    by_id = {f.id: f for f in a.fixtures}
    next_id = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        for fid in members:
            by_id[fid].cluster_id = next_id
            by_id[fid].cluster_size = len(members)
        next_id += 1


# ── Main entry point ──────────────────────────────────────────────────────

def analyze_scene_emission(scene: Dict[str, Any]) -> EmissionAnalysis:
    """Build the emission context from a (culled) scene dict.

    Never raises: a scene it cannot read yields an analysis with no
    fixtures, and the caller then leaves the scene alone.
    """
    a = EmissionAnalysis()

    try:
        _populate(scene, a)
    except Exception:  # noqa: BLE001 - this pass must never break a render
        log.exception("Emission analysis failed; returning empty analysis")
        a.fixtures = []
    return a


def _populate(scene: Dict[str, Any], a: EmissionAnalysis) -> None:
    a.day_render = bool(scene.get("day_render", True))
    a.night_render = bool(scene.get("night_render", False))
    a.environment_url = str(scene.get("environment_url", "") or "")

    ls = scene.get("_lightingSettings") or {}
    if isinstance(ls, dict):
        a.time_of_day = str(ls.get("timeOfDay", "day"))
        a.sun_altitude = float(ls.get("sunAltitude", 45.0) or 45.0)
        a.sun_azimuth = float(ls.get("sunAzimuth", 0.0) or 0.0)
        a.color_mood = str(ls.get("colorMood", scene.get("colorMood", "neutral")))
        a.illumination_level = int(ls.get("illuminationLevel", scene.get("illuminationLevel", 3)) or 3)
        a.ambient_intensity = float(ls.get("ambient", 0.3) or 0.3)
        a.directional_intensity = float(ls.get("directional", 1.0) or 1.0)

    amb = scene.get("ambient_light") or {}
    if isinstance(amb, dict) and "intensity" in amb:
        a.ambient_intensity = float(amb.get("intensity") or a.ambient_intensity)
    dirl = scene.get("directional_light") or {}
    if isinstance(dirl, dict) and "intensity" in dirl:
        a.directional_intensity = float(dirl.get("intensity") or a.directional_intensity)

    # ── Camera.  three.js is right-handed Y-up: plan coords are (x, z). ──
    cam = scene.get("threejs_camera") or {}
    if isinstance(cam, dict):
        pos = cam.get("position") or {}
        tgt = cam.get("target") or {}
        a.camera_x = float(pos.get("x", 0.0) or 0.0)
        a.camera_y = float(pos.get("z", 0.0) or 0.0)
        a.camera_height = float(pos.get("y", 1.5) or 1.5)
        a.camera_fov = float(cam.get("fov", 45.0) or 45.0)
        tgt_x = float(tgt.get("x", a.camera_x) or a.camera_x)
        tgt_y = float(tgt.get("z", a.camera_y) or a.camera_y)
    else:
        tgt_x, tgt_y = a.camera_x, a.camera_y

    # ── Surfaces: reuse the colour pass so the two cannot disagree. ──
    try:
        colors = analyze_scene_colors(scene)
        a.avg_wall_luminance = colors.avg_wall_luminance
        a.avg_floor_luminance = colors.avg_floor_luminance
        a.avg_ceiling_luminance = colors.avg_ceiling_luminance
        a.dominant_mood = colors.dominant_mood
        a.room_count = colors.room_count
    except Exception:  # noqa: BLE001
        log.warning("Colour analysis unavailable inside emission pass")

    a.floor_is_warm = a.dominant_mood == "warm" or a.avg_floor_luminance < 0.5
    # Walls dominate what the eye reads as room brightness.
    a.room_albedo = round(
        (a.avg_wall_luminance * 2 + a.avg_floor_luminance + a.avg_ceiling_luminance) / 4.0,
        3,
    )

    # ── Floor plan ──
    fpd = scene.get("floor_plan_data")
    if isinstance(fpd, str):
        try:
            fpd = json.loads(fpd)
        except (json.JSONDecodeError, TypeError):
            log.warning("emission: floor_plan_data is not parseable JSON")
            return
    if not isinstance(fpd, dict):
        return

    layers = fpd.get("layers")
    if not isinstance(layers, dict):
        return

    ies_profiles = fpd.get("iesProfiles") or {}
    ies_default: Optional[Dict[str, Any]] = None
    if isinstance(ies_profiles, dict) and ies_profiles:
        first = next(iter(ies_profiles.values()), None)
        if isinstance(first, dict):
            ies_default = first.get("parsed") or {}

    all_x: List[float] = []
    all_y: List[float] = []
    raw_items: List[Tuple[str, Dict[str, Any]]] = []
    furniture: List[str] = []
    window_area = 0.0
    window_pts: List[Tuple[float, float]] = []

    for layer in layers.values():
        if not isinstance(layer, dict):
            continue

        # Room bounds come from the areas' INNER polygons - the usable
        # floor, not the outside of the masonry. Measuring to the outer
        # polygon inflates the area by the wall thickness on all four
        # sides (9 m2 on a room this size) and, worse, makes every
        # fixture's distance-to-wall wrong by 0.23 m, which is exactly the
        # scale at which the wall hot-spot rule triggers.
        for area in (layer.get("areas") or {}).values():
            if not isinstance(area, dict):
                continue
            geom = area.get("geometry") or {}
            inner = geom.get("innerPolygon") or []
            for pt in inner:
                if isinstance(pt, dict):
                    all_x.append(float(pt.get("x", 0.0)) / MM)
                    all_y.append(float(pt.get("y", 0.0)) / MM)
            props = area.get("properties") or {}
            h = (props.get("height") or {}).get("length")
            if h:
                a.ceiling_height = float(h) / MM

        # Fallback only: a scene with no areas still needs bounds, and the
        # wall polygons are the next best thing.
        if not all_x:
            for line in (layer.get("lines") or {}).values():
                if not isinstance(line, dict):
                    continue
                geom = line.get("geometry") or {}
                for pt in (geom.get("cleanedPolygon") or []):
                    if isinstance(pt, dict):
                        all_x.append(float(pt.get("x", 0.0)) / MM)
                        all_y.append(float(pt.get("y", 0.0)) / MM)

        # Windows live as holes on the wall lines.
        for hole in (layer.get("holes") or {}).values():
            if not isinstance(hole, dict):
                continue
            if str(hole.get("type", "")).lower() != "window":
                continue
            w = float(hole.get("width", 0.0) or 0.0) / MM
            h = float(hole.get("height", 0.0) or 0.0) / MM
            window_area += w * h
            a.window_count += 1
            # Position it along its host wall so we can measure lamp-to-window
            # distance - a lamp sitting in the daylight pool must not compete.
            host = (layer.get("lines") or {}).get(hole.get("wallId") or hole.get("line"))
            if isinstance(host, dict):
                verts = host.get("vertices") or []
                vmap = layer.get("vertices") or {}
                if len(verts) == 2 and all(v in vmap for v in verts):
                    v0, v1 = vmap[verts[0]], vmap[verts[1]]
                    x0, y0 = float(v0.get("x", 0)) / MM, float(v0.get("y", 0)) / MM
                    x1, y1 = float(v1.get("x", 0)) / MM, float(v1.get("y", 0)) / MM
                    wall_len = math.hypot(x1 - x0, y1 - y0)
                    if wall_len > 1e-6:
                        t = (float(hole.get("offset", 0.0) or 0.0) / MM + w / 2.0) / wall_len
                        t = max(0.0, min(1.0, t))
                        window_pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))

        for item_id, item in (layer.get("items") or {}).items():
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("light_meta"), dict):
                raw_items.append((item_id, item))
            else:
                nm = str(item.get("name", "")).strip()
                if nm:
                    furniture.append(nm)

    if all_x and all_y:
        a.min_x, a.max_x = min(all_x), max(all_x)
        a.min_y, a.max_y = min(all_y), max(all_y)
        a.floor_area_m2 = round((a.max_x - a.min_x) * (a.max_y - a.min_y), 2)
        a.volume_m3 = round(a.floor_area_m2 * a.ceiling_height, 1)

    a.window_area_m2 = round(window_area, 2)
    a.window_to_floor_ratio = round(
        window_area / a.floor_area_m2, 4
    ) if a.floor_area_m2 > 0 else 0.0

    a.furniture = furniture
    a.inferred_room_type = _infer_room_type(furniture + [i[1].get("name", "") for i in raw_items])

    hfov = _horizontal_fov(a.camera_fov)

    # ── Build the fixtures ──
    for item_id, item in raw_items:
        lm = item.get("light_meta") or {}
        props = item.get("properties") or {}

        def _dim(key: str, fallback: str) -> float:
            v = (props.get(key) or {}).get("length")
            if v is None:
                v = item.get(fallback)
            return float(v or 0.0) / MM

        name = str(item.get("name", "unnamed"))
        glb = str((item.get("asset_urls") or {}).get("GLB_File_URL", ""))
        mount = str(item.get("mounting_type", "general"))

        w = _dim("width", "width")
        d = _dim("depth", "depth")
        h = _dim("height", "height")
        alt = _dim("altitude", "altitude")

        category = _classify_fixture(name, glb, height=h, altitude=alt, mounting_type=mount)
        openness = _emitter_openness(name, glb)

        fx = EmissionFixture(
            id=item_id,
            name=name,
            asset_id=str(item.get("assetId", "")),
            category=category,
            default_role=_ROLE_BY_CATEGORY.get(category, "fill"),
            mounting_type=mount,
            x=round(float(item.get("x", 0.0) or 0.0) / MM, 3),
            y=round(float(item.get("y", 0.0) or 0.0) / MM, 3),
            altitude=round(alt, 3),
            top_height=round(alt + h, 3),
            width=round(w, 3),
            depth=round(d, 3),
            height=round(h, 3),
            emitter_area_m2=_emitter_area(w, d, openness),
            emitter_openness=openness,
            current_intensity=(
                float(lm["intensity"]) if isinstance(lm.get("intensity"), (int, float)) else None
            ),
            current_temperature=(
                float(lm["temperature"]) if isinstance(lm.get("temperature"), (int, float)) else None
            ),
            locked=bool(lm.get("locked") or lm.get("userLocked") or item.get("_lightLocked")),
        )

        fx.ceiling_clearance = round(max(0.0, a.ceiling_height - fx.top_height), 3)
        # The light leaves the middle of the fixture body, not its base.
        # This is the height that matters for how far two fixtures actually
        # are from each other.
        fx.emitter_height = round(alt + h / 2.0, 3)

        if all_x and all_y:
            fx.nearest_wall_dist = round(
                min(
                    abs(fx.x - a.min_x), abs(a.max_x - fx.x),
                    abs(fx.y - a.min_y), abs(a.max_y - fx.y),
                ),
                3,
            )

        if window_pts:
            fx.nearest_window_dist = round(
                min(math.hypot(fx.x - wx, fx.y - wy) for wx, wy in window_pts), 3
            )

        fx.camera_dist = round(math.hypot(fx.x - a.camera_x, fx.y - a.camera_y), 3)
        fx.in_camera_frame = _point_in_frame(
            a.camera_x, a.camera_y, tgt_x, tgt_y, fx.x, fx.y, hfov
        )

        if ies_default and item.get("iesProfileId"):
            fx.has_ies = True
            fx.ies_lumens = float(ies_default.get("lumens", 0.0) or 0.0)
            fx.ies_watts = float(ies_default.get("watts", 0.0) or 0.0)

        a.fixtures.append(fx)

    # Neighbour distances need every fixture placed first.
    #
    # Measured in 3D, not in plan. A ceiling pendant and a floor lamp can
    # sit 1.0 m apart on the plan and still be 1.7 m apart in the room,
    # lighting completely different volumes. Using plan distance made every
    # scene with a pendant over a side table look like a cluster and dimmed
    # both fixtures for a collision that does not exist.
    for fx in a.fixtures:
        best, best_id = float("inf"), ""
        for other in a.fixtures:
            if other.id == fx.id:
                continue
            dist = math.sqrt(
                (fx.x - other.x) ** 2
                + (fx.y - other.y) ** 2
                + (fx.emitter_height - other.emitter_height) ** 2
            )
            if dist < best:
                best, best_id = dist, other.id
        if best < float("inf"):
            fx.nearest_neighbour_dist = round(best, 3)
            fx.nearest_neighbour_id = best_id

        # Purely geometric crowding: every other fixture summed with a
        # smooth distance falloff, so three lamps in a row score higher than
        # two and a lamp under a pendant scores lower than one beside it.
        # The agent computes a second, flux-weighted version that also knows
        # how bright each neighbour is; this one exists so the MODEL can see
        # the clustering in its prompt rather than having to infer it from
        # six sets of coordinates.
        fx.crowding_index = round(
            sum(
                1.0 / (1.0 + (
                    math.sqrt(
                        (fx.x - o.x) ** 2
                        + (fx.y - o.y) ** 2
                        + (fx.emitter_height - o.emitter_height) ** 2
                    ) / CROWDING_REFERENCE_DISTANCE
                ) ** 2)
                for o in a.fixtures if o.id != fx.id
            ),
            3,
        )

    _assign_clusters(a)

    # Ceiling/pendant assets are the key light.  If the scene has none, the
    # tallest fixture is promoted so there is always exactly one key.
    if a.fixtures and not any(f.default_role == "key" for f in a.fixtures):
        tallest = max(a.fixtures, key=lambda f: f.top_height)
        tallest.default_role = "key"

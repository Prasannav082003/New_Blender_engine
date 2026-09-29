"""
Per-fixture emission agent: decides `light_meta.intensity` and
`light_meta.temperature` for every light asset in the scene.

The contract with the rest of the pipeline is narrow on purpose.  This pass
does NOT place lights, does NOT touch illuminationLevel, and does NOT move
anything.  It only answers, per existing asset: how bright, what colour,
on or off.  Placement is `lighting_agent.py`; global level is
`lighting_adjuster.py`.  Keeping the three separate is what lets each one
fail without taking the render with it.

Two things make this pass safe to run unattended:

1.  Every number the model returns is re-derived through `_validate`,
    which enforces the physical constraints (ceiling clearance, proximity,
    hierarchy, temperature coherence) rather than trusting the model to
    have respected them.  The model chooses; the validator vetoes.

2.  `_fallback_emission` implements the same rules deterministically.  If
    Ollama is down, mid-pull, or returns garbage, the scene still gets
    coherent emission - it just gets it from arithmetic instead of a
    model.  A render box with no model must not produce worse images than
    one with a model, only less nuanced ones.

On intensity: it is computed on its merits - role, emitter area, room
albedo, and how crowded the fixture's neighbourhood is - and allowed to
spread as far as those say.  There is no configured MIN/MAX band.

Two earlier versions got this wrong in opposite directions.  The first
reduced a scalar by a cascade of multipliers; compounded they took floor
lamps to ~0.5 and the renderer returned lamps emitting nothing.  The second
mapped every fixture onto a [MIN, MAX] range, which stopped the dead lamps
but flattened the room - six fixtures all landing between 0.92 and 1.22 is
not a lit room, it is six lamps at one brightness.

What remains is a single safety net, DERIVED FROM THE SCENE rather than
configured: no enabled fixture is written below VISIBILITY_FRACTION of the
room's own key light.  A lamp under about half the key reads as unlit in a
room lit to that key, whatever the absolute numbers are.  Being a ratio, it
needs no calibration and no .env entry.
"""

from __future__ import annotations

import logging
import math
import os
import statistics
from typing import Any, Dict, List, Optional, Tuple

from .emission_analyzer import EmissionAnalysis, EmissionFixture

log = logging.getLogger(__name__)


# ── Hard limits ───────────────────────────────────────────────────────────

TEMP_MIN, TEMP_MAX = 1800.0, 7500.0      # matches schema.LIMITS["colorTemp"]


# ══════════════════════════════════════════════════════════════════════════
# How intensity is decided.  Read this before changing any number below it.
# ══════════════════════════════════════════════════════════════════════════
#
# Base value by role, then three modifiers - room albedo, emitter area, and
# neighbourhood crowding - applied on their merits with no band to compress
# them.  A safety floor at VISIBILITY_FRACTION of the key light catches the
# one failure mode that matters (a lamp so dim its shade stops reading as
# lit) without flattening everything else towards it.

# Base emission by role, in engine units, before any modifier.  This is the
# hierarchy: the ceiling fixture anchors the room and everything else sits
# under it.  Spread these further apart to make the room more dramatic,
# closer together to make it more even.
ROLE_INTENSITY = {
    "key": 1.60,
    "fill": 1.10,
    "wall_wash": 0.95,
    "accent": 0.85,
    "decorative": 0.60,
}

# The floor, as a fraction of the room's OWN key light rather than an
# absolute value.  Set low on purpose: this is a safety net for the one
# fixture that came out wrong, not a shaping tool.  At 0.50 it was catching
# three fills at once and pinning them all to the same value - flattening
# the room at the bottom of its range instead of the middle, which is the
# same bug wearing a different hat.  Scene-derived on purpose: the threshold where an emissive
# material stops reading as lit depends on the tonemapper, the exposure and
# the asset library, none of which this module can measure - but it tracks
# the key light, which this module sets.  A ratio needs no calibration.
VISIBILITY_FRACTION = 0.40

# Anti-garbage ceiling, as a multiple of the key light.  NOT a design band -
# it exists so that a model returning 900 does not write 900 into the scene.
SANITY_CEILING_FACTOR = 3.0

# ── Crowding ──────────────────────────────────────────────────────────────
#
# Distance at which a neighbour's crowding weight has fallen to one half.
# The falloff is smooth and unbounded rather than a threshold: the old rule
# cut hard at 1.5 m, so a lamp 1.49 m away lost a flat 15% and one 1.51 m
# away lost nothing, and only the single NEAREST neighbour counted at all -
# three lamps in a row were handled exactly like two.
CROWDING_RADIUS = 2.5

# How hard crowding pushes a fixture away from the room's average.
#
# Crowding is measured RELATIVE to the rest of the room, not absolutely. An
# absolute measure fails on exactly the scenes that need it: six lamps in
# one room all score highly, all get dimmed by the same amount, and the
# result is a uniformly darker room rather than a room where the crowded
# corner has yielded to the open one. What matters is which fixtures are
# more crowded THAN THE OTHERS.
#
# So a fixture with average company is untouched at 1.0, a fixture half
# again as crowded as average lands near 0.82, and an isolated fixture in a
# busy room is lifted slightly.
CROWDING_STRENGTH = 0.45

# Crowding depends on neighbour brightness, which depends on crowding.
# Solved by fixed-point iteration; it converges in two and settles by three.
CROWDING_PASSES = 3

# ── Room energy budget ────────────────────────────────────────────────────
#
# Per-fixture values are chosen on each fixture's own merits, and N of them
# then sum LINEARLY into one room. Nothing was budgeting that total, and it
# did not matter while most of the pass was inert - but once every fixture
# actually reached the renderer, six lamps put more light into an 84 m2 room
# than the sun did (interior 7.78 against a directional of 4.0), and the
# render came back uniformly white with every fixture indistinguishable.
#
# The rule is the engine's own, from the ceiling-fixture normalisation in
# image_glb_creation.gd: "N lights add energy linearly -> 21 fixtures = 21x
# one light. Divide per-fixture energy by sqrt(N) so total luminance is
# about sqrt(N) x base." Reusing that rather than inventing a second scheme
# keeps one idea in the codebase instead of two that can disagree.
#
# Expressed against a reference count so a room with the reference number of
# fixtures is unchanged: scale = sqrt(reference / N).
#
#   3 fixtures -> 1.00      6 fixtures -> 0.71      12 fixtures -> 0.50
#
# Bounded below so a very busy room dims rather than goes dark, and never
# above 1.0 - a sparse room is not a licence to overdrive what is there.
# Reference raised 3.0 -> 6.0.
#
# At 3.0 this pass was scaling a six-lamp room by 0.71, and that was
# compensating for a bug rather than for physics: the room's walls, ceiling
# and floor were at albedo 1.00, giving a 9x indirect gain, and the fixtures
# were being dimmed to survive a room that amplified everything. With the
# albedo capped at its physical maximum the amplification is gone, and the
# same 0.71 pushed three shades onto the engine's emission floor - dimming
# them out of visibility to solve a problem that no longer exists.
#
# Six is a normal, fully-furnished room, so it is the calibration point: a
# room of that size is now unscaled, and the sqrt(N) protection still
# engages for genuinely crowded scenes (12 fixtures -> 0.71).
ENERGY_BUDGET_REFERENCE_FIXTURES = 6.0
ENERGY_BUDGET_MIN_SCALE = 0.45

# Bounds on the crowding adjustment. Past the floor the cluster is a
# placement problem, not an emission one, and dimming further just turns a
# crowded corner into a dark one.
CROWDING_FLOOR = 0.60

# ── Cluster ladder ────────────────────────────────────────────────────────
#
# Crowding says HOW MUCH company a fixture has. It cannot say WHICH of the
# neighbours should be the bright one, because it is a per-fixture score
# and the answer is a property of the set. Three lamps in a row that are
# equally crowded all get the same crowding factor and stay equal to each
# other - a row of three identical bright shades, which is exactly the
# "many assets near each other, all high" the renders kept showing.
#
# So within each cluster the fixtures are ranked and stepped down: one
# leads at its full value, the next sits clearly under it, and so on. That
# is how a real room reads - one dominant lamp per corner, the rest
# supporting - and it is what breaks a row of equal shades into a
# composition.
CLUSTER_LADDER = [1.00, 0.75, 0.60, 0.50]
CLUSTER_LADDER_FLOOR = 0.45

# ── Opaque shades ─────────────────────────────────────────────────────────
#
# An open emitter (a bare globe, a lit ring) is MEANT to read as a bright
# source and can be pushed hard. A shade - bell, drum, dome, cone - is an
# opaque object with light behind it, and driving its emissive too hard
# does not make it brighter, it makes it stop looking like an object: the
# shade blows to flat white, loses its shading and silhouette, and reads as
# transparent. The bell lamp in the reference render failed exactly this
# way. Shaded emitters are therefore capped lower than open ones.
SHADED_EMITTER_CEILING = 0.80   # x the role's base value
OPEN_EMITTER_CEILING = 1.25
# Asymmetric on purpose. Crowding's job is to make a crowded fixture yield;
# rewarding an isolated one is a bonus, and letting that bonus run pushes
# the key light up and away from the fills until the visibility floor has
# to drag them back, flattening them together.
CROWDING_CEILING = 1.10

# The single most important constraint in this file.  A real room is lit by
# lamps that were bought to match; a 5000K spread is the difference between
# "architectural render" and "video game level".
TEMP_BAND = 400.0

# Kelvin range that reads as natural for each room type.  A living room at
# 5500K looks like a dentist's waiting area no matter how well exposed it is.
ROOM_TEMP_BANDS: Dict[str, Tuple[float, float]] = {
    "living_room": (2400.0, 3600.0),
    "bedroom": (2200.0, 3300.0),
    "dining_room": (2400.0, 3400.0),
    "kitchen": (3000.0, 5000.0),
    "office": (3200.0, 5000.0),
    "bathroom": (2700.0, 4500.0),
}
DEFAULT_TEMP_BAND = (2400.0, 4000.0)

# Starting point before room type nudges it.
MOOD_TEMP = {
    "warm": 2700.0,
    "soft": 2900.0,
    "neutral": 3200.0,
    "cool": 4000.0,
    # Day was 3000. Raised because the daylight a "day" render sits in is
    # around 6500K, and a 3000K lamp against it is a 3500K gap - far enough
    # that the fixture reads as belonging to a different scene rather than
    # as a warm light in this one. 3400 keeps lamps clearly warmer than the
    # daylight without separating from it.
    "day": 3400.0,
}

# Accents may sit slightly warmer than the room key; task lights slightly
# cooler.  Both stay inside TEMP_BAND.
ROLE_TEMP_OFFSET = {
    "key": 0.0,
    "fill": 0.0,
    "wall_wash": -50.0,
    "accent": -300.0,
    "decorative": -300.0,
}

VALID_ROLES = tuple(ROLE_INTENSITY.keys())


# ── Geometry + crowding ───────────────────────────────────────────────────

def _distance3(a: EmissionFixture, b: EmissionFixture) -> float:
    """Distance between two emitters in 3D, metres.

    In plan, not 3D, a pendant and the floor lamp standing under it look
    like a tight cluster; in the room they are far apart and light different
    volumes.
    """
    return math.sqrt(
        (a.x - b.x) ** 2
        + (a.y - b.y) ** 2
        + (a.emitter_height - b.emitter_height) ** 2
    )


def _neighbour_weight(distance: float) -> float:
    """How much a neighbour at `distance` metres crowds this fixture.

    Continuous and never zero: 1.0 touching, 0.5 at CROWDING_RADIUS, 0.2 at
    twice that. Every neighbour is summed, so the third lamp in a row of
    three actually registers instead of being invisible behind the nearest.
    """
    if distance <= 0.0:
        return 1.0
    return 1.0 / (1.0 + (distance / CROWDING_RADIUS) ** 2)


def _crowding_factors(
    analysis: EmissionAnalysis,
    intensities: Dict[str, float],
) -> Dict[str, float]:
    """Per-asset dimming from the light its neighbours put into the same volume.

    Neighbours are weighted by FLUX - intensity x emitter area - not by mere
    presence, and that asymmetry is the whole point. A large bright pendant
    crowds a small table lamp beside it far more than that lamp crowds the
    pendant, so the table lamp yields and the pendant does not. Treating
    proximity as mutual and equal is what made a ceiling pendant dim itself
    because a floor lamp happened to stand under it.

    Because a neighbour's flux depends on its own crowding, this is solved
    by iterating: dim everything, recompute from the dimmed values, repeat.
    """
    lit = [f for f in analysis.fixtures if intensities.get(f.id, 0.0) > 0.0]
    factors = {f.id: 1.0 for f in analysis.fixtures}
    if len(lit) < 2:
        return factors

    dist = {
        (a.id, b.id): _distance3(a, b)
        for a in lit for b in lit if a.id != b.id
    }
    current = {f.id: intensities[f.id] for f in lit}

    for _ in range(CROWDING_PASSES):
        flux = {
            f.id: max(1e-6, current[f.id] * max(f.emitter_area_m2, 1e-3))
            for f in lit
        }
        mean_flux = statistics.mean(flux.values())
        if mean_flux <= 0:
            break

        influence = {
            f.id: sum(
                _neighbour_weight(dist[(f.id, g.id)]) * (flux[g.id] / mean_flux)
                for g in lit if g.id != f.id
            )
            for f in lit
        }

        # Relative to the room, not absolute. This is what stops a room with
        # many lamps from simply going dark all over.
        mean_influence = statistics.mean(influence.values())
        if mean_influence <= 1e-9:
            break

        for f in lit:
            excess = influence[f.id] / mean_influence - 1.0
            factors[f.id] = _clamp(
                1.0 / (1.0 + CROWDING_STRENGTH * excess),
                CROWDING_FLOOR,
                CROWDING_CEILING,
            )
        current = {f.id: intensities[f.id] * factors[f.id] for f in lit}

    return {k: round(v, 4) for k, v in factors.items()}


def _cluster_ladder(
    analysis: EmissionAnalysis,
    roles: Dict[str, str],
    intensities: Dict[str, float],
) -> Dict[str, float]:
    """Rank the fixtures in each cluster and step them down.

    Ordering is by role first (the room's key light leads any cluster it is
    part of), then by emitter area, then by id so the result is stable
    across runs. Ranking by current intensity would be circular - the
    values are what we are deciding.
    """
    factors = {f.id: 1.0 for f in analysis.fixtures}
    clusters: Dict[int, List[EmissionFixture]] = {}
    for f in analysis.fixtures:
        if f.cluster_size > 1 and intensities.get(f.id, 0.0) > 0.0:
            clusters.setdefault(f.cluster_id, []).append(f)

    role_rank = {r: i for i, r in enumerate(ROLE_INTENSITY)}
    for members in clusters.values():
        if len(members) < 2:
            continue
        members.sort(
            key=lambda f: (
                role_rank.get(roles.get(f.id, f.default_role), 99),
                -f.emitter_area_m2,
                f.id,
            )
        )
        for rank, f in enumerate(members):
            factors[f.id] = (
                CLUSTER_LADDER[rank] if rank < len(CLUSTER_LADDER)
                else CLUSTER_LADDER_FLOOR
            )
    return factors


# ── Structured output schema ──────────────────────────────────────────────

EMISSION_FIXTURE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "The fixture id exactly as given in the asset list.",
        },
        "role": {
            "type": "string",
            "enum": list(VALID_ROLES),
            "description": (
                "key = the room's primary source (ceiling/pendant), must be "
                "brightest. fill = floor lamps. accent = table lamps. "
                "wall_wash = sconces. decorative = visible but barely lit."
            ),
        },
        "intensity": {
            "type": "number",
            "description": (
                "Emission intensity in engine units. Typical values are given "
                "per role in the brief. There is no fixed upper bound - a large "
                "or isolated fixture may go higher - but a fixture that is on "
                "must stay bright enough that its shade still visibly glows; to "
                "switch one off use enabled:false, never a token intensity."
            ),
        },
        "temperature": {
            "type": "number",
            "description": "Colour temperature in Kelvin, 1800-7500.",
        },
        "enabled": {
            "type": "boolean",
            "description": "False turns the fixture off (intensity forced to 0).",
        },
        "reason": {
            "type": "string",
            "description": "One short sentence: why this value for this fixture.",
        },
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence in this decision.",
        },
    },
    "required": ["id", "role", "intensity", "temperature", "enabled", "reason"],
}


EMISSION_DECISION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "roomKeyTemperature": {
            "type": "number",
            "description": (
                "The single colour temperature the room is lit to, in Kelvin. "
                "Every fixture sits within 400K of this."
            ),
        },
        "verdict": {
            "type": "string",
            "enum": ["good", "acceptable", "poor"],
            "description": "Assessment of the CURRENT emission values, before your changes.",
        },
        "fixtures": {
            "type": "array",
            "items": EMISSION_FIXTURE_SCHEMA,
            "description": "One entry for EVERY fixture in the asset list. Do not omit any.",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Problems found in the current values.",
        },
        "reasoning": {
            "type": "string",
            "description": "2-4 sentences explaining the lighting plan for this room.",
        },
    },
    "required": ["roomKeyTemperature", "verdict", "fixtures", "warnings", "reasoning"],
}


SYSTEM_PROMPT = """\
You are a lighting technical director setting the emission of individual \
light fixtures in an architectural interior render.

You are given: the render mode and sun/sky settings, the room's dimensions \
and surface colours, the daylight available through its windows, the camera, \
and every light asset in the room with its size, position, mounting height \
and current values.

For EACH fixture you choose `intensity` and `temperature`. You are not \
placing lights, not moving them, and not setting any global exposure. \
Those are handled elsewhere.

Return one entry for EVERY fixture id in the list. Never omit one.

=== COLOUR TEMPERATURE ===

1. Choose ONE `roomKeyTemperature` for the whole room, then keep every \
   fixture within 400K of it. Real rooms are lit by matched lamps. A room \
   spanning thousands of Kelvin is the single most common reason a render \
   reads as fake, and it is the first thing you should fix.
2. Base it on the surface palette and the mood: warm surfaces (wood, \
   terracotta, beige) want 2700-3200K. Cool surfaces (grey, white marble, \
   blue) tolerate 3200-4000K. A warm wood floor under 5000K+ light always \
   looks wrong.
3. Room type sets the ceiling: living rooms and bedrooms 2400-3600K, \
   kitchens and offices 3000-5000K, bathrooms 2700-4500K.
4. In a DAY render the sun is the cool source (around 6500K), so interior \
   lamps stay WARMER than it - but not so far from it that they read as a \
   different scene. Aim to sit clearly warm against the daylight while \
   still belonging to the same room; roughly 3200-3800K rather than 2700K. \
   A lamp two or three thousand Kelvin below the daylight photographs as an \
   orange object rather than as a light.
5. Accent and table lamps may sit up to 300K warmer than the key. Task \
   lights may sit slightly cooler. Both stay inside the 400K band.

=== INTENSITY ===

0. INTENSITY IS COMPUTED ON ITS MERITS, NOT SQUEEZED INTO A BAND. There is \
   no fixed range. Give each fixture the value its role, size, surroundings \
   and neighbours justify, and let the room spread as wide as it needs to. \
   The one hard rule: a fixture that is ON must stay bright enough that its \
   shade visibly glows - a lamp too dim renders as dead plastic, which is a \
   bug and not a dim lamp. A fixture that should not contribute light is \
   `enabled: false`, never a fixture at a token intensity.

6. Establish a HIERARCHY, do not pick six independent numbers. The ceiling \
   or pendant fixture is the `key` and sits highest in the range. Floor \
   lamps are `fill`, below it. Table and side lamps are `accent`, lower \
   still but still above MIN. No fixture may exceed the key.
7. NORMALISE BY EMITTER AREA. `emitter=` in the asset list is the fixture's \
   emissive area in m2. Intensity is radiance, not total output: a large \
   pendant ring at 1.0 puts far more light in the room than a small sphere \
   at 1.0. Two fixtures of different size need different intensity values \
   to contribute comparably.
8. Scale to the room. A high-albedo room (bright walls, given as \
   `room albedo`) bounces light and needs LESS per fixture. A dark room \
   absorbs it and needs MORE. Larger volume needs more total output.
9. DAY vs NIGHT. In a day render, interior lamps support the daylight - \
   keep them visible but subordinate, and pull down any fixture sitting \
   inside the window's daylight pool (small `window dist`). In a night \
   render the fixtures ARE the key light and carry the whole image.
10. A fixture flagged `IN FRAME` is visible to the camera. Its emissive \
    surface will clip to white if pushed too hard - keep it conservative. \
    A fixture out of frame only contributes bounce and can go higher.
11. A small `ceiling gap` means the fixture is close to the ceiling and \
    will burn a hot spot on it. Bring it down.
12. CROWDING. `crowd=` in the asset list is how much company each fixture \
    has: the sum of every other fixture's influence on it, falling off with \
    distance in 3D. It is not a count and not a nearest-neighbour distance \
    - three lamps in a row all read higher than two, and a lamp under a \
    pendant reads lower than one beside it, because the pendant is far \
    away vertically. Fixtures with a high `crowd` are lighting a volume \
    that is already lit: give them less than an isolated fixture of the \
    same role. Weigh WHO the neighbours are, not just how many - a small \
    lamp beside a large bright pendant should yield to it, not the reverse.
12b. WHEN FIXTURES CLUSTER, ONE LEADS AND THE REST STEP DOWN. Fixtures \
    sharing a `cluster #N` marker light the same volume. Do NOT give them \
    all a similar high value - that is a row of identical bright shades and \
    it reads as one blown patch, not as lighting. Pick the one that should \
    dominate (the largest, or the one the composition points at), give it \
    the full value for its role, and place the others clearly below it - \
    roughly three quarters, then three fifths, then half. If one is high, \
    the others must be low. A cluster where every member is high is wrong \
    even when each individual value looks reasonable on its own. But \
    STEPPING DOWN IS NOT SWITCHING OFF: the quietest member of a cluster \
    must still visibly emit. A lamp reduced past the point where its shade \
    glows does not read as a dim lamp, it reads as a grey plastic toy \
    standing in the room, and that is worse than the blown-out version you \
    were fixing. Reduce until it is subordinate, never until it is dark.
12c. OPAQUE SHADES CANNOT BE DRIVEN HARD. A fixture marked `(shaded)` or \
    `OPAQUE SHADE` - a bell, drum, dome or cone - is a solid object with \
    light behind it. Past a point, more emission does not make it brighter, \
    it makes it stop looking like an object: the shade goes flat white, \
    loses its shading and silhouette and starts to read as transparent. \
    Keep shaded fixtures noticeably below open ones. A fixture marked \
    `(open)` - a bare globe, a lit ring, a tube - is meant to read as a \
    bright source and can take a higher value.
13. Never leave a fixture at intensity 0, or so low its shade stops \
    glowing, when it is modelled as a light source. Give it a real value, \
    or set `enabled: false` if the design calls for the lamp to be off.
14. Fixtures of the same asset type in the same room get the same values.
15. When several of rules 9-12 apply to one fixture they do not stack into \
    a large cut. Take the strongest and apply it once. Four small \
    reductions compounding is how a lamp ends up unlit.

Put every problem you found in the CURRENT values into `warnings`, and give \
a one-sentence `reason` for every fixture. Those fields are read by a human \
reviewing the job, so be concrete: name the fixture and the number.

Respond ONLY with the JSON object matching the schema.
"""


def _emission_brief() -> str:
    """Typical per-role values, stated in the user prompt as real numbers.

    The system prompt describes the hierarchy in words; a model also given
    the actual figures anchors on them instead of inventing a scale. Every
    number here comes from the same constants the validator uses, so the two
    cannot drift apart.
    """
    lines = [
        "=== EMISSION GUIDE (read before choosing any intensity) ===",
        "Typical values by role. These are starting points, not limits - move",
        "up or down as the room, the emitter size and the crowding require:",
    ]
    for role, base in ROLE_INTENSITY.items():
        lines.append(f"  {role:11s} ~{base:g}")
    lines.append("")
    lines.append(
        f"A fixture that is on should not fall below about "
        f"{VISIBILITY_FRACTION:.0%} of whatever you give the key light - "
        f"under that its shade stops reading as lit and the lamp renders as "
        f"dead plastic. To switch a lamp off use enabled:false; do not "
        f"express 'off' or 'barely on' as a very low intensity."
    )
    return "\n".join(lines)


# ── The agent ─────────────────────────────────────────────────────────────

class EmissionAgent:
    """Asks the model for per-fixture emission, then validates it hard."""

    def __init__(self, client):
        """`client` is an OllamaClient instance."""
        self.client = client

    def adjust(self, analysis: EmissionAnalysis) -> Dict[str, Any]:
        if not analysis.fixtures:
            return {
                "roomKeyTemperature": 0,
                "verdict": "good",
                "fixtures": [],
                "warnings": [],
                "reasoning": "No light assets in the scene; nothing to adjust.",
                "source": "skipped",
            }

        log.info(
            "Requesting emission decision for %d fixture(s) "
            "(room=%s, albedo=%.2f, mode=%s)",
            len(analysis.fixtures),
            analysis.inferred_room_type,
            analysis.room_albedo,
            "night" if analysis.night_render else "day",
        )

        source = "llm"
        try:
            raw = self.client.chat_json(
                system=SYSTEM_PROMPT,
                user=_emission_brief() + "\n\n" + analysis.summary_for_llm(),
                schema=EMISSION_DECISION_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM emission pass failed (%s) - using fallback", exc)
            raw = _fallback_emission(analysis)
            source = "fallback"

        out = _validate(raw, analysis)
        out["source"] = source
        return out


# ── Deterministic fallback ────────────────────────────────────────────────

def _room_temp_band(room_type: str) -> Tuple[float, float]:
    return ROOM_TEMP_BANDS.get(room_type, DEFAULT_TEMP_BAND)


def _default_key_temperature(analysis: EmissionAnalysis) -> float:
    """The room's colour temperature from mood, room type and surfaces."""
    mood = str(analysis.color_mood or "neutral").strip().lower()
    temp = MOOD_TEMP.get(mood, 3000.0)

    if analysis.inferred_room_type in ("kitchen", "office"):
        temp += 800.0
    elif analysis.inferred_room_type == "bathroom":
        temp += 400.0
    elif analysis.inferred_room_type == "bedroom":
        temp -= 200.0

    # A warm floor under cool light is the most visible mismatch there is.
    if analysis.floor_is_warm:
        temp -= 100.0
    # Very cool surfaces can carry a slightly cooler lamp without clashing.
    if analysis.dominant_mood == "cool":
        temp += 200.0

    lo, hi = _room_temp_band(analysis.inferred_room_type)
    return round(max(lo, min(hi, temp)))


def _albedo_modifier(albedo: float) -> float:
    """Bright rooms bounce; dark rooms swallow. Bounded so a mis-read
    texture cannot blow the whole scene out."""
    if albedo <= 0.01:
        return 1.4
    return max(0.75, min(1.4, 0.55 / albedo))


def _area_modifier(emitter_area: float, reference_area: float) -> float:
    """Compensate for how much surface the fixture radiates from.

    `intensity` behaves as radiance, so the same value on a 0.07 m2 bell
    shade and a 0.71 m2 pendant ring are an order of magnitude apart in
    actual output. Without this, "give every fill lamp 0.68" produces six
    fixtures that look nothing like each other.

    The exponent is deliberately well below 1.0. Full inverse-area
    normalisation would be right if the emitter were a perfect Lambertian
    disc of exactly the plan diameter, and `emitter_area_m2` is a proxy,
    not a measurement - so it corrects most of the difference and does not
    pretend to precision it does not have. Bounded for the same reason.
    """
    if emitter_area <= 1e-4 or reference_area <= 1e-4:
        return 1.0
    return max(0.75, min(1.35, (reference_area / emitter_area) ** 0.35))


def _fallback_emission(analysis: EmissionAnalysis) -> Dict[str, Any]:
    """Coherent emission without a model.

    Deliberately conservative: it fixes distribution (temperature
    coherence, hierarchy, dead fixtures) and leaves the scene's overall
    energy near where the author put it.
    """
    key_temp = _default_key_temperature(analysis)
    albedo_mod = _albedo_modifier(analysis.room_albedo)

    # Reference area is taken PER ROLE, not across the whole scene.
    #
    # Normalising against a scene-wide median inverts the hierarchy: the
    # ceiling pendant has by far the largest emitter, so it takes the
    # largest cut and lands below the floor lamps it is supposed to
    # anchor. The role weights already express "the key is brighter than
    # the fills"; area normalisation only has to answer "and which of
    # these four floor lamps needs more radiance to match the others".
    role_areas: Dict[str, List[float]] = {}
    for f in analysis.fixtures:
        if f.emitter_area_m2 > 0:
            r = f.default_role if f.default_role in ROLE_INTENSITY else "fill"
            role_areas.setdefault(r, []).append(f.emitter_area_m2)
    ref_by_role = {r: statistics.median(v) for r, v in role_areas.items()}

    # Pass 1: base value from role, adjusted for room albedo and emitter
    # size. No crowding yet - crowding depends on how bright the neighbours
    # are, and that is what we are computing.
    roles: Dict[str, str] = {}
    base: Dict[str, float] = {}
    area_mods: Dict[str, float] = {}
    for fx in analysis.fixtures:
        role = fx.default_role if fx.default_role in ROLE_INTENSITY else "fill"
        area_mod = _area_modifier(fx.emitter_area_m2, ref_by_role.get(role, 0.0))
        roles[fx.id] = role
        area_mods[fx.id] = area_mod
        base[fx.id] = ROLE_INTENSITY[role] * albedo_mod * area_mod

    # Pass 2: each fixture yields to the light its neighbours already put
    # into the same volume, and within each cluster one fixture leads while
    # the rest step down. The two answer different questions - how much
    # company, versus which member of the group is the bright one - so both
    # are computed, and the STRONGER of the two is applied. They are not
    # multiplied: that is how the old cascade produced unlit lamps.
    crowding = _crowding_factors(analysis, base)
    ladder = _cluster_ladder(analysis, roles, base)
    combined = {
        fid: min(crowding.get(fid, 1.0), ladder.get(fid, 1.0))
        for fid in base
    }

    fixtures: List[Dict[str, Any]] = []
    for fx in analysis.fixtures:
        role = roles[fx.id]
        intensity = base[fx.id] * combined[fx.id]
        intensity = min(intensity, _emitter_ceiling(fx, role))
        temperature = key_temp + ROLE_TEMP_OFFSET.get(role, 0.0)
        fixtures.append(
            {
                "id": fx.id,
                "role": role,
                "intensity": round(intensity, 3),
                "temperature": round(temperature),
                "enabled": True,
                "reason": (
                    f"Fallback: {role} base {ROLE_INTENSITY[role]:g}, "
                    f"albedo x{albedo_mod:.2f}, area x{area_mods[fx.id]:.2f}, "
                    f"crowding x{crowding[fx.id]:.2f}, cluster rank "
                    f"x{ladder[fx.id]:.2f}; room key {key_temp:.0f}K."
                ),
                "confidence": 0.5,
            }
        )

    return {
        "roomKeyTemperature": key_temp,
        "verdict": "acceptable",
        "fixtures": fixtures,
        "warnings": analysis.detect_problems(),
        "reasoning": (
            f"Deterministic fallback: unified the room to {key_temp:.0f}K, set "
            f"each fixture from its role and emitter size, then let every "
            f"fixture yield to the light its neighbours already contribute."
        ),
    }


# ── Validation: the model proposes, this disposes ─────────────────────────

def fx_name(by_id: Dict[str, EmissionFixture], fid: str) -> str:
    fx = by_id.get(fid)
    return fx.name if fx is not None else fid


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _energy_budget_scale(lit_count: int) -> float:
    """How much to scale the whole room so N fixtures do not sum linearly."""
    if lit_count <= 1:
        return 1.0
    scale = math.sqrt(ENERGY_BUDGET_REFERENCE_FIXTURES / float(lit_count))
    return _clamp(scale, ENERGY_BUDGET_MIN_SCALE, 1.0)


def _emitter_ceiling(fx: EmissionFixture, role: str) -> float:
    """The most this fixture can emit before it stops looking like itself.

    An opaque shade driven too hard does not read as a brighter lamp, it
    reads as a transparent one: the emissive swamps the albedo, the shade
    loses its shading and silhouette, and you see through it. That is a
    different failure from an open emitter clipping to white, and it needs
    a tighter limit.
    """
    base = ROLE_INTENSITY.get(role, ROLE_INTENSITY["fill"])
    if fx.emitter_openness == "shaded":
        return base * SHADED_EMITTER_CEILING
    if fx.emitter_openness == "open":
        return base * OPEN_EMITTER_CEILING
    return base * 1.0


def _finite(value: Any) -> Optional[float]:
    """Parse a number, rejecting None, junk, NaN and infinity alike."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _validate(raw: Dict[str, Any], analysis: EmissionAnalysis) -> Dict[str, Any]:
    """Re-derive every value through the physical constraints.

    The model is good at judgement (what mood, which fixture is the key)
    and unreliable at arithmetic under constraints (staying in a 400K band
    across eight fixtures while also respecting a hierarchy). So it does
    the first and this does the second.
    """
    by_id = {f.id: f for f in analysis.fixtures}
    notes: List[str] = []

    # --- room key temperature ---
    lo, hi = _room_temp_band(analysis.inferred_room_type)
    try:
        key_temp = float(raw.get("roomKeyTemperature") or 0)
    except (TypeError, ValueError):
        key_temp = 0.0
    if not (TEMP_MIN <= key_temp <= TEMP_MAX):
        key_temp = _default_key_temperature(analysis)
        notes.append("roomKeyTemperature missing or out of range; derived it instead.")
    clamped_key = _clamp(key_temp, lo, hi)
    if abs(clamped_key - key_temp) > 1:
        notes.append(
            f"roomKeyTemperature {key_temp:.0f}K is outside the "
            f"{analysis.inferred_room_type} band {lo:.0f}-{hi:.0f}K; "
            f"clamped to {clamped_key:.0f}K."
        )
    key_temp = clamped_key

    # --- gather the model's entries, filling in anything it skipped ---
    proposed: Dict[str, Dict[str, Any]] = {}
    for entry in raw.get("fixtures") or []:
        if not isinstance(entry, dict):
            continue
        fid = str(entry.get("id", "")).strip()
        if fid not in by_id:
            # Models occasionally invent ids or return the asset name.
            match = next(
                (f.id for f in analysis.fixtures if f.name == fid or f.id.startswith(fid)),
                None,
            )
            if match is None:
                notes.append(f"Dropped unknown fixture id {fid!r}.")
                continue
            fid = match
        proposed[fid] = entry

    fb = _fallback_emission(analysis)
    fb_by_id = {e["id"]: e for e in fb["fixtures"]}
    for fid in by_id:
        if fid not in proposed:
            proposed[fid] = fb_by_id[fid]
            notes.append(
                f"Model omitted '{by_id[fid].name}'; filled from deterministic rules."
            )

    # A model working on the wrong scale returns values that are all under
    # MIN. Clamping each one individually would put every fixture on the
    # floor at exactly MIN - in range, so no lamp renders dead, but flat,
    # with the hierarchy gone. When most of the response is below the floor,
    # the response is uncalibrated rather than merely dim: keep the model's
    # judgement (roles, temperatures, which lamps are off) and take the
    # intensities from the deterministic rules, which are in scale by
    # construction.
    # Compared against the MEDIAN of the deterministic values, not their
    # maximum. Against the maximum this fired on the fallback's own output -
    # a fill legitimately sits well under the key - and reported a model
    # scale error where there was none.
    fb_floor = 0.5 * statistics.median(
        [e["intensity"] for e in fb["fixtures"]] or [1.0]
    )
    on = [
        e for e in proposed.values()
        if bool(e.get("enabled", True)) and _finite(e.get("intensity")) is not None
    ]
    under = [e for e in on if _finite(e["intensity"]) < fb_floor]
    if on and len(under) * 2 >= len(on):
        for fid, entry in proposed.items():
            entry["intensity"] = fb_by_id[fid]["intensity"]
        notes.append(
            f"{len(under)} of {len(on)} lit fixtures came back under "
            f"{fb_floor:.2f} - the response is on the wrong scale, not merely "
            f"dim. Kept the model's roles and temperatures and took intensities "
            f"from the deterministic rules, so the hierarchy survives instead "
            f"of collapsing onto the floor."
        )

    # --- per-fixture clamping ---
    results: Dict[str, Dict[str, Any]] = {}
    for fid, entry in proposed.items():
        fx = by_id[fid]

        # A locked fixture is the user's decision, not ours.
        if fx.locked:
            results[fid] = {
                "id": fid,
                "name": fx.name,
                "role": fx.default_role,
                "intensity": fx.current_intensity if fx.current_intensity is not None else 0.0,
                "temperature": fx.current_temperature if fx.current_temperature is not None else key_temp,
                "enabled": bool(fx.current_intensity),
                "reason": "Locked by the user; left unchanged.",
                "confidence": 1.0,
                "locked": True,
            }
            continue

        role = str(entry.get("role", fx.default_role))
        if role not in ROLE_INTENSITY:
            role = fx.default_role if fx.default_role in ROLE_INTENSITY else "fill"

        # float("NaN") and float("inf") both PARSE, and then every
        # comparison against them is False - so a NaN slips past "<= 0",
        # past the floor, past the ceiling, and gets written into the scene
        # where it becomes a black or blown fixture. Non-finite values have
        # to be rejected explicitly, not caught by range checks.
        temperature = _finite(entry.get("temperature"))
        if temperature is None:
            temperature = key_temp
        intensity = _finite(entry.get("intensity"))
        if intensity is None:
            intensity = fb_by_id[fid]["intensity"]
            notes.append(
                f"'{fx.name}' returned a non-numeric intensity "
                f"({entry.get('intensity')!r}); used the deterministic value."
            )

        # Temperature: global range, then the room band, then the 400K
        # coherence band around the key. Order matters - the coherence band
        # is the one that must survive.
        temperature = _clamp(temperature, TEMP_MIN, TEMP_MAX)
        temperature = _clamp(temperature, lo, hi)
        before = temperature
        temperature = _clamp(temperature, key_temp - TEMP_BAND, key_temp + TEMP_BAND)
        if abs(before - temperature) > 1:
            notes.append(
                f"'{fx.name}' at {before:.0f}K was outside the {TEMP_BAND:.0f}K "
                f"band around {key_temp:.0f}K; pulled to {temperature:.0f}K."
            )

        enabled = bool(entry.get("enabled", True))
        if not enabled:
            intensity = 0.0
        elif intensity <= 0.0:
            # "enabled but zero" is the dead-prop bug stated in the model's
            # own words. It cannot be scaled or clamped into something
            # sensible - zero carries no information about how bright the
            # lamp should be - so it is replaced outright. The visibility
            # floor further down only protects fixtures that are already
            # lit; it skips anything sitting at zero, so this has to be
            # caught here.
            intensity = fb_by_id[fid]["intensity"]
            notes.append(
                f"'{fx.name}' came back enabled at intensity 0, which renders "
                f"as an unlit prop; replaced with the deterministic "
                f"{intensity:g}. Use enabled:false to switch a lamp off."
            )

        results[fid] = {
            "id": fid,
            "name": fx.name,
            "role": role,
            "intensity": intensity,
            "temperature": round(temperature),
            "enabled": enabled,
            "reason": str(entry.get("reason", ""))[:300],
            "confidence": _clamp(float(entry.get("confidence", 0.6) or 0.6), 0.0, 1.0),
            "locked": False,
        }

    # --- physical constraints: strongest wins, they do not compound ---
    #
    # This used to apply each cut in turn: 0.90 ceiling x 0.90 proximity x
    # 0.92 wall x 0.75 daylight = 0.56, so a fixture that happened to be
    # near the ceiling, a neighbour, a wall and a window lost nearly half
    # its output to four individually reasonable rules and came back from
    # the renderer emitting nothing. Four constraints saying "this belongs
    # dimmer" does not mean "dimmer by four times".
    #
    # Crowding is deliberately NOT in this list. It is not a binary
    # condition to be caught by a threshold, it is a continuous property of
    # the fixture's neighbourhood, and it is applied below over every
    # neighbour at once.
    for fid, res in results.items():
        fx = by_id[fid]
        if res["locked"] or not res["enabled"]:
            continue

        candidates: List[Tuple[float, str]] = []

        if 0 < fx.ceiling_clearance < 0.25:
            candidates.append((
                0.85,
                f"{fx.ceiling_clearance:.2f} m below the ceiling (hot-spot headroom)",
            ))
        if 0 < fx.nearest_wall_dist < 0.4:
            candidates.append((
                0.88,
                f"{fx.nearest_wall_dist:.2f} m from a wall",
            ))
        if (
            analysis.day_render
            and not analysis.night_render
            and 0 < fx.nearest_window_dist < 1.5
        ):
            candidates.append((
                0.70,
                f"{fx.nearest_window_dist:.2f} m from a window in a day render",
            ))

        if candidates:
            factor, reason = min(candidates, key=lambda c: c[0])
            before = res["intensity"]
            res["intensity"] = round(res["intensity"] * factor, 3)
            extra = (
                f" (also {len(candidates) - 1} weaker constraint(s), not compounded)"
                if len(candidates) > 1 else ""
            )
            notes.append(
                f"'{fx.name}' {reason}; {before:g} -> {res['intensity']:g}{extra}."
            )

    # --- crowding: every neighbour, weighted by distance and by output ---
    #
    # Applied here, after the model's values and the constraints above, so
    # it responds to what each fixture is ACTUALLY set to rather than to
    # what its role suggests. A model that already accounted for the cluster
    # gets a correspondingly smaller adjustment; one that ignored it gets
    # the full one.
    active_intensity = {
        fid: (res["intensity"] if res["enabled"] and not res["locked"] else 0.0)
        for fid, res in results.items()
    }
    # A locked fixture does not move, but it still crowds its neighbours.
    for fid, res in results.items():
        if res["locked"] and res["enabled"]:
            active_intensity[fid] = res["intensity"]

    crowding = _crowding_factors(analysis, active_intensity)
    for fid, res in results.items():
        if res["locked"] or not res["enabled"]:
            continue
        factor = crowding.get(fid, 1.0)
        res["crowding"] = factor
        if abs(factor - 1.0) > 0.005:
            before = res["intensity"]
            res["intensity"] = round(res["intensity"] * factor, 3)
            notes.append(
                f"'{fx_name(by_id, fid)}' is "
                + ("more" if factor < 1.0 else "less")
                + f" crowded than the room average (x{factor:.2f}); "
                f"{before:g} -> {res['intensity']:g}."
            )

    # --- emitter ceiling: opaque shades cannot be driven hard ---
    #
    # A ceiling, never a cut. An open emitter clipping to white is a bright
    # lamp; an opaque shade doing it stops being an object and reads as
    # transparent, which is what happened to the bell lamp.
    for fid, res in results.items():
        fx = by_id[fid]
        if res["locked"] or not res["enabled"]:
            continue
        cap = _emitter_ceiling(fx, res["role"])
        if res["intensity"] > cap:
            notes.append(
                f"'{fx.name}' is a {fx.emitter_openness} emitter at "
                f"{res['intensity']:g}; capped at {cap:.2f} - past that an "
                + ("opaque shade washes out and reads as transparent"
                   if fx.emitter_openness == "shaded"
                   else "emissive surface clips to white")
                + "."
            )
            res["intensity"] = round(cap, 3)

    # --- hierarchy, then the visibility floor ---
    active = {k: v for k, v in results.items() if v["intensity"] > 0 and not v["locked"]}
    if active:
        # Room energy budget, applied BEFORE the hierarchy and the floor so
        # everything downstream is computed against the budgeted values -
        # scaling afterwards would push fixtures back under a floor that had
        # already been enforced.
        #
        # Uniform across the room on purpose: it corrects the total, not the
        # distribution. Which fixture is brighter than which has already been
        # decided by role, emitter area, crowding and the cluster ladder, and
        # a uniform scale leaves every one of those relationships intact.
        budget_scale = _energy_budget_scale(len(active))
        if budget_scale < 0.995:
            for v in active.values():
                v["intensity"] = round(v["intensity"] * budget_scale, 3)
                v["budget_scale"] = budget_scale
            notes.append(
                f"Room energy budget: {len(active)} lit fixtures sum linearly, "
                f"so the whole room is scaled x{budget_scale:.2f} "
                f"(sqrt({ENERGY_BUDGET_REFERENCE_FIXTURES:g}/{len(active)})). "
                f"Relative brightness between fixtures is unchanged."
            )

        keys = {k: v for k, v in active.items() if v["role"] == "key"}
        if not keys:
            top = max(active, key=lambda k: active[k]["intensity"])
            active[top]["role"] = "key"
            keys = {top: active[top]}
            notes.append(
                f"No key fixture in the decision; promoted '{active[top]['name']}'."
            )

        # Restore the hierarchy by RAISING THE KEY, not by sinking the
        # offender. Pushing a fill down far enough to clear a dim key is how
        # a lamp ends up too dark to read as lit.
        key_top = max(keys.values(), key=lambda v: v["intensity"])
        others = [v for v in active.values() if v["role"] != "key"]
        if others:
            highest = max(others, key=lambda v: v["intensity"])
            if highest["intensity"] >= key_top["intensity"]:
                before = key_top["intensity"]
                key_top["intensity"] = round(highest["intensity"] / 0.85, 3)
                notes.append(
                    f"'{highest['name']}' at {highest['intensity']:g} matched or "
                    f"exceeded the key light; raised '{key_top['name']}' "
                    f"{before:g} -> {key_top['intensity']:g} to restore the "
                    f"hierarchy."
                )

        # THE ONE SAFETY NET. Derived from the room's own key light rather
        # than configured: a lamp under roughly half the key reads as unlit
        # in a room lit to that key, whatever the absolute scale is. This is
        # the last step so that nothing above it can undo it.
        # Rounded once, here, so the value written to a fixture is exactly
        # the value the floor is defined as. Comparing against an unrounded
        # bound and then storing a rounded one leaves fixtures a hair under
        # their own floor - invisible in a render, but it makes the
        # invariant untestable, which is worse.
        floor = round(VISIBILITY_FRACTION * key_top["intensity"], 3)
        ceiling = round(SANITY_CEILING_FACTOR * key_top["intensity"], 3)

        # --- cluster ladder: one leads, the rest step down ---
        #
        # Applied HERE, after the floor is known, and as an interpolation
        # TOWARDS the floor rather than a multiplier through it:
        #
        #     new = floor + (current - floor) * rank_factor
        #
        # As a plain multiplier it drove the lower members of a cluster
        # under the floor, the floor pulled them all back up to exactly
        # itself, and the ladder's whole point - that the members differ -
        # was destroyed at the bottom of the range instead of the middle.
        # Interpolating cannot underflow, and because the rank factors are
        # strictly decreasing the ordering always survives: the lead keeps
        # its value, and each member below it sits a fixed fraction of the
        # way down towards the floor.
        #
        # Crowding is already applied; the ladder takes only the remainder,
        # so a fixture the crowding pass has dimmed is not dimmed twice for
        # the same neighbours.
        roles_now = {fid: r["role"] for fid, r in results.items()}
        ladder = _cluster_ladder(
            analysis,
            roles_now,
            {fid: r["intensity"] for fid, r in results.items()},
        )
        for fid, res in results.items():
            if res["locked"] or not res["enabled"]:
                continue
            factor = ladder.get(fid, 1.0)
            res["cluster_rank_factor"] = factor
            if factor >= 0.995:
                continue
            already = res.get("crowding", 1.0)
            remaining = min(1.0, factor / already) if already > 0 else factor
            if remaining >= 0.995:
                continue
            before = res["intensity"]
            res["intensity"] = round(
                floor + (res["intensity"] - floor) * remaining, 3
            )
            fx = by_id[fid]
            rank = 1 + sum(
                1 for other_id, other in ladder.items()
                if by_id[other_id].cluster_id == fx.cluster_id and other > factor
            )
            notes.append(
                f"'{fx.name}' is rank {rank} of {fx.cluster_size} in cluster "
                f"#{fx.cluster_id}; stepped down to x{factor:.2f} of its role "
                f"value; {before:g} -> {res['intensity']:g}."
            )
        for v in active.values():
            if v["intensity"] < floor:
                notes.append(
                    f"'{v['name']}' at {v['intensity']:g} was under "
                    f"{floor:.2f} ({VISIBILITY_FRACTION:.0%} of the "
                    f"{key_top['intensity']:g} key) where the shade stops "
                    f"reading as lit; raised to {floor:.2f}."
                )
                v["intensity"] = floor
            elif v["intensity"] > ceiling:
                notes.append(
                    f"'{v['name']}' at {v['intensity']:g} is implausible next to a "
                    f"{key_top['intensity']:g} key light; capped at {ceiling:.2f}."
                )
                v["intensity"] = ceiling

    ordered = [
        results[f.id] for f in analysis.fixtures if f.id in results
    ]

    warnings = [str(w)[:300] for w in (raw.get("warnings") or []) if w]
    warnings.extend(notes)

    return {
        "roomKeyTemperature": round(key_temp),
        "verdict": str(raw.get("verdict", "acceptable")),
        "fixtures": ordered,
        "warnings": warnings,
        "reasoning": str(raw.get("reasoning", ""))[:800],
        "validator_notes": notes,
    }

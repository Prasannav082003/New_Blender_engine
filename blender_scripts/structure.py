"""
structure.py — Blender port of structure.gd (columns, beams, flues, partition walls, ramps,
steps, staircases, false ceilings, floor openings).

All maths stays in Godot Y-up space; box_godot()/mesh_object() convert to Blender at write time.
Ported 1:1: squarecolumn/flue/beam/box, circlecolumn, partitionwall(rect/l/u), ramp, step,
staircase (straight + u_shaped, stepped bottom), falseceiling (flat + coffered), flooropening cutters.
Simplified (logged): winder staircases (first flight only), flat-bottom stairs (built stepped),
sunshade + custom_shape structures (skipped for now — rarely present in image jobs).
"""

import math
from mathutils import Matrix, Vector

from coords import godot_trs, three_to_blender
from blender_utils import log, box_godot, mesh_object, planar_uvs


class StructureBuilder:
    def __init__(self, main):
        self.main = main  # SceneBuilder

    # ------------------------------------------------------------------
    def resolve_structure_material(self, structure, structure_type):
        mats = structure.get("materials") if isinstance(structure.get("materials"), dict) else {}
        preferred = {
            "squarecolumn": ["Column"], "circlecolumn": ["Column"], "flue": ["Column"],
            "beam": ["Beam"], "ramp": ["Ramp"], "step": ["Step"],
            "falseceiling": ["False Ceiling", "Drop Section"],
            "staircase": ["StaircaseTop", "Step", "LandingTop"],
            "flooropening": ["FloorOpening", "Frame"],
        }.get(structure_type, [])
        for k in preferred:
            if isinstance(mats.get(k), dict):
                m = dict(mats[k]); m["is_structure"] = True
                return m
        for k, v in mats.items():
            if isinstance(v, dict):
                m = dict(v); m["is_structure"] = True
                return m
        return {"color": "#c7c7c7", "is_structure": True}

    # ------------------------------------------------------------------
    def box_part(self, root_m, size, mat_data, local_pos, rot_y=0.0, name="StructPart"):
        size = (max(size[0], 0.03), max(size[1], 0.03), max(size[2], 0.03))
        m = root_m @ godot_trs(local_pos, rot_y)
        verts, faces = box_godot(size, m)
        mat = self.main.materials.create_material(mat_data, "structure",
                                                  self.main.structure_uv_scale(mat_data))
        obj = mesh_object(name, verts, faces, self.main.col_arch, [mat])
        self._uv(obj)
        return obj

    def _uv(self, obj):
        planar_uvs(obj, (1, 0, 0), (0, 1, 0), (0, 0, 1))  # triplanar-like (Godot uv1_triplanar)

    def multi_mat_box(self, root_m, size, pos, mats, rot_y=0.0):
        side = mats.get("Sides", mats.get("fallback", {}))
        top = mats.get("Top", side)
        bottom = mats.get("Bottom", side)
        if not ("Top" in mats or "Bottom" in mats):
            return self.box_part(root_m, size, side, pos, rot_y)
        holder = root_m @ godot_trs(pos, rot_y)
        core_h = max(size[1] - 0.004, 0.01)
        core_d = max(size[2] - 0.004, 0.01)
        self.box_part(holder, (size[0], core_h, core_d), side, (0, 0, 0))
        self.box_part(holder, (size[0], 0.002, size[2]), top, (0, size[1] * 0.5 - 0.001, 0))
        self.box_part(holder, (size[0], 0.002, size[2]), bottom, (0, -size[1] * 0.5 + 0.001, 0))
        self.box_part(holder, (size[0], core_h, 0.002), top, (0, 0, size[2] * 0.5 - 0.001))
        self.box_part(holder, (size[0], core_h, 0.002), bottom, (0, 0, -size[2] * 0.5 + 0.001))

    # ------------------------------------------------------------------
    def build(self, structure, layer_altitude):
        M = self.main
        stype = str(structure.get("structure_type", structure.get("type", ""))).lower()
        if not stype:
            return
        props = structure.get("properties") if isinstance(structure.get("properties"), dict) else {}
        s = M.unit_scale
        gd = M.get_dimension_value
        width = gd(props.get("width", structure.get("width", 100))) * s
        depth = gd(props.get("depth", structure.get("depth", 100))) * s
        height = gd(props.get("height", structure.get("height", 100))) * s
        altitude = gd(props.get("altitude", structure.get("altitude", 0))) * s

        xy = M.plan_point_2d(float(structure.get("x", 0)), float(structure.get("y", 0)))
        rot = float(structure.get("rotation", props.get("rotation", 0.0)) or 0.0)
        root_m = godot_trs(
            (xy[0] * s, layer_altitude + altitude, xy[1] * s),
            rot_y=M.world_yaw_from_source_rotation(rot),
            rot_x=float(structure.get("rotationX", 0.0)),
            rot_z=float(structure.get("rotationZ", 0.0)),
            scale=(-1.0 if structure.get("flipX") else 1.0, 1.0, -1.0 if structure.get("flipZ") else 1.0),
        )
        mat = self.resolve_structure_material(structure, stype)

        if stype == "partitionwall":
            self.partitionwall(root_m, structure, width, depth, height, mat)
        elif stype == "circlecolumn":
            self.cylinder(root_m, width, depth, height, mat)
        elif stype == "ramp":
            self.ramp(root_m, width, depth, height, mat)
        elif stype == "step":
            self.step(root_m, structure, width, depth, height, mat)
        elif stype == "falseceiling":
            self.false_ceiling(root_m, structure, width, depth, height, mat)
        elif stype == "staircase":
            self.staircase(root_m, structure, width, depth, height, mat)
        elif stype == "flooropening":
            pass  # frame disabled in Godot too; the cut is done in floor_opening_cutters()
        elif stype in ("sunshade", "custom_shape"):
            log(f"[Structure] '{stype}' not yet ported to Blender — skipped ({structure.get('id')})")
        else:  # squarecolumn, flue, beam and anything unknown
            self.box_part(root_m, (width, height, depth), mat, (0, max(height, 0.05) * 0.5, 0), name=f"Struct_{stype}")

    # ------------------------------------------------------------------
    def partitionwall(self, root_m, structure, width, depth, height, mat):
        props = structure.get("properties", {}) or {}
        sub = str(props.get("partition_shape", "")).lower().strip() or \
            str(structure.get("partitionwall_subtype", "rect")).lower().strip()
        t = 0.1524
        wt = props.get("wall_thickness")
        if wt is not None:
            raw = float(wt.get("length", 0)) if isinstance(wt, dict) else float(wt)
            if raw > 0:
                t = raw * self.main.unit_scale
        B = self.box_part
        if sub == "rect":
            B(root_m, (width, height, t), mat, (0, height / 2, depth / 2 - t / 2))
            B(root_m, (width, height, t), mat, (0, height / 2, -depth / 2 + t / 2))
            B(root_m, (t, height, depth - 2 * t), mat, (width / 2 - t / 2, height / 2, 0))
            B(root_m, (t, height, depth - 2 * t), mat, (-width / 2 + t / 2, height / 2, 0))
        elif sub == "l_shaped":
            B(root_m, (width, height, t), mat, (0, height / 2, -depth / 2 + t / 2))
            B(root_m, (t, height, depth - t), mat, (-width / 2 + t / 2, height / 2, t / 2))
        elif sub == "u_shaped":
            B(root_m, (width, height, t), mat, (0, height / 2, -depth / 2 + t / 2))
            B(root_m, (t, height, depth - t), mat, (width / 2 - t / 2, height / 2, t / 2))
            B(root_m, (t, height, depth - t), mat, (-width / 2 + t / 2, height / 2, t / 2))
        else:
            B(root_m, (width, height, depth), mat, (0, height / 2, 0))

    def cylinder(self, root_m, width, depth, height, mat_data, segs=24):
        r = max(width, depth) * 0.5
        h = max(height, 0.05)
        verts, faces = [], []
        for k in range(2):
            for i in range(segs):
                a = 2 * math.pi * i / segs
                verts.append(three_to_blender(root_m @ Vector((r * math.cos(a), h * k, r * math.sin(a)))))
        faces.append(list(range(segs)))
        faces.append(list(range(segs, 2 * segs)))
        for i in range(segs):
            j = (i + 1) % segs
            faces.append([i, j, j + segs, i + segs])
        mat = self.main.materials.create_material(mat_data, "structure", self.main.structure_uv_scale(mat_data))
        obj = mesh_object("Struct_circlecolumn", verts, faces, self.main.col_arch, [mat])
        self._uv(obj)

    def ramp(self, root_m, width, depth, height, mat_data):
        w, d, h = max(width, 0.05), max(depth, 0.05), max(height, 0.05)
        local = [(-w / 2, 0, -d / 2), (-w / 2, 0, d / 2), (-w / 2, h, d / 2),
                 (w / 2, 0, -d / 2), (w / 2, 0, d / 2), (w / 2, h, d / 2)]
        verts = [three_to_blender(root_m @ Vector(v)) for v in local]
        faces = [[0, 1, 2], [3, 5, 4], [0, 3, 4, 1], [1, 4, 5, 2], [0, 2, 5, 3]]
        mat = self.main.materials.create_material(mat_data, "structure", self.main.structure_uv_scale(mat_data))
        obj = mesh_object("Struct_ramp", verts, faces, self.main.col_arch, [mat])
        self._uv(obj)

    def step(self, root_m, structure, width, depth, height, mat):
        props = structure.get("properties", {}) or {}
        stair = props.get("stair", {}) if isinstance(props.get("stair"), dict) else {}
        flights = stair.get("flights", []) if isinstance(stair.get("flights"), list) else []
        s = self.main.unit_scale
        count, riser, tread, direction = 1, max(height, 0.05), max(depth, 0.05), "forward"
        if flights and isinstance(flights[0], dict):
            f = flights[0]
            count = max(int(f.get("step_count", 1)), 1)
            riser = float(f.get("riser_height", 0)) * s if float(f.get("riser_height", 0)) > 0 else max(height / count, 0.05)
            tread = float(f.get("tread_depth", 0)) * s if float(f.get("tread_depth", 0)) > 0 else max(depth / count, 0.05)
            direction = str(f.get("direction", "forward")).lower()
        elif stair.get("auto_calculate_steps"):
            count = max(int(round(height / 0.15)), 1)
            riser, tread = max(height / count, 0.05), max(depth / count, 0.05)
        dir_vec, rot = {"backward": ((0, 0, 1), 0.0), "back": ((0, 0, 1), 0.0), "reverse": ((0, 0, 1), 0.0),
                        "left": ((-1, 0, 0), math.pi / 2), "right": ((1, 0, 0), math.pi / 2)}.get(direction, ((0, 0, -1), 0.0))
        dv = Vector(dir_vec)
        run = tread * count
        base = root_m @ Matrix.Translation(-dv * run)
        for i in range(count):
            p = dv * (tread * (i + 0.5))
            p.y = riser * (i + 0.5)
            # stepped mass: each step box goes down to the floor (matches Godot support mesh + steps)
            full_h = riser * (i + 1)
            self.box_part(base, (max(width, 0.05), full_h, tread + 0.002), mat, (p.x, full_h / 2, p.z), rot, name="Struct_step")

    def staircase(self, root_m, structure, width, depth, height, mat):
        props = structure.get("properties", {}) or {}
        stair = props.get("stair", {}) if isinstance(props.get("stair"), dict) else {}
        stype = str(stair.get("stair_type", "straight")).lower()
        flights = stair.get("flights", []) if isinstance(stair.get("flights"), list) else []
        raw = structure.get("materials", {}) if isinstance(structure.get("materials"), dict) else {}
        step_mats = {"Top": raw.get("StaircaseTop", raw.get("Step", mat)), "Bottom": raw.get("StaircaseBottom", mat),
                     "Sides": raw.get("StaircaseSides", mat), "fallback": mat}
        landing_mats = {"Top": raw.get("LandingTop", step_mats["Top"]), "Bottom": raw.get("LandingBottom", step_mats["Bottom"]),
                        "Sides": raw.get("LandingSides", step_mats["Sides"]), "fallback": mat}
        s = self.main.unit_scale
        travel = lambda d: {"backward": (0, 0, -1), "back": (0, 0, -1), "reverse": (0, 0, -1),
                            "left": (1, 0, 0), "right": (-1, 0, 0)}.get(d, (0, 0, 1))
        srot = lambda d: {"left": math.pi / 2, "right": -math.pi / 2}.get(d, 0.0)
        if not flights:
            self.flight(root_m, 12, 0.1524, 0.254, width if width > 0.05 else 1.0, Vector((0, 0, 0)), Vector((0, 0, 1)), 0.0, step_mats)
            return
        f0 = flights[0]
        n0 = max(int(f0.get("step_count", 12)), 1)
        r0 = float(f0.get("riser_height", 15.24)) * s
        if r0 <= 0:
            r0 = height / n0
        t0 = float(f0.get("tread_depth", 0)) * s if float(f0.get("tread_depth", 0)) > 0 else depth / n0
        d0 = str(f0.get("direction", "forward")).lower()
        self.flight(root_m, n0, r0, t0, width, Vector((0, 0, 0)), Vector(travel(d0)), srot(d0), step_mats)
        cur_h, run0 = n0 * r0, n0 * t0
        land_h, land_d = max(r0, 0.05), max(width, t0 * 1.5)
        if stype == "u_shaped":
            turn = str(stair.get("turn_direction", structure.get("turn_direction", "left"))).lower()
            lx, sx = (width * 0.5, width) if turn != "right" else (-width * 0.5, -width)
            self.multi_mat_box(root_m, (width * 2, land_h, land_d), (lx, cur_h - land_h / 2, run0 + land_d / 2), landing_mats)
            if len(flights) > 1:
                f1 = flights[1]
                n1 = int(f1.get("step_count", n0))
                r1 = float(f1.get("riser_height", 0)) * s if float(f1.get("riser_height", 0)) > 0 else r0
                t1 = float(f1.get("tread_depth", 0)) * s if float(f1.get("tread_depth", 0)) > 0 else t0
                self.flight(root_m, n1, r1, t1, width, Vector((sx, cur_h, run0)), Vector((0, 0, -1)), math.pi, step_mats)
        elif stype != "straight" and len(flights) > 1:
            log(f"[Structure] staircase type '{stype}': only the first flight is ported so far")

    def flight(self, root_m, steps, riser, tread, width, start, direction, rot_y, mats):
        steps = max(int(steps), 1)
        w, h, d = max(width, 0.05), max(riser, 0.03), max(tread, 0.05)
        dvec = direction.normalized() if direction.length > 1e-3 else Vector((0, 0, 1))
        for i in range(steps):
            p = start + dvec * (d * (i + 0.5))
            p = Vector((p.x, start.y + h * (i + 0.5), p.z))
            self.multi_mat_box(root_m, (w, h, d), tuple(p), mats, rot_y)

    def false_ceiling(self, root_m, structure, width, depth, height, mat):
        props = structure.get("properties", {}) or {}
        drop = self.main.get_dimension_value(props.get("drop_height", 0)) * self.main.unit_scale
        pattern = str(props.get("pattern_type", "")).lower()
        slab = max(height, 0.03)
        drop_mat = self.resolve_structure_material(structure, "falseceiling")
        mats = structure.get("materials", {}) if isinstance(structure.get("materials"), dict) else {}
        if isinstance(mats.get("Drop Section"), dict):
            drop_mat = mats["Drop Section"]
        B = self.box_part
        if pattern == "coffered" and drop > 0.01 and width > 0.2 and depth > 0.2:
            b = min(max(min(width, depth) * 0.16, 0.08), 0.35)
            B(root_m, (max(width - 2 * b, 0.1), slab, max(depth - 2 * b, 0.1)), mat, (0, -slab / 2, 0))
            y = -(drop + slab / 2)
            B(root_m, (width, slab, b), drop_mat, (0, y, depth / 2 - b / 2))
            B(root_m, (width, slab, b), drop_mat, (0, y, -depth / 2 + b / 2))
            B(root_m, (b, slab, max(depth - 2 * b, 0.1)), drop_mat, (width / 2 - b / 2, y, 0))
            B(root_m, (b, slab, max(depth - 2 * b, 0.1)), drop_mat, (-width / 2 + b / 2, y, 0))
        else:
            B(root_m, (width, slab, depth), mat, (0, -slab / 2, 0))

    # ------------------------------------------------------------------
    def floor_opening_cutters(self, structures, area_id, layer_altitude, cut_height):
        """Returns Godot-space (matrix, size) boxes to subtract from the floor slab."""
        out = []
        if not isinstance(structures, dict):
            return out
        M = self.main
        s = M.unit_scale
        for sid, st in structures.items():
            if not isinstance(st, dict):
                continue
            if str(st.get("structure_type", st.get("type", ""))).lower() != "flooropening":
                continue
            if "parentId" in st and str(st["parentId"]) != area_id:
                continue
            props = st.get("properties", {}) or {}
            w = M.get_dimension_value(props.get("width", st.get("width", 100))) * s
            d = M.get_dimension_value(props.get("depth", st.get("depth", 100))) * s
            alt = M.get_dimension_value(props.get("altitude", st.get("altitude", 0))) * s
            xy = M.plan_point_2d(float(st.get("x", 0)), float(st.get("y", 0)))
            ch = max(cut_height, 0.2)
            # exact port of structure.gd (_add_floor_opening_cutters): unrotated box
            m = godot_trs((xy[0] * s, layer_altitude + alt - ch * 0.5, xy[1] * s))
            out.append((m, (max(w, 0.05), ch, max(d, 0.05))))
        return out

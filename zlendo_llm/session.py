"""
RenderSession - the mutable state a lighting agent operates on.

This is the single object that both the MCP server (external clients) and
the internal Qwen agent loop drive. Keeping one implementation means a
tool behaves identically whether Claude Desktop called it or your own
pipeline did.

A session owns:
  - the scene dict that will be handed to Godot
  - the audit report from the last analyse pass
  - the render history (path + metrics per render)
  - a budget, because a 4K path trace is expensive and an agent left
    unsupervised will happily burn six of them
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .validate import validate_decision

log = logging.getLogger(__name__)


@dataclass
class RenderRecord:
    index: int
    path: str
    metrics: Dict[str, Any]
    settings: Dict[str, Any]
    seconds: float


@dataclass
class SessionConfig:
    godot_exe: str
    project_dir: str
    work_dir: str = field(default_factory=lambda: tempfile.gettempdir())
    scene_script: str = "res://image_glb_creation.gd"
    main_scene: str = "res://main.tscn"

    # A full 4K path trace. Their existing pipeline allows 1500s.
    render_timeout: int = 1800
    audit_timeout: int = 900

    # Echo Godot's stdout live. The audit can take minutes while it loads
    # GLB assets, and a silent subprocess is indistinguishable from a hang.
    stream_output: bool = False

    # Hard ceiling on 4K renders per session. The agent is told about
    # this. Without it a "keep improving" instruction is unbounded.
    max_renders: int = 3


class RenderSession:
    def __init__(
        self,
        scene: Dict[str, Any],
        config: SessionConfig,
        job_id: str = "session",
    ):
        self.scene = copy.deepcopy(scene)
        self.cfg = config
        self.job_id = job_id

        self.audit: Optional[Dict[str, Any]] = None
        self.renders: List[RenderRecord] = []
        self.log: List[str] = []
        self._pending_lights: Dict[str, Dict[str, Any]] = {}

        Path(self.cfg.work_dir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _note(self, msg: str) -> None:
        log.info("[session %s] %s", self.job_id, msg)
        self.log.append(msg)

    @property
    def renders_remaining(self) -> int:
        return max(0, self.cfg.max_renders - len(self.renders))

    def _settings(self) -> Dict[str, Any]:
        s = self.scene.setdefault("_lightingSettings", {})
        if not isinstance(s, dict):
            s = {}
            self.scene["_lightingSettings"] = s
        return s

    def _write_scene(self) -> Path:
        path = Path(self.cfg.work_dir) / f"{self.job_id}_scene.json"
        path.write_text(json.dumps(self.scene, indent=2), encoding="utf-8")
        return path

    def _room(self) -> Dict[str, Any]:
        if self.audit:
            return self.audit.get("room", {}) or {}
        return {}

    @staticmethod
    def _enrich(report: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add derived metric fields the GDScript side cannot compute cheaply.

        Godot emits glazing area in PLAN units squared, because it does not
        always know the unit scale at that point. Everything downstream
        asks for `total_area_m2`, so without this conversion the model is
        told nothing about window size - and a model that cannot see the
        glazing will happily claim a room with windows has "zero glazing".
        """
        room = report.get("room") or {}
        win = report.get("windows") or {}
        us = float(room.get("unit_scale", 1.0) or 1.0)
        if us <= 0:
            us = 1.0

        plan_area = win.get("total_area_plan2")
        if isinstance(plan_area, (int, float)) and "total_area_m2" not in win:
            win["total_area_m2"] = round(plan_area * us * us, 2)

        # Glazing ratio is the single most useful number for deciding
        # whether a day render needs help. ~10-20% is a normally lit room.
        floor = room.get("floor_area_m2")
        if (isinstance(floor, (int, float)) and floor > 0
                and isinstance(win.get("total_area_m2"), (int, float))):
            win["glazing_to_floor_pct"] = round(
                win["total_area_m2"] / floor * 100.0, 1
            )

        report["windows"] = win
        return report

    # Patterns in Godot output that indicate a fatal script error.
    # When any of these appear, the scene will never produce a report and
    # there is no point waiting the full audit_timeout.
    _FATAL_PATTERNS = (
        "Could not resolve",
        "Parse Error",
        "SCRIPT ERROR",
        "Cannot preload",
        "Invalid call",
        "Parser Error",
    )

    def _run_godot(self, cmd: list, env: dict):
        """
        Run Godot with line-by-line output monitoring.

        Always reads stdout incrementally so that fatal GDScript errors
        (parse failures, missing preloads, etc.) are detected within
        seconds instead of waiting the full audit_timeout. The timeout
        value itself is unchanged — it is still the hard ceiling for
        a legitimately slow scene build.

        When stream_output is True, lines are echoed live.
        """
        import threading

        proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace",
        )
        collected: List[str] = []
        fatal_error: List[str] = []  # mutable container for thread-safe flag

        def pump():
            assert proc.stdout is not None
            for line in proc.stdout:
                collected.append(line)
                stripped = line.rstrip()
                if self.cfg.stream_output and stripped:
                    print(f"        | {stripped[:160]}")
                # Check for fatal GDScript errors — kill early
                for pat in RenderSession._FATAL_PATTERNS:
                    if pat in line:
                        fatal_error.append(stripped)
                        self._note(f"Fatal Godot error detected: {stripped[:200]}")
                        proc.kill()
                        return

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            proc.wait(timeout=self.cfg.audit_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        t.join(timeout=10)

        result = subprocess.CompletedProcess(
            cmd, proc.returncode, "".join(collected), ""
        )

        # If a fatal error was detected, surface it immediately instead of
        # letting the caller silently retry or wait.
        if fatal_error:
            raise RuntimeError(
                f"Godot script error (detected early, avoided {self.cfg.audit_timeout}s wait):\n"
                + "\n".join(fatal_error[:5])
                + f"\n\nFull output tail:\n{''.join(collected[-20:])}"
            )

        return result

    # ------------------------------------------------------------------
    # TOOL: analyze_scene
    # ------------------------------------------------------------------
    def _audit_hook_missing(self) -> str:
        """
        Why the audit cannot possibly work, or "" if the wiring looks present.

        Checked BEFORE launching Godot. Without this, the analysis boots the
        whole scene headless, finds no report, boots it AGAIN windowed, finds
        no report again, and only then raises - roughly 5.5 minutes of GPU time
        per job to discover something a grep rules out in milliseconds.

        The hook lives in image_glb_creation.gd: build_scene() calls
        LightingAudit.emit(...) guarded by the ZLENDO_LIGHTING_AUDIT env var.
        If that call is not in the file, no env var will bring it back.
        """
        project = Path(self.cfg.project_dir)
        builder = project / "image_glb_creation.gd"
        audit_script = project / "lighting_audit.gd"

        if not audit_script.is_file():
            return f"lighting_audit.gd is missing from {project}"
        if not builder.is_file():
            return f"image_glb_creation.gd is missing from {project}"

        try:
            source = builder.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return f"could not read image_glb_creation.gd ({exc})"

        if "LightingAudit" not in source:
            return (
                "image_glb_creation.gd never calls LightingAudit.emit(...), so "
                "build_scene() cannot write a report no matter what the env var "
                "says. Re-add the env-guarded emit block at the end of "
                "build_scene()"
            )
        return ""

    def analyze_scene(self, force: bool = False) -> Dict[str, Any]:
        """
        Build the scene headlessly and measure it. Does not render.
        Cheap relative to a render - this is the agent's eyes.
        """
        if self.audit is not None and not force:
            return self.audit

        missing = self._audit_hook_missing()
        if missing:
            # The Godot-side audit is not wired up in this install. Rather than
            # raise - which skipped the entire AI lighting pass on every render -
            # compute the same report from the scene JSON.
            #
            # This still avoids the two wasted scene builds the check was written
            # to prevent; it just answers the question a different way. The
            # numbers describe the plan as authored rather than the scene as
            # built, which for the agent's purposes (area, glazing, light counts)
            # is equivalent. See scene_audit.py for where that distinction bites.
            from .scene_audit import derive_audit

            self._note(f"Godot lighting audit unavailable ({missing}); "
                       f"deriving the report from the scene JSON instead.")
            print(f"[AI-LIGHTING] Godot audit hook missing ({missing}).")
            print(f"[AI-LIGHTING] Deriving the lighting report from the scene "
                  f"JSON instead - no extra Godot boot, no GPU time.")
            self.audit = self._enrich(derive_audit(self.scene))

            room = self.audit.get("room", {}) or {}
            win = self.audit.get("windows", {}) or {}
            lights = self.audit.get("lights", {}) or {}
            print(f"[AI-LIGHTING] Derived: {room.get('floor_area_m2', 0)} m2 floor, "
                  f"{room.get('ceiling_height', 0)} m ceiling, "
                  f"{win.get('count', 0)} windows "
                  f"({win.get('total_area_m2', 0)} m2 glazing), "
                  f"{lights.get('custom_light_count', 0)} authored lights, "
                  f"{lights.get('fixture_item_count', 0)} fixture props.")
            return self.audit

        scene_path = self._write_scene()
        report_path = Path(self.cfg.work_dir) / f"{self.job_id}_audit.json"
        report_path.unlink(missing_ok=True)

        env = os.environ.copy()
        env["ZLENDO_LIGHTING_AUDIT"] = str(report_path)

        # The audit must boot the scene EXACTLY as a render does.
        #
        # render.gd is the scene script on main.tscn; its _ready() parses the
        # input/output paths from the command line and calls build_scene().
        # Running `--script res://image_glb_creation.gd` instead does nothing
        # useful: that script extends Node3D, has no main-loop entry point,
        # so Godot exits immediately without ever calling build_scene - which
        # is why the audit silently produced no report and no output.
        #
        # A dummy output path is required because render.gd bails out if
        # either argument is missing. Nothing is ever written to it: the
        # _audit_only guard in render.gd returns before any render starts.
        dummy_out = Path(self.cfg.work_dir) / f"{self.job_id}_audit_unused.png"

        def _cmd(headless: bool) -> list:
            c = [self.cfg.godot_exe, "--path", self.cfg.project_dir]
            if headless:
                c.append("--headless")
            c += [self.cfg.main_scene, "--", str(scene_path), str(dummy_out)]
            return c

        self._note("analysing scene (headless: no window, no GPU by design) ...")

        # Headless is much faster and avoids opening a 4K window. If the
        # project turns out to need a real rendering device to build the
        # scene, fall back to a windowed run rather than failing.
        #
        # However, if _run_godot detects a fatal script error (parse
        # failure, missing preload, etc.) it raises RuntimeError
        # immediately. In that case retrying windowed is pointless —
        # the same script error will occur — so we let it propagate.
        proc = self._run_godot(_cmd(True), env)
        if not report_path.exists():
            self._note("headless analysis produced nothing - retrying windowed")
            proc = self._run_godot(_cmd(False), env)

        dummy_out.unlink(missing_ok=True)

        if not report_path.exists():
            tail = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
            raise RuntimeError(
                "Scene analysis produced no report.\n"
                "Check that (a) lighting_audit.gd is in godot_project, "
                "(b) build_scene() in image_glb_creation.gd sets _audit_only, "
                "and (c) render.gd returns on _audit_only after build_scene.\n"
                f"Godot output:\n{tail or '(no output captured)'}"
            )

        self.audit = self._enrich(json.loads(report_path.read_text(encoding="utf-8")))
        report_path.unlink(missing_ok=True)

        room = self.audit.get("room", {})
        lights = self.audit.get("lights", {})
        self._note(
            f"analysed: {room.get('floor_area_m2', 0):.1f} m2, "
            f"{lights.get('ceiling_fixture_count', 0)} fixtures, "
            f"{self.audit.get('windows', {}).get('count', 0)} windows"
        )
        return self.audit

    # ------------------------------------------------------------------
    # TOOL: set_lighting
    # ------------------------------------------------------------------
    def set_lighting(
        self,
        lighting_profile: Optional[str] = None,
        color_mood: Optional[str] = None,
        illumination_level: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Set the three global controls. Values are validated and clamped."""
        current = self._settings()
        proposal = {
            "lightingSettings": {
                "lighting_profile": lighting_profile
                or current.get("lighting_profile", "day"),
                "colorMood": color_mood or current.get("colorMood", "day"),
                "illuminationLevel": (
                    illumination_level
                    if illumination_level is not None
                    else current.get("illuminationLevel", 5)
                ),
            },
            "addLights": [],
        }
        patch, rep = validate_decision(proposal, self._room())
        ls = patch["lightingSettings"]

        current.update(ls)
        for key in ("lighting_profile", "colorMood", "illuminationLevel"):
            self.scene[key] = ls[key]
        prof = ls["lighting_profile"]
        self.scene["day_render"] = prof == "day"
        self.scene["night_render"] = prof == "night"
        self.scene["sunset_render"] = prof == "sunset"

        self._note(f"lighting set -> {ls}")
        return {"applied": ls, "corrections": rep.corrections}

    # ------------------------------------------------------------------
    # TOOL: add_light / remove_light / list_lights
    # ------------------------------------------------------------------
    def add_light(self, light: Dict[str, Any]) -> Dict[str, Any]:
        """Add one light. Rejected if it falls outside the room footprint."""
        if self.audit is None:
            self.analyze_scene()

        proposal = {"lightingSettings": self._settings(), "addLights": [light]}
        patch, rep = validate_decision(proposal, self._room())
        accepted = patch["customLights"]

        if not accepted:
            reason = rep.dropped[0] if rep.dropped else "rejected by validator"
            self._note(f"light REJECTED: {reason}")
            return {"accepted": False, "reason": reason}

        lid, ldata = next(iter(accepted.items()))
        self._pending_lights[lid] = ldata
        self._commit_lights()
        self._note(f"light added: {lid} {ldata.get('_llm_purpose', '')}")
        return {
            "accepted": True,
            "id": lid,
            "light": ldata,
            "corrections": rep.corrections,
        }

    def remove_light(self, light_id: str) -> Dict[str, Any]:
        lid = light_id if light_id.startswith("llm_") else "llm_" + light_id
        existed = self._pending_lights.pop(lid, None) is not None
        self._commit_lights()
        self._note(f"light removed: {lid} (existed={existed})")
        return {"removed": existed, "id": lid}

    def reset_lights(self) -> Dict[str, Any]:
        n = len(self._pending_lights)
        self._pending_lights.clear()
        self._commit_lights()
        self._note(f"cleared {n} agent-added lights")
        return {"cleared": n}

    def _commit_lights(self) -> None:
        existing = self.scene.get("customLights")
        if not isinstance(existing, dict):
            existing = {}
        # Preserve author-placed lights; replace only our own.
        kept = {k: v for k, v in existing.items() if not str(k).startswith("llm_")}
        kept.update(self._pending_lights)
        self.scene["customLights"] = kept
        # Invalidate the audit - the scene changed.
        self.audit = None

    def list_lights(self) -> Dict[str, Any]:
        existing = self.scene.get("customLights") or {}
        return {
            "agent_added": list(self._pending_lights.keys()),
            "author_placed": [
                k for k in existing if not str(k).startswith("llm_")
            ],
            "settings": dict(self._settings()),
        }

    # ------------------------------------------------------------------
    # TOOL: render
    # ------------------------------------------------------------------
    def render(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Fire a full 4K path-traced render, then measure it.

        This is the expensive operation. Budget is enforced here rather
        than trusted to the agent's judgement.
        """
        if self.renders_remaining <= 0:
            return {
                "rendered": False,
                "reason": (
                    f"Render budget exhausted ({self.cfg.max_renders} used). "
                    "Finalise with the best render so far."
                ),
                "best": self.best_render_summary(),
            }

        from .render_metrics import analyse

        idx = len(self.renders) + 1
        out = Path(output_path) if output_path else (
            Path(self.cfg.work_dir) / f"{self.job_id}_render_{idx}.png"
        )
        out.parent.mkdir(parents=True, exist_ok=True)

        scene_path = self._write_scene()

        # NOTE: no --headless. Godot 4 disables rendering under headless,
        # which is why the existing _run_godot_render omits it too.
        cmd = [
            self.cfg.godot_exe,
            "--path", self.cfg.project_dir,
            self.cfg.main_scene,
            "--",
            str(scene_path),
            str(out),
        ]
        self._note(f"render {idx}/{self.cfg.max_renders} starting (4K path trace) ...")
        t0 = time.time()
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=self.cfg.render_timeout, errors="replace",
        )
        elapsed = time.time() - t0

        if not out.exists():
            tail = (proc.stdout or "")[-3000:]
            raise RuntimeError(f"Render produced no output.\nGodot output:\n{tail}")

        metrics = analyse(out)
        record = RenderRecord(
            index=idx,
            path=str(out),
            metrics=metrics,
            settings=dict(self._settings()),
            seconds=round(elapsed, 1),
        )
        self.renders.append(record)
        self._note(
            f"render {idx} done in {elapsed:.0f}s -> {metrics['heuristic_flags']}"
        )

        return {
            "rendered": True,
            "index": idx,
            "path": str(out),
            "seconds": round(elapsed, 1),
            "metrics": metrics,
            "renders_remaining": self.renders_remaining,
        }

    # ------------------------------------------------------------------
    def score(self, metrics: Dict[str, Any]) -> float:
        """
        Deterministic quality score, lower is better.

        Used to pick the winning render. Deliberately NOT delegated to the
        model: choosing between two candidates should be reproducible, and
        the model already had its say when it set the parameters.
        """
        m = metrics
        mean = m.get("mean_luminance", 0.0)
        # Target mid-luminance around 0.22 for a lit interior.
        penalty = abs(mean - 0.22) * 4.0
        penalty += max(0.0, m.get("clipped_highlight_pct", 0.0) - 1.0) * 0.35
        penalty += max(0.0, m.get("crushed_shadow_pct", 0.0) - 10.0) * 0.06
        penalty += max(0.0, 6.0 - m.get("dynamic_range_stops", 0.0)) * 0.18
        penalty += max(0.0, 1.1 - m.get("log_contrast", 0.0)) * 0.5
        cast = m.get("colour_cast_rgb", [0, 0, 0])
        penalty += max(abs(c) for c in cast) * 0.8
        return round(penalty, 4)

    def best_render(self) -> Optional[RenderRecord]:
        if not self.renders:
            return None
        return min(self.renders, key=lambda r: self.score(r.metrics))

    def best_render_summary(self) -> Optional[Dict[str, Any]]:
        best = self.best_render()
        if best is None:
            return None
        return {
            "index": best.index,
            "path": best.path,
            "score": self.score(best.metrics),
            "flags": best.metrics.get("heuristic_flags"),
            "settings": best.settings,
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "renders": [
                {
                    "index": r.index,
                    "path": r.path,
                    "score": self.score(r.metrics),
                    "flags": r.metrics.get("heuristic_flags"),
                    "seconds": r.seconds,
                }
                for r in self.renders
            ],
            "best": self.best_render_summary(),
            "lights_added": len(self._pending_lights),
            "settings": dict(self._settings()),
            "log": self.log,
        }

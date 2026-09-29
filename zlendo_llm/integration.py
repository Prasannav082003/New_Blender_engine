"""
The hook into main.py's existing render path.

Your `/generate-4k-render/` endpoint already works: it queues a job,
`_background_render_task` picks it up, `_run_godot_render` localises
assets, culls the scene, writes the JSON, and calls Godot.

The AI upgrade slots into exactly one place in that chain - after culling,
before the JSON is written - so the endpoint, the queue, the status
updates and the upload path are all untouched.

In `_run_godot_render`, right after the scene-culling block:

    # 2.5 Scene Culling ...
    try:
        optimizer = SceneOptimizer(log_path=culling_log_path)
        godot_input = optimizer.cull_scene(godot_input)
    except Exception as e:
        ...

    # 2.6 AI lighting upgrade  <-- ADD THIS
    from zlendo_llm.integration import ai_upgrade_lighting
    godot_input = ai_upgrade_lighting(godot_input, job_id=job_id, logger=logger)

    # 3. Save Input JSON  (unchanged)

That is the whole integration. It is fail-open by design: any error in
the AI path logs a warning and returns the original scene, so a broken
Ollama install degrades to your current behaviour instead of failing the
job.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ai_model, env_flag, llm_disabled_reason

log = logging.getLogger(__name__)

# Kept as an alias so anything that imported _env_flag from here still works.
_env_flag = env_flag


def resolve_godot_paths() -> tuple:
    """
    Find the Godot binary and project directory.

    main.py does NOT require GODOT_EXE in .env - it computes a default and
    only reads the env var as an override. So relying on the env var alone
    would silently disable the AI pass on a perfectly normal install.
    We therefore try, in order:

      1. the environment (explicit override wins)
      2. the already-imported main module's resolved constants
      3. a filesystem probe relative to the working directory

    Returns (godot_exe, project_dir); either may be "" if not found.
    """
    import sys

    godot = os.getenv("GODOT_EXE", "").strip()
    project = os.getenv("GODOT_PROJECT_PATH", "").strip()

    # 2. Borrow whatever main.py already worked out. We are called from
    #    inside it, so it is in sys.modules and its constants are resolved.
    if not godot or not project:
        main_mod = sys.modules.get("main") or sys.modules.get("__main__")
        if main_mod is not None:
            godot = godot or str(getattr(main_mod, "GODOT_EXE", "") or "")
            project = project or str(getattr(main_mod, "GODOT_PROJECT_PATH", "") or "")

    # 3. Probe the filesystem.
    if not godot:
        for candidate in (
            Path.cwd() / "godot" / "bin" / "godot.windows.editor.x86_64.console.exe",
            Path.cwd() / "godot" / "bin" / "godot.windows.editor.x86_64.exe",
        ):
            if candidate.exists():
                godot = str(candidate)
                break
    if not project:
        candidate = Path.cwd() / "godot_project"
        if candidate.is_dir():
            project = str(candidate)

    return godot, project


def _save_input_json(
    godot_input: Dict[str, Any],
    job_id: str,
    suffix: str,
) -> Optional[str]:
    """
    Save a snapshot of the godot_input dict so the user can compare
    the original scene JSON against what the AI produced.

    Writes to input_json/<job_id>_<suffix>.json.
    suffix is "original" or "ai_modified".
    """
    try:
        out_dir = Path(os.getenv("AI_INPUT_JSON_DIR", "input_json"))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{job_id}_{suffix}.json"
        path.write_text(
            json.dumps(godot_input, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("Saved %s input JSON -> %s", suffix, path)
        return str(path)
    except Exception as exc:  # noqa: BLE001 - logging must never break a job
        log.warning("Could not save %s JSON for %s: %s", suffix, job_id, exc)
        return None


def _write_ai_log(job_id: str, record: Dict[str, Any]) -> Optional[str]:
    """
    Persist the AI decision next to the other per-job logs.

    Without this the only trace of what the model did is a line in the
    uvicorn console and a key buried in a temp file - so after the fact
    there is no way to answer "did the AI pass run, and what did it
    change?". That is not an acceptable audit trail for something that
    alters a paid render.
    """
    try:
        out_dir = Path(os.getenv("AI_LOGS_DIR", "ai_logs"))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{job_id}_ai.json"
        path.write_text(
            json.dumps(record, indent=2, default=str), encoding="utf-8"
        )
        return str(path)
    except Exception as exc:  # noqa: BLE001 - logging must never break a job
        log.warning("Could not write AI log for %s: %s", job_id, exc)
        return None


def ai_upgrade_lighting(
    godot_input: Dict[str, Any],
    job_id: str = "job",
    logger: Optional[Any] = None,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Run the LLM lighting pass over a prepared Godot input dict.

    Returns a patched copy, or the original unchanged if the AI pass is
    disabled or fails. NEVER raises - a render job must not die because a
    model was unavailable.

    Controlled by environment:
        USE_LLM=True                    master switch, default on
        ZLENDO_AI_MODEL=qwen3:8b        model tag (auto-pulled if missing)
        ZLENDO_AI_AUTO_PULL=True        download a missing model, default on
        ZLENDO_AI_HOST=http://...       Ollama host
        ZLENDO_AI_MODE=advise|agent     advise (default) or full agent loop
        ZLENDO_AI_MAX_RENDERS=2         agent mode only

    `force=True` runs the pass regardless of USE_LLM. That is for
    preflight_ai.py, which exists precisely to test the chain on a machine
    where the switch is off.
    """
    disabled = llm_disabled_reason()
    if not force and disabled:
        _write_ai_log(job_id, {
            "job_id": job_id,
            "enabled": False,
            "status": "skipped",
            "reason": disabled,
        })
        return godot_input

    def _log(msg: str) -> None:
        log.info("[ai-lighting %s] %s", job_id, msg)
        print(f"[AI-LIGHTING] {msg}")
        if logger is not None:
            for meth in ("add_step", "add_note", "log"):
                fn = getattr(logger, meth, None)
                if callable(fn):
                    try:
                        fn(f"[AI] {msg}")
                    except Exception:
                        pass
                    break

    record: Dict[str, Any] = {
        "job_id": job_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "enabled": True,
        "mode": os.getenv("ZLENDO_AI_MODE", "advise").strip().lower(),
        "model": ai_model(),
        "status": "started",
    }

    # Save the ORIGINAL input JSON before any AI modification
    _save_input_json(godot_input, job_id, "original")

    t0 = time.time()
    # Held outside the try so the finally can always reach it. Unloading the
    # model is NOT optional cleanup on a 16GB card - see the finally block.
    client = None
    try:
        from .lighting_agent import LightingAgent, apply_patch
        from .ollama_client import OllamaClient, OllamaConfig

        cfg = OllamaConfig(
            host=os.getenv("ZLENDO_AI_HOST", "http://127.0.0.1:11434"),
            model=ai_model(),
        )
        if "thinking" in cfg.model.lower():
            cfg.enable_thinking = True
            cfg.request_timeout = 1800

        client = OllamaClient(cfg)
        if not client.is_up():
            _log("Ollama unreachable - rendering without AI upgrade.")
            record.update(status="skipped", reason=f"Ollama unreachable at {cfg.host}")
            _write_ai_log(job_id, record)
            return godot_input

        # Verify the MODEL exists before doing a minute of scene analysis, and
        # download it if it is missing (ZLENDO_AI_AUTO_PULL, default on).
        # Ollama answers /api/chat with 404 for an unknown model, which is
        # easy to misread as a broken endpoint - catch it here with a
        # message that names the model and lists what is installed.
        try:
            client.ensure_model()
        except Exception as exc:
            installed = []
            try:
                installed = client.available_models()
            except Exception:
                pass
            _log(f"Model '{cfg.model}' unavailable - rendering without AI upgrade.")
            # The exception carries the real reason - unreachable host, bad tag,
            # failed download. Without it the operator sees only "unavailable"
            # and cannot tell a typo in .env from a dead network.
            _log(f"  reason: {exc}")
            _log(f"  installed models: {installed or '(could not list)'}")
            _log(f"  fix: set ZLENDO_AI_MODEL in .env to an installed model, "
                 f"or run 'ollama pull {cfg.model}'")
            record.update(
                status="skipped",
                reason=f"model '{cfg.model}' not available: {exc}",
                installed_models=installed,
            )
            _write_ai_log(job_id, record)
            return godot_input

        # Force-load the model into VRAM with a tiny warmup call, then
        # check whether it actually landed on GPU. Without this, the
        # first real inference silently loads on CPU if CUDA is broken.
        _log("Warming up model (loading into VRAM)...")
        try:
            client.warmup()
            client.check_gpu_loading()
        except Exception as exc:
            _log(f"Warmup failed ({exc}) - continuing, inference may be slow.")

        mode = os.getenv("ZLENDO_AI_MODE", "advise").strip().lower()

        if mode == "agent":
            result = _run_agent_mode(godot_input, job_id, client, _log, record)
            if result is not None:
                godot_input = result
        else:
            godot_input = _run_advise_mode(godot_input, job_id, client, _log, record)

        # Save the AI-MODIFIED input JSON for comparison
        _save_input_json(godot_input, job_id, "ai_modified")

        record["status"] = "applied"
        record["elapsed_seconds"] = round(time.time() - t0, 1)
        written = _write_ai_log(job_id, record)
        _log(f"AI pass finished in {record['elapsed_seconds']}s"
             + (f" - log: {written}" if written else ""))
        return godot_input

    except Exception as exc:  # noqa: BLE001 - must never fail the job
        log.exception("AI lighting pass failed")
        _log(f"AI pass failed ({type(exc).__name__}: {exc}) - rendering unchanged.")
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}",
                      elapsed_seconds=round(time.time() - t0, 1))
        _write_ai_log(job_id, record)
        return godot_input

    finally:
        # THIS MUST BE IN A FINALLY.
        #
        # The unload used to sit on the success path. When the pass raised -
        # which it does on any analysis failure - qwen3:8b stayed resident in
        # VRAM for the full keep_alive window, and the 4K path trace then ran
        # ~6GB short on a 16GB A4000. Godot's viewport capture returned null,
        # render.gd skipped save_png and still exited 0, and main.py's
        # file-exists check turned that into a full re-render. Three times.
        #
        # The render is the expensive, user-visible artifact. Freeing the card
        # is not cleanup we can afford to skip on the error path - it is the
        # error path that needs it most.
        if client is not None:
            # Persist the real token cost. Ollama reports it per call and we
            # were only ever printing it, so the question "what does this model
            # cost per render?" had no durable answer.
            try:
                usage = client.usage_summary()
                record["usage"] = usage
                if usage.get("calls"):
                    _log(f"tokens: {usage['prompt_tokens']} in + "
                         f"{usage['output_tokens']} out = {usage['total_tokens']} "
                         f"over {usage['calls']} call(s), {usage['seconds']}s")
                _write_ai_log(job_id, record)
            except Exception:  # noqa: BLE001
                pass
            try:
                from .ollama_client import print_gpu_status
                print_gpu_status("AI-PRE-UNLOAD")
                client.unload()
                print_gpu_status("AI-POST-UNLOAD")
                _log("model unloaded; VRAM released for the path tracer")
            except Exception as unload_exc:  # noqa: BLE001
                # Report loudly. A failed unload means the next render is
                # about to be starved, and a silent warning here is exactly
                # how that turned into an unexplained retry loop.
                log.error("Could not unload the model: %s", unload_exc)
                _log(f"WARNING: model unload FAILED ({unload_exc}). "
                     f"The 4K render may run short of VRAM.")


# ----------------------------------------------------------------------
def _run_advise_mode(
    godot_input: Dict[str, Any],
    job_id: str,
    client,
    _log,
    record: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Default mode: analyse once, decide once, patch, hand back to the
    existing render path. Adds ~1-2 minutes and exactly zero extra renders.
    """
    from .lighting_agent import LightingAgent, apply_patch
    from .scene_material_analyzer import analyze_scene_materials, format_for_prompt
    from .session import RenderSession, SessionConfig

    godot_exe, project = resolve_godot_paths()
    if not godot_exe or not project:
        _log("Could not locate the Godot binary or project - skipping AI pass.")
        if record is not None:
            record.update(status="skipped",
                          reason="Godot binary or project directory not found")
        return godot_input

    cfg = SessionConfig(
        godot_exe=godot_exe,
        project_dir=project,
        max_renders=0,  # advise mode never renders
    )

    session = RenderSession(godot_input, cfg, job_id=job_id)
    report = session.analyze_scene()

    # ── Material-aware lighting: extract surface colours, existing
    #    lights, and asset context so the model can account for
    #    reflectivity, colour temperature conflicts, and specular risk.
    material_context = None
    try:
        room = report.get("room", {}) or {}
        unit_scale = float(room.get("unit_scale", 1.0) or 1.0)
        if unit_scale <= 0:
            unit_scale = 1.0
        material_analysis = analyze_scene_materials(godot_input, unit_scale=unit_scale)
        material_context = format_for_prompt(material_analysis)
        _log(
            f"Material analysis: reflectivity={material_analysis['metrics'].get('avg_reflectivity', '?')}, "
            f"warmth={material_analysis['metrics'].get('surface_warmth_bias', '?')}, "
            f"existing_lights={material_analysis['metrics'].get('active_lights', 0)}, "
            f"room_type={material_analysis['assets'].get('inferred_room_type', '?')}"
        )
        if record is not None:
            record["material_analysis"] = {
                "avg_reflectivity": material_analysis["metrics"].get("avg_reflectivity"),
                "surface_warmth_bias": material_analysis["metrics"].get("surface_warmth_bias"),
                "warmth_conflict": material_analysis["metrics"].get("warmth_conflict"),
                "specular_risk": material_analysis["metrics"].get("specular_risk"),
                "existing_lights": len(material_analysis.get("lights", [])),
                "inferred_room_type": material_analysis["assets"].get("inferred_room_type"),
                "total_intensity": material_analysis["metrics"].get("total_intensity"),
                "avg_color_temp_k": material_analysis["metrics"].get("avg_color_temp_k"),
            }
    except Exception as exc:
        # Material analysis is supplementary - never block the lighting pass.
        log.warning("Material analysis failed: %s", exc)
        _log(f"Material analysis skipped ({exc}) - proceeding with geometry-only lighting.")

    agent = LightingAgent(client)
    patch, validation = agent.review(report, material_context=material_context)

    _log(
        f"verdict={patch['verdict']} "
        f"issues={len(patch['issues'])} "
        f"lights_added={len(patch['customLights'])}"
    )
    for issue in patch["issues"]:
        _log(f"  issue: {issue}")
    if not validation.clean:
        _log(f"  validator adjusted: {validation.as_dict()}")

    if record is not None:
        room = report.get("room", {}) or {}
        lights = report.get("lights", {}) or {}
        record["analysis"] = {
            "floor_area_m2": round(room.get("floor_area_m2", 0.0), 1),
            "ceiling_height_m": round(room.get("ceiling_height_m", 0.0), 2),
            "window_count": (report.get("windows") or {}).get("count"),
            "lights_built": lights.get("ceiling_fixture_count"),
            "requested_profile": (report.get("profile") or {}).get("lighting_profile"),
        }
        record["decision"] = {
            "verdict": patch["verdict"],
            "issues": patch["issues"],
            "settings": patch["lightingSettings"],
            "lights_added": list(patch["customLights"].keys()),
            "reasoning": patch["reasoning"],
        }
        record["lights_detail"] = patch["customLights"]
        record["validation"] = validation.as_dict()

    return apply_patch(godot_input, patch)


def _run_agent_mode(
    godot_input: Dict[str, Any],
    job_id: str,
    client,
    _log,
    record: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Full autonomous mode: the agent renders, measures, and iterates at 4K
    within a hard budget, then hands back the winning scene.

    Costs real GPU time - ZLENDO_AI_MAX_RENDERS 4K path traces. Because
    the agent has already produced the final image, the caller can use
    `_ai_final_render_path` to skip re-rendering.
    """
    from .agent import LightingDirectorAgent
    from .session import RenderSession, SessionConfig

    godot_exe, project = resolve_godot_paths()
    if not godot_exe or not project:
        _log("Could not locate the Godot binary or project - falling back to advise mode.")
        if record is not None:
            record.update(status="skipped",
                          reason="Godot binary or project directory not found")
        return None

    max_renders = int(os.getenv("ZLENDO_AI_MAX_RENDERS", "2"))
    cfg = SessionConfig(
        godot_exe=godot_exe,
        project_dir=project,
        max_renders=max_renders,
    )
    session = RenderSession(godot_input, cfg, job_id=job_id)

    agent = LightingDirectorAgent(client)
    summary = agent.run(session)

    best = summary.get("best")
    _log(
        f"agent used {summary.get('steps_used')} steps, "
        f"{len(summary.get('renders', []))} renders"
    )
    if best:
        _log(f"best render: score={best['score']} settings={best['settings']}")

    if record is not None:
        record["agent"] = {
            "steps_used": summary.get("steps_used"),
            "renders": summary.get("renders"),
            "best": best,
            "finished_cleanly": summary.get("agent_finished_cleanly"),
        }
        record["decision"] = {"settings": summary.get("settings")}

    out = dict(session.scene)
    out["_aiAgentSummary"] = {
        "renders": summary.get("renders"),
        "best": best,
        "finished_cleanly": summary.get("agent_finished_cleanly"),
    }
    if best:
        # The caller may reuse this instead of rendering again.
        out["_ai_final_render_path"] = best["path"]
    return out

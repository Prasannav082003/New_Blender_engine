"""
End-to-end orchestration: JSON in -> audit -> LLM -> patch -> render.

Run standalone:

    .\\venv\\Scripts\\python.exe -m zlendo_llm.pipeline ^
        --scene image.json ^
        --godot "C:\\Zlendo2026\\4k_PT_Engine_dev\\godot\\bin\\godot.windows.editor.x86_64.console.exe" ^
        --project "C:\\Zlendo2026\\4k_PT_Engine_dev\\godot_project" ^
        --out output_renders\\shot01.png

The VRAM ordering here is load-bearing on a 16GB card. Sequence is:

    Godot audit pass (no LLM in memory, tiny GPU cost)
      -> Ollama loads, decides, UNLOADS
        -> Godot 4K render gets the whole card

Never overlap step 2 and step 3.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .lighting_agent import LightingAgent, apply_patch
from .ollama_client import OllamaClient, OllamaConfig

log = logging.getLogger("zlendo.pipeline")


class GodotRunner:
    def __init__(self, godot_exe: str, project_dir: str, script: str = "res://image_glb_creation.gd"):
        self.godot = Path(godot_exe)
        self.project = Path(project_dir)
        self.script = script
        if not self.godot.exists():
            raise FileNotFoundError(f"Godot binary not found: {self.godot}")
        if not self.project.exists():
            raise FileNotFoundError(f"Godot project not found: {self.project}")

    def _run(self, extra_env: Dict[str, str], timeout: int) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(extra_env)
        cmd = [
            str(self.godot),
            "--path", str(self.project),
            "--headless",
            "--script", self.script,
        ]
        log.info("godot: %s", " ".join(cmd))
        return subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            timeout=timeout, errors="replace",
        )

    def audit(self, scene_json_path: Path, timeout: int = 900) -> Dict[str, Any]:
        """
        Build the scene headlessly and dump the lighting report, then quit
        before rendering. Cheap compared to a full render.
        """
        report_path = Path(tempfile.gettempdir()) / f"zlendo_audit_{os.getpid()}.json"
        if report_path.exists():
            report_path.unlink()

        proc = self._run(
            {
                "ZLENDO_INPUT_JSON": str(scene_json_path),
                "ZLENDO_LIGHTING_AUDIT": str(report_path),
            },
            timeout,
        )

        if not report_path.exists():
            log.error("Audit produced no report.\nSTDOUT:\n%s\nSTDERR:\n%s",
                      proc.stdout[-4000:], proc.stderr[-2000:])
            raise RuntimeError("Godot audit pass did not emit a lighting report.")

        with report_path.open(encoding="utf-8") as f:
            report = json.load(f)
        report_path.unlink(missing_ok=True)
        log.info("Audit complete: %s", _brief(report))
        return report

    def render(self, scene_json_path: Path, out_path: Path, timeout: int = 14400) -> Path:
        proc = self._run(
            {
                "ZLENDO_INPUT_JSON": str(scene_json_path),
                "ZLENDO_OUTPUT_PATH": str(out_path),
            },
            timeout,
        )
        if proc.returncode != 0:
            log.error("Render failed (rc=%s)\n%s", proc.returncode, proc.stderr[-4000:])
            raise RuntimeError(f"Godot render failed with code {proc.returncode}")
        if not out_path.exists():
            raise RuntimeError(f"Render reported success but {out_path} is missing.")
        log.info("Render complete -> %s", out_path)
        return out_path


def _brief(report: Dict[str, Any]) -> str:
    room = report.get("room", {})
    lights = report.get("lights", {})
    return (
        f"{room.get('floor_area_m2', 0):.1f}m2, "
        f"{lights.get('ceiling_fixture_count', 0)} fixtures, "
        f"{report.get('windows', {}).get('count', 0)} windows"
    )


def run(
    scene_path: str,
    godot_exe: str,
    project_dir: str,
    out_path: str,
    model: Optional[str] = None,
    ollama_host: str = "http://127.0.0.1:11434",
    check_exposure: bool = True,
    max_exposure_retries: int = 1,
    dry_run: bool = False,
) -> Dict[str, Any]:
    t_start = time.time()

    scene_file = Path(scene_path)
    with scene_file.open(encoding="utf-8") as f:
        scene = json.load(f)

    cfg = OllamaConfig(host=ollama_host)
    if model:
        cfg.model = model
        if "thinking" in model.lower():
            cfg.enable_thinking = True
            cfg.request_timeout = 1800

    client = OllamaClient(cfg)
    client.ensure_model()
    agent = LightingAgent(client)
    runner = GodotRunner(godot_exe, project_dir)

    result: Dict[str, Any] = {"scene": str(scene_file)}

    # --- 1. Audit -----------------------------------------------------
    report = runner.audit(scene_file)
    result["audit"] = report

    # --- 2. Decide ----------------------------------------------------
    patch, validation = agent.review(report)
    result["decision"] = patch
    result["validation"] = validation.as_dict()

    patched = apply_patch(scene, patch)
    patched_path = scene_file.with_name(scene_file.stem + ".llm.json")
    patched_path.write_text(json.dumps(patched, indent=2), encoding="utf-8")
    result["patched_scene"] = str(patched_path)
    log.info("Patched scene written -> %s", patched_path)

    if dry_run:
        client.unload()
        result["elapsed_s"] = round(time.time() - t_start, 1)
        return result

    # --- 3. Free the GPU before rendering. Not optional at 16GB. -------
    client.unload()
    still_loaded = client.loaded_models()
    if still_loaded:
        log.warning("Model still resident after unload: %s", still_loaded)

    # --- 4. Render ----------------------------------------------------
    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    runner.render(patched_path, out_file)
    result["render"] = str(out_file)

    # --- 5. Optional exposure critic ----------------------------------
    if check_exposure:
        from .render_metrics import analyse

        attempt = 0
        while attempt <= max_exposure_retries:
            metrics = analyse(out_file)
            result.setdefault("metrics", []).append(metrics)
            log.info("Render metrics: %s", metrics["heuristic_flags"])

            if metrics["heuristic_flags"] == ["nominal"]:
                log.info("Exposure nominal - no correction needed.")
                break
            if attempt >= max_exposure_retries:
                log.info("Exposure imperfect but retry budget exhausted.")
                break

            verdict = agent.review_exposure(metrics)
            result.setdefault("exposure_reviews", []).append(verdict)
            log.info("Exposure verdict: %s (%s)", verdict["verdict"], verdict["problem"])

            if not verdict["shouldRerender"]:
                break

            level = patched.get("_lightingSettings", {}).get("illuminationLevel", 5)
            new_level = max(1, min(5, level + verdict["illuminationLevelDelta"]))
            if new_level == level and abs(verdict["tonemapExposureScale"] - 1.0) < 0.02:
                log.info("Correction is a no-op; stopping.")
                break

            patched["_lightingSettings"]["illuminationLevel"] = new_level
            patched["illuminationLevel"] = new_level
            patched["_tonemapExposureScale"] = verdict["tonemapExposureScale"]
            patched_path.write_text(json.dumps(patched, indent=2), encoding="utf-8")
            log.info("Re-rendering at illuminationLevel=%d", new_level)

            client.unload()
            runner.render(patched_path, out_file)
            attempt += 1

    client.unload()
    result["elapsed_s"] = round(time.time() - t_start, 1)
    return result


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(description="LLM-assisted lighting pass for the 4k PT engine.")
    ap.add_argument("--scene", required=True, help="Input scene JSON (image.json / video.json).")
    ap.add_argument("--godot", required=True, help="Path to godot console exe.")
    ap.add_argument("--project", required=True, help="Path to godot_project.")
    ap.add_argument("--out", required=True, help="Output render path.")
    ap.add_argument("--model", default=None, help="Ollama model tag override.")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--no-exposure-check", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="Audit + decide + write patched JSON, but do not render.")
    args = ap.parse_args()

    try:
        result = run(
            scene_path=args.scene,
            godot_exe=args.godot,
            project_dir=args.project,
            out_path=args.out,
            model=args.model,
            ollama_host=args.host,
            check_exposure=not args.no_exposure_check,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        log.exception("Pipeline failed: %s", exc)
        return 1

    print("\n" + "=" * 70)
    d = result["decision"]
    print(f"Verdict on original lighting : {d['verdict']}")
    print(f"Issues found                 : {len(d['issues'])}")
    for issue in d["issues"]:
        print(f"  - {issue}")
    print(f"Lights added                 : {len(d['customLights'])}")
    print(f"Settings                     : {d['lightingSettings']}")
    print(f"\nReasoning: {d['reasoning']}")
    print(f"\nElapsed: {result['elapsed_s']}s")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())

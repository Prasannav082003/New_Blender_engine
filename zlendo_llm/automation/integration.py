"""
Hook for main.py: colour-aware automatic lighting adjustment.

Slots in AFTER scene culling and BEFORE the existing AI lighting pass:

    # 2.5  Scene Culling
    godot_input = optimizer.cull_scene(godot_input)

    # 2.55 Automation: colour-aware lighting  <-- THIS
    from zlendo_llm.automation.integration import auto_adjust_lighting
    godot_input = auto_adjust_lighting(godot_input, job_id=job_id, logger=logger)

    # 2.6  AI Lighting Upgrade (existing)
    godot_input = ai_upgrade_lighting(godot_input, ...)

    # 3.   Save Input JSON

WHY after culling:
  The culled scene contains only the rooms/walls visible from the camera.
  Sending the full scene wastes tokens on rooms the viewer never sees,
  and worse, their colours skew the analysis (e.g. a dark utility room
  behind the camera would make the LLM brighten a well-lit living room).

WHY before ai_upgrade_lighting:
  This pass adjusts global settings (illuminationLevel, colorMood,
  ambientBoost).  The existing AI lighting pass adds custom lights
  and fine-tunes on top of whatever globals are set.  So this goes first
  as a "base layer" that the lighting agent can then refine.

Fail-open: any error returns the scene unchanged.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import ai_model, automation_disabled_reason, env_flag

log = logging.getLogger(__name__)


def auto_adjust_lighting(
    godot_input: Dict[str, Any],
    job_id: str = "job",
    logger: Optional[Any] = None,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Analyse the culled scene's surface colours and adjust lighting
    parameters to compensate for dark/light surfaces.

    Returns a patched copy, or the original unchanged if the pass is
    disabled or fails.  NEVER raises.
    """
    disabled = automation_disabled_reason()
    if not force and disabled:
        print(f"[AUTO-LIGHTING] {disabled}")
        return godot_input

    def _log(msg: str) -> None:
        log.info("[auto-lighting %s] %s", job_id, msg)
        print(f"[AUTO-LIGHTING] {msg}")
        if logger is not None:
            for meth in ("add_step", "add_note", "log"):
                fn = getattr(logger, meth, None)
                if callable(fn):
                    try:
                        fn(f"[AUTO] {msg}")
                    except Exception:
                        pass
                    break

    t0 = time.time()
    client = None

    try:
        from .color_analyzer import analyze_scene_colors
        from .lighting_adjuster import AutoLightingAdjuster

        # ── 1. Analyse colours (no LLM needed) ──────────────────
        analysis = analyze_scene_colors(godot_input)
        _log(
            f"Colour analysis: {analysis.room_count} room(s), "
            f"overall luminance={analysis.overall_luminance:.3f} "
            f"({analysis.overall_classification}), "
            f"mood={analysis.dominant_mood}"
        )

        if analysis.room_count == 0:
            _log("No rooms found in culled scene - skipping automation.")
            return godot_input

        # ── 2. Ask the LLM for lighting adjustments ──────────────
        from ..ollama_client import OllamaClient, OllamaConfig

        cfg = OllamaConfig(
            host=os.getenv("ZLENDO_AI_HOST", "http://127.0.0.1:11434"),
            model=ai_model(),
        )

        client = OllamaClient(cfg)
        if not client.is_up():
            _log("Ollama unreachable - using deterministic fallback.")
            # Use the fallback path inside the adjuster
            from .lighting_adjuster import _fallback_adjustment, _validate
            adjustment = _validate(
                _fallback_adjustment(analysis), analysis
            )
        else:
            try:
                client.ensure_model()
            except Exception as exc:
                _log(f"Model unavailable ({exc}) - using deterministic fallback.")
                from .lighting_adjuster import _fallback_adjustment, _validate
                adjustment = _validate(
                    _fallback_adjustment(analysis), analysis
                )
            else:
                adjuster = AutoLightingAdjuster(client)
                adjustment = adjuster.adjust(analysis)

        _log(
            f"Adjustment: illumination={adjustment['illuminationLevel']}, "
            f"mood={adjustment['colorMood']}, "
            f"ambientBoost={adjustment['ambientBoost']:.2f}, "
            f"sunMult={adjustment['sunEnergyMultiplier']:.2f}, "
            f"exposure={adjustment.get('exposureScale', 1.0):.2f}"
        )
        _log(f"Reason: {adjustment['reasoning']}")

        # ── 3. Apply the adjustment to the scene ─────────────────
        result = _apply_adjustment(godot_input, adjustment, analysis)

        elapsed = round(time.time() - t0, 1)
        _log(f"Automation pass done in {elapsed}s")

        # Save log
        _save_auto_log(job_id, analysis, adjustment, elapsed)

        return result

    except Exception as exc:
        log.exception("Automation lighting pass failed")
        _log(f"Automation failed ({type(exc).__name__}: {exc}) - unchanged.")
        return godot_input

    finally:
        # Unload the model so VRAM is free for the next pass (or the render).
        if client is not None:
            try:
                client.unload()
                _log("Model unloaded after automation pass.")
            except Exception:
                pass


def _apply_adjustment(
    scene: Dict[str, Any],
    adjustment: Dict[str, Any],
    analysis: Any,
) -> Dict[str, Any]:
    """Merge the adjustment into the scene JSON."""
    out = copy.deepcopy(scene)

    # illuminationLevel — goes at root AND _lightingSettings
    level = adjustment["illuminationLevel"]
    out["illuminationLevel"] = level
    ls = out.setdefault("_lightingSettings", {})
    if isinstance(ls, dict):
        ls["illuminationLevel"] = level

    # colorMood — same dual placement
    mood = adjustment["colorMood"]
    out["colorMood"] = mood
    if isinstance(ls, dict):
        ls["colorMood"] = mood

    # ambientBoost — stored for image_glb_creation.gd to read
    out["_automationAmbientBoost"] = adjustment["ambientBoost"]

    # sunEnergyMultiplier — stored for image_glb_creation.gd to read
    out["_automationSunMultiplier"] = adjustment["sunEnergyMultiplier"]

    # exposureScale — the whole-image brightness trim. Written under the name
    # the engine and pipeline.py already use for it, so the exposure critic
    # and this pass share one field rather than two that can disagree.
    if "exposureScale" in adjustment:
        out["_tonemapExposureScale"] = adjustment["exposureScale"]

    # Provenance tag
    out["_automationColorAdjustment"] = {
        "overall_luminance": analysis.overall_luminance,
        "overall_classification": analysis.overall_classification,
        "dominant_mood": analysis.dominant_mood,
        "illuminationLevel": level,
        "colorMood": mood,
        "ambientBoost": adjustment["ambientBoost"],
        "sunEnergyMultiplier": adjustment["sunEnergyMultiplier"],
        "exposureScale": adjustment.get("exposureScale", 1.0),
        "reasoning": adjustment["reasoning"],
    }

    return out


def _save_auto_log(
    job_id: str,
    analysis: Any,
    adjustment: Dict[str, Any],
    elapsed: float,
) -> None:
    """Persist the automation decision for audit."""
    try:
        out_dir = Path(os.getenv("AI_LOGS_DIR", "ai_logs"))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{job_id}_auto_color.json"
        record = {
            "job_id": job_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "elapsed_seconds": elapsed,
            "analysis": analysis.as_dict(),
            "adjustment": adjustment,
        }
        path.write_text(
            json.dumps(record, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("Could not save automation log: %s", exc)


# ══════════════════════════════════════════════════════════════════════════
# Per-fixture emission pass
# ══════════════════════════════════════════════════════════════════════════
#
# Runs AFTER auto_adjust_lighting, for two reasons.
#
# It reads colorMood and illuminationLevel, and the colour pass is what
# sets them. Running emission first would tune every lamp to a mood that
# is about to change underneath it.
#
# And the two passes answer different questions. The colour pass decides
# how bright the ROOM is (illuminationLevel, ambientBoost, sun multiplier).
# This one decides what each individual light ASSET emits. A scene can have
# a perfect illuminationLevel and still look wrong because its six lamps
# span 2000K-7500K, or because the ceiling pendant is dimmer than the floor
# lamps. No global control reaches that; only per-fixture values do.
#
# It costs a second model load. That is accepted: the pass is short, the
# prompt is a summary rather than the scene, and the alternative - folding
# it into the colour pass - would mean one prompt doing two jobs and one
# failure taking out both.
#
# Gated by AUTOMATION_LLM_EMISSION (and USE_LLM above it). Fail-open: any
# error returns the scene unchanged.


def auto_adjust_emission(
    godot_input: Dict[str, Any],
    job_id: str = "job",
    logger: Optional[Any] = None,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Set light_meta.intensity / light_meta.temperature per light asset.

    Returns a patched copy, or the original unchanged if the pass is
    disabled, finds no light assets, or fails. NEVER raises.
    """
    from ..config import emission_disabled_reason

    disabled = emission_disabled_reason()
    if not force and disabled:
        print(f"[AUTO-EMISSION] {disabled}")
        return godot_input

    def _log(msg: str) -> None:
        log.info("[auto-emission %s] %s", job_id, msg)
        print(f"[AUTO-EMISSION] {msg}")
        if logger is not None:
            for meth in ("add_step", "add_note", "log"):
                fn = getattr(logger, meth, None)
                if callable(fn):
                    try:
                        fn(f"[AUTO] {msg}")
                    except Exception:
                        pass
                    break

    t0 = time.time()
    client = None

    try:
        from .emission_agent import EmissionAgent, _fallback_emission, _validate
        from .emission_analyzer import analyze_scene_emission

        # ── 1. Analyse (no LLM) ─────────────────────────────────
        analysis = analyze_scene_emission(godot_input)

        if not analysis.fixtures:
            _log("No light assets with light_meta found - skipping emission pass.")
            return godot_input

        _log(
            f"{len(analysis.fixtures)} light asset(s), "
            f"room={analysis.inferred_room_type}, "
            f"albedo={analysis.room_albedo:.2f}, "
            f"mode={'night' if analysis.night_render else 'day'}"
        )
        for problem in analysis.detect_problems():
            _log(f"  issue: {problem}")

        # ── 2. Decide ────────────────────────────────────────────
        from ..ollama_client import OllamaClient, OllamaConfig

        cfg = OllamaConfig(
            host=os.getenv("ZLENDO_AI_HOST", "http://127.0.0.1:11434"),
            model=ai_model(),
        )
        client = OllamaClient(cfg)

        if not client.is_up():
            _log("Ollama unreachable - using deterministic fallback.")
            decision = _validate(_fallback_emission(analysis), analysis)
            decision["source"] = "fallback"
        else:
            try:
                client.ensure_model()
            except Exception as exc:
                _log(f"Model unavailable ({exc}) - using deterministic fallback.")
                decision = _validate(_fallback_emission(analysis), analysis)
                decision["source"] = "fallback"
            else:
                decision = EmissionAgent(client).adjust(analysis)

        _log(
            f"Room key temperature {decision['roomKeyTemperature']}K "
            f"(source={decision.get('source', '?')})"
        )
        for f in decision["fixtures"]:
            _log(
                f"  {f['name'][:34]:34s} {f['role']:9s} "
                f"int={f['intensity']:<6g} temp={f['temperature']}K"
                + ("  [locked]" if f.get("locked") else "")
            )
        for w in decision.get("validator_notes", []):
            _log(f"  validator: {w}")

        # ── 3. Apply ─────────────────────────────────────────────
        result = _apply_emission(godot_input, decision)

        elapsed = round(time.time() - t0, 1)
        _log(f"Emission pass done in {elapsed}s")
        _save_emission_log(job_id, analysis, decision, elapsed)
        return result

    except Exception as exc:
        log.exception("Emission pass failed")
        _log(f"Emission pass failed ({type(exc).__name__}: {exc}) - unchanged.")
        return godot_input

    finally:
        if client is not None:
            try:
                client.unload()
                _log("Model unloaded after emission pass.")
            except Exception:
                pass


def _apply_emission(
    scene: Dict[str, Any],
    decision: Dict[str, Any],
) -> Dict[str, Any]:
    """Write the decided values back into each item's light_meta.

    floor_plan_data arrives as a JSON *string* from the editor and as a dict
    once something upstream has parsed it. Whichever it was on the way in is
    what it must be on the way out - Godot's parser is not tolerant of the
    swap, and a pass that silently changes the type of that field breaks the
    render two stages later where nobody looks.
    """
    out = copy.deepcopy(scene)

    fpd = out.get("floor_plan_data")
    was_string = isinstance(fpd, str)
    if was_string:
        try:
            fpd = json.loads(fpd)
        except (json.JSONDecodeError, TypeError):
            log.warning("emission: cannot parse floor_plan_data; scene unchanged")
            return scene
    if not isinstance(fpd, dict):
        return scene

    by_id = {f["id"]: f for f in decision.get("fixtures", [])}
    applied = 0

    for layer in (fpd.get("layers") or {}).values():
        if not isinstance(layer, dict):
            continue
        for item_id, item in (layer.get("items") or {}).items():
            if not isinstance(item, dict) or item_id not in by_id:
                continue
            f = by_id[item_id]
            if f.get("locked"):
                continue
            lm = item.get("light_meta")
            if not isinstance(lm, dict):
                lm = {}
            # Preserve any keys the editor put here that we do not own.
            lm["intensity"] = float(f["intensity"])
            lm["temperature"] = float(f["temperature"])
            item["light_meta"] = lm
            applied += 1

    out["floor_plan_data"] = json.dumps(fpd, separators=(",", ":")) if was_string else fpd

    out["_automationEmissionAdjustment"] = {
        "roomKeyTemperature": decision.get("roomKeyTemperature"),
        "verdict": decision.get("verdict"),
        "source": decision.get("source"),
        "fixtures_adjusted": applied,
        "fixtures": [
            {
                "id": f["id"],
                "name": f["name"],
                "role": f["role"],
                "intensity": f["intensity"],
                "temperature": f["temperature"],
                "enabled": f["enabled"],
                "reason": f.get("reason", ""),
            }
            for f in decision.get("fixtures", [])
        ],
        "warnings": decision.get("warnings", []),
        "reasoning": decision.get("reasoning", ""),
    }

    log.info("Emission applied to %d fixture(s)", applied)
    return out


def _save_emission_log(
    job_id: str,
    analysis: Any,
    decision: Dict[str, Any],
    elapsed: float,
) -> None:
    """Persist the emission decision for audit."""
    try:
        out_dir = Path(os.getenv("AI_LOGS_DIR", "ai_logs"))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{job_id}_auto_emission.json"
        record = {
            "job_id": job_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "elapsed_seconds": elapsed,
            "analysis": analysis.as_dict(),
            "decision": decision,
        }
        path.write_text(
            json.dumps(record, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("Could not save emission log: %s", exc)

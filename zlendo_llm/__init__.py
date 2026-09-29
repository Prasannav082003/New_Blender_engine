"""LLM-assisted lighting and rendering for the 4k_PT_Engine Godot pipeline."""

# NOTE (2026-08-27): the furniture-placement and prompt-material passes were
# removed from this package - `furniture_agent`, `furniture_apply`,
# `furniture_catalog`, `furniture_integration`, `furniture_placement`,
# `prompt_agent`, `prompt_apply`, `prompt_integration`, `prompt_schema`,
# `prompt_targets`, `material_catalog` and `glb_materials` have no source here,
# only stale bytecode under __pycache__/.
#
# They used to be imported below. Because importing ANY submodule executes this
# file, those twelve dead imports raised ModuleNotFoundError for the whole
# package - which silently disabled the lighting and automation passes too,
# since every call site in main.py swallows the error and prints
# "[WARN] ... unavailable (continuing)". The renders succeeded and simply had
# no AI pass at all.
#
# This file now exports only what actually ships. If those modules are ever
# restored, re-add their imports and their __all__ entries together.

from .agent import LightingDirectorAgent
from .integration import ai_upgrade_lighting
from .lighting_agent import LightingAgent, apply_patch
from .ollama_client import OllamaClient, OllamaConfig, OllamaError
from .render_rules import ALL_RULES, prompt_fragment, rules_for
from .session import RenderSession, SessionConfig
from .tools import TOOLS, dispatch, ollama_tool_specs

# Automation: colour-aware lighting adjustment
from .automation.integration import auto_adjust_lighting

__all__ = [
    "LightingAgent",
    "LightingDirectorAgent",
    "RenderSession",
    "SessionConfig",
    "OllamaClient",
    "OllamaConfig",
    "OllamaError",
    "ai_upgrade_lighting",
    "apply_patch",
    "ALL_RULES",
    "prompt_fragment",
    "rules_for",
    "dispatch",
    "ollama_tool_specs",
    "TOOLS",
    # Automation
    "auto_adjust_lighting",
]

__version__ = "0.4.0"

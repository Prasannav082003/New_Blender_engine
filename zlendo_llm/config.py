"""
One switch for every LLM pass in the pipeline.

`USE_LLM` in .env decides whether any model runs at all. Every pass gates on it,
including the colour-aware automation pass - see automation_enabled(), which
checks USE_LLM before its own flag. There used to be a second flag,
ZLENDO_AI_RENDER, sitting in front of the lighting pass; it is gone. Two
switches for one capability only ever produced the same bug report twice:
"the AI did nothing" because the other flag was unset.

The switches nest; they are not peers:

    USE_LLM=False                      nothing runs. No model is contacted by
                                       any pass, whatever else is set.
    USE_LLM=True                       the AI lighting pass runs.
      AUTOMATION_LLM_ADJUSTMENT=True   ... and so does the automation pass.
      AUTOMATION_LLM_ADJUSTMENT=False  ... but not the automation pass.

This file previously described AUTOMATION_LLM_ADJUSTMENT as "independent of
USE_LLM", and the code agreed with that comment rather than with the paragraph
above. The result was that USE_LLM=False logged "LLM passes are turned off" and
then ran a model anyway, writing model-authored `reasoning` into the job JSON.
If you are tempted to make a pass independent of USE_LLM again: don't. A
kill-switch that does not kill is worse than no kill-switch.

Default is ON. A missing USE_LLM means the operator never opted out, and
silently rendering without the AI pass is the failure that is hardest to
notice - the job succeeds, the image is just worse than it should be. The
passes are individually fail-open anyway, so an unreachable Ollama degrades to
the old behaviour without needing a flag to say so.

Set USE_LLM=False to turn everything off.

PER-JOB OVERRIDE (input JSON)
-----------------------------
A request may also carry "USE_LLM" in its input JSON. It is checked SECOND,
under .env, never over it:

    .env USE_LLM=False   nothing runs, whatever the JSON says. The operator
                         who owns the box has taken the model offline; a
                         request arriving over the API does not get to switch
                         it back on. This is the whole point of a kill-switch.
    .env USE_LLM=True    the JSON decides for this job:
        JSON USE_LLM=True    passes run (same as before this field existed)
        JSON USE_LLM=False   passes are skipped for THIS job only
        JSON key absent      passes run - .env stands

So the effective answer is `env AND json`, with a missing JSON key reading as
"no opinion" rather than as False. Any other rule would either let a caller
re-enable a model the operator disabled, or make every existing caller - none
of which send the field - suddenly stop using the LLM.

The override is per-job state, held in a ContextVar and set by main.py around
the pass block (see apply_job_llm_flag). It is deliberately NOT a module
global: two renders in two threads must not read each other's flag.
"""

from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar, Token
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

# Accepted spellings for both states. Anything else is treated as "unparseable"
# and falls back to the default, with a warning - a typo like USE_LLM=Ture
# should not silently disable the feature.
_TRUE = frozenset({"1", "true", "yes", "on", "y", "t", "enable", "enabled"})
_FALSE = frozenset({"0", "false", "no", "off", "n", "f", "disable", "disabled"})


def parse_flag(raw: Any) -> Optional[bool]:
    """Parse a boolean out of a value of unknown type.

    Returns None when the value carries no opinion (absent, empty) or cannot
    be read as a boolean. Callers decide what "no opinion" means for them -
    env_flag() falls back to its default, the JSON override treats it as
    "field not sent".

    Accepts real booleans, 0/1 numbers, and the string spellings in _TRUE /
    _FALSE, so an input JSON written by hand ("true", "True", "yes") and one
    written by a serialiser (true) both work.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return bool(raw)
    if isinstance(raw, str):
        text = raw.strip().strip('"').strip("'").lower()
        if not text:
            return None
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable tolerantly."""
    raw = os.getenv(name)
    if raw is None:
        return default
    parsed = parse_flag(raw)
    if parsed is None:
        if str(raw).strip():
            log.warning(
                "%s=%r is not a boolean - using default %s", name, raw, default
            )
        return default
    return parsed


# The model every pass uses, and the only place its default lives.
#
# This used to be spelled out at each call site, and they had drifted apart:
# integration.py defaulted to "qwen3:14b" while automation/integration.py
# defaulted to "qwen3:8b", and pipeline.py passed no model at all so it silently
# took the dataclass default in ollama_client.py - a third value. Setting
# ZLENDO_AI_MODEL in .env fixed two of the three and left the rest to chance.
# One function now answers "which model?" for the whole package.
DEFAULT_AI_MODEL = "qwen3:8b"


def ai_model() -> str:
    """The model tag from ZLENDO_AI_MODEL, or the package default."""
    raw = os.getenv("ZLENDO_AI_MODEL")
    if raw is None:
        return DEFAULT_AI_MODEL
    text = raw.strip().strip('"').strip("'")
    return text or DEFAULT_AI_MODEL


def auto_pull_enabled() -> bool:
    """True when a missing model should be downloaded automatically.

    Default ON: an operator who sets ZLENDO_AI_MODEL in .env is stating which
    model the pipeline should use, and having the first render quietly skip the
    AI passes because nobody ran `ollama pull` is the failure that wastes the
    most time - the job succeeds and the image is merely worse.

    Set ZLENDO_AI_AUTO_PULL=False on a metered, air-gapped or disk-constrained
    render box, where an unattended multi-GB download is the worse surprise.
    """
    return env_flag("ZLENDO_AI_AUTO_PULL", True)


# --- per-job override, read from the request's input JSON -------------------
#
# ContextVar, not a module global: renders can run concurrently, and a video
# job that sent USE_LLM=false must not switch off an image job running beside
# it. Each render sets it around its own pass block and resets it after.
_JOB_LLM_OVERRIDE: ContextVar[Optional[bool]] = ContextVar(
    "zlendo_job_llm_override", default=None
)

# Spellings accepted for the field in the input JSON. Callers write it as
# USE_LLM (matching .env), but a JS front end serialising camelCase should not
# silently lose the flag - a dropped kill-switch is exactly the failure this
# whole file exists to prevent.
_JSON_LLM_KEYS = ("USE_LLM", "use_llm", "useLlm", "useLLM", "UseLLM")


def env_llm_enabled() -> bool:
    """The .env answer alone, ignoring any per-job override."""
    return env_flag("USE_LLM", True)


def read_llm_flag(source: Any) -> Optional[bool]:
    """Pull USE_LLM out of an input-JSON-shaped object.

    Looks at the top level first, then inside floor_plan_data (which may be a
    dict or a JSON string) - some callers put job-level switches there. Returns
    None when the field is absent or unreadable, meaning "no opinion, .env
    stands".
    """
    if source is None:
        return None

    def _lookup(obj: Any) -> Optional[bool]:
        if not isinstance(obj, Mapping):
            return None
        for key in _JSON_LLM_KEYS:
            if key in obj:
                raw = obj[key]
                parsed = parse_flag(raw)
                if parsed is None:
                    log.warning(
                        "input JSON %s=%r is not a boolean - ignoring it, "
                        ".env decides",
                        key,
                        raw,
                    )
                return parsed
        return None

    found = _lookup(source)
    if found is not None:
        return found

    if isinstance(source, Mapping):
        nested = source.get("floor_plan_data")
        if isinstance(nested, str):
            try:
                nested = json.loads(nested)
            except (ValueError, TypeError):
                nested = None
        return _lookup(nested)
    return None


def set_job_llm_override(value: Optional[bool]) -> Token:
    """Set the per-job override. Pass the returned token to reset_job_llm_override()."""
    return _JOB_LLM_OVERRIDE.set(value)


def reset_job_llm_override(token: Optional[Token] = None) -> None:
    """Undo set_job_llm_override(). Without a token, clears the override."""
    if token is not None:
        try:
            _JOB_LLM_OVERRIDE.reset(token)
            return
        except ValueError:
            # Token from another context (thread hand-off); fall through.
            pass
    _JOB_LLM_OVERRIDE.set(None)


def job_llm_override() -> Optional[bool]:
    """The per-job override, or None when the job did not send one."""
    return _JOB_LLM_OVERRIDE.get()


def apply_job_llm_flag(source: Any, job_id: Any = None) -> Token:
    """Read USE_LLM from a job's input JSON and install it for this job.

    Call once per render, before the pass block, and hand the returned token to
    reset_job_llm_override() when the block is done. Logs what it decided:
    "the AI did nothing" is a bug report that costs an hour when the log does
    not say which switch was responsible.
    """
    flag = read_llm_flag(source)
    token = set_job_llm_override(flag)
    tag = f"[LLM-GATE {job_id}]" if job_id is not None else "[LLM-GATE]"
    env_on = env_llm_enabled()
    if not env_on:
        msg = f"{tag} .env USE_LLM is off - no LLM pass will run for this job"
    elif flag is None:
        msg = f"{tag} input JSON has no USE_LLM - .env stands, LLM passes ON"
    elif flag:
        msg = f"{tag} input JSON USE_LLM=true - LLM passes ON"
    else:
        msg = f"{tag} input JSON USE_LLM=false - LLM passes OFF for this job"
    log.info(msg)
    print(msg)
    return token


def llm_enabled() -> bool:
    """True when an LLM pass may run for the job in flight.

    .env is checked FIRST and it wins: an operator who set USE_LLM=False has
    taken the model off this box, and a request must not be able to turn it
    back on. The input JSON can only narrow that, never widen it.
    """
    if not env_llm_enabled():
        return False
    override = job_llm_override()
    if override is None:
        return True
    return override


def llm_disabled_reason() -> Optional[str]:
    """A log-ready explanation, or None when the LLM is enabled.

    Names whichever switch actually turned it off - .env or the job's own
    input JSON - so the log points at the thing that needs changing.
    """
    if llm_enabled():
        return None
    if not env_llm_enabled():
        return f"USE_LLM={os.getenv('USE_LLM', '')!r} in .env - LLM passes are turned off"
    return "USE_LLM=false in the job input JSON - LLM passes are off for this job"


def automation_enabled() -> bool:
    """True when the colour-aware automation pass should run.

    Requires BOTH switches:
      USE_LLM                    master kill-switch for every AI pass
      AUTOMATION_LLM_ADJUSTMENT  this pass specifically (default True)

    USE_LLM is checked FIRST and it wins. This used to read only
    AUTOMATION_LLM_ADJUSTMENT, described as "independent of USE_LLM" - which
    meant USE_LLM=False stopped the lighting pass, printed
    "LLM passes are turned off", and then the automation pass went ahead and
    called the model anyway. An operator who turns the LLM off and still finds
    model-authored reasoning in their job JSON has been lied to by the switch.

    The nesting is the point: USE_LLM=False means no model runs, full stop.
    AUTOMATION_LLM_ADJUSTMENT=False turns off just this pass while leaving the
    AI lighting pass on. There is no combination that runs a model with
    USE_LLM=False.
    """
    if not llm_enabled():
        return False
    return env_flag("AUTOMATION_LLM_ADJUSTMENT", True)


def automation_disabled_reason() -> Optional[str]:
    """A log-ready explanation, or None when automation is enabled.

    Names whichever switch actually turned it off, so the log points at the
    line in .env that needs changing rather than at the pass that stopped.
    """
    if automation_enabled():
        return None
    # Report the master switch first - it is the one that overrides.
    master = llm_disabled_reason()
    if master:
        return f"{master} (this also disables the automation pass)"
    return (
        f"AUTOMATION_LLM_ADJUSTMENT="
        f"{os.getenv('AUTOMATION_LLM_ADJUSTMENT', '')!r}"
        f" - automation lighting pass is turned off"
    )


def emission_enabled() -> bool:
    """True when the per-fixture emission pass should run.

    Nests under USE_LLM exactly like automation_enabled(), for the same
    reason spelled out at the top of this file: a kill-switch that does not
    kill is worse than no kill-switch.

      USE_LLM=False                nothing runs, including this pass.
      USE_LLM=True                 the AI lighting pass runs.
        AUTOMATION_LLM_EMISSION=True   ... and so does the emission pass.
        AUTOMATION_LLM_EMISSION=False  ... but not the emission pass.

    Deliberately a SEPARATE flag from AUTOMATION_LLM_ADJUSTMENT rather than
    riding on it. The two passes write different fields and fail
    differently: the colour pass sets room-level exposure, this one rewrites
    every fixture's light_meta. An operator debugging a washed-out render
    needs to be able to turn off one and keep the other, and an operator who
    has hand-authored their light_meta values needs to turn this one off
    without losing colour adaptation.

    Default ON, matching the automation pass: a missing flag means nobody
    opted out, and the failure it prevents - a render that succeeds while
    looking wrong - is the one hardest to notice.
    """
    if not llm_enabled():
        return False
    return env_flag("AUTOMATION_LLM_EMISSION", True)


def emission_disabled_reason() -> Optional[str]:
    """A log-ready explanation, or None when the emission pass is enabled."""
    if emission_enabled():
        return None
    master = llm_disabled_reason()
    if master:
        return f"{master} (this also disables the emission pass)"
    return (
        f"AUTOMATION_LLM_EMISSION="
        f"{os.getenv('AUTOMATION_LLM_EMISSION', '')!r}"
        f" - per-fixture emission pass is turned off"
    )

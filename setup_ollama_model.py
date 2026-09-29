#!/usr/bin/env python3
"""
Get the Ollama model named in .env onto this machine, and prove it works.

    python setup_ollama_model.py

Run this once per server, before the first render. It is deliberately
standalone - stdlib only, no imports from zlendo_llm, no third-party packages -
so it works on a bare box before `pip install -r requirements.txt` has run.

WHY THIS EXISTS
---------------
The render pipeline auto-pulls a missing model (ZLENDO_AI_AUTO_PULL, default
on), but it can only do that once the Ollama SERVER is reachable: ensure_model()
checks `is_up()` first and gives up there. On a fresh server the log reads

    "reason": "Ollama unreachable at http://127.0.0.1:11434"

which is not a download failure - nothing was ever asked to download. Ollama was
not installed or not running. A render job is the wrong place to install a
service, so that stays a deliberate, separate step: this script.

WHAT IT DOES
    1. Reads ZLENDO_AI_MODEL / ZLENDO_AI_HOST from .env (real values, not the
       code defaults - the point is to match what the renderer will ask for).
    2. Checks Ollama is installed, and starts `ollama serve` if it is not up.
    3. Pulls the model, with live progress.
    4. Verifies it is listed AND that it actually answers a prompt - a model can
       be present and still fail to load on a card with no free VRAM.
    5. Unloads it, so the path tracer gets the card back.

EXIT CODES
    0  ready to render
    1  Ollama not installed         - install it, then re-run
    2  Ollama not reachable         - start it, then re-run
    3  pull failed                  - check the tag and the network
    4  installed but will not run   - usually VRAM; see the message
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_HOST = "http://127.0.0.1:11434"

# Where the Windows installer puts ollama.exe when it is not on PATH. A fresh
# install very often is not on PATH until the user opens a new shell, and
# "ollama is not installed" is a confusing thing to be told when it plainly is.
_WINDOWS_CANDIDATES = (
    r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe",
    r"%PROGRAMFILES%\Ollama\ollama.exe",
    r"%USERPROFILE%\AppData\Local\Programs\Ollama\ollama.exe",
)
_POSIX_CANDIDATES = (
    "/usr/local/bin/ollama",
    "/usr/bin/ollama",
    "/opt/homebrew/bin/ollama",
)


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------
def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: str, msg: str) -> None:
    say(f"\n[{n}] {msg}")


def fail(msg: str) -> None:
    say(f"  FAILED: {msg}")


# ----------------------------------------------------------------------
# .env
# ----------------------------------------------------------------------
def read_env_file(path: Path) -> Dict[str, str]:
    """Minimal .env reader - no python-dotenv dependency.

    Handles `KEY=value`, `export KEY=value`, quotes, inline `#` comments and
    blank lines. That is the whole grammar this project's .env uses.
    """
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        say(f"  ! could not read {path}: {exc}")
        return out

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if value[:1] in ("'", '"'):
            # Quoted: the value ends at the closing quote. Anything after it is
            # a comment. Without this, ZLENDO_AI_MODEL="qwen3:8b"  # note
            # yields the literal '"qwen3:8b"  # note' as the model tag.
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            # Unquoted: only ` #` (hash preceded by whitespace) starts a comment.
            # Splitting on a bare '#' would truncate a DATABASE_URL whose
            # password or fragment legitimately contains one.
            for marker in (" #", "\t#"):
                if marker in value:
                    value = value.split(marker, 1)[0].strip()
            if value.startswith("#"):
                value = ""

        if key:
            out[key] = value
    return out


def resolve_settings(args: argparse.Namespace) -> Tuple[str, str, str]:
    """Model and host, with the same precedence the renderer uses.

    Command line beats the real process environment, which beats .env, which
    beats the built-in default. Returns (model, host, where_the_model_came_from).
    """
    env_file = read_env_file(ENV_PATH)

    if args.model:
        return args.model, _resolve_host(args, env_file), "--model argument"
    if os.getenv("ZLENDO_AI_MODEL"):
        return (os.environ["ZLENDO_AI_MODEL"].strip(),
                _resolve_host(args, env_file), "ZLENDO_AI_MODEL in the environment")
    if env_file.get("ZLENDO_AI_MODEL"):
        return (env_file["ZLENDO_AI_MODEL"].strip(),
                _resolve_host(args, env_file), f"ZLENDO_AI_MODEL in {ENV_PATH.name}")
    return DEFAULT_MODEL, _resolve_host(args, env_file), "built-in default"


def _resolve_host(args: argparse.Namespace, env_file: Dict[str, str]) -> str:
    # ZLENDO_AI_HOST is what the render pipeline reads. OLLAMA_HOST is what
    # Ollama's own tooling reads, and zlendo_llm/setup_ai.py reads it too - so
    # honour both here rather than silently checking a different server from
    # the one the renderer will call.
    for source in (args.host,
                   os.getenv("ZLENDO_AI_HOST"),
                   env_file.get("ZLENDO_AI_HOST"),
                   os.getenv("OLLAMA_HOST"),
                   env_file.get("OLLAMA_HOST")):
        if source and str(source).strip():
            return str(source).strip().rstrip("/")
    return DEFAULT_HOST


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
def http_json(url: str, payload: Optional[dict] = None,
              timeout: float = 10.0) -> Optional[dict]:
    """GET or POST returning parsed JSON, or None on any failure."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def server_up(host: str) -> bool:
    return http_json(f"{host}/api/tags", timeout=5.0) is not None


def installed_models(host: str) -> List[str]:
    body = http_json(f"{host}/api/tags", timeout=15.0) or {}
    return [m.get("name", "") for m in body.get("models", []) if m.get("name")]


def model_matches(wanted: str, installed: List[str]) -> bool:
    """Ollama reports 'qwen3:8b'; a bare 'qwen3' should still match.

    Same rule as OllamaClient.ensure_model(), so this script and the renderer
    never disagree about whether a model is present.
    """
    return any(n == wanted or n.startswith(wanted + ":") for n in installed)


# ----------------------------------------------------------------------
# Ollama binary / service
# ----------------------------------------------------------------------
def find_ollama() -> Optional[str]:
    exe = shutil.which("ollama")
    if exe:
        return exe
    candidates = (_WINDOWS_CANDIDATES if platform.system() == "Windows"
                  else _POSIX_CANDIDATES)
    for raw in candidates:
        path = os.path.expandvars(raw)
        if "%" not in path and Path(path).is_file():
            return path
    return None


def start_server(exe: str, host: str, wait_seconds: int = 30) -> bool:
    """Launch `ollama serve` detached and wait for it to answer."""
    say(f"  starting: {exe} serve")
    try:
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if platform.system() == "Windows":
            # Detach so the server outlives this script.
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([exe, "serve"], **kwargs)
    except OSError as exc:
        fail(f"could not launch it: {exc}")
        return False

    for i in range(wait_seconds):
        time.sleep(1)
        if server_up(host):
            say(f"  up after {i + 1}s")
            return True
    return False


def install_hint() -> str:
    system = platform.system()
    if system == "Windows":
        return ("  Install it from https://ollama.com/download/windows\n"
                "  then open a NEW terminal (the installer adds it to PATH) "
                "and re-run this script.")
    if system == "Darwin":
        return "  Install it with:  brew install ollama\n  or from https://ollama.com/download"
    return ("  Install it with:  curl -fsSL https://ollama.com/install.sh | sh\n"
            "  or see https://ollama.com/download")


# ----------------------------------------------------------------------
# Pull
# ----------------------------------------------------------------------
def pull_via_cli(exe: str, model: str) -> bool:
    """Preferred: `ollama pull` renders its own progress bar."""
    say(f"  running: {exe} pull {model}")
    say("  (multi-GB download; leave this running)\n")
    try:
        return subprocess.run([exe, "pull", model]).returncode == 0
    except OSError as exc:
        fail(f"could not run the CLI: {exc}")
        return False


def pull_via_http(host: str, model: str) -> bool:
    """Fallback when only the server is reachable (remote host, no local CLI)."""
    say(f"  POST {host}/api/pull   model={model}")
    req = urllib.request.Request(
        f"{host}/api/pull",
        data=json.dumps({"model": model, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    last_pct = -1
    try:
        # No timeout: a large pull legitimately takes many minutes.
        with urllib.request.urlopen(req) as resp:
            for raw in resp:
                if not raw.strip():
                    continue
                try:
                    evt = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                # Ollama streams {"error": ...} with HTTP 200, so a 200 is not
                # proof of success.
                if evt.get("error"):
                    fail(str(evt["error"]))
                    return False
                total = evt.get("total") or 0
                done = evt.get("completed") or 0
                if total:
                    pct = int(done * 100 / total)
                    if pct >= last_pct + 5 or pct == 100:
                        last_pct = pct
                        say(f"  {evt.get('status', 'downloading')}: {pct}% "
                            f"({done / 1e9:.2f}/{total / 1e9:.2f} GB)")
                elif evt.get("status"):
                    say(f"  {evt['status']}")
        return True
    except (urllib.error.URLError, OSError) as exc:
        fail(f"{exc}")
        return False


# ----------------------------------------------------------------------
# Verify
# ----------------------------------------------------------------------
def smoke_test(host: str, model: str) -> bool:
    """Ask the model one trivial question.

    Being listed by /api/tags only means the weights are on disk. Loading is
    where a busy or too-small GPU actually fails, and finding that out here is
    much cheaper than finding it out mid-render.
    """
    say("  sending a one-token prompt (first load can take ~30s)...")
    body = http_json(
        f"{host}/api/chat",
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the word OK."}],
            "stream": False,
            "options": {"num_predict": 16, "temperature": 0},
        },
        timeout=300.0,
    )
    if not body:
        return False
    reply = (body.get("message") or {}).get("content", "")
    say(f"  model replied: {reply.strip()[:80]!r}")
    return True


def unload(host: str, model: str) -> None:
    """Free the VRAM again - the path tracer needs the whole card."""
    http_json(f"{host}/api/generate",
              {"model": model, "prompt": "", "keep_alive": 0}, timeout=30.0)


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download and verify the Ollama model named in .env.")
    ap.add_argument("--model", default=None,
                    help="Override the model tag (default: ZLENDO_AI_MODEL from .env).")
    ap.add_argument("--host", default=None,
                    help="Override the Ollama host (default: ZLENDO_AI_HOST from .env).")
    ap.add_argument("--no-start", action="store_true",
                    help="Do not try to start `ollama serve`; just report it is down.")
    ap.add_argument("--force", action="store_true",
                    help="Pull again even if the model is already installed.")
    ap.add_argument("--skip-verify", action="store_true",
                    help="Skip the inference smoke test (only checks it is listed).")
    args = ap.parse_args()

    model, host, origin = resolve_settings(args)

    say("=" * 68)
    say("  Ollama model setup - 4k PT Engine")
    say("=" * 68)
    say(f"  model : {model}   (from {origin})")
    say(f"  host  : {host}")
    say(f"  .env  : {ENV_PATH}{'' if ENV_PATH.is_file() else '   [NOT FOUND]'}")

    # 1. Binary
    step("1/4", "Locating Ollama")
    exe = find_ollama()
    if exe:
        say(f"  found: {exe}")
    else:
        say("  not found on PATH or in the usual install locations.")

    # 2. Server
    step("2/4", "Checking the server")
    if server_up(host):
        say(f"  reachable at {host}")
    elif not exe:
        fail(f"nothing listening at {host}, and the ollama binary is missing.")
        say(install_hint())
        say("\n  If Ollama runs on ANOTHER machine, set ZLENDO_AI_HOST in .env "
            "to point at it\n  and re-run this script.")
        return 1
    elif args.no_start:
        fail(f"nothing listening at {host} and --no-start was passed.")
        return 2
    else:
        say(f"  nothing listening at {host} - starting it.")
        if not start_server(exe, host):
            fail("the server did not come up in 30s.")
            say(f"  Try it by hand in another terminal:  {exe} serve")
            say("  then re-run this script.")
            return 2

    # 3. Pull
    step("3/4", f"Ensuring '{model}' is installed")
    present = model_matches(model, installed_models(host))
    if present and not args.force:
        say(f"  already installed. (--force to re-pull)")
    else:
        if present:
            say("  installed, but --force was passed; pulling again.")
        else:
            say(f"  not installed. Installed models: "
                f"{installed_models(host) or '(none)'}")
        ok = pull_via_cli(exe, model) if exe else pull_via_http(host, model)
        if not ok:
            fail(f"could not pull '{model}'.")
            say("  Check the tag exists at https://ollama.com/library and that "
                "this machine\n  can reach the internet.")
            return 3
        # Confirm rather than trust the exit code.
        if not model_matches(model, installed_models(host)):
            fail(f"the pull reported success but '{model}' is still not listed.")
            return 3
        say(f"  '{model}' installed.")

    # 4. Verify
    step("4/4", "Verifying the model runs")
    if args.skip_verify:
        say("  skipped (--skip-verify).")
    elif smoke_test(host, model):
        say("  OK.")
        unload(host, model)
        say("  unloaded; VRAM released for the path tracer.")
    else:
        fail("the model is installed but did not answer.")
        say("  Most often this is VRAM - something else is holding the GPU.")
        say("  Check with:  nvidia-smi     and:  ollama ps")
        return 4

    say("\n" + "=" * 68)
    say("  Ready.")
    say("=" * 68)
    say(f"  The renderer will use: {model}")
    say("  Make sure .env has:")
    say(f"    ZLENDO_AI_MODEL={model}")
    say("    USE_LLM=True")
    say("    AUTOMATION_LLM_ADJUSTMENT=True")
    if host != DEFAULT_HOST:
        say(f"    ZLENDO_AI_HOST={host}")
    say("")
    say("  Note: Ollama must be RUNNING whenever you render. If this server")
    say("  reboots, start it again (or install it as a service) - otherwise")
    say("  the render log will say 'Ollama unreachable' and silently skip the")
    say("  AI passes.")
    say("=" * 68)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nInterrupted.")
        sys.exit(130)

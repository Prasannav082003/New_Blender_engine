"""
Ollama client tuned for the 4k_PT_Engine pipeline.

The single most important thing this module does beyond "call an LLM" is
VRAM lifecycle management. The path tracer needs the full 16GB of the
A4000 to render at 4K. If Ollama is still holding weights when Godot
starts the render, you get either an OOM or a catastrophic slowdown as
the driver pages textures to host memory.

So: every call goes out with an explicit keep_alive, and `unload()` must
be called before handing control to Godot.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import subprocess

import requests

from .config import ai_model, auto_pull_enabled

log = logging.getLogger(__name__)


def _query_gpu_status() -> Optional[Dict[str, Any]]:
    """Query nvidia-smi for GPU utilization and VRAM usage."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        parts = result.stdout.strip().split(", ")
        if len(parts) >= 5:
            return {
                "gpu_name": parts[0],
                "gpu_util_pct": int(parts[1]),
                "vram_used_mb": int(parts[2]),
                "vram_total_mb": int(parts[3]),
                "temp_c": int(parts[4]),
            }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def print_gpu_status(label: str = "GPU") -> None:
    """Print a one-line GPU status to the console."""
    info = _query_gpu_status()
    if info:
        print(
            f"[{label}] {info['gpu_name']} | "
            f"GPU util: {info['gpu_util_pct']}% | "
            f"VRAM: {info['vram_used_mb']}MB / {info['vram_total_mb']}MB | "
            f"Temp: {info['temp_c']}C"
        )
    else:
        print(f"[{label}] nvidia-smi unavailable")

# Strips the reasoning block that Thinking-series models emit when the
# Ollama build is older than the native `thinking` response field.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class OllamaError(RuntimeError):
    pass


class _TruncatedThink(OllamaError):
    """
    The model hit its token ceiling before closing <think>, so the strip left
    nothing. Separate from a genuinely empty reply because the fix is opposite:
    a truncated reply needs a BIGGER budget, an empty one needs a retry.
    """


def _truncated_think(raw: str, body: Dict[str, Any]) -> bool:
    """
    True when the reply was cut off mid-reasoning.

    Two independent signals, either of which is enough:
      * an opening <think> with no closing tag - unambiguous
      * Ollama reporting done_reason "length" with a non-empty reply, i.e. it
        stopped because it ran out of budget rather than because it finished
    """
    text = raw or ""
    lowered = text.lower()
    if "<think>" in lowered and "</think>" not in lowered:
        return True
    return bool(text.strip()) and str(body.get("done_reason", "")).lower() == "length"


@dataclass
class OllamaConfig:
    host: str = "http://127.0.0.1:11434"

    # Resolved from ZLENDO_AI_MODEL in .env at construction time, falling back
    # to config.DEFAULT_AI_MODEL. It is a default_factory rather than a literal
    # so that a caller who constructs OllamaConfig WITHOUT naming a model still
    # gets the operator's choice - pipeline.py does exactly that, and used to
    # silently run a different model from the two render passes as a result.
    #
    # If you want the MoE, set in .env:
    #     ZLENDO_AI_MODEL=qwen3:30b-a3b-thinking-2507-q4_K_M
    # It is 19GB and WILL partially offload to system RAM on a 16GB card.
    # Because only ~3B params are active per token the offload penalty is
    # far smaller than a dense 30B would suffer, but the Thinking variant
    # also emits long reasoning traces, so budget 60-180s per call.
    model: str = field(default_factory=ai_model)

    # Context window. The lighting report is compact (~1-3KB) so 16K is
    # generous. Raising this costs VRAM linearly - keep it tight.
    num_ctx: int = 16384

    # Near-deterministic. We want reproducible lighting decisions, not
    # creative variety. Do not raise this above ~0.3.
    temperature: float = 0.15
    top_p: float = 0.9
    top_k: int = 20
    seed: int = 42

    # How long Ollama keeps weights resident after a call. "0" unloads
    # immediately. During a multi-call agent loop we keep it warm, then
    # explicitly unload before the render.
    keep_alive_warm: str = "10m"

    request_timeout: int = 900
    max_retries: int = 3

    # Set False for non-Thinking models; harmless either way on builds
    # that do not support the flag.
    enable_thinking: bool = False

    # Output token ceiling for a structured call.
    #
    # This was 2048 and it silently broke multi-part prompts. Qwen3 emits a
    # <think> block even when told not to; a request like "red wall, new sofa
    # fabric, blue ceiling" reasons longer than a single-clause one, spends the
    # whole budget inside <think>, and the strip leaves an EMPTY string - which
    # reads as "the model said nothing" rather than "the model was cut off".
    # 4096 covers the observed worst case with headroom, and num_predict_max
    # lets a truncated attempt retry with MORE room instead of repeating the
    # identical failing request.
    num_predict: int = 4096
    num_predict_max: int = 16384

    extra_options: Dict[str, Any] = field(default_factory=dict)


class OllamaClient:
    def __init__(self, config: Optional[OllamaConfig] = None):
        self.cfg = config or OllamaConfig()
        self._session = requests.Session()
        # Running token count for this client's lifetime.
        #
        # Ollama reports prompt_eval_count / eval_count on every response and
        # we were printing them to the console and then throwing them away.
        # "How much does this model cost us per render?" was therefore only
        # answerable by scrolling a terminal buffer that had already rolled
        # over. Accumulating here lets the per-job log carry the real figures
        # instead of an estimate.
        self.usage: Dict[str, Any] = {
            "calls": 0,
            "failed_calls": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "seconds": 0.0,
        }

    def _record_usage(self, body: Dict[str, Any], elapsed: float) -> None:
        try:
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += int(body.get("prompt_eval_count") or 0)
            self.usage["output_tokens"] += int(body.get("eval_count") or 0)
            self.usage["seconds"] = round(self.usage["seconds"] + elapsed, 1)
        except Exception:  # noqa: BLE001 - accounting must never break a call
            pass

    def usage_summary(self) -> Dict[str, Any]:
        """Token totals for this client, safe to drop straight into a log."""
        u = dict(self.usage)
        u["total_tokens"] = u["prompt_tokens"] + u["output_tokens"]
        u["model"] = self.cfg.model
        if u["seconds"] > 0 and u["output_tokens"]:
            u["output_tokens_per_sec"] = round(u["output_tokens"] / u["seconds"], 1)
        return u

    # ------------------------------------------------------------------
    # Health / lifecycle
    # ------------------------------------------------------------------
    def is_up(self) -> bool:
        try:
            r = self._session.get(f"{self.cfg.host}/api/tags", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def available_models(self) -> List[str]:
        r = self._session.get(f"{self.cfg.host}/api/tags", timeout=15)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def ensure_model(self, pull_if_missing: Optional[bool] = None) -> None:
        """Verify the configured model is present, downloading it if not.

        `pull_if_missing=None` (the default) defers to ZLENDO_AI_AUTO_PULL in
        .env, which itself defaults to True. Pass True/False to override.

        This used to default to False at every call site, so a .env naming a
        model nobody had pulled produced a render that quietly skipped the AI
        passes. Setting ZLENDO_AI_MODEL is now taken as an instruction to make
        that model available, not merely to request it.
        """
        if pull_if_missing is None:
            pull_if_missing = auto_pull_enabled()

        if not self.is_up():
            raise OllamaError(
                f"Ollama is not reachable at {self.cfg.host}. "
                "Start it with `ollama serve` (or the Windows tray app)."
            )

        names = self.available_models()
        # Ollama reports "qwen3:8b"; a bare "qwen3" config should still match.
        wanted = self.cfg.model
        if any(n == wanted or n.startswith(wanted + ":") for n in names):
            print(f"[AI-LLM] Model '{wanted}' found in Ollama.")
            print_gpu_status("AI-INIT")
            return

        if not pull_if_missing:
            raise OllamaError(
                f"Model '{wanted}' not found. Installed: {names or '(none)'}. "
                f"Run:  ollama pull {wanted}  "
                f"(or set ZLENDO_AI_AUTO_PULL=True to download it automatically)"
            )

        self.pull_model(wanted, installed=names)

    def pull_model(self, wanted: str, installed: Optional[List[str]] = None) -> None:
        """Download a model, reporting progress to the console.

        Progress matters more than it looks. This is a multi-GB download inside
        what the operator experiences as "a render job", and the previous
        implementation reported only via log.info - invisible under the default
        logging config. A ten-minute silent gap in the render log is
        indistinguishable from a hang, and gets killed.
        """
        print(f"[AI-LLM] Model '{wanted}' not installed"
              f"{f' (have: {installed})' if installed else ''}.")
        print("[AI-LLM] Downloading it now - this is a multi-GB, one-time download.")
        print(f"[AI-LLM] Set ZLENDO_AI_AUTO_PULL=False in .env to disable this.")
        log.warning("Pulling %s - this is a multi-GB download.", wanted)

        t0 = time.time()
        last_pct = -1
        last_status = ""
        try:
            with self._session.post(
                f"{self.cfg.host}/api/pull",
                json={"model": wanted, "stream": True},
                stream=True,
                timeout=None,   # a large pull legitimately takes many minutes
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue

                    # Ollama streams {"error": "..."} instead of an HTTP error
                    # for things like an unknown model name, so a 200 response
                    # is NOT proof the pull worked.
                    if evt.get("error"):
                        raise OllamaError(
                            f"Pull of '{wanted}' failed: {evt['error']}. "
                            f"Check the tag exists (https://ollama.com/library)."
                        )

                    status = str(evt.get("status", ""))
                    total = evt.get("total") or 0
                    completed = evt.get("completed") or 0

                    if total:
                        pct = int(completed * 100 / total)
                        # Throttled to every 5% so a long pull leaves a readable
                        # trail in the render log instead of thousands of lines.
                        if pct >= last_pct + 5 or pct == 100:
                            last_pct = pct
                            print(f"[AI-LLM] pull {status}: {pct}% "
                                  f"({completed / 1e9:.2f}/{total / 1e9:.2f} GB)")
                    elif status and status != last_status:
                        last_status = status
                        print(f"[AI-LLM] pull: {status}")
                    log.info("pull: %s", status)
        except OllamaError:
            raise
        except requests.RequestException as exc:
            raise OllamaError(
                f"Pull of '{wanted}' failed: {exc}. "
                f"Check network access to the Ollama registry, or pre-pull the "
                f"model and set ZLENDO_AI_AUTO_PULL=False."
            ) from exc

        # Confirm rather than assume - a pull that streamed to completion but
        # left nothing installed must not be reported as success.
        names_after = self.available_models()
        if not any(n == wanted or n.startswith(wanted + ":") for n in names_after):
            raise OllamaError(
                f"Pull of '{wanted}' finished but the model is still not listed. "
                f"Installed: {names_after or '(none)'}."
            )

        print(f"[AI-LLM] Model '{wanted}' downloaded in {time.time() - t0:.0f}s.")
        print_gpu_status("AI-INIT")

    def warmup(self) -> None:
        """
        Force-load the model into VRAM by sending a trivial prompt.

        Ollama lazy-loads: /api/tags shows the model is available but the
        weights are not in VRAM until the first inference. This tiny call
        triggers the load so we can check GPU vs CPU placement BEFORE the
        real (expensive) inference.
        """
        try:
            r = self._session.post(
                f"{self.cfg.host}/api/generate",
                json={
                    "model": self.cfg.model,
                    "prompt": "hi",
                    "options": {"num_predict": 1},
                    "keep_alive": self.cfg.keep_alive_warm,
                    "stream": False,
                },
                timeout=120,
            )
            if r.status_code >= 400:
                log.warning("Warmup got HTTP %d - model may not be GPU-loaded.", r.status_code)
            else:
                log.info("Warmup complete - model loaded.")
        except requests.RequestException as exc:
            log.warning("Warmup request failed: %s", exc)

    def unload(self) -> None:
        """
        Evict the model from VRAM. Call this before launching the Godot
        render. This is not optional on a 16GB card.
        """
        try:
            self._session.post(
                f"{self.cfg.host}/api/generate",
                json={"model": self.cfg.model, "prompt": "", "keep_alive": 0},
                timeout=60,
            )
            log.info("Unloaded %s from VRAM.", self.cfg.model)
        except requests.RequestException as exc:
            # Never let a failed unload kill the pipeline - worst case the
            # render is slower.
            log.warning("Could not unload model (continuing): %s", exc)

    def loaded_models(self) -> List[Dict[str, Any]]:
        """What Ollama currently holds in memory, with VRAM sizes."""
        try:
            r = self._session.get(f"{self.cfg.host}/api/ps", timeout=10)
            r.raise_for_status()
            return r.json().get("models", [])
        except requests.RequestException:
            return []

    def check_gpu_loading(self) -> None:
        """
        Print whether the model is loaded on GPU or CPU.
        Ollama /api/ps reports size_vram for each loaded model.
        If size_vram is 0 or much smaller than size, it's on CPU.
        """
        models = self.loaded_models()
        if not models:
            print(f"[AI-GPU] No models currently loaded in Ollama.")
            return
        for m in models:
            name = m.get("name", "?")
            size = m.get("size", 0)
            size_vram = m.get("size_vram", 0)
            size_mb = size / (1024 * 1024)
            vram_mb = size_vram / (1024 * 1024)
            if size > 0 and size_vram > 0:
                gpu_pct = (size_vram / size) * 100
                if gpu_pct > 90:
                    print(f"[AI-GPU] {name}: FULLY on GPU ({vram_mb:.0f}MB VRAM)")
                elif gpu_pct > 10:
                    print(f"[AI-GPU] {name}: PARTIAL GPU ({gpu_pct:.0f}% on GPU, "
                          f"{vram_mb:.0f}MB VRAM / {size_mb:.0f}MB total)")
                else:
                    print(f"[AI-GPU] WARNING: {name}: mostly on CPU! "
                          f"Only {vram_mb:.0f}MB of {size_mb:.0f}MB on GPU")
            elif size > 0 and size_vram == 0:
                print(f"[AI-GPU] WARNING: {name}: running ENTIRELY on CPU! "
                      f"({size_mb:.0f}MB, 0MB VRAM)")
                print(f"[AI-GPU] This will be very slow. Check that Ollama "
                      f"detects your GPU: ollama ps")
            else:
                print(f"[AI-GPU] {name}: loaded ({size_mb:.0f}MB)")

    # ------------------------------------------------------------------
    def _http_error(self, response) -> str:
        """
        Turn an Ollama HTTP error into something actionable.

        The bare requests message ("404 Client Error: Not Found for url
        ...") is actively misleading here, because Ollama uses 404 for a
        missing MODEL as well as a missing route.
        """
        try:
            detail = response.json().get("error", "")
        except Exception:
            detail = (response.text or "").strip()[:400]

        msg = f"HTTP {response.status_code} from /api/chat"
        if detail:
            msg += f": {detail}"

        if response.status_code == 404:
            installed = []
            try:
                installed = self.available_models()
            except Exception:
                pass
            msg += (
                f"\n  Ollama returns 404 when the MODEL does not exist, "
                f"not only for bad URLs."
                f"\n  Configured model : '{self.cfg.model}'"
                f"\n  Installed models : {installed or '(could not list)'}"
            )
            if installed and self.cfg.model not in installed:
                msg += (
                    f"\n  -> '{self.cfg.model}' is NOT installed. Either set "
                    f"ZLENDO_AI_MODEL to one of the above, or run: "
                    f"ollama pull {self.cfg.model}"
                )
        return msg

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------
    def chat_json(
        self,
        system: str,
        user: str,
        schema: Dict[str, Any],
        keep_alive: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        One structured-output call. `schema` is a JSON Schema dict; Ollama
        constrains generation against it, so the result parses or we raise.
        """
        options = {
            "temperature": self.cfg.temperature,
            "top_p": self.cfg.top_p,
            "top_k": self.cfg.top_k,
            "seed": self.cfg.seed,
            "num_ctx": self.cfg.num_ctx,
            **self.cfg.extra_options,
        }

        # Qwen3 models generate <think>...</think> reasoning by default,
        # which can produce thousands of hidden tokens (3-4 min of waste).
        if not self.cfg.enable_thinking:
            options["num_predict"] = self.cfg.num_predict

        # Qwen3 respects /no_think to skip its internal reasoning chain.
        # This is a REQUEST, not a guarantee - the model still emits a short
        # <think> block often enough that the budget has to allow for one.
        if not self.cfg.enable_thinking and "qwen3" in self.cfg.model.lower():
            user = user + "\n\n/no_think"

        payload: Dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema,          # <- structured outputs
            "stream": False,
            "options": options,
            "keep_alive": keep_alive or self.cfg.keep_alive_warm,
        }
        # The native flag is far more reliable than the /no_think string above,
        # which the model is free to ignore. Ollama builds that predate it just
        # ignore the field, so sending it costs nothing.
        payload["think"] = bool(self.cfg.enable_thinking)

        last_err: Optional[Exception] = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                print(f"[AI-LLM] Sending request to {self.cfg.model} (attempt {attempt}/{self.cfg.max_retries})...")
                print_gpu_status("AI-LLM-PRE")

                t0 = time.time()
                r = self._session.post(
                    f"{self.cfg.host}/api/chat",
                    json=payload,
                    timeout=self.cfg.request_timeout,
                )

                elapsed = time.time() - t0

                # Ollama puts the actual reason in the RESPONSE BODY and
                # raise_for_status() throws it away. A 404 here almost
                # always means "model not found", not a bad URL - without
                # the body that is indistinguishable from a broken route.
                if r.status_code >= 400:
                    raise OllamaError(self._http_error(r))

                body = r.json()
                raw = (body.get("message") or {}).get("content", "")
                content = _THINK_RE.sub("", raw).strip()

                # Truncation is checked BEFORE emptiness and before parsing,
                # because it shows up in three different disguises depending on
                # where the cut landed:
                #   * think block closed, nothing after  -> content is empty
                #   * think block never closed           -> content is prose,
                #     non-empty, and fails to parse as JSON
                #   * budget hit mid-JSON                -> truncated JSON
                # All three need a BIGGER budget; only the last two look like
                # anything other than "empty", which is why the original code
                # retried the identical request and got the identical failure.
                if _truncated_think(raw, body):
                    raise _TruncatedThink(
                        f"model hit its {options.get('num_predict')}-token budget "
                        f"before finishing the JSON "
                        f"(done_reason={body.get('done_reason')!r})"
                    )

                if not content:
                    raise OllamaError("Model returned an empty response.")

                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    # Unparseable output that still carries an opening <think>
                    # is reasoning that never terminated, not malformed JSON.
                    if "<think>" in content.lower():
                        raise _TruncatedThink(
                            f"model was still reasoning when its "
                            f"{options.get('num_predict')}-token budget ran out"
                        ) from None
                    raise

                # Print performance stats so user can see LLM is working
                eval_count = body.get("eval_count", 0)
                prompt_eval_count = body.get("prompt_eval_count", 0)
                eval_duration = body.get("eval_duration", 0) / 1e9  # nanoseconds to seconds
                prompt_eval_duration = body.get("prompt_eval_duration", 0) / 1e9
                tok_per_sec = eval_count / eval_duration if eval_duration > 0 else 0

                print(f"[AI-LLM] Inference complete in {elapsed:.1f}s")
                print(f"[AI-LLM]   Prompt: {prompt_eval_count} tokens ({prompt_eval_duration:.1f}s)")
                print(f"[AI-LLM]   Output: {eval_count} tokens ({eval_duration:.1f}s, {tok_per_sec:.1f} tok/s)")
                print_gpu_status("AI-LLM-POST")
                self.check_gpu_loading()

                log.info(
                    "LLM ok in %.1fs (eval %s tok, %.1f tok/s)",
                    elapsed, eval_count, tok_per_sec,
                )
                self._record_usage(body, elapsed)
                return parsed

            except _TruncatedThink as exc:
                # A truncated attempt still consumed a full generation budget.
                # Leaving it out of the tally would understate the real cost of
                # exactly the case that is most expensive.
                self.usage["failed_calls"] += 1
                self.usage["output_tokens"] += int(options.get("num_predict") or 0)
                # Retrying the identical request cannot help - the model will
                # think for just as long again. Double the budget instead, and
                # tell the model in-band to stop deliberating.
                last_err = exc
                current = int(options.get("num_predict") or self.cfg.num_predict)
                grown = min(current * 2, self.cfg.num_predict_max)
                log.warning("LLM attempt %d/%d truncated: %s", attempt,
                            self.cfg.max_retries, exc)
                print(f"[AI-LLM] Output truncated inside <think>; "
                      f"raising num_predict {current} -> {grown} and retrying.")
                if grown <= current:
                    # Already at the ceiling. More attempts are wasted GPU time.
                    break
                options["num_predict"] = grown
                payload["options"] = options
                payload["messages"][-1]["content"] = (
                    user + "\n\nAnswer with the JSON object only. Do not deliberate."
                )
                if attempt < self.cfg.max_retries:
                    time.sleep(2 * attempt)

            except (requests.RequestException, json.JSONDecodeError, OllamaError) as exc:
                last_err = exc
                log.warning("LLM attempt %d/%d failed: %s",
                            attempt, self.cfg.max_retries, exc)
                if attempt < self.cfg.max_retries:
                    time.sleep(2 * attempt)

        raise OllamaError(f"All {self.cfg.max_retries} attempts failed: {last_err}")

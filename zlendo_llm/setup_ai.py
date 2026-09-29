"""
One-command setup: detect the GPU, install Ollama if missing, pick a
quantization that actually fits, pull it, verify it works.

    python -m zlendo_llm.setup_ai

An honest note on "quantize automatically", because the word gets used
loosely: this does NOT convert an FP16 checkpoint to a quantized one on
your machine. Doing that for Qwen3-30B would mean downloading ~61GB and
running llama.cpp's converter for an hour, to arrive at a file Ollama
already hosts. What this DOES is measure your VRAM and select the correct
pre-quantized GGUF build, which is the part that actually matters.

If you genuinely want a custom quantization (say Q5_K_M, which nobody
publishes for this model), pass --custom-quant and it will do the real
llama.cpp conversion. Expect a long download and a long conversion.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger("zlendo.setup")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")


@dataclass
class ModelChoice:
    tag: str
    size_gb: float
    note: str


# Ordered best-first. `needs_gb` is the VRAM required to hold the whole
# model plus a working KV cache; below that it still runs but offloads to
# system RAM.
CANDIDATES: List[tuple] = [
    # (needs_gb, ModelChoice)
    (34.0, ModelChoice("qwen3:30b-a3b-thinking-2507-q8_0", 32.0,
                       "MoE 30B, 8-bit. Best quality if you have the VRAM.")),
    (22.0, ModelChoice("qwen3:30b-a3b-thinking-2507-q4_K_M", 19.0,
                       "MoE 30B thinking, 4-bit. Strong reasoning.")),
    (19.0, ModelChoice("qwen3:30b-a3b-instruct-2507-q4_K_M", 19.0,
                       "MoE 30B instruct, 4-bit. Faster than thinking.")),
    (12.0, ModelChoice("qwen3:14b", 9.3,
                       "Dense 14B. Fits comfortably in 16GB with KV cache.")),
    (7.0, ModelChoice("qwen3:8b", 5.2,
                      "Dense 8B. Leaves most of the GPU free.")),
    (0.0, ModelChoice("qwen3:4b", 2.6,
                      "Dense 4B. CPU-friendly fallback.")),
]


# ----------------------------------------------------------------------
# GPU detection
# ----------------------------------------------------------------------
def detect_vram_gb() -> Optional[float]:
    """Total VRAM of the largest CUDA device, in GB."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30,
        )
        vals = [float(v.strip()) for v in out.stdout.split("\n") if v.strip()]
        return max(vals) / 1024.0 if vals else None
    except (subprocess.SubprocessError, ValueError):
        return None


def detect_gpu_name() -> Optional[str]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30,
        )
        line = out.stdout.strip().split("\n")[0].strip()
        return line or None
    except subprocess.SubprocessError:
        return None


def detect_compute_cap() -> Optional[float]:
    """CUDA compute capability of the first device, e.g. 8.6 for an A4000."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30,
        )
        line = out.stdout.strip().split("\n")[0].strip()
        return float(line) if line else None
    except (subprocess.SubprocessError, ValueError):
        return None


def supports_fp8(gpu_name: Optional[str] = None,
                 compute_cap: Optional[float] = None) -> bool:
    """
    FP8 tensor cores require compute capability >= 8.9 (Ada) or >= 9.0
    (Hopper/Blackwell). Ampere - A4000, A5000, A6000, 3090, A100 - does not
    have them, regardless of how much VRAM it has.

    Compute capability is the authoritative signal, so use it when
    available. The name fallback exists only for when nvidia-smi cannot
    report compute_cap, and it matches on WHOLE tokens: substring matching
    is a trap here, because "A4000" contains "40" and "A100-SXM4-40GB"
    contains both "40" and "100".
    """
    if compute_cap is not None:
        return compute_cap >= 8.9

    if not gpu_name:
        return False

    import re

    n = gpu_name.lower()

    # Explicitly Ampere and older - checked FIRST so an "A4000" can never
    # fall through to a looser pattern below.
    ampere_or_older = re.search(
        r"\b(a\d{3,4}|rtx\s*[23]\d{3}|titan|quadro\s*rtx|tesla|v100|p100|t4)\b", n
    )
    if ampere_or_older and "ada" not in n:
        return False

    fp8_capable = re.search(
        r"\b(rtx\s*(40|50)\d{2}|l4|l40s?|h100|h200|h800|gh200|b100|b200|gb200)\b", n
    ) or "ada" in n or "blackwell" in n or "rtx pro" in n
    return bool(fp8_capable)


def choose_model(vram_gb: Optional[float], reserve_gb: float = 4.0) -> ModelChoice:
    """
    Pick the best model that fits.

    `reserve_gb` is held back for the path tracer. Even though we unload
    the model before rendering, leaving headroom avoids fragmentation and
    lets a preview render coexist with the agent.
    """
    if vram_gb is None:
        log.warning("No CUDA GPU detected - selecting a CPU-friendly model.")
        return CANDIDATES[-1][1]

    usable = vram_gb - reserve_gb
    for needs, choice in CANDIDATES:
        if usable >= needs:
            return choice
    return CANDIDATES[-1][1]


# ----------------------------------------------------------------------
# Ollama install / control
# ----------------------------------------------------------------------
def ollama_installed() -> bool:
    return shutil.which("ollama") is not None


def ollama_running(host: str = OLLAMA_HOST) -> bool:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def install_ollama() -> bool:
    system = platform.system()
    log.info("Ollama not found. Installing for %s ...", system)

    if system == "Windows":
        url = "https://ollama.com/download/OllamaSetup.exe"
        dest = os.path.join(os.environ.get("TEMP", "."), "OllamaSetup.exe")
        log.info("Downloading %s", url)
        try:
            urllib.request.urlretrieve(url, dest)
        except Exception as exc:
            log.error("Download failed: %s", exc)
            log.error("Install manually from https://ollama.com/download")
            return False

        log.info("Running installer (silent). A UAC prompt may appear.")
        try:
            # /VERYSILENT is Inno Setup; the Ollama installer honours it.
            subprocess.run([dest, "/VERYSILENT", "/NORESTART"], check=True, timeout=900)
        except (subprocess.SubprocessError, OSError) as exc:
            log.error("Silent install failed (%s). Run the installer manually: %s", exc, dest)
            return False

    elif system in ("Linux", "Darwin"):
        if system == "Darwin":
            log.error("On macOS install from https://ollama.com/download")
            return False
        log.info("Running the official install script.")
        try:
            subprocess.run(
                "curl -fsSL https://ollama.com/install.sh | sh",
                shell=True, check=True, timeout=900,
            )
        except subprocess.SubprocessError as exc:
            log.error("Install failed: %s", exc)
            return False
    else:
        log.error("Unsupported platform: %s", system)
        return False

    # PATH in this process is stale after an install; re-resolve.
    for _ in range(20):
        if shutil.which("ollama"):
            log.info("Ollama installed.")
            return True
        time.sleep(3)

    log.warning("Ollama installed but not on PATH yet. Open a new terminal.")
    return shutil.which("ollama") is not None


def start_ollama() -> bool:
    if ollama_running():
        return True
    exe = shutil.which("ollama")
    if not exe:
        return False
    log.info("Starting the Ollama server ...")
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([exe, "serve"], **kwargs)
    except OSError as exc:
        log.error("Could not start Ollama: %s", exc)
        return False

    for _ in range(30):
        if ollama_running():
            log.info("Ollama is up.")
            return True
        time.sleep(2)
    log.error("Ollama did not become ready.")
    return False


def model_present(tag: str) -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=10) as r:
            names = [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return False
    return any(n == tag or n.startswith(tag.split(":")[0] + ":") and n == tag for n in names)


def pull_model(tag: str) -> bool:
    exe = shutil.which("ollama")
    if not exe:
        return False
    log.info("Pulling %s - this is a multi-GB download, please wait ...", tag)
    try:
        proc = subprocess.Popen(
            [exe, "pull", tag],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace",
        )
        last = ""
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            # Progress lines repeat constantly; only log real changes.
            if line and line[:24] != last[:24]:
                log.info("  %s", line[:110])
                last = line
        proc.wait()
        return proc.returncode == 0
    except subprocess.SubprocessError as exc:
        log.error("Pull failed: %s", exc)
        return False


def verify(tag: str) -> bool:
    """Round-trip a structured-output call - the thing the pipeline relies on."""
    log.info("Verifying %s can produce structured JSON ...", tag)
    payload = {
        "model": tag,
        "messages": [{"role": "user", "content": "Reply with ok=true and level=3."}],
        "format": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "level": {"type": "integer"}},
            "required": ["ok", "level"],
        },
        "stream": False,
        "options": {"temperature": 0, "num_ctx": 2048},
        "keep_alive": 0,
    }
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            body = json.loads(r.read())
        content = (body.get("message") or {}).get("content", "")
        parsed = json.loads(content)
        ok = "ok" in parsed and "level" in parsed
        log.info("Verification %s (model returned %s)", "PASSED" if ok else "FAILED", parsed)
        return ok
    except Exception as exc:
        log.error("Verification failed: %s", exc)
        return False


def custom_quantize(hf_repo: str, quant: str, out_dir: str) -> Optional[str]:
    """
    Real quantization via llama.cpp, for a quant level nobody publishes.

    This is the slow path: it downloads the full-precision weights,
    converts to GGUF, then quantizes. Only worth it if the standard
    Q4_K_M / Q8_0 builds genuinely do not suit you.
    """
    log.warning("Custom quantization: expect a very large download and a long conversion.")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                        "huggingface_hub", "sentencepiece"], check=True)
    except subprocess.SubprocessError as exc:
        log.error("Could not install prerequisites: %s", exc)
        return None

    os.makedirs(out_dir, exist_ok=True)
    llama_dir = os.path.join(out_dir, "llama.cpp")
    if not os.path.isdir(llama_dir):
        if not shutil.which("git"):
            log.error("git is required for custom quantization.")
            return None
        subprocess.run(["git", "clone", "--depth", "1",
                        "https://github.com/ggml-org/llama.cpp", llama_dir], check=True)

    from huggingface_hub import snapshot_download  # type: ignore

    log.info("Downloading %s ...", hf_repo)
    src = snapshot_download(repo_id=hf_repo, local_dir=os.path.join(out_dir, "hf"))

    f16 = os.path.join(out_dir, "model-f16.gguf")
    log.info("Converting to GGUF ...")
    subprocess.run([sys.executable, os.path.join(llama_dir, "convert_hf_to_gguf.py"),
                    src, "--outfile", f16, "--outtype", "f16"], check=True)

    quant_bin = shutil.which("llama-quantize") or os.path.join(llama_dir, "build", "bin", "llama-quantize")
    if not os.path.exists(quant_bin):
        log.error("llama-quantize not built. Build llama.cpp in %s first.", llama_dir)
        return None

    out = os.path.join(out_dir, f"model-{quant}.gguf")
    subprocess.run([quant_bin, f16, out, quant], check=True)
    log.info("Quantized model at %s", out)
    return out


# ----------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    ap = argparse.ArgumentParser(description="Set up the local AI render stack.")
    ap.add_argument("--model", default=None, help="Force a specific Ollama tag.")
    ap.add_argument("--reserve-gb", type=float, default=4.0,
                    help="VRAM to hold back for the path tracer.")
    ap.add_argument("--no-install", action="store_true",
                    help="Fail instead of installing Ollama.")
    ap.add_argument("--custom-quant", default=None,
                    help="Real llama.cpp quantization, e.g. Q5_K_M. Slow.")
    ap.add_argument("--hf-repo", default="Qwen/Qwen3-30B-A3B-Thinking-2507")
    args = ap.parse_args()

    print("=" * 68)
    print("  4k PT Engine - AI render setup")
    print("=" * 68)

    # 1. Hardware
    gpu = detect_gpu_name()
    vram = detect_vram_gb()
    cap = detect_compute_cap()
    log.info("GPU  : %s", gpu or "none detected")
    log.info("VRAM : %s", f"{vram:.1f} GB" if vram else "unknown")
    log.info("Compute capability: %s", f"{cap:.1f}" if cap else "unknown")

    if gpu and not supports_fp8(gpu, cap):
        log.warning(
            "%s (compute %s) has no FP8 tensor cores - those need compute "
            "capability 8.9+ (Ada/Hopper/Blackwell). FP8 checkpoints such as "
            "Qwen3-30B-A3B-Thinking-2507-FP8 cannot run here; using a "
            "GGUF build instead.", gpu, f"{cap:.1f}" if cap else "?",
        )

    # 2. Ollama
    if not ollama_installed():
        if args.no_install:
            log.error("Ollama is not installed and --no-install was passed.")
            return 1
        if not install_ollama():
            return 1
    else:
        log.info("Ollama is already installed.")

    if not start_ollama():
        log.error("Could not reach the Ollama server.")
        return 1

    # 3. Model
    if args.custom_quant:
        path = custom_quantize(args.hf_repo, args.custom_quant,
                               os.path.join(os.getcwd(), "quantized"))
        if not path:
            return 1
        print(f"\nCustom GGUF written to {path}")
        print("Register it with a Modelfile:  ollama create myqwen -f Modelfile")
        return 0

    if args.model:
        choice = ModelChoice(args.model, 0.0, "user-specified")
    else:
        choice = choose_model(vram, args.reserve_gb)

    log.info("Selected model: %s  (~%.1f GB)", choice.tag, choice.size_gb)
    log.info("Reason: %s", choice.note)

    if model_present(choice.tag):
        log.info("Model already present.")
    elif not pull_model(choice.tag):
        log.error("Could not pull %s", choice.tag)
        return 1

    # 4. Verify
    if not verify(choice.tag):
        log.error("The model is installed but failed the structured-output check.")
        return 1

    print("\n" + "=" * 68)
    print("  Setup complete")
    print("=" * 68)
    print(f"  Model : {choice.tag}")
    print(f"  GPU   : {gpu or 'CPU only'}")
    print("\n  Add to your .env:")
    print(f"    ZLENDO_AI_MODEL={choice.tag}")
    print("    USE_LLM=True        # master switch; omit it and the LLM is on")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())

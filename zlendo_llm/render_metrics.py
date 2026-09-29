"""
Deterministic photometric analysis of a finished render.

This exists so the LLM never has to look at an image. Two reasons:

1. Qwen3-30B-A3B-Thinking is a text model - it has no vision encoder.
2. Even with a VLM, asking a model to eyeball exposure from a downscaled
   JPEG is strictly worse than measuring the histogram. A luminance
   percentile is exact; "looks a bit dark" is not.

So we compute hard numbers here and let the model reason over them. That
is the division of labour that actually works: deterministic measurement,
probabilistic judgement.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Dict

import numpy as np

log = logging.getLogger(__name__)

# Rec. 709 luma weights.
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _load_image(path: Path) -> np.ndarray:
    """
    Return float32 RGB in linear-ish space, shape (H, W, 3).

    EXR is already linear. PNG/JPEG are sRGB-encoded, so we linearise -
    measuring 'mean luminance' on gamma-encoded data gives numbers that
    look fine when the render is actually two stops under.
    """
    suffix = path.suffix.lower()

    if suffix == ".exr":
        try:
            import cv2  # opencv-python-headless is already in requirements
        except ImportError as exc:
            raise RuntimeError("Reading EXR requires opencv-python-headless") from exc
        os_env = __import__("os").environ
        os_env.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
        if img is None:
            raise RuntimeError(f"Could not read {path}")
        img = img[:, :, :3][:, :, ::-1]  # BGR -> RGB
        return img.astype(np.float32)

    from PIL import Image

    with Image.open(path) as im:
        arr = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0

    # sRGB EOTF -> linear
    linear = np.where(
        arr <= 0.04045,
        arr / 12.92,
        ((arr + 0.055) / 1.055) ** 2.4,
    )
    return linear.astype(np.float32)


def analyse(image_path: str | Path, max_side: int = 1024) -> Dict[str, Any]:
    """
    Measure exposure characteristics of a render.

    Downsamples first - a 4K image is 8M pixels and the statistics are
    identical at 1K, so this keeps the pass under a second.
    """
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(path)

    img = _load_image(path)
    h, w = img.shape[:2]

    stride = max(1, int(math.ceil(max(h, w) / max_side)))
    if stride > 1:
        img = img[::stride, ::stride]

    lum = img @ _LUMA
    lum = np.clip(lum, 0.0, None)
    flat = lum.reshape(-1)

    p = np.percentile(flat, [1, 5, 25, 50, 75, 95, 99]).astype(float)

    # Clipping thresholds. For LDR sources 1.0 is the ceiling; for HDR EXR
    # we treat >1.0 as headroom, so measure against a display-referred
    # threshold instead.
    is_hdr = path.suffix.lower() == ".exr"
    highlight_thresh = 1.0 if is_hdr else 0.98
    shadow_thresh = 0.005

    clipped_high = float((flat >= highlight_thresh).mean() * 100.0)
    crushed_low = float((flat <= shadow_thresh).mean() * 100.0)

    # Dynamic range in stops between the 1st and 99th percentile.
    lo = max(p[0], 1e-5)
    hi = max(p[6], lo * 1.0001)
    dynamic_range = float(math.log2(hi / lo))

    # Colour cast: how far the channel means diverge.
    ch_mean = img.reshape(-1, 3).mean(axis=0)
    denom = float(ch_mean.mean()) or 1e-6
    cast = ((ch_mean - denom) / denom).astype(float)

    # RMS contrast on log-luminance is more perceptually meaningful than
    # raw stddev, which is dominated by a handful of bright pixels.
    log_lum = np.log2(np.clip(flat, 1e-5, None))
    contrast = float(log_lum.std())

    metrics = {
        "source": path.name,
        "resolution": [int(w), int(h)],
        "colour_space": "linear (EXR)" if is_hdr else "linearised from sRGB",
        "mean_luminance": round(float(flat.mean()), 4),
        "p1_luminance": round(float(p[0]), 4),
        "p25_luminance": round(float(p[2]), 4),
        "p50_luminance": round(float(p[3]), 4),
        "p75_luminance": round(float(p[4]), 4),
        "p99_luminance": round(float(p[6]), 4),
        "clipped_highlight_pct": round(clipped_high, 3),
        "crushed_shadow_pct": round(crushed_low, 3),
        "dynamic_range_stops": round(dynamic_range, 2),
        "log_contrast": round(contrast, 3),
        "colour_cast_rgb": [round(float(c), 4) for c in cast],
    }

    metrics["heuristic_flags"] = _flags(metrics)
    return metrics


def _flags(m: Dict[str, Any]) -> list[str]:
    """Cheap deterministic pre-diagnosis; gives the model an anchor."""
    out: list[str] = []
    if m["mean_luminance"] < 0.12:
        out.append("underexposed")
    if m["mean_luminance"] > 0.55:
        out.append("overexposed")
    if m["clipped_highlight_pct"] > 2.0:
        out.append("blown_highlights")
    if m["crushed_shadow_pct"] > 15.0:
        out.append("crushed_shadows")
    if m["dynamic_range_stops"] < 5.0:
        out.append("low_dynamic_range")
    if m["log_contrast"] < 1.0:
        out.append("flat_contrast")
    cast = m["colour_cast_rgb"]
    if max(abs(c) for c in cast) > 0.18:
        out.append("colour_cast")
    if not out:
        out.append("nominal")
    return out

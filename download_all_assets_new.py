"""
=============================================================================
  ASSET & TEXTURE MASTERY — FULL LIBRARY DOWNLOADER  (v2 — correct fields)
  ─────────────────────────────────────────────────────────────────────────
  Phase 1  →  Downloads ALL high-poly 3D assets (.glb) first
  Phase 2  →  Then downloads ALL high-resolution textures

  Output files  (inside asset_downloads/):
  ┌──────────────────────┬──────────────────────────────────────────────────┐
  │ asset_url_map.json   │ Maps LR/source URL → local HR file path          │
  │                      │ (same convention used by the renderer pipeline)   │
  ├──────────────────────┼──────────────────────────────────────────────────┤
  │ failed_downloads.json│ Every URL that failed, with full error details    │
  │                      │ so you can debug / retry them later               │
  └──────────────────────┴──────────────────────────────────────────────────┘

  API shape discovered:
    Assets   → relative path in  glb_Url / highPoly_Glb  (no full URL)
    Textures → relative paths in  high_Resolution_Url[]  &  low_Resolution_Url[]
=============================================================================
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime
import requests
from pathlib import Path
from urllib.parse import urlparse, unquote

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
ASSETS_AND_TEXTURE_API_KEY = "zrsk_beta_8a1d4c7e6f2b9a5d3c1e0f8b6a4d"
ASSETS_AND_TEXTURE_API_HEADER_NAME = "ZRealtyServiceApiKey"
ASSET_ENDPOINT = "http://216.48.178.133:4050/api/v1/AssetMaster/GetAllAssets3D"
TEXTURE_ENDPOINT = "http://216.48.178.133:4050/api/v1/TextureMaster/GetAllTextureLibraries"

# Blob storage base — all relative paths from the API are under this root
BLOB_BASE = "https://zrealtystoragedev.blob.core.windows.net"

# Azure SAS token — required for private blob storage access
# Appended to every download URL at request time.
# The clean URL (without SAS) is still used as the key in asset_url_map.json.
BLOB_SAS_TOKEN = (
    "sv=2024-11-04"
    "&ss=bfqt"
    "&srt=sco"
    "&sp=rwdlacupiytfx"
    "&se=2026-11-18T20:34:53Z"
    "&st=2025-09-12T12:19:53Z"
    "&spr=https,http"
    "&sig=KNQs7rhe81AeQfnd%2BS4QMPWWo55VbNICTufFVYe5KhA%3D"
)

# Folders
BASE_DIR        = Path(__file__).parent
DOWNLOAD_DIR    = BASE_DIR / "asset_downloads"
URL_MAP_FILE    = DOWNLOAD_DIR / "asset_url_map.json"
FAILED_MAP_FILE = DOWNLOAD_DIR / "failed_downloads.json"

# Timeout: None = infinite (as requested)
REQUEST_TIMEOUT = None

# ---------------------------------------------------------------------------
# TIMER / SCHEDULE CONTROL
# ---------------------------------------------------------------------------
# USE_TIMER = True  → run on a daily schedule (waits for SCHEDULED_TIME each day)
# USE_TIMER = False → run instantly when the script is launched (no waiting)
USE_TIMER = False

# Headers for every API call
API_HEADERS = {
    ASSETS_AND_TEXTURE_API_HEADER_NAME: ASSETS_AND_TEXTURE_API_KEY,
    "Accept": "application/json",
}

# Retry settings for transient errors
MAX_RETRIES   = 3
RETRY_DELAY_S = 5

# ---------------------------------------------------------------------------
# LOGGING  (console + file)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "download_all_assets.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def short_hash(text: str, length: int = 8) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:length]


def relative_to_full_url(rel_path: str) -> str:
    """
    Turn a relative API path like 'glb-assets/Table/foo.glb'
    into a full Azure Blob URL.
    If the value already starts with http it is returned unchanged.
    """
    rel_path = rel_path.strip()
    if rel_path.startswith("http"):
        return rel_path
    # strip any accidental leading slash
    rel_path = rel_path.lstrip("/")
    return f"{BLOB_BASE}/{rel_path}"


def url_to_local_filename(url: str) -> str:
    """
    <8-char-hash>_<original-basename>
    e.g.  'abc12345_MinimalistSofa_HP.glb'
    Hash is computed on the CLEAN url (no SAS) so filenames are stable.
    """
    clean = url.split("?")[0]   # strip any existing query string before hashing
    basename = os.path.basename(unquote(urlparse(clean).path))
    if not basename:
        basename = "unnamed_file"
    return f"{short_hash(clean)}_{basename}"


def add_sas_token(url: str) -> str:
    """
    Append the Azure SAS token to a blob storage URL.
    If the URL already has a query string the SAS is appended with '&'.
    Non-blob URLs are returned unchanged.
    """
    if BLOB_BASE not in url:
        return url
    clean = url.split("?")[0]   # ensure we don't double-append
    return f"{clean}?{BLOB_SAS_TOKEN}"


def load_json_file(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            log.warning("Could not parse %s (%s) — starting fresh.", path.name, exc)
    return {}


def save_json_file(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    log.info("  💾 Saved %-30s (%d entries)", path.name, len(data))


# ---------------------------------------------------------------------------
# DOWNLOAD
# ---------------------------------------------------------------------------

def download_file(url: str, dest_path: Path) -> tuple[bool, str]:
    """
    Download *url* → *dest_path*.
    Returns (success: bool, error_message: str).
    Skips if file already exists and is non-empty.
    Retries MAX_RETRIES times on transient errors.
    Timeout = None (infinite).
    """
    if dest_path.exists() and dest_path.stat().st_size > 0:
        log.info("  ✓ SKIP  (already exists on disk)  %s", dest_path.name)
        return True, "skipped"

    # Always attach SAS token for Azure Blob Storage URLs
    signed_url = add_sas_token(url)

    last_error = "unknown error"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info("  ↓ [%d/%d] %s", attempt, MAX_RETRIES, url)
            with requests.get(signed_url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
                resp.raise_for_status()
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            fh.write(chunk)
            size_mb = dest_path.stat().st_size / (1024 * 1024)
            log.info("  ✓ SAVED %.2f MB  →  %s", size_mb, dest_path.name)
            return True, ""

        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            last_error = f"HTTP {code}: {exc}"
            log.warning("  ✗ HTTP %s  (attempt %d)  %s", code, attempt, url)

        except requests.exceptions.ConnectionError as exc:
            last_error = f"ConnectionError: {exc}"
            log.warning("  ✗ Connection error  (attempt %d)  %s", attempt, url)

        except requests.exceptions.Timeout as exc:
            last_error = f"Timeout: {exc}"
            log.warning("  ✗ Timeout  (attempt %d)  %s", attempt, url)

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("  ✗ Error  (attempt %d)  %s  [%s]", attempt, url, exc)

        # Clean up partial file
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)

        if attempt < MAX_RETRIES:
            log.info("  … retry in %ds …", RETRY_DELAY_S)
            time.sleep(RETRY_DELAY_S)

    log.error("  ✗ FAILED after %d attempts: %s  [%s]", MAX_RETRIES, url, last_error)
    return False, last_error


# ---------------------------------------------------------------------------
# API  —  fetch full catalogues
# ---------------------------------------------------------------------------

def fetch_api(endpoint: str, label: str) -> list:
    log.info("Fetching %s catalogue: %s", label, endpoint)
    try:
        resp = requests.get(endpoint, headers=API_HEADERS, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        count = len(data) if isinstance(data, list) else "?"
        log.info("  → %s records received: %s", label, count)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.error("  ✗ Failed to fetch %s: %s", label, exc)
        return []


# ---------------------------------------------------------------------------
# ASSET extraction  (Phase 1)
# ---------------------------------------------------------------------------
# API fields (confirmed from live response):
#   glb_Url       — relative path to the primary GLB (always present)
#   highPoly_Glb  — relative path to HP GLB (may be null)
#   isActive      — bool
#
# Strategy:
#   Prefer highPoly_Glb when present, fall back to glb_Url.
#   Skip records where both are null/empty or asset is inactive.

def extract_asset_items(assets: list) -> list[tuple[str, str, str, str]]:
    """
    Returns list of  (source_url, download_url, label, asset_id)
    Both source_url and download_url are the same full URL for assets
    (we don't do LR→HR swap for GLBs — the API provides the correct path).
    """
    items = []
    skipped_inactive = 0
    skipped_no_url   = 0

    for rec in assets:
        if not rec.get("isActive", True):
            skipped_inactive += 1
            continue

        # Pick the best available GLB path
        hp_rel  = rec.get("highPoly_Glb") or ""
        glb_rel = rec.get("glb_Url")      or ""

        # Try highPoly first, then fall back to glb_Url
        rel_path = (hp_rel or glb_rel).strip()
        if not rel_path:
            skipped_no_url += 1
            continue

        full_url = relative_to_full_url(rel_path)
        label    = f"Asset [{rec.get('asset3D_Name', '?')}]  HP={bool(hp_rel)}"
        asset_id = str(rec.get("asset3D_Id", ""))
        items.append((full_url, full_url, label, asset_id))

    log.info(
        "  Asset items: %d usable  |  %d inactive  |  %d no-url",
        len(items), skipped_inactive, skipped_no_url,
    )
    return items


# ---------------------------------------------------------------------------
# TEXTURE extraction  (Phase 2)
# ---------------------------------------------------------------------------
# API fields (confirmed from live response):
#   low_Resolution_Url   — list of relative paths  (LR)
#   high_Resolution_Url  — list of relative paths  (HR)
#   textureProcessStatus — 2 = Completed (only these have real files)
#
# Mapping convention (matching existing asset_url_map.json):
#   key   = full LR URL   (what callers reference)
#   value = local file path of downloaded HR file
#
# IMPORTANT: The API returns LR[] and HR[] in DIFFERENT orders!
# We must match them by texture type (BaseColor, NormalGL, etc.)
# extracted from the filename, NOT by array index.

import re as _re

def _extract_texture_type(url: str) -> str:
    """
    Extract the texture type keyword from a URL's filename.
    E.g.  '.../Planks008_LR_NormalGL.png' → 'normalgl'
          '.../Planks008_HR_BaseColor.png' → 'basecolor'
    """
    clean = url.split("?")[0]
    basename = os.path.basename(unquote(urlparse(clean).path))
    stem = os.path.splitext(basename)[0]
    stem_lower = stem.lower()

    # Remove resolution markers so we can find the type at the end
    cleaned = _re.sub(
        r'_(?:lr|hr|lr_hr|4k|4k-png|1k|2k|1024|2048|4096)(?=_|$)',
        '',
        stem_lower,
    )

    parts = cleaned.rsplit('_', 1)
    if len(parts) >= 2 and parts[-1].strip():
        return parts[-1].strip()

    # Fallback: last meaningful segment from original
    for part in reversed(stem_lower.split('_')):
        part = part.strip()
        if part in ('lr', 'hr', '4k', '1k', '2k', '1024', '2048', '4096'):
            continue
        if part:
            return part
    return stem_lower


def _match_lr_hr_by_type(lr_urls: list, hr_urls: list, texture_name: str) -> list:
    """
    Match LR and HR URLs by texture type (BaseColor, NormalGL, etc.)
    Returns list of (lr_url, hr_url) tuples.
    """
    # Build type → URL map for HR
    hr_type_map = {}
    for url in hr_urls:
        ttype = _extract_texture_type(url)
        hr_type_map[ttype] = url

    # Fuzzy aliases for type matching
    _aliases = {
        'basecolor': ['color', 'albedo', 'diffuse', 'diff'],
        'color': ['basecolor', 'albedo', 'diffuse', 'diff'],
        'albedo': ['basecolor', 'color', 'diffuse'],
        'normal': ['normalgl', 'normaldx'],
        'normalgl': ['normal'],
        'normaldx': ['normal'],
        'height': ['displacement', 'disp'],
        'displacement': ['height', 'disp'],
        'disp': ['displacement', 'height'],
        'roughness': ['rough'],
        'rough': ['roughness'],
        'ao': ['ambientocclusion'],
        'ambientocclusion': ['ao'],
        'metallic': ['metalness', 'metal'],
        'metalness': ['metallic', 'metal'],
        'specular': ['specularlevel'],
        'specularlevel': ['specular'],
    }

    pairs = []
    for lr_url in lr_urls:
        lr_type = _extract_texture_type(lr_url)
        hr_url = hr_type_map.get(lr_type)

        if not hr_url:
            # Try aliases
            for alias in _aliases.get(lr_type, []):
                if alias in hr_type_map:
                    hr_url = hr_type_map[alias]
                    log.info("  ℹ Texture '%s': fuzzy matched LR '%s' → HR '%s'",
                             texture_name, lr_type, alias)
                    break

        if hr_url:
            pairs.append((lr_url, hr_url))
        else:
            log.warning("  ⚠ Texture '%s': no HR match for LR type '%s' — %s",
                        texture_name, lr_type, lr_url)

    return pairs


def extract_texture_items(textures: list) -> list[tuple[str, str, str, str]]:
    """
    Returns list of  (lr_source_url, hr_download_url, label, asset_id)
    Matches LR↔HR by texture type (BaseColor, NormalGL, etc.)
    """
    items = []
    skipped_not_done = 0
    skipped_empty    = 0

    for rec in textures:
        name   = rec.get("textureLibraryName", "?")
        status = rec.get("textureProcessStatus", 0)

        # Only process successfully completed textures (status == 2)
        if status != 2:
            skipped_not_done += 1
            continue

        lr_paths = rec.get("low_Resolution_Url")  or []
        hr_paths = rec.get("high_Resolution_Url") or []

        if not hr_paths:
            skipped_empty += 1
            continue

        # Convert to full URLs
        lr_urls = [relative_to_full_url(p) for p in lr_paths if p]
        hr_urls = [relative_to_full_url(p) for p in hr_paths if p]

        # Match by texture type, not by index
        pairs = _match_lr_hr_by_type(lr_urls, hr_urls, name)

        for idx, (lr_url, hr_url) in enumerate(pairs):
            label = f"Texture [{name}] idx={idx}"
            items.append((lr_url, hr_url, label, ""))
            # Also map HR URL to itself so it's cached if referenced directly
            if hr_url != lr_url:
                items.append((hr_url, hr_url, label + " (HR source)", ""))

        # If no LR URLs, still download HR and map HR→HR
        if not lr_urls:
            for idx, hr_rel in enumerate(hr_paths):
                hr_url = relative_to_full_url(hr_rel)
                label = f"Texture [{name}] idx={idx} (HR-only)"
                items.append((hr_url, hr_url, label, ""))

    log.info(
        "  Texture items: %d usable  |  %d not-completed  |  %d empty-HR",
        len(items), skipped_not_done, skipped_empty,
    )
    return items


# ---------------------------------------------------------------------------
# DOWNLOAD PHASE
# ---------------------------------------------------------------------------

def run_download_phase(
    items:      list[tuple[str, str, str, str]],
    phase_name: str,
    url_map:    dict,
    failed_map: dict,
) -> tuple[int, int, int]:
    """
    Downloads all items in *items*.
    Mutates url_map and failed_map in-place.
    Returns (succeeded, skipped, failed).
    """
    total     = len(items)
    succeeded = skipped = failed = 0

    log.info("")
    log.info("━" * 70)
    log.info(" %s  (%d items)", phase_name, total)
    log.info("━" * 70)

    for idx, (source_url, download_url, label, asset_id) in enumerate(items, start=1):
        # Ensure source_url is clean (no query string) for the mapping key
        source_url = source_url.split('?')[0].strip()
        
        log.info("[%d/%d] %s", idx, total, label)
        log.info("        SRC: %s", source_url)
        if download_url != source_url:
            log.info("        DL:  %s", download_url)

        # Already in success map and file still on disk → skip
        if source_url in url_map:
            entry = url_map[source_url]
            local_path_str = entry["local_path"] if isinstance(entry, dict) else entry
            local_path = Path(local_path_str)
            if local_path.exists() and local_path.stat().st_size > 0:
                log.info("  ✓ SKIP  (already in url_map, file present)")
                skipped  += 1
                
                # Make sure old flat entries get upgraded automatically even if skipped
                if not isinstance(entry, dict):
                    url_map[source_url] = {"frontend_url": source_url, "local_path": str(local_path), "asset_id": asset_id}
                    save_json_file(URL_MAP_FILE, url_map)
                    
                continue
            log.info("  → In url_map but file missing — re-downloading.")

        filename  = url_to_local_filename(download_url)
        dest_path = DOWNLOAD_DIR / filename

        ok, status_or_err = download_file(download_url, dest_path)

        if ok:
            url_map[source_url] = {"frontend_url": source_url, "local_path": str(dest_path), "asset_id": asset_id}
            failed_map.pop(source_url, None)   # clear any old failure
            
            if status_or_err == "skipped":
                skipped += 1
            else:
                succeeded += 1
                
            # Save progress after every successful action
            save_json_file(URL_MAP_FILE, url_map)
        else:
            failed_map[source_url] = {
                "source_url":   source_url,
                "download_url": download_url,
                "label":        label,
                "error":        status_or_err,
                "failed_at":    time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            failed += 1
            save_json_file(FAILED_MAP_FILE, failed_map)

    return succeeded, skipped, failed


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 70)
    log.info(" ASSET & TEXTURE MASTERY — FULL LIBRARY DOWNLOADER  v2")
    log.info("=" * 70)

    ensure_dir(DOWNLOAD_DIR)

    # Load existing maps (allow resuming without re-downloading)
    url_map    = load_json_file(URL_MAP_FILE)
    failed_map = load_json_file(FAILED_MAP_FILE)

    # ── 1. Fetch catalogues ────────────────────────────────────────────────
    assets_data   = fetch_api(ASSET_ENDPOINT,   "3D Assets")
    textures_data = fetch_api(TEXTURE_ENDPOINT, "Textures")

    # ── 2. Extract download items ──────────────────────────────────────────
    log.info("")
    log.info("Extracting asset items …")
    asset_items = extract_asset_items(assets_data)

    log.info("Extracting texture items …")
    texture_items = extract_texture_items(textures_data)

    # ── 3. Phase 1: Download 3D assets first ──────────────────────────────
    a_ok, a_skip, a_fail = run_download_phase(
        asset_items, "PHASE 1 — 3D Assets (GLB)", url_map, failed_map
    )

    # ── 4. Phase 2: Download textures ──────────────────────────────────────
    t_ok, t_skip, t_fail = run_download_phase(
        texture_items, "PHASE 2 — Textures (High-Resolution)", url_map, failed_map
    )

    # ── 5. Final save & summary ────────────────────────────────────────────
    save_json_file(URL_MAP_FILE,    url_map)
    save_json_file(FAILED_MAP_FILE, failed_map)

    total_ok   = a_ok   + t_ok
    total_skip = a_skip + t_skip
    total_fail = a_fail + t_fail
    grand      = len(asset_items) + len(texture_items)

    log.info("")
    log.info("=" * 70)
    log.info(" DOWNLOAD COMPLETE")
    log.info("─" * 70)
    log.info("  %-40s  %d", "Total items:", grand)
    log.info("  %-40s  %d  (assets=%d, textures=%d)", "Succeeded:",    total_ok,   a_ok,   t_ok)
    log.info("  %-40s  %d  (assets=%d, textures=%d)", "Skipped:",      total_skip, a_skip, t_skip)
    log.info("  %-40s  %d  (assets=%d, textures=%d)", "Failed:",       total_fail, a_fail, t_fail)
    log.info("─" * 70)
    log.info("  asset_url_map.json    →  %d entries", len(url_map))
    log.info("  failed_downloads.json →  %d entries", len(failed_map))
    log.info("=" * 70)

    if failed_map:
        log.warning(
            "%d download(s) failed. Inspect failed_downloads.json to retry.", total_fail
        )


if __name__ == "__main__":
    SCHEDULED_TIME = "00:00"  # 12:00 AM — only used when USE_TIMER = True

    if not USE_TIMER:
        # ── Instant mode: download assets right now, then exit ──────────────
        log.info("=" * 70)
        log.info(" USE_TIMER=False — Running asset download INSTANTLY")
        log.info("=" * 70)
        try:
            main()
            log.info("✅ Instant download task finished.")
        except Exception as e:
            log.error(f"❌ Critical error during instant run: {e}")
    else:
        # ── Scheduled mode: wait for SCHEDULED_TIME each day ─────────────────
        last_run_date = ""

        log.info("=" * 70)
        log.info(f" USE_TIMER=True — SCHEDULER STARTING, waiting for {SCHEDULED_TIME} daily trigger")
        log.info("=" * 70)

        while True:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            today_date = now.strftime("%Y-%m-%d")

            # Check if it's 12:00 AM and we haven't run it yet today
            if current_time == SCHEDULED_TIME and last_run_date != today_date:
                log.info("⏰ Time is 12:00 AM! Starting automatic asset download...")
                try:
                    main()
                    last_run_date = today_date
                    log.info("✅ Daily download task finished. Waiting for next 12:00 AM...")
                except Exception as e:
                    log.error(f"❌ Critical error during scheduled run: {e}")

            # Check every 10 seconds
            time.sleep(10)

# # filename: main.py
# from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Body, Depends, WebSocket
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
# from fastapi.staticfiles import StaticFiles
# from pydantic import BaseModel
# from typing import Optional, List, Set, Dict, Any
# from typing_extensions import Annotated
# from pathlib import Path
# from PIL import Image
# import os
# import sys
# import io
# import json
# import uuid
# import copy
# import hashlib
# import subprocess
# import requests
# import tempfile
# import httpx
# from urllib.parse import urlparse
# from datetime import datetime
# import time
# from concurrent.futures import ThreadPoolExecutor, as_completed, wait, wait
# import shlex
# import re
# import glob
# from app.database import models, session
# from app.services import auth_service
# from app.services.render_logger import RenderLogger
# from threading import Lock, Thread
# import queue
# import signal
# import asyncio
# import logging
# from dotenv import load_dotenv

# # AI Render Service - Moved to app/services/ai_render_service.py
# from app.services.ai_render_service import get_ai_render_service

# # Load environment variables from .env file
# load_dotenv()

# REROUTE_ENGINE_URL = os.getenv("REROUTE_ENGINE_URL", "")
# REROUTE_ENGINE_API_KEY = os.getenv("REROUTE_ENGINE_API_KEY", "")


# map_lock = Lock()

# # --- CONSOLE COLORS ---
# COLOR_RED = "\033[91m"
# COLOR_GREEN = "\033[92m"
# COLOR_YELLOW = "\033[93m"
# COLOR_BLUE = "\033[94m"
# COLOR_RESET = "\033[0m"

# # --- ASSET & TEXTURE MASTERY API CONFIGURATION ---
# ASSETS_AND_TEXTURE_API_KEY = os.getenv("ASSETS_AND_TEXTURE_API_KEY")
# ASSETS_AND_TEXTURE_API_HEADER_NAME = os.getenv("ASSETS_AND_TEXTURE_API_HEADER_NAME", "ZRealtyServiceApiKey")
# ASSET_ENDPOINT = os.getenv("ASSET_ENDPOINT")
# TEXTURE_ENDPOINT = os.getenv("TEXTURE_ENDPOINT")

# # --- BASE DIRECTORY ---
# BASE_DIR = Path(__file__).resolve().parent

# # AI Render Service Initialization
# ai_service = get_ai_render_service(BASE_DIR)

# # Global Cache for Master Data
# _CACHED_ASSETS_MAP = {}
# _CACHED_TEXTURES_MAP = {}
# _CACHED_ASSET_URL_TO_HP_MAP = {}  # Map: Any GLB URL -> its HP GLB URL
# _CACHED_TEXTURE_URL_TO_HP_MAP = {} # Map: Any Texture URL -> its HR Texture URL
# _MASTER_DATA_FETCHED = False
# _MASTER_DATA_LOCK = Lock()

# def _fetch_master_data_from_api():
#     """
#     Fetches Asset and Texture master data from the API and populates the global caches.
#     This should be called once at the start of the download process.
#     """
#     global _CACHED_ASSETS_MAP, _CACHED_TEXTURES_MAP, _CACHED_ASSET_URL_TO_HP_MAP, _MASTER_DATA_FETCHED
    
#     with _MASTER_DATA_LOCK:
#         if _MASTER_DATA_FETCHED:
#             return

#         print(f"\n{COLOR_BLUE}--- Fetching Master Data from API... ---{COLOR_RESET}")
#         headers = {
#             ASSETS_AND_TEXTURE_API_HEADER_NAME: ASSETS_AND_TEXTURE_API_KEY,
#             "Content-Type": "application/json"
#         }
        
#         # 1. Fetch Assets
#         try:
#             print(f"  Requesting Assets from: {ASSET_ENDPOINT}")
#             # verify=False to avoid SSL issues with some internal/QA certs
#             resp = requests.get(ASSET_ENDPOINT, headers=headers, verify=False, timeout=60)
#             if resp.status_code == 200:
#                 assets_list = resp.json()
#                 if isinstance(assets_list, list):
#                     # Map ID -> Asset Object
#                     _CACHED_ASSETS_MAP = {str(item.get('asset3D_Id')): item for item in assets_list if item.get('asset3D_Id') is not None}
                    
#                     # --- NEW: Build URL-to-HP mapping ---
#                     _CACHED_ASSET_URL_TO_HP_MAP = {}
#                     for item in assets_list:
#                         hp_url = item.get('highPoly_Glb')
#                         if not hp_url or not isinstance(hp_url, str):
#                             continue
                        
#                         # Normalize HP URL
#                         norm_hp = hp_url.strip().replace("\\", "/")
#                         if norm_hp.startswith("/"): norm_hp = norm_hp[1:]
                        
#                         # Assets to map to this HP
#                         potential_keys = [
#                             item.get('lowPoly_Glb'),
#                             item.get('mediumPoly_Glb'),
#                             item.get('highPoly_Glb')
#                         ]
#                         for k in potential_keys:
#                             if k and isinstance(k, str):
#                                 norm_k = k.strip().replace("\\", "/")
#                                 if norm_k.startswith("/"): norm_k = norm_k[1:]
#                                 # Store exact relative path (e.g. glb-assets/...)
#                                 _CACHED_ASSET_URL_TO_HP_MAP[norm_k] = norm_hp
#                                 # Store filename only (fallback)
#                                 _CACHED_ASSET_URL_TO_HP_MAP[os.path.basename(norm_k)] = norm_hp
#                                 # Store with full blob prefix if missing
#                                 if not norm_k.startswith("http"):
#                                      full_u = f"https://zrealtystoragedev.blob.core.windows.net/{norm_k}"
#                                      _CACHED_ASSET_URL_TO_HP_MAP[full_u] = norm_hp
                    
#                     print(f"  {COLOR_GREEN}[OK] Loaded {len(_CACHED_ASSETS_MAP)} assets and indexed {len(_CACHED_ASSET_URL_TO_HP_MAP)} URLs.{COLOR_RESET}")
#                     # DEBUG: Check for specific ID 147
#                     if 147 in _CACHED_ASSETS_MAP or "147" in _CACHED_ASSETS_MAP:
#                         print(f"  {COLOR_GREEN}[OK] Asset ID 147 successfully loaded in cache.{COLOR_RESET}")
#                     else:
#                         print(f"  {COLOR_RED}[X] Asset ID 147 NOT found in cache.{COLOR_RESET}")

#                 else:
#                     print(f"  {COLOR_RED}[X] Asset response was not a list.{COLOR_RESET}")
#             else:
#                 if resp.status_code == 404:
#                     print(f"  {COLOR_YELLOW}[!] Asset fetch failed: 404 Not Found (Master data not available){COLOR_RESET}")
#                 else:
#                     error_text = resp.text[:100] if resp.text and not resp.text.strip().startswith("<") else "Unknown Error"
#                     print(f"  {COLOR_RED}[X] Asset fetch failed: {resp.status_code} - {error_text}{COLOR_RESET}")
#         except Exception as e:
#             print(f"  {COLOR_RED}[X] Asset fetch exception: {e}{COLOR_RESET}")

#         # 2. Fetch Textures
#         try:
#             print(f"  Requesting Textures from: {TEXTURE_ENDPOINT}")
#             resp = requests.get(TEXTURE_ENDPOINT, headers=headers, verify=False, timeout=60)
#             if resp.status_code == 200:
#                 textures_list = resp.json()
#                 if isinstance(textures_list, list):
#                     # Map ID -> Texture Object
#                     _CACHED_TEXTURES_MAP = {str(item.get('textureLibrary_Id')): item for item in textures_list if item.get('textureLibrary_Id') is not None}
                    
#                     # --- NEW: Build Texture URL mapping ---
#                     _CACHED_TEXTURE_URL_TO_HP_MAP = {}
#                     for item in textures_list:
#                         hr_urls = item.get('high_Resolution_Url')
#                         lr_urls = item.get('low_Resolution_Url')
                        
#                         if hr_urls and isinstance(hr_urls, list):
#                             # Map everything to HR list
#                             potential_sources = []
#                             if hr_urls: potential_sources.extend(hr_urls)
#                             if lr_urls and isinstance(lr_urls, list): potential_sources.extend(lr_urls)
                            
#                             for s in potential_sources:
#                                 if s and isinstance(s, str):
#                                     norm_s = s.strip().replace("\\", "/")
#                                     # Texture Library maps to a list of HR URLs
#                                     _CACHED_TEXTURE_URL_TO_HP_MAP[norm_s] = hr_urls
                    
#                     print(f"  {COLOR_GREEN}[OK] Loaded {len(_CACHED_TEXTURES_MAP)} textures and indexed {len(_CACHED_TEXTURE_URL_TO_HP_MAP)} urls.{COLOR_RESET}")
#                 else:
#                     print(f"  {COLOR_RED}[X] Texture response was not a list.{COLOR_RESET}")
#             else:
#                 if resp.status_code == 404:
#                      print(f"  {COLOR_YELLOW}[!] Texture fetch failed: 404 Not Found (Master data not available){COLOR_RESET}")
#                 else:
#                     error_text = resp.text[:100] if resp.text and not resp.text.strip().startswith("<") else "Unknown Error"
#                     print(f"  {COLOR_RED}[X] Texture fetch failed: {resp.status_code} - {error_text}{COLOR_RESET}")
#         except Exception as e:
#             print(f"  {COLOR_RED}[X] Texture fetch exception: {e}{COLOR_RESET}")
            
#         _MASTER_DATA_FETCHED = True

# def _resolve_asset_url(asset_id, show_details=False):
#     """
#     Looks up asset_id in the cache and returns the best available GLB URL.
#     HighPoly is preferred, then glb_Url as a fallback.
#     """
#     if not asset_id:
#         return None
        
#     # All keys in _CACHED_ASSETS_MAP are strings now
#     asset = _CACHED_ASSETS_MAP.get(str(asset_id))

#     if not asset:
#         if show_details:
#              print(f"  {COLOR_YELLOW}Warning: Asset ID {asset_id} not found in master data.{COLOR_RESET}")
#         return None
        
#     # Priority: High Poly first, then fall back to the main GLB URL.
#     hp_url = asset.get('highPoly_Glb') or asset.get('glb_Url')

#     if show_details:
#         print(f"DEBUG: Resolving Asset {asset_id}. Found HP URL: {hp_url}")

#     # If keys are None or empty string, treat as empty
#     hp_url = hp_url if (hp_url and isinstance(hp_url, str) and hp_url.strip()) else None

#     # Convert relative blob paths to full URLs for the downloader.
#     final_url = _convert_glb_path_to_url(hp_url) if hp_url else None
    
#     if final_url:
#         return final_url
#     return None

# def _resolve_hp_url_from_url(url: str, show_details=False) -> Any:
#     """
#     Given a URL or path, looks up if it matches:
#     1. Any known asset's GLB URL -> returns HP GLB URL (str)
#     2. Any known texture's URL -> returns list of HR URLs (list[str])
#     """
#     if not url or not isinstance(url, str):
#         return None
        
#     norm_url = url.strip().replace("\\", "/")
#     if norm_url.startswith("/"): norm_url = norm_url[1:]
    
#     # NEW: Handle spaces in URL (decode %20)
#     if '%' in norm_url:
#         from urllib.parse import unquote
#         norm_url_decoded = unquote(norm_url)
#     else:
#         norm_url_decoded = norm_url

#     # 1. Check Assets Map (GLBs)
#     # Try exact match first
#     hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(norm_url)
    
#     # Try decoded match (e.g. "Office Table_LP.glb" vs "Office%20Table_LP.glb")
#     if not hp_found:
#         hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(norm_url_decoded)

#     # NEW: Handle spaces in URL -> Try ENCODED match (Space -> %20)
#     # The Map might store keys as proper URLs (with %20), but input has spaces.
#     if not hp_found and ' ' in norm_url:
#         from urllib.parse import quote
#         # Quote paths but not slashes/colons generally, though for just filename/path component
#         # a simple replace for space is safest given the context of GLB filenames.
#         norm_url_encoded = norm_url.replace(" ", "%20")
#         hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(norm_url_encoded)
#     # Try decoded match (e.g. "Office Table_LP.glb" vs "Office%20Table_LP.glb")
#     if not hp_found:
#         hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(norm_url_decoded)
    
#     if not hp_found:
#         path_part = urlparse(url).path
#         if path_part.startswith("/"): path_part = path_part[1:]
#         hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(path_part)
        
#         # Try decoded path part
#         if not hp_found:
#             path_part_decoded = unquote(path_part) if '%' in path_part else path_part
#             hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(path_part_decoded)
        
#     if hp_found:
#         full_hp_url = _convert_glb_path_to_url(hp_found)
#         if show_details:
#             print(f"  {COLOR_GREEN}✓ Resolved Asset {os.path.basename(url)} -> {os.path.basename(full_hp_url)} via URL match{COLOR_RESET}")
#         return full_hp_url
        
#     # 2. Check Textures Map (Images)
#     hr_list = _CACHED_TEXTURE_URL_TO_HP_MAP.get(norm_url)
#     if not hr_list:
#         # Also try just the filename/path part for textures? 
#         # Usually textures have long paths. Let's try direct norm_url first.
#         # If no match, try without SAS if present
#         if '?' in norm_url:
#             base_norm = norm_url.split('?')[0]
#             hr_list = _CACHED_TEXTURE_URL_TO_HP_MAP.get(base_norm)

#     if hr_list and isinstance(hr_list, list):
#         if show_details:
#              print(f"  {COLOR_GREEN}✓ Resolved Texture {os.path.basename(url)} -> HR List ({len(hr_list)} items) via URL match{COLOR_RESET}")
#         return hr_list
        
#     return None

# def _resolve_texture_urls(texture_id, show_details=False):
#     """
#     Looks up texture_id and returns a list of High Resolution URLs.
#     """
#     if not texture_id:
#         return []
        
#     texture = _CACHED_TEXTURES_MAP.get(str(texture_id))
#     if not texture:
#         if show_details:
#              print(f"  {COLOR_YELLOW}Warning: Texture ID {texture_id} not found in master data.{COLOR_RESET}")
#         return []
        
#     # Try High Resolution first
#     urls = texture.get('high_Resolution_Url')
#     if not urls:
#         urls = texture.get('low_Resolution_Url')
        
#     if urls and isinstance(urls, list):
#          return [
#              u if u.startswith("http") else f"https://zrealtystoragedev.blob.core.windows.net/{u.lstrip('/')}"
#              for u in urls
#              if isinstance(u, str) and u.strip()
#          ]
#     return []


# # --- RENDER QUEUE SYSTEM ---
# # Queue to hold render jobs (processes one at a time to avoid render conflicts)
# render_queue = queue.Queue()
# queue_lock = Lock()
# active_jobs = {}  # Track job status: {job_id: {"status": "queued|processing|completed|failed", "position": int}}
# is_shutting_down = False  # Flag to track shutdown state

# # --- GLOBAL STATUS CACHE (Recommended Python Implementation) ---
# # Optimized for high-frequency polling
# CURRENT_STATUS = {
#     "is_busy": False,
#     "last_updated": time.time(),
#     "active_job": None
# }

# def _update_global_status(
#     job_id=None,
#     status=None,
#     step=None,
#     progress=None,
#     message=None,
#     project_id=None,
#     gallery_id=None,
#     job_type=None,
#     render_size: Optional[Dict[str, Any]] = None,
# ):
#     """
#     Updates the lightweight global status dictionary for O(1) polling.
#     """
#     global CURRENT_STATUS
#     CURRENT_STATUS["last_updated"] = time.time()
#     if render_size is None and job_id:
#         with queue_lock:
#             render_size = _extract_job_render_size(active_jobs.get(job_id, {}))
    
#     # Determine if busy based on status
#     is_active_state = status and status.lower() in ['processing', 'starting', 'rendering', 'encoding', 'uploading', 'initializing', 'queued']
    
#     if is_active_state:
#         CURRENT_STATUS["is_busy"] = status.lower() != 'queued' # Queued doesn't mean busy engine necessarily, but "processing" does.
#         # Actually prompt says "Busy (Job Running)". Queued is waiting.
        
#         if status.lower() == 'processing' or CURRENT_STATUS["is_busy"]:
#              CURRENT_STATUS["is_busy"] = True

#         # Only update active_job details if we have them and it's an active state
#         if job_id and CURRENT_STATUS["is_busy"]:
#             # If we don't have an active job or it's a different one, start fresh
#             if not CURRENT_STATUS["active_job"] or CURRENT_STATUS["active_job"].get("id") != job_id:
#                   CURRENT_STATUS["active_job"] = {
#                     "id": job_id,
#                     "project_id": project_id,
#                     "gallery_id": gallery_id,
#                     "status": status,
#                     "step": step or "Initializing",
#                     "progress": progress if progress is not None else 0,
#                     "message": message or "Starting...",
#                     "started_at": datetime.now().strftime("%H:%M:%S"),
#                     "type": job_type if job_type else "image"
#                 }
#                   if render_size:
#                       CURRENT_STATUS["active_job"].update(render_size)
#             else:
#                 # Update existing
#                 current = CURRENT_STATUS["active_job"]
#                 if status: current["status"] = status
#                 if step: current["step"] = step
#                 if progress is not None: current["progress"] = progress
#                 if message: current["message"] = message
#                 # Don't overwrite IDs if not provided
#                 if project_id: current["project_id"] = project_id
#                 if gallery_id: current["gallery_id"] = gallery_id
#                 if job_type: current["type"] = job_type
#                 if render_size:
#                     current.update(render_size)
                
#     elif status and status.lower() in ['completed', 'failed', 'error', 'cancelled']:
#         CURRENT_STATUS["is_busy"] = False
#         # Preserve active_job info on failure so monitor can read it
#         if CURRENT_STATUS["active_job"]:
#              CURRENT_STATUS["active_job"]["status"] = status
#              if message: CURRENT_STATUS["active_job"]["message"] = message
#              if step: CURRENT_STATUS["active_job"]["step"] = step
#              if progress is not None: CURRENT_STATUS["active_job"]["progress"] = progress
#              if render_size:
#                  CURRENT_STATUS["active_job"].update(render_size)
#     elif status is None and job_id is None:
#         # Reset
#         CURRENT_STATUS["is_busy"] = False
#         CURRENT_STATUS["active_job"] = None


# def _build_ai_job_snapshot(job_id: str, job: Dict[str, Any]) -> Dict[str, Any]:
#     """Return a monitor-friendly snapshot of an AI render job."""
#     status = str(job.get("status") or "queued").lower()
#     progress = int(job.get("progress", 0) or 0)
#     queued_at = job.get("queued_at")
#     started_at = job.get("started_at")
#     completed_at = job.get("completed_at")
#     failed_at = job.get("failed_at")
#     details = job.get("details") or {}
#     if not isinstance(details, dict):
#         details = {"raw": details}

#     return {
#         "job_id": job_id,
#         "type": job.get("type") or job.get("render_type") or "ai render",
#         "display_type": job.get("display_type") or "ai render",
#         "render_type": job.get("render_type") or "AI_RENDER",
#         "status": status,
#         "project_id": job.get("project_id", 0),
#         "gallery_id": job.get("gallery_id", 0),
#         "user_id": job.get("user_id", 0),
#         "progress": progress,
#         "message": job.get("message") or "AI render job queued.",
#         "current_step": job.get("current_step") or ("completed" if status == "completed" else status),
#         "queued_at": queued_at,
#         "started_at": started_at,
#         "completed_at": completed_at,
#         "failed_at": failed_at,
#         "error": job.get("error"),
#         "image_url": job.get("image_url"),
#         "output_filename": job.get("output_filename"),
#         "input_filename": job.get("input_filename"),
#         "input_width": job.get("input_width"),
#         "input_height": job.get("input_height"),
#         "input_resolution": job.get("input_resolution"),
#         "output_width": job.get("output_width"),
#         "output_height": job.get("output_height"),
#         "output_resolution": job.get("output_resolution"),
#         "output_size_bytes": job.get("output_size_bytes"),
#         "output_size_mb": job.get("output_size_mb"),
#         "render_size": job.get("render_size"),
#         "details": details,
#         "queue_source": job.get("queue_source", "External AI Engine"),
#         "is_ai_render": True,
#         "engine_name": "External 4K + AI Engine",
#         "engine_url": os.getenv("AI_RENDER_ENGINE_URL", "embedded://external-4k-ai-main"),
#         "estimated_eta_minutes": job.get("estimated_eta_minutes"),
#         "estimated_wait_minutes": job.get("estimated_wait_minutes"),
#         "completed": status == "completed",
#         "failed": status in {"failed", "error"},
#     }


# def _get_ai_jobs_snapshot() -> List[Dict[str, Any]]:
#     with queue_lock:
#         snapshot = [
#             _build_ai_job_snapshot(job_id, dict(job))
#             for job_id, job in active_jobs.items()
#         ]

#     # Fetch external jobs and merge
#     if REROUTE_ENGINE_URL:
#         try:
#             headers = {}
#             if REROUTE_ENGINE_API_KEY:
#                 headers["X-API-Key"] = REROUTE_ENGINE_API_KEY
#             resp = requests.get(f"{REROUTE_ENGINE_URL.rstrip('/')}/queue-status-public", headers=headers, timeout=2.0)
#             if resp.status_code == 200:
#                 external_jobs = resp.json().get("jobs", [])
#                 snapshot_dict = {job["job_id"]: job for job in snapshot}
#                 for ext_job in external_jobs:
#                     ext_job["is_external"] = True
#                     snapshot_dict[ext_job["job_id"]] = ext_job
#                 snapshot = list(snapshot_dict.values())
#         except Exception as e:
#             print(f"[WARNING] Failed to fetch external jobs for monitoring: {e}")

#     def _sort_key(item: Dict[str, Any]):
#         status_rank = 0 if item["status"] in {"processing", "queued"} else 1
#         queued_at = item.get("queued_at") or ""
#         started_at = item.get("started_at") or ""
#         return (status_rank, queued_at, started_at, item["job_id"])

#     snapshot.sort(key=_sort_key)

#     queued = sum(1 for job in snapshot if job["status"] == "queued")
#     processing = sum(1 for job in snapshot if job["status"] == "processing")
#     completed = sum(1 for job in snapshot if job["status"] == "completed")
#     failed = sum(1 for job in snapshot if job["status"] in {"failed", "error"})
#     queued_jobs = [job for job in snapshot if job["status"] == "queued"]

#     for idx, job in enumerate(queued_jobs, start=1):
#         job["queue_position"] = idx
#         if job.get("estimated_eta_minutes") is None:
#             job["estimated_eta_minutes"] = round(max(1.0, idx * 2.0), 1)
#         job["estimated_wait_minutes"] = job["estimated_eta_minutes"]

#     return snapshot


# def _graceful_shutdown(signum, frame):
#     """
#     Graceful shutdown handler - sends error status to all active jobs.
#     Called when Ctrl+C or terminal close is detected.
#     """
#     global is_shutting_down
    
#     if is_shutting_down:
#         return  # Already shutting down
    
#     is_shutting_down = True
    
#     print(f"\n{COLOR_YELLOW}{'='*80}")
#     print(f"[WARN]  SHUTDOWN SIGNAL RECEIVED - Initiating graceful shutdown...")
#     print(f"{'='*80}{COLOR_RESET}\n")
    
#     # 1. Stop accepting new jobs
#     print(f"🛑 Stopping new job acceptance...")
    
#     # Get current queue status
#     with queue_lock:
#         queued_jobs = [(job_id, info) for job_id, info in active_jobs.items() if info.get('status') == 'queued']
#         processing_jobs = [(job_id, info) for job_id, info in active_jobs.items() if info.get('status') == 'processing']
#         total_jobs = len(queued_jobs) + len(processing_jobs)
    
#     # Print queue summary
#     print(f"\n[DB] Current Queue Status:")
#     print(f"   Total Active Jobs: {total_jobs}")
#     print(f"   Queued Jobs: {len(queued_jobs)}")
#     print(f"   Processing Jobs: {len(processing_jobs)}")
    
#     if total_jobs == 0:
#         print(f"\n{COLOR_GREEN}✓ Queue: NULL (No active jobs to cancel){COLOR_RESET}\n")
#     else:
#         print(f"\n📋 Job Details:")
    
#     # 2. Send error status to all queued jobs
#     if queued_jobs:
#         print(f"\n📤 Sending error status to {len(queued_jobs)} queued job(s)...")
#         for job_id, job_info in queued_jobs:
#             job_type = job_info.get('type', 'IMAGE').upper()
#             project_id = job_info.get('project_id', 'N/A')
#             gallery_id = job_info.get('gallery_id', 'N/A')
#             print(f"   → Job: {job_id[:8]}... | Type: {job_type} | Project: {project_id} | Gallery: {gallery_id}")
#             try:
#                 _send_status_update(
#                     project_id=job_info.get('project_id', 0),
#                     gallery_id=job_info.get('gallery_id', 0),
#                     user_id=0,
#                     job_id=job_id,
#                     status="error",
#                     step="shutdown",
#                     progress=0,
#                     message="Sorry, the rendering process was interrupted.",
#                     render_type=job_type
#                 )
#                 print(f"     ✓ Status 4 (Error) sent successfully")
#             except Exception as e:
#                 print(f"     [WARN]  Failed to send status: {e}")
            
#             # UPDATE LOCAL STATE
#             with queue_lock:
#                 if job_id in active_jobs:
#                     active_jobs[job_id]['status'] = 'failed'
#                     active_jobs[job_id]['error'] = 'Server shutdown - job cancelled'
#                     active_jobs[job_id]['failed_at'] = datetime.now().isoformat()
#                     active_jobs[job_id]['message'] = 'Sorry, the rendering process was interrupted.'
    
#     # 3. Send error status to currently processing job
#     if processing_jobs:
#         print(f"\n📤 Sending error status to {len(processing_jobs)} processing job(s)...")
#         for job_id, job_info in processing_jobs:
#             job_type = job_info.get('type', 'IMAGE').upper()
#             project_id = job_info.get('project_id', 'N/A')
#             gallery_id = job_info.get('gallery_id', 'N/A')
#             print(f"   → Job: {job_id[:8]}... | Type: {job_type} | Project: {project_id} | Gallery: {gallery_id}")
#             try:
#                 _send_status_update(
#                     project_id=job_info.get('project_id', 0),
#                     gallery_id=job_info.get('gallery_id', 0),
#                     user_id=0,
#                     job_id=job_id,
#                     status="error",
#                     step="shutdown",
#                     progress=0,
#                     message="Sorry, the rendering process was interrupted.",
#                     render_type=job_type
#                 )
#                 print(f"     ✓ Status 4 (Error) sent successfully")
#             except Exception as e:
#                 print(f"     [WARN]  Failed to send status: {e}")
            
#             # UPDATE LOCAL STATE to ensure monitoring dashboard sees the failure immediately
#             with queue_lock:
#                 if job_id in active_jobs:
#                     active_jobs[job_id]['status'] = 'failed'
#                     active_jobs[job_id]['error'] = 'Server shutdown - render interrupted'
#                     active_jobs[job_id]['failed_at'] = datetime.now().isoformat()
#                     active_jobs[job_id]['message'] = 'Sorry, the rendering process was interrupted.'

    
#     # 4. Signal queue worker to stop
#     print(f"Stopping render worker queue...")
#     try:
#         render_queue.put(None)  # Poison pill
#         ai_service.shutdown()
#     except:
#         pass
    
#     print(f"\n{COLOR_GREEN}[OK] Graceful shutdown complete{COLOR_RESET}")
#     print(f"{'='*80}\n")
    
#     # Don't call sys.exit() - let the server shut down naturally
#     # This prevents SystemExit from propagating through async operations

# FAILED_INPUTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "failed_inputs")
# os.makedirs(FAILED_INPUTS_DIR, exist_ok=True)

# # Maximum number of render attempts before marking a job as permanently failed
# MAX_RENDER_ATTEMPTS = 3

# def _save_failed_input(job_data: dict, error_msg: str, attempt: int):
#     """
#     Saves the failed render input JSON and error details to the failed_inputs/ folder.
#     Files are named based on user_id and project_id for easy identification.
#     """
#     try:
#         user_id = job_data.get('user_id', 'unknown')
#         project_id = job_data.get('project_id', 'unknown')
#         gallery_id = job_data.get('gallery_id', 'unknown')
#         job_id = job_data.get('job_id', 'unknown')
#         job_type = job_data.get('type', 'unknown')
#         timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')

#         # Base name: user_{user_id}_project_{project_id}_{timestamp}
#         base_name = f"user_{user_id}_project_{project_id}_{timestamp_str}"

#         # 1. Save the input JSON payload
#         input_json_path = os.path.join(FAILED_INPUTS_DIR, f"{base_name}_input.json")
#         payload_obj = job_data.get('payload')
#         if payload_obj is not None:
#             # Pydantic model → dict if possible
#             try:
#                 payload_dict = payload_obj.dict() if hasattr(payload_obj, 'dict') else payload_obj
#             except Exception:
#                 payload_dict = str(payload_obj)
#             with open(input_json_path, 'w', encoding='utf-8') as f:
#                 json.dump(payload_dict, f, indent=2, ensure_ascii=False, default=str)
#         else:
#             with open(input_json_path, 'w', encoding='utf-8') as f:
#                 json.dump({"error": "payload was None"}, f, indent=2)

#         # 2. Save the error details text file
#         error_txt_path = os.path.join(FAILED_INPUTS_DIR, f"{base_name}_error.txt")
#         with open(error_txt_path, 'w', encoding='utf-8') as f:
#             f.write(f"{'='*60}\n")
#             f.write(f"RENDER FAILURE REPORT\n")
#             f.write(f"{'='*60}\n\n")
#             f.write(f"Timestamp      : {datetime.now().isoformat()}\n")
#             f.write(f"Job ID         : {job_id}\n")
#             f.write(f"Job Type       : {job_type.upper()}\n")
#             f.write(f"User ID        : {user_id}\n")
#             f.write(f"Project ID     : {project_id}\n")
#             f.write(f"Gallery ID     : {gallery_id}\n")
#             f.write(f"Total Attempts : {attempt}/{MAX_RENDER_ATTEMPTS}\n")
#             f.write(f"\n{'─'*60}\n")
#             f.write(f"ERROR DETAILS:\n")
#             f.write(f"{'─'*60}\n")
#             f.write(f"{error_msg}\n")
#             f.write(f"\n{'='*60}\n")

#         print(f"{COLOR_YELLOW}📁 Failed input saved to: {FAILED_INPUTS_DIR}{COLOR_RESET}")
#         print(f"   📄 Input JSON : {os.path.basename(input_json_path)}")
#         print(f"   📄 Error File : {os.path.basename(error_txt_path)}")

#     except Exception as save_err:
#         print(f"{COLOR_RED}[WARN]  Could not save failed input: {save_err}{COLOR_RESET}")


# def render_queue_worker():
#     """
#     Background worker thread that processes render jobs one at a time from the queue.
#     This prevents multiple render instances from running simultaneously.

#     RETRY PIPELINE:
#       • Each job is attempted up to MAX_RENDER_ATTEMPTS (3) times.
#       • On attempts 1 and 2: failure is logged but NO error status is sent to
#         the external API. The job is silently retried.
#       • On the final attempt (3rd): if it still fails, the error status IS sent,
#         the input JSON and failure reason are saved to failed_inputs/, and
#         the worker moves on to the next job.
#     """
#     print(f"\n{'='*80}\n[RUN] RENDER QUEUE WORKER STARTED (with retry pipeline: max {MAX_RENDER_ATTEMPTS} attempts)\n{'='*80}\n")
    
#     while True:
#         try:
#             # Get next job from queue (blocks until a job is available)
#             job_data = render_queue.get()
            
#             if job_data is None:  # Poison pill to stop the worker
#                 print(f"{COLOR_YELLOW}🛑 Queue worker received shutdown signal{COLOR_RESET}")
#                 break
            
#             # Check if shutting down
#             if is_shutting_down:
#                 print(f"{COLOR_YELLOW}[WARN]  Skipping job due to shutdown{COLOR_RESET}")
#                 render_queue.task_done()
#                 continue
            
#             job_id = job_data['job_id']
#             job_type = job_data['type']  # 'image' or 'video'
            
#             # Update job status to processing
#             with queue_lock:
#                 if job_id in active_jobs:
#                     active_jobs[job_id]['status'] = 'processing'
#                     active_jobs[job_id]['started_at'] = datetime.now().isoformat()
#                     active_jobs[job_id]['progress'] = 0
#                     active_jobs[job_id]['current_step'] = 'starting'
#                     active_jobs[job_id]['message'] = 'Starting render process'
            
#             # UPDATE GLOBAL STATUS
#             _update_global_status(
#                 job_id=job_id, 
#                 status='processing',
#                 step='Starting render process',
#                 progress=0,
#                 message='Initializing engine...',
#                 project_id=job_data.get('project_id'),
#                 gallery_id=job_data.get('gallery_id'),
#                 job_type=job_type
#             )

            
#             print(f"\n{'='*80}\n🎬 PROCESSING JOB FROM QUEUE\n{'='*80}")
#             print(f"Job ID: {job_id}")
#             print(f"Type: {job_type.upper()}")
#             print(f"Queue Size: {render_queue.qsize()} remaining")
#             print(f"Max Attempts: {MAX_RENDER_ATTEMPTS}")
#             print(f"{'='*80}\n")
            
#             # ── RETRY LOOP ──────────────────────────────────────────────
#             job_succeeded = False
#             last_error_msg = ""
            
#             for attempt in range(1, MAX_RENDER_ATTEMPTS + 1):
#                 is_final_attempt = (attempt == MAX_RENDER_ATTEMPTS)
                
#                 try:
#                     if attempt > 1:
#                         print(f"\n{'─'*60}")
#                         print(f"[RUN] RETRY ATTEMPT {attempt}/{MAX_RENDER_ATTEMPTS} for Job {job_id}")
#                         print(f"{'─'*60}\n")
                        
#                         # Brief pause before retry to let resources settle
#                         time.sleep(3)
                        
#                         # Reset job status for retry
#                         with queue_lock:
#                             if job_id in active_jobs:
#                                 active_jobs[job_id]['status'] = 'processing'
#                                 active_jobs[job_id]['progress'] = 0
#                                 active_jobs[job_id]['current_step'] = f'retry_{attempt}'
#                                 active_jobs[job_id]['message'] = f'Retrying render (attempt {attempt}/{MAX_RENDER_ATTEMPTS})...'
                        
#                         _update_global_status(
#                             job_id=job_id,
#                             status='processing',
#                             step=f'Retry attempt {attempt}/{MAX_RENDER_ATTEMPTS}',
#                             progress=0,
#                             message=f'Retrying render (attempt {attempt}/{MAX_RENDER_ATTEMPTS})...',
#                             project_id=job_data.get('project_id'),
#                             gallery_id=job_data.get('gallery_id'),
#                             job_type=job_type
#                         )
                    
#                     # Execute the appropriate render function
#                     # suppress_error_status=True on non-final attempts:
#                     #   → prevents sending error status to API during retries
#                     #   → re-raises exception so retry loop can catch it
#                     # suppress_error_status=False on final attempt:
#                     #   → normal error handling (sends error status to API)
#                     suppress = not is_final_attempt
                    
#                     if job_type == 'image':
#                         _background_render_task(
#                             job_data['project_id'],
#                             job_data['gallery_id'],
#                             job_data['payload'],
#                             job_id,
#                             job_data['user_id'],
#                             suppress_error_status=suppress
#                         )
#                     elif job_type == 'video':
#                         _background_video_render_task(
#                             job_data['project_id'],
#                             job_data['gallery_id'],
#                             job_data['payload'],
#                             job_id,
#                             job_data['user_id'],
#                             suppress_error_status=suppress
#                         )
                    
#                     # If we reach here, the render succeeded
#                     job_succeeded = True
                    
#                     # Mark job as completed
#                     with queue_lock:
#                         if job_id in active_jobs:
#                             active_jobs[job_id]['status'] = 'completed'
#                             active_jobs[job_id]['completed_at'] = datetime.now().isoformat()
#                             active_jobs[job_id]['progress'] = 100
#                             active_jobs[job_id]['current_step'] = 'completed'
#                             active_jobs[job_id]['message'] = 'Render completed successfully'
#                             if attempt > 1:
#                                 active_jobs[job_id]['message'] = f'Render completed successfully (after {attempt} attempts)'
                    
#                     # UPDATE GLOBAL STATUS - FINISHED
#                     _update_global_status(job_id=job_id, status='completed')

#                     if attempt > 1:
#                         print(f"\n{COLOR_GREEN}[OK] Job {job_id} completed successfully on attempt {attempt}/{MAX_RENDER_ATTEMPTS}{COLOR_RESET}\n")
#                     else:
#                         print(f"\n{COLOR_GREEN}[OK] Job {job_id} completed successfully{COLOR_RESET}\n")
                    
#                     break  # Exit retry loop on success
                    
#                 except Exception as e:
#                     last_error_msg = str(e)
                    
#                     if not is_final_attempt:
#                         # ── NOT THE FINAL ATTEMPT: log but DO NOT send fail status ──
#                         print(f"\n{COLOR_YELLOW}[WARN]  Job {job_id} failed on attempt {attempt}/{MAX_RENDER_ATTEMPTS}: {last_error_msg}{COLOR_RESET}")
#                         print(f"{COLOR_YELLOW}   → Will retry ({MAX_RENDER_ATTEMPTS - attempt} attempt(s) remaining)...{COLOR_RESET}\n")
#                         # Do NOT send status update — silent retry
#                         continue
#                     else:
#                         # ── FINAL ATTEMPT FAILED: send fail status + save to failed_inputs ──
#                         print(f"\n{COLOR_RED}{'='*80}")
#                         print(f"❌ Job {job_id} PERMANENTLY FAILED after {MAX_RENDER_ATTEMPTS} attempts")
#                         print(f"   Last error: {last_error_msg}")
#                         print(f"{'='*80}{COLOR_RESET}\n")
                        
#                         # Mark job as failed
#                         with queue_lock:
#                             if job_id in active_jobs:
#                                 active_jobs[job_id]['status'] = 'failed'
#                                 active_jobs[job_id]['error'] = last_error_msg
#                                 active_jobs[job_id]['failed_at'] = datetime.now().isoformat()
#                                 active_jobs[job_id]['progress'] = 0
#                                 active_jobs[job_id]['current_step'] = 'failed'
#                                 active_jobs[job_id]['message'] = f'Render failed after {MAX_RENDER_ATTEMPTS} attempts: {last_error_msg}'
#                                 active_jobs[job_id]['total_attempts'] = MAX_RENDER_ATTEMPTS
                        
#                         # UPDATE GLOBAL STATUS - FAILED (only on final attempt)
#                         _update_global_status(job_id=job_id, status='failed')
                        
#                         # Save the failed input JSON + error details to failed_inputs/
#                         import traceback
#                         full_error_msg = f"{last_error_msg}\n\nFull Traceback:\n{traceback.format_exc()}"
#                         _save_failed_input(job_data, full_error_msg, attempt)
                        
#                         print(f"{COLOR_RED}❌ Job {job_id} failed permanently. Moving to next job.{COLOR_RESET}\n")
            
#             # ── End of retry loop ────────────────────────────────────────
#             render_queue.task_done()

                
#         except Exception as e:
#             print(f"{COLOR_RED}❌ Queue worker error: {str(e)}{COLOR_RESET}")
#             import traceback
#             traceback.print_exc()

# # Start the queue worker thread when the application starts
# queue_worker_thread = Thread(target=render_queue_worker, daemon=True)
# queue_worker_thread.start()

# # --- AI RENDER UTILITY FUNCTIONS ---

# # AI Worker logic moved to AIRenderService

# # --- GRACEFUL SHUTDOWN HANDLERS ---
# # We use FastAPI's shutdown event instead of manual signal handling
# # to avoid conflicts with Uvicorn's signal handling.



# print(f"{COLOR_GREEN}[OK] Graceful shutdown handlers registered via FastAPI{COLOR_RESET}")
# print(f"   Press Ctrl+C to gracefully shutdown the server\n")

# # --- DATABASE INITIALIZATION ---
# # Automatically create tables if they don't exist in the render database
# try:
#     print(f"[DB] Initializing database: render database")
#     print(f"   Database URL: {session.DATABASE_URL}")
#     models.Base.metadata.create_all(bind=session.engine)
#     print(f"[OK] Database tables ready (created or already exist)")
# except Exception as e:
#     print(f"[WARN]  Warning: Could not initialize database tables: {e}")
#     print(f"   You may need to run 'python init_database.py' manually")
#     # Don't fail the application startup if database is not available
#     # Some endpoints may still work without database

# # --- APP INITIALIZATION ---
# app = FastAPI(title="Z_Realty 4K Render Backend",
# docs_url="/docs",
# openapi_url="/openapi.json",
# root_path="/render")

# # --- LOGGING CONFIGURATION ---
# class EndpointFilter(logging.Filter):
#     def filter(self, record: logging.LogRecord) -> bool:
#         # Filter out 200 OK responses for /queue-status to reduce noise
#         return record.getMessage().find("GET /queue-status") == -1

# async def poll_external_engine_status():
#     if not REROUTE_ENGINE_URL:
#         return
#     import httpx
#     import asyncio
#     while not is_shutting_down:
#         await asyncio.sleep(3)
#         external_jobs_exist = False
#         with queue_lock:
#             for job_id, info in active_jobs.items():
#                 if info.get('processing_externally') and info.get('status') not in ('completed', 'failed', 'error'):
#                     external_jobs_exist = True
#                     break
        
#         if not external_jobs_exist:
#             continue
            
#         try:
#             async with httpx.AsyncClient() as client:
#                 headers = {}
#                 if REROUTE_ENGINE_API_KEY:
#                     headers["X-API-Key"] = REROUTE_ENGINE_API_KEY
                
#                 resp = await client.get(
#                     f"{REROUTE_ENGINE_URL.rstrip('/')}/queue-status-public", 
#                     headers=headers, 
#                     timeout=5.0
#                 )
#                 if resp.status_code == 200:
#                     data = resp.json()
#                     ext_jobs = data.get("jobs", [])
#                     ext_jobs_dict = {j["job_id"]: j for j in ext_jobs}
                    
#                     with queue_lock:
#                         for job_id, local_info in active_jobs.items():
#                             if local_info.get('processing_externally') and local_info.get('status') not in ('completed', 'failed', 'error'):
#                                 if job_id in ext_jobs_dict:
#                                     ext_job = ext_jobs_dict[job_id]
#                                     new_status = ext_job.get("status")
#                                     new_step = ext_job.get("step") or ext_job.get("current_step")
#                                     new_progress = ext_job.get("progress")
#                                     new_message = ext_job.get("message")
                                    
#                                     if new_status != local_info.get("status") or new_step != local_info.get("current_step") or new_progress != local_info.get("progress"):
#                                         local_info["status"] = new_status
#                                         local_info["current_step"] = new_step
#                                         local_info["progress"] = new_progress
#                                         local_info["message"] = new_message
                                        
#                                         if new_status == 'completed':
#                                             local_info["completed_at"] = datetime.now().isoformat()
                                        
#                                         asyncio.get_event_loop().run_in_executor(
#                                             None, 
#                                             _send_status_update,
#                                             local_info.get("project_id", 0),
#                                             local_info.get("gallery_id", 0),
#                                             local_info.get("user_id", 0),
#                                             job_id,
#                                             new_status,
#                                             new_step,
#                                             new_progress,
#                                             new_message,
#                                             local_info.get("type", local_info.get("render_type", "IMAGE"))
#                                         )
#         except Exception:
#             pass

# @app.on_event("startup")
# async def configure_logging():
#     # Filter out /queue-status endpoint logs from uvicorn access logger
#     logging.getLogger("uvicorn.access").addFilter(EndpointFilter())
    
#     # --- AI Render Service Setup ---
#     ai_service.setup(active_jobs, queue_lock, _send_status_update, _update_global_status)
#     ai_service.upload_to_blob_fallback = _upload_to_blob_storage
    
#     # Start polling external jobs
#     asyncio.create_task(poll_external_engine_status())

# @app.on_event("shutdown")
# def shutdown_event():
#     """
#     Handle application shutdown.
#     """
#     _graceful_shutdown(None, None)

# # --- Include Auth Router ---
# from app.routers import auth
# app.include_router(auth.router, prefix="/auth", tags=["authentication"])

# # --- API Key Authentication Dependency ---
# # This ensures only authenticated users with valid API keys can access render endpoints
# CurrentUser = Annotated[models.User, Depends(auth_service.get_user_from_api_key)]

# # --- CONFIGURATION ---
# # --- CONFIGURATION ---
# # SCRIPT_DIR must be defined first — ASSET_DOWNLOAD_DIR is anchored to it.
# SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# # Use SCRIPT_DIR as the anchor so ASSET_DOWNLOAD_DIR is always an absolute path.
# # This ensures _get_cached_asset_path never rejects a valid cached file due to
# # a CWD mismatch, which was causing repeated re-downloads of the same asset.
# _raw_asset_download_dir = os.getenv("ASSET_DOWNLOAD_DIR", "asset_downloads")
# ASSET_DOWNLOAD_DIR = (
#     _raw_asset_download_dir
#     if os.path.isabs(_raw_asset_download_dir)
#     else os.path.join(SCRIPT_DIR, _raw_asset_download_dir)
# )
# GLB_OUTPUT_DIR = os.getenv("GLB_OUTPUT_DIR", "output_glb")
# RENDER_OUTPUT_DIR = os.getenv("RENDER_OUTPUT_DIR", "output_renders")
# GODOT_LOGS_DIR = os.getenv("GODOT_LOGS_DIR", "godot_logs")
# RUNTIME_DIR = os.path.join(tempfile.gettempdir(), "zrealty_backend_runtime")
# ASSET_URL_MAP_FILE = os.path.join(ASSET_DOWNLOAD_DIR, "asset_url_map.json")
# ENABLE_VIDEO_CULLING = True  # Set to False to skip VideoSceneOptimizer for video renders
# VIDEO_RENDER_MAX_CHUNK_SECONDS = float(os.getenv("VIDEO_RENDER_MAX_CHUNK_SECONDS", "10"))

# # --- GODOT CONFIGURATION ---
# # Check for both mono and non-mono versions
# # Only use mono exe if GodotSharp assemblies directory actually exists
# GODOT_BIN_DIR = os.path.join(SCRIPT_DIR, "godot", "bin")
# GODOT_SHARP_DIR = os.path.join(GODOT_BIN_DIR, "GodotSharp")
# DEFAULT_GODOT_EXE_MONO = os.path.join(GODOT_BIN_DIR, "godot.windows.editor.x86_64.mono.console.exe")
# DEFAULT_GODOT_EXE_PLAIN = os.path.join(GODOT_BIN_DIR, "godot.windows.editor.x86_64.console.exe")

# # Prefer plain (non-mono) exe first; only use mono if GodotSharp assemblies are present
# if os.path.exists(DEFAULT_GODOT_EXE_PLAIN):
#     DEFAULT_GODOT_EXE = DEFAULT_GODOT_EXE_PLAIN
# elif os.path.exists(DEFAULT_GODOT_EXE_MONO) and os.path.isdir(GODOT_SHARP_DIR):
#     DEFAULT_GODOT_EXE = DEFAULT_GODOT_EXE_MONO
# elif os.path.exists(DEFAULT_GODOT_EXE_MONO):
#     # Mono exe exists but GodotSharp is missing - use it anyway but warn
#     print(f"{COLOR_YELLOW}[WARN]  WARNING: Mono Godot exe found but GodotSharp directory is missing at: {GODOT_SHARP_DIR}")
#     print(f"   This may cause '.NET assemblies not found' errors.{COLOR_RESET}")
#     DEFAULT_GODOT_EXE = DEFAULT_GODOT_EXE_MONO
# else:
#     DEFAULT_GODOT_EXE = DEFAULT_GODOT_EXE_PLAIN  # Will fail later with a clear "file not found"

# GODOT_EXE = os.getenv("GODOT_EXE", DEFAULT_GODOT_EXE)
# GODOT_PROJECT_PATH = os.getenv("GODOT_PROJECT_PATH", os.path.join(SCRIPT_DIR, "godot_project"))

# # --- CREATE DIRECTORIES ---
# os.makedirs(ASSET_DOWNLOAD_DIR, exist_ok=True)
# os.makedirs(GLB_OUTPUT_DIR, exist_ok=True)
# os.makedirs(RENDER_OUTPUT_DIR, exist_ok=True)
# os.makedirs(GODOT_LOGS_DIR, exist_ok=True)
# os.makedirs(RUNTIME_DIR, exist_ok=True)

# # --- API & TOKENS ---
# # --- API & TOKENS ---
# SAS_TOKEN = os.getenv("AZURE_SAS_TOKEN", "sv=2024-11-04&ss=bfqt&srt=sco&sp=rwdlacupiytfx&se=2026-11-18T20:34:53Z&st=2025-09-12T12:19:53Z&spr=https,http&sig=KNQs7rhe81AeQfnd%2BS4QMPWWo55VbNICTufFVYe5KhA%3D")
# UPLOAD_API_URL = (
#     os.getenv("UPLOAD_API_URL", "").strip()
#     or os.getenv("AI_CALLBACK_API_URL", "").strip()
#     or "http://216.48.178.133:4050/api/v1/Project/UploadProjectGalleryAI"
# )
# VIDEO_UPLOAD_API_URL = (
#     os.getenv("VIDEO_UPLOAD_API_URL", "").strip()
#     or os.getenv("AI_VIDEO_CALLBACK_API_URL", "").strip()
#     or "http://216.48.178.133:4050/api/v1/Project/UploadProjectVideoAI"
# )
# # Internal monitor callback URL. Keep legacy names supported because the .env
# # file in this repo uses MONITOR_UPLOAD_API_URL / MONITOR_VIDEO_UPLOAD_API_URL.
# MONITORING_API_URL = (
#     os.getenv("MONITOR_UPLOAD_API_URL", "").strip()
#     or os.getenv("MONITORING_API_URL", "").strip()
# )
# API_KEY = os.getenv("API_KEY", "")
# USE_API_KEY = os.getenv("USE_API_KEY", "false").lower() == "true"



# # --- CORS MIDDLEWARE ---
# origins = [
#     "http://localhost:3000",
#     "http://localhost:3001",
#     "http://localhost:3002",
#     "http://127.0.0.1:8005",
#     "http://127.0.0.1:8006",
# ]
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=origins,
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# # --- STATUS MONITORING API ---
# # Read-only endpoints for external monitoring system
# from api_status import create_status_router
# from scene_optimizer import SceneOptimizer, CULLING_LOGS_DIR
# from scene_optimizer_video import VideoSceneOptimizer

# status_router = create_status_router(active_jobs, render_queue, queue_lock, CURRENT_STATUS, ai_render_queue=ai_service.render_queue)
# # Mount authenticated monitoring routes under /monitoring so they do not collide
# # with the public AI status endpoints defined later in this file.
# app.include_router(status_router, prefix="/monitoring")

# # --- DATA MODELS (SCHEMA) ---
# class Vector3(BaseModel):
#     x: float
#     y: float
#     z: float

# class ColorRGB(BaseModel):
#     r: float
#     g: float
#     b: float

# class DirectionalLight(BaseModel):
#     intensity: float
#     position: Vector3
#     color: ColorRGB
#     target: Optional[Vector3] = None
#     cast_shadow: Optional[bool] = True

# class AmbientLight(BaseModel):
#     intensity: float

# class GenerationPayload(BaseModel):
#     floor_plan_data: str
#     fov: float = 45.0
#     directional_light: DirectionalLight
#     ambient_light: Optional[AmbientLight] = None
#     aspect_ratio: str
#     render_quality: str
#     # Some callers omit this field for image renders; keep it optional so the
#     # request can still start instead of failing validation up front.
#     timestamp: int = 0
#     hdri_filename: Optional[str] = "kloppenheim_06_puresky_4k.exr"
#     environment_url: Optional[str] = None
#     # Camera data fields (optional, will be extracted from JSON)
#     threejs_camera: Optional[Dict[str, Any]] = None
#     enable_status_updates: bool = True  # If True, send status updates to UploadGallerAI endpoint at each step
#     draft_render: Any = True
    
#     class Config:
#         extra = "allow"  # Allow extra fields that aren't in the model

# # --- VIDEO-SPECIFIC DATA MODELS ---
# class VideoAnimation(BaseModel):
#     duration_seconds: float = 5.0
#     fps: int = 30
#     camera_position_start: Optional[Vector3] = None
#     camera_position_end: Optional[Vector3] = None
#     camera_target_start: Optional[Vector3] = None
#     camera_target_end: Optional[Vector3] = None
#     # Frame-by-frame keyframes array (if provided by frontend)
#     # Each keyframe: {"frame": int, "position": {"x": float, "y": float, "z": float}, "target": {"x": float, "y": float, "z": float}}
#     keyframes: Optional[List[Dict[str, Any]]] = None

# class VideoDirectionalLight(BaseModel):
#     intensity: float = 1.0
#     position: Optional[Vector3] = None
#     color: Optional[ColorRGB] = None
#     target: Optional[Vector3] = None
#     cast_shadow: Optional[bool] = False
#     shadow_map_size: Optional[int] = 512
#     shadow_bias: Optional[float] = 0

# class VideoGenerationPayload(BaseModel):
#     """Payload model for video rendering with camera animation data."""
#     video_animation: Optional[VideoAnimation] = None  # Optional — if missing, Godot auto-generates a horizontal pan
#     threejs_camera: Optional[Dict[str, Any]] = None
#     coordinate_system: Optional[str] = "right_handed_y_up"
#     target_coordinate_system: Optional[str] = "right_handed_z_up"
#     ambient_light: Optional[AmbientLight] = None
#     directional_light: Optional[VideoDirectionalLight] = None
#     aspect_ratio: str = "16:9"
#     render_quality: str = "hd"
#     timestamp: int = 0
#     floor_plan_data: Optional[Any] = None  # Can be string or dict — both formats accepted
#     environment_url: Optional[str] = None
#     enable_status_updates: bool = True  # If True, send status updates to UploadGallerAI endpoint at each step
#     draft_render: Any = True
    
#     class Config:
#         extra = "allow"  # Allow extra fields that aren't in the model

# # --- RENDER SIZE HELPERS ---
# QUALITY_RESOLUTION_MAP: Dict[str, tuple[int, int]] = {
#     "12K": (12288, 6642),
#     "8K": (7680, 4320),
#     "6K": (6144, 3321),
#     "4K": (3840, 2160),
#     "2K": (2560, 1440),
#     "QUAD HD": (2048, 1080),
#     "1080P": (1920, 1080),
#     "FHD": (1920, 1080),
#     "FULL HD": (1920, 1080),
#     "HD": (1280, 720),
# }


# def _parse_aspect_ratio_value(raw_value: Any) -> Optional[float]:
#     """Parse an aspect ratio as width / height from common encodings."""
#     if raw_value is None:
#         return None

#     if isinstance(raw_value, (int, float)):
#         value = float(raw_value)
#         return value if value > 0.0 else None

#     if isinstance(raw_value, str):
#         text = raw_value.strip()
#         if not text:
#             return None
#         if ":" in text:
#             parts = text.split(":")
#             if len(parts) == 2:
#                 try:
#                     width = float(parts[0].strip())
#                     height = float(parts[1].strip())
#                     if width > 0.0 and height > 0.0:
#                         return width / height
#                 except Exception:
#                     return None
#         try:
#             value = float(text)
#             return value if value > 0.0 else None
#         except Exception:
#             return None

#     if isinstance(raw_value, dict):
#         width = raw_value.get("width", raw_value.get("w"))
#         height = raw_value.get("height", raw_value.get("h"))
#         if width is not None and height is not None:
#             try:
#                 width_f = float(width)
#                 height_f = float(height)
#                 if width_f > 0.0 and height_f > 0.0:
#                     return width_f / height_f
#             except Exception:
#                 pass
#         if "x" in raw_value and "y" in raw_value:
#             try:
#                 width_f = float(raw_value.get("x"))
#                 height_f = float(raw_value.get("y"))
#                 if width_f > 0.0 and height_f > 0.0:
#                     return width_f / height_f
#             except Exception:
#                 pass

#     return None


# def _normalize_render_quality(render_quality: Any) -> str:
#     return str(render_quality or "").replace("_", " ").strip().upper()


# def _resolve_base_render_resolution(render_quality: Any, width: Any = None, height: Any = None) -> tuple[int, int]:
#     quality = _normalize_render_quality(render_quality)
#     base_width, base_height = QUALITY_RESOLUTION_MAP.get(quality, (1920, 1080))

#     if width is not None:
#         try:
#             base_width = int(width)
#         except Exception:
#             pass
#     if height is not None:
#         try:
#             base_height = int(height)
#         except Exception:
#             pass

#     return max(base_width, 1), max(base_height, 1)


# def _apply_aspect_ratio_to_resolution(width: int, height: int, aspect_ratio: Any) -> tuple[int, int]:
#     aspect_ratio_value = _parse_aspect_ratio_value(aspect_ratio)
#     if not aspect_ratio_value:
#         return max(width, 1), max(height, 1)

#     current_ratio = float(width) / float(max(height, 1))
#     if abs(aspect_ratio_value - current_ratio) <= 0.001:
#         return max(width, 1), max(height, 1)

#     base_long_side = max(width, height)
#     if aspect_ratio_value >= 1.0:
#         width = base_long_side
#         height = int(round(float(base_long_side) / aspect_ratio_value))
#     else:
#         height = base_long_side
#         width = int(round(float(base_long_side) * aspect_ratio_value))

#     return max(width, 1), max(height, 1)


# def _build_render_size_info(
#     output_width: int,
#     output_height: int,
#     *,
#     input_width: Optional[int] = None,
#     input_height: Optional[int] = None,
#     render_quality: Optional[Any] = None,
#     aspect_ratio: Optional[Any] = None,
# ) -> Dict[str, Any]:
#     output_width = max(int(output_width), 1)
#     output_height = max(int(output_height), 1)
#     size_info: Dict[str, Any] = {
#         "output_width": output_width,
#         "output_height": output_height,
#         "output_resolution": f"{output_width}x{output_height}",
#         "render_size": {
#             "output": {
#                 "width": output_width,
#                 "height": output_height,
#                 "resolution": f"{output_width}x{output_height}",
#             }
#         },
#     }

#     if input_width is not None and input_height is not None:
#         input_width = max(int(input_width), 1)
#         input_height = max(int(input_height), 1)
#         size_info["input_width"] = input_width
#         size_info["input_height"] = input_height
#         size_info["input_resolution"] = f"{input_width}x{input_height}"
#         size_info["render_size"]["input"] = {
#             "width": input_width,
#             "height": input_height,
#             "resolution": f"{input_width}x{input_height}",
#         }

#     if render_quality is not None:
#         size_info["render_quality"] = str(render_quality)
#         size_info["render_size"]["render_quality"] = str(render_quality)

#     if aspect_ratio is not None:
#         size_info["aspect_ratio"] = str(aspect_ratio)
#         size_info["render_size"]["aspect_ratio"] = str(aspect_ratio)

#     return size_info


# def _calculate_image_render_size(payload: GenerationPayload) -> Dict[str, Any]:
#     width, height = _resolve_base_render_resolution(
#         payload.render_quality,
#         getattr(payload, "width", None),
#         getattr(payload, "height", None),
#     )
#     width, height = _apply_aspect_ratio_to_resolution(width, height, getattr(payload, "aspect_ratio", None))
#     return _build_render_size_info(
#         width,
#         height,
#         render_quality=payload.render_quality,
#         aspect_ratio=getattr(payload, "aspect_ratio", None),
#     )


# def _calculate_video_render_size(payload: VideoGenerationPayload) -> Dict[str, Any]:
#     width, height = _resolve_base_render_resolution(
#         payload.render_quality,
#         getattr(payload, "width", None),
#         getattr(payload, "height", None),
#     )
#     return _build_render_size_info(
#         width,
#         height,
#         render_quality=payload.render_quality,
#         aspect_ratio=getattr(payload, "aspect_ratio", None),
#     )


# def _calculate_ai_render_size(input_width: int, input_height: int, target_long_side: int) -> Dict[str, Any]:
#     input_width = max(int(input_width), 1)
#     input_height = max(int(input_height), 1)
#     target_long_side = max(int(target_long_side), 1)

#     long_side = max(input_width, input_height)
#     if long_side >= target_long_side:
#         output_width = input_width
#         output_height = input_height
#     else:
#         scale = target_long_side / long_side
#         output_width = max(8, int(input_width * scale))
#         output_height = max(8, int(input_height * scale))

#     return _build_render_size_info(
#         output_width,
#         output_height,
#         input_width=input_width,
#         input_height=input_height,
#         render_quality="AI_RENDER",
#         aspect_ratio=f"{input_width}:{input_height}",
#     )


# def _get_file_size_info(file_path: Optional[str]) -> Dict[str, Any]:
#     """Return file size metadata for a rendered artifact on disk."""
#     if not file_path:
#         return {}
#     try:
#         if not os.path.exists(file_path):
#             return {}
#         size_bytes = os.path.getsize(file_path)
#         return {
#             "output_size_bytes": size_bytes,
#             "output_size_mb": round(size_bytes / (1024 * 1024), 2),
#         }
#     except Exception:
#         return {}


# def _extract_job_render_size(job: Dict[str, Any]) -> Dict[str, Any]:
#     size_keys = (
#         "input_width",
#         "input_height",
#         "input_resolution",
#         "output_width",
#         "output_height",
#         "output_resolution",
#         "output_size_bytes",
#         "output_size_mb",
#         "render_quality",
#         "aspect_ratio",
#         "render_size",
#     )
#     return {key: job.get(key) for key in size_keys if job.get(key) is not None}

# # --- PAYLOAD VALIDATION FUNCTIONS ---

# def _validate_image_payload(payload: GenerationPayload) -> tuple[bool, str]:
#     """
#     Validates image generation payload to ensure all required fields are present and valid.
    
#     Returns:
#         (is_valid, error_message) - True if valid, False with error message if invalid
#     """
#     try:
#         # Check required fields
#         if not payload.floor_plan_data:
#             return False, "Missing required field: floor_plan_data"

#         # Validate floor_plan_data is valid JSON
#         try:
#             if isinstance(payload.floor_plan_data, str):
#                 json.loads(payload.floor_plan_data)
#         except json.JSONDecodeError:
#             return False, "Invalid floor_plan_data: not valid JSON"
        
#         # Validate directional_light
#         if not payload.directional_light:
#             return False, "Missing required field: directional_light"
        
#         # Validate render quality
#         valid_qualities = ['HD', 'FULL HD', 'FULLHD', 'QUAD HD', 'QUADHD', '2K', '4K', '6K', '8K', '12K', 'FAST_PREVIEW', 'LOW', 'MEDIUM']
#         if payload.render_quality.upper() not in valid_qualities:
#             return False, f"Invalid render_quality: {payload.render_quality}. Must be one of: {', '.join(valid_qualities)}"
        
#         # Validate aspect ratio
#         if not payload.aspect_ratio:
#             return False, "Missing required field: aspect_ratio"

#         return True, ""
        
#     except Exception as e:
#         return False, f"Validation error: {str(e)}"

# def _validate_video_payload(payload: VideoGenerationPayload) -> tuple[bool, str]:
#     """
#     Validates video generation payload to ensure all required fields are present and valid.
    
#     Returns:
#         (is_valid, error_message) - True if valid, False with error message if invalid
#     """
#     try:
#         # Check video_animation — if not present, Godot will auto-generate a horizontal pan
#         if not payload.video_animation:
#             print("ℹ️  No video_animation provided — Godot will auto-generate horizontal pan from camera position")
#             return True, ""
        
#         # Validate duration and fps
#         if payload.video_animation.duration_seconds <= 0:
#             return False, f"Invalid duration_seconds: {payload.video_animation.duration_seconds}. Must be greater than 0"
        
#         if payload.video_animation.fps <= 0:
#             return False, f"Invalid fps: {payload.video_animation.fps}. Must be greater than 0"
        
#         # Validate duration is reasonable (max 60 seconds)
#         if payload.video_animation.duration_seconds > 60:
#             return False, f"Invalid duration_seconds: {payload.video_animation.duration_seconds}. Maximum allowed is 60 seconds"
        
#         # Validate fps is reasonable (max 60 fps)
#         if payload.video_animation.fps > 60:
#             return False, f"Invalid fps: {payload.video_animation.fps}. Maximum allowed is 60 fps"
        
#         # Check for camera animation data (keyframes or start/end positions)
#         has_keyframes = payload.video_animation.keyframes and len(payload.video_animation.keyframes) > 0
#         has_start_end = (
#             payload.video_animation.camera_position_start is not None and
#             payload.video_animation.camera_position_end is not None
#         )
        
#         if not has_keyframes and not has_start_end:
#             # No keyframes or start/end — Godot will auto-generate from camera position
#             print("ℹ️  No keyframes or start/end positions — Godot will auto-generate from camera")
        
#         # Validate render quality
#         valid_qualities = ['HD', 'FULL HD', 'FULLHD', 'QUAD HD', 'QUADHD', '2K', '4K', '6K', '8K', '12K', 'FAST_PREVIEW', 'LOW', 'MEDIUM']
#         if payload.render_quality.upper() not in valid_qualities:
#             return False, f"Invalid render_quality: {payload.render_quality}. Must be one of: {', '.join(valid_qualities)}"
        
#         # Validate aspect ratio
#         if not payload.aspect_ratio:
#             return False, "Missing required field: aspect_ratio"
        
#         return True, ""
        
#     except Exception as e:
#         return False, f"Validation error: {str(e)}"

# # --- ASSET HANDLING & URL LOCALIZATION ---

# def _load_asset_url_map() -> dict:
#     """Loads the URL-to-local-file mapping from JSON file."""
#     if os.path.exists(ASSET_URL_MAP_FILE):
#         try:
#             with open(ASSET_URL_MAP_FILE, 'r', encoding='utf-8') as f:
#                 return json.load(f)
#         except (json.JSONDecodeError, IOError) as e:
#             print(f"{COLOR_YELLOW}Warning: Could not load asset URL map: {e}. Starting with empty map.{COLOR_RESET}")
#             return {}
#     return {}

# def _save_asset_url_map(url_map: dict):
#     """Saves the URL-to-local-file mapping to JSON file."""
#     try:
#         with open(ASSET_URL_MAP_FILE, 'w', encoding='utf-8') as f:
#             json.dump(url_map, f, indent=2, ensure_ascii=False)
#     except IOError as e:
#         print(f"{COLOR_YELLOW}Warning: Could not save asset URL map: {e}{COLOR_RESET}")

# def _get_cached_asset_path(url: str) -> Optional[str]:
#     """
#     Checks if the URL is in asset_url_map.json and the mapped local file exists.
#     Supports both dict format { "local_path": "...", "asset_id": "..." }
#     and the legacy plain-string format for backward compat.
#     THREAD-SAFE: Uses a lock to ensure atomic read/write.
#     """
#     with map_lock:
#         url_map = _load_asset_url_map()
#         if url in url_map:
#             entry = url_map[url]
#             local_path = entry["local_path"] if isinstance(entry, dict) else entry
#             if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
#                 return local_path
#             else:
#                 print(f"{COLOR_YELLOW}  ↳ Cached entry found but file is missing on disk, removing from map: {os.path.basename(local_path)}{COLOR_RESET}")
#                 del url_map[url]
#                 _save_asset_url_map(url_map)
#         return None

# def _add_to_asset_url_map(url: str, local_path: str, asset_id: str = ""):
#     """
#     Adds / updates a URL entry in asset_url_map.json.
#     Format:
#       { "frontend_url": "<original LR/blob URL>", "local_path": "<local file>", "asset_id": "<id or blank>" }
#     - GLB assets:  asset_id holds the numeric ID from the frontend JSON.
#     - Textures:    asset_id is blank (textures have no ID in the frontend JSON, only direct URLs).
#     THREAD-SAFE: Uses a lock to ensure atomic read-update-write.
#     """
#     with map_lock:
#         url_map = _load_asset_url_map()
#         new_entry = {"frontend_url": url, "local_path": local_path, "asset_id": asset_id}
#         existing = url_map.get(url)
#         # Preserve a previously stored asset_id if the caller doesn't supply one
#         if not asset_id and isinstance(existing, dict) and existing.get("asset_id"):
#             new_entry["asset_id"] = existing["asset_id"]
#         if existing != new_entry:
#             url_map[url] = new_entry
#             _save_asset_url_map(url_map)

# import struct

# def _check_glb_for_webp(filepath: str) -> bool:
#     """
#     Checks if a GLB file contains the EXT_texture_webp extension.
#     Uses direct binary inspection of the JSON chunk.
#     """
#     if not os.path.exists(filepath):
#         return False
#     try:
#         with open(filepath, 'rb') as f:
#             # Header: magic (4), version (4), length (4)
#             magic = f.read(4)
#             if magic != b'glTF': return False
#             f.read(8) # Skip version and total length
            
#             # Chunk 0 (JSON): length (4), type (4)
#             chunk_length = struct.unpack('<I', f.read(4))[0]
#             chunk_type = f.read(4)
#             if chunk_type != b'JSON': return False
            
#             json_bytes = f.read(chunk_length)
#             json_text = json_bytes.decode('utf-8', errors='ignore')
            
#             # Look for the extension string
#             return "EXT_texture_webp" in json_text
#     except Exception as e:
#         print(f"{COLOR_YELLOW}  [WARN]  Warning: Could not inspect GLB {os.path.basename(filepath)}: {e}{COLOR_RESET}")
#         return False

# def _repair_glb_file(filepath: str):
#     """
#     Repairs WebP textures in place.
#     """
#     try:
#         print(f"{COLOR_YELLOW}  [WARN]  WebP detected in GLB, but automatic repair is disabled in the Godot pipeline:{COLOR_RESET} {os.path.basename(filepath)}")
#         return False
#     except Exception as e:
#         print(f"{COLOR_RED}  ❌ Repair error: {e}{COLOR_RESET}")
#         return False

# def _check_glb_for_draco(filepath: str) -> bool:
#     """
#     Checks if a GLB file contains Draco mesh compression.
#     """
#     if not os.path.exists(filepath):
#         return False
#     try:
#         with open(filepath, 'rb') as f:
#             magic = f.read(4)
#             if magic != b'glTF':
#                 return False
#             f.read(8)
#             chunk_length = struct.unpack('<I', f.read(4))[0]
#             chunk_type = f.read(4)
#             if chunk_type != b'JSON':
#                 return False
#             json_bytes = f.read(chunk_length)
#             json_text = json_bytes.decode('utf-8', errors='ignore')
#             return "KHR_draco_mesh_compression" in json_text
#     except Exception as e:
#         print(f"{COLOR_YELLOW}  [WARN]  Warning: Could not inspect GLB {os.path.basename(filepath)}: {e}{COLOR_RESET}")
#         return False

# def _build_godot_compatible_glb_path(source_url: str, local_path: str) -> str:
#     """
#     Builds a stable output path for a Godot-compatible GLB copy.
#     """
#     compat_dir = os.path.abspath(os.path.join(ASSET_DOWNLOAD_DIR, "_godot_compatible"))
#     os.makedirs(compat_dir, exist_ok=True)

#     source_key = source_url or local_path or ""
#     digest = hashlib.sha1(source_key.encode("utf-8", errors="ignore")).hexdigest()[:10]
#     base_name = os.path.splitext(os.path.basename(local_path))[0] or "asset"
#     return os.path.join(compat_dir, f"{digest}_{base_name}_nodraco.glb")

# def _convert_draco_glb_to_plain_glb(input_path: str, output_path: str) -> tuple[bool, str]:
#     """
#     Uses a small Python helper to decode Draco-compressed meshes into a plain GLB.
#     """
#     script_path = os.path.join(SCRIPT_DIR, "tools", "decompress_draco_glb.py")
#     if not os.path.exists(script_path):
#         return False, f"Missing Draco conversion helper: {script_path}"

#     try:
#         os.makedirs(os.path.dirname(output_path), exist_ok=True)
#         timeout_seconds = int(os.getenv("DRACO_GLB_CONVERSION_TIMEOUT_SECONDS", "300"))
#         result = subprocess.run(
#             [sys.executable, script_path, input_path, output_path],
#             cwd=SCRIPT_DIR,
#             capture_output=True,
#             text=True,
#             timeout=timeout_seconds,
#         )
#         if result.returncode != 0:
#             message = (result.stderr or result.stdout or "").strip()
#             if not message:
#                 message = f"Node helper exited with code {result.returncode}"
#             return False, message
#         if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
#             return False, "Draco conversion finished but output file was not created"
#         return True, ""
#     except FileNotFoundError:
#         return False, "Python interpreter was not found"
#     except subprocess.TimeoutExpired:
#         return False, f"Draco conversion timed out after {timeout_seconds}s"
#     except Exception as e:
#         return False, str(e)

# def _convert_glb_webp_textures_to_png(input_path: str, output_path: str) -> tuple[bool, str]:
#     """
#     Rewrites embedded WebP textures in a GLB as PNGs.
#     """
#     script_path = os.path.join(SCRIPT_DIR, "tools", "convert_glb_webp_to_png.py")
#     if not os.path.exists(script_path):
#         return False, f"Missing WebP conversion helper: {script_path}"

#     try:
#         os.makedirs(os.path.dirname(output_path), exist_ok=True)
#         result = subprocess.run(
#             [sys.executable, script_path, input_path, output_path],
#             cwd=SCRIPT_DIR,
#             capture_output=True,
#             text=True,
#             timeout=int(os.getenv("GLB_WEBP_CONVERSION_TIMEOUT_SECONDS", "300")),
#         )
#         if result.returncode != 0:
#             message = (result.stderr or result.stdout or "").strip()
#             if not message:
#                 message = f"Python helper exited with code {result.returncode}"
#             return False, message
#         if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
#             return False, "WebP conversion finished but output file was not created"
#         return True, ""
#     except subprocess.TimeoutExpired:
#         return False, "WebP conversion timed out"
#     except Exception as e:
#         return False, str(e)

# def _ensure_godot_compatible_glb(source_url: str, local_path: str) -> str:
#     """
#     Converts Draco-compressed GLBs and embedded WebP textures into a Godot-friendly GLB.
#     Returns the original path if conversion is not needed or fails.
#     """
#     if not local_path or not isinstance(local_path, str):
#         return local_path
#     if not local_path.lower().endswith(".glb"):
#         return local_path
#     draco_present = _check_glb_for_draco(local_path)
#     webp_present = _check_glb_for_webp(local_path)
#     if not draco_present and not webp_present:
#         return local_path

#     current_path = local_path
#     show_details = os.getenv("ASSETS_DETAIL_PRINT", "false").lower() == "true"

#     # Use _godot_compatible directory for caching if either Draco or WebP is present
#     compatible_path = _build_godot_compatible_glb_path(source_url, local_path)

#     # Check if a valid cached compatible file already exists
#     if os.path.exists(compatible_path) and os.path.getsize(compatible_path) > 0:
#         if not _check_glb_for_draco(compatible_path) and not _check_glb_for_webp(compatible_path):
#             # Already converted and cached — use it directly
#             if source_url:
#                 _add_to_asset_url_map(source_url, compatible_path)
#             return compatible_path

#     if draco_present:
#         compatible_path = _build_godot_compatible_glb_path(source_url, local_path)
#         needs_draco_convert = (
#             not os.path.exists(compatible_path)
#             or os.path.getsize(compatible_path) == 0
#             or _check_glb_for_draco(compatible_path)
#             or _check_glb_for_webp(compatible_path)
#         )
#         if needs_draco_convert:
#             if show_details:
#                 print(f"{COLOR_YELLOW}  ↻ Draco GLB detected, converting for Godot:{COLOR_RESET} {os.path.basename(local_path)}")
#             ok, error = _convert_draco_glb_to_plain_glb(current_path, compatible_path)
#             if not ok:
#                 print(f"{COLOR_YELLOW}  [WARN]  Draco conversion failed, keeping original GLB:{COLOR_RESET} {os.path.basename(local_path)} ({error})")
#                 return local_path
#         current_path = compatible_path

#     if _check_glb_for_webp(current_path):
#         # If we haven't moved to compatible_path yet (WebP-only, no Draco), copy first
#         if current_path == local_path:
#             import shutil
#             os.makedirs(os.path.dirname(compatible_path), exist_ok=True)
#             shutil.copy2(local_path, compatible_path)
#             current_path = compatible_path

#         png_tmp_path = current_path + ".pngtmp"
#         if show_details:
#             print(f"{COLOR_YELLOW}  ↻ WebP textures detected, converting to PNG:{COLOR_RESET} {os.path.basename(current_path)}")
#         ok, error = _convert_glb_webp_textures_to_png(current_path, png_tmp_path)
#         if ok:
#             os.replace(png_tmp_path, current_path)
#         else:
#             print(f"{COLOR_YELLOW}  [WARN]  WebP texture conversion failed, keeping current GLB:{COLOR_RESET} {os.path.basename(current_path)} ({error})")
#             if os.path.exists(png_tmp_path):
#                 try:
#                     os.remove(png_tmp_path)
#                 except OSError:
#                     pass
#             return local_path

#     if source_url:
#         _add_to_asset_url_map(source_url, current_path)  # asset_id unknown here
#     return current_path

# def _convert_glb_path_to_url(glb_path: str) -> str:
#     """
#     Converts a relative GLB path to a full Azure Blob Storage URL.
    
#     Input: glb-assets/Sofa/Double_Seater_Sofa/Nordic_Sofa_HP.glb
#     Output: https://zrealtystoragedev.blob.core.windows.net/glb-assets/Sofa/Double_Seater_Sofa/Nordic_Sofa_HP.glb
    
#     Does NOT modify the filename (trusts input).
#     """
#     if not glb_path or not isinstance(glb_path, str):
#         return ""

#     if glb_path.startswith("http"):
#         return glb_path
    
#     # Add base URL prefix
#     base_url = "https://zrealtystoragedev.blob.core.windows.net/"
    
#     # Remove leading slash if present
#     if glb_path.startswith("/"):
#         glb_path = glb_path[1:]
    
#     # Construct full URL
#     full_url = f"{base_url}{glb_path}"
    
#     return full_url

# def _timed_download_worker(url: str, context: str = "", asset_id: str = ""):
#     """
#     Downloads a single file with retries.
#     Only prints the asset filename when a fresh download starts, and an error
#     line if all attempts fail. Cache hits are silent.
#     """
#     start_time = time.monotonic()
    
#     base_url_prefix = "https://zrealtystoragedev.blob.core.windows.net/"
#     cleaned_url = url.strip().strip('"')
#     if not cleaned_url or not isinstance(cleaned_url, str) or not cleaned_url.startswith(base_url_prefix):
#         return url, "", 0.0, None

#     # ── CACHE CHECK ──────────────────────────────────────────────────────────
#     # If the URL is in asset_url_map.json the asset was previously downloaded —
#     # just verify the file still exists on disk and use it silently.
#     cached_path = _get_cached_asset_path(cleaned_url)
#     if cached_path:
#         if cached_path.lower().endswith('.glb'):
#             cached_path = _ensure_godot_compatible_glb(cleaned_url, cached_path)
#         duration = time.monotonic() - start_time
#         return url, cached_path, duration, None

#     # ── FRESH DOWNLOAD ───────────────────────────────────────────────────────
#     asset_name = os.path.basename(urlparse(cleaned_url).path)
#     print(f"  ↓ Downloading: {asset_name}")

#     last_error = None
#     for attempt in range(3):
#         try:
#             download_url = f"{cleaned_url}?{SAS_TOKEN}" if '?' not in cleaned_url else f"{cleaned_url}&{SAS_TOKEN.lstrip('?')}"
#             req_timeout = int(os.getenv("ASSET_INDIVIDUAL_TIMEOUT_SECONDS", "150"))
#             response = requests.get(download_url, timeout=req_timeout)
#             response.raise_for_status()
            
#             safe_name = asset_name.replace("_LR_", "_HR_")
#             stable_prefix = hashlib.md5(cleaned_url.encode("utf-8", errors="ignore")).hexdigest()[:8]
#             filename = f"{stable_prefix}_{safe_name}"
#             local_path = os.path.abspath(os.path.join(ASSET_DOWNLOAD_DIR, filename))
            
#             with open(local_path, "wb") as f:
#                 f.write(response.content)

#             if cleaned_url.lower().endswith('.glb'):
#                 local_path = _ensure_godot_compatible_glb(cleaned_url, local_path)

#             _add_to_asset_url_map(cleaned_url, local_path, asset_id=asset_id)
#             duration = time.monotonic() - start_time
#             return url, local_path, duration, None

#         except requests.RequestException as e:
#             last_error = e
#             if attempt < 2:
#                 time.sleep(2)
    
#     duration = time.monotonic() - start_time
#     print(f"{COLOR_RED}  ✗ Failed: {asset_name}{COLOR_RESET}")
#     return url, "", duration, last_error

# def _download_asset_with_fallback(original_url: str, use_high_poly: bool = True, asset_id: str = ""):
#     """
#     Downloads a single asset URL and returns (original_url, local_path).
#     The 'use_high_poly' flag is kept for API compatibility.
#     """
#     if not original_url:
#         return "", ""

#     if any(skip in original_url.lower() for skip in ['.svg', 'thumbnail', 'svg_']):
#         return original_url, ""
    
#     if original_url.lower().endswith('.glb'):
#         file_type = "GLB"
#     elif "/textures/" in original_url.lower():
#         file_type = "Texture"
#     elif original_url.lower().endswith('.obj'):
#         file_type = "OBJ"
#     else:
#         file_type = "Asset"
    
#     url, local_path, duration, error = _timed_download_worker(
#         original_url, file_type, asset_id=asset_id
#     )
#     return original_url, local_path

# def _parse_texture_urls_from_mtl(mtl_filepath: str) -> Set[str]:
#     """Scans a local MTL file and extracts all external texture URLs."""
#     urls = set()
#     texture_keys = ('map_Kd', 'map_Ks', 'map_Ka', 'map_Bump', 'bump', 'map_d', 'map_Pr', 'map_Pm', 'map_Ke', 'map_bump', 'map_disp', 'map_Disp')
#     try:
#         with open(mtl_filepath, 'r', encoding='utf-8', errors='ignore') as f:
#             for line in f:
#                 line = line.strip()
#                 parts = line.split()
#                 if not parts or parts[0] not in texture_keys:
#                     continue
                
#                 for part in parts[1:]:
#                     cleaned_part = part.strip('\"\'')
#                     if cleaned_part.startswith(('http://', 'https://')):
#                         urls.add(cleaned_part)
#                         break
#     except Exception as e:
#         print(f"{COLOR_RED}ERROR parsing MTL for texture URLs {os.path.basename(mtl_filepath)}: {e}{COLOR_RESET}")
#     return urls

# def _update_mtl_texture_paths(mtl_filepath: str, download_map: dict):
#     """Updates texture URLs inside a local MTL file to local basenames."""
#     print(f"{COLOR_YELLOW}  -> Finalizing texture paths in: {os.path.basename(mtl_filepath)}{COLOR_RESET}")
#     try:
#         with open(mtl_filepath, 'r', encoding='utf-8', errors='ignore') as f:
#             lines = f.readlines()
        
#         new_lines, was_updated = [], False
#         for line in lines:
#             for original_url, local_path in list(download_map.items()):
#                 if local_path and original_url in line:
#                     basename = os.path.basename(local_path)
#                     line = line.replace(original_url, basename)
#                     was_updated = True
#                     break
#             new_lines.append(line)
        
#         if was_updated:
#             with open(mtl_filepath, 'w', encoding='utf-8') as f:
#                 f.writelines(new_lines)
#     except Exception as e:
#         print(f"{COLOR_RED}ERROR updating MTL file {mtl_filepath}: {e}{COLOR_RESET}")

# # --- Paste this corrected function into your main.py ---

# def _download_and_localize_assets(plan: dict, use_high_res: bool = True, logger: Optional[RenderLogger] = None):
#     """
#     ID-driven workflow:
#     1. Recursively collect asset3D_Id / textureLibrary_Id references from the plan.
#     2. Also collect direct material texture URLs such as mapUrl / normalUrl / roughnessUrl.
#     3. Resolve those IDs through the master catalogues and download only the matched URLs.
#     4. Parse downloaded MTLs to find and download any secondary texture assets.
#     5. Update MTLs to use local texture paths.
#     6. Update the main floorplan dict to use local paths.
#     """
#     if logger:
#         logger.start_asset_download()
    
#     # --- FETCH MASTER DATA (Once per run) ---
#     _fetch_master_data_from_api()
    
#     all_urls_to_find = set()
    
#     # Store ID -> URL mappings so we can look them up later during replacement
#     asset_id_to_url = {}
#     texture_id_to_urls = {}
#     input_url_to_hp = {}
#     direct_texture_keys = {
#         "mapUrl",
#         "normalUrl",
#         "roughnessUrl",
#         "metalnessUrl",
#         "aoUrl",
#         "emissionUrl",
#         "displacementUrl",
#         "bumpUrl",
#         "alphaUrl",
#         "heightMapUrl",
#     }

#     def _register_id_mapping(mapping: dict, key, value) -> None:
#         """Store an ID lookup using both raw and stringified keys."""
#         if key is None or value is None:
#             return
#         mapping[key] = value
#         key_str = str(key)
#         mapping[key_str] = value
#         try:
#             key_int = int(key_str)
#         except (TypeError, ValueError):
#             key_int = None
#         if key_int is not None:
#             mapping[key_int] = value

#     def _add_texture_url_candidate(url_set: set, value) -> None:
#         """Collect a direct texture URL or a list of texture URLs."""
#         if value is None:
#             return
#         if isinstance(value, list):
#             for item in value:
#                 _add_texture_url_candidate(url_set, item)
#             return
#         if isinstance(value, str):
#             cleaned = value.strip().strip('"').strip("'")
#             if cleaned and cleaned.lower() not in ("null", "none"):
#                 if cleaned.startswith('textures/') or cleaned.startswith('glb-assets/'):
#                     cleaned = f"https://zrealtystoragedev.blob.core.windows.net/{cleaned}"
#                 url_set.add(cleaned)
    
#     # If plan is a string, parse it first so we can scan the object structure
#     is_json_string = False
#     if isinstance(plan, str):
#         try:
#             plan_data = json.loads(plan)
#             is_json_string = True
#         except Exception:
#             plan_data = plan # Fallback
#     else:
#         plan_data = plan

#     layer_to_scan = plan_data

#     # Helper to recursively find asset and texture IDs in the plan.
#     def collect_ids_recursive(data_obj, url_set):
#         if isinstance(data_obj, str):
#             # Try to parse stringified JSON first (e.g. floor_plan_data is often stringified)
#             try:
#                 parsed_json = json.loads(data_obj)
#                 if isinstance(parsed_json, (dict, list)):
#                     collect_ids_recursive(parsed_json, url_set)
#             except (json.JSONDecodeError, TypeError):
#                 pass
#         elif isinstance(data_obj, list):
#             for item in data_obj:
#                 collect_ids_recursive(item, url_set)
#         elif isinstance(data_obj, dict):
#             asset_urls_dict = data_obj.get('asset_urls', {}) if isinstance(data_obj.get('asset_urls'), dict) else {}

#             a_id = (
#                 data_obj.get('asset3D_Id')
#                 or data_obj.get('assetId_3D')
#                 or data_obj.get('assetId')
#                 or asset_urls_dict.get('asset3D_Id')
#                 or asset_urls_dict.get('assetId_3D')
#                 or asset_urls_dict.get('assetId')
#             )
#             if a_id is not None:
#                 resolved_asset_url = _resolve_asset_url(a_id, show_details=True)
#                 if resolved_asset_url:
#                     _register_id_mapping(asset_id_to_url, a_id, resolved_asset_url)
#                     url_set.add(resolved_asset_url)

#             t_id = data_obj.get('textureLibrary_Id')
#             if not t_id and isinstance(asset_urls_dict, dict):
#                 t_id = asset_urls_dict.get('textureLibrary_Id')
#             if t_id is not None:
#                 resolved_urls = _resolve_texture_urls(t_id, show_details=True)
#                 if resolved_urls:
#                     _register_id_mapping(texture_id_to_urls, t_id, resolved_urls)
#                     for u in resolved_urls:
#                         url_set.add(u)

#             # Collect direct material texture URLs so floor/ceiling materials
#             # are localized even when they do not use textureLibrary_Id.
#             for key, value in data_obj.items():
#                 if key in direct_texture_keys:
#                     _add_texture_url_candidate(url_set, value)

#             # Recursively process nested values to find more IDs.
#             for key, value in data_obj.items():
#                 collect_ids_recursive(value, url_set)

#     # Phase 1: Collect ALL URLs from the entire plan structure
#     collect_ids_recursive(layer_to_scan, all_urls_to_find)

#     if not all_urls_to_find:
#         print(f"\n{COLOR_YELLOW}--- No asset/texture references found. Skipping downloads. ---{COLOR_RESET}")
#         if logger:
#             logger.end_asset_download()
#         if is_json_string:
#             return json.dumps(plan_data)
#         return plan_data

#     # Build reverse map: resolved_url → asset_id so each GLB download worker
#     # knows the originating asset ID to store in asset_url_map.json.
#     url_to_asset_id: dict = {}
#     for aid, aurl in asset_id_to_url.items():
#         url_to_asset_id.setdefault(aurl, str(aid))

#     # Phase 2: Download all collected assets
#     print(f"\n{COLOR_BLUE}--- Asset Download Step 1: Found {len(all_urls_to_find)} unique assets. Starting download... ---{COLOR_RESET}")

#     download_map = {}
    
#     # Use manual executor management instead of 'with' block to allow non-blocking shutdown
#     executor = ThreadPoolExecutor(max_workers=16)
#     try:
#         futures = {
#             executor.submit(
#                 _download_asset_with_fallback,
#                 url,
#                 use_high_res,
#                 url_to_asset_id.get(url, ""),  # asset_id for GLBs; blank for textures
#             ): url
#             for url in all_urls_to_find
#         }
        
#         # --- IMPLEMENT ASSET DOWNLOAD TIMEOUT (Configurable via .env, default 2.5 mins) ---
#         start_time = time.monotonic()
#         # Default to 150 seconds if not set
#         TIMEOUT_SECONDS = int(os.getenv("ASSET_DOWNLOAD_TIMEOUT_SECONDS", "150"))
#         PRINT_INTERVAL = 10
#         next_print_time = start_time + PRINT_INTERVAL
        
#         completed_count = 0
#         total_count = len(futures)
        
#         # Collect futures as they complete, but with a total loop timeout
#         # We use a while loop to check for timeout and print status
#         pending = set(futures.keys())
        
#         while pending:
#             # Calculate time remaining
#             now = time.monotonic()
#             elapsed = now - start_time
            
#             if elapsed > TIMEOUT_SECONDS:
#                 print(f"\n{COLOR_YELLOW}[WARN]  ASSET DOWNLOAD TIMEOUT REACHED ({TIMEOUT_SECONDS}s){COLOR_RESET}")
#                 print(f"  Proceeding with {completed_count}/{total_count} assets downloaded.")
#                 # Force non-blocking shutdown to proceed immediately
#                 # cancel_futures=True stops pending tasks. wait=False ignores running tasks.
#                 executor.shutdown(wait=False, cancel_futures=True)
#                 break
                
#             # Wait for a short interval to allow printing status
#             # We use wait() on the whole set, but with a short timeout
#             # Note: wait() returns (done, not_done)
#             done_now, pending_now = wait(pending, timeout=min(1.0, TIMEOUT_SECONDS - elapsed))
            
#             # Process completed ones
#             for future in done_now:
#                 pending.remove(future)
#                 completed_count += 1
#                 try:
#                     original_url, final_path = future.result()
#                     download_map[original_url] = final_path
                    
#                     # Log asset
#                     if logger and original_url:
#                         asset_name = os.path.basename(original_url)
#                         cached = final_path and os.path.exists(final_path) if final_path else False
#                         logger.add_asset(original_url, asset_name, final_path, cached)
#                 except Exception as e:
#                     print(f"{COLOR_RED}Error in initial download worker for {futures[future]}: {e}{COLOR_RESET}")

#             # Print Status update
#             if time.monotonic() >= next_print_time:
#                 show_details = os.getenv("ASSETS_DETAIL_PRINT", "false").lower() == "true"
#                 if not show_details:
#                     print(f"⏳ Assets downloading ({int(elapsed)}s)...")
#                 next_print_time = time.monotonic() + PRINT_INTERVAL
                
#     finally:
#         # Ensure executor is eventually cleaned up, but don't block if we already shut it down
#         try:
#             # If we broke due to timeout, executor might be shut down already or running zombie threads.
#             # Calling shutdown again with wait=False is safe.
#             executor.shutdown(wait=False, cancel_futures=True)
#         except Exception:
#             pass

#     # Phase 3: Parse MTLs and download secondary item/hole textures
#     item_textures_to_download = set()
#     mtl_files_to_update = []
    
#     # Find which of our downloaded files are MTLs
#     for url, local_path in download_map.items():
#         if local_path and local_path.lower().endswith('.mtl'):
#             mtl_files_to_update.append(local_path)
#             print(f"\n{COLOR_BLUE}--- Parsing MTL '{os.path.basename(local_path)}' for associated textures... ---{COLOR_RESET}")
#             texture_urls_in_mtl = _parse_texture_urls_from_mtl(local_path)
#             if texture_urls_in_mtl:
#                 # Find textures that we haven't already downloaded
#                 new_textures = texture_urls_in_mtl - download_map.keys()
#                 if new_textures:
#                     print(f"  Found {len(new_textures)} new texture(s) to download.")
#                     item_textures_to_download.update(new_textures)
#                 else:
#                     print("  All associated textures already accounted for.")
#             else:
#                 print("  No external texture URLs found in this MTL.")
    
#     if item_textures_to_download:
#         print(f"\n{COLOR_BLUE}--- Asset Download Step 2: Downloading {len(item_textures_to_download)} additional textures discovered in MTLs ---{COLOR_RESET}")
#         with ThreadPoolExecutor(max_workers=16) as executor:
#             futures = {executor.submit(_download_asset_with_fallback, url, use_high_res): url for url in item_textures_to_download}
#             for future in as_completed(futures):
#                 try:
#                     original_url, final_path = future.result()
#                     download_map[original_url] = final_path
                    
#                     # Log asset
#                     if logger and original_url:
#                         asset_name = os.path.basename(original_url)
#                         cached = final_path and os.path.exists(final_path) if final_path else False
#                         logger.add_asset(original_url, asset_name, final_path, cached)
#                 except Exception as e:
#                     print(f"{COLOR_RED}Error in texture download worker for {futures[future]}: {e}{COLOR_RESET}")

#     # Step 4: Update downloaded MTL files with local texture paths
#     if mtl_files_to_update:
#         print(f"\n{COLOR_BLUE}--- Asset Processing: Updating MTL files with local texture paths ---{COLOR_RESET}")
#         for mtl_path in mtl_files_to_update:
#             _update_mtl_texture_paths(mtl_path, download_map)

#     # Step 5: Replace all URLs in the main floorplan dict with their local paths
#     def replace_urls_in_plan(data_obj):
#         if isinstance(data_obj, str):
#             # Case 1: The string is a direct URL match
#             test_url = input_url_to_hp.get(data_obj, data_obj)
#             if isinstance(test_url, list):
#                 # If resolved to multiple HR textures, return local paths as a JSON list or similar
#                 # For MTL/Texture mapping, we usually expect a single local path. 
#                 # If it's a list, the renderer might need it differently. 
#                 # For now, let's return the first available local path if it's a single string context.
#                 local_paths = [download_map[u] for u in test_url if u in download_map]
#                 if local_paths:
#                     return local_paths[0] if len(local_paths) == 1 else json.dumps(local_paths)
#                 return data_obj
#             elif test_url in download_map:
#                 return download_map[test_url]
                
#             # Case 2: The string is a relative GLB path - convert to full URL and look up
#             if data_obj.startswith('glb-assets/'):
#                 test_url = input_url_to_hp.get(data_obj)
#                 if isinstance(test_url, list):
#                     local_paths = [download_map[u] for u in test_url if u in download_map]
#                     if local_paths: return json.dumps(local_paths) if len(local_paths) > 1 else local_paths[0]
#                 elif not test_url:
#                     full_url = _convert_glb_path_to_url(data_obj)
#                     test_url = input_url_to_hp.get(full_url, full_url)
                
#                 if not isinstance(test_url, list) and test_url in download_map:
#                     return download_map[test_url]
#                 elif isinstance(test_url, list):
#                     local_paths = [download_map[u] for u in test_url if u in download_map]
#                     if local_paths: return json.dumps(local_paths) if len(local_paths) > 1 else local_paths[0]
            
#             # Case 3: The string is a relative Texture path - convert to full URL and look up
#             if data_obj.startswith('textures/'):
#                  full_url = f"https://zrealtystoragedev.blob.core.windows.net/{data_obj}"
#                  if full_url in download_map:
#                      return download_map[full_url]
            
#             # Case 4: The string is a JSON list of URLs or a stringified JSON object
#             stripped = data_obj.strip()
#             if (stripped.startswith('[') and stripped.endswith(']')) or (stripped.startswith('{') and stripped.endswith('}')):
#                 try:
#                     parsed = json.loads(data_obj)
#                     if isinstance(parsed, (list, dict)):
#                         # Recursively process the parsed JSON
#                         replaced = replace_urls_in_plan(parsed)
#                         return json.dumps(replaced)
#                 except (json.JSONDecodeError, TypeError):
#                     pass # Fallback to original string if parsing fails
#             return data_obj # Return original string if no match
#         elif isinstance(data_obj, list):
#             return [replace_urls_in_plan(item) for item in data_obj]
#         elif isinstance(data_obj, dict):
#             # Process dictionary values
#             new_dict = {}
#             for k, v in data_obj.items():
#                 new_dict[k] = replace_urls_in_plan(v)
            
#             # --- NEW: Inject Local Paths based on IDs ---
            
#             # Explicitly handle isColorEdited to ensure it's a boolean (Fix for "asset colours not changing")
#             if 'isColorEdited' in new_dict:
#                 val = new_dict['isColorEdited']
#                 if isinstance(val, str):
#                     new_dict['isColorEdited'] = val.lower() == 'true'
            
#             # Also check if it's inside 'asset_urls' commonly used in frontend
#             if 'asset_urls' in new_dict and isinstance(new_dict['asset_urls'], dict):
#                 au = new_dict['asset_urls']
#                 if 'isColorEdited' in au:
#                     val = au['isColorEdited']
#                     if isinstance(val, str):
#                         au['isColorEdited'] = val.lower() == 'true'
            
#             # 1. Check direct keys
#             a_id = new_dict.get('asset3D_Id') or new_dict.get('assetId_3D') or new_dict.get('assetId')

#             # 2. Check nested inside 'asset_urls'
#             if not a_id and 'asset_urls' in new_dict and isinstance(new_dict['asset_urls'], dict):
#                 a_id = (
#                     new_dict['asset_urls'].get('asset3D_Id')
#                     or new_dict['asset_urls'].get('assetId_3D')
#                     or new_dict['asset_urls'].get('assetId')
#                 )

#             if a_id and a_id in asset_id_to_url:
#                 url = asset_id_to_url[a_id]
#                 if url in download_map:
#                      local_path = download_map[url]
#                      # Godot prefers forward slashes for paths even on Windows
#                      local_path = local_path.replace("\\", "/")
                     
#                      # Update Top-level keys
#                      if 'glb_Url' in new_dict: new_dict['glb_Url'] = local_path
#                      if 'GLB_File_URL' in new_dict: new_dict['GLB_File_URL'] = local_path
#                      if 'glb_url' in new_dict: new_dict['glb_url'] = local_path
                     
#                      # Update Nested 'asset_urls' keys
#                      if 'asset_urls' in new_dict and isinstance(new_dict['asset_urls'], dict):
#                          au = new_dict['asset_urls']
#                          if 'glb_Url' in au: au['glb_Url'] = local_path
#                          if 'GLB_File_URL' in au: au['GLB_File_URL'] = local_path
#                          if 'glb_url' in au: au['glb_url'] = local_path

#             # If this dict has a textureLibrary_Id, we can update/inject 'texture_urls'
#             if 'textureLibrary_Id' in new_dict:
#                 t_id = new_dict['textureLibrary_Id']
#                 if t_id in texture_id_to_urls:
#                     urls = texture_id_to_urls[t_id]
#                     # Filter to those that were successfully downloaded
#                     local_texture_paths = []
#                     for u in urls:
#                         if u in download_map:
#                             local_texture_paths.append(download_map[u])
                    
#                     if local_texture_paths:
#                         # Inject as 'texture_urls' (or 'textures'?) - using 'texture_urls' as a safe bet for renderer
#                         new_dict['texture_urls'] = local_texture_paths
            
#             return new_dict
#         return data_obj

#     print(f"\n{COLOR_BLUE}--- Finalizing floor plan data with all local asset paths ---{COLOR_RESET}")
    
#     if logger:
#         logger.end_asset_download()
    
#     localized_plan = replace_urls_in_plan(plan_data)
    
#     # Post-processing: fix any broken local paths
#     localized_plan = _fix_broken_local_paths(localized_plan)
    
#     # If original input was a string, return string (for Godot input compatibility)
#     if is_json_string:
#         return json.dumps(localized_plan)
        
#     return localized_plan


# def _fix_broken_local_paths(data, _depth=0):
#     """
#     Recursively walks plan data and fixes local file paths that don't exist.
#     For paths pointing to other project folders (e.g., 'r&d'), tries to:
#     1. Find the same filename in local asset_downloads
#     2. Re-download from API if assetId_3D is available
#     """
#     script_dir = os.path.dirname(os.path.abspath(__file__))
#     local_downloads = os.path.join(script_dir, "asset_downloads")
    
#     if _depth > 20:  # Prevent infinite recursion
#         return data
    
#     if isinstance(data, str):
#         # Check if it looks like a local file path that doesn't exist
#         if (os.sep in data or '/' in data) and not data.startswith('http'):
#             # It's a local path — check common file extensions
#             ext = os.path.splitext(data)[1].lower()
#             if ext in ('.glb', '.obj', '.mtl', '.jpg', '.jpeg', '.png', '.exr', '.hdr'):
#                 if not os.path.exists(data):
#                     filename = os.path.basename(data)
#                     local_candidate = os.path.join(local_downloads, filename).replace("\\", "/")
#                     if os.path.exists(local_candidate):
#                         print(f"  [RUN] Fixed broken path: {filename} (was: ...{data[-40:]})")
#                         return local_candidate
#                     else:
#                         # Fallback: Check if there's a file with a different hash prefix
#                         if "_" in filename:
#                             base_part = filename.split('_', 1)[1]
#                             for f in os.listdir(local_downloads):
#                                 if f.endswith(base_part):
#                                     fallback_path = os.path.join(local_downloads, f).replace("\\", "/")
#                                     print(f"  [RUN] Fixed broken path (fuzzy match): {filename} -> {f}")
#                                     return fallback_path
#                         print(f"  [WARN] Missing asset (not in cache): {filename}")
#         return data
    
#     elif isinstance(data, dict):
#         fixed = {}
#         for key, value in data.items():
#             fixed[key] = _fix_broken_local_paths(value, _depth + 1)
#         return fixed
    
#     elif isinstance(data, list):
#         return [_fix_broken_local_paths(item, _depth + 1) for item in data]
    
#     return data

# def _run_godot_render(job_id: str, payload: GenerationPayload, logger: Optional[RenderLogger] = None) -> str:
#     """
#     Runs the Godot rendering process.
#     1. Localize assets.
#     2. Save input JSON.
#     3. Call Godot headless.
#     4. Return path to output image.
#     """
#     if logger:
#         # Reusing logs for consistency
#         logger.start_glb_creation() 
    
#     print(f"{COLOR_BLUE}--- Starting Godot Render ---{COLOR_RESET}")
    
#     # 1. Localize Assets
#     floor_plan_data = payload.floor_plan_data
#     scene_data = json.loads(floor_plan_data) if isinstance(floor_plan_data, str) else floor_plan_data
#     localized_geometry = _download_and_localize_assets(scene_data, use_high_res=True, logger=logger)
    
#     # 2. Build Godot Input
#     # Convert payload to dict to get camera, lights, etc.
#     godot_input = payload.dict(exclude={'floor_plan_data', 'video_animation'}, exclude_unset=True)
#     # Inject localized geometry
#     godot_input['floor_plan_data'] = localized_geometry
    
#     if payload.environment_url:
#         res = _download_asset_with_fallback(payload.environment_url, use_high_poly=True)
#         if res and res[1]:
#             godot_input['environment_url'] = res[1]

#     # 2.5 Scene Culling — remove rooms/items not visible from camera
#     output_basename = f"{job_id}_render"
#     culling_log_path = os.path.join(CULLING_LOGS_DIR, f"{output_basename}_culling.txt")
#     try:
#         optimizer = SceneOptimizer(log_path=culling_log_path)
#         godot_input = optimizer.cull_scene(godot_input)
#         print(f"[OK] Scene culling complete. Log: {culling_log_path}")
#     except Exception as e:
#         print(f"[WARN] Scene culling failed (continuing without culling): {e}")
    
#     # 3. Save Input JSON
#     input_json_path = os.path.abspath(os.path.join(RUNTIME_DIR, f"{job_id}_godot_input.json"))
#     with open(input_json_path, "w", encoding="utf-8") as f:
#         json.dump(godot_input, f, indent=2)
        
#     output_image_path = os.path.abspath(os.path.join(RENDER_OUTPUT_DIR, f"{job_id}_render.png"))
#     godot_log_path = os.path.join(GODOT_LOGS_DIR, f"{job_id}_godot.log")
    
#     # Call Godot
#     godot_exe = GODOT_EXE
#     project_path = GODOT_PROJECT_PATH
    
#     # Arguments: --path <project> "res://main.tscn" -- <input> <output>
#     # NOTE: Do NOT use --headless for renders; headless disables rendering in Godot 4.x.
#     cmd = [godot_exe]
#     cmd.extend([
#         "--path", project_path,
#         "res://main.tscn",
#         "--",
#         input_json_path,
#         output_image_path
#     ])
    
#     print(f"Executing Godot: {' '.join(cmd)}")

#     all_output_lines: list[str] = []
#     TIMEOUT = 1500  # seconds

#     try:
#         proc = subprocess.Popen(
#             cmd,
#             stdout=subprocess.PIPE,
#             stderr=subprocess.STDOUT,
#             text=True,
#             encoding="utf-8",
#             errors="replace",
#             bufsize=1,
#         )

#         def _stream_godot_output():
#             try:
#                 assert proc.stdout is not None
#                 for raw_line in proc.stdout:
#                     line = raw_line.rstrip("\r\n")
#                     all_output_lines.append(line)
#                     print(f"[Godot] {line}", flush=True)
#             except Exception as stream_exc:
#                 print(f"{COLOR_YELLOW}Warning: Godot stream reader stopped early: {stream_exc}{COLOR_RESET}")

#         reader = Thread(target=_stream_godot_output, daemon=True)
#         reader.start()

#         try:
#             proc.wait(timeout=TIMEOUT)
#         except subprocess.TimeoutExpired:
#             proc.kill()
#             try:
#                 proc.wait(timeout=10)
#             except Exception:
#                 pass
#             raise Exception(f"Godot execution timed out after {TIMEOUT}s")
#         finally:
#             reader.join(timeout=10)

#         with open(godot_log_path, "w", encoding="utf-8") as lf:
#             lf.write("\n".join(all_output_lines))

#         if proc.returncode != 0:
#             print(f"[GodotLog] Godot exited with non-zero code {proc.returncode}")
#             print(f"[GodotLog] Full log saved to: {godot_log_path}")
#             raise Exception(
#                 f"Godot execution failed (exit code {proc.returncode}). See log: {godot_log_path}"
#             )

#     except Exception:
#         if all_output_lines:
#             try:
#                 with open(godot_log_path, "w", encoding="utf-8") as lf:
#                     lf.write("\n".join(all_output_lines))
#             except Exception:
#                 pass
#         raise

#     if not os.path.exists(output_image_path):
#         raise Exception("Godot did not produce an output image.")

#     print(f"{COLOR_GREEN}[OK] Godot Render Success: {output_image_path}{COLOR_RESET}")

#     if logger:
#         logger.end_glb_creation() # Mark step done

#     return output_image_path
# def _find_ffmpeg_path():
#     """
#     Finds ffmpeg executable path on Windows system.
#     Checks local installation first, then common installation locations and PATH.
#     """
#     # Check local installation first (in project directory)
#     local_ffmpeg = os.path.join(os.path.dirname(__file__), "ffmpeg", "bin", "ffmpeg.exe")
#     if os.path.exists(local_ffmpeg):
#         return local_ffmpeg
    
#     # Common ffmpeg installation paths on Windows
#     common_paths = [
#         r"C:\ffmpeg\bin\ffmpeg.exe",
#         r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
#         r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
#         r"C:\tools\ffmpeg\bin\ffmpeg.exe",
#         r"C:\Users\{}\AppData\Local\ffmpeg\bin\ffmpeg.exe".format(os.getenv('USERNAME', '')),
#     ]
    
#     # Check common paths
#     for path in common_paths:
#         if os.path.exists(path):
#             print(f"[OK] Found ffmpeg at: {path}")
#             return path
    
#     # Check if ffmpeg is in PATH
#     try:
#         result = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
#         if result.returncode == 0:
#             print("[OK] Found ffmpeg in system PATH")
#             return 'ffmpeg'  # Return command name if found in PATH
#     except (FileNotFoundError, subprocess.TimeoutExpired):
#         pass
    
#     # Try to find in Program Files
#     program_files_paths = [
#         r"C:\Program Files",
#         r"C:\Program Files (x86)",
#     ]
    
#     for base_path in program_files_paths:
#         if os.path.exists(base_path):
#             try:
#                 for item in os.listdir(base_path):
#                     ffmpeg_path = os.path.join(base_path, item, "bin", "ffmpeg.exe")
#                     if os.path.exists(ffmpeg_path):
#                         print(f"[OK] Found ffmpeg at: {ffmpeg_path}")
#                         return ffmpeg_path
#             except (PermissionError, OSError):
#                 continue
    
#     return None

# def _delete_png_frames(output_dir: str, base_name: str):
#     """
#     Deletes PNG frame files after successful MP4 encoding.
    
#     Args:
#         output_dir: Directory containing PNG frames
#         base_name: Base name of the PNG files (e.g., "render" for "render_0001.png")
#     """
#     try:
#         import glob
#         pattern = os.path.join(output_dir, f"{base_name}_*.png")
#         png_files = sorted(glob.glob(pattern))
        
#         if not png_files:
#             return
        
#         print(f"\n🧹 Cleaning up {len(png_files)} temporary PNG frames...")
#         deleted_count = 0
        
#         for png_file in png_files:
#             try:
#                 os.remove(png_file)
#                 deleted_count += 1
#             except Exception as e:
#                 print(f"[WARN]  Warning: Could not delete {png_file}: {e}")
        
#         if deleted_count > 0:
#             print(f"[OK] Deleted {deleted_count} PNG frame files")
#         else:
#             print(f"[WARN]  No PNG files were deleted")
#     except Exception as e:
#         print(f"[WARN]  Warning: Error during PNG cleanup: {e}")

# def _encode_png_sequence_to_mp4(output_dir: str, base_name: str, output_mp4_path: str, fps: int = 30, delete_frames: bool = True) -> bool:
#     """
#     Encodes PNG image sequence to MP4 video using ffmpeg.
    
#     Args:
#         output_dir: Directory containing PNG frames
#         base_name: Base name of the PNG files (e.g., "render" for "render_0001.png")
#         output_mp4_path: Full path where MP4 should be saved
#         fps: Frame rate for the video (default: 30)
#         delete_frames: Whether to delete PNG frames after successful encoding (default: True)
    
#     Returns:
#         True if encoding succeeded, False otherwise
#     """
#     try:
#         print(f"\n{'='*60}\n🎬 ENCODING PNG SEQUENCE TO MP4\n{'='*60}")
#         print(f"Input directory: {output_dir}")
#         print(f"Base name: {base_name}")
#         print(f"Output MP4: {output_mp4_path}")
#         print(f"Frame rate: {fps} fps")
        
#         # Find all PNG files matching the pattern
#         import glob
#         pattern = os.path.join(output_dir, f"{base_name}_*.png")
#         png_files = sorted(glob.glob(pattern))
        
#         if not png_files:
#             print(f"❌ ERROR: No PNG files found matching pattern: {pattern}")
#             return False
        
#         print(f"[OK] Found {len(png_files)} PNG frames")
        
#         # Find ffmpeg executable
#         ffmpeg_path = _find_ffmpeg_path()
#         if not ffmpeg_path:
#             print("\n" + "="*80)
#             print("❌ ERROR: ffmpeg not found. Please install ffmpeg to encode videos.")
#             print("="*80)
#             print("\n📥 QUICK INSTALLATION (Recommended):")
#             print("   Run this command to automatically download and install ffmpeg:")
#             print("   python install_ffmpeg.py")
#             print("\n📥 MANUAL INSTALLATION OPTIONS:")
#             print("\n1. Download from official website:")
#             print("   https://www.gyan.dev/ffmpeg/builds/")
#             print("   - Download 'ffmpeg-release-essentials.zip'")
#             print("   - Extract to: " + os.path.join(os.path.dirname(__file__), "ffmpeg"))
#             print("\n2. Install via Chocolatey (if installed):")
#             print("   choco install ffmpeg")
#             print("\n3. Install via winget (Windows 10/11):")
#             print("   winget install ffmpeg")
#             print("\n[WARN]  After installation, restart this application.")
#             print("="*80 + "\n")
#             return False
        
#         # Determine frame numbering pattern
#         # Check if frames start from 0000 or 0001
#         first_frame_0000 = os.path.exists(os.path.join(output_dir, f"{base_name}_0000.png"))
#         first_frame_0001 = os.path.exists(os.path.join(output_dir, f"{base_name}_0001.png"))
        
#         if first_frame_0000:
#             # Frames start from 0000
#             input_pattern = os.path.join(output_dir, f"{base_name}_%04d.png")
#             start_number = 0
#         elif first_frame_0001:
#             # Frames start from 0001
#             input_pattern = os.path.join(output_dir, f"{base_name}_%04d.png")
#             start_number = 1
#         else:
#             # Try to find the first frame
#             first_frame = None
#             for i in range(10000):
#                 frame_path = os.path.join(output_dir, f"{base_name}_{i:04d}.png")
#                 if os.path.exists(frame_path):
#                     first_frame = i
#                     break
            
#             if first_frame is None:
#                 print(f"❌ ERROR: Could not find first frame in sequence")
#                 return False
            
#             input_pattern = os.path.join(output_dir, f"{base_name}_%04d.png")
#             start_number = first_frame
#             print(f"   Detected frame numbering starting from: {start_number:04d}")
            
#         # VALIDATE FRAME SEQUENCE (Prevent missing frames/gaps)
#         print(f"🔍 Validating frame sequence ({len(png_files)} frames detected)...")
#         expected_count = len(png_files)
#         # Check for gaps
#         frames_found = []
#         for f in png_files:
#             try:
#                 # Extract number from filename (assuming ..._XXXX.png)
#                 num_part = f[-8:-4] 
#                 if num_part.isdigit():
#                     frames_found.append(int(num_part))
#             except:
#                 pass
        
#         frames_found.sort()
#         if len(frames_found) > 1:
#             # Check for gaps
#             gaps = []
#             for i in range(len(frames_found) - 1):
#                 if frames_found[i+1] != frames_found[i] + 1:
#                     gaps.append((frames_found[i], frames_found[i+1]))
            
#             if gaps:
#                 print(f"[WARN]  WARNING: Found {len(gaps)} gaps in frame sequence!")
#                 print(f"   First gap: between {gaps[0][0]} and {gaps[0][1]}")
#                 print(f"   FFmpeg might skip or shorten the video.")
#                 # Optional: We could fill gaps here, but for now just warn
#             else:
#                 print(f"[OK] Frame sequence is contiguous (Indices {frames_found[0]} to {frames_found[-1]})")
        
#         # Build ffmpeg command - Two step process:
#         # Step 1: Create video from PNG frames (no rotation) → temporary file
#         # Step 2: Rotate the video by 180 degrees → final output
#         temp_video_path = os.path.join(output_dir, f"{base_name}_temp.mp4")
        
#         # Step 1: Create video from PNG frames (no rotation)
#         ffmpeg_cmd_step1 = [
#             ffmpeg_path,
#             '-y',  # Overwrite output file
#             '-start_number', str(start_number),
#             '-framerate', str(fps), # Input framerate
#             '-i', input_pattern,
#             '-r', str(fps), # Output framerate (CRITICAL: Locks output timing)
#             '-c:v', 'libx264',
#             '-pix_fmt', 'yuv420p',
#             '-crf', '18',  # High quality
#             '-preset', 'medium',
#             temp_video_path
#         ]
        
#         print(f"📹 Step 1: Creating video from PNG frames...")
#         print(f"   Command: {' '.join(ffmpeg_cmd_step1)}")
        
#         # Run step 1: Create video
#         result_step1 = subprocess.run(
#             ffmpeg_cmd_step1,
#             capture_output=True,
#             text=True,
#             timeout=1500  # 10 minute timeout for encoding
#         )
        
#         if result_step1.returncode != 0:
#             print(f"❌ ERROR: Step 1 (video creation) failed")
#             print(f"   Return code: {result_step1.returncode}")
#             if result_step1.stderr:
#                 print(f"   Error output: {result_step1.stderr[:500]}")
#             return False
        
#         if not os.path.exists(temp_video_path) or os.path.getsize(temp_video_path) == 0:
#             print(f"❌ ERROR: Temporary video file was not created or is empty")
#             return False
        
#         # Step 2: Add audio to video (rotation commented out)
#         # Rotation was: transpose=2,transpose=2 = +180° (clockwise) | transpose=1,transpose=1 = -180° (counter-clockwise)
        
#         # Find audio file
#         script_dir = os.path.dirname(os.path.abspath(__file__))
#         audio_path = os.path.join(script_dir, "audio", "audio.mp3")
#         has_audio = os.path.exists(audio_path)
        
#         if has_audio:
#             print(f"🎵 Found audio file: {audio_path}")
#             # Step 2: Add audio (rotation commented out)
#             ffmpeg_cmd_step2 = [
#                 ffmpeg_path,
#                 '-y',  # Overwrite output file
#                 '-i', temp_video_path,  # Video input
#                 '-i', audio_path,  # Audio input
#                 # '-vf', 'transpose=2,transpose=2',  # Rotate 180 degrees clockwise - COMMENTED OUT
#                 '-c:v', 'libx264',
#                 '-c:a', 'aac',  # Audio codec
#                 '-b:a', '192k',  # Audio bitrate
#                 '-shortest',  # Finish encoding when the shortest input stream ends
#                 '-pix_fmt', 'yuv420p',
#                 '-crf', '18',  # High quality
#                 '-preset', 'medium',
#                 output_mp4_path
#             ]
#             print(f"🎵 Step 2: Adding audio to video...")
#         else:
#             print(f"[WARN]  Audio file not found: {audio_path} (continuing without audio)")
#             # Step 2: Copy video only (no rotation, no audio)
#             ffmpeg_cmd_step2 = [
#                 ffmpeg_path,
#                 '-y',  # Overwrite output file
#                 '-i', temp_video_path,
#                 # '-vf', 'transpose=2,transpose=2',  # Rotate 180 degrees clockwise - COMMENTED OUT
#                 '-c:v', 'libx264',
#                 '-pix_fmt', 'yuv420p',
#                 '-crf', '18',  # High quality
#                 '-preset', 'medium',
#                 output_mp4_path
#             ]
#             print(f"📹 Step 2: Copying video to final output...")
        
#         print(f"   Command: {' '.join(ffmpeg_cmd_step2)}")
        
#         # Run step 2: Process video (add audio and/or rotate)
#         result_step2 = subprocess.run(
#             ffmpeg_cmd_step2,
#             capture_output=True,
#             text=True,
#             timeout=1500  # 10 minute timeout for encoding
#         )
        
#         # Clean up temporary file
#         if os.path.exists(temp_video_path):
#             try:
#                 os.remove(temp_video_path)
#                 print(f"🧹 Cleaned up temporary video file")
#             except Exception as e:
#                 print(f"[WARN]  Warning: Could not delete temporary file: {e}")
        
#         if result_step2.returncode != 0:
#             print(f"❌ ERROR: Step 2 (video processing) failed")
#             print(f"   Return code: {result_step2.returncode}")
#             if result_step2.stderr:
#                 print(f"   Error output: {result_step2.stderr[:500]}")
#             return False
        
#         if os.path.exists(output_mp4_path) and os.path.getsize(output_mp4_path) > 0:
#             file_size = os.path.getsize(output_mp4_path)
#             audio_status = "with audio" if has_audio else "without audio"
#             print(f"[OK] SUCCESS: MP4 video encoded and {audio_status} successfully!")
#             print(f"   Output: {output_mp4_path}")
#             print(f"   Size: {file_size:,} bytes ({file_size / (1024*1024):.2f} MB)")
#             if has_audio:
#                 print(f"   🎵 Audio: {audio_path}")
            
#             # Delete PNG frames after successful encoding (if requested)
#             if delete_frames:
#                 _delete_png_frames(output_dir, base_name)
            
#             return True
#         else:
#             print(f"❌ ERROR: Final MP4 file was not created or is empty")
#             return False
            
#     except subprocess.TimeoutExpired:
#         print(f"❌ ERROR: ffmpeg encoding timed out after 10 minutes")
#         return False
#     except Exception as e:
#         print(f"❌ ERROR: Failed to encode PNG sequence to MP4: {e}")
#         import traceback
#         traceback.print_exc()
#         return False



# def _get_thumbnail_path(file_path: str) -> str:
#     """Return the expected thumbnail path next to a rendered image or video."""
#     base_dir = os.path.dirname(os.path.abspath(file_path))
#     base_name = os.path.splitext(os.path.basename(file_path))[0]
#     return os.path.join(base_dir, f"{base_name}_thumb.webp")


# def _upload_to_blob_storage(file_path: str, blob_container: str = "render-images") -> str:
#     """
#     Uploads the rendered file (image or video) to Azure Blob Storage and returns the blob URL.
    
#     Args:
#         file_path: Local path to the file (image or video)
#         blob_container: Name of the blob container (default: "render-images")
    
#     Returns:
#         Full URL of the uploaded blob, or None if upload failed
#     """
#     try:
#         print(f"\n{'='*60}\n☁️  UPLOADING TO BLOB STORAGE\n{'='*60}")
#         print(f"File Path: {file_path}")
#         print(f"Container: {blob_container}")
        
#         # Get the filename
#         filename = os.path.basename(file_path)
        
#         # Determine content type based on file extension
#         file_ext = os.path.splitext(filename)[1].lower()
#         if file_ext == '.mp4':
#             content_type = 'video/mp4'
#             file_type = "Video"
#         elif file_ext in ['.png', '.jpg', '.jpeg']:
#             content_type = 'image/png' if file_ext == '.png' else 'image/jpeg'
#             file_type = "Image"
#         elif file_ext == '.webp':
#             content_type = 'image/webp'
#             file_type = "Thumbnail"
#         else:
#             content_type = 'application/octet-stream'
#             file_type = "File"
        
#         # Construct blob URL
#         blob_base_url = "https://zrealtystoragedev.blob.core.windows.net"
#         blob_path = f"{blob_container}/{filename}"
#         blob_url = f"{blob_base_url}/{blob_path}"
        
#         # Construct upload URL with SAS token
#         upload_url = f"{blob_url}?{SAS_TOKEN}"
        
#         print(f"Blob URL: {blob_url}")
#         print(f"Uploading {file_type}: {filename}")
        
#         # Read the file
#         with open(file_path, 'rb') as file:
#             file_content = file.read()
#             file_size = len(file_content)
#             print(f"File size: {file_size:,} bytes ({file_size / (1024*1024):.2f} MB)")
        
#         # Upload to blob storage using PUT request
#         headers = {
#             'x-ms-blob-type': 'BlockBlob',
#             'Content-Type': content_type
#         }
        
#         print(f"Uploading to: {blob_url}")
#         response = requests.put(upload_url, data=file_content, headers=headers, timeout=1500)  # Longer timeout for videos
        
#         if response.status_code in [200, 201]:
#             print(f"{COLOR_GREEN}[OK] SUCCESS: {file_type} uploaded to blob storage!{COLOR_RESET}")
#             print(f"   Blob URL: {blob_url}")
#             return blob_url
#         else:
#             print(f"{COLOR_RED}❌ FAILED: Blob upload returned status {response.status_code}{COLOR_RESET}")
#             print(f"   Response: {response.text}")
#             return None
            
#     except Exception as e:
#         print(f"{COLOR_RED}❌ Error uploading to blob storage: {e}{COLOR_RESET}") 
#         import traceback
#         traceback.print_exc()
#         return None

# def _send_status_update(
#     project_id: int, 
#     gallery_id: int, 
#     user_id: int, 
#     job_id: str,
#     status: str,
#     step: str,
#     progress: int,
#     message: str,
#     render_type: str = "IMAGE",
#     file_path: Optional[str] = None,
#     blob_url: Optional[str] = None,
#     thumbnail_url: Optional[str] = None,
#     details: Optional[Dict[str, Any]] = None,
#     logger: Optional[RenderLogger] = None,
#     api_endpoint: Optional[str] = None,
#     wait_for_delivery: bool = False
# ) -> bool:
#     """
#     Sends status update to UploadGallerAI endpoint in a NON-BLOCKING background thread.
    
#     Status Codes (NEW):
#     - 1 = InProgress (for all steps before the last step)
#     - 2 = Completed (for the last/final step)
#     - 3 = Rejected (for exceptional handling/validation failures)
#     - 4 = Error (for errors/exceptions)
    
#     If wait_for_delivery is True, the status payload is sent synchronously so
#     the engine cannot reorder the AI job's in-progress and completed updates.

#     Returns:
#         True immediately (queued successfully) unless wait_for_delivery is used.
#     """
    
#     # 1. Update active_jobs dictionary details IMMEDIATELY (synchronous)
#     # This ensures the local monitoring dashboard is instant
#     with queue_lock:
#         if job_id in active_jobs:
#             active_jobs[job_id]['status'] = status
#             active_jobs[job_id]['progress'] = progress
#             active_jobs[job_id]['current_step'] = step
#             active_jobs[job_id]['message'] = message
            
#     # 2. UPDATE GLOBAL STATUS CACHE
#     _update_global_status(
#         job_id=job_id,
#         status=status,
#         step=step,
#         progress=progress,
#         message=message,
#         project_id=project_id,
#         gallery_id=gallery_id,
#         job_type=render_type.lower(),
#         render_size=_extract_job_render_size(active_jobs.get(job_id, {}))
#     )
    


#     # 3. Define the network work to be done in background
#     def _network_worker():
#         try:
#             # Map string status to numeric status code
#             status_code_map = {
#                 "processing": 1, "in_progress": 1, "queued": 1,
#                 "completed": 2, "success": 2,
#                 "rejected": 3,
#                 "failed": 4, "error": 4
#             }
#             status_code = status_code_map.get(status.lower(), 1)
            
#             status_names = {1: "InProgress", 2: "Completed", 3: "Rejected", 4: "Error"}
#             status_name = status_names.get(status_code, f"Unknown({status_code})")
            
#             # Prepare payload
#             status_payload_form = {
#                 "UserId": str(user_id),
#                 "ProjectId": str(project_id),
#                 "Id": str(gallery_id),
#                 "RenderType": render_type.upper(),
#                 "StatusMessage": message,
#                 "Status": status_code,
#                 "JobId": job_id,
#                 "StatusText": status,
#                 "Step": step,
#                 "Progress": str(progress),
#                 "Timestamp": datetime.now().isoformat()
#             }
            
#             payload_details = dict(details or {})
#             for key, value in _extract_job_render_size(active_jobs.get(job_id, {})).items():
#                 payload_details.setdefault(key, value)
#             if blob_url:
#                 url_field_name = "VideoUrl" if render_type == "VIDEO" else "ImageUrl"
#                 status_payload_form[url_field_name] = blob_url
#                 payload_details.setdefault("image_url", blob_url)
#                 payload_details.setdefault("output_url", blob_url)
#                 payload_details.setdefault("blob_url", blob_url)
#             if thumbnail_url:
#                 status_payload_form["ThumbnailUrl"] = thumbnail_url
#                 payload_details.setdefault("thumbnail_url", thumbnail_url)

#             if payload_details:
#                 status_payload_form["Details"] = json.dumps(payload_details)
                
#             # Determine endpoint
#             upload_url = api_endpoint if api_endpoint else (VIDEO_UPLOAD_API_URL if render_type == "VIDEO" else UPLOAD_API_URL)
            
#             print(f"\n{COLOR_BLUE}📡 [Background] Sending Status: {status_name} ({progress}%) - {message}{COLOR_RESET}")
#             # DEBUG: Print payload to verify ImageUrl/VideoUrl/ThumbnailUrl
#             print(f"   Payload: {json.dumps(status_payload_form, default=str)}")
            
#             request_start_time = time.monotonic()

#             # Add API Key headers if enabled
#             headers = {}
#             if USE_API_KEY and API_KEY:
#                 headers["ZRealtyServiceApiKey"] = API_KEY
#                 # Also try adding it as a query param just in case, or stick to header?
#                 # Sticking to header simply.

#             # Always notify the local monitor first so the queue/UI can complete
#             # even if the public callback endpoint is slow or unavailable.
#             # If the monitor accepts the update, it becomes the single sender to
#             # the public API. That avoids duplicate or out-of-order status writes
#             # to UploadProjectGalleryAI / UploadProjectVideoAI.
#             files = {'UploadImage': (None, '')}
#             monitor_sent = False
#             if MONITORING_API_URL:
#                 try:
#                     print(f"   📡 Broadcasting to Monitor: {MONITORING_API_URL}")
#                     monitor_response = None
#                     for attempt in range(1, 4):
#                         try:
#                             monitor_response = requests.post(
#                                 MONITORING_API_URL,
#                                 data=status_payload_form,
#                                 files=files,
#                                 headers=headers,
#                                 verify=False,
#                                 timeout=10,
#                             )
#                             print(f"   📡 Monitor broadcast response: {monitor_response.status_code} (attempt {attempt}/3)")
#                             if monitor_response.status_code in [200, 201, 202]:
#                                 monitor_sent = True
#                                 break
#                         except Exception as monitor_exc:
#                             print(f"   [WARN] Monitor broadcast attempt {attempt}/3 failed: {monitor_exc}")
#                             time.sleep(1.5 * attempt)
#                 except Exception as mon_e:
#                     print(f"   [WARN] Monitoring broadcast failed: {mon_e}")

#             if monitor_sent:
#                 request_duration = time.monotonic() - request_start_time
#                 if logger:
#                     logger.add_status_update(step, f"{status_name}({status_code})", request_duration, 200)
#                 print(f"{COLOR_GREEN}   ✓ Status accepted by monitor; skipping direct callback to avoid duplicate writes ({request_duration:.2f}s){COLOR_RESET}")
#                 return

#             # Send request to Primary API
#             response = None
#             primary_error = None
#             try:
#                 response = requests.post(upload_url, data=status_payload_form, files=files, headers=headers, verify=False, timeout=60)
#             except Exception as upload_exc:
#                 primary_error = upload_exc

#             request_duration = time.monotonic() - request_start_time

#             if logger and response is not None:
#                 logger.add_status_update(step, f"{status_name}({status_code})", request_duration, response.status_code)

#             if response is not None and response.status_code in [200, 201, 202]:
#                 print(f"{COLOR_GREEN}   ✓ Status sent successfully ({request_duration:.2f}s){COLOR_RESET}")
#             elif response is not None:
#                 # Try to parse JSON error message
#                 error_detail = response.text[:200]
#                 try:
#                     error_json = response.json()
#                     if "message" in error_json:
#                         error_detail = error_json["message"]
#                     elif "Message" in error_json:
#                         error_detail = error_json["Message"]
#                 except:
#                     pass
#                 print(f"{COLOR_YELLOW}   [WARN] Status API Error ({response.status_code}): {error_detail} ({request_duration:.2f}s){COLOR_RESET}")
#             elif primary_error is not None:
#                 print(f"{COLOR_YELLOW}   [WARN] Status API Request Failed: {primary_error} ({request_duration:.2f}s){COLOR_RESET}")
                
#         except Exception as e:
#             print(f"{COLOR_YELLOW}   [WARN] Background status update failed: {e}{COLOR_RESET}")

#     if wait_for_delivery:
#         _network_worker()
#         return True

#     # 4. Start background thread
#     try:
#         t = Thread(target=_network_worker, daemon=True)
#         t.start()
#         return True
#     except Exception as e:
#         print(f"Error starting status thread: {e}")
#         return False

# def _upload_render_to_api(project_id: int, gallery_id: int, file_path: str, user_id: int = 0, logger: Optional[RenderLogger] = None, job_id: str = ""):
#     """
#     Step 6: Finalizes the render job by uploading the binary and sending the success message.
#     """
#     try:
#         render_type = "VIDEO" if file_path.lower().endswith('.mp4') else "IMAGE"
#         print(f"\n{'='*60}\n📤 STEP 6: FINAL UPLOAD TO API ({render_type})\n{'='*60}")
        
#         # Step 1: Upload to blob storage for permanent record
#         file_ext = os.path.splitext(file_path)[1].lower()
#         blob_container = "render-videos" if file_ext == '.mp4' else "render-images"
#         blob_url = _upload_to_blob_storage(file_path, blob_container=blob_container)
#         thumbnail_url = None
#         thumbnail_path = _get_thumbnail_path(file_path)
#         if os.path.exists(thumbnail_path):
#             print(f"{COLOR_BLUE}🖼️ Uploading thumbnail: {thumbnail_path}{COLOR_RESET}")
#             thumbnail_url = _upload_to_blob_storage(thumbnail_path, blob_container=blob_container)
#             if not thumbnail_url:
#                 print(f"{COLOR_YELLOW}[WARN] Thumbnail upload failed, continuing with the main render only.{COLOR_RESET}")
#         else:
#             print(f"{COLOR_YELLOW}[WARN] Thumbnail not found at: {thumbnail_path}{COLOR_RESET}")
        
#         # Step 2: Send blob URL + status to main API (NO binary file)
#         if not blob_url:
#             print(f"{COLOR_RED}❌ Upload to blob storage failed. Cannot complete job.{COLOR_RESET}")
#             _send_status_update(
#                 project_id=project_id,
#                 gallery_id=gallery_id,
#                 user_id=user_id,
#                 job_id=job_id,
#                 status="error",
#                 step="upload_binary",
#                 progress=0,
#                 message="Something went wrong. Please try again.",
#                 render_type=render_type,
#                 logger=logger,
#                 wait_for_delivery=True
#             )
#             return False

#         # We use _send_status_update which sends the main blob URL and thumbnail URL
        
#         # --- NEW: Read Asset Report to include log details in final JSON ---
#         final_details = {}
#         try:
#             asset_report_path = os.path.join(GLB_OUTPUT_DIR, f"{job_id}_asset_report.json")
#             if os.path.exists(asset_report_path):
#                 with open(asset_report_path, 'r', encoding='utf-8') as f:
#                     asset_report = json.load(f)
#                 final_details['asset_processing_report'] = asset_report
#                 print(f"📄 Included asset report with {len(asset_report)} items.")
#         except Exception as e:
#             print(f"[WARN] Could not include asset report: {e}")

#         if thumbnail_url:
#             final_details["thumbnail_url"] = thumbnail_url

#         file_size_info = _get_file_size_info(file_path)
#         if file_size_info:
#             final_details.update(file_size_info)
#             if job_id and job_id in active_jobs:
#                 active_jobs[job_id].update(file_size_info)

#         success = _send_status_update(
#             project_id=project_id,
#             gallery_id=gallery_id,
#             user_id=user_id,
#             job_id=job_id,
#             status="completed",
#             step="completed",
#             progress=100,
#             message="Your render is ready!" if render_type == "IMAGE" else "Your video is ready!",
#             render_type=render_type,
#             blob_url=blob_url,  # Send blob URL in ImageUrl field
#             thumbnail_url=thumbnail_url,
#             logger=logger,
#             details=final_details,
#             wait_for_delivery=True
#         )
        
        

                
#         if success:
#             print(f"{COLOR_GREEN}[OK] Final upload and status update successful!{COLOR_RESET}")
#             if blob_url:
#                 print(f"   Permanent Blob Storage URL: {blob_url}")
#             return True
#         else:
#             print(f"{COLOR_YELLOW}[WARN]  Status update failed but blob upload succeeded{COLOR_RESET}")
#             if blob_url:
#                 print(f"   Permanent Blob Storage URL: {blob_url}")
#             return False
            
#     except Exception as e:
#         print(f"{COLOR_RED}❌ ERROR in final upload: {str(e)}{COLOR_RESET}")
#         import traceback
#         traceback.print_exc()
#         return False

# # --- BACKGROUND RENDER TASK ---

# def _background_render_task(project_id: int, gallery_id: int, payload: GenerationPayload, job_id: str, user_id: int = 0, suppress_error_status: bool = False):
#     """The main function that runs in the background to perform the full render and upload pipeline."""
#     logger = RenderLogger(job_id)
#     logger.start_process()
#     logger.set_render_type("IMAGE")
#     logger.set_user_details(project_id, gallery_id, user_id)
#     logger.set_render_quality(payload.render_quality) 
    
#     output_image_path = None
#     enable_status_updates = getattr(payload, 'enable_status_updates', False)
    
#     try:
#         start_time = time.monotonic()
#         print(f"\n{'='*80}\n🚀 BACKGROUND RENDER TASK STARTED (GODOT)\n{'='*80}\nJob ID: {job_id}\nProject ID: {project_id}\nGallery ID: {gallery_id}\nUser ID: {user_id}\nStatus Updates: {'ENABLED' if enable_status_updates else 'DISABLED'}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*80}\n")
        
#         # Step 1/5: Preparing your scene
#         if enable_status_updates:
#             _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "preparing", 10, "Preparing your scene...", render_type="IMAGE", logger=logger)
        
#         print(f"🔍 STEP 1/5: Validating input payload...\n{'─'*60}")
#         is_valid, validation_error = _validate_image_payload(payload)
#         if not is_valid:
#             error_msg = f"Invalid payload: {validation_error}"
#             print(f"{COLOR_RED}❌ VALIDATION FAILED: {error_msg}{COLOR_RESET}")
#             if enable_status_updates:
#                 _send_status_update(
#                     project_id, gallery_id, user_id, job_id, 
#                     "rejected", "validation", 0, 
#                     "Something went wrong. Please try again.", 
#                     render_type="IMAGE", logger=logger
#                 )
#             logger.add_interrupt("validation_failed", error_msg)
#             logger.end_process(success=False, error=error_msg)
#             return
#         print(f"[OK] Payload validation passed\n")
        
#         # Step 2/5: Setting up your space
#         if enable_status_updates:
#             _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "loading", 30, "Setting up your space...", render_type="IMAGE", logger=logger)
        
#         # Step 3/5: Rendering your design
#         if enable_status_updates:
#             _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "rendering", 50, "Rendering your design...", render_type="IMAGE", logger=logger)
            
#         print(f"🎬 STEP 3/5: Running Render...\n{'─'*60}")
        
#         output_image_path = _run_godot_render(job_id, payload, logger=logger)
        
#         if not output_image_path or not os.path.exists(output_image_path):
#             raise Exception("Godot render failed to produce output")
            
#         print(f"[OK] Render completed successfully: {output_image_path}\n")

#         # Step 4/5: Adding final touches
#         if enable_status_updates:
#              _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "finalizing", 80, "Adding final touches...", render_type="IMAGE", logger=logger, wait_for_delivery=True)
        
#         print(f"📤 STEP 4/5: Uploading to external API...\n{'─'*60}")
             
#         upload_success = _upload_render_to_api(project_id, gallery_id, output_image_path, user_id, logger=logger, job_id=job_id)
        
#         end_time = time.monotonic()
#         if upload_success:
#             print(f"\n{'='*80}\n🎉 ALL STEPS COMPLETED SUCCESSFULLY! (Total Time: {(end_time - start_time):.2f}s)\n{'='*80}")
#             logger.end_process(success=True)
#         else:
#             print(f"\n{'='*80}\n[WARN] RENDER COMPLETED BUT UPLOAD FAILED (Total Time: {(end_time - start_time):.2f}s)\n{'='*80}")
#             logger.end_process(success=True, error="Upload failed")
            
#     except Exception as e:
#         error_msg = f"Unexpected error: {str(e)}"
#         print(f"\n{'='*80}\n{COLOR_RED}❌ ERROR IN BACKGROUND TASK{COLOR_RESET}\n{'='*80}\nJob ID: {job_id}\nError: {error_msg}\n{'='*80}\n")
#         import traceback
#         traceback.print_exc()
        
#         if suppress_error_status:
#             # Re-raise so the retry loop can catch it and retry
#             logger.add_interrupt("error", error_msg)
#             logger.end_process(success=False, error=error_msg)
#             raise
        
#         if enable_status_updates:
#             _send_status_update(project_id, gallery_id, user_id, job_id, "error", "error", 0, "Scene is too large to render. Please try a different setup.", render_type="IMAGE", logger=logger)
#         logger.add_interrupt("error", error_msg)
#         logger.end_process(success=False, error=error_msg)
#         raise  # Always propagate so retry loop detects the failure
#     finally:
#         pass

# def _run_godot_video_render(job_id: str, payload: VideoGenerationPayload, logger: Optional[RenderLogger] = None, include_audio: bool = True) -> str:
#     """
#     Runs the Godot video rendering process using MovieWriter (--write-movie) for fast rendering.
#     Falls back to PNG frame-by-frame if MovieWriter is not available.
#     """
#     # 1. Localize assets (same pattern as image render)
#     if logger: logger.start_glb_creation()
    
#     print(f"🎬 Starting Godot Video Render for Job {job_id}")
    
#     localized_geometry = None
#     if payload.floor_plan_data:
#         # Parse floor_plan_data string → dict if needed
#         floor_plan_data = payload.floor_plan_data
#         scene_data = json.loads(floor_plan_data) if isinstance(floor_plan_data, str) else floor_plan_data
#         # Download and localize all asset paths to absolute local paths
#         localized_geometry = _download_and_localize_assets(scene_data, use_high_res=True, logger=logger)
#         print(f"--- Finalizing floor plan data with all local asset paths ---")
    
#     if logger: logger.end_glb_creation()

#     # 2. Build Godot input JSON with localized paths
#     # Exclude floor_plan_data since we replace it with localized version
#     godot_input = payload.dict(exclude={'floor_plan_data'}, exclude_unset=False)
#     if localized_geometry:
#         godot_input['floor_plan_data'] = localized_geometry
#     elif payload.floor_plan_data:
#         # If localization failed, try to use raw data
#         if isinstance(payload.floor_plan_data, str):
#             try:
#                 godot_input['floor_plan_data'] = json.loads(payload.floor_plan_data)
#             except:
#                 godot_input['floor_plan_data'] = payload.floor_plan_data
#         else:
#             godot_input['floor_plan_data'] = payload.floor_plan_data

#     if payload.environment_url:
#         res = _download_asset_with_fallback(payload.environment_url, use_high_poly=True)
#         if res and res[1]:
#             godot_input['environment_url'] = res[1]

#     # Remove None values to prevent GDScript null issues (has() returns true but value is null)
#     godot_input = {k: v for k, v in godot_input.items() if v is not None}
    
#     # 2.5 Video Scene Culling — remove rooms/items not visible along camera path
#     output_basename = f"{job_id}_render"
#     culling_log_path = os.path.join(CULLING_LOGS_DIR, f"{output_basename}_culling.txt")
#     if ENABLE_VIDEO_CULLING:
#         try:
#             video_optimizer = VideoSceneOptimizer(log_path=culling_log_path)
#             godot_input = video_optimizer.cull_scene(godot_input)
#             print(f"[OK] Video scene culling complete. Log: {culling_log_path}")
#         except Exception as e:
#             print(f"[WARN] Video scene culling failed (continuing without culling): {e}")
#     else:
#         print("ℹ️  Video scene culling disabled by main.py flag; rendering full scene.")

#     # Save the properly assembled input JSON
#     input_json_path = os.path.join(RUNTIME_DIR, f"{job_id}_video_input.json")
#     try:
#         with open(input_json_path, 'w', encoding='utf-8') as f:
#             json.dump(godot_input, f, ensure_ascii=False, default=str)
#         print(f"📄 Saved video input JSON to: {input_json_path}")
#     except Exception as e:
#         print(f"❌ Failed to save video input JSON: {e}")
#         raise e

#     # 3. Prepare Output Directory
#     output_dir = os.path.abspath(os.path.join(RENDER_OUTPUT_DIR, job_id))
#     os.makedirs(output_dir, exist_ok=True)
    
#     # Output paths
#     # MovieWriter saves AVI relative to the Godot project directory
#     # Use global paths configured at start
#     godot_exe = GODOT_EXE
#     project_path = GODOT_PROJECT_PATH
    
#     avi_filename = f"{job_id}_render.avi"
#     avi_godot_output = os.path.join(project_path, avi_filename)  # Where Godot will save it
#     output_video_path = os.path.abspath(os.path.join(RENDER_OUTPUT_DIR, f"{job_id}_render.mp4"))
#     temp_img_base = os.path.join(output_dir, "frame.png")  # PNG fallback
    
#     fps = payload.video_animation.fps if payload.video_animation else 30
    
#     # 4. Run Godot with MovieWriter
#     # NOTE: Do NOT use --headless for renders; headless disables rendering in Godot 4.x.
#     cmd = [godot_exe]
#     cmd.extend([
#         "--path", project_path,
#         "--write-movie", avi_filename,  # Simple filename - Godot saves in project dir
#         "--fixed-fps", str(fps),
#         "res://video_main.tscn",
#         "--",
#         input_json_path,
#         output_video_path,
#         temp_img_base  # passed so GDScript knows where to save PNGs if MovieWriter fails
#     ])
    
#     print(f"🚀 Running Godot MovieWriter Command: {' '.join(cmd)}")
    
#     timeout = int(os.getenv("VIDEO_RENDER_TIMEOUT_SECONDS", "1800"))
    
#     try:
#         proc = subprocess.Popen(
#             cmd,
#             stdout=subprocess.PIPE,
#             stderr=subprocess.STDOUT,
#             text=True,
#             encoding="utf-8",
#             errors="replace",
#             bufsize=1,
#         )

#         all_output = []
#         def _stream_video_output():
#             try:
#                 assert proc.stdout is not None
#                 for raw_line in proc.stdout:
#                     line = raw_line.rstrip("\r\n")
#                     all_output.append(line)
#                     clean_line = line.strip()
#                     if clean_line:
#                         print(f"[GodotVideo] {clean_line}", flush=True)
#             except Exception as stream_exc:
#                 print(f"Warning: Godot stream reader stopped early: {stream_exc}")

#         reader = Thread(target=_stream_video_output, daemon=True)
#         reader.start()

#         try:
#             proc.wait(timeout=timeout)
#         except subprocess.TimeoutExpired:
#             proc.kill()
#             try:
#                 proc.wait(timeout=10)
#             except Exception:
#                 pass
#             raise

#         full_stdout = "\n".join(all_output)
        
#         # Check if MovieWriter actually produced the AVI file
#         if not os.path.exists(avi_godot_output) or os.path.getsize(avi_godot_output) == 0:
#             # Only fail if no AVI was produced AND no meaningful output
#             if "VIDEO RENDER COMPLETE" not in full_stdout:
#                 print(f"❌ Godot failed to produce video (exit code {proc.returncode})")
#                 raise Exception(f"Godot render failed with exit code {proc.returncode}")
#     except subprocess.TimeoutExpired:
#         print(f"❌ Godot execution timed out after {timeout}s")
#         raise Exception("Godot render timed out")

#     # 5. Check what was produced and convert to MP4
#     if os.path.exists(avi_godot_output) and os.path.getsize(avi_godot_output) > 0:
#         # MovieWriter produced an AVI — convert to MP4
#         avi_size_mb = os.path.getsize(avi_godot_output) / (1024 * 1024)
#         print(f"[OK] MovieWriter AVI created: {avi_godot_output} ({avi_size_mb:.1f} MB)")
#         print(f"🎞️ Converting AVI to MP4...")
        
#         ffmpeg_path = _find_ffmpeg_path()
#         if not ffmpeg_path:
#             print("❌ FFmpeg not found! Cannot convert AVI to MP4.")
#             raise Exception("FFmpeg not found")
        
#         # Find audio file
#         script_dir = os.path.dirname(os.path.abspath(__file__))
#         audio_path = os.path.join(script_dir, "audio", "audio.mp3")
#         has_audio = include_audio and os.path.exists(audio_path)
        
#         # Build FFmpeg command: AVI → MP4
#         # MovieWriter AVI includes a silent audio track — we must explicitly ignore it
#         # DLSS priming: MovieWriter captures ALL rendered frames, including:
#         # - 20 warmup frames (scene settling, GI bake) 
#         # - 5 DLSS priming frames (DLSS RR temporal history)
#         # Total = 25 frames to trim from the start of EVERY chunk's AVI.
#         warmup_frames = 20   # Must match _warmup_frames in video_render.gd
#         dlss_prime_frames = 5  # Must match DLSS_PRIME_FRAMES in video_render.gd
#         total_trim_frames = warmup_frames + dlss_prime_frames
#         dlss_trim_seconds = total_trim_frames / max(fps, 1)
#         ffmpeg_cmd = [
#             ffmpeg_path,
#             '-y',
#             '-ss', f'{dlss_trim_seconds:.4f}',  # Skip DLSS priming frames
#             '-i', avi_godot_output,
#         ]
        
#         if has_audio:
#             ffmpeg_cmd.extend(['-i', audio_path])
#             ffmpeg_cmd.extend([
#                 '-map', '0:v',     # Video from AVI (input 0)
#                 '-map', '1:a',     # Audio from external MP3 (input 1)
#                 '-c:v', 'libx264',
#                 '-c:a', 'aac',
#                 '-b:a', '192k',
#                 '-shortest',
#             ])
#             print(f"🎵 Adding audio: {audio_path}")
#         else:
#             ffmpeg_cmd.extend([
#                 '-map', '0:v',     # Video only, no audio
#                 '-c:v', 'libx264',
#                 '-an',             # Explicitly no audio
#             ])
        
#         ffmpeg_cmd.extend([
#             '-pix_fmt', 'yuv420p',
#             '-crf', '18',
#             '-preset', 'medium',
#             output_video_path
#         ])
        
#         print(f"📹 FFmpeg command: {' '.join(ffmpeg_cmd)}")
        
#         result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=600)
        
#         if result.returncode != 0:
#             print(f"❌ FFmpeg conversion failed: {result.stderr[:500] if result.stderr else 'No error output'}")
#             raise Exception("FFmpeg AVI to MP4 conversion failed")
        
#         # Clean up AVI
#         try:
#             os.remove(avi_godot_output)
#             print(f"🧹 Cleaned up AVI file")
#         except:
#             pass
        
#         if os.path.exists(output_video_path) and os.path.getsize(output_video_path) > 0:
#             mp4_size_mb = os.path.getsize(output_video_path) / (1024 * 1024)
#             print(f"[OK] Video output saved: {output_video_path} ({mp4_size_mb:.1f} MB)")
#             # Clean up the temp frames folder (no longer needed after MP4 is saved)
#             try:
#                 import shutil
#                 shutil.rmtree(output_dir, ignore_errors=True)
#                 print(f"🧹 Cleaned up temp folder: {output_dir}")
#             except Exception:
#                 pass
#             return output_video_path
#         else:
#             raise Exception("MP4 output file was not created")
#     else:
#         # Fallback: check for PNG sequence (MovieWriter might not have been available)
#         print("[WARN]  MovieWriter AVI not found — checking for PNG fallback frames...")
        
#         print(f"🎞️ Encoding PNG sequence to MP4 (FPS: {fps})...")
#         success = _encode_png_sequence_to_mp4(output_dir, "frame", output_video_path, fps=fps, delete_frames=True)
        
#         if success:
#             print(f"[OK] Video output saved: {output_video_path}")
#             # Clean up the temp frames folder (PNG frames already deleted by encoder,
#             # but the folder itself still remains — remove it)
#             try:
#                 import shutil
#                 shutil.rmtree(output_dir, ignore_errors=True)
#                 print(f"🧹 Cleaned up temp folder: {output_dir}")
#             except Exception:
#                 pass
#             return output_video_path
#         else:
#             raise Exception("Video encoding failed — no AVI or PNG frames produced")

# def _vector3_to_plain_dict(value: Any) -> Optional[Dict[str, float]]:
#     """Normalize a Vector3-like value into a plain {x, y, z} dictionary."""
#     if value is None:
#         return None

#     if isinstance(value, dict):
#         x = value.get("x")
#         y = value.get("y")
#         z = value.get("z")
#     elif hasattr(value, "dict"):
#         return _vector3_to_plain_dict(value.dict())
#     elif hasattr(value, "x") and hasattr(value, "y") and hasattr(value, "z"):
#         x = getattr(value, "x")
#         y = getattr(value, "y")
#         z = getattr(value, "z")
#     elif isinstance(value, (list, tuple)) and len(value) >= 3:
#         x, y, z = value[0], value[1], value[2]
#     else:
#         return None

#     if x is None or y is None or z is None:
#         return None

#     return {"x": float(x), "y": float(y), "z": float(z)}

# def _lerp_plain_vector3(start_value: Any, end_value: Any, t: float) -> Optional[Dict[str, float]]:
#     """Linearly interpolate between two Vector3-like values and return a plain dict."""
#     start_dict = _vector3_to_plain_dict(start_value)
#     end_dict = _vector3_to_plain_dict(end_value)
#     if not start_dict or not end_dict:
#         return None

#     return {
#         "x": float(start_dict["x"] + (end_dict["x"] - start_dict["x"]) * t),
#         "y": float(start_dict["y"] + (end_dict["y"] - start_dict["y"]) * t),
#         "z": float(start_dict["z"] + (end_dict["z"] - start_dict["z"]) * t),
#     }

# def _split_video_animation_into_chunks(video_animation: VideoAnimation, max_chunk_seconds: float) -> List[VideoAnimation]:
#     """
#     Split a video animation into consecutive chunks of at most max_chunk_seconds.

#     For frame-by-frame animations, slices the keyframe list directly.
#     For start/end animations, creates smaller start/end spans for each chunk.
#     """
#     if not video_animation:
#         return []

#     duration_seconds = float(getattr(video_animation, "duration_seconds", 0.0) or 0.0)
#     fps = int(getattr(video_animation, "fps", 30) or 30)
#     fps = max(1, fps)

#     if duration_seconds <= max_chunk_seconds:
#         return [video_animation]

#     keyframes = list(video_animation.keyframes or [])

#     if keyframes:
#         chunks: List[VideoAnimation] = []
#         total_frames = len(keyframes)
#         chunk_count = max(1, int((duration_seconds - 1e-9) / max_chunk_seconds) + 1)
#         for chunk_index in range(chunk_count):
#             chunk_start_seconds = chunk_index * max_chunk_seconds
#             chunk_end_seconds = min((chunk_index + 1) * max_chunk_seconds, duration_seconds)
#             start_ratio = chunk_start_seconds / duration_seconds if duration_seconds > 0 else 0.0
#             end_ratio = chunk_end_seconds / duration_seconds if duration_seconds > 0 else 1.0
#             start_idx = int(round(start_ratio * (total_frames - 1))) if total_frames > 1 else 0
#             end_idx = int(round(end_ratio * (total_frames - 1))) if total_frames > 1 else total_frames
#             if chunk_index == chunk_count - 1:
#                 end_idx = total_frames
#             else:
#                 end_idx = max(start_idx + 1, min(end_idx, total_frames))
#             chunk = video_animation.copy(deep=True)
#             chunk.keyframes = []
#             for frame_offset, frame_data in enumerate(keyframes[start_idx:end_idx]):
#                 frame_copy = copy.deepcopy(frame_data)
#                 if isinstance(frame_copy, dict) and "frame" in frame_copy:
#                     frame_copy["frame"] = frame_offset
#                 chunk.keyframes.append(frame_copy)
#             chunk.fps = fps
#             chunk.duration_seconds = max(float(len(chunk.keyframes)) / float(fps), 1.0 / float(fps))
#             chunks.append(chunk)
#         return chunks

#     has_start_end = (
#         video_animation.camera_position_start is not None and
#         video_animation.camera_position_end is not None
#     )
#     if not has_start_end:
#         return [video_animation]

#     chunks = []
#     chunk_count = max(1, int((duration_seconds - 1e-9) / max_chunk_seconds) + 1)

#     for chunk_index in range(chunk_count):
#         chunk_start_seconds = chunk_index * max_chunk_seconds
#         chunk_end_seconds = min((chunk_index + 1) * max_chunk_seconds, duration_seconds)
#         start_t = chunk_start_seconds / duration_seconds if duration_seconds > 0 else 0.0
#         end_t = chunk_end_seconds / duration_seconds if duration_seconds > 0 else 1.0
#         segment = video_animation.copy(deep=True)
#         segment.fps = fps
#         segment.duration_seconds = max(float(chunk_end_seconds - chunk_start_seconds), 1.0 / float(fps))

#         if video_animation.camera_position_start is not None and video_animation.camera_position_end is not None:
#             segment.camera_position_start = _lerp_plain_vector3(
#                 video_animation.camera_position_start,
#                 video_animation.camera_position_end,
#                 start_t,
#             )
#             segment.camera_position_end = _lerp_plain_vector3(
#                 video_animation.camera_position_start,
#                 video_animation.camera_position_end,
#                 end_t,
#             )

#         if video_animation.camera_target_start is not None and video_animation.camera_target_end is not None:
#             segment.camera_target_start = _lerp_plain_vector3(
#                 video_animation.camera_target_start,
#                 video_animation.camera_target_end,
#                 start_t,
#             )
#             segment.camera_target_end = _lerp_plain_vector3(
#                 video_animation.camera_target_start,
#                 video_animation.camera_target_end,
#                 end_t,
#             )

#         segment.keyframes = None
#         chunks.append(segment)

#     return chunks

# def _escape_ffmpeg_concat_path(path: str) -> str:
#     """Escape a file path for use in ffmpeg concat lists."""
#     return path.replace("\\", "/").replace("'", "'\\''")

# def _merge_video_segments_with_ffmpeg(segment_paths: List[str], output_video_path: str) -> str:
#     """Merge chunked MP4 segments into a final MP4, then mux audio once if available."""
#     if not segment_paths:
#         raise Exception("No video segments were produced")

#     ffmpeg_path = _find_ffmpeg_path()
#     if not ffmpeg_path:
#         raise Exception("FFmpeg not found")

#     script_dir = os.path.dirname(os.path.abspath(__file__))
#     audio_path = os.path.join(script_dir, "audio", "audio.mp3")
#     has_audio = os.path.exists(audio_path)

#     import shutil

#     with tempfile.TemporaryDirectory(prefix="video_chunk_merge_") as temp_dir:
#         concat_list_path = os.path.join(temp_dir, "concat_list.txt")
#         merged_video_path = os.path.join(temp_dir, "merged_video.mp4")

#         with open(concat_list_path, "w", encoding="utf-8") as f:
#             for segment_path in segment_paths:
#                 f.write(f"file '{_escape_ffmpeg_concat_path(os.path.abspath(segment_path))}'\n")

#         concat_cmd = [
#             ffmpeg_path,
#             "-y",
#             "-f", "concat",
#             "-safe", "0",
#             "-i", concat_list_path,
#             "-c", "copy",
#             merged_video_path,
#         ]

#         print(f"🎞️ Concatenating {len(segment_paths)} video segments...")
#         print(f"📼 FFmpeg concat command: {' '.join(concat_cmd)}")
#         concat_result = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=900)
#         if concat_result.returncode != 0:
#             raise Exception(
#                 f"FFmpeg concat failed: {concat_result.stderr[:1000] if concat_result.stderr else 'No error output'}"
#             )

#         final_output = os.path.abspath(output_video_path)
#         os.makedirs(os.path.dirname(final_output), exist_ok=True)
#         if os.path.exists(final_output):
#             try:
#                 os.remove(final_output)
#             except Exception:
#                 pass

#         if has_audio:
#             muxed_output_path = os.path.join(temp_dir, "merged_with_audio.mp4")
#             mux_cmd = [
#                 ffmpeg_path,
#                 "-y",
#                 "-i", merged_video_path,
#                 "-i", audio_path,
#                 "-map", "0:v",
#                 "-map", "1:a",
#                 "-c:v", "copy",
#                 "-c:a", "aac",
#                 "-b:a", "192k",
#                 "-shortest",
#                 muxed_output_path,
#             ]
#             print(f"🎵 Adding audio to merged video: {audio_path}")
#             print(f"📼 FFmpeg mux command: {' '.join(mux_cmd)}")
#             mux_result = subprocess.run(mux_cmd, capture_output=True, text=True, timeout=900)
#             if mux_result.returncode != 0:
#                 raise Exception(
#                     f"FFmpeg audio mux failed: {mux_result.stderr[:1000] if mux_result.stderr else 'No error output'}"
#                 )
#             shutil.move(muxed_output_path, final_output)
#         else:
#             shutil.move(merged_video_path, final_output)

#         return final_output

# def _run_chunked_godot_video_render(job_id: str, payload: VideoGenerationPayload, logger: Optional[RenderLogger] = None) -> str:
#     """Render long videos in chunks and stitch the results together with ffmpeg."""
#     video_animation = payload.video_animation
#     if not video_animation:
#         return _run_godot_video_render(job_id, payload, logger=logger)

#     duration_seconds = float(getattr(video_animation, "duration_seconds", 0.0) or 0.0)
#     if duration_seconds <= VIDEO_RENDER_MAX_CHUNK_SECONDS:
#         return _run_godot_video_render(job_id, payload, logger=logger)

#     chunk_animations = _split_video_animation_into_chunks(video_animation, VIDEO_RENDER_MAX_CHUNK_SECONDS)
#     if len(chunk_animations) <= 1:
#         return _run_godot_video_render(job_id, payload, logger=logger)

#     print(f"🎬 Chunking long video render into {len(chunk_animations)} segments (max {VIDEO_RENDER_MAX_CHUNK_SECONDS:.0f}s each)")

#     chunk_plan_log_path = os.path.join(CULLING_LOGS_DIR, f"{job_id}_render_culling.txt")
#     try:
#         with open(chunk_plan_log_path, "w", encoding="utf-8") as f:
#             f.write(f"{'='*60}\n")
#             f.write("VIDEO CHUNK PLAN\n")
#             f.write(f"{'='*60}\n")
#             f.write(f"Job ID          : {job_id}\n")
#             f.write(f"Total duration   : {duration_seconds:.3f} sec\n")
#             f.write(f"Chunk threshold  : {VIDEO_RENDER_MAX_CHUNK_SECONDS:.3f} sec\n")
#             f.write(f"Chunk count      : {len(chunk_animations)}\n")
#             f.write(f"\n{'-'*60}\n")
#             elapsed_preview = 0.0
#             for idx, preview_anim in enumerate(chunk_animations, start=1):
#                 preview_duration = float(preview_anim.duration_seconds or 0.0)
#                 preview_end = elapsed_preview + preview_duration
#                 preview_frames = len(preview_anim.keyframes or [])
#                 if preview_frames <= 0:
#                     preview_frames = max(1, int(round(preview_duration * float(preview_anim.fps or 30))))
#                 if idx == 1:
#                     preview_label = "first 10 sec"
#                 elif idx == 2 and len(chunk_animations) >= 2:
#                     preview_label = "next 10 sec"
#                 elif idx == len(chunk_animations) and preview_duration < VIDEO_RENDER_MAX_CHUNK_SECONDS:
#                     preview_label = f"final {preview_duration:.3f} sec"
#                 else:
#                     preview_label = f"segment {idx}"
#                 f.write(
#                     f"Chunk {idx}/{len(chunk_animations)} - {preview_label}: "
#                     f"{elapsed_preview:.3f}s -> {preview_end:.3f}s "
#                     f"({preview_duration:.3f}s, {preview_frames} frames)\n"
#                 )
#                 elapsed_preview = preview_end
#             f.write(f"\n{'='*60}\n")
#         print(f"📄 Saved chunk plan log to: {chunk_plan_log_path}")
#     except Exception as e:
#         print(f"[WARN]  Could not save chunk plan log: {e}")

#     segment_paths: List[str] = []
#     try:
#         elapsed_seconds = 0.0
#         for index, chunk_animation in enumerate(chunk_animations, start=1):
#             segment_job_id = f"{job_id}_part{index:02d}"
#             segment_payload = payload.copy(deep=True)
#             segment_payload.video_animation = chunk_animation
#             chunk_duration = float(chunk_animation.duration_seconds or 0.0)
#             if index == 1:
#                 chunk_label = "first 10 sec"
#             elif index == 2 and len(chunk_animations) >= 2:
#                 chunk_label = "next 10 sec"
#             elif index == len(chunk_animations) and chunk_duration < VIDEO_RENDER_MAX_CHUNK_SECONDS:
#                 chunk_label = f"final {chunk_duration:.3f} sec"
#             else:
#                 chunk_label = f"segment {index}"
#             chunk_start_seconds = elapsed_seconds
#             chunk_end_seconds = chunk_start_seconds + chunk_duration
#             frame_count = len(chunk_animation.keyframes or [])
#             if frame_count <= 0:
#                 frame_count = max(1, int(round(chunk_duration * float(chunk_animation.fps or 30))))
#             print(
#                 f"[VideoChunk] Rendering {chunk_label} chunk {index}/{len(chunk_animations)} "
#                 f"({chunk_start_seconds:.3f}s -> {chunk_end_seconds:.3f}s, {chunk_duration:.3f}s, {frame_count} frames)"
#             )
#             print(
#                 f"🎞️ Rendering chunk {index}/{len(chunk_animations)} "
#                 f"({chunk_start_seconds:.3f}s -> {chunk_end_seconds:.3f}s, {chunk_duration:.3f}s)"
#             )
#             if logger:
#                 video_chunks = logger.log_data.setdefault("video_chunks", [])
#                 video_chunks.append({
#                     "chunk_index": int(index),
#                     "total_chunks": int(len(chunk_animations)),
#                     "label": chunk_label,
#                     "start_seconds": round(chunk_start_seconds, 3),
#                     "end_seconds": round(chunk_end_seconds, 3),
#                     "duration_seconds": round(chunk_duration, 3),
#                     "frame_count": int(frame_count),
#                     "segment_job_id": segment_job_id,
#                     "timestamp": datetime.now().isoformat(),
#                 })
#             segment_path = _run_godot_video_render(segment_job_id, segment_payload, logger=None, include_audio=False)
#             segment_paths.append(segment_path)
#             elapsed_seconds = chunk_end_seconds

#         print(f"🎞️ Merging {len(segment_paths)} rendered chunk(s) into final video...")
#         final_output_path = os.path.abspath(os.path.join(RENDER_OUTPUT_DIR, f"{job_id}_render.mp4"))
#         return _merge_video_segments_with_ffmpeg(segment_paths, final_output_path)
#     finally:
#         for segment_path in segment_paths:
#             try:
#                 if os.path.exists(segment_path):
#                     os.remove(segment_path)
#             except Exception:
#                 pass

# def _background_video_render_task(project_id: int, gallery_id: int, payload: VideoGenerationPayload, job_id: str, user_id: int = 0, suppress_error_status: bool = False):
#     """The main function that runs in the background to perform the full video render and upload pipeline."""
#     logger = RenderLogger(job_id)
#     logger.start_process()
#     logger.set_render_type("VIDEO")
#     logger.set_user_details(project_id, gallery_id, user_id)
#     logger.set_render_quality(payload.render_quality)
    
#     output_video_path = None
#     enable_status_updates = getattr(payload, 'enable_status_updates', False)
    
#     try:
#         start_time = time.monotonic()
#         video_animation = payload.video_animation
#         duration_seconds = video_animation.duration_seconds if video_animation else 5.0
#         fps = video_animation.fps if video_animation else 30
#         print(f"\n{'='*80}\n🎬 BACKGROUND VIDEO RENDER TASK STARTED (GODOT)\n{'='*80}\nJob ID: {job_id}\nProject ID: {project_id}\nGallery ID: {gallery_id}\nUser ID: {user_id}\nDuration: {duration_seconds}s @ {fps}fps\nStatus Updates: {'ENABLED' if enable_status_updates else 'DISABLED'}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*80}\n")
        
#         # Step 1/5: Preparing your scene
#         if enable_status_updates:
#             _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "preparing", 10, "Preparing your scene...", render_type="VIDEO", logger=logger)
        
#         print(f"🔍 STEP 1/5: Validating input payload...\n{'─'*60}")
#         is_valid, validation_error = _validate_video_payload(payload)
#         if not is_valid:
#             error_msg = f"Invalid payload: {validation_error}"
#             print(f"{COLOR_RED}❌ VALIDATION FAILED: {error_msg}{COLOR_RESET}")
#             if enable_status_updates:
#                 _send_status_update(
#                     project_id, gallery_id, user_id, job_id, 
#                     "rejected", "validation", 0, 
#                     "Something went wrong. Please try again.", 
#                     render_type="VIDEO", logger=logger
#                 )
#             logger.add_interrupt("validation_failed", error_msg)
#             logger.end_process(success=False, error=error_msg)
#             return
#         print(f"[OK] Payload validation passed\n")
        
#         # Step 2/5: Setting up your space
#         if enable_status_updates:
#             _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "loading", 30, "Setting up your space...", render_type="VIDEO", logger=logger)
        
#         # Step 3/5: Creating your video
#         if enable_status_updates:
#             _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "rendering", 50, "Creating your video...", render_type="VIDEO", logger=logger)
            
#         print(f"🎬 STEP 3/5: Running Video Render...\n{'─'*60}")
        
#         # Optimization (Optional, kept from original logic if needed, but handled by Godot's throughput mostly)
#         # Note: Godot can handle unoptimized scenes better in some cases, but culling is always good.
#         # We reused the optimizer in previous code. Let's skip it for simplicity unless requested, as it was complex.
#         # If needed, we can re-add `VideoSceneOptimizer` usage here.
        
#         output_video_path = _run_chunked_godot_video_render(job_id, payload, logger=logger)
        
#         if not output_video_path or not os.path.exists(output_video_path):
#             raise Exception("Godot video render failed to produce output")
            
#         print(f"[OK] Video Render completed successfully: {output_video_path}\n")

#         # Step 4/5: Processing your video
#         if enable_status_updates:
#              _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "finalizing", 80, "Processing your video...", render_type="VIDEO", logger=logger, wait_for_delivery=True)
        
#         print(f"📤 STEP 4/5: Uploading to external API...\n{'─'*60}")
             
#         upload_success = _upload_render_to_api(project_id, gallery_id, output_video_path, user_id, logger=logger, job_id=job_id)
        
#         end_time = time.monotonic()
#         if upload_success:
#             print(f"\n{'='*80}\n🎉 ALL VIDEO STEPS COMPLETED SUCCESSFULLY! (Total Time: {(end_time - start_time):.2f}s)\n{'='*80}")
#             logger.end_process(success=True)
#         else:
#             print(f"\n{'='*80}\n[WARN] VIDEO RENDER COMPLETED BUT UPLOAD FAILED (Total Time: {(end_time - start_time):.2f}s)\n{'='*80}")
#             logger.end_process(success=True, error="Upload failed")
            
#     except Exception as e:
#         error_msg = f"Unexpected error: {str(e)}"
#         print(f"\n{'='*80}\n{COLOR_RED}❌ ERROR IN VIDEO BACKGROUND TASK{COLOR_RESET}\n{'='*80}\nJob ID: {job_id}\nError: {error_msg}\n{'='*80}\n")
#         import traceback
#         traceback.print_exc()
        
#         if suppress_error_status:
#             # Re-raise so the retry loop can catch it and retry
#             logger.add_interrupt("error", error_msg)
#             logger.end_process(success=False, error=error_msg)
#             raise
        
#         if enable_status_updates:
#             _send_status_update(project_id, gallery_id, user_id, job_id, "error", "error", 0, "Scene is too large to render. Please try a different setup.", render_type="VIDEO", logger=logger)
#         logger.add_interrupt("error", error_msg)
#         logger.end_process(success=False, error=error_msg)
#         raise  # Always propagate so retry loop detects the failure
#     finally:
#         pass

# # --- API ENDPOINTS ---

# @app.get("/")
# async def root():
#     """Root endpoint to confirm the service is running."""
#     return {"message": "Service Online: The Dream Engine is ready to bring your visions to life."}





# @app.post("/api/cancel-job/{job_id}")
# async def cancel_job(job_id: str):
#     """
#     Cancel a specific job by marking it as failed.
    
#     **No Authentication Required:** This endpoint is for internal dashboard use only.
    
#     Args:
#         job_id: The UUID of the job to cancel
    
#     Returns:
#         Success message with job details
#     """
#     with queue_lock:
#         if job_id not in active_jobs:
#             raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
#         job = active_jobs[job_id]
        
#         # Only allow cancelling queued or processing jobs
#         if job["status"] not in ["queued", "processing"]:
#             raise HTTPException(
#                 status_code=400, 
#                 detail=f"Cannot cancel job with status '{job['status']}'. Only queued or processing jobs can be cancelled."
#             )
        
#         # Mark job as failed
#         job["status"] = "failed"
#         job["failed_at"] = datetime.now().isoformat()
#         job["error"] = "Cancelled by user"
#         job["message"] = "Job Cancelled: The creation process has been gently set aside."
#         job["progress"] = 0
        
#         print(f"\n{'='*80}\n❌ JOB CANCELLED BY USER\n{'='*80}\nJob ID: {job_id}\nType: {job.get('type', 'unknown')}\nProject: {job.get('project_id', 'unknown')}\n{'='*80}\n")
    

    
#     return JSONResponse(
#         status_code=200,
#         content={
#             "status": "success",
#             "message": f"Job {job_id} cancelled successfully",
#             "job_id": job_id
#         }
#     )


# @app.post("/generate-glb-with-camera/", response_model=None)
# async def generate_glb_with_camera(
#     payload: GenerationPayload,
#     current_user: CurrentUser
# ):
#     """
#     Generates a fast, low-resolution GLB for preview purposes (without camera - camera handled by Godot).
    
#     **Authentication Required:** This endpoint requires a valid API key in the X-API-Key header.
#     """
#     job_id = str(uuid.uuid4())
#     logger = RenderLogger(job_id)
#     logger.start_process()
    
#     try:
#         print(f"Generating low-res GLB for preview (Job ID: {job_id}, User: {current_user.email})")
#         base_glb = _build_base_glb(job_id, payload.floor_plan_data, use_high_res=False, logger=logger)
#         # Create camera coordinates file - pass full payload dict to extract all camera data
#         # Use dict(exclude_unset=False) to include all fields including optional camera data
#         payload_dict = payload.dict(exclude_unset=False, exclude_none=False)
#         camera_json_path = _create_camera_coordinates_file(base_glb, payload_dict, payload.render_quality, payload.aspect_ratio, logger=logger)
#         logger.end_process(success=True)
#         return FileResponse(path=base_glb, media_type='model/gltf-binary', filename=f"model_{job_id}.glb")
#     except subprocess.CalledProcessError as e:
#         error_msg = f"Godot render prep error:\n{e.stdout}\n{e.stderr}"
#         logger.end_process(success=False, error=error_msg)
#         raise HTTPException(status_code=500, detail=error_msg)
#     except Exception as e:
#         logger.end_process(success=False, error=str(e))
#         raise HTTPException(status_code=500, detail=str(e))

# @app.post("/generate-4k-render/")
# async def generate_4k_render_async(
#     current_user: CurrentUser,
#     project_id: int = Query(..., alias="ProjectId", description="The master Project ID for the render."),
#     id: int = Query(..., alias="Id", description="The specific gallery item ID for this render."),
#     user_id: int = Query(0, alias="UserId", description="The user ID for this render."),
#     job_id_param: Optional[str] = Query(None, alias="JobId", description="Optional manual job ID."),
#     payload: GenerationPayload = Body(...)
# ):
#     """
#     Accepts a render job, adds it to the queue, and immediately returns a confirmation.
#     Jobs are processed ONE AT A TIME to prevent render conflicts.
    
#     **Authentication Required:** This endpoint requires a valid API key in the X-API-Key header.
#     """
#     job_id = job_id_param if job_id_param else str(uuid.uuid4())
#     size_info = _calculate_image_render_size(payload)
#     print(f"\n{'='*80}\n[IMAGE] NEW RENDER REQUEST RECEIVED\n{'='*80}")
#     print(f"Job ID: {job_id}\nAuthenticated User: {current_user.email}\nProject ID: {project_id}\nGallery ID: {id}\nUser ID: {user_id}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nQuality: {payload.render_quality}\nOutput: {size_info['output_resolution']}\n{'='*80}")
    
#     if str(payload.draft_render).lower() == 'false' and REROUTE_ENGINE_URL:
#         print(f"\n{COLOR_YELLOW}[FORWARDING] Rerouting IMAGE job {job_id} to external engine: {REROUTE_ENGINE_URL}{COLOR_RESET}")
        
#         async with httpx.AsyncClient() as client:
#             try:
#                 headers = {}
#                 if REROUTE_ENGINE_API_KEY:
#                     headers["X-API-Key"] = REROUTE_ENGINE_API_KEY
                
#                 params = {
#                     "ProjectId": project_id,
#                     "Id": id,
#                     "UserId": user_id,
#                     "JobId": job_id
#                 }
                
#                 response = await client.post(
#                     f"{REROUTE_ENGINE_URL.rstrip('/')}/generate-4k-render/",
#                     json=payload.model_dump(),
#                     params=params,
#                     headers=headers,
#                     timeout=30.0
#                 )
                
#                 if response.status_code in (200, 202):
#                     with queue_lock:
#                         active_jobs[job_id] = {
#                             'status': 'queued',
#                             'position': 0,
#                             'queued_at': datetime.now().isoformat(),
#                             'render_type': 'IMAGE',
#                             'project_id': project_id,
#                             'gallery_id': id,
#                             'processing_externally': True,
#                             **size_info,
#                         }
                    
#                     return JSONResponse(
#                         status_code=202,
#                         content={
#                             "status": "queued",
#                             "message": "Job Queued (External): Your vision has been routed to our high-quality rendering node.",
#                             "job_id": job_id,
#                             "queue_position": 0,
#                             "queue_size": render_queue.qsize(),
#                             "project_id": project_id,
#                             "UserId": user_id,
#                             "RenderId": 0,
#                             "RenderType": "IMAGE",
#                             **size_info,
#                         }
#                     )
#                 else:
#                     print(f"{COLOR_RED}[ERROR] External engine returned {response.status_code}: {response.text}{COLOR_RESET}")
#                     raise HTTPException(status_code=502, detail=f"External engine error: {response.text}")
                    
#             except Exception as e:
#                 print(f"{COLOR_RED}[ERROR] Failed to communicate with external engine: {str(e)}{COLOR_RESET}")
#                 raise HTTPException(status_code=502, detail=f"External engine communication failed: {str(e)}")

#     # Add job to queue
#     job_data = {
#         'job_id': job_id,
#         'type': 'image',
#         'project_id': project_id,
#         'gallery_id': id,
#         'user_id': user_id,
#         'payload': payload,
#         **size_info,
#     }
    
#     # Track job status
#     with queue_lock:
#         queue_position = render_queue.qsize() + 1  # Position in queue (1-indexed)
#         active_jobs[job_id] = {
#             'status': 'queued',
#             'position': queue_position,
#             'queued_at': datetime.now().isoformat(),
#             'render_type': 'IMAGE',
#             'project_id': project_id,
#             'gallery_id': id,
#             **size_info,
#         }
    
#     render_queue.put(job_data)
    
#     # Broadcast queue update to monitoring dashboard

    
#     print(f"\n{COLOR_GREEN}[OK] Job added to queue (Position: {queue_position}){COLOR_RESET}")
#     print(f"[STATS] Current queue size: {render_queue.qsize()}")
#     print(f"[NOTE] Jobs are processed ONE AT A TIME to prevent system overload\n")
    
#     return JSONResponse(
#         status_code=202,
#         content = {
#             "status": "queued",
#             "message": "Job Queued: Your vision has been successfully entrusted to us. The magic begins now.",
#             "job_id": job_id,
#             "queue_position": queue_position,
#             "queue_size": render_queue.qsize(),
#             "project_id": project_id,
#             "UserId": user_id,
#             "RenderId": 0,
#             "RenderType": "IMAGE",
#             **size_info,
#     }
#     )

# @app.post("/generate-4k-video/")
# async def generate_4k_video_async(
#     current_user: CurrentUser,
#     project_id: int = Query(..., alias="ProjectId", description="The master Project ID for the video render."),
#     id: int = Query(..., alias="Id", description="The specific gallery item ID for this video render."),
#     user_id: int = Query(0, alias="UserId", description="The user ID for this video render."),
#     job_id_param: Optional[str] = Query(None, alias="JobId", description="Optional manual job ID."),
#     payload: VideoGenerationPayload = Body(...)
# ):
#     """
#     Accepts a video render job with camera animation, adds it to the queue, and immediately returns a confirmation.
#     Jobs are processed ONE AT A TIME to prevent render conflicts.
    
#     **Authentication Required:** This endpoint requires a valid API key in the X-API-Key header.
#     """
#     job_id = job_id_param if job_id_param else str(uuid.uuid4())
#     size_info = _calculate_video_render_size(payload)
#     video_animation = payload.video_animation
#     duration_seconds = video_animation.duration_seconds if video_animation else 5.0
#     fps = video_animation.fps if video_animation else 30
    
#     print(f"\n{'='*80}\n[VIDEO] NEW VIDEO RENDER REQUEST RECEIVED\n{'='*80}")
#     print(f"Job ID: {job_id}\nAuthenticated User: {current_user.email}\nProject ID: {project_id}\nGallery ID: {id}\nUser ID: {user_id}\nDuration: {duration_seconds}s @ {fps}fps\nOutput: {size_info['output_resolution']}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*80}")
    
#     if str(payload.draft_render).lower() == 'false' and REROUTE_ENGINE_URL:
#         print(f"\n{COLOR_YELLOW}[FORWARDING] Rerouting VIDEO job {job_id} to external engine: {REROUTE_ENGINE_URL}{COLOR_RESET}")
        
#         async with httpx.AsyncClient() as client:
#             try:
#                 headers = {}
#                 if REROUTE_ENGINE_API_KEY:
#                     headers["X-API-Key"] = REROUTE_ENGINE_API_KEY
                
#                 params = {
#                     "ProjectId": project_id,
#                     "Id": id,
#                     "UserId": user_id,
#                     "JobId": job_id
#                 }
                
#                 response = await client.post(
#                     f"{REROUTE_ENGINE_URL.rstrip('/')}/generate-4k-video/",
#                     json=payload.model_dump(),
#                     params=params,
#                     headers=headers,
#                     timeout=30.0
#                 )
                
#                 if response.status_code in (200, 202):
#                     with queue_lock:
#                         active_jobs[job_id] = {
#                             'status': 'queued',
#                             'position': 0,
#                             'queued_at': datetime.now().isoformat(),
#                             'render_type': 'VIDEO',
#                             'project_id': project_id,
#                             'gallery_id': id,
#                             'duration': duration_seconds,
#                             'fps': fps,
#                             'processing_externally': True,
#                             **size_info,
#                         }
                    
#                     return JSONResponse(
#                         status_code=202,
#                         content={
#                             "status": "queued",
#                             "message": "Video Job Queued (External): Your vision has been routed to our high-quality rendering node.",
#                             "job_id": job_id,
#                             "queue_position": 0,
#                             "queue_size": render_queue.qsize(),
#                             "project_id": project_id,
#                             "UserId": user_id,
#                             "RenderId": 0,
#                             "RenderType": "VIDEO",
#                             **size_info,
#                         }
#                     )
#                 else:
#                     print(f"{COLOR_RED}[ERROR] External engine returned {response.status_code}: {response.text}{COLOR_RESET}")
#                     raise HTTPException(status_code=502, detail=f"External engine error: {response.text}")
                    
#             except Exception as e:
#                 print(f"{COLOR_RED}[ERROR] Failed to communicate with external engine: {str(e)}{COLOR_RESET}")
#                 raise HTTPException(status_code=502, detail=f"External engine communication failed: {str(e)}")

#     # Add job to queue
#     job_data = {
#         'job_id': job_id,
#         'type': 'video',
#         'project_id': project_id,
#         'gallery_id': id,
#         'user_id': user_id,
#         'payload': payload,
#         **size_info,
#     }
    
#     # Track job status
#     with queue_lock:
#         queue_position = render_queue.qsize() + 1  # Position in queue (1-indexed)
#         active_jobs[job_id] = {
#             'status': 'queued',
#             'position': queue_position,
#             'queued_at': datetime.now().isoformat(),
#             'render_type': 'VIDEO',
#             'project_id': project_id,
#             'gallery_id': id,
#             'duration': duration_seconds,
#             'fps': fps,
#             **size_info,
#         }
    
#     render_queue.put(job_data)
    
#     # Broadcast queue update to monitoring dashboard

    
#     print(f"\n{COLOR_GREEN}[OK] Video job added to queue (Position: {queue_position}){COLOR_RESET}")
#     print(f"[DB] Current queue size: {render_queue.qsize()}")
#     print(f"💡 Jobs are processed ONE AT A TIME to prevent system overload\n")
    
#     return JSONResponse(
#         status_code=202,
#         content={
#             "status": "queued",
#             "message": "Job Queued: Your cinematic story has been accepted. Preparing the stage.",
#             "job_id": job_id,
#             "queue_position": queue_position,
#             "queue_size": render_queue.qsize(),
#             "project_id": project_id,
#             "UserId": user_id,
#             "RenderId": 0,
#             "RenderType": "VIDEO",
#             "duration_seconds": duration_seconds,
#             "fps": fps,
#             **size_info
#         }
#     )
# @app.get("/logs/{job_id}")
# async def get_job_log(job_id: str):
#     """
#     Returns the JSON render log for a specific job ID.
#     Used by the monitoring system to collect artifacts.
#     """
#     with queue_lock:
#         job = active_jobs.get(job_id)
        
#     if job and job.get('processing_externally') and REROUTE_ENGINE_URL:
#         import httpx
#         try:
#             async with httpx.AsyncClient() as client:
#                 headers = {}
#                 if REROUTE_ENGINE_API_KEY:
#                     headers["X-API-Key"] = REROUTE_ENGINE_API_KEY
#                 resp = await client.get(f"{REROUTE_ENGINE_URL.rstrip('/')}/logs/{job_id}", headers=headers, timeout=5.0)
#                 if resp.status_code == 200:
#                     return JSONResponse(content=resp.json())
#                 elif resp.status_code == 404:
#                     raise HTTPException(status_code=404, detail=f"Log file for external job {job_id} not found yet")
#                 else:
#                     raise HTTPException(status_code=resp.status_code, detail=f"External engine log fetch error: {resp.text}")
#         except Exception as e:
#             if isinstance(e, HTTPException):
#                 raise
#             raise HTTPException(status_code=502, detail=f"Failed to fetch logs from external engine: {e}")

#     log_path = os.path.join("logs", f"{job_id}_render.json")
#     if not os.path.exists(log_path):
#         raise HTTPException(status_code=404, detail=f"Log file for job {job_id} not found")
    
#     return FileResponse(log_path, media_type="application/json", filename=f"{job_id}_render.json")


# # --- AI RENDER ENDPOINT ---
# from fastapi import File, UploadFile

# @app.post("/ai-render/convert-day")
# async def sd_airender(
#     current_user: CurrentUser,
#     project_id: int = Query(..., alias="ProjectId", description="The master Project ID"),
#     id: int = Query(..., alias="Id", description="The specific gallery item ID"),
#     user_id: int = Query(..., alias="UserId", description="The user ID"),
#     job_id_param: Optional[str] = Query(None, alias="JobId", description="Optional existing Job ID"),
#     image: UploadFile = File(..., description="PNG image file to convert"),
# ):
#     if not ai_service.is_libraries_installed:
#         raise HTTPException(status_code=503, detail="AI rendering is disabled - missing dependencies (diffusers, torch, etc.)")
    
#     if image.content_type not in {"image/png", "image/x-png"}:
#         raise HTTPException(status_code=400, detail="Only PNG images are supported for ai render.")
    
#     file_bytes = await image.read()
#     if len(file_bytes) / (1024 * 1024) > ai_service.max_file_mb:
#         raise HTTPException(status_code=400, detail=f"File exceeds {ai_service.max_file_mb}MB.")

#     try:
#         with Image.open(io.BytesIO(file_bytes)) as source_image:
#             input_width, input_height = source_image.size
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=f"Invalid PNG image: {e}")

#     size_info = _calculate_ai_render_size(input_width, input_height, ai_service.target_long_side)
    
#     job_id = job_id_param if job_id_param else str(uuid.uuid4())
#     input_path = ai_service.input_dir / f"{job_id}_{Path(image.filename).stem}.png"
#     output_path = ai_service.output_dir / f"{job_id}_render.png"
    
#     # Save uploaded file
#     try:
#         input_path.write_bytes(file_bytes)
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"Failed to save input image: {e}")
    
#     with queue_lock:
#         active_jobs[job_id] = {
#             "status": "queued",
#             "type": "ai render",
#             "display_type": "ai render",
#             "render_type": ai_service.status_render_type,
#             "project_id": project_id,
#             "gallery_id": id,
#             "user_id": user_id,
#             "queued_at": datetime.now().isoformat(),
#             "progress": 0,
#             "message": "AI render job queued.",
#             "current_step": "queued",
#             "input_filename": image.filename,
#             "output_filename": output_path.name,
#             "started_at": None,
#             "completed_at": None,
#             "failed_at": None,
#             **size_info,
#         }
    
#     job_data = {
#         "job_id": job_id,
#         "project_id": project_id,
#         "gallery_id": id,
#         "user_id": user_id,
#         "input_path": str(input_path),
#         "output_path": str(output_path),
#         "filename": image.filename,
#         **size_info,
#     }
    
#     ai_service.render_queue.put(job_data)
    
#     print(f"{COLOR_GREEN}[OK] AI Render job added to queue: {job_id}{COLOR_RESET}")
    
#     return JSONResponse(
#         status_code=202,
#         content={
#             "status": "queued",
#             "job_id": job_id,
#             "ProjectId": project_id,
#             "Id": id,
#             "UserId": user_id,
#             "RenderType": ai_service.status_render_type,
#             **size_info,
#         }
#     )


# @app.get("/engine-status")
# def ai_engine_status():
#     jobs = _get_ai_jobs_snapshot()
#     queued = sum(1 for job in jobs if job.get("status") == "queued")
#     processing = sum(1 for job in jobs if job.get("status") == "processing")
#     completed = sum(1 for job in jobs if job.get("status") == "completed")
#     failed = sum(1 for job in jobs if job.get("status") in {"failed", "error"})

#     current_job = CURRENT_STATUS.get("active_job")
#     if current_job and isinstance(current_job, dict):
#         current_job = dict(current_job)
#         current_job_id = current_job.get("id")
#         if current_job_id:
#             with queue_lock:
#                 live_job = active_jobs.get(current_job_id, {})
#             current_job.update(_extract_job_render_size(live_job))

#     return {
#         "status": "ok",
#         "online": True,
#         "is_paused": False,
#         "is_busy": CURRENT_STATUS.get("is_busy", False),
#         "queue_size": queued,
#         "active_jobs": queued + processing,
#         "completed_jobs": completed,
#         "failed_jobs": failed,
#         "current_job": current_job,
#     }


# @app.get("/health")
# def ai_health():
#     return ai_engine_status()


# @app.get("/queue-status-public")
# def ai_queue_status_public():
#     jobs = _get_ai_jobs_snapshot()
#     queued = sum(1 for job in jobs if job.get("status") == "queued")
#     processing = sum(1 for job in jobs if job.get("status") == "processing")
#     completed = sum(1 for job in jobs if job.get("status") == "completed")
#     failed = sum(1 for job in jobs if job.get("status") in {"failed", "error"})

#     return JSONResponse(
#         content={
#             "queue_size": queued,
#             "active_jobs_count": queued + processing,
#             "processing_jobs_count": processing,
#             "completed_jobs_count": completed,
#             "failed_jobs_count": failed,
#             "is_paused": False,
#             "jobs": jobs,
#             "engine_online": True,
#             "engine_name": "External 4K + AI Engine",
#             "engine_url": os.getenv("AI_RENDER_ENGINE_URL", "embedded://external-4k-ai-main"),
#         }
#     )


# @app.get("/queue-status")
# def ai_queue_status():
#     return ai_queue_status_public()


# @app.get("/job-status/{job_id}")
# def ai_job_status(job_id: str):
#     with queue_lock:
#         job = active_jobs.get(job_id)
#     if not job:
#         raise HTTPException(status_code=404, detail=f"AI job {job_id} not found")
#     return {"job_id": job_id, **_build_ai_job_snapshot(job_id, dict(job))}


# @app.get("/ai-render/health")
# def ai_render_health_alias():
#     return ai_engine_status()


# @app.get("/ai-render/")
# @app.get("/ai-render")
# def ai_render_root_alias():
#     return ai_engine_status()


# @app.get("/ai-render/queue-status-public")
# def ai_render_queue_status_public_alias():
#     return ai_queue_status_public()


# @app.get("/ai-render/queue-status")
# def ai_render_queue_status_alias():
#     return ai_queue_status_public()


# @app.get("/ai-render/job-status/{job_id}")
# def ai_render_job_status_alias(job_id: str):
#     return ai_job_status(job_id)
# filename: main.py
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Body, Depends, WebSocket, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List, Set, Dict, Any
from typing_extensions import Annotated
from pathlib import Path
from PIL import Image
import os
import sys
import io
import json
import uuid
import copy
import hashlib
import subprocess
import requests
import tempfile
import httpx
from urllib.parse import urlparse
from datetime import datetime
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, wait
import shlex
import re
import glob
from app.database import models, session
from app.services import auth_service
from app.services.render_logger import RenderLogger
from threading import Lock, Thread
import queue
import signal
import asyncio
import logging
from dotenv import load_dotenv

# AI Render Service - Moved to app/services/ai_render_service.py
from app.services.ai_render_service import get_ai_render_service

# Load environment variables from .env file
load_dotenv()

REROUTE_ENGINE_URL = os.getenv("REROUTE_ENGINE_URL", "")
REROUTE_ENGINE_API_KEY = os.getenv("REROUTE_ENGINE_API_KEY", "")


map_lock = Lock()

# --- CONSOLE COLORS ---
COLOR_RED = "\033[91m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_BLUE = "\033[94m"
COLOR_RESET = "\033[0m"

# --- ASSET & TEXTURE MASTERY API CONFIGURATION ---
ASSETS_AND_TEXTURE_API_KEY = os.getenv("ASSETS_AND_TEXTURE_API_KEY")
ASSETS_AND_TEXTURE_API_HEADER_NAME = os.getenv("ASSETS_AND_TEXTURE_API_HEADER_NAME", "ZRealtyServiceApiKey")
ASSET_ENDPOINT = os.getenv("ASSET_ENDPOINT")
TEXTURE_ENDPOINT = os.getenv("TEXTURE_ENDPOINT")

# --- BASE DIRECTORY ---
BASE_DIR = Path(__file__).resolve().parent

# AI Render Service Initialization
ai_service = get_ai_render_service(BASE_DIR)

# Global Cache for Master Data
_CACHED_ASSETS_MAP = {}
_CACHED_TEXTURES_MAP = {}
_CACHED_ASSET_URL_TO_HP_MAP = {}  # Map: Any GLB URL -> its HP GLB URL
_CACHED_TEXTURE_URL_TO_HP_MAP = {} # Map: Any Texture URL -> its HR Texture URL
_MASTER_DATA_FETCHED = False
_MASTER_DATA_LOCK = Lock()

def _fetch_master_data_from_api():
    """
    Fetches Asset and Texture master data from the API and populates the global caches.
    This should be called once at the start of the download process.
    """
    global _CACHED_ASSETS_MAP, _CACHED_TEXTURES_MAP, _CACHED_ASSET_URL_TO_HP_MAP, _MASTER_DATA_FETCHED
    
    with _MASTER_DATA_LOCK:
        if _MASTER_DATA_FETCHED:
            return

        print(f"\n{COLOR_BLUE}--- Fetching Master Data from API... ---{COLOR_RESET}")
        headers = {
            ASSETS_AND_TEXTURE_API_HEADER_NAME: ASSETS_AND_TEXTURE_API_KEY,
            "Content-Type": "application/json"
        }
        
        # 1. Fetch Assets
        try:
            print(f"  Requesting Assets from: {ASSET_ENDPOINT}")
            # verify=False to avoid SSL issues with some internal/QA certs
            resp = requests.get(ASSET_ENDPOINT, headers=headers, verify=False, timeout=60)
            if resp.status_code == 200:
                assets_list = resp.json()
                if isinstance(assets_list, list):
                    # Map ID -> Asset Object
                    _CACHED_ASSETS_MAP = {item.get('asset3D_Id'): item for item in assets_list if 'asset3D_Id' in item}
                    
                    # --- NEW: Build URL-to-HP mapping ---
                    _CACHED_ASSET_URL_TO_HP_MAP = {}
                    for item in assets_list:
                        hp_url = item.get('highPoly_Glb')
                        if not hp_url or not isinstance(hp_url, str):
                            continue
                        
                        # Normalize HP URL
                        norm_hp = hp_url.strip().replace("\\", "/")
                        if norm_hp.startswith("/"): norm_hp = norm_hp[1:]
                        
                        # Assets to map to this HP
                        potential_keys = [
                            item.get('lowPoly_Glb'),
                            item.get('mediumPoly_Glb'),
                            item.get('highPoly_Glb')
                        ]
                        for k in potential_keys:
                            if k and isinstance(k, str):
                                norm_k = k.strip().replace("\\", "/")
                                if norm_k.startswith("/"): norm_k = norm_k[1:]
                                # Store exact relative path (e.g. glb-assets/...)
                                _CACHED_ASSET_URL_TO_HP_MAP[norm_k] = norm_hp
                                # Store filename only (fallback)
                                _CACHED_ASSET_URL_TO_HP_MAP[os.path.basename(norm_k)] = norm_hp
                                # Store with full blob prefix if missing
                                if not norm_k.startswith("http"):
                                     full_u = f"https://zrealtystoragedev.blob.core.windows.net/{norm_k}"
                                     _CACHED_ASSET_URL_TO_HP_MAP[full_u] = norm_hp
                    
                    print(f"  {COLOR_GREEN}[OK] Loaded {len(_CACHED_ASSETS_MAP)} assets and indexed {len(_CACHED_ASSET_URL_TO_HP_MAP)} URLs.{COLOR_RESET}")
                    # DEBUG: Check for specific ID 147
                    if 147 in _CACHED_ASSETS_MAP or "147" in _CACHED_ASSETS_MAP:
                        print(f"  {COLOR_GREEN}[OK] Asset ID 147 successfully loaded in cache.{COLOR_RESET}")
                    else:
                        print(f"  {COLOR_RED}[X] Asset ID 147 NOT found in cache.{COLOR_RESET}")

                else:
                    print(f"  {COLOR_RED}[X] Asset response was not a list.{COLOR_RESET}")
            else:
                if resp.status_code == 404:
                    print(f"  {COLOR_YELLOW}[!] Asset fetch failed: 404 Not Found (Master data not available){COLOR_RESET}")
                else:
                    error_text = resp.text[:100] if resp.text and not resp.text.strip().startswith("<") else "Unknown Error"
                    print(f"  {COLOR_RED}[X] Asset fetch failed: {resp.status_code} - {error_text}{COLOR_RESET}")
        except Exception as e:
            print(f"  {COLOR_RED}[X] Asset fetch exception: {e}{COLOR_RESET}")

        # 2. Fetch Textures
        try:
            print(f"  Requesting Textures from: {TEXTURE_ENDPOINT}")
            resp = requests.get(TEXTURE_ENDPOINT, headers=headers, verify=False, timeout=60)
            if resp.status_code == 200:
                textures_list = resp.json()
                if isinstance(textures_list, list):
                    # Map ID -> Texture Object
                    _CACHED_TEXTURES_MAP = {item.get('textureLibrary_Id'): item for item in textures_list if 'textureLibrary_Id' in item}
                    
                    # --- NEW: Build Texture URL mapping ---
                    _CACHED_TEXTURE_URL_TO_HP_MAP = {}
                    for item in textures_list:
                        hr_urls = item.get('high_Resolution_Url')
                        lr_urls = item.get('low_Resolution_Url')
                        
                        if hr_urls and isinstance(hr_urls, list):
                            # Map everything to HR list
                            potential_sources = []
                            if hr_urls: potential_sources.extend(hr_urls)
                            if lr_urls and isinstance(lr_urls, list): potential_sources.extend(lr_urls)
                            
                            for s in potential_sources:
                                if s and isinstance(s, str):
                                    norm_s = s.strip().replace("\\", "/")
                                    # Texture Library maps to a list of HR URLs
                                    _CACHED_TEXTURE_URL_TO_HP_MAP[norm_s] = hr_urls
                    
                    print(f"  {COLOR_GREEN}[OK] Loaded {len(_CACHED_TEXTURES_MAP)} textures and indexed {len(_CACHED_TEXTURE_URL_TO_HP_MAP)} urls.{COLOR_RESET}")
                else:
                    print(f"  {COLOR_RED}[X] Texture response was not a list.{COLOR_RESET}")
            else:
                if resp.status_code == 404:
                     print(f"  {COLOR_YELLOW}[!] Texture fetch failed: 404 Not Found (Master data not available){COLOR_RESET}")
                else:
                    error_text = resp.text[:100] if resp.text and not resp.text.strip().startswith("<") else "Unknown Error"
                    print(f"  {COLOR_RED}[X] Texture fetch failed: {resp.status_code} - {error_text}{COLOR_RESET}")
        except Exception as e:
            print(f"  {COLOR_RED}[X] Texture fetch exception: {e}{COLOR_RESET}")
            
        _MASTER_DATA_FETCHED = True

def _resolve_asset_url(asset_id, show_details=False):
    """
    Looks up asset_id in the cache and returns the best available GLB URL.
    HighPoly is preferred, then glb_Url as a fallback.
    """
    if not asset_id:
        return None
        
    # Robust lookup: Try as is, then as int, then as str
    asset = _CACHED_ASSETS_MAP.get(asset_id)
    if not asset:
        try:
            asset = _CACHED_ASSETS_MAP.get(int(asset_id))
        except (ValueError, TypeError):
            pass
    if not asset:
        asset = _CACHED_ASSETS_MAP.get(str(asset_id))

    if not asset:
        if show_details:
             print(f"  {COLOR_YELLOW}Warning: Asset ID {asset_id} not found in master data (Checked int/str).{COLOR_RESET}")
        return None
        
    # Priority: High Poly first, then fall back to the main GLB URL.
    hp_url = asset.get('highPoly_Glb') or asset.get('glb_Url')

    if show_details:
        print(f"DEBUG: Resolving Asset {asset_id}. Found HP URL: {hp_url}")

    # If keys are None or empty string, treat as empty
    hp_url = hp_url if (hp_url and isinstance(hp_url, str) and hp_url.strip()) else None

    # Convert relative blob paths to full URLs for the downloader.
    final_url = _convert_glb_path_to_url(hp_url) if hp_url else None
    
    if final_url:
        return final_url
    return None

def _resolve_hp_url_from_url(url: str, show_details=False) -> Any:
    """
    Given a URL or path, looks up if it matches:
    1. Any known asset's GLB URL -> returns HP GLB URL (str)
    2. Any known texture's URL -> returns list of HR URLs (list[str])
    """
    if not url or not isinstance(url, str):
        return None
        
    norm_url = url.strip().replace("\\", "/")
    if norm_url.startswith("/"): norm_url = norm_url[1:]
    
    # NEW: Handle spaces in URL (decode %20)
    if '%' in norm_url:
        from urllib.parse import unquote
        norm_url_decoded = unquote(norm_url)
    else:
        norm_url_decoded = norm_url

    # 1. Check Assets Map (GLBs)
    # Try exact match first
    hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(norm_url)
    
    # Try decoded match (e.g. "Office Table_LP.glb" vs "Office%20Table_LP.glb")
    if not hp_found:
        hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(norm_url_decoded)

    # NEW: Handle spaces in URL -> Try ENCODED match (Space -> %20)
    # The Map might store keys as proper URLs (with %20), but input has spaces.
    if not hp_found and ' ' in norm_url:
        from urllib.parse import quote
        # Quote paths but not slashes/colons generally, though for just filename/path component
        # a simple replace for space is safest given the context of GLB filenames.
        norm_url_encoded = norm_url.replace(" ", "%20")
        hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(norm_url_encoded)
    # Try decoded match (e.g. "Office Table_LP.glb" vs "Office%20Table_LP.glb")
    if not hp_found:
        hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(norm_url_decoded)
    
    if not hp_found:
        path_part = urlparse(url).path
        if path_part.startswith("/"): path_part = path_part[1:]
        hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(path_part)
        
        # Try decoded path part
        if not hp_found:
            path_part_decoded = unquote(path_part) if '%' in path_part else path_part
            hp_found = _CACHED_ASSET_URL_TO_HP_MAP.get(path_part_decoded)
        
    if hp_found:
        full_hp_url = _convert_glb_path_to_url(hp_found)
        if show_details:
            print(f"  {COLOR_GREEN}✓ Resolved Asset {os.path.basename(url)} -> {os.path.basename(full_hp_url)} via URL match{COLOR_RESET}")
        return full_hp_url
        
    # 2. Check Textures Map (Images)
    hr_list = _CACHED_TEXTURE_URL_TO_HP_MAP.get(norm_url)
    if not hr_list:
        # Also try just the filename/path part for textures? 
        # Usually textures have long paths. Let's try direct norm_url first.
        # If no match, try without SAS if present
        if '?' in norm_url:
            base_norm = norm_url.split('?')[0]
            hr_list = _CACHED_TEXTURE_URL_TO_HP_MAP.get(base_norm)

    if hr_list and isinstance(hr_list, list):
        if show_details:
             print(f"  {COLOR_GREEN}✓ Resolved Texture {os.path.basename(url)} -> HR List ({len(hr_list)} items) via URL match{COLOR_RESET}")
        return hr_list
        
    return None

def _resolve_texture_urls(texture_id, show_details=False):
    """
    Looks up texture_id and returns a list of High Resolution URLs.
    """
    if not texture_id:
        return []
        
    texture = _CACHED_TEXTURES_MAP.get(texture_id)
    if not texture:
        if show_details:
             print(f"  {COLOR_YELLOW}Warning: Texture ID {texture_id} not found in master data.{COLOR_RESET}")
        return []
        
    # Try High Resolution first
    urls = texture.get('high_Resolution_Url')
    if not urls:
        urls = texture.get('low_Resolution_Url')
        
    if urls and isinstance(urls, list):
         return [
             u if u.startswith("http") else f"https://zrealtystoragedev.blob.core.windows.net/{u.lstrip('/')}"
             for u in urls
             if isinstance(u, str) and u.strip()
         ]
    return []


# --- RENDER QUEUE SYSTEM ---
# Queue to hold render jobs (processes one at a time to avoid render conflicts)
render_queue = queue.Queue()
queue_lock = Lock()
active_jobs = {}  # Track job status: {job_id: {"status": "queued|processing|completed|failed", "position": int}}
is_shutting_down = False  # Flag to track shutdown state

# --- GLOBAL STATUS CACHE (Recommended Python Implementation) ---
# Optimized for high-frequency polling
CURRENT_STATUS = {
    "is_busy": False,
    "last_updated": time.time(),
    "active_job": None
}

def _update_global_status(
    job_id=None,
    status=None,
    step=None,
    progress=None,
    message=None,
    project_id=None,
    gallery_id=None,
    job_type=None,
    render_size: Optional[Dict[str, Any]] = None,
):
    """
    Updates the lightweight global status dictionary for O(1) polling.
    """
    global CURRENT_STATUS
    CURRENT_STATUS["last_updated"] = time.time()
    if render_size is None and job_id:
        with queue_lock:
            render_size = _extract_job_render_size(active_jobs.get(job_id, {}))
    
    # Determine if busy based on status
    is_active_state = status and status.lower() in ['processing', 'starting', 'rendering', 'encoding', 'uploading', 'initializing', 'queued']
    
    if is_active_state:
        CURRENT_STATUS["is_busy"] = status.lower() != 'queued' # Queued doesn't mean busy engine necessarily, but "processing" does.
        # Actually prompt says "Busy (Job Running)". Queued is waiting.
        
        if status.lower() == 'processing' or CURRENT_STATUS["is_busy"]:
             CURRENT_STATUS["is_busy"] = True

        # Only update active_job details if we have them and it's an active state
        if job_id and CURRENT_STATUS["is_busy"]:
            # If we don't have an active job or it's a different one, start fresh
            if not CURRENT_STATUS["active_job"] or CURRENT_STATUS["active_job"].get("id") != job_id:
                  CURRENT_STATUS["active_job"] = {
                    "id": job_id,
                    "project_id": project_id,
                    "gallery_id": gallery_id,
                    "status": status,
                    "step": step or "Initializing",
                    "progress": progress if progress is not None else 0,
                    "message": message or "Starting...",
                    "started_at": datetime.now().strftime("%H:%M:%S"),
                    "type": job_type if job_type else "image"
                }
                  if render_size:
                      CURRENT_STATUS["active_job"].update(render_size)
            else:
                # Update existing
                current = CURRENT_STATUS["active_job"]
                if status: current["status"] = status
                if step: current["step"] = step
                if progress is not None: current["progress"] = progress
                if message: current["message"] = message
                # Don't overwrite IDs if not provided
                if project_id: current["project_id"] = project_id
                if gallery_id: current["gallery_id"] = gallery_id
                if job_type: current["type"] = job_type
                if render_size:
                    current.update(render_size)
                
    elif status and status.lower() in ['completed', 'failed', 'error', 'cancelled']:
        CURRENT_STATUS["is_busy"] = False
        # Preserve active_job info on failure so monitor can read it
        if CURRENT_STATUS["active_job"]:
             CURRENT_STATUS["active_job"]["status"] = status
             if message: CURRENT_STATUS["active_job"]["message"] = message
             if step: CURRENT_STATUS["active_job"]["step"] = step
             if progress is not None: CURRENT_STATUS["active_job"]["progress"] = progress
             if render_size:
                 CURRENT_STATUS["active_job"].update(render_size)
    elif status is None and job_id is None:
        # Reset
        CURRENT_STATUS["is_busy"] = False
        CURRENT_STATUS["active_job"] = None


def _build_ai_job_snapshot(job_id: str, job: Dict[str, Any]) -> Dict[str, Any]:
    """Return a monitor-friendly snapshot of an AI render job."""
    status = str(job.get("status") or "queued").lower()
    progress = int(job.get("progress", 0) or 0)
    queued_at = job.get("queued_at")
    started_at = job.get("started_at")
    completed_at = job.get("completed_at")
    failed_at = job.get("failed_at")
    details = job.get("details") or {}
    if not isinstance(details, dict):
        details = {"raw": details}

    return {
        "job_id": job_id,
        "type": job.get("type") or job.get("render_type") or "ai render",
        "display_type": job.get("display_type") or "ai render",
        "render_type": job.get("render_type") or "AI_RENDER",
        "status": status,
        "project_id": job.get("project_id", 0),
        "gallery_id": job.get("gallery_id", 0),
        "user_id": job.get("user_id", 0),
        "progress": progress,
        "message": job.get("message") or "AI render job queued.",
        "current_step": job.get("current_step") or ("completed" if status == "completed" else status),
        "queued_at": queued_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "failed_at": failed_at,
        "error": job.get("error"),
        "image_url": job.get("image_url"),
        "output_filename": job.get("output_filename"),
        "input_filename": job.get("input_filename"),
        "input_width": job.get("input_width"),
        "input_height": job.get("input_height"),
        "input_resolution": job.get("input_resolution"),
        "output_width": job.get("output_width"),
        "output_height": job.get("output_height"),
        "output_resolution": job.get("output_resolution"),
        "output_size_bytes": job.get("output_size_bytes"),
        "output_size_mb": job.get("output_size_mb"),
        "render_size": job.get("render_size"),
        "details": details,
        "queue_source": job.get("queue_source", "External AI Engine"),
        "is_ai_render": True,
        "engine_name": "External 4K + AI Engine",
        "engine_url": os.getenv("AI_RENDER_ENGINE_URL", "embedded://external-4k-ai-main"),
        "estimated_eta_minutes": job.get("estimated_eta_minutes"),
        "estimated_wait_minutes": job.get("estimated_wait_minutes"),
        "completed": status == "completed",
        "failed": status in {"failed", "error"},
    }


def _get_ai_jobs_snapshot() -> List[Dict[str, Any]]:
    with queue_lock:
        snapshot = [
            _build_ai_job_snapshot(job_id, dict(job))
            for job_id, job in active_jobs.items()
        ]

    # Fetch external jobs and merge
    if REROUTE_ENGINE_URL:
        try:
            headers = {}
            if REROUTE_ENGINE_API_KEY:
                headers["X-API-Key"] = REROUTE_ENGINE_API_KEY
            resp = requests.get(f"{REROUTE_ENGINE_URL.rstrip('/')}/queue-status-public", headers=headers, timeout=2.0)
            if resp.status_code == 200:
                external_jobs = resp.json().get("jobs", [])
                snapshot_dict = {job["job_id"]: job for job in snapshot}
                for ext_job in external_jobs:
                    ext_job["is_external"] = True
                    snapshot_dict[ext_job["job_id"]] = ext_job
                snapshot = list(snapshot_dict.values())
        except Exception as e:
            print(f"[WARNING] Failed to fetch external jobs for monitoring: {e}")

    def _sort_key(item: Dict[str, Any]):
        status_rank = 0 if item["status"] in {"processing", "queued"} else 1
        queued_at = item.get("queued_at") or ""
        started_at = item.get("started_at") or ""
        return (status_rank, queued_at, started_at, item["job_id"])

    snapshot.sort(key=_sort_key)

    queued = sum(1 for job in snapshot if job["status"] == "queued")
    processing = sum(1 for job in snapshot if job["status"] == "processing")
    completed = sum(1 for job in snapshot if job["status"] == "completed")
    failed = sum(1 for job in snapshot if job["status"] in {"failed", "error"})
    queued_jobs = [job for job in snapshot if job["status"] == "queued"]

    for idx, job in enumerate(queued_jobs, start=1):
        job["queue_position"] = idx
        if job.get("estimated_eta_minutes") is None:
            job["estimated_eta_minutes"] = round(max(1.0, idx * 2.0), 1)
        job["estimated_wait_minutes"] = job["estimated_eta_minutes"]

    return snapshot


def _graceful_shutdown(signum, frame):
    """
    Graceful shutdown handler - sends error status to all active jobs.
    Called when Ctrl+C or terminal close is detected.
    """
    global is_shutting_down
    
    if is_shutting_down:
        return  # Already shutting down
    
    is_shutting_down = True
    
    print(f"\n{COLOR_YELLOW}{'='*80}")
    print(f"[WARN]  SHUTDOWN SIGNAL RECEIVED - Initiating graceful shutdown...")
    print(f"{'='*80}{COLOR_RESET}\n")
    
    # 1. Stop accepting new jobs
    print(f"🛑 Stopping new job acceptance...")
    
    # Get current queue status
    with queue_lock:
        queued_jobs = [(job_id, info) for job_id, info in active_jobs.items() if info.get('status') == 'queued']
        processing_jobs = [(job_id, info) for job_id, info in active_jobs.items() if info.get('status') == 'processing']
        total_jobs = len(queued_jobs) + len(processing_jobs)
    
    # Print queue summary
    print(f"\n[DB] Current Queue Status:")
    print(f"   Total Active Jobs: {total_jobs}")
    print(f"   Queued Jobs: {len(queued_jobs)}")
    print(f"   Processing Jobs: {len(processing_jobs)}")
    
    if total_jobs == 0:
        print(f"\n{COLOR_GREEN}✓ Queue: NULL (No active jobs to cancel){COLOR_RESET}\n")
    else:
        print(f"\n📋 Job Details:")
    
    # 2. Send error status to all queued jobs
    if queued_jobs:
        print(f"\n📤 Sending error status to {len(queued_jobs)} queued job(s)...")
        for job_id, job_info in queued_jobs:
            job_type = job_info.get('type', 'IMAGE').upper()
            project_id = job_info.get('project_id', 'N/A')
            gallery_id = job_info.get('gallery_id', 'N/A')
            print(f"   → Job: {job_id[:8]}... | Type: {job_type} | Project: {project_id} | Gallery: {gallery_id}")
            try:
                _send_status_update(
                    project_id=job_info.get('project_id', 0),
                    gallery_id=job_info.get('gallery_id', 0),
                    user_id=0,
                    job_id=job_id,
                    status="error",
                    step="shutdown",
                    progress=0,
                    message="Sorry, the rendering process was interrupted.",
                    render_type=job_type
                )
                print(f"     ✓ Status 4 (Error) sent successfully")
            except Exception as e:
                print(f"     [WARN]  Failed to send status: {e}")
            
            # UPDATE LOCAL STATE
            with queue_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]['status'] = 'failed'
                    active_jobs[job_id]['error'] = 'Server shutdown - job cancelled'
                    active_jobs[job_id]['failed_at'] = datetime.now().isoformat()
                    active_jobs[job_id]['message'] = 'Sorry, the rendering process was interrupted.'
    
    # 3. Send error status to currently processing job
    if processing_jobs:
        print(f"\n📤 Sending error status to {len(processing_jobs)} processing job(s)...")
        for job_id, job_info in processing_jobs:
            job_type = job_info.get('type', 'IMAGE').upper()
            project_id = job_info.get('project_id', 'N/A')
            gallery_id = job_info.get('gallery_id', 'N/A')
            print(f"   → Job: {job_id[:8]}... | Type: {job_type} | Project: {project_id} | Gallery: {gallery_id}")
            try:
                _send_status_update(
                    project_id=job_info.get('project_id', 0),
                    gallery_id=job_info.get('gallery_id', 0),
                    user_id=0,
                    job_id=job_id,
                    status="error",
                    step="shutdown",
                    progress=0,
                    message="Sorry, the rendering process was interrupted.",
                    render_type=job_type
                )
                print(f"     ✓ Status 4 (Error) sent successfully")
            except Exception as e:
                print(f"     [WARN]  Failed to send status: {e}")
            
            # UPDATE LOCAL STATE to ensure monitoring dashboard sees the failure immediately
            with queue_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]['status'] = 'failed'
                    active_jobs[job_id]['error'] = 'Server shutdown - render interrupted'
                    active_jobs[job_id]['failed_at'] = datetime.now().isoformat()
                    active_jobs[job_id]['message'] = 'Sorry, the rendering process was interrupted.'

    
    # 4. Signal queue worker to stop
    print(f"Stopping render worker queue...")
    try:
        render_queue.put(None)  # Poison pill
        ai_service.shutdown()
    except:
        pass
    
    print(f"\n{COLOR_GREEN}[OK] Graceful shutdown complete{COLOR_RESET}")
    print(f"{'='*80}\n")
    
    # Don't call sys.exit() - let the server shut down naturally
    # This prevents SystemExit from propagating through async operations

FAILED_INPUTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "failed_inputs")
os.makedirs(FAILED_INPUTS_DIR, exist_ok=True)

# Maximum number of render attempts before marking a job as permanently failed
MAX_RENDER_ATTEMPTS = 3

def _save_failed_input(job_data: dict, error_msg: str, attempt: int):
    """
    Saves the failed render input JSON and error details to the failed_inputs/ folder.
    Files are named based on user_id and project_id for easy identification.
    """
    try:
        user_id = job_data.get('user_id', 'unknown')
        project_id = job_data.get('project_id', 'unknown')
        gallery_id = job_data.get('gallery_id', 'unknown')
        job_id = job_data.get('job_id', 'unknown')
        job_type = job_data.get('type', 'unknown')
        timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')

        # Base name: user_{user_id}_project_{project_id}_{timestamp}
        base_name = f"user_{user_id}_project_{project_id}_{timestamp_str}"

        # 1. Save the input JSON payload
        input_json_path = os.path.join(FAILED_INPUTS_DIR, f"{base_name}_input.json")
        payload_obj = job_data.get('payload')
        if payload_obj is not None:
            # Pydantic model → dict if possible
            try:
                payload_dict = payload_obj.dict() if hasattr(payload_obj, 'dict') else payload_obj
            except Exception:
                payload_dict = str(payload_obj)
            with open(input_json_path, 'w', encoding='utf-8') as f:
                json.dump(payload_dict, f, indent=2, ensure_ascii=False, default=str)
        else:
            with open(input_json_path, 'w', encoding='utf-8') as f:
                json.dump({"error": "payload was None"}, f, indent=2)

        # 2. Save the error details text file
        error_txt_path = os.path.join(FAILED_INPUTS_DIR, f"{base_name}_error.txt")
        with open(error_txt_path, 'w', encoding='utf-8') as f:
            f.write(f"{'='*60}\n")
            f.write(f"RENDER FAILURE REPORT\n")
            f.write(f"{'='*60}\n\n")
            f.write(f"Timestamp      : {datetime.now().isoformat()}\n")
            f.write(f"Job ID         : {job_id}\n")
            f.write(f"Job Type       : {job_type.upper()}\n")
            f.write(f"User ID        : {user_id}\n")
            f.write(f"Project ID     : {project_id}\n")
            f.write(f"Gallery ID     : {gallery_id}\n")
            f.write(f"Total Attempts : {attempt}/{MAX_RENDER_ATTEMPTS}\n")
            f.write(f"\n{'─'*60}\n")
            f.write(f"ERROR DETAILS:\n")
            f.write(f"{'─'*60}\n")
            f.write(f"{error_msg}\n")
            f.write(f"\n{'='*60}\n")

        print(f"{COLOR_YELLOW}📁 Failed input saved to: {FAILED_INPUTS_DIR}{COLOR_RESET}")
        print(f"   📄 Input JSON : {os.path.basename(input_json_path)}")
        print(f"   📄 Error File : {os.path.basename(error_txt_path)}")

    except Exception as save_err:
        print(f"{COLOR_RED}[WARN]  Could not save failed input: {save_err}{COLOR_RESET}")


def render_queue_worker():
    """
    Background worker thread that processes render jobs one at a time from the queue.
    This prevents multiple render instances from running simultaneously.

    RETRY PIPELINE:
      • Each job is attempted up to MAX_RENDER_ATTEMPTS (3) times.
      • On attempts 1 and 2: failure is logged but NO error status is sent to
        the external API. The job is silently retried.
      • On the final attempt (3rd): if it still fails, the error status IS sent,
        the input JSON and failure reason are saved to failed_inputs/, and
        the worker moves on to the next job.
    """
    print(f"\n{'='*80}\n[RUN] RENDER QUEUE WORKER STARTED (with retry pipeline: max {MAX_RENDER_ATTEMPTS} attempts)\n{'='*80}\n")
    
    while True:
        try:
            # Get next job from queue (blocks until a job is available)
            job_data = render_queue.get()
            
            if job_data is None:  # Poison pill to stop the worker
                print(f"{COLOR_YELLOW}🛑 Queue worker received shutdown signal{COLOR_RESET}")
                break
            
            # Check if shutting down
            if is_shutting_down:
                print(f"{COLOR_YELLOW}[WARN]  Skipping job due to shutdown{COLOR_RESET}")
                render_queue.task_done()
                continue
            
            job_id = job_data['job_id']
            job_type = job_data['type']  # 'image' or 'video'
            
            # Update job status to processing
            with queue_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]['status'] = 'processing'
                    active_jobs[job_id]['started_at'] = datetime.now().isoformat()
                    active_jobs[job_id]['progress'] = 0
                    active_jobs[job_id]['current_step'] = 'starting'
                    active_jobs[job_id]['message'] = 'Starting render process'
            
            # UPDATE GLOBAL STATUS
            _update_global_status(
                job_id=job_id, 
                status='processing',
                step='Starting render process',
                progress=0,
                message='Initializing engine...',
                project_id=job_data.get('project_id'),
                gallery_id=job_data.get('gallery_id'),
                job_type=job_type
            )

            
            print(f"\n{'='*80}\n🎬 PROCESSING JOB FROM QUEUE\n{'='*80}")
            print(f"Job ID: {job_id}")
            print(f"Type: {job_type.upper()}")
            print(f"Queue Size: {render_queue.qsize()} remaining")
            print(f"Max Attempts: {MAX_RENDER_ATTEMPTS}")
            print(f"{'='*80}\n")
            
            # ── RETRY LOOP ──────────────────────────────────────────────
            job_succeeded = False
            last_error_msg = ""
            
            for attempt in range(1, MAX_RENDER_ATTEMPTS + 1):
                is_final_attempt = (attempt == MAX_RENDER_ATTEMPTS)
                
                try:
                    if attempt > 1:
                        print(f"\n{'─'*60}")
                        print(f"[RUN] RETRY ATTEMPT {attempt}/{MAX_RENDER_ATTEMPTS} for Job {job_id}")
                        print(f"{'─'*60}\n")
                        
                        # Brief pause before retry to let resources settle
                        time.sleep(3)
                        
                        # Reset job status for retry
                        with queue_lock:
                            if job_id in active_jobs:
                                active_jobs[job_id]['status'] = 'processing'
                                active_jobs[job_id]['progress'] = 0
                                active_jobs[job_id]['current_step'] = f'retry_{attempt}'
                                active_jobs[job_id]['message'] = f'Retrying render (attempt {attempt}/{MAX_RENDER_ATTEMPTS})...'
                        
                        _update_global_status(
                            job_id=job_id,
                            status='processing',
                            step=f'Retry attempt {attempt}/{MAX_RENDER_ATTEMPTS}',
                            progress=0,
                            message=f'Retrying render (attempt {attempt}/{MAX_RENDER_ATTEMPTS})...',
                            project_id=job_data.get('project_id'),
                            gallery_id=job_data.get('gallery_id'),
                            job_type=job_type
                        )
                    
                    # Execute the appropriate render function
                    # suppress_error_status=True on non-final attempts:
                    #   → prevents sending error status to API during retries
                    #   → re-raises exception so retry loop can catch it
                    # suppress_error_status=False on final attempt:
                    #   → normal error handling (sends error status to API)
                    suppress = not is_final_attempt
                    
                    if job_type == 'image':
                        _background_render_task(
                            job_data['project_id'],
                            job_data['gallery_id'],
                            job_data['payload'],
                            job_id,
                            job_data['user_id'],
                            suppress_error_status=suppress
                        )
                    elif job_type == 'video':
                        _background_video_render_task(
                            job_data['project_id'],
                            job_data['gallery_id'],
                            job_data['payload'],
                            job_id,
                            job_data['user_id'],
                            suppress_error_status=suppress
                        )
                    
                    # If we reach here, the render succeeded
                    job_succeeded = True
                    
                    # Mark job as completed
                    with queue_lock:
                        if job_id in active_jobs:
                            active_jobs[job_id]['status'] = 'completed'
                            active_jobs[job_id]['completed_at'] = datetime.now().isoformat()
                            active_jobs[job_id]['progress'] = 100
                            active_jobs[job_id]['current_step'] = 'completed'
                            active_jobs[job_id]['message'] = 'Render completed successfully'
                            if attempt > 1:
                                active_jobs[job_id]['message'] = f'Render completed successfully (after {attempt} attempts)'
                    
                    # UPDATE GLOBAL STATUS - FINISHED
                    _update_global_status(job_id=job_id, status='completed')

                    if attempt > 1:
                        print(f"\n{COLOR_GREEN}[OK] Job {job_id} completed successfully on attempt {attempt}/{MAX_RENDER_ATTEMPTS}{COLOR_RESET}\n")
                    else:
                        print(f"\n{COLOR_GREEN}[OK] Job {job_id} completed successfully{COLOR_RESET}\n")
                    
                    break  # Exit retry loop on success
                    
                except Exception as e:
                    last_error_msg = str(e)
                    
                    # ── GPU / VRAM FAILURE: DO NOT RETRY ────────────────────
                    # A VRAM exhaustion or graphics-driver reset reproduces on
                    # every attempt for the same scene on the same GPU, so the
                    # 3-attempt retry policy only burns render time and can
                    # leave the driver in a worse state. Record it as a failed
                    # render straight away. EVERY OTHER root cause still gets
                    # the normal MAX_RENDER_ATTEMPTS attempts.
                    # (Name-based check: this worker thread starts before
                    # GPUResourceError is defined later in the module.)
                    if type(e).__name__ == "GPUResourceError":
                        print(f"\n{COLOR_RED}{'='*80}")
                        print(f"[GPU] Job {job_id} FAILED on attempt {attempt} - GPU/VRAM capacity error")
                        print(f"      Skipping the remaining {MAX_RENDER_ATTEMPTS - attempt} retry attempt(s) - retrying cannot help.")
                        print(f"      Reason: {last_error_msg}")
                        print(f"{'='*80}{COLOR_RESET}\n")

                        with queue_lock:
                            if job_id in active_jobs:
                                active_jobs[job_id]['status'] = 'failed'
                                active_jobs[job_id]['error'] = last_error_msg
                                active_jobs[job_id]['failed_at'] = datetime.now().isoformat()
                                active_jobs[job_id]['progress'] = 0
                                active_jobs[job_id]['current_step'] = 'failed_gpu'
                                active_jobs[job_id]['message'] = 'Sorry, render failed'
                                active_jobs[job_id]['total_attempts'] = attempt
                                active_jobs[job_id]['no_retry_reason'] = 'gpu_vram'

                        # UPDATE GLOBAL STATUS - FAILED (no retries for GPU faults)
                        _update_global_status(job_id=job_id, status='failed')

                        # Save the failed input JSON + error details to failed_inputs/
                        import traceback
                        full_error_msg = f"{last_error_msg}\n\nFull Traceback:\n{traceback.format_exc()}"
                        _save_failed_input(job_data, full_error_msg, attempt)

                        print(f"{COLOR_RED}[GPU] Job {job_id} recorded in failed renders (insufficient GPU VRAM). Moving to next job.{COLOR_RESET}\n")
                        break  # Exit retry loop - no further attempts

                    if not is_final_attempt:
                        # ── NOT THE FINAL ATTEMPT: log but DO NOT send fail status ──
                        print(f"\n{COLOR_YELLOW}[WARN]  Job {job_id} failed on attempt {attempt}/{MAX_RENDER_ATTEMPTS}: {last_error_msg}{COLOR_RESET}")
                        print(f"{COLOR_YELLOW}   → Will retry ({MAX_RENDER_ATTEMPTS - attempt} attempt(s) remaining)...{COLOR_RESET}\n")
                        # Do NOT send status update — silent retry
                        continue
                    else:
                        # ── FINAL ATTEMPT FAILED: send fail status + save to failed_inputs ──
                        print(f"\n{COLOR_RED}{'='*80}")
                        print(f"❌ Job {job_id} PERMANENTLY FAILED after {MAX_RENDER_ATTEMPTS} attempts")
                        print(f"   Last error: {last_error_msg}")
                        print(f"{'='*80}{COLOR_RESET}\n")
                        
                        # Mark job as failed
                        with queue_lock:
                            if job_id in active_jobs:
                                active_jobs[job_id]['status'] = 'failed'
                                active_jobs[job_id]['error'] = last_error_msg
                                active_jobs[job_id]['failed_at'] = datetime.now().isoformat()
                                active_jobs[job_id]['progress'] = 0
                                active_jobs[job_id]['current_step'] = 'failed'
                                active_jobs[job_id]['message'] = 'Sorry, render failed'
                                active_jobs[job_id]['total_attempts'] = MAX_RENDER_ATTEMPTS
                        
                        # UPDATE GLOBAL STATUS - FAILED (only on final attempt)
                        _update_global_status(job_id=job_id, status='failed')
                        
                        # Save the failed input JSON + error details to failed_inputs/
                        import traceback
                        full_error_msg = f"{last_error_msg}\n\nFull Traceback:\n{traceback.format_exc()}"
                        _save_failed_input(job_data, full_error_msg, attempt)
                        
                        print(f"{COLOR_RED}❌ Job {job_id} failed permanently. Moving to next job.{COLOR_RESET}\n")
            
            # ── End of retry loop ────────────────────────────────────────
            render_queue.task_done()

                
        except Exception as e:
            print(f"{COLOR_RED}❌ Queue worker error: {str(e)}{COLOR_RESET}")
            import traceback
            traceback.print_exc()

# Start the queue worker thread when the application starts
queue_worker_thread = Thread(target=render_queue_worker, daemon=True)
queue_worker_thread.start()

# --- AI RENDER UTILITY FUNCTIONS ---

# AI Worker logic moved to AIRenderService

# --- GRACEFUL SHUTDOWN HANDLERS ---
# We use FastAPI's shutdown event instead of manual signal handling
# to avoid conflicts with Uvicorn's signal handling.



print(f"{COLOR_GREEN}[OK] Graceful shutdown handlers registered via FastAPI{COLOR_RESET}")
print(f"   Press Ctrl+C to gracefully shutdown the server\n")

# --- DATABASE INITIALIZATION ---
# Automatically create tables if they don't exist in the render database
try:
    print(f"[DB] Initializing database: render database")
    print(f"   Database URL: {session.DATABASE_URL}")
    models.Base.metadata.create_all(bind=session.engine)
    print(f"[OK] Database tables ready (created or already exist)")
except Exception as e:
    print(f"[WARN]  Warning: Could not initialize database tables: {e}")
    print(f"   You may need to run 'python init_database.py' manually")
    # Don't fail the application startup if database is not available
    # Some endpoints may still work without database

# --- APP INITIALIZATION ---
app = FastAPI(title="Z_Realty 4K Render Backend",
docs_url="/docs",
openapi_url="/openapi.json",
root_path="/render")

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print(f"\n{COLOR_RED}[VALIDATION ERROR] Request rejected with 422 Unprocessable Entity!{COLOR_RESET}")
    print(f"URL: {request.url}")
    print(f"Headers: {request.headers}")
    print(f"Errors: {exc.errors()}")
    try:
        body = await request.json()
        print(f"Body: {body}")
    except:
        pass
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": exc.body}
    )

# --- LOGGING CONFIGURATION ---
class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Filter out 200 OK responses for /queue-status to reduce noise
        return record.getMessage().find("GET /queue-status") == -1

async def poll_external_engine_status():
    if not REROUTE_ENGINE_URL:
        return
    import httpx
    import asyncio
    while not is_shutting_down:
        await asyncio.sleep(3)
        external_jobs_exist = False
        with queue_lock:
            for job_id, info in active_jobs.items():
                if info.get('processing_externally') and info.get('status') not in ('completed', 'failed', 'error'):
                    external_jobs_exist = True
                    break
        
        if not external_jobs_exist:
            continue
            
        try:
            async with httpx.AsyncClient() as client:
                headers = {}
                if REROUTE_ENGINE_API_KEY:
                    headers["X-API-Key"] = REROUTE_ENGINE_API_KEY
                
                resp = await client.get(
                    f"{REROUTE_ENGINE_URL.rstrip('/')}/queue-status-public", 
                    headers=headers, 
                    timeout=5.0
                )
                if resp.status_code == 200:
                    data = resp.json()
                    ext_jobs = data.get("jobs", [])
                    ext_jobs_dict = {j["job_id"]: j for j in ext_jobs}
                    
                    with queue_lock:
                        for job_id, local_info in active_jobs.items():
                            if local_info.get('processing_externally') and local_info.get('status') not in ('completed', 'failed', 'error'):
                                if job_id in ext_jobs_dict:
                                    ext_job = ext_jobs_dict[job_id]
                                    new_status = ext_job.get("status")
                                    new_step = ext_job.get("step") or ext_job.get("current_step")
                                    new_progress = ext_job.get("progress")
                                    new_message = ext_job.get("message")
                                    
                                    if new_status != local_info.get("status") or new_step != local_info.get("current_step") or new_progress != local_info.get("progress"):
                                        local_info["status"] = new_status
                                        local_info["current_step"] = new_step
                                        local_info["progress"] = new_progress
                                        local_info["message"] = new_message
                                        
                                        if new_status == 'completed':
                                            local_info["completed_at"] = datetime.now().isoformat()
                                        
                                        asyncio.get_event_loop().run_in_executor(
                                            None, 
                                            _send_status_update,
                                            local_info.get("project_id", 0),
                                            local_info.get("gallery_id", 0),
                                            local_info.get("user_id", 0),
                                            job_id,
                                            new_status,
                                            new_step,
                                            new_progress,
                                            new_message,
                                            local_info.get("type", local_info.get("render_type", "IMAGE"))
                                        )
        except Exception:
            pass

@app.on_event("startup")
async def configure_logging():
    # Filter out /queue-status endpoint logs from uvicorn access logger
    logging.getLogger("uvicorn.access").addFilter(EndpointFilter())
    
    # --- AI Render Service Setup ---
    ai_service.setup(active_jobs, queue_lock, _send_status_update, _update_global_status)
    ai_service.upload_to_blob_fallback = _upload_to_blob_storage
    
    # Start polling external jobs
    asyncio.create_task(poll_external_engine_status())

@app.on_event("shutdown")
def shutdown_event():
    """
    Handle application shutdown.
    """
    _graceful_shutdown(None, None)

# --- Include Auth Router ---
from app.routers import auth
app.include_router(auth.router, prefix="/auth", tags=["authentication"])

# --- API Key Authentication Dependency ---
# This ensures only authenticated users with valid API keys can access render endpoints
CurrentUser = Annotated[models.User, Depends(auth_service.get_user_from_api_key)]

# --- CONFIGURATION ---
# --- CONFIGURATION ---
# SCRIPT_DIR must be defined first — ASSET_DOWNLOAD_DIR is anchored to it.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Use SCRIPT_DIR as the anchor so ASSET_DOWNLOAD_DIR is always an absolute path.
# This ensures _get_cached_asset_path never rejects a valid cached file due to
# a CWD mismatch, which was causing repeated re-downloads of the same asset.
_raw_asset_download_dir = os.getenv("ASSET_DOWNLOAD_DIR", "asset_downloads")
ASSET_DOWNLOAD_DIR = (
    _raw_asset_download_dir
    if os.path.isabs(_raw_asset_download_dir)
    else os.path.join(SCRIPT_DIR, _raw_asset_download_dir)
)
GLB_OUTPUT_DIR = os.getenv("GLB_OUTPUT_DIR", "output_glb")
RENDER_OUTPUT_DIR = os.getenv("RENDER_OUTPUT_DIR", "output_renders")
GODOT_LOGS_DIR = os.getenv("GODOT_LOGS_DIR", "godot_logs")
RUNTIME_DIR = os.path.join(tempfile.gettempdir(), "zrealty_backend_runtime")
ASSET_URL_MAP_FILE = os.path.join(ASSET_DOWNLOAD_DIR, "asset_url_map.json")
ENABLE_VIDEO_CULLING = True  # Set to False to skip VideoSceneOptimizer for video renders
VIDEO_RENDER_MAX_CHUNK_SECONDS = float(os.getenv("VIDEO_RENDER_MAX_CHUNK_SECONDS", "10"))

# --- GODOT CONFIGURATION ---
# Check for both mono and non-mono versions
# Only use mono exe if GodotSharp assemblies directory actually exists
GODOT_BIN_DIR = os.path.join(SCRIPT_DIR, "godot", "bin")
GODOT_SHARP_DIR = os.path.join(GODOT_BIN_DIR, "GodotSharp")
DEFAULT_GODOT_EXE_MONO = os.path.join(GODOT_BIN_DIR, "godot.windows.editor.x86_64.mono.console.exe")
DEFAULT_GODOT_EXE_PLAIN = os.path.join(GODOT_BIN_DIR, "godot.windows.editor.x86_64.console.exe")

# Prefer plain (non-mono) exe first; only use mono if GodotSharp assemblies are present
if os.path.exists(DEFAULT_GODOT_EXE_PLAIN):
    DEFAULT_GODOT_EXE = DEFAULT_GODOT_EXE_PLAIN
elif os.path.exists(DEFAULT_GODOT_EXE_MONO) and os.path.isdir(GODOT_SHARP_DIR):
    DEFAULT_GODOT_EXE = DEFAULT_GODOT_EXE_MONO
elif os.path.exists(DEFAULT_GODOT_EXE_MONO):
    # Mono exe exists but GodotSharp is missing - use it anyway but warn
    print(f"{COLOR_YELLOW}[WARN]  WARNING: Mono Godot exe found but GodotSharp directory is missing at: {GODOT_SHARP_DIR}")
    print(f"   This may cause '.NET assemblies not found' errors.{COLOR_RESET}")
    DEFAULT_GODOT_EXE = DEFAULT_GODOT_EXE_MONO
else:
    DEFAULT_GODOT_EXE = DEFAULT_GODOT_EXE_PLAIN  # Will fail later with a clear "file not found"

GODOT_EXE = os.getenv("GODOT_EXE", DEFAULT_GODOT_EXE)
GODOT_PROJECT_PATH = os.getenv("GODOT_PROJECT_PATH", os.path.join(SCRIPT_DIR, "godot_project"))

# --- BLENDER CONFIGURATION (image renders) ---
# RENDER_BACKEND=blender  -> image jobs are rendered by the self-built Blender (blender_scripts/render.py)
# RENDER_BACKEND=godot    -> original Godot path, unchanged
# Video jobs still use Godot until the Blender video port (phase 2) lands.
RENDER_BACKEND = os.getenv("RENDER_BACKEND", "blender").strip().lower()
BLENDER_RENDER_EXE = os.getenv(
    "BLENDER_RENDER_EXE",
    os.path.join(SCRIPT_DIR, "build_windows_x64_vc18_Release", "bin", "Release", "blender.exe"),
)
BLENDER_SCRIPTS_DIR = os.getenv("BLENDER_SCRIPTS_DIR", os.path.join(SCRIPT_DIR, "blender_scripts"))
BLENDER_RENDER_TIMEOUT = int(os.getenv("IMAGE_RENDER_TIMEOUT_SECONDS", "1800"))

# --- CREATE DIRECTORIES ---
os.makedirs(ASSET_DOWNLOAD_DIR, exist_ok=True)
os.makedirs(GLB_OUTPUT_DIR, exist_ok=True)
os.makedirs(RENDER_OUTPUT_DIR, exist_ok=True)
os.makedirs(GODOT_LOGS_DIR, exist_ok=True)
os.makedirs(RUNTIME_DIR, exist_ok=True)

# --- API & TOKENS ---
# --- API & TOKENS ---
SAS_TOKEN = os.getenv("AZURE_SAS_TOKEN", "sv=2024-11-04&ss=bfqt&srt=sco&sp=rwdlacupiytfx&se=2026-11-18T20:34:53Z&st=2025-09-12T12:19:53Z&spr=https,http&sig=KNQs7rhe81AeQfnd%2BS4QMPWWo55VbNICTufFVYe5KhA%3D")
UPLOAD_API_URL = (
    os.getenv("UPLOAD_API_URL", "").strip()
    or os.getenv("AI_CALLBACK_API_URL", "").strip()
    or "http://216.48.178.133:4050/api/v1/Project/UploadProjectGalleryAI"
)
VIDEO_UPLOAD_API_URL = (
    os.getenv("VIDEO_UPLOAD_API_URL", "").strip()
    or os.getenv("AI_VIDEO_CALLBACK_API_URL", "").strip()
    or "http://216.48.178.133:4050/api/v1/Project/UploadProjectVideoAI"
)
# Internal monitor callback URL. Keep legacy names supported because the .env
# file in this repo uses MONITOR_UPLOAD_API_URL / MONITOR_VIDEO_UPLOAD_API_URL.
MONITORING_API_URL = (
    os.getenv("MONITOR_UPLOAD_API_URL", "").strip()
    or os.getenv("MONITORING_API_URL", "").strip()
)
API_KEY = os.getenv("API_KEY", "")
USE_API_KEY = os.getenv("USE_API_KEY", "false").lower() == "true"



# --- CORS MIDDLEWARE ---
origins = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3002",
    "http://127.0.0.1:8005",
    "http://127.0.0.1:8006",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- STATUS MONITORING API ---
# Read-only endpoints for external monitoring system
from api_status import create_status_router
from scene_optimizer import SceneOptimizer, CULLING_LOGS_DIR
from scene_optimizer_video import VideoSceneOptimizer

status_router = create_status_router(active_jobs, render_queue, queue_lock, CURRENT_STATUS, ai_render_queue=ai_service.render_queue)
# Mount authenticated monitoring routes under /monitoring so they do not collide
# with the public AI status endpoints defined later in this file.
app.include_router(status_router, prefix="/monitoring")

# --- DATA MODELS (SCHEMA) ---
class Vector3(BaseModel):
    x: float
    y: float
    z: float

class ColorRGB(BaseModel):
    r: float
    g: float
    b: float

class DirectionalLight(BaseModel):
    intensity: float
    position: Vector3
    color: ColorRGB
    target: Optional[Vector3] = None
    cast_shadow: Optional[bool] = True

class AmbientLight(BaseModel):
    intensity: float

class GenerationPayload(BaseModel):
    floor_plan_data: str
    fov: float = 45.0
    directional_light: Optional[DirectionalLight] = None
    ambient_light: Optional[AmbientLight] = None
    aspect_ratio: str
    render_quality: str
    # Some callers omit this field for image renders; keep it optional so the
    # request can still start instead of failing validation up front.
    timestamp: int = 0
    hdri_filename: Optional[str] = "kloppenheim_06_puresky_4k.exr"
    environment_url: Optional[str] = None
    # Camera data fields (optional, will be extracted from JSON)
    threejs_camera: Optional[Dict[str, Any]] = None
    enable_status_updates: bool = True  # If True, send status updates to UploadGallerAI endpoint at each step
    draft_render: Any = True
    # Natural-language material request, e.g. "change the floor texture to
    # marble" or "make the walls sage green".
    # NOTE: the prompt material pass (zlendo_llm.prompt_integration) has no
    # source in this tree, so nothing consumes this yet. It is declared here so
    # the field survives validation as a typed attribute instead of an untyped
    # extra — every reader uses getattr(payload, "prompt", None), and it is
    # echoed in the request banner so the operator can see it arrived.
    prompt: Optional[str] = None
    # Per-job LLM switch. Omit it and .env decides (the behaviour every caller
    # had before this field existed). Send false to skip every AI pass for this
    # job. Sending true does NOT override .env USE_LLM=False - the operator's
    # kill-switch is not something a request gets to undo. Read in
    # _run_godot_render via zlendo_llm.config.apply_job_llm_flag; declared here
    # so it is a typed attribute rather than an untyped extra.
    USE_LLM: Optional[Any] = None

    class Config:
        extra = "allow"  # Allow extra fields that aren't in the model

# --- VIDEO-SPECIFIC DATA MODELS ---
class VideoAnimation(BaseModel):
    duration_seconds: float = 5.0
    fps: int = 30
    camera_position_start: Optional[Vector3] = None
    camera_position_end: Optional[Vector3] = None
    camera_target_start: Optional[Vector3] = None
    camera_target_end: Optional[Vector3] = None
    # Frame-by-frame keyframes array (if provided by frontend)
    # Each keyframe: {"frame": int, "position": {"x": float, "y": float, "z": float}, "target": {"x": float, "y": float, "z": float}}
    keyframes: Optional[List[Dict[str, Any]]] = None

class VideoDirectionalLight(BaseModel):
    intensity: float = 1.0
    position: Optional[Vector3] = None
    color: Optional[ColorRGB] = None
    target: Optional[Vector3] = None
    cast_shadow: Optional[bool] = False
    shadow_map_size: Optional[int] = 512
    shadow_bias: Optional[float] = 0

class VideoGenerationPayload(BaseModel):
    """Payload model for video rendering with camera animation data."""
    video_animation: Optional[VideoAnimation] = None  # Optional — if missing, Godot auto-generates a horizontal pan
    threejs_camera: Optional[Dict[str, Any]] = None
    coordinate_system: Optional[str] = "right_handed_y_up"
    target_coordinate_system: Optional[str] = "right_handed_z_up"
    ambient_light: Optional[AmbientLight] = None
    directional_light: Optional[VideoDirectionalLight] = None
    aspect_ratio: str = "16:9"
    render_quality: str = "hd"
    timestamp: int = 0
    floor_plan_data: Optional[Any] = None  # Can be string or dict — both formats accepted
    environment_url: Optional[str] = None
    enable_status_updates: bool = True  # If True, send status updates to UploadGallerAI endpoint at each step
    draft_render: Any = True
    # Same field as GenerationPayload — see the note there. Declared on the
    # video payload too so the image and video request shapes do not diverge.
    prompt: Optional[str] = None
    # Same field as GenerationPayload — see the note there. Present on both so
    # a still and a walkthrough of the same scene take the same switch.
    USE_LLM: Optional[Any] = None

    class Config:
        extra = "allow"  # Allow extra fields that aren't in the model

# --- RENDER SIZE HELPERS ---
QUALITY_RESOLUTION_MAP: Dict[str, tuple[int, int]] = {
    "12K": (12288, 6642),
    "8K": (7680, 4320),
    "6K": (6144, 3321),
    "4K": (3840, 2160),
    "2K": (2560, 1440),
    "QUAD HD": (2048, 1080),
    "1080P": (1920, 1080),
    "FHD": (1920, 1080),
    "FULL HD": (1920, 1080),
    "HD": (1280, 720),
}


def _parse_aspect_ratio_value(raw_value: Any) -> Optional[float]:
    """Parse an aspect ratio as width / height from common encodings."""
    if raw_value is None:
        return None

    if isinstance(raw_value, (int, float)):
        value = float(raw_value)
        return value if value > 0.0 else None

    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return None
        if ":" in text:
            parts = text.split(":")
            if len(parts) == 2:
                try:
                    width = float(parts[0].strip())
                    height = float(parts[1].strip())
                    if width > 0.0 and height > 0.0:
                        return width / height
                except Exception:
                    return None
        try:
            value = float(text)
            return value if value > 0.0 else None
        except Exception:
            return None

    if isinstance(raw_value, dict):
        width = raw_value.get("width", raw_value.get("w"))
        height = raw_value.get("height", raw_value.get("h"))
        if width is not None and height is not None:
            try:
                width_f = float(width)
                height_f = float(height)
                if width_f > 0.0 and height_f > 0.0:
                    return width_f / height_f
            except Exception:
                pass
        if "x" in raw_value and "y" in raw_value:
            try:
                width_f = float(raw_value.get("x"))
                height_f = float(raw_value.get("y"))
                if width_f > 0.0 and height_f > 0.0:
                    return width_f / height_f
            except Exception:
                pass

    return None


def _normalize_render_quality(render_quality: Any) -> str:
    return str(render_quality or "").replace("_", " ").strip().upper()


def _resolve_base_render_resolution(render_quality: Any, width: Any = None, height: Any = None) -> tuple[int, int]:
    quality = _normalize_render_quality(render_quality)
    base_width, base_height = QUALITY_RESOLUTION_MAP.get(quality, (1920, 1080))

    if width is not None:
        try:
            base_width = int(width)
        except Exception:
            pass
    if height is not None:
        try:
            base_height = int(height)
        except Exception:
            pass

    return max(base_width, 1), max(base_height, 1)


def _apply_aspect_ratio_to_resolution(width: int, height: int, aspect_ratio: Any) -> tuple[int, int]:
    aspect_ratio_value = _parse_aspect_ratio_value(aspect_ratio)
    if not aspect_ratio_value:
        return max(width, 1), max(height, 1)

    current_ratio = float(width) / float(max(height, 1))
    if abs(aspect_ratio_value - current_ratio) <= 0.001:
        return max(width, 1), max(height, 1)

    base_long_side = max(width, height)
    if aspect_ratio_value >= 1.0:
        width = base_long_side
        height = int(round(float(base_long_side) / aspect_ratio_value))
    else:
        height = base_long_side
        width = int(round(float(base_long_side) * aspect_ratio_value))

    return max(width, 1), max(height, 1)


def _build_render_size_info(
    output_width: int,
    output_height: int,
    *,
    input_width: Optional[int] = None,
    input_height: Optional[int] = None,
    render_quality: Optional[Any] = None,
    aspect_ratio: Optional[Any] = None,
) -> Dict[str, Any]:
    output_width = max(int(output_width), 1)
    output_height = max(int(output_height), 1)
    size_info: Dict[str, Any] = {
        "output_width": output_width,
        "output_height": output_height,
        "output_resolution": f"{output_width}x{output_height}",
        "render_size": {
            "output": {
                "width": output_width,
                "height": output_height,
                "resolution": f"{output_width}x{output_height}",
            }
        },
    }

    if input_width is not None and input_height is not None:
        input_width = max(int(input_width), 1)
        input_height = max(int(input_height), 1)
        size_info["input_width"] = input_width
        size_info["input_height"] = input_height
        size_info["input_resolution"] = f"{input_width}x{input_height}"
        size_info["render_size"]["input"] = {
            "width": input_width,
            "height": input_height,
            "resolution": f"{input_width}x{input_height}",
        }

    if render_quality is not None:
        size_info["render_quality"] = str(render_quality)
        size_info["render_size"]["render_quality"] = str(render_quality)

    if aspect_ratio is not None:
        size_info["aspect_ratio"] = str(aspect_ratio)
        size_info["render_size"]["aspect_ratio"] = str(aspect_ratio)

    return size_info


def _calculate_image_render_size(payload: GenerationPayload) -> Dict[str, Any]:
    width, height = _resolve_base_render_resolution(
        payload.render_quality,
        getattr(payload, "width", None),
        getattr(payload, "height", None),
    )
    width, height = _apply_aspect_ratio_to_resolution(width, height, getattr(payload, "aspect_ratio", None))
    return _build_render_size_info(
        width,
        height,
        render_quality=payload.render_quality,
        aspect_ratio=getattr(payload, "aspect_ratio", None),
    )


def _calculate_video_render_size(payload: VideoGenerationPayload) -> Dict[str, Any]:
    width, height = _resolve_base_render_resolution(
        payload.render_quality,
        getattr(payload, "width", None),
        getattr(payload, "height", None),
    )
    return _build_render_size_info(
        width,
        height,
        render_quality=payload.render_quality,
        aspect_ratio=getattr(payload, "aspect_ratio", None),
    )


def _calculate_ai_render_size(input_width: int, input_height: int, target_long_side: int) -> Dict[str, Any]:
    input_width = max(int(input_width), 1)
    input_height = max(int(input_height), 1)
    target_long_side = max(int(target_long_side), 1)

    long_side = max(input_width, input_height)
    if long_side >= target_long_side:
        output_width = input_width
        output_height = input_height
    else:
        scale = target_long_side / long_side
        output_width = max(8, int(input_width * scale))
        output_height = max(8, int(input_height * scale))

    return _build_render_size_info(
        output_width,
        output_height,
        input_width=input_width,
        input_height=input_height,
        render_quality="AI_RENDER",
        aspect_ratio=f"{input_width}:{input_height}",
    )


def _get_file_size_info(file_path: Optional[str]) -> Dict[str, Any]:
    """Return file size metadata for a rendered artifact on disk."""
    if not file_path:
        return {}
    try:
        if not os.path.exists(file_path):
            return {}
        size_bytes = os.path.getsize(file_path)
        return {
            "output_size_bytes": size_bytes,
            "output_size_mb": round(size_bytes / (1024 * 1024), 2),
        }
    except Exception:
        return {}


def _extract_job_render_size(job: Dict[str, Any]) -> Dict[str, Any]:
    size_keys = (
        "input_width",
        "input_height",
        "input_resolution",
        "output_width",
        "output_height",
        "output_resolution",
        "output_size_bytes",
        "output_size_mb",
        "render_quality",
        "aspect_ratio",
        "render_size",
    )
    return {key: job.get(key) for key in size_keys if job.get(key) is not None}

# --- PAYLOAD VALIDATION FUNCTIONS ---

def _validate_image_payload(payload: GenerationPayload) -> tuple[bool, str]:
    """
    Validates image generation payload to ensure all required fields are present and valid.
    
    Returns:
        (is_valid, error_message) - True if valid, False with error message if invalid
    """
    try:
        # Check required fields
        if not payload.floor_plan_data:
            return False, "Missing required field: floor_plan_data"

        # Validate floor_plan_data is valid JSON
        try:
            if isinstance(payload.floor_plan_data, str):
                json.loads(payload.floor_plan_data)
        except json.JSONDecodeError:
            return False, "Invalid floor_plan_data: not valid JSON"
        
        # Validate directional_light
        if not payload.directional_light:
            return False, "Missing required field: directional_light"
        
        # Validate render quality
        valid_qualities = ['HD', 'FULL HD', 'FULLHD', 'QUAD HD', 'QUADHD', '2K', '4K', '6K', '8K', '12K', 'FAST_PREVIEW', 'LOW', 'MEDIUM']
        if payload.render_quality.upper() not in valid_qualities:
            return False, f"Invalid render_quality: {payload.render_quality}. Must be one of: {', '.join(valid_qualities)}"
        
        # Validate aspect ratio
        if not payload.aspect_ratio:
            return False, "Missing required field: aspect_ratio"

        return True, ""
        
    except Exception as e:
        return False, f"Validation error: {str(e)}"

def _validate_video_payload(payload: VideoGenerationPayload) -> tuple[bool, str]:
    """
    Validates video generation payload to ensure all required fields are present and valid.
    
    Returns:
        (is_valid, error_message) - True if valid, False with error message if invalid
    """
    try:
        # Check video_animation — if not present, Godot will auto-generate a horizontal pan
        if not payload.video_animation:
            print("ℹ️  No video_animation provided — Godot will auto-generate horizontal pan from camera position")
            return True, ""
        
        # Validate duration and fps
        if payload.video_animation.duration_seconds <= 0:
            return False, f"Invalid duration_seconds: {payload.video_animation.duration_seconds}. Must be greater than 0"
        
        if payload.video_animation.fps <= 0:
            return False, f"Invalid fps: {payload.video_animation.fps}. Must be greater than 0"
        
        # Validate duration is reasonable (max 60 seconds)
        if payload.video_animation.duration_seconds > 60:
            return False, f"Invalid duration_seconds: {payload.video_animation.duration_seconds}. Maximum allowed is 60 seconds"
        
        # Validate fps is reasonable (max 60 fps)
        if payload.video_animation.fps > 60:
            return False, f"Invalid fps: {payload.video_animation.fps}. Maximum allowed is 60 fps"
        
        # Check for camera animation data (keyframes or start/end positions)
        has_keyframes = payload.video_animation.keyframes and len(payload.video_animation.keyframes) > 0
        has_start_end = (
            payload.video_animation.camera_position_start is not None and
            payload.video_animation.camera_position_end is not None
        )
        
        if not has_keyframes and not has_start_end:
            # No keyframes or start/end — Godot will auto-generate from camera position
            print("ℹ️  No keyframes or start/end positions — Godot will auto-generate from camera")
        
        # Validate render quality
        valid_qualities = ['HD', 'FULL HD', 'FULLHD', 'QUAD HD', 'QUADHD', '2K', '4K', '6K', '8K', '12K', 'FAST_PREVIEW', 'LOW', 'MEDIUM']
        if payload.render_quality.upper() not in valid_qualities:
            return False, f"Invalid render_quality: {payload.render_quality}. Must be one of: {', '.join(valid_qualities)}"
        
        # Validate aspect ratio
        if not payload.aspect_ratio:
            return False, "Missing required field: aspect_ratio"
        
        return True, ""
        
    except Exception as e:
        return False, f"Validation error: {str(e)}"

# --- ASSET HANDLING & URL LOCALIZATION ---

def _load_asset_url_map() -> dict:
    """Loads the URL-to-local-file mapping from JSON file."""
    if os.path.exists(ASSET_URL_MAP_FILE):
        try:
            with open(ASSET_URL_MAP_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"{COLOR_YELLOW}Warning: Could not load asset URL map: {e}. Starting with empty map.{COLOR_RESET}")
            return {}
    return {}

def _save_asset_url_map(url_map: dict):
    """Saves the URL-to-local-file mapping to JSON file."""
    try:
        with open(ASSET_URL_MAP_FILE, 'w', encoding='utf-8') as f:
            json.dump(url_map, f, indent=2, ensure_ascii=False)
    except IOError as e:
        print(f"{COLOR_YELLOW}Warning: Could not save asset URL map: {e}{COLOR_RESET}")

def _get_cached_asset_path(url: str) -> Optional[str]:
    """
    Checks if the URL is in asset_url_map.json and the mapped local file exists.
    Supports both dict format { "local_path": "...", "asset_id": "..." }
    and the legacy plain-string format for backward compat.
    THREAD-SAFE: Uses a lock to ensure atomic read/write.
    """
    with map_lock:
        url_map = _load_asset_url_map()
        if url in url_map:
            entry = url_map[url]
            local_path = entry["local_path"] if isinstance(entry, dict) else entry
            if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                return local_path
            else:
                print(f"{COLOR_YELLOW}  ↳ Cached entry found but file is missing on disk, removing from map: {os.path.basename(local_path)}{COLOR_RESET}")
                del url_map[url]
                _save_asset_url_map(url_map)
        return None

def _add_to_asset_url_map(url: str, local_path: str, asset_id: str = ""):
    """
    Adds / updates a URL entry in asset_url_map.json.
    Format:
      { "frontend_url": "<original LR/blob URL>", "local_path": "<local file>", "asset_id": "<id or blank>" }
    - GLB assets:  asset_id holds the numeric ID from the frontend JSON.
    - Textures:    asset_id is blank (textures have no ID in the frontend JSON, only direct URLs).
    THREAD-SAFE: Uses a lock to ensure atomic read-update-write.
    """
    with map_lock:
        url_map = _load_asset_url_map()
        new_entry = {"frontend_url": url, "local_path": local_path, "asset_id": asset_id}
        existing = url_map.get(url)
        # Preserve a previously stored asset_id if the caller doesn't supply one
        if not asset_id and isinstance(existing, dict) and existing.get("asset_id"):
            new_entry["asset_id"] = existing["asset_id"]
        if existing != new_entry:
            url_map[url] = new_entry
            _save_asset_url_map(url_map)

import struct

def _check_glb_for_webp(filepath: str) -> bool:
    """
    Checks if a GLB file contains the EXT_texture_webp extension.
    Uses direct binary inspection of the JSON chunk.
    """
    if not os.path.exists(filepath):
        return False
    try:
        with open(filepath, 'rb') as f:
            # Header: magic (4), version (4), length (4)
            magic = f.read(4)
            if magic != b'glTF': return False
            f.read(8) # Skip version and total length
            
            # Chunk 0 (JSON): length (4), type (4)
            chunk_length = struct.unpack('<I', f.read(4))[0]
            chunk_type = f.read(4)
            if chunk_type != b'JSON': return False
            
            json_bytes = f.read(chunk_length)
            json_text = json_bytes.decode('utf-8', errors='ignore')
            
            # Look for the extension string
            return "EXT_texture_webp" in json_text
    except Exception as e:
        print(f"{COLOR_YELLOW}  [WARN]  Warning: Could not inspect GLB {os.path.basename(filepath)}: {e}{COLOR_RESET}")
        return False

def _repair_glb_file(filepath: str):
    """
    Godot-only pipeline: WebP repair is not performed here.
    This function logs the condition and leaves the file unchanged.
    """
    try:
        print(f"{COLOR_YELLOW}  [WARN]  WebP detected in GLB, but automatic repair is disabled in the Godot pipeline:{COLOR_RESET} {os.path.basename(filepath)}")
        return False
    except Exception as e:
        print(f"{COLOR_RED}  ❌ Repair error: {e}{COLOR_RESET}")
        return False

def _check_glb_for_draco(filepath: str) -> bool:
    """
    Checks if a GLB file contains Draco mesh compression.
    """
    if not os.path.exists(filepath):
        return False
    try:
        with open(filepath, 'rb') as f:
            magic = f.read(4)
            if magic != b'glTF':
                return False
            f.read(8)
            chunk_length = struct.unpack('<I', f.read(4))[0]
            chunk_type = f.read(4)
            if chunk_type != b'JSON':
                return False
            json_bytes = f.read(chunk_length)
            json_text = json_bytes.decode('utf-8', errors='ignore')
            return "KHR_draco_mesh_compression" in json_text
    except Exception as e:
        print(f"{COLOR_YELLOW}  [WARN]  Warning: Could not inspect GLB {os.path.basename(filepath)}: {e}{COLOR_RESET}")
        return False

def _build_godot_compatible_glb_path(source_url: str, local_path: str) -> str:
    """
    Builds a stable output path for a Godot-compatible GLB copy.
    """
    compat_dir = os.path.abspath(os.path.join(ASSET_DOWNLOAD_DIR, "_godot_compatible"))
    os.makedirs(compat_dir, exist_ok=True)

    source_key = source_url or local_path or ""
    digest = hashlib.sha1(source_key.encode("utf-8", errors="ignore")).hexdigest()[:10]
    base_name = os.path.splitext(os.path.basename(local_path))[0] or "asset"
    return os.path.join(compat_dir, f"{digest}_{base_name}_nodraco.glb")

def _convert_draco_glb_to_plain_glb(input_path: str, output_path: str) -> tuple[bool, str]:
    """
    Uses a small Python helper to decode Draco-compressed meshes into a plain GLB.
    """
    script_path = os.path.join(SCRIPT_DIR, "tools", "decompress_draco_glb.py")
    if not os.path.exists(script_path):
        return False, f"Missing Draco conversion helper: {script_path}"

    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        timeout_seconds = int(os.getenv("DRACO_GLB_CONVERSION_TIMEOUT_SECONDS", "300"))
        result = subprocess.run(
            [sys.executable, script_path, input_path, output_path],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            if not message:
                message = f"Node helper exited with code {result.returncode}"
            return False, message
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return False, "Draco conversion finished but output file was not created"
        return True, ""
    except FileNotFoundError:
        return False, "Python interpreter was not found"
    except subprocess.TimeoutExpired:
        return False, f"Draco conversion timed out after {timeout_seconds}s"
    except Exception as e:
        return False, str(e)

def _convert_glb_webp_textures_to_png(input_path: str, output_path: str) -> tuple[bool, str]:
    """
    Rewrites embedded WebP textures in a GLB as PNGs.
    """
    script_path = os.path.join(SCRIPT_DIR, "tools", "convert_glb_webp_to_png.py")
    if not os.path.exists(script_path):
        return False, f"Missing WebP conversion helper: {script_path}"

    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        result = subprocess.run(
            [sys.executable, script_path, input_path, output_path],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=int(os.getenv("GLB_WEBP_CONVERSION_TIMEOUT_SECONDS", "300")),
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            if not message:
                message = f"Python helper exited with code {result.returncode}"
            return False, message
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return False, "WebP conversion finished but output file was not created"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "WebP conversion timed out"
    except Exception as e:
        return False, str(e)

def _ensure_godot_compatible_glb(source_url: str, local_path: str) -> str:
    """
    Converts Draco-compressed GLBs and embedded WebP textures into a Godot-friendly GLB.
    Returns the original path if conversion is not needed or fails.
    """
    if not local_path or not isinstance(local_path, str):
        return local_path
    if not local_path.lower().endswith(".glb"):
        return local_path
    draco_present = _check_glb_for_draco(local_path)
    webp_present = _check_glb_for_webp(local_path)
    if not draco_present and not webp_present:
        return local_path

    current_path = local_path
    show_details = os.getenv("ASSETS_DETAIL_PRINT", "false").lower() == "true"

    if draco_present:
        compatible_path = _build_godot_compatible_glb_path(source_url, local_path)
        needs_draco_convert = (
            not os.path.exists(compatible_path)
            or os.path.getsize(compatible_path) == 0
            or _check_glb_for_draco(compatible_path)
            or _check_glb_for_webp(compatible_path)
        )
        if needs_draco_convert:
            if show_details:
                print(f"{COLOR_YELLOW}  ↻ Draco GLB detected, converting for Godot:{COLOR_RESET} {os.path.basename(local_path)}")
            ok, error = _convert_draco_glb_to_plain_glb(current_path, compatible_path)
            if not ok:
                print(f"{COLOR_YELLOW}  [WARN]  Draco conversion failed, keeping original GLB:{COLOR_RESET} {os.path.basename(local_path)} ({error})")
                return local_path
        current_path = compatible_path

    if _check_glb_for_webp(current_path):
        png_tmp_path = current_path + ".pngtmp"
        if show_details:
            print(f"{COLOR_YELLOW}  ↻ WebP textures detected, converting to PNG:{COLOR_RESET} {os.path.basename(current_path)}")
        ok, error = _convert_glb_webp_textures_to_png(current_path, png_tmp_path)
        if ok:
            os.replace(png_tmp_path, current_path)
        else:
            print(f"{COLOR_YELLOW}  [WARN]  WebP texture conversion failed, keeping current GLB:{COLOR_RESET} {os.path.basename(current_path)} ({error})")
            if os.path.exists(png_tmp_path):
                try:
                    os.remove(png_tmp_path)
                except OSError:
                    pass
            if draco_present and current_path != local_path:
                if source_url:
                    _add_to_asset_url_map(source_url, current_path)  # asset_id unknown here
                return current_path
            return local_path

    if source_url:
        _add_to_asset_url_map(source_url, current_path)  # asset_id unknown here
    return current_path

def _convert_glb_path_to_url(glb_path: str) -> str:
    """
    Converts a relative GLB path to a full Azure Blob Storage URL.
    
    Input: glb-assets/Sofa/Double_Seater_Sofa/Nordic_Sofa_HP.glb
    Output: https://zrealtystoragedev.blob.core.windows.net/glb-assets/Sofa/Double_Seater_Sofa/Nordic_Sofa_HP.glb
    
    Does NOT modify the filename (trusts input).
    """
    if not glb_path or not isinstance(glb_path, str):
        return ""

    if glb_path.startswith("http"):
        return glb_path
    
    # Add base URL prefix
    base_url = "https://zrealtystoragedev.blob.core.windows.net/"
    
    # Remove leading slash if present
    if glb_path.startswith("/"):
        glb_path = glb_path[1:]
    
    # Construct full URL
    full_url = f"{base_url}{glb_path}"
    
    return full_url

def _timed_download_worker(url: str, context: str = "", asset_id: str = ""):
    """
    Downloads a single file with retries.
    Only prints the asset filename when a fresh download starts, and an error
    line if all attempts fail. Cache hits are silent.
    """
    start_time = time.monotonic()
    
    base_url_prefix = "https://zrealtystoragedev.blob.core.windows.net/"
    cleaned_url = url.strip().strip('"')
    if not cleaned_url or not isinstance(cleaned_url, str) or not cleaned_url.startswith(base_url_prefix):
        return url, "", 0.0, None

    # ── CACHE CHECK ──────────────────────────────────────────────────────────
    # If the URL is in asset_url_map.json the asset was previously downloaded —
    # just verify the file still exists on disk and use it silently.
    cached_path = _get_cached_asset_path(cleaned_url)
    if cached_path:
        if cached_path.lower().endswith('.glb'):
            if _check_glb_for_webp(cached_path):
                _repair_glb_file(cached_path)
            cached_path = _ensure_godot_compatible_glb(cleaned_url, cached_path)
        duration = time.monotonic() - start_time
        return url, cached_path, duration, None

    # ── FRESH DOWNLOAD ───────────────────────────────────────────────────────
    asset_name = os.path.basename(urlparse(cleaned_url).path)
    print(f"  ↓ Downloading: {asset_name}")

    last_error = None
    for attempt in range(3):
        try:
            download_url = f"{cleaned_url}?{SAS_TOKEN}" if '?' not in cleaned_url else f"{cleaned_url}&{SAS_TOKEN.lstrip('?')}"
            req_timeout = int(os.getenv("ASSET_INDIVIDUAL_TIMEOUT_SECONDS", "150"))
            response = requests.get(download_url, timeout=req_timeout)
            response.raise_for_status()
            
            safe_name = asset_name.replace("_LR_", "_HR_")
            stable_prefix = hashlib.md5(cleaned_url.encode("utf-8", errors="ignore")).hexdigest()[:8]
            filename = f"{stable_prefix}_{safe_name}"
            local_path = os.path.abspath(os.path.join(ASSET_DOWNLOAD_DIR, filename))
            
            with open(local_path, "wb") as f:
                f.write(response.content)

            if cleaned_url.lower().endswith('.glb'):
                if _check_glb_for_webp(local_path):
                    _repair_glb_file(local_path)
                local_path = _ensure_godot_compatible_glb(cleaned_url, local_path)

            _add_to_asset_url_map(cleaned_url, local_path, asset_id=asset_id)
            duration = time.monotonic() - start_time
            return url, local_path, duration, None

        except requests.RequestException as e:
            last_error = e
            if attempt < 2:
                time.sleep(2)
    
    duration = time.monotonic() - start_time
    print(f"{COLOR_RED}  ✗ Failed: {asset_name}{COLOR_RESET}")
    return url, "", duration, last_error

def _download_asset_with_fallback(original_url: str, use_high_poly: bool = True, asset_id: str = ""):
    """
    Downloads a single asset URL and returns (original_url, local_path).
    The 'use_high_poly' flag is kept for API compatibility.
    """
    if not original_url:
        return "", ""

    if any(skip in original_url.lower() for skip in ['.svg', 'thumbnail', 'svg_']):
        return original_url, ""
    
    if original_url.lower().endswith('.glb'):
        file_type = "GLB"
    elif "/textures/" in original_url.lower():
        file_type = "Texture"
    elif original_url.lower().endswith('.obj'):
        file_type = "OBJ"
    else:
        file_type = "Asset"
    
    url, local_path, duration, error = _timed_download_worker(
        original_url, file_type, asset_id=asset_id
    )
    return original_url, local_path

def _parse_texture_urls_from_mtl(mtl_filepath: str) -> Set[str]:
    """Scans a local MTL file and extracts all external texture URLs."""
    urls = set()
    texture_keys = ('map_Kd', 'map_Ks', 'map_Ka', 'map_Bump', 'bump', 'map_d', 'map_Pr', 'map_Pm', 'map_Ke', 'map_bump', 'map_disp', 'map_Disp')
    try:
        with open(mtl_filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                parts = line.split()
                if not parts or parts[0] not in texture_keys:
                    continue
                
                for part in parts[1:]:
                    cleaned_part = part.strip('\"\'')
                    if cleaned_part.startswith(('http://', 'https://')):
                        urls.add(cleaned_part)
                        break
    except Exception as e:
        print(f"{COLOR_RED}ERROR parsing MTL for texture URLs {os.path.basename(mtl_filepath)}: {e}{COLOR_RESET}")
    return urls

def _update_mtl_texture_paths(mtl_filepath: str, download_map: dict):
    """Updates texture URLs inside a local MTL file to local basenames."""
    print(f"{COLOR_YELLOW}  -> Finalizing texture paths in: {os.path.basename(mtl_filepath)}{COLOR_RESET}")
    try:
        with open(mtl_filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        new_lines, was_updated = [], False
        for line in lines:
            for original_url, local_path in list(download_map.items()):
                if local_path and original_url in line:
                    basename = os.path.basename(local_path)
                    line = line.replace(original_url, basename)
                    was_updated = True
                    break
            new_lines.append(line)
        
        if was_updated:
            with open(mtl_filepath, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
    except Exception as e:
        print(f"{COLOR_RED}ERROR updating MTL file {mtl_filepath}: {e}{COLOR_RESET}")

# --- Paste this corrected function into your main.py ---

def _download_and_localize_assets(plan: dict, use_high_res: bool = True, logger: Optional[RenderLogger] = None):
    """
    ID-driven workflow:
    1. Recursively collect asset3D_Id / textureLibrary_Id references from the plan.
    2. Also collect direct material texture URLs such as mapUrl / normalUrl / roughnessUrl.
    3. Resolve those IDs through the master catalogues and download only the matched URLs.
    4. Parse downloaded MTLs to find and download any secondary texture assets.
    5. Update MTLs to use local texture paths.
    6. Update the main floorplan dict to use local paths.
    """
    if logger:
        logger.start_asset_download()
    
    # --- FETCH MASTER DATA (Once per run) ---
    _fetch_master_data_from_api()
    
    all_urls_to_find = set()
    
    # Store ID -> URL mappings so we can look them up later during replacement
    asset_id_to_url = {}
    texture_id_to_urls = {}
    input_url_to_hp = {}
    direct_texture_keys = {
        "mapUrl",
        "normalUrl",
        "roughnessUrl",
        "metalnessUrl",
        "aoUrl",
        "emissionUrl",
        "displacementUrl",
        "bumpUrl",
        "alphaUrl",
        "heightMapUrl",
    }

    def _register_id_mapping(mapping: dict, key, value) -> None:
        """Store an ID lookup using both raw and stringified keys."""
        if key is None or value is None:
            return
        mapping[key] = value
        key_str = str(key)
        mapping[key_str] = value
        try:
            key_int = int(key_str)
        except (TypeError, ValueError):
            key_int = None
        if key_int is not None:
            mapping[key_int] = value

    def _add_texture_url_candidate(url_set: set, value) -> None:
        """Collect a direct texture URL or a list of texture URLs."""
        if value is None:
            return
        if isinstance(value, list):
            for item in value:
                _add_texture_url_candidate(url_set, item)
            return
        if isinstance(value, str):
            cleaned = value.strip().strip('"').strip("'")
            if cleaned and cleaned.lower() not in ("null", "none"):
                if cleaned.startswith('textures/') or cleaned.startswith('glb-assets/'):
                    cleaned = f"https://zrealtystoragedev.blob.core.windows.net/{cleaned}"
                url_set.add(cleaned)
    
    # If plan is a string, parse it first so we can scan the object structure
    is_json_string = False
    if isinstance(plan, str):
        try:
            plan_data = json.loads(plan)
            is_json_string = True
        except Exception:
            plan_data = plan # Fallback
    else:
        plan_data = plan

    layer_to_scan = plan_data

    # Helper to recursively find asset and texture IDs in the plan.
    def collect_ids_recursive(data_obj, url_set):
        if isinstance(data_obj, str):
            # Try to parse stringified JSON first (e.g. floor_plan_data is often stringified)
            try:
                parsed_json = json.loads(data_obj)
                if isinstance(parsed_json, (dict, list)):
                    collect_ids_recursive(parsed_json, url_set)
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(data_obj, list):
            for item in data_obj:
                collect_ids_recursive(item, url_set)
        elif isinstance(data_obj, dict):
            asset_urls_dict = data_obj.get('asset_urls', {}) if isinstance(data_obj.get('asset_urls'), dict) else {}

            a_id = (
                data_obj.get('asset3D_Id')
                or data_obj.get('assetId_3D')
                or data_obj.get('assetId')
                or asset_urls_dict.get('asset3D_Id')
                or asset_urls_dict.get('assetId_3D')
                or asset_urls_dict.get('assetId')
            )
            if a_id is not None:
                resolved_asset_url = _resolve_asset_url(a_id, show_details=True)
                if resolved_asset_url:
                    _register_id_mapping(asset_id_to_url, a_id, resolved_asset_url)
                    url_set.add(resolved_asset_url)

            t_id = data_obj.get('textureLibrary_Id')
            if not t_id and isinstance(asset_urls_dict, dict):
                t_id = asset_urls_dict.get('textureLibrary_Id')
            if t_id is not None:
                resolved_urls = _resolve_texture_urls(t_id, show_details=True)
                if resolved_urls:
                    _register_id_mapping(texture_id_to_urls, t_id, resolved_urls)
                    for u in resolved_urls:
                        url_set.add(u)

            # Collect direct material texture URLs so floor/ceiling materials
            # are localized even when they do not use textureLibrary_Id.
            for key, value in data_obj.items():
                if key in direct_texture_keys:
                    _add_texture_url_candidate(url_set, value)

            # Recursively process nested values to find more IDs.
            for key, value in data_obj.items():
                collect_ids_recursive(value, url_set)

    # Phase 1: Collect ALL URLs from the entire plan structure
    collect_ids_recursive(layer_to_scan, all_urls_to_find)

    if not all_urls_to_find:
        print(f"\n{COLOR_YELLOW}--- No asset/texture references found. Skipping downloads. ---{COLOR_RESET}")
        if logger:
            logger.end_asset_download()
        if is_json_string:
            return json.dumps(plan_data)
        return plan_data

    # Build reverse map: resolved_url → asset_id so each GLB download worker
    # knows the originating asset ID to store in asset_url_map.json.
    url_to_asset_id: dict = {}
    for aid, aurl in asset_id_to_url.items():
        url_to_asset_id.setdefault(aurl, str(aid))

    # Phase 2: Download all collected assets
    print(f"\n{COLOR_BLUE}--- Asset Download Step 1: Found {len(all_urls_to_find)} unique assets. Starting download... ---{COLOR_RESET}")

    download_map = {}
    
    # Use manual executor management instead of 'with' block to allow non-blocking shutdown
    executor = ThreadPoolExecutor(max_workers=16)
    try:
        futures = {
            executor.submit(
                _download_asset_with_fallback,
                url,
                use_high_res,
                url_to_asset_id.get(url, ""),  # asset_id for GLBs; blank for textures
            ): url
            for url in all_urls_to_find
        }
        
        # --- IMPLEMENT ASSET DOWNLOAD TIMEOUT (Configurable via .env, default 2.5 mins) ---
        start_time = time.monotonic()
        # Default to 150 seconds if not set
        TIMEOUT_SECONDS = int(os.getenv("ASSET_DOWNLOAD_TIMEOUT_SECONDS", "150"))
        PRINT_INTERVAL = 10
        next_print_time = start_time + PRINT_INTERVAL
        
        completed_count = 0
        total_count = len(futures)
        
        # Collect futures as they complete, but with a total loop timeout
        # We use a while loop to check for timeout and print status
        pending = set(futures.keys())
        
        while pending:
            # Calculate time remaining
            now = time.monotonic()
            elapsed = now - start_time
            
            if elapsed > TIMEOUT_SECONDS:
                print(f"\n{COLOR_YELLOW}[WARN]  ASSET DOWNLOAD TIMEOUT REACHED ({TIMEOUT_SECONDS}s){COLOR_RESET}")
                print(f"  Proceeding with {completed_count}/{total_count} assets downloaded.")
                # Force non-blocking shutdown to proceed immediately
                # cancel_futures=True stops pending tasks. wait=False ignores running tasks.
                executor.shutdown(wait=False, cancel_futures=True)
                break
                
            # Wait for a short interval to allow printing status
            # We use wait() on the whole set, but with a short timeout
            # Note: wait() returns (done, not_done)
            done_now, pending_now = wait(pending, timeout=min(1.0, TIMEOUT_SECONDS - elapsed))
            
            # Process completed ones
            for future in done_now:
                pending.remove(future)
                completed_count += 1
                try:
                    original_url, final_path = future.result()
                    download_map[original_url] = final_path
                    
                    # Log asset
                    if logger and original_url:
                        asset_name = os.path.basename(original_url)
                        cached = final_path and os.path.exists(final_path) if final_path else False
                        logger.add_asset(original_url, asset_name, final_path, cached)
                except Exception as e:
                    print(f"{COLOR_RED}Error in initial download worker for {futures[future]}: {e}{COLOR_RESET}")

            # Print Status update
            if time.monotonic() >= next_print_time:
                show_details = os.getenv("ASSETS_DETAIL_PRINT", "false").lower() == "true"
                if not show_details:
                    print(f"⏳ Assets downloading ({int(elapsed)}s)...")
                next_print_time = time.monotonic() + PRINT_INTERVAL
                
    finally:
        # Ensure executor is eventually cleaned up, but don't block if we already shut it down
        try:
            # If we broke due to timeout, executor might be shut down already or running zombie threads.
            # Calling shutdown again with wait=False is safe.
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    # Phase 3: Parse MTLs and download secondary item/hole textures
    item_textures_to_download = set()
    mtl_files_to_update = []
    
    # Find which of our downloaded files are MTLs
    for url, local_path in download_map.items():
        if local_path and local_path.lower().endswith('.mtl'):
            mtl_files_to_update.append(local_path)
            print(f"\n{COLOR_BLUE}--- Parsing MTL '{os.path.basename(local_path)}' for associated textures... ---{COLOR_RESET}")
            texture_urls_in_mtl = _parse_texture_urls_from_mtl(local_path)
            if texture_urls_in_mtl:
                # Find textures that we haven't already downloaded
                new_textures = texture_urls_in_mtl - download_map.keys()
                if new_textures:
                    print(f"  Found {len(new_textures)} new texture(s) to download.")
                    item_textures_to_download.update(new_textures)
                else:
                    print("  All associated textures already accounted for.")
            else:
                print("  No external texture URLs found in this MTL.")
    
    if item_textures_to_download:
        print(f"\n{COLOR_BLUE}--- Asset Download Step 2: Downloading {len(item_textures_to_download)} additional textures discovered in MTLs ---{COLOR_RESET}")
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {executor.submit(_download_asset_with_fallback, url, use_high_res): url for url in item_textures_to_download}
            for future in as_completed(futures):
                try:
                    original_url, final_path = future.result()
                    download_map[original_url] = final_path
                    
                    # Log asset
                    if logger and original_url:
                        asset_name = os.path.basename(original_url)
                        cached = final_path and os.path.exists(final_path) if final_path else False
                        logger.add_asset(original_url, asset_name, final_path, cached)
                except Exception as e:
                    print(f"{COLOR_RED}Error in texture download worker for {futures[future]}: {e}{COLOR_RESET}")

    # Step 4: Update downloaded MTL files with local texture paths
    if mtl_files_to_update:
        print(f"\n{COLOR_BLUE}--- Asset Processing: Updating MTL files with local texture paths ---{COLOR_RESET}")
        for mtl_path in mtl_files_to_update:
            _update_mtl_texture_paths(mtl_path, download_map)

    # Step 5: Replace all URLs in the main floorplan dict with their local paths
    def replace_urls_in_plan(data_obj):
        if isinstance(data_obj, str):
            # Case 1: The string is a direct URL match
            test_url = input_url_to_hp.get(data_obj, data_obj)
            if isinstance(test_url, list):
                # If resolved to multiple HR textures, return local paths as a JSON list or similar
                # For MTL/Texture mapping, we usually expect a single local path. 
                # If it's a list, the renderer might need it differently. 
                # For now, let's return the first available local path if it's a single string context.
                local_paths = [download_map[u] for u in test_url if u in download_map]
                if local_paths:
                    return local_paths[0] if len(local_paths) == 1 else json.dumps(local_paths)
                return data_obj
            elif test_url in download_map:
                return download_map[test_url]
                
            # Case 2: The string is a relative GLB path - convert to full URL and look up
            if data_obj.startswith('glb-assets/'):
                test_url = input_url_to_hp.get(data_obj)
                if isinstance(test_url, list):
                    local_paths = [download_map[u] for u in test_url if u in download_map]
                    if local_paths: return json.dumps(local_paths) if len(local_paths) > 1 else local_paths[0]
                elif not test_url:
                    full_url = _convert_glb_path_to_url(data_obj)
                    test_url = input_url_to_hp.get(full_url, full_url)
                
                if not isinstance(test_url, list) and test_url in download_map:
                    return download_map[test_url]
                elif isinstance(test_url, list):
                    local_paths = [download_map[u] for u in test_url if u in download_map]
                    if local_paths: return json.dumps(local_paths) if len(local_paths) > 1 else local_paths[0]
            
            # Case 3: The string is a relative Texture path - convert to full URL and look up
            if data_obj.startswith('textures/'):
                 full_url = f"https://zrealtystoragedev.blob.core.windows.net/{data_obj}"
                 if full_url in download_map:
                     return download_map[full_url]
            
            # Case 4: The string is a JSON list of URLs or a stringified JSON object
            stripped = data_obj.strip()
            if (stripped.startswith('[') and stripped.endswith(']')) or (stripped.startswith('{') and stripped.endswith('}')):
                try:
                    parsed = json.loads(data_obj)
                    if isinstance(parsed, (list, dict)):
                        # Recursively process the parsed JSON
                        replaced = replace_urls_in_plan(parsed)
                        return json.dumps(replaced)
                except (json.JSONDecodeError, TypeError):
                    pass # Fallback to original string if parsing fails
            return data_obj # Return original string if no match
        elif isinstance(data_obj, list):
            return [replace_urls_in_plan(item) for item in data_obj]
        elif isinstance(data_obj, dict):
            # Process dictionary values
            new_dict = {}
            for k, v in data_obj.items():
                new_dict[k] = replace_urls_in_plan(v)
            
            # --- NEW: Inject Local Paths based on IDs ---
            
            # Explicitly handle isColorEdited to ensure it's a boolean (Fix for "asset colours not changing")
            if 'isColorEdited' in new_dict:
                val = new_dict['isColorEdited']
                if isinstance(val, str):
                    new_dict['isColorEdited'] = val.lower() == 'true'
            
            # Also check if it's inside 'asset_urls' commonly used in frontend
            if 'asset_urls' in new_dict and isinstance(new_dict['asset_urls'], dict):
                au = new_dict['asset_urls']
                if 'isColorEdited' in au:
                    val = au['isColorEdited']
                    if isinstance(val, str):
                        au['isColorEdited'] = val.lower() == 'true'
            
            # 1. Check direct keys
            a_id = new_dict.get('asset3D_Id') or new_dict.get('assetId_3D') or new_dict.get('assetId')

            # 2. Check nested inside 'asset_urls'
            if not a_id and 'asset_urls' in new_dict and isinstance(new_dict['asset_urls'], dict):
                a_id = (
                    new_dict['asset_urls'].get('asset3D_Id')
                    or new_dict['asset_urls'].get('assetId_3D')
                    or new_dict['asset_urls'].get('assetId')
                )

            if a_id and a_id in asset_id_to_url:
                url = asset_id_to_url[a_id]
                if url in download_map:
                     local_path = download_map[url]
                     # Godot prefers forward slashes for paths even on Windows
                     local_path = local_path.replace("\\", "/")
                     
                     # Update Top-level keys
                     if 'glb_Url' in new_dict: new_dict['glb_Url'] = local_path
                     if 'GLB_File_URL' in new_dict: new_dict['GLB_File_URL'] = local_path
                     if 'glb_url' in new_dict: new_dict['glb_url'] = local_path
                     
                     # Update Nested 'asset_urls' keys
                     if 'asset_urls' in new_dict and isinstance(new_dict['asset_urls'], dict):
                         au = new_dict['asset_urls']
                         if 'glb_Url' in au: au['glb_Url'] = local_path
                         if 'GLB_File_URL' in au: au['GLB_File_URL'] = local_path
                         if 'glb_url' in au: au['glb_url'] = local_path

            # If this dict has a textureLibrary_Id, we can update/inject 'texture_urls'
            if 'textureLibrary_Id' in new_dict:
                t_id = new_dict['textureLibrary_Id']
                if t_id in texture_id_to_urls:
                    urls = texture_id_to_urls[t_id]
                    # Filter to those that were successfully downloaded
                    local_texture_paths = []
                    for u in urls:
                        if u in download_map:
                            local_texture_paths.append(download_map[u])
                    
                    if local_texture_paths:
                        # Inject as 'texture_urls' (or 'textures'?) - using 'texture_urls' as a safe bet for renderer
                        new_dict['texture_urls'] = local_texture_paths
            
            return new_dict
        return data_obj

    print(f"\n{COLOR_BLUE}--- Finalizing floor plan data with all local asset paths ---{COLOR_RESET}")
    
    if logger:
        logger.end_asset_download()
    
    localized_plan = replace_urls_in_plan(plan_data)
    
    # Post-processing: fix any broken local paths
    localized_plan = _fix_broken_local_paths(localized_plan)
    
    # If original input was a string, return string (for Godot input compatibility)
    if is_json_string:
        return json.dumps(localized_plan)
        
    return localized_plan


IES_PROFILE_REF_KEYS = ("iesProfileId", "iesId")


def _iter_scene_light_dicts(plan_data: dict):
    """Yield every sceneLights entry in the plan (top-level and per-layer)."""
    if not isinstance(plan_data, dict):
        return
    containers = [plan_data]
    layers = plan_data.get("layers")
    if isinstance(layers, dict):
        containers.extend(l for l in layers.values() if isinstance(l, dict))
    for container in containers:
        lights = container.get("sceneLights")
        if isinstance(lights, dict):
            for light in lights.values():
                if isinstance(light, dict):
                    yield light


def _ies_profile_ref_id(light: dict) -> str:
    """Return the IES profile id a light references, or '' if it isn't an IES light."""
    if str(light.get("photometricType", "")).strip().lower() != "ies":
        return ""
    for key in IES_PROFILE_REF_KEYS:
        raw = light.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def _localize_ies_profiles(plan, logger: Optional[RenderLogger] = None):
    """
    Download the .ies photometric files referenced by sceneLights and rewrite the
    plan so the renderer gets absolute local paths.

    Profiles live in a top-level `iesProfiles` dict; a light points at one via
    `iesProfileId` or `iesId`. Only the profile's `url` is used as the source.

    Caching is keyed by URL, never by profile id: ids are per-project and get
    reused for different files, so an id-keyed cache would serve a stale profile
    to a later project. `_timed_download_worker` already caches on URL via
    asset_url_map.json, so this reuses it rather than adding a second scheme.

    Runs separately from _download_and_localize_assets because that function
    early-returns when a plan has no mesh/texture references — a lighting-only
    plan would otherwise never reach the download step.
    """
    is_json_string = isinstance(plan, str)
    try:
        plan_data = json.loads(plan) if is_json_string else plan
    except (ValueError, TypeError):
        return plan
    if not isinstance(plan_data, dict):
        return plan

    profiles = plan_data.get("iesProfiles")
    if not isinstance(profiles, dict) or not profiles:
        return plan

    # Only fetch profiles some light actually points at.
    referenced = {}
    for light in _iter_scene_light_dicts(plan_data):
        pid = _ies_profile_ref_id(light)
        if pid:
            referenced.setdefault(pid, []).append(light)

    if not referenced:
        return plan

    print(f"\n{COLOR_BLUE}--- IES: {len(referenced)} profile(s) referenced by scene lights ---{COLOR_RESET}")

    resolved: dict = {}
    for pid, lights in referenced.items():
        profile = profiles.get(pid) or profiles.get(str(pid))
        if not isinstance(profile, dict):
            print(f"{COLOR_YELLOW}  ! IES profile '{pid}' referenced but not present in iesProfiles — skipping{COLOR_RESET}")
            continue

        local_path = ""
        url = str(profile.get("url", "")).strip()
        if url:
            try:
                _, downloaded, _, err = _timed_download_worker(url, context="ies", asset_id="")
                if downloaded:
                    local_path = downloaded
                elif err:
                    print(f"{COLOR_YELLOW}  ! IES '{pid}' download failed: {err}{COLOR_RESET}")
            except Exception as e:
                print(f"{COLOR_YELLOW}  ! IES '{pid}' download error: {e}{COLOR_RESET}")

        # Fallback: the payload already carries the file's text, so a failed or
        # missing URL doesn't have to cost us the photometry.
        if not local_path:
            raw_text = profile.get("rawText")
            if isinstance(raw_text, str) and raw_text.strip():
                digest = hashlib.md5(raw_text.encode("utf-8", errors="ignore")).hexdigest()[:8]
                fallback_path = os.path.abspath(
                    os.path.join(ASSET_DOWNLOAD_DIR, f"{digest}_ies_{pid}.ies")
                )
                try:
                    if not (os.path.exists(fallback_path) and os.path.getsize(fallback_path) > 0):
                        with open(fallback_path, "w", encoding="utf-8", newline="") as f:
                            f.write(raw_text)
                    local_path = fallback_path
                    print(f"  ↳ IES '{pid}': using embedded rawText → {os.path.basename(local_path)}")
                except OSError as e:
                    print(f"{COLOR_YELLOW}  ! IES '{pid}' rawText fallback failed: {e}{COLOR_RESET}")

        if not local_path:
            print(f"{COLOR_RED}  ✗ IES '{pid}' unresolved — lights fall back to non-photometric{COLOR_RESET}")
            continue

        profile["local_path"] = local_path
        resolved[pid] = local_path
        # Stamp the path onto each referencing light so the renderer needs no
        # id lookup (and still works if the profile table is culled later).
        for light in lights:
            light["iesFile"] = local_path
        print(f"  ✓ IES '{pid}' ({profile.get('name', '?')}) → {os.path.basename(local_path)} [{len(lights)} light(s)]")

    if not resolved:
        return plan

    if is_json_string:
        return json.dumps(plan_data)
    return plan_data


def _fix_broken_local_paths(data, _depth=0):
    """
    Recursively walks plan data and fixes local file paths that don't exist.
    For paths pointing to other project folders (e.g., 'r&d'), tries to:
    1. Find the same filename in local asset_downloads
    2. Re-download from API if assetId_3D is available
    """
    # Use the configured asset cache (ASSET_DOWNLOAD_DIR from .env), NOT a
    # hardcoded path next to this script. The two can point at different
    # projects, and listing a non-existent folder killed the job with WinError 3.
    local_downloads = ASSET_DOWNLOAD_DIR

    if _depth > 20:  # Prevent infinite recursion
        return data
    
    if isinstance(data, str):
        # Check if it looks like a local file path that doesn't exist
        if (os.sep in data or '/' in data) and not data.startswith('http'):
            # It's a local path — check common file extensions
            ext = os.path.splitext(data)[1].lower()
            if ext in ('.glb', '.obj', '.mtl', '.jpg', '.jpeg', '.png', '.exr', '.hdr'):
                if not os.path.exists(data):
                    filename = os.path.basename(data)
                    local_candidate = os.path.join(local_downloads, filename).replace("\\", "/")
                    if os.path.exists(local_candidate):
                        print(f"  [RUN] Fixed broken path: {filename} (was: ...{data[-40:]})")
                        return local_candidate
                    else:
                        # Fallback: Check if there's a file with a different hash prefix
                        if "_" in filename and os.path.isdir(local_downloads):
                            base_part = filename.split('_', 1)[1]
                            for f in os.listdir(local_downloads):
                                if f.endswith(base_part):
                                    fallback_path = os.path.join(local_downloads, f).replace("\\", "/")
                                    print(f"  [RUN] Fixed broken path (fuzzy match): {filename} -> {f}")
                                    return fallback_path
                        print(f"  [WARN] Missing asset (not in cache): {filename}")
        return data
    
    elif isinstance(data, dict):
        fixed = {}
        for key, value in data.items():
            fixed[key] = _fix_broken_local_paths(value, _depth + 1)
        return fixed
    
    elif isinstance(data, list):
        return [_fix_broken_local_paths(item, _depth + 1) for item in data]
    
    return data

# ─────────────────────────────────────────────────────────────────────────────
# GPU / VRAM FAILURE DETECTION
# ─────────────────────────────────────────────────────────────────────────────
# When the GPU runs out of VRAM (or the driver resets mid-render), Godot does
# NOT reliably exit with a non-zero code. DLSS Ray Reconstruction fails per
# frame, the Vulkan device is lost, and Godot can still write out a black /
# garbage frame. Shipping that to the customer looks like a "successful"
# render of an empty room.
#
# Rule: if any driver-level failure marker appears in the Godot output, the
# render is DISCARDED and the job fails with an explicit GPU-capacity message.
# Never upload the crashed frame.

class GPUResourceError(Exception):
    """Render failed because of GPU/VRAM limits or a graphics-driver reset."""
    pass


# Customer-facing text. Deliberately generic - we do NOT expose GPU/VRAM
# internals to the end user, we just tell them the render failed.
GPU_STATUS_MESSAGE = "Sorry, Render failed !"

# Internal-only text. Goes into server logs, the render log and the exception
# so engineers can see the real reason. Never sent to the customer.
GPU_INTERNAL_MESSAGE = (
    "GPU render failure: the GPU ran out of video memory (VRAM) or the graphics "
    "driver reset while rendering this scene. This machine's GPU does not have "
    "enough VRAM for this scene at this quality. No output was produced."
)

# Case-insensitive substrings that mean "the graphics device failed".
_GPU_FAILURE_MARKERS = (
    "slevaluatefeature",                # Streamline / DLSS evaluation failed
    "eerrorngxfailed",                  # NGX (DLSS) backend failure
    "dlss ray reconstruction. result",  # DLSS RR failure line
    "couldn't wait for vulkan fence",   # device lost while waiting on a fence
    "vkresult error -4",                # VK_ERROR_DEVICE_LOST
    "vkresult error -2",                # VK_ERROR_OUT_OF_DEVICE_MEMORY
    "vk_error_device_lost",
    "vk_error_out_of_device_memory",
    "print_lost_device_info",
    "last known breadcrumb",
    "crashhandlerexception",            # Godot crash handler fired
    "program crashed",
    "dxgi_error_device_removed",
    "device_removed",
    "out of device memory",
    "out of video memory",
    "nvoglv64.dll",                     # NVIDIA driver frame in the backtrace
    "nvwgf2umx.dll",
    "amdvlk",
    "atidxx64.dll",
)


def _detect_gpu_failure(output_lines) -> Optional[str]:
    """Return the first Godot output line matching a GPU/driver failure marker."""
    for line in output_lines or []:
        low = str(line).lower()
        for marker in _GPU_FAILURE_MARKERS:
            if marker in low:
                return str(line).strip()
    return None


def _is_dead_black_frame(image_path: str) -> bool:
    """
    True when the rendered PNG is *completely* black (every channel maxes at 0).

    A real interior render always has some light in it, so an all-zero frame
    means the GPU dropped the render. Deliberately strict (exact zero) so a
    legitimately dark night render is never rejected.
    """
    try:
        if not image_path or not os.path.isfile(image_path):
            return False
        with Image.open(image_path) as img:
            extrema = img.convert("RGB").getextrema()
        return all(channel[1] == 0 for channel in extrema)
    except Exception as exc:
        print(f"[GPU] Black-frame check skipped ({exc})")
        return False


def _purge_crashed_outputs(*paths) -> None:
    """Delete the black / partial artefacts left behind by a crashed GPU render."""
    for path in paths:
        if not path:
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
                print(f"[GPU] Discarded crashed render artefact: {path}")
        except Exception as exc:
            print(f"[GPU] Could not delete crashed artefact {path}: {exc}")


def _abort_render_process(proc, marker_line: str, label: str = "Godot") -> None:
    """
    Kill a still-running render process the moment a GPU/driver error appears
    in its console output.

    Without this the engine keeps grinding through the remaining frames -
    every one of them failing the same way - until it finally hard-crashes or
    hits the render timeout. There is nothing to recover once the graphics
    device is gone, so we stop immediately and free the GPU for the next job.
    """
    print(f"{COLOR_RED}{'=' * 80}{COLOR_RESET}")
    print(f"{COLOR_RED}[GPU] {label} reported a GPU/driver failure - STOPPING THE RENDER NOW{COLOR_RESET}")
    print(f"{COLOR_RED}      (not waiting for the process to crash or time out){COLOR_RESET}")
    print(f"{COLOR_RED}      {marker_line}{COLOR_RESET}")
    print(f"{COLOR_RED}{'=' * 80}{COLOR_RESET}")
    try:
        proc.kill()
    except Exception as exc:
        print(f"[GPU] Could not stop the {label} process: {exc}")


def _raise_gpu_failure(marker_line: str, log_path: str = "", extra_paths=()) -> None:
    """Delete crashed output, log loudly, and raise a clear GPU-capacity error."""
    _purge_crashed_outputs(*extra_paths)
    print(f"{COLOR_RED}{'=' * 80}{COLOR_RESET}")
    print(f"{COLOR_RED}[GPU] GPU FAILURE DETECTED - render aborted, nothing will be uploaded{COLOR_RESET}")
    print(f"{COLOR_RED}      Driver marker: {marker_line}{COLOR_RESET}")
    if log_path:
        print(f"{COLOR_RED}      Godot log: {log_path}{COLOR_RESET}")
    print(f"{COLOR_RED}{'=' * 80}{COLOR_RESET}")
    raise GPUResourceError(f"{GPU_INTERNAL_MESSAGE} [driver error: {marker_line}]")


def _run_blender_process(job_id: str, input_json_path: str, output_image_path: str) -> str:
    """
    Blender counterpart of the Godot subprocess call in _run_godot_render().
    Reads the SAME localized *_godot_input.json and writes the same outputs
    (<job>_render.png + <job>_render_thumb.webp), so upload/status code is unchanged.
    """
    script = os.path.join(BLENDER_SCRIPTS_DIR, "render.py")
    if not os.path.exists(BLENDER_RENDER_EXE):
        raise Exception(f"Blender executable not found: {BLENDER_RENDER_EXE} (set BLENDER_RENDER_EXE)")
    if not os.path.exists(script):
        raise Exception(f"Blender render script not found: {script}")
    log_path = os.path.join(GODOT_LOGS_DIR, f"{job_id}_blender.log")
    cmd = [BLENDER_RENDER_EXE, "--background", "--factory-startup", "--python-exit-code", "1", "--python", script, "--",
           input_json_path, output_image_path]
    print(f"Executing Blender: {' '.join(cmd)}")
    env = dict(os.environ)
    env["ASSET_DOWNLOAD_DIR"] = ASSET_DOWNLOAD_DIR
    lines: list = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1, env=env)

    def _stream():
        try:
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\r\n")
                lines.append(line)
                print(line if line.startswith("[Blender]") else f"[Blender] {line}", flush=True)
        except Exception as exc:
            print(f"Warning: Blender stream reader stopped early: {exc}")

    reader = Thread(target=_stream, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=BLENDER_RENDER_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise Exception(f"Blender execution timed out after {BLENDER_RENDER_TIMEOUT}s")
    finally:
        reader.join(timeout=10)
        try:
            with open(log_path, "w", encoding="utf-8") as lf:
                lf.write("\n".join(lines))
        except Exception:
            pass
    if proc.returncode != 0:
        raise Exception(f"Blender execution failed (exit code {proc.returncode}). See log: {log_path}")
    if not os.path.exists(output_image_path):
        raise Exception("Blender did not produce an output image.")
    if _is_dead_black_frame(output_image_path):
        _purge_crashed_outputs(output_image_path)
        raise Exception("Blender render produced an empty/black frame.")
    print(f"{COLOR_GREEN}[OK] Blender Render Success: {output_image_path}{COLOR_RESET}")
    return output_image_path


def _run_godot_render(job_id: str, payload: GenerationPayload, logger: Optional[RenderLogger] = None) -> str:
    """
    Runs the Godot rendering process.
    1. Localize assets.
    2. Save input JSON.
    3. Call Godot headless.
    4. Return path to output image.
    """
    if logger:
        # Reusing logs for consistency
        logger.start_glb_creation() 
    
    print(f"{COLOR_BLUE}--- Starting Godot Render ---{COLOR_RESET}")
    
    # 1. Localize Assets
    floor_plan_data = payload.floor_plan_data
    scene_data = json.loads(floor_plan_data) if isinstance(floor_plan_data, str) else floor_plan_data
    localized_geometry = _download_and_localize_assets(scene_data, use_high_res=True, logger=logger)
    localized_geometry = _localize_ies_profiles(localized_geometry, logger=logger)

    # 2. Build Godot Input
    # Convert payload to dict to get camera, lights, etc.
    godot_input = payload.dict(exclude={'floor_plan_data', 'video_animation'}, exclude_unset=True)
    # Inject localized geometry
    godot_input['floor_plan_data'] = localized_geometry
    
    if payload.environment_url:
        res = _download_asset_with_fallback(payload.environment_url, use_high_poly=True)
        if res and res[1]:
            godot_input['environment_url'] = res[1]

    # 2.5 Scene Culling — remove rooms/items not visible from camera
    output_basename = f"{job_id}_render"
    culling_log_path = os.path.join(CULLING_LOGS_DIR, f"{output_basename}_culling.txt")
    try:
        optimizer = SceneOptimizer(log_path=culling_log_path)
        godot_input = optimizer.cull_scene(godot_input)
        print(f"[OK] Scene culling complete. Log: {culling_log_path}")
    except Exception as e:
        print(f"[WARN] Scene culling failed (continuing without culling): {e}")

    # 2.54 Per-job LLM switch. The request's input JSON may carry
    # "USE_LLM": true/false. It is read here, once, and applies to all three
    # passes below — they all gate through zlendo_llm.config.llm_enabled().
    #
    # .env wins: USE_LLM=False there means no model runs whatever the JSON
    # says. The JSON can only turn the passes OFF for this one job, never turn
    # them back on. A caller that omits the field gets the .env behaviour, so
    # every existing request keeps working unchanged.
    _llm_gate_token = None
    try:
        from zlendo_llm.config import apply_job_llm_flag
        _llm_gate_token = apply_job_llm_flag(godot_input, job_id=job_id)
    except Exception as e:
        print(f"[WARN] Could not read USE_LLM from input JSON (using .env): {e}")

    # 2.55 Automation: colour-aware lighting adjustment.
    # MUST run AFTER culling — it averages the wall/floor/ceiling colours of the
    # rooms that survive, so running it earlier would weight rooms that are
    # about to be thrown away. Writes _automationAmbientBoost /
    # _automationSunMultiplier / _automationColorAdjustment into godot_input;
    # image_glb_creation.gd reads the first two in setup_lighting().
    # Gated by AUTOMATION_LLM_ADJUSTMENT in .env.
    try:
        from zlendo_llm.automation.integration import auto_adjust_lighting
        godot_input = auto_adjust_lighting(godot_input, job_id=job_id, logger=logger)
    except Exception as e:
        print(f"[WARN] Automation lighting adjustment unavailable (continuing): {e}")

    # 2.56 Automation: per-fixture emission control. Sets light_meta.intensity
    # and light_meta.temperature on every light asset in the scene.
    #
    # MUST run AFTER the colour pass above, which is what sets colorMood -
    # tuning each lamp to a mood that is about to change underneath it is
    # wasted work. The two passes answer different questions: the one above
    # decides how bright the ROOM is, this one decides what each light ASSET
    # emits. A scene can have a perfect illuminationLevel and still look
    # wrong because its six lamps span 2000K-7500K, or because the ceiling
    # pendant is dimmer than the floor lamps. No global control reaches
    # either of those; only per-fixture values do.
    #
    # Writes light_meta back into floor_plan_data (preserving whether that
    # field arrived as a string or a dict) plus _automationEmissionAdjustment
    # for audit. Gated by AUTOMATION_LLM_EMISSION in .env.
    try:
        from zlendo_llm.automation.integration import auto_adjust_emission
        godot_input = auto_adjust_emission(godot_input, job_id=job_id, logger=logger)
    except Exception as e:
        print(f"[WARN] Automation emission adjustment unavailable (continuing): {e}")

    # 2.6 AI Lighting Upgrade — local Qwen reviews the scene and fixes the
    # lighting before the path tracer runs. Gated by USE_LLM in .env.
    try:
        from zlendo_llm.integration import ai_upgrade_lighting
        godot_input = ai_upgrade_lighting(godot_input, job_id=job_id, logger=logger)
    except Exception as e:
        print(f"[WARN] AI lighting upgrade unavailable (continuing): {e}")

    # 2.65 The per-job LLM switch applies only to the block above; drop it so
    # nothing later in this process inherits one job's choice.
    if _llm_gate_token is not None:
        try:
            from zlendo_llm.config import reset_job_llm_override
            reset_job_llm_override(_llm_gate_token)
        except Exception:
            pass

    # Both passes above mutate godot_input, so they MUST stay above this line —
    # the JSON below is what Godot actually reads.
    # 3. Save Input JSON
    input_json_path = os.path.abspath(os.path.join(RUNTIME_DIR, f"{job_id}_godot_input.json"))
    with open(input_json_path, "w", encoding="utf-8") as f:
        json.dump(godot_input, f, indent=2)
        
    output_image_path = os.path.abspath(os.path.join(RENDER_OUTPUT_DIR, f"{job_id}_render.png"))
    godot_log_path = os.path.join(GODOT_LOGS_DIR, f"{job_id}_godot.log")

    # Blender backend: same localized input JSON, same output paths.
    if RENDER_BACKEND == "blender":
        _run_blender_process(job_id, input_json_path, output_image_path)
        if logger:
            logger.end_glb_creation()
        return output_image_path

    # Call Godot
    godot_exe = GODOT_EXE
    project_path = GODOT_PROJECT_PATH
    
    # Arguments: --path <project> "res://main.tscn" -- <input> <output>
    # NOTE: Do NOT use --headless for renders; headless disables rendering in Godot 4.x.
    cmd = [godot_exe]
    cmd.extend([
        "--path", project_path,
        "res://main.tscn",
        "--",
        input_json_path,
        output_image_path
    ])
    
    print(f"Executing Godot: {' '.join(cmd)}")

    all_output_lines: list[str] = []
    TIMEOUT = 1500  # seconds

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        # Holds the first GPU/driver failure line seen on the console, if any.
        gpu_abort: list = []

        def _stream_godot_output():
            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\r\n")
                    all_output_lines.append(line)
                    print(f"[Godot] {line}", flush=True)
                    # FAIL FAST: kill Godot on the first GPU/driver error line
                    # instead of letting it churn through the remaining frames.
                    if not gpu_abort:
                        marker = _detect_gpu_failure((line,))
                        if marker:
                            gpu_abort.append(marker)
                            _abort_render_process(proc, marker, "Godot")
            except Exception as stream_exc:
                print(f"{COLOR_YELLOW}Warning: Godot stream reader stopped early: {stream_exc}{COLOR_RESET}")

        reader = Thread(target=_stream_godot_output, daemon=True)
        reader.start()

        try:
            proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            # If the GPU already failed, report that - not a timeout.
            if gpu_abort:
                _raise_gpu_failure(gpu_abort[0], godot_log_path, extra_paths=(output_image_path,))
            raise Exception(f"Godot execution timed out after {TIMEOUT}s")
        finally:
            reader.join(timeout=10)

        with open(godot_log_path, "w", encoding="utf-8") as lf:
            lf.write("\n".join(all_output_lines))

        # GPU / VRAM failure check - MUST run before we trust the exit code or
        # the output file. A lost Vulkan device / failed DLSS pass can still
        # leave a black PNG on disk and exit with code 0.
        gpu_marker = gpu_abort[0] if gpu_abort else _detect_gpu_failure(all_output_lines)
        if gpu_marker:
            _raise_gpu_failure(gpu_marker, godot_log_path, extra_paths=(output_image_path,))

        if proc.returncode != 0:
            print(f"[GodotLog] Godot exited with non-zero code {proc.returncode}")
            print(f"[GodotLog] Full log saved to: {godot_log_path}")
            raise Exception(
                f"Godot execution failed (exit code {proc.returncode}). See log: {godot_log_path}"
            )

    except Exception:
        if all_output_lines:
            try:
                with open(godot_log_path, "w", encoding="utf-8") as lf:
                    lf.write("\n".join(all_output_lines))
            except Exception:
                pass
        raise

    if not os.path.exists(output_image_path):
        raise Exception("Godot did not produce an output image.")

    # Backstop: some GPU drop-outs leave no matchable error line but write a
    # fully black frame. Never ship that as a finished render.
    if _is_dead_black_frame(output_image_path):
        _purge_crashed_outputs(output_image_path)
        print(f"{COLOR_RED}[GPU] Render output was a fully black frame - treating as GPU failure.{COLOR_RESET}")
        raise GPUResourceError(
            f"{GPU_INTERNAL_MESSAGE} [render produced an empty/black frame]"
        )

    print(f"{COLOR_GREEN}[OK] Godot Render Success: {output_image_path}{COLOR_RESET}")

    if logger:
        logger.end_glb_creation() # Mark step done

    return output_image_path
def _find_ffmpeg_path():
    """
    Finds ffmpeg executable path on Windows system.
    Checks local installation first, then common installation locations and PATH.
    """
    # Check local installation first (in project directory)
    local_ffmpeg = os.path.join(os.path.dirname(__file__), "ffmpeg", "bin", "ffmpeg.exe")
    if os.path.exists(local_ffmpeg):
        return local_ffmpeg
    
    # Common ffmpeg installation paths on Windows
    common_paths = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        r"C:\tools\ffmpeg\bin\ffmpeg.exe",
        r"C:\Users\{}\AppData\Local\ffmpeg\bin\ffmpeg.exe".format(os.getenv('USERNAME', '')),
    ]
    
    # Check common paths
    for path in common_paths:
        if os.path.exists(path):
            print(f"[OK] Found ffmpeg at: {path}")
            return path
    
    # Check if ffmpeg is in PATH
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        if result.returncode == 0:
            print("[OK] Found ffmpeg in system PATH")
            return 'ffmpeg'  # Return command name if found in PATH
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    # Try to find in Program Files
    program_files_paths = [
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    ]
    
    for base_path in program_files_paths:
        if os.path.exists(base_path):
            try:
                for item in os.listdir(base_path):
                    ffmpeg_path = os.path.join(base_path, item, "bin", "ffmpeg.exe")
                    if os.path.exists(ffmpeg_path):
                        print(f"[OK] Found ffmpeg at: {ffmpeg_path}")
                        return ffmpeg_path
            except (PermissionError, OSError):
                continue
    
    return None

def _delete_png_frames(output_dir: str, base_name: str):
    """
    Deletes PNG frame files after successful MP4 encoding.
    
    Args:
        output_dir: Directory containing PNG frames
        base_name: Base name of the PNG files (e.g., "render" for "render_0001.png")
    """
    try:
        import glob
        pattern = os.path.join(output_dir, f"{base_name}_*.png")
        png_files = sorted(glob.glob(pattern))
        
        if not png_files:
            return
        
        print(f"\n🧹 Cleaning up {len(png_files)} temporary PNG frames...")
        deleted_count = 0
        
        for png_file in png_files:
            try:
                os.remove(png_file)
                deleted_count += 1
            except Exception as e:
                print(f"[WARN]  Warning: Could not delete {png_file}: {e}")
        
        if deleted_count > 0:
            print(f"[OK] Deleted {deleted_count} PNG frame files")
        else:
            print(f"[WARN]  No PNG files were deleted")
    except Exception as e:
        print(f"[WARN]  Warning: Error during PNG cleanup: {e}")

def _encode_png_sequence_to_mp4(output_dir: str, base_name: str, output_mp4_path: str, fps: int = 30, delete_frames: bool = True) -> bool:
    """
    Encodes PNG image sequence to MP4 video using ffmpeg.
    
    Args:
        output_dir: Directory containing PNG frames
        base_name: Base name of the PNG files (e.g., "render" for "render_0001.png")
        output_mp4_path: Full path where MP4 should be saved
        fps: Frame rate for the video (default: 30)
        delete_frames: Whether to delete PNG frames after successful encoding (default: True)
    
    Returns:
        True if encoding succeeded, False otherwise
    """
    try:
        print(f"\n{'='*60}\n🎬 ENCODING PNG SEQUENCE TO MP4\n{'='*60}")
        print(f"Input directory: {output_dir}")
        print(f"Base name: {base_name}")
        print(f"Output MP4: {output_mp4_path}")
        print(f"Frame rate: {fps} fps")
        
        # Find all PNG files matching the pattern
        import glob
        pattern = os.path.join(output_dir, f"{base_name}_*.png")
        png_files = sorted(glob.glob(pattern))
        
        if not png_files:
            print(f"❌ ERROR: No PNG files found matching pattern: {pattern}")
            return False
        
        print(f"[OK] Found {len(png_files)} PNG frames")
        
        # Find ffmpeg executable
        ffmpeg_path = _find_ffmpeg_path()
        if not ffmpeg_path:
            print("\n" + "="*80)
            print("❌ ERROR: ffmpeg not found. Please install ffmpeg to encode videos.")
            print("="*80)
            print("\n📥 QUICK INSTALLATION (Recommended):")
            print("   Run this command to automatically download and install ffmpeg:")
            print("   python install_ffmpeg.py")
            print("\n📥 MANUAL INSTALLATION OPTIONS:")
            print("\n1. Download from official website:")
            print("   https://www.gyan.dev/ffmpeg/builds/")
            print("   - Download 'ffmpeg-release-essentials.zip'")
            print("   - Extract to: " + os.path.join(os.path.dirname(__file__), "ffmpeg"))
            print("\n2. Install via Chocolatey (if installed):")
            print("   choco install ffmpeg")
            print("\n3. Install via winget (Windows 10/11):")
            print("   winget install ffmpeg")
            print("\n[WARN]  After installation, restart this application.")
            print("="*80 + "\n")
            return False
        
        # Determine frame numbering pattern
        # Check if frames start from 0000 or 0001
        first_frame_0000 = os.path.exists(os.path.join(output_dir, f"{base_name}_0000.png"))
        first_frame_0001 = os.path.exists(os.path.join(output_dir, f"{base_name}_0001.png"))
        
        if first_frame_0000:
            # Frames start from 0000
            input_pattern = os.path.join(output_dir, f"{base_name}_%04d.png")
            start_number = 0
        elif first_frame_0001:
            # Frames start from 0001
            input_pattern = os.path.join(output_dir, f"{base_name}_%04d.png")
            start_number = 1
        else:
            # Try to find the first frame
            first_frame = None
            for i in range(10000):
                frame_path = os.path.join(output_dir, f"{base_name}_{i:04d}.png")
                if os.path.exists(frame_path):
                    first_frame = i
                    break
            
            if first_frame is None:
                print(f"❌ ERROR: Could not find first frame in sequence")
                return False
            
            input_pattern = os.path.join(output_dir, f"{base_name}_%04d.png")
            start_number = first_frame
            print(f"   Detected frame numbering starting from: {start_number:04d}")
            
        # VALIDATE FRAME SEQUENCE (Prevent missing frames/gaps)
        print(f"🔍 Validating frame sequence ({len(png_files)} frames detected)...")
        expected_count = len(png_files)
        # Check for gaps
        frames_found = []
        for f in png_files:
            try:
                # Extract number from filename (assuming ..._XXXX.png)
                num_part = f[-8:-4] 
                if num_part.isdigit():
                    frames_found.append(int(num_part))
            except:
                pass
        
        frames_found.sort()
        if len(frames_found) > 1:
            # Check for gaps
            gaps = []
            for i in range(len(frames_found) - 1):
                if frames_found[i+1] != frames_found[i] + 1:
                    gaps.append((frames_found[i], frames_found[i+1]))
            
            if gaps:
                print(f"[WARN]  WARNING: Found {len(gaps)} gaps in frame sequence!")
                print(f"   First gap: between {gaps[0][0]} and {gaps[0][1]}")
                print(f"   FFmpeg might skip or shorten the video.")
                # Optional: We could fill gaps here, but for now just warn
            else:
                print(f"[OK] Frame sequence is contiguous (Indices {frames_found[0]} to {frames_found[-1]})")
        
        # Build ffmpeg command - Two step process:
        # Step 1: Create video from PNG frames (no rotation) → temporary file
        # Step 2: Rotate the video by 180 degrees → final output
        temp_video_path = os.path.join(output_dir, f"{base_name}_temp.mp4")
        
        # Step 1: Create video from PNG frames (no rotation)
        ffmpeg_cmd_step1 = [
            ffmpeg_path,
            '-y',  # Overwrite output file
            '-start_number', str(start_number),
            '-framerate', str(fps), # Input framerate
            '-i', input_pattern,
            '-r', str(fps), # Output framerate (CRITICAL: Locks output timing)
            '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p',
            '-crf', '18',  # High quality
            '-preset', 'medium',
            temp_video_path
        ]
        
        print(f"📹 Step 1: Creating video from PNG frames...")
        print(f"   Command: {' '.join(ffmpeg_cmd_step1)}")
        
        # Run step 1: Create video
        result_step1 = subprocess.run(
            ffmpeg_cmd_step1,
            capture_output=True,
            text=True,
            timeout=1500  # 10 minute timeout for encoding
        )
        
        if result_step1.returncode != 0:
            print(f"❌ ERROR: Step 1 (video creation) failed")
            print(f"   Return code: {result_step1.returncode}")
            if result_step1.stderr:
                print(f"   Error output: {result_step1.stderr[:500]}")
            return False
        
        if not os.path.exists(temp_video_path) or os.path.getsize(temp_video_path) == 0:
            print(f"❌ ERROR: Temporary video file was not created or is empty")
            return False
        
        # Step 2: Add audio to video (rotation commented out)
        # Rotation was: transpose=2,transpose=2 = +180° (clockwise) | transpose=1,transpose=1 = -180° (counter-clockwise)
        
        # Find audio file
        script_dir = os.path.dirname(os.path.abspath(__file__))
        audio_path = os.path.join(script_dir, "audio", "audio.mp3")
        has_audio = os.path.exists(audio_path)
        
        if has_audio:
            print(f"🎵 Found audio file: {audio_path}")
            # Step 2: Add audio (rotation commented out)
            ffmpeg_cmd_step2 = [
                ffmpeg_path,
                '-y',  # Overwrite output file
                '-i', temp_video_path,  # Video input
                '-i', audio_path,  # Audio input
                # '-vf', 'transpose=2,transpose=2',  # Rotate 180 degrees clockwise - COMMENTED OUT
                '-c:v', 'libx264',
                '-c:a', 'aac',  # Audio codec
                '-b:a', '192k',  # Audio bitrate
                '-shortest',  # Finish encoding when the shortest input stream ends
                '-pix_fmt', 'yuv420p',
                '-crf', '18',  # High quality
                '-preset', 'medium',
                output_mp4_path
            ]
            print(f"🎵 Step 2: Adding audio to video...")
        else:
            print(f"[WARN]  Audio file not found: {audio_path} (continuing without audio)")
            # Step 2: Copy video only (no rotation, no audio)
            ffmpeg_cmd_step2 = [
                ffmpeg_path,
                '-y',  # Overwrite output file
                '-i', temp_video_path,
                # '-vf', 'transpose=2,transpose=2',  # Rotate 180 degrees clockwise - COMMENTED OUT
                '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p',
                '-crf', '18',  # High quality
                '-preset', 'medium',
                output_mp4_path
            ]
            print(f"📹 Step 2: Copying video to final output...")
        
        print(f"   Command: {' '.join(ffmpeg_cmd_step2)}")
        
        # Run step 2: Process video (add audio and/or rotate)
        result_step2 = subprocess.run(
            ffmpeg_cmd_step2,
            capture_output=True,
            text=True,
            timeout=1500  # 10 minute timeout for encoding
        )
        
        # Clean up temporary file
        if os.path.exists(temp_video_path):
            try:
                os.remove(temp_video_path)
                print(f"🧹 Cleaned up temporary video file")
            except Exception as e:
                print(f"[WARN]  Warning: Could not delete temporary file: {e}")
        
        if result_step2.returncode != 0:
            print(f"❌ ERROR: Step 2 (video processing) failed")
            print(f"   Return code: {result_step2.returncode}")
            if result_step2.stderr:
                print(f"   Error output: {result_step2.stderr[:500]}")
            return False
        
        if os.path.exists(output_mp4_path) and os.path.getsize(output_mp4_path) > 0:
            file_size = os.path.getsize(output_mp4_path)
            audio_status = "with audio" if has_audio else "without audio"
            print(f"[OK] SUCCESS: MP4 video encoded and {audio_status} successfully!")
            print(f"   Output: {output_mp4_path}")
            print(f"   Size: {file_size:,} bytes ({file_size / (1024*1024):.2f} MB)")
            if has_audio:
                print(f"   🎵 Audio: {audio_path}")
            
            # Delete PNG frames after successful encoding (if requested)
            if delete_frames:
                _delete_png_frames(output_dir, base_name)
            
            return True
        else:
            print(f"❌ ERROR: Final MP4 file was not created or is empty")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"❌ ERROR: ffmpeg encoding timed out after 10 minutes")
        return False
    except Exception as e:
        print(f"❌ ERROR: Failed to encode PNG sequence to MP4: {e}")
        import traceback
        traceback.print_exc()
        return False



def _get_thumbnail_path(file_path: str) -> str:
    """Return the expected thumbnail path next to a rendered image or video."""
    base_dir = os.path.dirname(os.path.abspath(file_path))
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    return os.path.join(base_dir, f"{base_name}_thumb.webp")


def _upload_to_blob_storage(file_path: str, blob_container: str = "render-images") -> str:
    """
    Uploads the rendered file (image or video) to Azure Blob Storage and returns the blob URL.
    
    Args:
        file_path: Local path to the file (image or video)
        blob_container: Name of the blob container (default: "render-images")
    
    Returns:
        Full URL of the uploaded blob, or None if upload failed
    """
    try:
        print(f"\n{'='*60}\n☁️  UPLOADING TO BLOB STORAGE\n{'='*60}")
        print(f"File Path: {file_path}")
        print(f"Container: {blob_container}")
        
        # Get the filename
        filename = os.path.basename(file_path)
        
        # Determine content type based on file extension
        file_ext = os.path.splitext(filename)[1].lower()
        if file_ext == '.mp4':
            content_type = 'video/mp4'
            file_type = "Video"
        elif file_ext in ['.png', '.jpg', '.jpeg']:
            content_type = 'image/png' if file_ext == '.png' else 'image/jpeg'
            file_type = "Image"
        elif file_ext == '.webp':
            content_type = 'image/webp'
            file_type = "Thumbnail"
        else:
            content_type = 'application/octet-stream'
            file_type = "File"
        
        # Construct blob URL
        blob_base_url = "https://zrealtystoragedev.blob.core.windows.net"
        blob_path = f"{blob_container}/{filename}"
        blob_url = f"{blob_base_url}/{blob_path}"
        
        # Construct upload URL with SAS token
        upload_url = f"{blob_url}?{SAS_TOKEN}"
        
        print(f"Blob URL: {blob_url}")
        print(f"Uploading {file_type}: {filename}")
        
        # Read the file
        with open(file_path, 'rb') as file:
            file_content = file.read()
            file_size = len(file_content)
            print(f"File size: {file_size:,} bytes ({file_size / (1024*1024):.2f} MB)")
        
        # Upload to blob storage using PUT request
        headers = {
            'x-ms-blob-type': 'BlockBlob',
            'Content-Type': content_type
        }
        
        print(f"Uploading to: {blob_url}")
        response = requests.put(upload_url, data=file_content, headers=headers, timeout=1500)  # Longer timeout for videos
        
        if response.status_code in [200, 201]:
            print(f"{COLOR_GREEN}[OK] SUCCESS: {file_type} uploaded to blob storage!{COLOR_RESET}")
            print(f"   Blob URL: {blob_url}")
            return blob_url
        else:
            print(f"{COLOR_RED}❌ FAILED: Blob upload returned status {response.status_code}{COLOR_RESET}")
            print(f"   Response: {response.text}")
            return None
            
    except Exception as e:
        print(f"{COLOR_RED}❌ Error uploading to blob storage: {e}{COLOR_RESET}") 
        import traceback
        traceback.print_exc()
        return None

def _send_status_update(
    project_id: int, 
    gallery_id: int, 
    user_id: int, 
    job_id: str,
    status: str,
    step: str,
    progress: int,
    message: str,
    render_type: str = "IMAGE",
    file_path: Optional[str] = None,
    blob_url: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    logger: Optional[RenderLogger] = None,
    api_endpoint: Optional[str] = None,
    wait_for_delivery: bool = False
) -> bool:
    """
    Sends status update to UploadGallerAI endpoint in a NON-BLOCKING background thread.
    
    Status Codes (NEW):
    - 1 = InProgress (for all steps before the last step)
    - 2 = Completed (for the last/final step)
    - 3 = Rejected (for exceptional handling/validation failures)
    - 4 = Error (for errors/exceptions)
    
    If wait_for_delivery is True, the status payload is sent synchronously so
    the engine cannot reorder the AI job's in-progress and completed updates.

    Returns:
        True immediately (queued successfully) unless wait_for_delivery is used.
    """
    
    # 1. Update active_jobs dictionary details IMMEDIATELY (synchronous)
    # This ensures the local monitoring dashboard is instant
    with queue_lock:
        if job_id in active_jobs:
            active_jobs[job_id]['status'] = status
            active_jobs[job_id]['progress'] = progress
            active_jobs[job_id]['current_step'] = step
            active_jobs[job_id]['message'] = message
            
    # 2. UPDATE GLOBAL STATUS CACHE
    _update_global_status(
        job_id=job_id,
        status=status,
        step=step,
        progress=progress,
        message=message,
        project_id=project_id,
        gallery_id=gallery_id,
        job_type=render_type.lower(),
        render_size=_extract_job_render_size(active_jobs.get(job_id, {}))
    )
    


    # 3. Define the network work to be done in background
    def _network_worker():
        try:
            # Map string status to numeric status code
            status_code_map = {
                "processing": 1, "in_progress": 1, "queued": 1,
                "completed": 2, "success": 2,
                "rejected": 3,
                "failed": 4, "error": 4
            }
            status_code = status_code_map.get(status.lower(), 1)
            
            status_names = {1: "InProgress", 2: "Completed", 3: "Rejected", 4: "Error"}
            status_name = status_names.get(status_code, f"Unknown({status_code})")
            
            # Prepare payload
            status_payload_form = {
                "UserId": str(user_id),
                "ProjectId": str(project_id),
                "Id": str(gallery_id),
                "RenderType": render_type.upper(),
                "StatusMessage": message,
                "Status": status_code,
                "JobId": job_id,
                "StatusText": status,
                "Step": step,
                "Progress": str(progress),
                "Timestamp": datetime.now().isoformat()
            }
            
            payload_details = dict(details or {})
            for key, value in _extract_job_render_size(active_jobs.get(job_id, {})).items():
                payload_details.setdefault(key, value)
            if blob_url:
                url_field_name = "VideoUrl" if render_type == "VIDEO" else "ImageUrl"
                status_payload_form[url_field_name] = blob_url
                payload_details.setdefault("image_url", blob_url)
                payload_details.setdefault("output_url", blob_url)
                payload_details.setdefault("blob_url", blob_url)
            if thumbnail_url:
                status_payload_form["ThumbnailUrl"] = thumbnail_url
                payload_details.setdefault("thumbnail_url", thumbnail_url)

            if payload_details:
                status_payload_form["Details"] = json.dumps(payload_details)
                
            # Determine endpoint
            upload_url = api_endpoint if api_endpoint else (VIDEO_UPLOAD_API_URL if render_type == "VIDEO" else UPLOAD_API_URL)
            
            print(f"\n{COLOR_BLUE}📡 [Background] Sending Status: {status_name} ({progress}%) - {message}{COLOR_RESET}")
            # DEBUG: Print payload to verify ImageUrl/VideoUrl/ThumbnailUrl
            print(f"   Payload: {json.dumps(status_payload_form, default=str)}")
            
            request_start_time = time.monotonic()

            # Add API Key headers if enabled
            headers = {}
            if USE_API_KEY and API_KEY:
                headers["ZRealtyServiceApiKey"] = API_KEY
                # Also try adding it as a query param just in case, or stick to header?
                # Sticking to header simply.

            # Always notify the local monitor first so the queue/UI can complete
            # even if the public callback endpoint is slow or unavailable.
            # If the monitor accepts the update, it becomes the single sender to
            # the public API. That avoids duplicate or out-of-order status writes
            # to UploadProjectGalleryAI / UploadProjectVideoAI.
            files = {'UploadImage': (None, '')}
            monitor_sent = False
            if MONITORING_API_URL:
                try:
                    print(f"   📡 Broadcasting to Monitor: {MONITORING_API_URL}")
                    monitor_response = None
                    for attempt in range(1, 4):
                        try:
                            monitor_response = requests.post(
                                MONITORING_API_URL,
                                data=status_payload_form,
                                files=files,
                                headers=headers,
                                verify=False,
                                timeout=10,
                            )
                            print(f"   📡 Monitor broadcast response: {monitor_response.status_code} (attempt {attempt}/3)")
                            if monitor_response.status_code in [200, 201, 202]:
                                monitor_sent = True
                                break
                        except Exception as monitor_exc:
                            print(f"   [WARN] Monitor broadcast attempt {attempt}/3 failed: {monitor_exc}")
                            time.sleep(1.5 * attempt)
                except Exception as mon_e:
                    print(f"   [WARN] Monitoring broadcast failed: {mon_e}")

            if monitor_sent:
                request_duration = time.monotonic() - request_start_time
                if logger:
                    logger.add_status_update(step, f"{status_name}({status_code})", request_duration, 200)
                print(f"{COLOR_GREEN}   ✓ Status accepted by monitor; skipping direct callback to avoid duplicate writes ({request_duration:.2f}s){COLOR_RESET}")
                return

            # Send request to Primary API
            response = None
            primary_error = None
            try:
                response = requests.post(upload_url, data=status_payload_form, files=files, headers=headers, verify=False, timeout=60)
            except Exception as upload_exc:
                primary_error = upload_exc

            request_duration = time.monotonic() - request_start_time

            if logger and response is not None:
                logger.add_status_update(step, f"{status_name}({status_code})", request_duration, response.status_code)

            if response is not None and response.status_code in [200, 201, 202]:
                print(f"{COLOR_GREEN}   ✓ Status sent successfully ({request_duration:.2f}s){COLOR_RESET}")
            elif response is not None:
                # Try to parse JSON error message
                error_detail = response.text[:200]
                try:
                    error_json = response.json()
                    if "message" in error_json:
                        error_detail = error_json["message"]
                    elif "Message" in error_json:
                        error_detail = error_json["Message"]
                except:
                    pass
                print(f"{COLOR_YELLOW}   [WARN] Status API Error ({response.status_code}): {error_detail} ({request_duration:.2f}s){COLOR_RESET}")
            elif primary_error is not None:
                print(f"{COLOR_YELLOW}   [WARN] Status API Request Failed: {primary_error} ({request_duration:.2f}s){COLOR_RESET}")
                
        except Exception as e:
            print(f"{COLOR_YELLOW}   [WARN] Background status update failed: {e}{COLOR_RESET}")

    if wait_for_delivery:
        _network_worker()
        return True

    # 4. Start background thread
    try:
        t = Thread(target=_network_worker, daemon=True)
        t.start()
        return True
    except Exception as e:
        print(f"Error starting status thread: {e}")
        return False

def _upload_render_to_api(project_id: int, gallery_id: int, file_path: str, user_id: int = 0, logger: Optional[RenderLogger] = None, job_id: str = ""):
    """
    Step 6: Finalizes the render job by uploading the binary and sending the success message.
    """
    try:
        render_type = "VIDEO" if file_path.lower().endswith('.mp4') else "IMAGE"
        print(f"\n{'='*60}\n📤 STEP 6: FINAL UPLOAD TO API ({render_type})\n{'='*60}")
        
        # Step 1: Upload to blob storage for permanent record
        file_ext = os.path.splitext(file_path)[1].lower()
        blob_container = "render-videos" if file_ext == '.mp4' else "render-images"
        blob_url = _upload_to_blob_storage(file_path, blob_container=blob_container)
        thumbnail_url = None
        thumbnail_path = _get_thumbnail_path(file_path)
        if os.path.exists(thumbnail_path):
            print(f"{COLOR_BLUE}🖼️ Uploading thumbnail: {thumbnail_path}{COLOR_RESET}")
            thumbnail_url = _upload_to_blob_storage(thumbnail_path, blob_container=blob_container)
            if not thumbnail_url:
                print(f"{COLOR_YELLOW}[WARN] Thumbnail upload failed, continuing with the main render only.{COLOR_RESET}")
        else:
            print(f"{COLOR_YELLOW}[WARN] Thumbnail not found at: {thumbnail_path}{COLOR_RESET}")
        
        # Step 2: Send blob URL + status to main API (NO binary file)
        if not blob_url:
            print(f"{COLOR_RED}❌ Upload to blob storage failed. Cannot complete job.{COLOR_RESET}")
            _send_status_update(
                project_id=project_id,
                gallery_id=gallery_id,
                user_id=user_id,
                job_id=job_id,
                status="error",
                step="upload_binary",
                progress=0,
                message="Something went wrong. Please try again.",
                render_type=render_type,
                logger=logger,
                wait_for_delivery=True
            )
            return False

        # We use _send_status_update which sends the main blob URL and thumbnail URL
        
        # --- NEW: Read Asset Report to include log details in final JSON ---
        final_details = {}
        try:
            asset_report_path = os.path.join(GLB_OUTPUT_DIR, f"{job_id}_asset_report.json")
            if os.path.exists(asset_report_path):
                with open(asset_report_path, 'r', encoding='utf-8') as f:
                    asset_report = json.load(f)
                final_details['asset_processing_report'] = asset_report
                print(f"📄 Included asset report with {len(asset_report)} items.")
        except Exception as e:
            print(f"[WARN] Could not include asset report: {e}")

        if thumbnail_url:
            final_details["thumbnail_url"] = thumbnail_url

        file_size_info = _get_file_size_info(file_path)
        if file_size_info:
            final_details.update(file_size_info)
            if job_id and job_id in active_jobs:
                active_jobs[job_id].update(file_size_info)

        success = _send_status_update(
            project_id=project_id,
            gallery_id=gallery_id,
            user_id=user_id,
            job_id=job_id,
            status="completed",
            step="completed",
            progress=100,
            message="Your render is ready!" if render_type == "IMAGE" else "Your video is ready!",
            render_type=render_type,
            blob_url=blob_url,  # Send blob URL in ImageUrl field
            thumbnail_url=thumbnail_url,
            logger=logger,
            details=final_details,
            wait_for_delivery=True
        )
        
        

                
        if success:
            print(f"{COLOR_GREEN}[OK] Final upload and status update successful!{COLOR_RESET}")
            if blob_url:
                print(f"   Permanent Blob Storage URL: {blob_url}")
            return True
        else:
            print(f"{COLOR_YELLOW}[WARN]  Status update failed but blob upload succeeded{COLOR_RESET}")
            if blob_url:
                print(f"   Permanent Blob Storage URL: {blob_url}")
            return False
            
    except Exception as e:
        print(f"{COLOR_RED}❌ ERROR in final upload: {str(e)}{COLOR_RESET}")
        import traceback
        traceback.print_exc()
        return False

# --- BACKGROUND RENDER TASK ---

def _background_render_task(project_id: int, gallery_id: int, payload: GenerationPayload, job_id: str, user_id: int = 0, suppress_error_status: bool = False):
    """The main function that runs in the background to perform the full render and upload pipeline."""
    logger = RenderLogger(job_id)
    logger.start_process()
    logger.set_render_type("IMAGE")
    logger.set_user_details(project_id, gallery_id, user_id)
    logger.set_render_quality(payload.render_quality) 
    
    output_image_path = None
    enable_status_updates = getattr(payload, 'enable_status_updates', False)
    
    try:
        start_time = time.monotonic()
        print(f"\n{'='*80}\n🚀 BACKGROUND RENDER TASK STARTED (GODOT)\n{'='*80}\nJob ID: {job_id}\nProject ID: {project_id}\nGallery ID: {gallery_id}\nUser ID: {user_id}\nStatus Updates: {'ENABLED' if enable_status_updates else 'DISABLED'}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*80}\n")
        
        # Step 1/5: Preparing your scene
        if enable_status_updates:
            _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "preparing", 10, "Preparing your scene...", render_type="IMAGE", logger=logger)
        
        print(f"🔍 STEP 1/5: Validating input payload...\n{'─'*60}")
        is_valid, validation_error = _validate_image_payload(payload)
        if not is_valid:
            error_msg = f"Invalid payload: {validation_error}"
            print(f"{COLOR_RED}❌ VALIDATION FAILED: {error_msg}{COLOR_RESET}")
            if enable_status_updates:
                _send_status_update(
                    project_id, gallery_id, user_id, job_id, 
                    "rejected", "validation", 0, 
                    "Something went wrong. Please try again.", 
                    render_type="IMAGE", logger=logger
                )
            logger.add_interrupt("validation_failed", error_msg)
            logger.end_process(success=False, error=error_msg)
            return
        print(f"[OK] Payload validation passed\n")
        
        # Step 2/5: Setting up your space
        if enable_status_updates:
            _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "loading", 30, "Setting up your space...", render_type="IMAGE", logger=logger)
        
        # Step 3/5: Rendering your design
        if enable_status_updates:
            _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "rendering", 50, "Rendering your design...", render_type="IMAGE", logger=logger)
            
        print(f"🎬 STEP 3/5: Running Render...\n{'─'*60}")
        
        output_image_path = _run_godot_render(job_id, payload, logger=logger)
        
        if not output_image_path or not os.path.exists(output_image_path):
            raise Exception("Godot render failed to produce output")
            
        print(f"[OK] Render completed successfully: {output_image_path}\n")

        # Step 4/5: Adding final touches
        if enable_status_updates:
             _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "finalizing", 80, "Adding final touches...", render_type="IMAGE", logger=logger, wait_for_delivery=True)
        
        print(f"📤 STEP 4/5: Uploading to external API...\n{'─'*60}")
             
        upload_success = _upload_render_to_api(project_id, gallery_id, output_image_path, user_id, logger=logger, job_id=job_id)
        
        end_time = time.monotonic()
        if upload_success:
            print(f"\n{'='*80}\n🎉 ALL STEPS COMPLETED SUCCESSFULLY! (Total Time: {(end_time - start_time):.2f}s)\n{'='*80}")
            logger.end_process(success=True)
        else:
            print(f"\n{'='*80}\n[WARN] RENDER COMPLETED BUT UPLOAD FAILED (Total Time: {(end_time - start_time):.2f}s)\n{'='*80}")
            logger.end_process(success=True, error="Upload failed")
            
    except GPUResourceError as e:
        # GPU ran out of VRAM / driver reset. The crashed (black) frame has
        # already been deleted upstream - report the real reason instead of
        # uploading a broken image.
        error_msg = str(e)
        print(f"\n{'='*80}\n{COLOR_RED}[GPU] GPU CAPACITY ERROR IN BACKGROUND TASK{COLOR_RESET}\n{'='*80}\nJob ID: {job_id}\nError: {error_msg}\n{'='*80}\n")
        # Always notify: a GPU fault is never retried, so this is the only
        # status update the customer will get for this job.
        if enable_status_updates:
            _send_status_update(
                project_id, gallery_id, user_id, job_id,
                "error", "error", 0,
                GPU_STATUS_MESSAGE,
                render_type="IMAGE", logger=logger
            )
        logger.add_interrupt("gpu_error", error_msg)
        logger.end_process(success=False, error=error_msg)
        raise  # Propagate so the retry loop / queue worker sees the failure

    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        print(f"\n{'='*80}\n{COLOR_RED}❌ ERROR IN BACKGROUND TASK{COLOR_RESET}\n{'='*80}\nJob ID: {job_id}\nError: {error_msg}\n{'='*80}\n")
        import traceback
        traceback.print_exc()
        
        if suppress_error_status:
            # Re-raise so the retry loop can catch it and retry
            logger.add_interrupt("error", error_msg)
            logger.end_process(success=False, error=error_msg)
            raise
        
        if enable_status_updates:
            _send_status_update(project_id, gallery_id, user_id, job_id, "error", "error", 0, "Scene is too large to render. Please try a different setup.", render_type="IMAGE", logger=logger)
        logger.add_interrupt("error", error_msg)
        logger.end_process(success=False, error=error_msg)
        raise  # Always propagate so retry loop detects the failure
    finally:
        pass

def _run_godot_video_render(job_id: str, payload: VideoGenerationPayload, logger: Optional[RenderLogger] = None, include_audio: bool = True) -> str:
    """
    Runs the Godot video rendering process using MovieWriter (--write-movie) for fast rendering.
    Falls back to PNG frame-by-frame if MovieWriter is not available.
    """
    # 1. Localize assets (same pattern as image render)
    if logger: logger.start_glb_creation()
    
    print(f"🎬 Starting Godot Video Render for Job {job_id}")
    
    localized_geometry = None
    if payload.floor_plan_data:
        # Parse floor_plan_data string → dict if needed
        floor_plan_data = payload.floor_plan_data
        scene_data = json.loads(floor_plan_data) if isinstance(floor_plan_data, str) else floor_plan_data
        # Download and localize all asset paths to absolute local paths
        localized_geometry = _download_and_localize_assets(scene_data, use_high_res=True, logger=logger)
        localized_geometry = _localize_ies_profiles(localized_geometry, logger=logger)
        print(f"--- Finalizing floor plan data with all local asset paths ---")
    
    if logger: logger.end_glb_creation()

    # 2. Build Godot input JSON with localized paths
    # Exclude floor_plan_data since we replace it with localized version
    godot_input = payload.dict(exclude={'floor_plan_data'}, exclude_unset=False)
    if localized_geometry:
        godot_input['floor_plan_data'] = localized_geometry
    elif payload.floor_plan_data:
        # If localization failed, try to use raw data
        if isinstance(payload.floor_plan_data, str):
            try:
                godot_input['floor_plan_data'] = json.loads(payload.floor_plan_data)
            except:
                godot_input['floor_plan_data'] = payload.floor_plan_data
        else:
            godot_input['floor_plan_data'] = payload.floor_plan_data

    if payload.environment_url:
        res = _download_asset_with_fallback(payload.environment_url, use_high_poly=True)
        if res and res[1]:
            godot_input['environment_url'] = res[1]

    # Remove None values to prevent GDScript null issues (has() returns true but value is null)
    godot_input = {k: v for k, v in godot_input.items() if v is not None}
    
    # 2.5 Video Scene Culling — remove rooms/items not visible along camera path
    output_basename = f"{job_id}_render"
    culling_log_path = os.path.join(CULLING_LOGS_DIR, f"{output_basename}_culling.txt")
    if ENABLE_VIDEO_CULLING:
        try:
            video_optimizer = VideoSceneOptimizer(log_path=culling_log_path)
            godot_input = video_optimizer.cull_scene(godot_input)
            print(f"[OK] Video scene culling complete. Log: {culling_log_path}")
        except Exception as e:
            print(f"[WARN] Video scene culling failed (continuing without culling): {e}")
    else:
        print("ℹ️  Video scene culling disabled by main.py flag; rendering full scene.")

    # 2.54 Per-job LLM switch — identical to the image path in
    # _run_godot_render, and identical on purpose: a still and a walkthrough of
    # the same scene must obey the same switch. The request's input JSON may
    # carry "USE_LLM": true/false; .env wins, and the JSON can only turn the
    # passes off for this job.
    _llm_gate_token = None
    try:
        from zlendo_llm.config import apply_job_llm_flag
        _llm_gate_token = apply_job_llm_flag(godot_input, job_id=job_id)
    except Exception as e:
        print(f"[WARN] Could not read USE_LLM from input JSON (using .env): {e}")

    # 2.55 Automation: colour-aware lighting adjustment.
    # Mirrors the image path in _run_godot_render. Same reason for sitting here:
    # the pass averages surface colours of the rooms that survive culling, so it
    # must run after the culling block above and before the JSON is written.
    # Note this runs even when ENABLE_VIDEO_CULLING is off — it then analyses the
    # full scene, which is correct, just less targeted.
    # Gated by AUTOMATION_LLM_ADJUSTMENT in .env.
    try:
        from zlendo_llm.automation.integration import auto_adjust_lighting
        godot_input = auto_adjust_lighting(godot_input, job_id=job_id, logger=logger)
    except Exception as e:
        print(f"[WARN] Automation lighting adjustment unavailable (continuing): {e}")

    # 2.56 Automation: per-fixture emission control. Sets light_meta.intensity
    # and light_meta.temperature on every light asset in the scene.
    #
    # MUST run AFTER the colour pass above, which is what sets colorMood -
    # tuning each lamp to a mood that is about to change underneath it is
    # wasted work. The two passes answer different questions: the one above
    # decides how bright the ROOM is, this one decides what each light ASSET
    # emits. A scene can have a perfect illuminationLevel and still look
    # wrong because its six lamps span 2000K-7500K, or because the ceiling
    # pendant is dimmer than the floor lamps. No global control reaches
    # either of those; only per-fixture values do.
    #
    # Writes light_meta back into floor_plan_data (preserving whether that
    # field arrived as a string or a dict) plus _automationEmissionAdjustment
    # for audit. Gated by AUTOMATION_LLM_EMISSION in .env.
    try:
        from zlendo_llm.automation.integration import auto_adjust_emission
        godot_input = auto_adjust_emission(godot_input, job_id=job_id, logger=logger)
    except Exception as e:
        print(f"[WARN] Automation emission adjustment unavailable (continuing): {e}")

    # 2.6 AI Lighting Upgrade — local Qwen reviews the scene and fixes the
    # lighting before the path tracer runs. Gated by USE_LLM in .env.
    # Keeping this identical to the image path matters: a still and a walkthrough
    # of the same room get compared side by side, so they must receive the same
    # lighting decisions.
    try:
        from zlendo_llm.integration import ai_upgrade_lighting
        godot_input = ai_upgrade_lighting(godot_input, job_id=job_id, logger=logger)
    except Exception as e:
        print(f"[WARN] AI lighting upgrade unavailable (continuing): {e}")

    # 2.65 The per-job LLM switch applies only to the block above; drop it so
    # nothing later in this process inherits one job's choice.
    if _llm_gate_token is not None:
        try:
            from zlendo_llm.config import reset_job_llm_override
            reset_job_llm_override(_llm_gate_token)
        except Exception:
            pass

    # Both passes above mutate godot_input, so they MUST stay above this line —
    # the JSON below is what Godot actually reads.
    # Save the properly assembled input JSON
    input_json_path = os.path.join(RUNTIME_DIR, f"{job_id}_video_input.json")
    try:
        with open(input_json_path, 'w', encoding='utf-8') as f:
            json.dump(godot_input, f, ensure_ascii=False, default=str)
        print(f"📄 Saved video input JSON to: {input_json_path}")
    except Exception as e:
        print(f"❌ Failed to save video input JSON: {e}")
        raise e

    # 3. Prepare Output Directory
    output_dir = os.path.abspath(os.path.join(RENDER_OUTPUT_DIR, job_id))
    os.makedirs(output_dir, exist_ok=True)
    
    # Output paths
    # MovieWriter saves AVI relative to the Godot project directory
    # Use global paths configured at start
    godot_exe = GODOT_EXE
    project_path = GODOT_PROJECT_PATH
    
    avi_filename = f"{job_id}_render.avi"
    avi_godot_output = os.path.join(project_path, avi_filename)  # Where Godot will save it
    output_video_path = os.path.abspath(os.path.join(RENDER_OUTPUT_DIR, f"{job_id}_render.mp4"))
    temp_img_base = os.path.join(output_dir, "frame.png")  # PNG fallback
    
    fps = payload.video_animation.fps if payload.video_animation else 30
    
    # 4. Run Godot with MovieWriter
    # NOTE: Do NOT use --headless for renders; headless disables rendering in Godot 4.x.
    cmd = [godot_exe]
    cmd.extend([
        "--path", project_path,
        "--write-movie", avi_filename,  # Simple filename - Godot saves in project dir
        "--fixed-fps", str(fps),
        "res://video_main.tscn",
        "--",
        input_json_path,
        output_video_path,
        temp_img_base  # passed so GDScript knows where to save PNGs if MovieWriter fails
    ])
    
    print(f"🚀 Running Godot MovieWriter Command: {' '.join(cmd)}")
    
    timeout = int(os.getenv("VIDEO_RENDER_TIMEOUT_SECONDS", "1800"))
    
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        all_output = []
        # Holds the first GPU/driver failure line seen on the console, if any.
        gpu_abort: list = []

        def _stream_video_output():
            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\r\n")
                    all_output.append(line)
                    clean_line = line.strip()
                    if clean_line:
                        print(f"[GodotVideo] {clean_line}", flush=True)
                    # FAIL FAST: kill Godot on the first GPU/driver error line
                    # instead of rendering the rest of the frames into a black
                    # video and waiting for the crash.
                    if not gpu_abort:
                        marker = _detect_gpu_failure((line,))
                        if marker:
                            gpu_abort.append(marker)
                            _abort_render_process(proc, marker, "GodotVideo")
            except Exception as stream_exc:
                print(f"Warning: Godot stream reader stopped early: {stream_exc}")

        reader = Thread(target=_stream_video_output, daemon=True)
        reader.start()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            # If the GPU already failed, report that - not a timeout.
            if gpu_abort:
                _purge_crashed_outputs(avi_godot_output, output_video_path)
                _raise_gpu_failure(gpu_abort[0])
            raise

        full_stdout = "\n".join(all_output)

        # GPU / VRAM failure check - before any AVI or PNG fallback is trusted.
        # A crashed GPU can leave a partial AVI or a folder of black frames.
        gpu_marker = gpu_abort[0] if gpu_abort else _detect_gpu_failure(all_output)
        if gpu_marker:
            _purge_crashed_outputs(avi_godot_output, output_video_path)
            try:
                import glob as _glob
                _purge_crashed_outputs(*_glob.glob(os.path.join(output_dir, "frame_*.png")))
            except Exception:
                pass
            _raise_gpu_failure(gpu_marker)
        
        # Check if MovieWriter actually produced the AVI file
        if not os.path.exists(avi_godot_output) or os.path.getsize(avi_godot_output) == 0:
            # Only fail if no AVI was produced AND no meaningful output
            if "VIDEO RENDER COMPLETE" not in full_stdout:
                print(f"❌ Godot failed to produce video (exit code {proc.returncode})")
                raise Exception(f"Godot render failed with exit code {proc.returncode}")
    except subprocess.TimeoutExpired:
        print(f"❌ Godot execution timed out after {timeout}s")
        raise Exception("Godot render timed out")

    # 5. Check what was produced and convert to MP4
    if os.path.exists(avi_godot_output) and os.path.getsize(avi_godot_output) > 0:
        # MovieWriter produced an AVI — convert to MP4
        avi_size_mb = os.path.getsize(avi_godot_output) / (1024 * 1024)
        print(f"[OK] MovieWriter AVI created: {avi_godot_output} ({avi_size_mb:.1f} MB)")
        print(f"🎞️ Converting AVI to MP4...")
        
        ffmpeg_path = _find_ffmpeg_path()
        if not ffmpeg_path:
            print("❌ FFmpeg not found! Cannot convert AVI to MP4.")
            raise Exception("FFmpeg not found")
        
        # Find audio file
        script_dir = os.path.dirname(os.path.abspath(__file__))
        audio_path = os.path.join(script_dir, "audio", "audio.mp3")
        has_audio = include_audio and os.path.exists(audio_path)
        
        # Build FFmpeg command: AVI → MP4
        # MovieWriter AVI includes a silent audio track — we must explicitly ignore it
        ffmpeg_cmd = [
            ffmpeg_path,
            '-y',
            '-i', avi_godot_output,
        ]
        
        if has_audio:
            ffmpeg_cmd.extend(['-i', audio_path])
            ffmpeg_cmd.extend([
                '-map', '0:v',     # Video from AVI (input 0)
                '-map', '1:a',     # Audio from external MP3 (input 1)
                '-c:v', 'libx264',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-shortest',
            ])
            print(f"🎵 Adding audio: {audio_path}")
        else:
            ffmpeg_cmd.extend([
                '-map', '0:v',     # Video only, no audio
                '-c:v', 'libx264',
                '-an',             # Explicitly no audio
            ])
        
        ffmpeg_cmd.extend([
            '-pix_fmt', 'yuv420p',
            '-crf', '18',
            '-preset', 'medium',
            output_video_path
        ])
        
        print(f"📹 FFmpeg command: {' '.join(ffmpeg_cmd)}")
        
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=600)
        
        if result.returncode != 0:
            print(f"❌ FFmpeg conversion failed: {result.stderr[:500] if result.stderr else 'No error output'}")
            raise Exception("FFmpeg AVI to MP4 conversion failed")
        
        # Clean up AVI
        try:
            os.remove(avi_godot_output)
            print(f"🧹 Cleaned up AVI file")
        except:
            pass
        
        if os.path.exists(output_video_path) and os.path.getsize(output_video_path) > 0:
            mp4_size_mb = os.path.getsize(output_video_path) / (1024 * 1024)
            print(f"[OK] Video output saved: {output_video_path} ({mp4_size_mb:.1f} MB)")
            # Clean up the temp frames folder (no longer needed after MP4 is saved)
            try:
                import shutil
                shutil.rmtree(output_dir, ignore_errors=True)
                print(f"🧹 Cleaned up temp folder: {output_dir}")
            except Exception:
                pass
            return output_video_path
        else:
            raise Exception("MP4 output file was not created")
    else:
        # Fallback: check for PNG sequence (MovieWriter might not have been available)
        print("[WARN]  MovieWriter AVI not found — checking for PNG fallback frames...")
        
        print(f"🎞️ Encoding PNG sequence to MP4 (FPS: {fps})...")
        success = _encode_png_sequence_to_mp4(output_dir, "frame", output_video_path, fps=fps, delete_frames=True)
        
        if success:
            print(f"[OK] Video output saved: {output_video_path}")
            # Clean up the temp frames folder (PNG frames already deleted by encoder,
            # but the folder itself still remains — remove it)
            try:
                import shutil
                shutil.rmtree(output_dir, ignore_errors=True)
                print(f"🧹 Cleaned up temp folder: {output_dir}")
            except Exception:
                pass
            return output_video_path
        else:
            raise Exception("Video encoding failed — no AVI or PNG frames produced")

def _vector3_to_plain_dict(value: Any) -> Optional[Dict[str, float]]:
    """Normalize a Vector3-like value into a plain {x, y, z} dictionary."""
    if value is None:
        return None

    if isinstance(value, dict):
        x = value.get("x")
        y = value.get("y")
        z = value.get("z")
    elif hasattr(value, "dict"):
        return _vector3_to_plain_dict(value.dict())
    elif hasattr(value, "x") and hasattr(value, "y") and hasattr(value, "z"):
        x = getattr(value, "x")
        y = getattr(value, "y")
        z = getattr(value, "z")
    elif isinstance(value, (list, tuple)) and len(value) >= 3:
        x, y, z = value[0], value[1], value[2]
    else:
        return None

    if x is None or y is None or z is None:
        return None

    return {"x": float(x), "y": float(y), "z": float(z)}

def _lerp_plain_vector3(start_value: Any, end_value: Any, t: float) -> Optional[Dict[str, float]]:
    """Linearly interpolate between two Vector3-like values and return a plain dict."""
    start_dict = _vector3_to_plain_dict(start_value)
    end_dict = _vector3_to_plain_dict(end_value)
    if not start_dict or not end_dict:
        return None

    return {
        "x": float(start_dict["x"] + (end_dict["x"] - start_dict["x"]) * t),
        "y": float(start_dict["y"] + (end_dict["y"] - start_dict["y"]) * t),
        "z": float(start_dict["z"] + (end_dict["z"] - start_dict["z"]) * t),
    }

def _split_video_animation_into_chunks(video_animation: VideoAnimation, max_chunk_seconds: float) -> List[VideoAnimation]:
    """
    Split a video animation into consecutive chunks of at most max_chunk_seconds.

    For frame-by-frame animations, slices the keyframe list directly.
    For start/end animations, creates smaller start/end spans for each chunk.
    """
    if not video_animation:
        return []

    duration_seconds = float(getattr(video_animation, "duration_seconds", 0.0) or 0.0)
    fps = int(getattr(video_animation, "fps", 30) or 30)
    fps = max(1, fps)

    if duration_seconds <= max_chunk_seconds:
        return [video_animation]

    keyframes = list(video_animation.keyframes or [])

    if keyframes:
        chunks: List[VideoAnimation] = []
        total_frames = len(keyframes)
        chunk_count = max(1, int((duration_seconds - 1e-9) / max_chunk_seconds) + 1)
        for chunk_index in range(chunk_count):
            chunk_start_seconds = chunk_index * max_chunk_seconds
            chunk_end_seconds = min((chunk_index + 1) * max_chunk_seconds, duration_seconds)
            start_ratio = chunk_start_seconds / duration_seconds if duration_seconds > 0 else 0.0
            end_ratio = chunk_end_seconds / duration_seconds if duration_seconds > 0 else 1.0
            start_idx = int(round(start_ratio * (total_frames - 1))) if total_frames > 1 else 0
            end_idx = int(round(end_ratio * (total_frames - 1))) if total_frames > 1 else total_frames
            if chunk_index == chunk_count - 1:
                end_idx = total_frames
            else:
                end_idx = max(start_idx + 1, min(end_idx, total_frames))
            chunk = video_animation.copy(deep=True)
            chunk.keyframes = []
            for frame_offset, frame_data in enumerate(keyframes[start_idx:end_idx]):
                frame_copy = copy.deepcopy(frame_data)
                if isinstance(frame_copy, dict) and "frame" in frame_copy:
                    frame_copy["frame"] = frame_offset
                chunk.keyframes.append(frame_copy)
            chunk.fps = fps
            chunk.duration_seconds = max(float(len(chunk.keyframes)) / float(fps), 1.0 / float(fps))
            chunks.append(chunk)
        return chunks

    has_start_end = (
        video_animation.camera_position_start is not None and
        video_animation.camera_position_end is not None
    )
    if not has_start_end:
        return [video_animation]

    chunks = []
    chunk_count = max(1, int((duration_seconds - 1e-9) / max_chunk_seconds) + 1)

    for chunk_index in range(chunk_count):
        chunk_start_seconds = chunk_index * max_chunk_seconds
        chunk_end_seconds = min((chunk_index + 1) * max_chunk_seconds, duration_seconds)
        start_t = chunk_start_seconds / duration_seconds if duration_seconds > 0 else 0.0
        end_t = chunk_end_seconds / duration_seconds if duration_seconds > 0 else 1.0
        segment = video_animation.copy(deep=True)
        segment.fps = fps
        segment.duration_seconds = max(float(chunk_end_seconds - chunk_start_seconds), 1.0 / float(fps))

        if video_animation.camera_position_start is not None and video_animation.camera_position_end is not None:
            segment.camera_position_start = _lerp_plain_vector3(
                video_animation.camera_position_start,
                video_animation.camera_position_end,
                start_t,
            )
            segment.camera_position_end = _lerp_plain_vector3(
                video_animation.camera_position_start,
                video_animation.camera_position_end,
                end_t,
            )

        if video_animation.camera_target_start is not None and video_animation.camera_target_end is not None:
            segment.camera_target_start = _lerp_plain_vector3(
                video_animation.camera_target_start,
                video_animation.camera_target_end,
                start_t,
            )
            segment.camera_target_end = _lerp_plain_vector3(
                video_animation.camera_target_start,
                video_animation.camera_target_end,
                end_t,
            )

        segment.keyframes = None
        chunks.append(segment)

    return chunks

def _escape_ffmpeg_concat_path(path: str) -> str:
    """Escape a file path for use in ffmpeg concat lists."""
    return path.replace("\\", "/").replace("'", "'\\''")

def _merge_video_segments_with_ffmpeg(segment_paths: List[str], output_video_path: str) -> str:
    """Merge chunked MP4 segments into a final MP4, then mux audio once if available."""
    if not segment_paths:
        raise Exception("No video segments were produced")

    ffmpeg_path = _find_ffmpeg_path()
    if not ffmpeg_path:
        raise Exception("FFmpeg not found")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    audio_path = os.path.join(script_dir, "audio", "audio.mp3")
    has_audio = os.path.exists(audio_path)

    import shutil

    with tempfile.TemporaryDirectory(prefix="video_chunk_merge_") as temp_dir:
        concat_list_path = os.path.join(temp_dir, "concat_list.txt")
        merged_video_path = os.path.join(temp_dir, "merged_video.mp4")

        with open(concat_list_path, "w", encoding="utf-8") as f:
            for segment_path in segment_paths:
                f.write(f"file '{_escape_ffmpeg_concat_path(os.path.abspath(segment_path))}'\n")

        concat_cmd = [
            ffmpeg_path,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            merged_video_path,
        ]

        print(f"🎞️ Concatenating {len(segment_paths)} video segments...")
        print(f"📼 FFmpeg concat command: {' '.join(concat_cmd)}")
        concat_result = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=900)
        if concat_result.returncode != 0:
            raise Exception(
                f"FFmpeg concat failed: {concat_result.stderr[:1000] if concat_result.stderr else 'No error output'}"
            )

        final_output = os.path.abspath(output_video_path)
        os.makedirs(os.path.dirname(final_output), exist_ok=True)
        if os.path.exists(final_output):
            try:
                os.remove(final_output)
            except Exception:
                pass

        if has_audio:
            muxed_output_path = os.path.join(temp_dir, "merged_with_audio.mp4")
            mux_cmd = [
                ffmpeg_path,
                "-y",
                "-i", merged_video_path,
                "-i", audio_path,
                "-map", "0:v",
                "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                muxed_output_path,
            ]
            print(f"🎵 Adding audio to merged video: {audio_path}")
            print(f"📼 FFmpeg mux command: {' '.join(mux_cmd)}")
            mux_result = subprocess.run(mux_cmd, capture_output=True, text=True, timeout=900)
            if mux_result.returncode != 0:
                raise Exception(
                    f"FFmpeg audio mux failed: {mux_result.stderr[:1000] if mux_result.stderr else 'No error output'}"
                )
            shutil.move(muxed_output_path, final_output)
        else:
            shutil.move(merged_video_path, final_output)

        return final_output

def _run_chunked_godot_video_render(job_id: str, payload: VideoGenerationPayload, logger: Optional[RenderLogger] = None) -> str:
    """Render long videos in chunks and stitch the results together with ffmpeg."""
    video_animation = payload.video_animation
    if not video_animation:
        return _run_godot_video_render(job_id, payload, logger=logger)

    duration_seconds = float(getattr(video_animation, "duration_seconds", 0.0) or 0.0)
    if duration_seconds <= VIDEO_RENDER_MAX_CHUNK_SECONDS:
        return _run_godot_video_render(job_id, payload, logger=logger)

    chunk_animations = _split_video_animation_into_chunks(video_animation, VIDEO_RENDER_MAX_CHUNK_SECONDS)
    if len(chunk_animations) <= 1:
        return _run_godot_video_render(job_id, payload, logger=logger)

    print(f"🎬 Chunking long video render into {len(chunk_animations)} segments (max {VIDEO_RENDER_MAX_CHUNK_SECONDS:.0f}s each)")

    chunk_plan_log_path = os.path.join(CULLING_LOGS_DIR, f"{job_id}_render_culling.txt")
    try:
        with open(chunk_plan_log_path, "w", encoding="utf-8") as f:
            f.write(f"{'='*60}\n")
            f.write("VIDEO CHUNK PLAN\n")
            f.write(f"{'='*60}\n")
            f.write(f"Job ID          : {job_id}\n")
            f.write(f"Total duration   : {duration_seconds:.3f} sec\n")
            f.write(f"Chunk threshold  : {VIDEO_RENDER_MAX_CHUNK_SECONDS:.3f} sec\n")
            f.write(f"Chunk count      : {len(chunk_animations)}\n")
            f.write(f"\n{'-'*60}\n")
            elapsed_preview = 0.0
            for idx, preview_anim in enumerate(chunk_animations, start=1):
                preview_duration = float(preview_anim.duration_seconds or 0.0)
                preview_end = elapsed_preview + preview_duration
                preview_frames = len(preview_anim.keyframes or [])
                if preview_frames <= 0:
                    preview_frames = max(1, int(round(preview_duration * float(preview_anim.fps or 30))))
                if idx == 1:
                    preview_label = "first 10 sec"
                elif idx == 2 and len(chunk_animations) >= 2:
                    preview_label = "next 10 sec"
                elif idx == len(chunk_animations) and preview_duration < VIDEO_RENDER_MAX_CHUNK_SECONDS:
                    preview_label = f"final {preview_duration:.3f} sec"
                else:
                    preview_label = f"segment {idx}"
                f.write(
                    f"Chunk {idx}/{len(chunk_animations)} - {preview_label}: "
                    f"{elapsed_preview:.3f}s -> {preview_end:.3f}s "
                    f"({preview_duration:.3f}s, {preview_frames} frames)\n"
                )
                elapsed_preview = preview_end
            f.write(f"\n{'='*60}\n")
        print(f"📄 Saved chunk plan log to: {chunk_plan_log_path}")
    except Exception as e:
        print(f"[WARN]  Could not save chunk plan log: {e}")

    segment_paths: List[str] = []
    try:
        elapsed_seconds = 0.0
        for index, chunk_animation in enumerate(chunk_animations, start=1):
            segment_job_id = f"{job_id}_part{index:02d}"
            segment_payload = payload.copy(deep=True)
            segment_payload.video_animation = chunk_animation
            chunk_duration = float(chunk_animation.duration_seconds or 0.0)
            if index == 1:
                chunk_label = "first 10 sec"
            elif index == 2 and len(chunk_animations) >= 2:
                chunk_label = "next 10 sec"
            elif index == len(chunk_animations) and chunk_duration < VIDEO_RENDER_MAX_CHUNK_SECONDS:
                chunk_label = f"final {chunk_duration:.3f} sec"
            else:
                chunk_label = f"segment {index}"
            chunk_start_seconds = elapsed_seconds
            chunk_end_seconds = chunk_start_seconds + chunk_duration
            frame_count = len(chunk_animation.keyframes or [])
            if frame_count <= 0:
                frame_count = max(1, int(round(chunk_duration * float(chunk_animation.fps or 30))))
            print(
                f"[VideoChunk] Rendering {chunk_label} chunk {index}/{len(chunk_animations)} "
                f"({chunk_start_seconds:.3f}s -> {chunk_end_seconds:.3f}s, {chunk_duration:.3f}s, {frame_count} frames)"
            )
            print(
                f"🎞️ Rendering chunk {index}/{len(chunk_animations)} "
                f"({chunk_start_seconds:.3f}s -> {chunk_end_seconds:.3f}s, {chunk_duration:.3f}s)"
            )
            if logger:
                video_chunks = logger.log_data.setdefault("video_chunks", [])
                video_chunks.append({
                    "chunk_index": int(index),
                    "total_chunks": int(len(chunk_animations)),
                    "label": chunk_label,
                    "start_seconds": round(chunk_start_seconds, 3),
                    "end_seconds": round(chunk_end_seconds, 3),
                    "duration_seconds": round(chunk_duration, 3),
                    "frame_count": int(frame_count),
                    "segment_job_id": segment_job_id,
                    "timestamp": datetime.now().isoformat(),
                })
            segment_path = _run_godot_video_render(segment_job_id, segment_payload, logger=None, include_audio=False)
            segment_paths.append(segment_path)
            elapsed_seconds = chunk_end_seconds

        print(f"🎞️ Merging {len(segment_paths)} rendered chunk(s) into final video...")
        final_output_path = os.path.abspath(os.path.join(RENDER_OUTPUT_DIR, f"{job_id}_render.mp4"))
        return _merge_video_segments_with_ffmpeg(segment_paths, final_output_path)
    finally:
        for segment_path in segment_paths:
            try:
                if os.path.exists(segment_path):
                    os.remove(segment_path)
            except Exception:
                pass

def _background_video_render_task(project_id: int, gallery_id: int, payload: VideoGenerationPayload, job_id: str, user_id: int = 0, suppress_error_status: bool = False):
    """The main function that runs in the background to perform the full video render and upload pipeline."""
    logger = RenderLogger(job_id)
    logger.start_process()
    logger.set_render_type("VIDEO")
    logger.set_user_details(project_id, gallery_id, user_id)
    logger.set_render_quality(payload.render_quality)
    
    output_video_path = None
    enable_status_updates = getattr(payload, 'enable_status_updates', False)
    
    try:
        start_time = time.monotonic()
        video_animation = payload.video_animation
        duration_seconds = video_animation.duration_seconds if video_animation else 5.0
        fps = video_animation.fps if video_animation else 30
        print(f"\n{'='*80}\n🎬 BACKGROUND VIDEO RENDER TASK STARTED (GODOT)\n{'='*80}\nJob ID: {job_id}\nProject ID: {project_id}\nGallery ID: {gallery_id}\nUser ID: {user_id}\nDuration: {duration_seconds}s @ {fps}fps\nStatus Updates: {'ENABLED' if enable_status_updates else 'DISABLED'}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*80}\n")
        
        # Step 1/5: Preparing your scene
        if enable_status_updates:
            _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "preparing", 10, "Preparing your scene...", render_type="VIDEO", logger=logger)
        
        print(f"🔍 STEP 1/5: Validating input payload...\n{'─'*60}")
        is_valid, validation_error = _validate_video_payload(payload)
        if not is_valid:
            error_msg = f"Invalid payload: {validation_error}"
            print(f"{COLOR_RED}❌ VALIDATION FAILED: {error_msg}{COLOR_RESET}")
            if enable_status_updates:
                _send_status_update(
                    project_id, gallery_id, user_id, job_id, 
                    "rejected", "validation", 0, 
                    "Something went wrong. Please try again.", 
                    render_type="VIDEO", logger=logger
                )
            logger.add_interrupt("validation_failed", error_msg)
            logger.end_process(success=False, error=error_msg)
            return
        print(f"[OK] Payload validation passed\n")
        
        # Step 2/5: Setting up your space
        if enable_status_updates:
            _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "loading", 30, "Setting up your space...", render_type="VIDEO", logger=logger)
        
        # Step 3/5: Creating your video
        if enable_status_updates:
            _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "rendering", 50, "Creating your video...", render_type="VIDEO", logger=logger)
            
        print(f"🎬 STEP 3/5: Running Video Render...\n{'─'*60}")
        
        # Optimization (Optional, kept from original logic if needed, but handled by Godot's throughput mostly)
        # Note: Godot can handle unoptimized scenes better in some cases, but culling is always good.
        # We reused the optimizer in previous code. Let's skip it for simplicity unless requested, as it was complex.
        # If needed, we can re-add `VideoSceneOptimizer` usage here.
        
        output_video_path = _run_chunked_godot_video_render(job_id, payload, logger=logger)
        
        if not output_video_path or not os.path.exists(output_video_path):
            raise Exception("Godot video render failed to produce output")
            
        print(f"[OK] Video Render completed successfully: {output_video_path}\n")

        # Step 4/5: Processing your video
        if enable_status_updates:
             _send_status_update(project_id, gallery_id, user_id, job_id, "processing", "finalizing", 80, "Processing your video...", render_type="VIDEO", logger=logger, wait_for_delivery=True)
        
        print(f"📤 STEP 4/5: Uploading to external API...\n{'─'*60}")
             
        upload_success = _upload_render_to_api(project_id, gallery_id, output_video_path, user_id, logger=logger, job_id=job_id)
        
        end_time = time.monotonic()
        if upload_success:
            print(f"\n{'='*80}\n🎉 ALL VIDEO STEPS COMPLETED SUCCESSFULLY! (Total Time: {(end_time - start_time):.2f}s)\n{'='*80}")
            logger.end_process(success=True)
        else:
            print(f"\n{'='*80}\n[WARN] VIDEO RENDER COMPLETED BUT UPLOAD FAILED (Total Time: {(end_time - start_time):.2f}s)\n{'='*80}")
            logger.end_process(success=True, error="Upload failed")
            
    except GPUResourceError as e:
        # GPU ran out of VRAM / driver reset. Crashed AVI + black frames were
        # already deleted upstream - report the real reason, upload nothing.
        error_msg = str(e)
        print(f"\n{'='*80}\n{COLOR_RED}[GPU] GPU CAPACITY ERROR IN VIDEO BACKGROUND TASK{COLOR_RESET}\n{'='*80}\nJob ID: {job_id}\nError: {error_msg}\n{'='*80}\n")
        # Always notify: a GPU fault is never retried, so this is the only
        # status update the customer will get for this job.
        if enable_status_updates:
            _send_status_update(
                project_id, gallery_id, user_id, job_id,
                "error", "error", 0,
                GPU_STATUS_MESSAGE,
                render_type="VIDEO", logger=logger
            )
        logger.add_interrupt("gpu_error", error_msg)
        logger.end_process(success=False, error=error_msg)
        raise  # Propagate so the retry loop / queue worker sees the failure

    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        print(f"\n{'='*80}\n{COLOR_RED}❌ ERROR IN VIDEO BACKGROUND TASK{COLOR_RESET}\n{'='*80}\nJob ID: {job_id}\nError: {error_msg}\n{'='*80}\n")
        import traceback
        traceback.print_exc()
        
        if suppress_error_status:
            # Re-raise so the retry loop can catch it and retry
            logger.add_interrupt("error", error_msg)
            logger.end_process(success=False, error=error_msg)
            raise
        
        if enable_status_updates:
            _send_status_update(project_id, gallery_id, user_id, job_id, "error", "error", 0, "Scene is too large to render. Please try a different setup.", render_type="VIDEO", logger=logger)
        logger.add_interrupt("error", error_msg)
        logger.end_process(success=False, error=error_msg)
        raise  # Always propagate so retry loop detects the failure
    finally:
        pass

# --- API ENDPOINTS ---

@app.get("/")
async def root():
    """Root endpoint to confirm the service is running."""
    return {"message": "Service Online: The Dream Engine is ready to bring your visions to life."}





@app.post("/api/cancel-job/{job_id}")
async def cancel_job(job_id: str):
    """
    Cancel a specific job by marking it as failed.
    
    **No Authentication Required:** This endpoint is for internal dashboard use only.
    
    Args:
        job_id: The UUID of the job to cancel
    
    Returns:
        Success message with job details
    """
    with queue_lock:
        if job_id not in active_jobs:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
        job = active_jobs[job_id]
        
        # Only allow cancelling queued or processing jobs
        if job["status"] not in ["queued", "processing"]:
            raise HTTPException(
                status_code=400, 
                detail=f"Cannot cancel job with status '{job['status']}'. Only queued or processing jobs can be cancelled."
            )
        
        # Mark job as failed
        job["status"] = "failed"
        job["failed_at"] = datetime.now().isoformat()
        job["error"] = "Cancelled by user"
        job["message"] = "Job Cancelled: The creation process has been gently set aside."
        job["progress"] = 0
        
        print(f"\n{'='*80}\n❌ JOB CANCELLED BY USER\n{'='*80}\nJob ID: {job_id}\nType: {job.get('type', 'unknown')}\nProject: {job.get('project_id', 'unknown')}\n{'='*80}\n")
    

    
    return JSONResponse(
        status_code=200,
        content={
            "status": "success",
            "message": f"Job {job_id} cancelled successfully",
            "job_id": job_id
        }
    )


@app.post("/generate-glb-with-camera/", response_model=None)
async def generate_glb_with_camera(
    payload: GenerationPayload,
    current_user: CurrentUser
):
    """
    Generates a fast, low-resolution GLB for preview purposes (without camera - camera handled by Godot).
    
    **Authentication Required:** This endpoint requires a valid API key in the X-API-Key header.
    """
    job_id = str(uuid.uuid4())
    logger = RenderLogger(job_id)
    logger.start_process()
    
    try:
        print(f"Generating low-res GLB for preview (Job ID: {job_id}, User: {current_user.email})")
        base_glb = _build_base_glb(job_id, payload.floor_plan_data, use_high_res=False, logger=logger)
        # Create camera coordinates file - pass full payload dict to extract all camera data
        # Use dict(exclude_unset=False) to include all fields including optional camera data
        payload_dict = payload.dict(exclude_unset=False, exclude_none=False)
        camera_json_path = _create_camera_coordinates_file(base_glb, payload_dict, payload.render_quality, payload.aspect_ratio, logger=logger)
        logger.end_process(success=True)
        return FileResponse(path=base_glb, media_type='model/gltf-binary', filename=f"model_{job_id}.glb")
    except subprocess.CalledProcessError as e:
        error_msg = f"Godot render prep error:\n{e.stdout}\n{e.stderr}"
        logger.end_process(success=False, error=error_msg)
        raise HTTPException(status_code=500, detail=error_msg)
    except Exception as e:
        logger.end_process(success=False, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate-4k-render/")
async def generate_4k_render_async(
    current_user: CurrentUser,
    project_id: int = Query(..., alias="ProjectId", description="The master Project ID for the render."),
    id: int = Query(..., alias="Id", description="The specific gallery item ID for this render."),
    user_id: int = Query(0, alias="UserId", description="The user ID for this render."),
    job_id_param: Optional[str] = Query(None, alias="JobId", description="Optional manual job ID."),
    payload: GenerationPayload = Body(...)
):
    """
    Accepts a render job, adds it to the queue, and immediately returns a confirmation.
    Jobs are processed ONE AT A TIME to prevent render conflicts.
    
    **Authentication Required:** This endpoint requires a valid API key in the X-API-Key header.
    """
    job_id = job_id_param if job_id_param else str(uuid.uuid4())
    size_info = _calculate_image_render_size(payload)
    print(f"\n{'='*80}\n[IMAGE] NEW RENDER REQUEST RECEIVED\n{'='*80}")
    # Echo the natural-language prompt. This is the only place in the log where
    # the operator can confirm a prompt actually reached the engine.
    _prompt_line = (
        f"\nPrompt: {payload.prompt.strip()}"
        if getattr(payload, "prompt", None) and payload.prompt.strip()
        else ""
    )
    print(f"Job ID: {job_id}\nAuthenticated User: {current_user.email}\nProject ID: {project_id}\nGallery ID: {id}\nUser ID: {user_id}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nQuality: {payload.render_quality}\nOutput: {size_info['output_resolution']}{_prompt_line}\n{'='*80}")
    
    if str(payload.draft_render).lower() == 'false' and REROUTE_ENGINE_URL:
        print(f"\n{COLOR_YELLOW}[FORWARDING] Rerouting IMAGE job {job_id} to external engine: {REROUTE_ENGINE_URL}{COLOR_RESET}")
        
        async with httpx.AsyncClient() as client:
            try:
                headers = {}
                if REROUTE_ENGINE_API_KEY:
                    headers["X-API-Key"] = REROUTE_ENGINE_API_KEY
                
                params = {
                    "ProjectId": project_id,
                    "Id": id,
                    "UserId": user_id,
                    "JobId": job_id
                }
                
                response = await client.post(
                    f"{REROUTE_ENGINE_URL.rstrip('/')}/generate-4k-render/",
                    json=payload.model_dump(),
                    params=params,
                    headers=headers,
                    timeout=30.0
                )
                
                if response.status_code in (200, 202):
                    with queue_lock:
                        active_jobs[job_id] = {
                            'status': 'queued',
                            'position': 0,
                            'queued_at': datetime.now().isoformat(),
                            'render_type': 'IMAGE',
                            'project_id': project_id,
                            'gallery_id': id,
                            'processing_externally': True,
                            **size_info,
                        }
                    
                    return JSONResponse(
                        status_code=202,
                        content={
                            "status": "queued",
                            "message": "Job Queued (External): Your vision has been routed to our high-quality rendering node.",
                            "job_id": job_id,
                            "queue_position": 0,
                            "queue_size": render_queue.qsize(),
                            "project_id": project_id,
                            "UserId": user_id,
                            "RenderId": 0,
                            "RenderType": "IMAGE",
                            **size_info,
                        }
                    )
                else:
                    print(f"{COLOR_RED}[ERROR] External engine returned {response.status_code}: {response.text}{COLOR_RESET}")
                    print(f"{COLOR_YELLOW}[INFO] Falling back to local engine...{COLOR_RESET}")
                    
            except Exception as e:
                print(f"{COLOR_RED}[ERROR] Failed to communicate with external engine: {str(e)}{COLOR_RESET}")
                print(f"{COLOR_YELLOW}[INFO] Falling back to local engine...{COLOR_RESET}")

    # Add job to queue
    job_data = {
        'job_id': job_id,
        'type': 'image',
        'project_id': project_id,
        'gallery_id': id,
        'user_id': user_id,
        'payload': payload,
        **size_info,
    }
    
    # Track job status
    with queue_lock:
        queue_position = render_queue.qsize() + 1  # Position in queue (1-indexed)
        active_jobs[job_id] = {
            'status': 'queued',
            'position': queue_position,
            'queued_at': datetime.now().isoformat(),
            'render_type': 'IMAGE',
            'project_id': project_id,
            'gallery_id': id,
            **size_info,
        }
    
    render_queue.put(job_data)
    
    # Broadcast queue update to monitoring dashboard

    
    print(f"\n{COLOR_GREEN}[OK] Job added to queue (Position: {queue_position}){COLOR_RESET}")
    print(f"[STATS] Current queue size: {render_queue.qsize()}")
    print(f"[NOTE] Jobs are processed ONE AT A TIME to prevent system overload\n")
    
    return JSONResponse(
        status_code=202,
        content = {
            "status": "queued",
            "message": "Job Queued: Your vision has been successfully entrusted to us. The magic begins now.",
            "job_id": job_id,
            "queue_position": queue_position,
            "queue_size": render_queue.qsize(),
            "project_id": project_id,
            "UserId": user_id,
            "RenderId": 0,
            "RenderType": "IMAGE",
            **size_info,
    }
    )

@app.post("/generate-4k-video/")
async def generate_4k_video_async(
    current_user: CurrentUser,
    project_id: int = Query(..., alias="ProjectId", description="The master Project ID for the video render."),
    id: int = Query(..., alias="Id", description="The specific gallery item ID for this video render."),
    user_id: int = Query(0, alias="UserId", description="The user ID for this video render."),
    job_id_param: Optional[str] = Query(None, alias="JobId", description="Optional manual job ID."),
    payload: VideoGenerationPayload = Body(...)
):
    """
    Accepts a video render job with camera animation, adds it to the queue, and immediately returns a confirmation.
    Jobs are processed ONE AT A TIME to prevent render conflicts.
    
    **Authentication Required:** This endpoint requires a valid API key in the X-API-Key header.
    """
    job_id = job_id_param if job_id_param else str(uuid.uuid4())
    size_info = _calculate_video_render_size(payload)
    video_animation = payload.video_animation
    duration_seconds = video_animation.duration_seconds if video_animation else 5.0
    fps = video_animation.fps if video_animation else 30
    
    print(f"\n{'='*80}\n[VIDEO] NEW VIDEO RENDER REQUEST RECEIVED\n{'='*80}")
    # Echo the natural-language prompt — same as the image banner. This is the
    # only place in the log where the operator can confirm a prompt arrived.
    _prompt_line = (
        f"\nPrompt: {payload.prompt.strip()}"
        if getattr(payload, "prompt", None) and payload.prompt.strip()
        else ""
    )
    print(f"Job ID: {job_id}\nAuthenticated User: {current_user.email}\nProject ID: {project_id}\nGallery ID: {id}\nUser ID: {user_id}\nDuration: {duration_seconds}s @ {fps}fps\nOutput: {size_info['output_resolution']}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{_prompt_line}\n{'='*80}")
    
    if str(payload.draft_render).lower() == 'false' and REROUTE_ENGINE_URL:
        print(f"\n{COLOR_YELLOW}[FORWARDING] Rerouting VIDEO job {job_id} to external engine: {REROUTE_ENGINE_URL}{COLOR_RESET}")
        
        async with httpx.AsyncClient() as client:
            try:
                headers = {}
                if REROUTE_ENGINE_API_KEY:
                    headers["X-API-Key"] = REROUTE_ENGINE_API_KEY
                
                params = {
                    "ProjectId": project_id,
                    "Id": id,
                    "UserId": user_id,
                    "JobId": job_id
                }
                
                response = await client.post(
                    f"{REROUTE_ENGINE_URL.rstrip('/')}/generate-4k-video/",
                    json=payload.model_dump(),
                    params=params,
                    headers=headers,
                    timeout=30.0
                )
                
                if response.status_code in (200, 202):
                    with queue_lock:
                        active_jobs[job_id] = {
                            'status': 'queued',
                            'position': 0,
                            'queued_at': datetime.now().isoformat(),
                            'render_type': 'VIDEO',
                            'project_id': project_id,
                            'gallery_id': id,
                            'duration': duration_seconds,
                            'fps': fps,
                            'processing_externally': True,
                            **size_info,
                        }
                    
                    return JSONResponse(
                        status_code=202,
                        content={
                            "status": "queued",
                            "message": "Video Job Queued (External): Your vision has been routed to our high-quality rendering node.",
                            "job_id": job_id,
                            "queue_position": 0,
                            "queue_size": render_queue.qsize(),
                            "project_id": project_id,
                            "UserId": user_id,
                            "RenderId": 0,
                            "RenderType": "VIDEO",
                            **size_info,
                        }
                    )
                else:
                    print(f"{COLOR_RED}[ERROR] External engine returned {response.status_code}: {response.text}{COLOR_RESET}")
                    print(f"{COLOR_YELLOW}[INFO] Falling back to local engine...{COLOR_RESET}")
                    
            except Exception as e:
                print(f"{COLOR_RED}[ERROR] Failed to communicate with external engine: {str(e)}{COLOR_RESET}")
                print(f"{COLOR_YELLOW}[INFO] Falling back to local engine...{COLOR_RESET}")

    # Add job to queue
    job_data = {
        'job_id': job_id,
        'type': 'video',
        'project_id': project_id,
        'gallery_id': id,
        'user_id': user_id,
        'payload': payload,
        **size_info,
    }
    
    # Track job status
    with queue_lock:
        queue_position = render_queue.qsize() + 1  # Position in queue (1-indexed)
        active_jobs[job_id] = {
            'status': 'queued',
            'position': queue_position,
            'queued_at': datetime.now().isoformat(),
            'render_type': 'VIDEO',
            'project_id': project_id,
            'gallery_id': id,
            'duration': duration_seconds,
            'fps': fps,
            **size_info,
        }
    
    render_queue.put(job_data)
    
    # Broadcast queue update to monitoring dashboard

    
    print(f"\n{COLOR_GREEN}[OK] Video job added to queue (Position: {queue_position}){COLOR_RESET}")
    print(f"[DB] Current queue size: {render_queue.qsize()}")
    print(f"💡 Jobs are processed ONE AT A TIME to prevent system overload\n")
    
    return JSONResponse(
        status_code=202,
        content={
            "status": "queued",
            "message": "Job Queued: Your cinematic story has been accepted. Preparing the stage.",
            "job_id": job_id,
            "queue_position": queue_position,
            "queue_size": render_queue.qsize(),
            "project_id": project_id,
            "UserId": user_id,
            "RenderId": 0,
            "RenderType": "VIDEO",
            "duration_seconds": duration_seconds,
            "fps": fps,
            **size_info
        }
    )
@app.get("/logs/{job_id}")
async def get_job_log(job_id: str):
    """
    Returns the JSON render log for a specific job ID.
    Used by the monitoring system to collect artifacts.
    """
    with queue_lock:
        job = active_jobs.get(job_id)
        
    if job and job.get('processing_externally') and REROUTE_ENGINE_URL:
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                headers = {}
                if REROUTE_ENGINE_API_KEY:
                    headers["X-API-Key"] = REROUTE_ENGINE_API_KEY
                resp = await client.get(f"{REROUTE_ENGINE_URL.rstrip('/')}/logs/{job_id}", headers=headers, timeout=5.0)
                if resp.status_code == 200:
                    return JSONResponse(content=resp.json())
                elif resp.status_code == 404:
                    raise HTTPException(status_code=404, detail=f"Log file for external job {job_id} not found yet")
                else:
                    raise HTTPException(status_code=resp.status_code, detail=f"External engine log fetch error: {resp.text}")
        except Exception as e:
            if isinstance(e, HTTPException):
                raise
            raise HTTPException(status_code=502, detail=f"Failed to fetch logs from external engine: {e}")

    log_path = os.path.join("logs", f"{job_id}_render.json")
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail=f"Log file for job {job_id} not found")
    
    return FileResponse(log_path, media_type="application/json", filename=f"{job_id}_render.json")


# --- AI RENDER ENDPOINT ---
from fastapi import File, UploadFile

@app.post("/ai-render/convert-day")
async def sd_airender(
    current_user: CurrentUser,
    project_id: int = Query(..., alias="ProjectId", description="The master Project ID"),
    id: int = Query(..., alias="Id", description="The specific gallery item ID"),
    user_id: int = Query(..., alias="UserId", description="The user ID"),
    job_id_param: Optional[str] = Query(None, alias="JobId", description="Optional existing Job ID"),
    image: UploadFile = File(..., description="PNG image file to convert"),
):
    if not ai_service.is_libraries_installed:
        raise HTTPException(status_code=503, detail="AI rendering is disabled - missing dependencies (diffusers, torch, etc.)")
    
    if image.content_type not in {"image/png", "image/x-png"}:
        raise HTTPException(status_code=400, detail="Only PNG images are supported for ai render.")
    
    file_bytes = await image.read()
    if len(file_bytes) / (1024 * 1024) > ai_service.max_file_mb:
        raise HTTPException(status_code=400, detail=f"File exceeds {ai_service.max_file_mb}MB.")

    try:
        with Image.open(io.BytesIO(file_bytes)) as source_image:
            input_width, input_height = source_image.size
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid PNG image: {e}")

    size_info = _calculate_ai_render_size(input_width, input_height, ai_service.target_long_side)
    
    job_id = job_id_param if job_id_param else str(uuid.uuid4())
    input_path = ai_service.input_dir / f"{job_id}_{Path(image.filename).stem}.png"
    output_path = ai_service.output_dir / f"{job_id}_render.png"
    
    # Save uploaded file
    try:
        input_path.write_bytes(file_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save input image: {e}")
    
    with queue_lock:
        active_jobs[job_id] = {
            "status": "queued",
            "type": "ai render",
            "display_type": "ai render",
            "render_type": ai_service.status_render_type,
            "project_id": project_id,
            "gallery_id": id,
            "user_id": user_id,
            "queued_at": datetime.now().isoformat(),
            "progress": 0,
            "message": "AI render job queued.",
            "current_step": "queued",
            "input_filename": image.filename,
            "output_filename": output_path.name,
            "started_at": None,
            "completed_at": None,
            "failed_at": None,
            **size_info,
        }
    
    job_data = {
        "job_id": job_id,
        "project_id": project_id,
        "gallery_id": id,
        "user_id": user_id,
        "input_path": str(input_path),
        "output_path": str(output_path),
        "filename": image.filename,
        **size_info,
    }
    
    ai_service.render_queue.put(job_data)
    
    print(f"{COLOR_GREEN}[OK] AI Render job added to queue: {job_id}{COLOR_RESET}")
    
    return JSONResponse(
        status_code=202,
        content={
            "status": "queued",
            "job_id": job_id,
            "ProjectId": project_id,
            "Id": id,
            "UserId": user_id,
            "RenderType": ai_service.status_render_type,
            **size_info,
        }
    )


@app.get("/engine-status")
def ai_engine_status():
    jobs = _get_ai_jobs_snapshot()
    queued = sum(1 for job in jobs if job.get("status") == "queued")
    processing = sum(1 for job in jobs if job.get("status") == "processing")
    completed = sum(1 for job in jobs if job.get("status") == "completed")
    failed = sum(1 for job in jobs if job.get("status") in {"failed", "error"})

    current_job = CURRENT_STATUS.get("active_job")
    if current_job and isinstance(current_job, dict):
        current_job = dict(current_job)
        current_job_id = current_job.get("id")
        if current_job_id:
            with queue_lock:
                live_job = active_jobs.get(current_job_id, {})
            current_job.update(_extract_job_render_size(live_job))

    return {
        "status": "ok",
        "online": True,
        "is_paused": False,
        "is_busy": CURRENT_STATUS.get("is_busy", False),
        "queue_size": queued,
        "active_jobs": queued + processing,
        "completed_jobs": completed,
        "failed_jobs": failed,
        "current_job": current_job,
    }


@app.get("/health")
def ai_health():
    return ai_engine_status()


@app.get("/queue-status-public")
def ai_queue_status_public():
    jobs = _get_ai_jobs_snapshot()
    queued = sum(1 for job in jobs if job.get("status") == "queued")
    processing = sum(1 for job in jobs if job.get("status") == "processing")
    completed = sum(1 for job in jobs if job.get("status") == "completed")
    failed = sum(1 for job in jobs if job.get("status") in {"failed", "error"})

    return JSONResponse(
        content={
            "queue_size": queued,
            "active_jobs_count": queued + processing,
            "processing_jobs_count": processing,
            "completed_jobs_count": completed,
            "failed_jobs_count": failed,
            "is_paused": False,
            "jobs": jobs,
            "engine_online": True,
            "engine_name": "External 4K + AI Engine",
            "engine_url": os.getenv("AI_RENDER_ENGINE_URL", "embedded://external-4k-ai-main"),
        }
    )


@app.get("/queue-status")
def ai_queue_status():
    return ai_queue_status_public()


@app.get("/job-status/{job_id}")
def ai_job_status(job_id: str):
    with queue_lock:
        job = active_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"AI job {job_id} not found")
    return {"job_id": job_id, **_build_ai_job_snapshot(job_id, dict(job))}


@app.get("/ai-render/health")
def ai_render_health_alias():
    return ai_engine_status()


@app.get("/ai-render/")
@app.get("/ai-render")
def ai_render_root_alias():
    return ai_engine_status()


@app.get("/ai-render/queue-status-public")
def ai_render_queue_status_public_alias():
    return ai_queue_status_public()


@app.get("/ai-render/queue-status")
def ai_render_queue_status_alias():
    return ai_queue_status_public()


@app.get("/ai-render/job-status/{job_id}")
def ai_render_job_status_alias(job_id: str):
    return ai_job_status(job_id)

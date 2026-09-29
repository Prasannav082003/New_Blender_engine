"""
Render logging service to track assets, timings, and save JSON logs.
"""
import os
import json
import time
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path

# Console colors
COLOR_GREEN = "\033[92m"
COLOR_RED = "\033[91m"
COLOR_RESET = "\033[0m"

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

class RenderLogger:
    """Logger for tracking render process metrics."""
    
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.log_data = {
            "job_id": job_id,
            "project_id": None,
            "gallery_id": None,
            "user_id": None,
            "render_quality": None, # e.g. 4K, HD
            "process_start_time": None,
            "process_end_time": None,
            "total_duration_seconds": None,
            "assets": {
                "used_assets": [],
                "total_count": 0
            },
            "timings": {
                "asset_download_time_seconds": 0.0,
                "glb_creation_time_seconds": 0.0,
                "python_script_creation_time_seconds": 0.0,
                "render_time_seconds": 0.0
            },
            "status": "in_progress",
            "render_type": None,  # IMAGE or VIDEO
            "glb_size_bytes": 0,
            "error": None,
            "camera": {
                "threejs_coordinates": None,
                "unreal_coordinates": None,
                "video_frames": []  # For video renders: first few frames only
            },
            "unreal_render_settings": None,
            "interrupts": []
        }
        self.asset_download_start = None
        self.glb_creation_start = None
        self.script_creation_start = None
        self.render_start = None
        
    def start_process(self):
        """Mark the start of the entire process."""
        self.log_data["process_start_time"] = datetime.now().isoformat()
        
    def end_process(self, success: bool = True, error: Optional[str] = None):
        """Mark the end of the entire process."""
        self.log_data["process_end_time"] = datetime.now().isoformat()
        if self.log_data["process_start_time"]:
            start = datetime.fromisoformat(self.log_data["process_start_time"])
            end = datetime.fromisoformat(self.log_data["process_end_time"])
            self.log_data["total_duration_seconds"] = round((end - start).total_seconds(), 3)
        self.log_data["status"] = "completed" if success else "failed"
        if error:
            self.log_data["error"] = str(error)
        self.save_log()
        
    def start_asset_download(self):
        """Mark the start of asset download."""
        self.asset_download_start = time.monotonic()
        
    def end_asset_download(self):
        """Mark the end of asset download."""
        if self.asset_download_start:
            duration = time.monotonic() - self.asset_download_start
            self.log_data["timings"]["asset_download_time_seconds"] = round(duration, 3)
            
    def add_asset(self, url: str, name: str, local_path: Optional[str] = None, cached: bool = False):
        """Add an asset to the log."""
        asset_info = {
            "url": url,
            "name": name,
            "local_path": local_path,
            "cached": cached
        }
        self.log_data["assets"]["used_assets"].append(asset_info)
        self.log_data["assets"]["total_count"] = len(self.log_data["assets"]["used_assets"])
        
    def start_glb_creation(self):
        """Mark the start of GLB creation."""
        self.glb_creation_start = time.monotonic()
        
    def end_glb_creation(self):
        """Mark the end of GLB creation."""
        if self.glb_creation_start:
            duration = time.monotonic() - self.glb_creation_start
            self.log_data["timings"]["glb_creation_time_seconds"] = round(duration, 3)
            
    def start_script_creation(self):
        """Mark the start of Python script creation."""
        self.script_creation_start = time.monotonic()
        
    def end_script_creation(self):
        """Mark the end of Python script creation."""
        if self.script_creation_start:
            duration = time.monotonic() - self.script_creation_start
            self.log_data["timings"]["python_script_creation_time_seconds"] = round(duration, 3)
            
    def start_render(self):
        """Mark the start of Unreal render."""
        self.render_start = time.monotonic()
        
    def end_render(self):
        """Mark the end of Unreal render."""
        if self.render_start:
            duration = time.monotonic() - self.render_start
            self.log_data["timings"]["render_time_seconds"] = round(duration, 3)
    
    def set_threejs_camera(self, camera_data: Dict[str, Any]):
        """Set ThreeJS camera coordinates from the input data."""
        if not camera_data:
            return
        
        threejs_coords = {}
        
        # Extract ThreeJS camera data
        if 'threejs_camera' in camera_data:
            threejs_cam = camera_data['threejs_camera']
            threejs_coords = {
                "position": threejs_cam.get('position', {}),
                "target": threejs_cam.get('target', {}),
                "rotation": threejs_cam.get('rotation', {}),
                "fov": threejs_cam.get('fov', None)
            }
        elif 'position' in camera_data:
            # Fallback to unified format
            threejs_coords = {
                "position": camera_data.get('position', {}),
                "target": camera_data.get('target', {}),
                "rotation": camera_data.get('rotation', {}),
                "fov": camera_data.get('fov', None)
            }
        
        if threejs_coords:
            self.log_data["camera"]["threejs_coordinates"] = threejs_coords
            
    def set_render_type(self, render_type: str):
        """Set the type of render (IMAGE or VIDEO)."""
        self.log_data["render_type"] = render_type

    def set_render_quality(self, quality: str):
        """Set the quality of render (e.g. 4K, HD)."""
        self.log_data["render_quality"] = quality

    def set_user_details(self, project_id: int, gallery_id: int, user_id: int):
        """Set the project and user details."""
        self.log_data["project_id"] = project_id
        self.log_data["gallery_id"] = gallery_id
        self.log_data["user_id"] = user_id
        
    def set_glb_size(self, size_bytes: int):
        """Set the size of the generated GLB file."""
        self.log_data["glb_size_bytes"] = size_bytes
    
    def set_unreal_camera(self, position: Dict[str, float] = None, rotation: Dict[str, float] = None, 
                         target: Dict[str, float] = None, fov: float = None, 
                         forward: Dict[str, float] = None, target_projected: Dict[str, float] = None):
        """Set Unreal Engine camera coordinates after conversion."""
        unreal_coords = {}
        
        if position:
            unreal_coords["final_camera_position"] = position
        if rotation:
            unreal_coords["final_camera_rotation_euler"] = rotation
        if target:
            unreal_coords["final_camera_target"] = target
        if fov is not None:
            unreal_coords["final_fov"] = fov
        if forward:
            unreal_coords["final_camera_forward"] = forward
        if target_projected:
            unreal_coords["final_camera_target_projected"] = target_projected
        
        if unreal_coords:
            self.log_data["camera"]["unreal_coordinates"] = unreal_coords
    
    def add_video_frame_coordinate(self, frame_number: int, unreal_position: Dict[str, float], 
                                   unreal_rotation: Dict[str, float] = None, target: Dict[str, float] = None,
                                   threejs_coordinates: Dict[str, Any] = None, max_frames: int = 5):
        """Add a video frame camera coordinate (only saves first few frames)."""
        # Only save first max_frames frames
        if len(self.log_data["camera"]["video_frames"]) < max_frames:
            frame_data = {
                "frame": frame_number,
                "unreal_position": unreal_position
            }
            if unreal_rotation:
                frame_data["unreal_rotation"] = unreal_rotation
            if target:
                frame_data["target"] = target
            if threejs_coordinates:
                frame_data["threejs_coordinates"] = threejs_coordinates
            
            self.log_data["camera"]["video_frames"].append(frame_data)
    
    def set_unreal_render_settings(self, settings: Dict[str, Any]):
        """Set Unreal Engine render settings."""
        self.log_data["unreal_render_settings"] = settings
    
    def add_interrupt(self, interrupt_type: str, message: str, timestamp: Optional[str] = None):
        """Record an interrupt during the render process."""
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        interrupt = {
            "type": interrupt_type,  # e.g., "timeout", "error", "user_interrupt", "process_error"
            "message": message,
            "timestamp": timestamp
        }
        self.log_data["interrupts"].append(interrupt)
    
    def add_status_update(self, step: str, status: str, duration_seconds: float, status_code: int):
        """
        Record a status update sent to the API.
        
        Args:
            step: The step name (e.g., "asset_processing", "rendering")
            status: The status (e.g., "processing", "completed", "failed")
            duration_seconds: Time taken to send the status update
            status_code: HTTP status code from the API response
        """
        if "status_updates" not in self.log_data:
            self.log_data["status_updates"] = []
        
        status_update = {
            "step": step,
            "status": status,
            "duration_seconds": round(duration_seconds, 3),
            "status_code": status_code,
            "timestamp": datetime.now().isoformat()
        }
        self.log_data["status_updates"].append(status_update)
    
    def get_log_data(self) -> Dict[str, Any]:
        """
        Get the complete log data dictionary.
        
        Returns:
            Dictionary containing all logged data
        """
        return self.log_data
    
    def save_log(self, force: bool = False):
        """
        Save the log to a JSON file.
        
        Args:
            force: If True, save even if process hasn't ended (useful for interrupts)
        """
        # Update end time if not set and we're forcing a save
        if force and not self.log_data["process_end_time"]:
            self.log_data["process_end_time"] = datetime.now().isoformat()
            if self.log_data["process_start_time"]:
                start = datetime.fromisoformat(self.log_data["process_start_time"])
                end = datetime.fromisoformat(self.log_data["process_end_time"])
                self.log_data["total_duration_seconds"] = round((end - start).total_seconds(), 3)
        
        log_filename = f"{self.job_id}_render.json"
        log_path = os.path.join(LOG_DIR, log_filename)
        
        try:
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(self.log_data, f, indent=2, ensure_ascii=False)
            print(f"\n{COLOR_GREEN}✅ Render log saved: {log_path}{COLOR_RESET}")
        except Exception as e:
            print(f"{COLOR_RED}❌ Failed to save render log: {e}{COLOR_RESET}")


import os
import uuid
import torch
import queue
import logging
import json
import time
import requests
from pathlib import Path
from threading import Lock, Thread
from datetime import datetime
from typing import Optional, List, Dict, Any
from PIL import Image, ImageOps, ImageEnhance
import io
import cv2
import numpy as np
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

# Try to import AI libraries
try:
    from diffusers import (
        AutoencoderKL,
        ControlNetModel,
        DPMSolverMultistepScheduler,
        StableDiffusionControlNetImg2ImgPipeline,
    )
    from huggingface_hub import hf_hub_download
    from controlnet_aux import CannyDetector, MidasDetector
    AI_LIBRARIES_INSTALLED = True
except ImportError:
    AI_LIBRARIES_INSTALLED = False

# Console Colors
COLOR_RED = "\033[91m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_BLUE = "\033[94m"
COLOR_RESET = "\033[0m"

class AIRenderService:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        
        # AI Cache/State
        self.pipeline = None
        self.canny_detector = None
        self.depth_detector = None
        self.pipeline_lock = Lock()
        self.setup_lock = Lock()
        self.render_queue = queue.Queue()
        self.worker_thread = None
        self.is_libraries_installed = AI_LIBRARIES_INSTALLED
        self.is_shutting_down = False

        # Device settings
        if self.is_libraries_installed:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self.torch_dtype = torch.float16 if self.device == "cuda" else torch.float32
        else:
            self.device = "cpu"
            self.torch_dtype = None

        # Configuration from Environment
        self.input_dir = base_dir / "uploads" / "ai-render" / "input"
        self.output_dir = base_dir / "uploads" / "ai-render" / "output"
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.model_path = Path(os.getenv("AI_RENDER_MODEL_PATH", str(base_dir / "Realistic_Vision_V5.1_noVAE" / "Realistic_Vision_V5.1.safetensors")))
        self.vae_path = Path(os.getenv("AI_RENDER_VAE_PATH", str(base_dir / "Realistic_Vision_V5.1_noVAE" / "vae-ft-mse-840000-ema-pruned.safetensors")))
        self.model_repo_id = os.getenv("AI_RENDER_MODEL_REPO_ID", "SG161222/Realistic_Vision_V5.1_noVAE")
        self.model_filename = os.getenv("AI_RENDER_MODEL_FILENAME", "Realistic_Vision_V5.1.safetensors")
        self.vae_repo_id = os.getenv("AI_RENDER_VAE_REPO_ID", "stabilityai/sd-vae-ft-mse-original")
        self.vae_filename = os.getenv("AI_RENDER_VAE_FILENAME", "vae-ft-mse-840000-ema-pruned.safetensors")
        self.controlnet_model_id = os.getenv("AI_RENDER_CONTROLNET_MODEL", "lllyasviel/sd-controlnet-canny")
        self.depth_controlnet_model_id = os.getenv("AI_RENDER_DEPTH_CONTROLNET_MODEL", "lllyasviel/control_v11f1p_sd15_depth")

        self.max_input_size = int(os.getenv("AI_RENDER_MAX_INPUT_SIZE", "1024"))
        self.target_long_side = int(os.getenv("AI_RENDER_TARGET_LONG_SIDE", "2048"))
        self.steps = int(os.getenv("AI_RENDER_STEPS", "30"))
        self.guidance_scale = float(os.getenv("AI_RENDER_GUIDANCE_SCALE", "7.5"))
        self.control_scale = float(os.getenv("AI_RENDER_CONTROL_SCALE", "0.60"))
        self.denoising_strength = float(os.getenv("AI_RENDER_DENOISING_STRENGTH", "0.52"))
        self.clip_skip = int(os.getenv("AI_RENDER_CLIP_SKIP", "2"))
        self.max_file_mb = float(os.getenv("AI_RENDER_MAX_FILE_MB", "20"))
        
        # Canny & Pre-processing defaults
        self.canny_low = 100
        self.canny_high = 200
        self.gaussian_blur_radius = 1.5

        self.storage_connection_string = (
            os.getenv("AI_AZURE_STORAGE_CONNECTION_STRING", "").strip('"')
            or os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip('"')
        )
        self.blob_container = (
            os.getenv("AI_AZURE_CONTAINER_NAME", "").strip()
            or os.getenv("AI_RENDER_BLOB_CONTAINER", "").strip()
            or "ai-images"
        )
        self.status_render_type = os.getenv("AI_RENDER_TYPE", "AI_RENDER")

        self.default_negative_prompt = (
            "CGI, 3D render, plastic texture, synthetic fabric, fake lighting, flat lighting, uniform lighting, "
            "overexposed floor, washed-out walls, game engine render, cartoon shadows, blown highlights, fake reflections, "
            "low realism, unreal engine, blender render, changed room layout, altered architecture, extra furniture, "
            "new decorations, fireplace, hallucinated objects, modified room structure, text, logo, signature, "
            "distorted geometry, warped perspective, blurry, motion blur, out of focus, low resolution, watermark"
        )
        self.prompt = os.getenv("AI_RENDER_PROMPT", "").strip()
        self.negative_prompt = os.getenv("AI_RENDER_NEGATIVE_PROMPT", self.default_negative_prompt).strip()
        
        # Adaptive Scene Dictionary with Camera Specs
        self.scene_templates = {
            "bedroom": "ultra photorealistic architectural photography, luxury master bedroom, natural window lighting, real fabric texture, king-size bed, natural wood grain, professional DSLR interior photography, Canon EOS R5, 24mm lens, preserved room structure, high material realism",
            "office": "professional executive office interior, ultra photorealistic, natural daylight entering from windows, realistic wood desk, leather textures, architectural realism, balanced exposure, Canon EOS R5",
            "living": "ultra photorealistic architectural photography, realistic daylight luxury living room, natural window lighting, balanced exposure, soft contact shadows, real fabric texture, woven sofa detail, natural wood grain, professional real estate photography",
            "kitchen": "contemporary luxury kitchen, ultra photorealistic, natural daylight, marble textures, high material realism, realistic reflections, physical material response, professional interior photography",
            "bathroom": "spa-like luxury bathroom, marble walls, ultra photorealistic, natural window light, realistic water reflections, high texture fidelity, architectural realism",
            "exterior": "modern architectural exterior, luxury villa facade, natural daylight photography, high-key architectural detail, realistic landscaping, physically accurate global illumination"
        }

        # Dependencies (to be injected)
        self.active_jobs = None
        self.queue_lock = None
        self.send_status_update = None
        self.update_global_status = None
        
        self.failed_inputs_dir = base_dir / "failed_inputs"
        self.failed_inputs_dir.mkdir(parents=True, exist_ok=True)
        self.max_render_attempts = 3

    def setup(self, active_jobs, queue_lock, send_status_update, update_global_status):
        """Injected dependencies from main.py"""
        self.active_jobs = active_jobs
        self.queue_lock = queue_lock
        self.send_status_update = send_status_update
        self.update_global_status = update_global_status

        if self.is_libraries_installed:
            print(f"{COLOR_BLUE}--- Initializing AI Render System ---{COLOR_RESET}")
            Thread(target=self.ensure_models_present, daemon=True).start()
            self.ensure_worker_started()

    def ensure_models_present(self):
        self.ensure_ai_model_present()
        self.ensure_ai_vae_present()

    def ensure_ai_model_present(self) -> Path:
        if not self.is_libraries_installed: return self.model_path
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        if self.model_path.exists():
            return self.model_path
        with self.setup_lock:
            if self.model_path.exists():
                return self.model_path
            print(f"{COLOR_YELLOW}AI render model not found locally. Downloading it now...{COLOR_RESET}")
            try:
                downloaded_path = Path(
                    hf_hub_download(
                        repo_id=self.model_repo_id,
                        filename=self.model_filename,
                        local_dir=str(self.model_path.parent),
                        local_dir_use_symlinks=False,
                    )
                )
            except Exception as exc:
                print(f"{COLOR_RED}Failed to download AI render model: {exc}{COLOR_RESET}")
                return self.model_path
            if downloaded_path != self.model_path:
                if self.model_path.exists():
                    self.model_path.unlink()
                downloaded_path.replace(self.model_path)
            print(f"{COLOR_GREEN}AI render model ready: {self.model_path}{COLOR_RESET}")
            return self.model_path

    def ensure_ai_vae_present(self) -> Path:
        if not self.is_libraries_installed: return self.vae_path
        self.vae_path.parent.mkdir(parents=True, exist_ok=True)
        if self.vae_path.exists():
            return self.vae_path
        with self.setup_lock:
            if self.vae_path.exists():
                return self.vae_path
            print(f"{COLOR_YELLOW}AI render VAE not found locally. Downloading it now...{COLOR_RESET}")
            try:
                downloaded_path = Path(
                    hf_hub_download(
                        repo_id=self.vae_repo_id,
                        filename=self.vae_filename,
                        local_dir=str(self.vae_path.parent),
                        local_dir_use_symlinks=False,
                    )
                )
            except Exception as exc:
                print(f"{COLOR_RED}Failed to download AI render VAE: {exc}{COLOR_RESET}")
                return self.vae_path
            if downloaded_path != self.vae_path:
                if self.vae_path.exists():
                    self.vae_path.unlink()
                downloaded_path.replace(self.vae_path)
            print(f"{COLOR_GREEN}AI render VAE ready: {self.vae_path}{COLOR_RESET}")
            return self.vae_path

    def load_pipeline(self):
        if not self.is_libraries_installed: return None
        if self.pipeline is not None and self.canny_detector is not None:
            return self.pipeline
        
        with self.pipeline_lock:
            if self.pipeline is not None and self.canny_detector is not None:
                return self.pipeline
            
            # Dynamic re-check for CUDA
            if self.device == "cpu" and torch.cuda.is_available():
                print(f"{COLOR_YELLOW}  ↳ GPU detected during late initialization. Switching to CUDA...{COLOR_RESET}")
                self.device = "cuda"
                self.torch_dtype = torch.float16

            self.ensure_ai_model_present()
            self.ensure_ai_vae_present()
            
            print(f"{COLOR_BLUE}--- AI Pipeline Initialization Details ---{COLOR_RESET}")
            print(f"    PyTorch Version: {torch.__version__}")
            print(f"    Target Device: {self.device} / Dtype: {self.torch_dtype}")
            print(f"    CUDA Available (Runtime): {torch.cuda.is_available()}")
            if torch.cuda.is_available():
                print(f"    Device Name: {torch.cuda.get_device_name(0)}")
            print(f"    Model Path: {self.model_path}")
            print(f"    VAE Path: {self.vae_path}")
            print(f"{COLOR_BLUE}----------------------------------------{COLOR_RESET}")
            
            canny_controlnet = ControlNetModel.from_pretrained(self.controlnet_model_id, torch_dtype=self.torch_dtype).to(self.device)
            depth_controlnet = ControlNetModel.from_pretrained(self.depth_controlnet_model_id, torch_dtype=self.torch_dtype).to(self.device)
            
            vae = AutoencoderKL.from_single_file(str(self.vae_path), torch_dtype=self.torch_dtype).to(self.device)
            pipeline = StableDiffusionControlNetImg2ImgPipeline.from_single_file(
                str(self.model_path),
                controlnet=[canny_controlnet, depth_controlnet],
                vae=vae,
                torch_dtype=self.torch_dtype,
                safety_checker=None,
                local_files_only=False,
            )
            pipeline = pipeline.to(self.device)
            pipeline.scheduler = DPMSolverMultistepScheduler.from_config(
                pipeline.scheduler.config,
                algorithm_type="dpmsolver++",
                solver_order=2,
                use_karras_sigmas=True,
            )
            if self.device == "cuda":
                try:
                    vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
                    print(f"    GPU VRAM: {vram_gb:.1f} GB")
                    # Quadro P2000 = 4 GB — always use sequential offload
                    # Hardware Adaptation Logic
                    if vram_gb < 4:
                        print(f"    ↳ VRAM < 4GB — ultra-safe mode")
                        pipeline.enable_sequential_cpu_offload()
                    elif vram_gb < 6:
                        print(f"    ↳ VRAM 4-6GB — performance mode")
                        pipeline.enable_sequential_cpu_offload()
                        pipeline.enable_vae_tiling()
                    else:
                        print(f"    ↳ VRAM > 6GB — full performance")
                        pipeline.enable_model_cpu_offload()
                        pipeline.enable_vae_tiling()
                    
                    pipeline.enable_attention_slicing()
                    pipeline.enable_vae_slicing()
                    # FreeU: Always enabled for quality boost
                    pipeline.enable_freeu(s1=0.9, s2=0.2, b1=1.2, b2=1.4)
                except Exception as e:
                    print(f"    Warning: Optimization step failed: {e}")
            
            torch.backends.cuda.matmul.allow_tf32 = True
            
            self.canny_detector = CannyDetector()
            self.depth_detector = MidasDetector.from_pretrained("lllyasviel/Annotators")
            self.pipeline = pipeline
            return self.pipeline

    def resize_for_generation(self, image: Image.Image, max_size: Optional[int] = None) -> Image.Image:
        if max_size is None:
            max_size = self.max_input_size
        width, height = image.size
        scale = min(max_size / max(width, height), 1.0)
        resized_width = max(8, (int(width * scale) // 8) * 8)
        resized_height = max(8, (int(height * scale) // 8) * 8)
        return image.resize((resized_width, resized_height), Image.Resampling.LANCZOS)

    def preprocess_image(self, image: Image.Image) -> Image.Image:
        img_np = np.array(image.convert("RGB")).astype(np.float32) / 255.0
        # Gentle percentile normalization
        p1, p99 = np.percentile(img_np, (1, 99))
        img_np = np.clip((img_np - p1) / (p99 - p1), 0, 1)
        # Slight shadow cooling
        img_np[:, :, 2] *= 1.015
        img_np = np.clip(img_np, 0, 1)
        img_8bit = (img_np * 255).astype(np.uint8)
        image = Image.fromarray(img_8bit)
        image = ImageEnhance.Contrast(image).enhance(1.02)
        image = ImageEnhance.Color(image).enhance(0.98)
        image = ImageEnhance.Sharpness(image).enhance(1.04)
        return image

    def postprocess_image(self, image: Image.Image) -> Image.Image:
        """Filmic Grade Pipeline: Shadow Toning + Luminance Grain + Micro-contrast"""
        from PIL import ImageFilter, ImageEnhance
        
        # 1. Shadow Blue Toning (Luminance-weighted blue lift)
        img_np = np.array(image).astype(np.float32) / 255.0
        luminance = 0.299 * img_np[:, :, 0] + 0.587 * img_np[:, :, 1] + 0.114 * img_np[:, :, 2]
        shadow_mask = np.clip(1.0 - (luminance / 0.4), 0, 1) # Target lower 40% luminance
        img_np[:, :, 2] += (shadow_mask * (4.0 / 255.0)) # Add +4 blue to shadows
        img_np = np.clip(img_np, 0, 1)
        
        # 2. Luminance-dependent Grain (highlights clean, shadows grainy)
        # grain_intensity = (1 - luminance) * 2.5 std
        grain_std = (1.0 - luminance) * (2.5 / 255.0)
        noise = np.random.normal(0, 1.0, img_np.shape) * grain_std[:, :, np.newaxis]
        img_np = np.clip(img_np + noise, 0, 1)
        
        # Back to 8-bit for PIL
        image = Image.fromarray((img_np * 255).astype(np.uint8))
        
        # 3. Micro-contrast & Sharpness Refinement (DSLR Finish)
        image = image.filter(ImageFilter.UnsharpMask(radius=0.5, percent=70, threshold=3))
        
        # 4. Cinematic Contrast & Saturation (Architectural Balance)
        image = ImageEnhance.Contrast(image).enhance(1.06)
        image = ImageEnhance.Color(image).enhance(1.05)
        image = ImageEnhance.Sharpness(image).enhance(1.1)
        
        return image

    def build_dynamic_prompt(self, base_request: str) -> str:
        """Dynamic Scene Prompting (< 75 tokens)"""
        request_lower = base_request.lower()
        scene_key = "living" # default
        for key in self.scene_templates.keys():
            if key in request_lower:
                scene_key = key
                break
        
        # Combine scene template + photorealistic signal
        optimized_prompt = self.scene_templates[scene_key]
        
        # Ensure we append essential signals if missing
        if "photography" not in optimized_prompt:
            optimized_prompt += ", professional architectural photography, 8k resolution, extreme detail, physically accurate global illumination"
            
        return optimized_prompt

    def get_scene_params(self, prompt: str) -> dict:
        """Returns adaptive Canny thresholds based on scene complexity"""
        p_lower = prompt.lower()
        if "bedroom" in p_lower:
            return {"low": 90, "high": 190, "blur": 1.8}
        if "office" in p_lower:
            return {"low": 110, "high": 210, "blur": 1.2}
        if "kitchen" in p_lower:
            return {"low": 100, "high": 200, "blur": 1.5}
        if "exterior" in p_lower:
            return {"low": 120, "high": 220, "blur": 1.0}
        return {"low": 100, "high": 200, "blur": 1.5} # Default

    def prepare_control_images(self, image: Image.Image, thresholds: dict = None) -> List[Image.Image]:
        """Generates Multi-Control inputs: Canny (edges) + Depth (geometry)"""
        if thresholds is None:
            thresholds = {"low": self.canny_low, "high": self.canny_high, "blur": self.gaussian_blur_radius}
            
        from PIL import ImageFilter
        smoothed_image = image.filter(ImageFilter.GaussianBlur(radius=thresholds["blur"]))
        
        # 1. Canny Control
        canny_image = self.canny_detector(smoothed_image, low_threshold=thresholds["low"], high_threshold=thresholds["high"])
        if not isinstance(canny_image, Image.Image):
            canny_image = Image.fromarray(canny_image)
        canny_image = canny_image.convert("RGB")
        
        # 2. Depth Control
        depth_image = self.depth_detector(image)
        if not isinstance(depth_image, Image.Image):
            depth_image = Image.fromarray(depth_image)
        depth_image = depth_image.convert("RGB")
        
        # Resize to match source
        if canny_image.size != image.size:
            canny_image = canny_image.resize(image.size, Image.Resampling.LANCZOS)
        if depth_image.size != image.size:
            depth_image = depth_image.resize(image.size, Image.Resampling.LANCZOS)
            
        return [canny_image, depth_image]

    def resize_for_delivery(self, image: Image.Image) -> Image.Image:
        width, height = image.size
        long_side = max(width, height)
        if long_side >= self.target_long_side:
            return image
        scale = self.target_long_side / long_side
        output_width = max(8, int(width * scale))
        output_height = max(8, int(height * scale))
        return image.resize((output_width, output_height), Image.Resampling.LANCZOS)

    def _get_file_size_info(self, file_path: str) -> Dict[str, Any]:
        """Return file size metadata for a rendered artifact on disk."""
        if not file_path or not os.path.exists(file_path):
            return {}
        try:
            size_bytes = os.path.getsize(file_path)
        except OSError:
            return {}
        return {
            "output_size_bytes": size_bytes,
            "output_size_mb": round(size_bytes / (1024 * 1024), 2),
        }

    def _build_job_size_info(self, job_data: dict, source_image: Optional[Image.Image] = None) -> Dict[str, Any]:
        """Normalize size metadata so it can be echoed in job status updates."""
        size_info: Dict[str, Any] = {}

        input_width = job_data.get("input_width")
        input_height = job_data.get("input_height")
        if input_width is None or input_height is None:
            if source_image is not None:
                input_width, input_height = source_image.size

        if input_width is not None and input_height is not None:
            input_width = max(int(input_width), 1)
            input_height = max(int(input_height), 1)
            size_info["input_width"] = input_width
            size_info["input_height"] = input_height
            size_info["input_resolution"] = job_data.get("input_resolution") or f"{input_width}x{input_height}"
            size_info["aspect_ratio"] = job_data.get("aspect_ratio") or f"{input_width}:{input_height}"
            size_info["input_size"] = {
                "width": input_width,
                "height": input_height,
                "resolution": size_info["input_resolution"],
            }

        output_width = job_data.get("output_width")
        output_height = job_data.get("output_height")
        if output_width is not None and output_height is not None:
            output_width = max(int(output_width), 1)
            output_height = max(int(output_height), 1)
            output_resolution = job_data.get("output_resolution") or f"{output_width}x{output_height}"
        elif input_width is not None and input_height is not None:
            long_side = max(int(input_width), int(input_height))
            if long_side >= self.target_long_side:
                output_width = int(input_width)
                output_height = int(input_height)
            else:
                scale = self.target_long_side / max(long_side, 1)
                output_width = max(8, int(int(input_width) * scale))
                output_height = max(8, int(int(input_height) * scale))
            output_resolution = f"{output_width}x{output_height}"
        else:
            output_resolution = job_data.get("output_resolution")

        if output_width is not None and output_height is not None:
            size_info["output_width"] = output_width
            size_info["output_height"] = output_height
            size_info["output_resolution"] = output_resolution
            size_info["render_size"] = {
                "width": output_width,
                "height": output_height,
                "resolution": output_resolution,
            }

        return size_info

    def render_image(self, source_image: Image.Image) -> Image.Image:
        pipeline = self.load_pipeline()
        if self.canny_detector is None:
            raise RuntimeError("AI render detector not initialized")
        
        # Stage 1: Build adaptive context
        current_prompt = self.prompt if self.prompt else self.build_dynamic_prompt(str(source_image.info.get('comment', '')))
        current_negative = self.negative_prompt
        scene_params = self.get_scene_params(current_prompt)
        
        # Detect if this is a "Day" render
        is_day = any(word in current_prompt.lower() for word in ["day", "sun", "morning", "bright"])
        
        # 10. GLOBAL DAYLIGHT IMPROVEMENT SETTINGS
        if is_day:
            render_strength = 0.62
            guidance = 7.2
            canny_scale = 0.62
            depth_scale = 0.88
            num_steps = 50
            tile_strength = 0.16
            tile_canny_scale = 0.50
            tile_depth_scale = 0.82
            current_prompt += (
                ", ultra photorealistic daylight interior photography, "
                "realistic global illumination, physically accurate indirect lighting, "
                "natural window light bounce, realistic ambient occlusion, "
                "soft shadow gradients, subtle contact shadows, "
                "real fabric micro texture, woven textile detail, "
                "natural wood grain pores, matte painted wall texture, "
                "cinematic DSLR interior photography, "
                "Canon EOS R5, 24mm lens, "
                "balanced exposure, preserved architecture, "
                "high dynamic range photography, "
                "photographic material realism, "
                "real-world interior lighting response"
            )
            current_negative += (
                ", CGI, game render, artificial lighting, "
                "plastic sofa, overexposed floor, flat walls, "
                "fake reflections, blown highlights, "
                "cartoon shading, unreal engine look, "
                "synthetic materials, low material detail, "
                "oversmoothed textures"
            )
        else:
            render_strength = 0.52      # Night settings (UNCHANGED)
            guidance = 7.5
            canny_scale = 0.60
            depth_scale = 0.60
            num_steps = 30
            tile_strength = 0.14
            tile_canny_scale = 0.98
            tile_depth_scale = 0.98

        # 5. Global Negative Prompt (Hallucination Prevention + Material Stability)
        current_negative += (
            ", altered architecture, extra furniture, fireplace, redesign, hallucinated objects, "
            "bad metallic reflections, distorted chandelier, warped reflections, melted metal, chrome artifacts"
        )

        # Preprocess input
        prepared_image = self.resize_for_generation(ImageOps.exif_transpose(source_image.convert("RGB")))
        prepared_image = self.preprocess_image(prepared_image)
        # 7. Multi-Control Image Generation
        control_images = self.prepare_control_images(prepared_image, thresholds=scene_params)
        
        print(f"{COLOR_BLUE}🚀 Starting Photographic Daylight Render (Steps: {num_steps})...{COLOR_RESET}")
        
        with self.pipeline_lock:
            # Latent noise stabilization
            generator = torch.Generator(device=self.device).manual_seed(42)
            
            # PASS 1: Global Photorealism
            rendered = pipeline(
                prompt=current_prompt,
                negative_prompt=current_negative,
                image=prepared_image,
                control_image=control_images, 
                height=prepared_image.height,
                width=prepared_image.width,
                strength=render_strength,
                num_inference_steps=num_steps,
                guidance_scale=guidance,
                controlnet_conditioning_scale=[canny_scale, depth_scale],
                clip_skip=2,
                generator=generator,
            ).images[0]
            
            # PASS 2: Tiled Super-Resolution & Detail Refinement (2048px)
            print(f"{COLOR_BLUE}✨ Stage 2: Tiled Super-Resolution Refinement (Target: {self.target_long_side}px)...{COLOR_RESET}")
            
            # 1. Upscale Pass 1 result to target resolution
            full_res_image = self.resize_for_delivery(rendered)
            width, height = full_res_image.size
            
            # 2. Refiner prompt
            refiner_prompt = (
                "extreme 8k resolution, raw photorealistic texture, sharp lens clarity, "
                "fine material grain, ultra-detailed fabric and wood grain, professional architectural photo"
            )
            
            # 3. Tiling Logic (Split into overlapping tiles to fit in 4GB VRAM)
            tile_size = 640  # Reduced for 4GB stability
            overlap = 96     # Sufficient for blending
            
            # Create a blank canvas for the result and a weights canvas for blending
            result_np = np.zeros((height, width, 3), dtype=np.float32)
            weights_np = np.zeros((height, width, 3), dtype=np.float32)
            
            # Create a linear feathering mask for the tile
            mask = np.ones((tile_size, tile_size, 3), dtype=np.float32)
            for i in range(overlap):
                val = i / overlap
                mask[i, :, :] *= val
                mask[tile_size-1-i, :, :] *= val
                mask[:, i, :] *= val
                mask[:, tile_size-1-i, :] *= val
            
            # Aggressive VRAM cleanup before starting tiling
            torch.cuda.empty_cache()
            
            # Iterate through tiles
            for y in range(0, height - overlap, tile_size - overlap):
                for x in range(0, width - overlap, tile_size - overlap):
                    # Ensure we don't go out of bounds
                    y_end = min(y + tile_size, height)
                    x_end = min(x + tile_size, width)
                    y_start = max(0, y_end - tile_size)
                    x_start = max(0, x_end - tile_size)
                    
                    tile = full_res_image.crop((x_start, y_start, x_end, y_end))
                    print(f"    ↳ Processing tile at ({x_start}, {y_start})...")
                    
                    # Render tile
                    with torch.cuda.amp.autocast():
                        tile_controls = self.prepare_control_images(tile, thresholds=scene_params)
                        rendered_tile = pipeline(
                            prompt=refiner_prompt,
                            negative_prompt=current_negative,
                            image=tile,
                            control_image=tile_controls,
                            height=tile.height,
                            width=tile.width,
                            strength=tile_strength,
                            num_inference_steps=20, 
                            guidance_scale=7.0,
                            controlnet_conditioning_scale=[tile_canny_scale, tile_depth_scale],
                            clip_skip=2,
                            generator=generator,
                        ).images[0]
                    
                    # Blend tile into result
                    tile_np = np.array(rendered_tile).astype(np.float32)
                    
                    # Adjust mask if tile is smaller (at edges)
                    current_mask = mask[:tile_np.shape[0], :tile_np.shape[1], :]
                    
                    result_np[y_start:y_end, x_start:x_end, :] += tile_np * current_mask
                    weights_np[y_start:y_end, x_start:x_end, :] += current_mask
                    
                    # Clear cache to keep VRAM free
                    torch.cuda.empty_cache()
            
            # Normalize and convert back to PIL
            final_np = np.divide(result_np, weights_np, out=np.zeros_like(result_np), where=weights_np != 0)
            rendered = Image.fromarray(np.clip(final_np, 0, 255).astype(np.uint8))
            
        return self.resize_for_delivery(self.postprocess_image(rendered))

    def upload_to_blob(self, file_path: str, blob_container: Optional[str] = None) -> Optional[str]:
        # Implementation moved from main.py
        container_name = (
            blob_container
            or os.getenv("AI_AZURE_CONTAINER_NAME", "").strip()
            or os.getenv("AI_RENDER_BLOB_CONTAINER", "").strip()
            or os.getenv("AZURE_CONTAINER_NAME", "render-images")
        )
        conn_str = self.storage_connection_string

        if conn_str:
            try:
                from azure.storage.blob import BlobServiceClient, ContentSettings
            except ImportError as e:
                print(f"{COLOR_YELLOW}⚠️  Azure SDK not installed: {e}. Falling back to main upload if available.{COLOR_RESET}")
            else:
                try:
                    filename = os.path.basename(file_path)
                    with open(file_path, "rb") as f:
                        data = f.read()
                    blob_service_client = BlobServiceClient.from_connection_string(conn_str)
                    blob_client = blob_service_client.get_blob_client(container=container_name, blob=filename)
                    blob_client.upload_blob(data, overwrite=True, content_settings=ContentSettings(content_type="image/png"))
                    return blob_client.url
                except Exception as e:
                    print(f"{COLOR_YELLOW}⚠️  Azure SDK upload failed: {e}.{COLOR_RESET}")

        return None

    def save_failed_input(self, job_data: dict, error_msg: str, attempt: int):
        try:
            user_id = job_data.get('user_id', 'unknown')
            project_id = job_data.get('project_id', 'unknown')
            gallery_id = job_data.get('gallery_id', 'unknown')
            job_id = job_data.get('job_id', 'unknown')
            job_type = job_data.get('type', 'unknown')
            timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')

            base_name = f"user_{user_id}_project_{project_id}_{timestamp_str}"
            input_json_path = self.failed_inputs_dir / f"{base_name}_input.json"
            
            payload_obj = job_data.get('payload')
            payload_dict = {}
            if payload_obj is not None:
                try:
                    payload_dict = payload_obj.dict() if hasattr(payload_obj, 'dict') else payload_obj
                except Exception:
                    payload_dict = str(payload_obj)
            
            with open(input_json_path, 'w', encoding='utf-8') as f:
                json.dump(payload_dict, f, indent=2, ensure_ascii=False, default=str)

            error_txt_path = self.failed_inputs_dir / f"{base_name}_error.txt"
            with open(error_txt_path, 'w', encoding='utf-8') as f:
                f.write(f"Timestamp      : {datetime.now().isoformat()}\n")
                f.write(f"Job ID         : {job_id}\n")
                f.write(f"Error          : {error_msg}\n")
                f.write(f"Attempt        : {attempt}\n")

            print(f"{COLOR_YELLOW}📁 Failed input saved to: {self.failed_inputs_dir}{COLOR_RESET}")
        except Exception as save_err:
            print(f"{COLOR_RED}⚠️  Could not save failed input: {save_err}{COLOR_RESET}")

    def background_task_main(self, job_data: dict):
        job_id = job_data["job_id"]
        try:
            with Image.open(job_data["input_path"]) as source_image:
                source_image = source_image.convert("RGB")
                size_info = self._build_job_size_info(job_data, source_image)

                with self.queue_lock:
                    if job_id in self.active_jobs:
                        self.active_jobs[job_id]["status"] = "processing"
                        self.active_jobs[job_id]["progress"] = 20
                        self.active_jobs[job_id]["render_type"] = self.status_render_type
                        self.active_jobs[job_id]["type"] = "ai render"
                        self.active_jobs[job_id]["current_step"] = "rendering"
                        self.active_jobs[job_id]["started_at"] = datetime.now().isoformat()
                        self.active_jobs[job_id].update(size_info)
                
                self.send_status_update(
                    project_id=job_data["project_id"],
                    gallery_id=job_data["gallery_id"],
                    user_id=job_data["user_id"],
                    job_id=job_id,
                    status="processing",
                    step="rendering",
                    progress=20,
                    message="AI render is processing your image.",
                    render_type=self.status_render_type,
                    details={**size_info, "input_filename": job_data.get("filename")},
                    wait_for_delivery=True
                )
                
                rendered_image = self.render_image(source_image)
            rendered_image.save(job_data["output_path"], format="PNG")
            file_size_info = self._get_file_size_info(job_data["output_path"])
            size_info.update(file_size_info)
            
            with self.queue_lock:
                if job_id in self.active_jobs:
                    self.active_jobs[job_id]["progress"] = 80
                    self.active_jobs[job_id].update(size_info)
            
            self.send_status_update(
                project_id=job_data["project_id"],
                gallery_id=job_data["gallery_id"],
                user_id=job_data["user_id"],
                job_id=job_id,
                status="processing",
                step="uploading",
                progress=80,
                message="Uploading rendered image.",
                render_type=self.status_render_type,
                details={
                    **size_info,
                    "output_filename": os.path.basename(job_data["output_path"]),
                },
                wait_for_delivery=True
            )
            
            blob_url = self.upload_to_blob(job_data["output_path"], blob_container=self.blob_container)
            if not blob_url and hasattr(self, 'upload_to_blob_fallback') and self.upload_to_blob_fallback:
                # Optional fallback to main.py's upload function if SAS is needed
                blob_url = self.upload_to_blob_fallback(job_data["output_path"], blob_container=self.blob_container)
                
            if not blob_url: raise RuntimeError("Blob upload failed")
            
            with self.queue_lock:
                if job_id in self.active_jobs:
                    self.active_jobs[job_id]["status"] = "completed"
                    self.active_jobs[job_id]["progress"] = 100
                    self.active_jobs[job_id]["image_url"] = blob_url
                    self.active_jobs[job_id]["current_step"] = "completed"
                    self.active_jobs[job_id]["completed_at"] = datetime.now().isoformat()
                    self.active_jobs[job_id].update(size_info)
            
            self.send_status_update(
                project_id=job_data["project_id"],
                gallery_id=job_data["gallery_id"],
                user_id=job_data["user_id"],
                job_id=job_id,
                status="completed",
                step="completed",
                progress=100,
                message="AI render completed successfully.",
                blob_url=blob_url,
                render_type=self.status_render_type,
                details={
                    **size_info,
                    "image_url": blob_url,
                    "output_filename": os.path.basename(job_data["output_path"]),
                    "render_type": self.status_render_type,
                },
                wait_for_delivery=True,
            )
        except Exception as e:
            error_msg = str(e)
            with self.queue_lock:
                if job_id in self.active_jobs:
                    self.active_jobs[job_id]["status"] = "failed"
                    self.active_jobs[job_id]["error"] = error_msg
                    self.active_jobs[job_id]["current_step"] = "failed"
                    self.active_jobs[job_id]["failed_at"] = datetime.now().isoformat()
                    if "size_info" in locals():
                        self.active_jobs[job_id].update(size_info)
            
            self.send_status_update(
                project_id=job_data["project_id"],
                gallery_id=job_data["gallery_id"],
                user_id=job_data["user_id"],
                job_id=job_id,
                status="error",
                step="error",
                progress=0,
                message=f"AI render failed: {error_msg}",
                render_type=self.status_render_type,
                details={**size_info} if "size_info" in locals() else None,
                wait_for_delivery=True
            )

    def worker_loop(self):
        print(f"{COLOR_GREEN}AI Render Worker Started{COLOR_RESET}")
        while not self.is_shutting_down:
            try:
                job_data = self.render_queue.get(timeout=1)
                if job_data is None: break
                self.background_task_main(job_data)
                self.render_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"{COLOR_RED}AI worker error: {e}{COLOR_RESET}")

    def ensure_worker_started(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return
        self.worker_thread = Thread(target=self.worker_loop, daemon=True, name="ai-render-worker")
        self.worker_thread.start()

    def shutdown(self):
        self.is_shutting_down = True
        try:
            self.render_queue.put(None)
        except:
            pass

# Create a singleton instance
# AIRenderService will be initialized in main.py
ai_render_service = None

def get_ai_render_service(base_dir: Path = None) -> AIRenderService:
    global ai_render_service
    if ai_render_service is None:
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent.parent
        ai_render_service = AIRenderService(base_dir)
    return ai_render_service



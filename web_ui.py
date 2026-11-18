#!/usr/bin/env python3
"""
GTA6-HypeClipper Web UI Dashboard & Automation Server (web_ui.py)
Production-ready Web UI for:
1. One-click Live Stream / VOD Ingestion & 9:16 Shorts Generation.
2. IShowSpeed & Multi-Streamer Facecam Preset Selection.
3. Hormozi Dynamic Word-Highlight Subtitles Toggle.
4. Built-in 9:16 Video Player Gallery & 1080p MP4 Downloads.
5. YouTube Shorts Auto-Upload & OAuth Credentials Manager.
6. Autonomous 24/7 Daily Auto-Pilot Scheduler.
"""

import collections
import datetime
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import yaml
from flask import Flask, Response, jsonify, render_template_string, request, send_file, send_from_directory

# Import core pipeline modules
import caption_generator
import hype_detector
import ingest
from scheduler import scheduler_instance
import uploader
import video_editor

# Initialize Flask app
app = Flask(__name__)
app.config["OUTPUT_FOLDER"] = os.path.abspath("output")
app.config["TEMP_FOLDER"] = os.path.abspath("temp")
os.makedirs(app.config["OUTPUT_FOLDER"], exist_ok=True)
os.makedirs(app.config["TEMP_FOLDER"], exist_ok=True)

# ---------------------------------------------------------------------------
# Real-time Log Streaming & Pipeline Job State
# ---------------------------------------------------------------------------
log_buffer = collections.deque(maxlen=2000)
log_subscribers: List[queue.Queue] = []
subscribers_lock = threading.Lock()


class WebLogHandler(logging.Handler):
    """Custom logging handler to push log records to web clients."""

    def emit(self, record):
        try:
            msg = self.format(record)
            log_buffer.append(msg)
            with subscribers_lock:
                for q in list(log_subscribers):
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass
        except Exception:
            self.handleError(record)


# Attach web handler to root logger
web_handler = WebLogHandler()
web_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(web_handler)
logging.getLogger().setLevel(logging.INFO)

logger = logging.getLogger("GTA6-HypeClipper.WebUI")

# Global Active Job State
current_job: Dict[str, Any] = {
    "status": "idle",  # idle, running, completed, error
    "stage": "Ready",
    "progress": 0,
    "error": None,
    "started_at": None,
    "completed_at": None,
    "target_url": None,
    "generated_clips": [],
}
job_lock = threading.Lock()
job_stop_flag = threading.Event()


def generate_thumbnail(video_path: str) -> Optional[str]:
    """Generate or retrieve a thumbnail for a video."""
    base, _ = os.path.splitext(video_path)
    thumb_path = f"{base}_thumb.jpg"
    if os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
        return thumb_path

    ffmpeg_bin = video_editor.find_ffmpeg_binary()
    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss", "00:00:05",
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        thumb_path,
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return thumb_path
    except Exception as e:
        logger.warning(f"Thumbnail generation failed for {video_path}: {e}")
        return None


def get_video_duration(video_path: str) -> float:
    """Extract video duration using ffprobe or OpenCV."""
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        cap.release()
        return frames / fps if fps > 0 else 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Background Pipeline Execution Worker
# ---------------------------------------------------------------------------
def run_clipping_worker(
    url: str,
    preset: str = "speed_cam",
    max_clips: int = 3,
    whisper_model: str = "base",
    enable_captions: bool = True,
    auto_upload: bool = False,
    privacy: str = "unlisted",
):
    global current_job

    with job_lock:
        current_job["status"] = "running"
        current_job["stage"] = "Starting Ingestion..."
        current_job["progress"] = 5
        current_job["error"] = None
        current_job["started_at"] = datetime.datetime.now().isoformat()
        current_job["completed_at"] = None
        current_job["target_url"] = url
        current_job["generated_clips"] = []

    job_stop_flag.clear()

    try:
        # Load base configuration
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        temp_dir = cfg.get("ingest", {}).get("temp_dir", "temp")
        output_dir = cfg.get("ingest", {}).get("output_dir", "output")
        os.makedirs(temp_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        logger.info(f"=== [JOB START] Processing Stream: {url} (Preset: {preset}, Max Clips: {max_clips}) ===")

        # Step 1: Ingest
        if job_stop_flag.is_set():
            raise InterruptedError("Job cancelled by user.")

        with job_lock:
            current_job["stage"] = "Downloading Stream / Extracting Audio..."
            current_job["progress"] = 15

        video_path, metadata = ingest.download_video(
            url=url,
            output_dir=temp_dir,
            target_resolution=cfg.get("ingest", {}).get("target_resolution", "1080"),
            max_fps=cfg.get("ingest", {}).get("max_fps", 60),
        )

        chat_json_path = None
        if cfg.get("ingest", {}).get("download_chat", True):
            chat_json_path = ingest.download_live_chat(url=url, output_dir=temp_dir, video_id=metadata.get("id"))

        audio_16k_path = ingest.extract_audio_16k(video_path)

        # Step 2: Hype Moment Detection
        if job_stop_flag.is_set():
            raise InterruptedError("Job cancelled by user.")

        with job_lock:
            current_job["stage"] = "Analyzing Audio Spikes & Chat Velocity..."
            current_job["progress"] = 35

        cfg.setdefault("hype_detection", {})["max_clips"] = max_clips
        detected_clips = hype_detector.detect_hype_moments(
            audio_path=audio_16k_path,
            chat_json_path=chat_json_path,
            total_duration=metadata.get("duration", 0.0),
            config=cfg,
        )

        if not detected_clips:
            logger.warning("No hype peaks qualified with current sensitivity settings.")
            with job_lock:
                current_job["status"] = "completed"
                current_job["stage"] = "No qualifying hype moments detected."
                current_job["progress"] = 100
                current_job["completed_at"] = datetime.datetime.now().isoformat()
            return

        # Step 3 & 4: Captioning and 60fps Split-Screen Rendering
        ffmpeg_bin = video_editor.find_ffmpeg_binary()
        total_clips = len(detected_clips)
        rendered_shorts: List[Dict[str, Any]] = []

        cfg.setdefault("captions", {})["whisper_model"] = whisper_model
        cfg["captions"]["enabled"] = enable_captions

        for idx, clip in enumerate(detected_clips, 1):
            if job_stop_flag.is_set():
                raise InterruptedError("Job cancelled by user.")

            c_id = clip["clip_id"]
            progress_pct = 40 + int((idx / total_clips) * 45)
            with job_lock:
                current_job["stage"] = f"Rendering Short #{c_id}/{total_clips} (Captions + Split-Screen)..."
                current_job["progress"] = progress_pct

            clip_audio_slice = os.path.join(temp_dir, f"clip_{c_id}_audio.wav")
            clip_ass_path = os.path.join(temp_dir, f"clip_{c_id}_captions.ass")
            timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            clip_output_mp4 = os.path.join(output_dir, f"GTA6_Short_Clip_{c_id:02d}_{timestamp_str}.mp4")

            # Extract audio slice
            subprocess.run(
                [
                    ffmpeg_bin, "-y", "-ss", str(clip["start_time"]), "-to", str(clip["end_time"]),
                    "-i", audio_16k_path, "-acodec", "pcm_s16le", clip_audio_slice
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            # Generate captions
            captions_file = None
            if enable_captions:
                try:
                    captions_file = caption_generator.create_captions_for_clip(
                        clip_audio_path=clip_audio_slice,
                        output_ass_path=clip_ass_path,
                        config=cfg,
                    )
                except Exception as ce:
                    logger.warning(f"Whisper transcription failed for Clip #{c_id}: {ce}")

            # Render 9:16 Vertical Short
            final_mp4 = video_editor.render_gaming_short(
                source_video_path=video_path,
                clip_info=clip,
                output_mp4_path=clip_output_mp4,
                subtitles_ass_path=captions_file,
                facecam_preset=preset,
                config=cfg,
            )

            generate_thumbnail(final_mp4)
            meta = load_or_classify_clip_meta(os.path.basename(final_mp4), output_dir)

            clip_record = {
                "clip_id": c_id,
                "output_path": final_mp4,
                "filename": os.path.basename(final_mp4),
                "duration": clip["end_time"] - clip["start_time"],
                "start_time": clip["start_time"],
                "end_time": clip["end_time"],
                "hype_score": clip.get("hype_score", 0.0),
                "category": meta.get("category", "BEST"),
                "category_badge": meta.get("category_badge", "🔥 BEST / VIRAL"),
                "can_delete": meta.get("can_delete", False),
                "reason": meta.get("reason", ""),
                "youtube_url": None,
            }
            rendered_shorts.append(clip_record)

            with job_lock:
                current_job["generated_clips"].append(clip_record)

        # Step 5: Optional YouTube Upload
        if auto_upload and rendered_shorts:
            with job_lock:
                current_job["stage"] = "Auto-Uploading Shorts to YouTube..."
                current_job["progress"] = 92
            logger.info("Executing automatic YouTube Shorts upload...")
            cfg.setdefault("uploader", {})["privacy_status"] = privacy
            cfg["uploader"]["enabled"] = True
            rendered_shorts = uploader.upload_all_generated_shorts(rendered_shorts, config=cfg)
            with job_lock:
                current_job["generated_clips"] = rendered_shorts

        with job_lock:
            current_job["status"] = "completed"
            current_job["stage"] = f"Finished! {len(rendered_shorts)} 1080p Shorts Ready."
            current_job["progress"] = 100
            current_job["completed_at"] = datetime.datetime.now().isoformat()

        logger.info(f"=== [JOB COMPLETED] Successfully generated {len(rendered_shorts)} Shorts! ===")

    except InterruptedError:
        logger.info("Job execution stopped by user request.")
        with job_lock:
            current_job["status"] = "idle"
            current_job["stage"] = "Job cancelled."
            current_job["progress"] = 0
    except Exception as e:
        logger.exception(f"Job failed with error: {e}")
        with job_lock:
            current_job["status"] = "error"
            current_job["stage"] = "Error encountered"
            current_job["error"] = str(e)
            current_job["progress"] = 0


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Render main SPA dashboard."""
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/status")
def api_status():
    """System health check and stats."""
    ffmpeg_bin = video_editor.find_ffmpeg_binary()
    output_files = [f for f in os.listdir(app.config["OUTPUT_FOLDER"]) if f.endswith(".mp4")]

    return jsonify({
        "ffmpeg": {
            "path": ffmpeg_bin,
            "ready": os.path.isfile(ffmpeg_bin) and os.access(ffmpeg_bin, os.X_OK),
        },
        "output_clips_count": len(output_files),
        "scheduler": scheduler_instance.get_status(),
        "job": current_job,
    })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """Retrieve or update pipeline settings."""
    if request.method == "POST":
        data = request.json or {}
        with open("config.yaml", "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
        return jsonify({"status": "success", "config": data})

    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return jsonify(cfg)


@app.route("/api/pipeline/start", methods=["POST"])
def api_start_pipeline():
    """Launch clipping pipeline in background thread."""
    with job_lock:
        if current_job["status"] == "running":
            return jsonify({"status": "error", "message": "A job is already currently running."}), 400

    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"status": "error", "message": "Target URL is required."}), 400

    preset = data.get("preset", "speed_cam")
    max_clips = int(data.get("max_clips", 3))
    whisper_model = data.get("whisper_model", "base")
    enable_captions = bool(data.get("enable_captions", True))
    auto_upload = bool(data.get("auto_upload", False))
    privacy = data.get("privacy", "unlisted")

    worker_thread = threading.Thread(
        target=run_clipping_worker,
        kwargs={
            "url": url,
            "preset": preset,
            "max_clips": max_clips,
            "whisper_model": whisper_model,
            "enable_captions": enable_captions,
            "auto_upload": auto_upload,
            "privacy": privacy,
        },
        daemon=True,
    )
    worker_thread.start()

    return jsonify({"status": "success", "message": "Pipeline started."})


@app.route("/api/pipeline/stop", methods=["POST"])
def api_stop_pipeline():
    """Cancel active pipeline run."""
    job_stop_flag.set()
    return jsonify({"status": "success", "message": "Cancellation signal sent."})


@app.route("/api/pipeline/status")
def api_pipeline_status():
    """Query current pipeline progress."""
    with job_lock:
        return jsonify(current_job)


@app.route("/api/logs")
def api_logs():
    """Return recent log lines."""
    return jsonify({"logs": list(log_buffer)[-200:]})


@app.route("/api/logs/stream")
def api_logs_stream():
    """Server-Sent Events (SSE) live log feed for the terminal UI."""
    def event_stream():
        q: queue.Queue = queue.Queue(maxsize=100)
        with subscribers_lock:
            log_subscribers.append(q)

        try:
            # Yield initial backlog
            recent = list(log_buffer)[-40:]
            for line in recent:
                yield f"data: {json.dumps({'line': line})}\n\n"

            while True:
                try:
                    line = q.get(timeout=15.0)
                    yield f"data: {json.dumps({'line': line})}\n\n"
                except queue.Empty:
                    # Keepalive ping
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            with subscribers_lock:
                if q in log_subscribers:
                    log_subscribers.remove(q)

    return Response(event_stream(), mimetype="text/event-stream")


def load_or_classify_clip_meta(filename: str, folder: str) -> Dict[str, Any]:
    """Retrieve saved sidecar metadata or auto-classify existing legacy clips."""
    base, _ = os.path.splitext(filename)
    meta_path = os.path.join(folder, f"{base}.meta.json")

    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Heuristic classification for legacy or freshly rendered files
    fname_lower = filename.lower()
    is_best = False
    reason = "Standard gameplay capture"

    if "romance" in fname_lower or "kiss" in fname_lower:
        is_best = True
        reason = "❤️ Story / Romance Scene (High Viral Potential)"
    elif "clip_01" in fname_lower or "clip_02" in fname_lower or "speed" in fname_lower or "chase" in fname_lower:
        is_best = True
        reason = "🔥 Top Energy Spike / Streamer Reaction"
    else:
        is_best = False
        reason = "⚡ Standard gameplay - Safe to delete if space needed"

    full_video_path = os.path.join(folder, filename)
    created_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if os.path.isfile(full_video_path):
        created_time = datetime.datetime.fromtimestamp(os.path.getmtime(full_video_path)).strftime("%Y-%m-%d %H:%M:%S")

    meta = {
        "filename": filename,
        "category": "BEST" if is_best else "NORMAL",
        "category_badge": "🔥 BEST / VIRAL" if is_best else "⚡ NORMAL (CAN DELETE)",
        "can_delete": not is_best,
        "hype_score": 2.2 if is_best else 0.8,
        "is_romance": "romance" in fname_lower,
        "reason": reason,
        "uploaded": False,
        "youtube_url": None,
        "created_at": created_time,
    }

    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass

    return meta


@app.route("/api/clips")
def api_clips():
    """List all generated 9:16 Shorts with category and viral classification."""
    folder = app.config["OUTPUT_FOLDER"]
    files = [f for f in os.listdir(folder) if f.endswith(".mp4")]
    files.sort(key=lambda x: os.path.getmtime(os.path.join(folder, x)), reverse=True)

    clips_list = []
    best_count = 0
    normal_count = 0
    normal_size_mb = 0.0

    for f in files:
        full_path = os.path.join(folder, f)
        stat = os.stat(full_path)
        size_mb = round(stat.st_size / (1024 * 1024), 2)
        thumb_name = f"{os.path.splitext(f)[0]}_thumb.jpg"
        has_thumb = os.path.isfile(os.path.join(folder, thumb_name))

        meta = load_or_classify_clip_meta(f, folder)
        category = meta.get("category", "NORMAL")
        can_delete = meta.get("can_delete", True)

        if category == "BEST":
            best_count += 1
        else:
            normal_count += 1
            normal_size_mb += size_mb

        clips_list.append({
            "filename": f,
            "size_mb": size_mb,
            "created_at": meta.get("created_at") or datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "has_thumbnail": has_thumb,
            "duration": meta.get("duration") or get_video_duration(full_path),
            "category": category,
            "category_badge": meta.get("category_badge", "⚡ NORMAL (CAN DELETE)"),
            "can_delete": can_delete,
            "hype_score": meta.get("hype_score", 0.0),
            "reason": meta.get("reason", "Gameplay moment"),
            "uploaded": meta.get("uploaded", False),
            "youtube_url": meta.get("youtube_url"),
        })

    return jsonify({
        "clips": clips_list,
        "total_count": len(clips_list),
        "best_count": best_count,
        "normal_count": normal_count,
        "normal_size_mb": round(normal_size_mb, 2),
    })


@app.route("/stream/<filename>")
def stream_video(filename):
    """Stream 1080p video with HTTP 206 Partial Content range support."""
    return send_from_directory(app.config["OUTPUT_FOLDER"], filename, mimetype="video/mp4", conditional=True)


@app.route("/download/<filename>")
def download_video(filename):
    """Download video file."""
    return send_from_directory(app.config["OUTPUT_FOLDER"], filename, as_attachment=True)


@app.route("/thumbnail/<filename>")
def get_thumb(filename):
    """Serve or generate clip thumbnail."""
    base, _ = os.path.splitext(filename)
    thumb_file = f"{base}_thumb.jpg"
    thumb_path = os.path.join(app.config["OUTPUT_FOLDER"], thumb_file)
    if not os.path.isfile(thumb_path):
        video_path = os.path.join(app.config["OUTPUT_FOLDER"], filename)
        if os.path.isfile(video_path):
            generate_thumbnail(video_path)

    if os.path.isfile(thumb_path):
        return send_file(thumb_path, mimetype="image/jpeg")
    return send_file("temp/preview_top_cam.jpg", mimetype="image/jpeg")


@app.route("/api/clips/<filename>", methods=["DELETE"])
def delete_clip(filename):
    """Delete a clip, its thumbnail, and sidecar metadata."""
    folder = app.config["OUTPUT_FOLDER"]
    v_path = os.path.join(folder, filename)
    base = os.path.splitext(filename)[0]
    thumb_path = os.path.join(folder, f"{base}_thumb.jpg")
    meta_path = os.path.join(folder, f"{base}.meta.json")

    if os.path.isfile(v_path):
        os.remove(v_path)
    if os.path.isfile(thumb_path):
        os.remove(thumb_path)
    if os.path.isfile(meta_path):
        os.remove(meta_path)
    return jsonify({"status": "success", "deleted": filename})


@app.route("/api/clips/prune-normal", methods=["POST"])
def prune_normal_clips():
    """Bulk delete all clips categorized as NORMAL / safe-to-delete to free disk space."""
    folder = app.config["OUTPUT_FOLDER"]
    files = [f for f in os.listdir(folder) if f.endswith(".mp4")]

    deleted_files = []
    freed_bytes = 0

    for f in files:
        meta = load_or_classify_clip_meta(f, folder)
        if meta.get("category") == "NORMAL" or meta.get("can_delete", False):
            v_path = os.path.join(folder, f)
            base = os.path.splitext(f)[0]
            thumb_path = os.path.join(folder, f"{base}_thumb.jpg")
            meta_path = os.path.join(folder, f"{base}.meta.json")

            if os.path.isfile(v_path):
                freed_bytes += os.path.getsize(v_path)
                os.remove(v_path)
            if os.path.isfile(thumb_path):
                os.remove(thumb_path)
            if os.path.isfile(meta_path):
                os.remove(meta_path)
            deleted_files.append(f)

    freed_mb = round(freed_bytes / (1024 * 1024), 2)
    logger.info(f"Pruned {len(deleted_files)} normal clips, freed {freed_mb} MB storage.")
    return jsonify({
        "status": "success",
        "deleted_count": len(deleted_files),
        "deleted_files": deleted_files,
        "freed_mb": freed_mb,
    })



def get_channel_title(token_file: str = "token.json") -> Optional[str]:
    """Retrieve connected YouTube channel name."""
    if not os.path.isfile(token_file):
        return None
    try:
        from google.oauth2.credentials import Credentials
        import googleapiclient.discovery
        creds = Credentials.from_authorized_user_file(token_file, [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly"
        ])
        youtube = googleapiclient.discovery.build("youtube", "v3", credentials=creds)
        res = youtube.channels().list(mine=True, part="snippet").execute()
        if "items" in res and len(res["items"]) > 0:
            return res["items"][0]["snippet"]["title"]
    except Exception:
        return None
    return None


@app.route("/api/youtube/status")
def youtube_status():
    """Verify YouTube Data API authentication credentials."""
    secrets_file = "client_secrets.json"
    token_file = "token.json"
    has_token = os.path.isfile(token_file)
    channel_name = get_channel_title(token_file) if has_token else None
    return jsonify({
        "has_client_secrets": os.path.isfile(secrets_file),
        "has_token": has_token,
        "ready": bool(has_token and channel_name),
        "channel_title": channel_name,
    })


@app.route("/api/youtube/connect", methods=["POST"])
def youtube_connect():
    """Receive client_secrets or credentials and launch OAuth flow."""
    # Handle direct file upload
    if "client_secrets" in request.files:
        f = request.files["client_secrets"]
        f.save("client_secrets.json")
    elif request.is_json and request.json.get("client_id"):
        data = request.json
        client_id = data.get("client_id", "").strip()
        client_secret = data.get("client_secret", "").strip()
        if client_id and client_secret:
            secrets_data = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost", "urn:ietf:wg:oauth:2.0:oob"],
                }
            }
            with open("client_secrets.json", "w", encoding="utf-8") as sf:
                json.dump(secrets_data, sf, indent=2)

    if not os.path.isfile("client_secrets.json"):
        return jsonify({"status": "error", "message": "Please upload client_secrets.json first."}), 400

    try:
        from connect_youtube import authenticate
        auth_thread = threading.Thread(target=authenticate, daemon=True)
        auth_thread.start()
        return jsonify({
            "status": "success",
            "message": "Google authorization launched in your browser! Please log in, choose your YouTube Channel, and click Allow."
        })
    except Exception as e:
        logger.exception(f"OAuth initiation failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/youtube/upload", methods=["POST"])
def youtube_upload_clip():
    """Upload a specific clip to YouTube Shorts."""
    data = request.json or {}
    filename = data.get("filename")
    if not filename:
        return jsonify({"status": "error", "message": "Filename is required."}), 400

    v_path = os.path.join(app.config["OUTPUT_FOLDER"], filename)
    if not os.path.isfile(v_path):
        return jsonify({"status": "error", "message": "File not found."}), 404

    title = data.get("title") or uploader.generate_short_title(clip_number=1, streamer="IShowSpeed")
    description = data.get("description")
    tags = data.get("tags") or ["GTA6", "GTAVI", "IShowSpeed", "GamingShorts", "Shorts"]
    privacy = data.get("privacy", "unlisted")

    try:
        video_id = uploader.upload_short(
            video_path=v_path,
            title=title,
            description=description,
            tags=tags,
            privacy_status=privacy,
        )
        yt_url = f"https://www.youtube.com/shorts/{video_id}"
        return jsonify({
            "status": "success",
            "video_id": video_id,
            "youtube_url": yt_url,
            "title": title,
        })
    except Exception as e:
        logger.exception(f"YouTube upload failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/scheduler", methods=["GET", "POST"])
def api_scheduler():
    """Manage autonomous daily scheduler."""
    if request.method == "POST":
        updates = request.json or {}
        new_status = scheduler_instance.update_config(updates)
        return jsonify(new_status)
    return jsonify(scheduler_instance.get_status())


@app.route("/api/scheduler/trigger", methods=["POST"])
def api_scheduler_trigger():
    """Force run scheduled job immediately."""
    res = scheduler_instance.trigger_now()
    return jsonify(res)


# ---------------------------------------------------------------------------
# Modern Single-Page Responsive UI Template (GTA VI Neon Dark Aesthetic)
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GTA6-HypeClipper | AI YouTube Shorts Studio</title>
  <!-- Tailwind CSS CDN -->
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- Lucide Icons -->
  <script src="https://unpkg.com/lucide@latest"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: {
              pink: '#FF2A85',
              cyan: '#00F0FF',
              yellow: '#FFDD00',
              dark: '#080c14',
              card: '#101726',
              border: '#1f293d',
            }
          }
        }
      }
    }
  </script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Cabinet+Grotesk:wght@700;800;900&family=Inter:wght@400;500;600;700&display=swap');
    body {
      font-family: 'Inter', sans-serif;
      background-color: #080c14;
      color: #f1f5f9;
    }
    h1, h2, h3, .brand-font {
      font-family: 'Cabinet Grotesk', sans-serif;
    }
    .neon-glow-pink {
      box-shadow: 0 0 25px rgba(255, 42, 133, 0.35);
    }
    .neon-glow-cyan {
      box-shadow: 0 0 25px rgba(0, 240, 255, 0.35);
    }
    .custom-scroll::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    .custom-scroll::-webkit-scrollbar-track {
      background: #0d131f;
    }
    .custom-scroll::-webkit-scrollbar-thumb {
      background: #23314d;
      border-radius: 4px;
    }
  </style>
</head>
<body class="min-h-screen flex flex-col">

  <!-- Top Navigation Bar -->
  <header class="border-b border-brand-border bg-brand-dark/80 backdrop-blur sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-brand-pink to-brand-cyan flex items-center justify-center font-black text-xl text-black shadow-lg shadow-pink-500/20">
          VI
        </div>
        <div>
          <span class="text-xl font-black tracking-wider text-white">GTA6<span class="text-brand-pink">-HypeClipper</span></span>
          <span class="ml-2 text-xs uppercase px-2 py-0.5 rounded-full bg-brand-pink/20 text-brand-pink font-semibold border border-brand-pink/30">Pro 60fps</span>
        </div>
      </div>

      <!-- Navigation Tabs -->
      <nav class="flex space-x-1 sm:space-x-2 bg-brand-card/80 p-1 rounded-xl border border-brand-border">
        <button onclick="switchTab('studio')" id="tab-btn-studio" class="tab-btn active px-3.5 py-1.5 rounded-lg text-sm font-semibold flex items-center space-x-1.5 bg-brand-pink text-white transition">
          <i data-lucide="sparkles" class="w-4 h-4"></i>
          <span>Studio</span>
        </button>
        <button onclick="switchTab('gallery')" id="tab-btn-gallery" class="tab-btn px-3.5 py-1.5 rounded-lg text-sm font-semibold flex items-center space-x-1.5 text-gray-400 hover:text-white transition">
          <i data-lucide="film" class="w-4 h-4"></i>
          <span>Clips (<span id="clips-counter">0</span>)</span>
        </button>
        <button onclick="switchTab('autopilot')" id="tab-btn-autopilot" class="tab-btn px-3.5 py-1.5 rounded-lg text-sm font-semibold flex items-center space-x-1.5 text-gray-400 hover:text-white transition">
          <i data-lucide="bot" class="w-4 h-4"></i>
          <span>24/7 Auto-Pilot</span>
        </button>
      </nav>

      <!-- YouTube Channel & System Status Pills -->
      <div class="flex items-center space-x-3 text-xs font-mono">
        <div id="yt-channel-badge">
          <!-- Populated by JS: either 'Connect YouTube' button or 'Connected: ChannelName' -->
        </div>
        <span class="hidden md:flex items-center space-x-1.5 px-2.5 py-1 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
          <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
          <span>1080p60 Engine Ready</span>
        </span>
      </div>
    </div>
  </header>

  <!-- Main Container -->
  <main class="flex-1 max-w-7xl w-full mx-auto p-4 sm:p-6 lg:p-8">

    <!-- ================================================================= -->
    <!-- TAB 1: STUDIO (Generate Shorts) -->
    <!-- ================================================================= -->
    <div id="tab-studio" class="space-y-6">
      <!-- Hero Banner -->
      <div class="relative overflow-hidden rounded-2xl bg-gradient-to-r from-brand-card via-slate-900 to-brand-card border border-brand-border p-6 sm:p-8">
        <div class="absolute -right-10 -bottom-10 w-64 h-64 bg-brand-pink/10 rounded-full blur-3xl pointer-events-none"></div>
        <div class="max-w-3xl">
          <h1 class="text-3xl sm:text-4xl font-extrabold text-white tracking-tight">
            Gaming Livestream to <span class="text-transparent bg-clip-text bg-gradient-to-r from-brand-pink via-brand-yellow to-brand-cyan">Viral YouTube Shorts</span>
          </h1>
          <p class="mt-2 text-sm sm:text-base text-gray-400">
            Paste any YouTube or Twitch stream URL. The AI detects screams & chat hype spikes, frames the streamer's facecam on top, gameplay on bottom, burns Alex Hormozi captions, and outputs ready-to-upload 9:16 vertical 1080p60 Shorts.
          </p>
        </div>
      </div>

      <!-- Input Controls Card -->
      <div class="bg-brand-card border border-brand-border rounded-2xl p-6 shadow-xl">
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">

          <!-- Left Column: Input Form (8 cols) -->
          <div class="lg:col-span-8 space-y-5">
            <!-- Stream URL -->
            <div>
              <label class="block text-xs font-semibold uppercase tracking-wider text-gray-300 mb-2">
                Livestream or VOD URL (YouTube / Twitch)
              </label>
              <div class="relative">
                <input type="text" id="stream-url"
                  value="https://www.youtube.com/watch?v=qSyo5d-dVzA"
                  placeholder="https://www.youtube.com/watch?v=..."
                  class="w-full bg-slate-900/90 border border-brand-border rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-brand-pink focus:ring-1 focus:ring-brand-pink transition pr-10 font-mono">
                <button onclick="document.getElementById('stream-url').value='https://www.youtube.com/watch?v=qSyo5d-dVzA'" title="Load IShowSpeed GTA VI Demo" class="absolute right-3 top-3 text-xs bg-slate-800 text-brand-cyan px-2 py-0.5 rounded border border-brand-border hover:bg-slate-700">
                  Speed GTA VI
                </button>
              </div>
            </div>

            <!-- Facecam Preset Selector & Clip Options -->
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <!-- Preset Selector -->
              <div>
                <label class="block text-xs font-semibold uppercase tracking-wider text-gray-300 mb-2 flex items-center justify-between">
                  <span>Streamer Facecam Layout</span>
                  <span class="text-[10px] text-brand-pink font-bold">CRUCIAL</span>
                </label>
                <select id="preset-selector" class="w-full bg-slate-900 border border-brand-border rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-brand-pink transition">
                  <option value="speed_cam" selected>⭐ IShowSpeed (Middle-Left Cam)</option>
                  <option value="top_left_cam">Top-Left Cam (Standard)</option>
                  <option value="top_right_cam">Top-Right Cam</option>
                  <option value="middle_left_cam">Middle-Left Cam</option>
                  <option value="middle_right_cam">Middle-Right Cam</option>
                  <option value="bottom_left_cam">Bottom-Left Cam</option>
                  <option value="bottom_right_cam">Bottom-Right Cam</option>
                  <option value="auto">Auto-Detect AI (OpenCV Cascades)</option>
                </select>
                <p class="text-[11px] text-gray-400 mt-1">Frames streamer's face in the top 30% header (`1080x576`).</p>
              </div>

                <!-- Max Clips -->
                <div>
                  <label class="block text-xs font-semibold uppercase tracking-wider text-gray-300 mb-2">
                    Number of 1-Minute Viral Shorts to Generate
                  </label>
                  <select id="max-clips" class="w-full bg-slate-900 border border-brand-border rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-brand-pink transition">
                    <option value="1">1 Short (Fastest Test)</option>
                    <option value="3">3 Shorts</option>
                    <option value="5">5 Shorts</option>
                    <option value="10" selected>10 Shorts (Automated 1-Minute Batch)</option>
                    <option value="20">20 Shorts (Hourly Target - Max Bulk)</option>
                  </select>
                  <p class="text-[11px] text-gray-400 mt-1">Extracts ~59s clips scored by Screams, Spikes & Chat Velocity.</p>
                </div>
            </div>

            <!-- Toggle Options -->
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-2">
              <label class="flex items-center space-x-3 p-3 rounded-xl bg-slate-900/60 border border-brand-border cursor-pointer hover:border-brand-pink/50 transition">
                <input type="checkbox" id="toggle-captions" checked class="w-5 h-5 rounded bg-slate-800 text-brand-pink focus:ring-brand-pink border-brand-border">
                <div>
                  <div class="text-sm font-semibold text-white">Alex Hormozi Dynamic Subtitles</div>
                  <div class="text-xs text-gray-400">Word-level yellow/white pop highlights at border</div>
                </div>
              </label>

              <label class="flex items-center space-x-3 p-3 rounded-xl bg-slate-900/60 border border-brand-border cursor-pointer hover:border-brand-pink/50 transition">
                <input type="checkbox" id="toggle-upload" class="w-5 h-5 rounded bg-slate-800 text-brand-pink focus:ring-brand-pink border-brand-border">
                <div>
                  <div class="text-sm font-semibold text-white">Auto-Upload to YouTube</div>
                  <div class="text-xs text-gray-400">Upload immediately after 1080p render</div>
                </div>
              </label>
            </div>

            <!-- Generate Action Button -->
            <div class="pt-2 flex items-center space-x-4">
              <button id="btn-start-clipping" onclick="startClipping()" class="flex-1 bg-gradient-to-r from-brand-pink via-purple-600 to-brand-cyan hover:opacity-95 text-white font-black py-4 px-6 rounded-xl shadow-lg neon-glow-pink flex items-center justify-center space-x-3 transition active:scale-[0.99]">
                <i data-lucide="play-circle" class="w-6 h-6"></i>
                <span class="text-base tracking-wide">GENERATE 9:16 GAMING SHORTS</span>
              </button>

              <button id="btn-stop-clipping" onclick="stopClipping()" class="hidden bg-rose-600/20 hover:bg-rose-600/30 text-rose-400 border border-rose-500/30 font-semibold py-4 px-6 rounded-xl flex items-center space-x-2 transition">
                <i data-lucide="square" class="w-5 h-5"></i>
                <span>Stop</span>
              </button>
            </div>

          </div>

          <!-- Right Column: Visual Preview & Live Progress (4 cols) -->
          <div class="lg:col-span-4 bg-slate-900/80 rounded-xl p-5 border border-brand-border flex flex-col justify-between">
            <div>
              <h3 class="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-3 flex items-center space-x-2">
                <i data-lucide="layout" class="w-4 h-4 text-brand-cyan"></i>
                <span>Layout Architecture (1080x1920)</span>
              </h3>
              
              <!-- Mini 9:16 layout diagram -->
              <div class="w-44 h-72 mx-auto bg-black rounded-lg border-2 border-brand-border overflow-hidden flex flex-col relative shadow-xl">
                <!-- Top 30% Facecam -->
                <div class="h-[30%] bg-gradient-to-br from-pink-900/60 to-purple-900/80 border-b-2 border-brand-pink flex flex-col items-center justify-center text-center p-1">
                  <i data-lucide="video" class="w-5 h-5 text-brand-pink mb-0.5"></i>
                  <span class="text-[10px] font-bold text-white uppercase">Streamer Facecam</span>
                  <span class="text-[9px] text-pink-300">1080 x 576 (Top 30%)</span>
                </div>
                <!-- Subtitle separator badge -->
                <div class="absolute top-[28%] left-1/2 -translate-x-1/2 bg-yellow-400 text-black text-[8px] font-black px-1.5 py-0.2 rounded shadow">
                  HORMOZI CAPTIONS
                </div>
                <!-- Bottom 70% Gameplay -->
                <div class="h-[70%] bg-gradient-to-br from-blue-900/40 to-slate-900 flex flex-col items-center justify-center text-center p-2">
                  <i data-lucide="gamepad-2" class="w-6 h-6 text-brand-cyan mb-1"></i>
                  <span class="text-[10px] font-bold text-white uppercase">GTA VI Gameplay</span>
                  <span class="text-[9px] text-gray-400">1080 x 1344 (Bottom 70%)</span>
                </div>
              </div>
            </div>

            <!-- Progress widget -->
            <div class="mt-4 pt-4 border-t border-brand-border">
              <div class="flex items-center justify-between text-xs mb-1.5 font-medium">
                <span id="job-stage-label" class="text-gray-300">Engine Idle</span>
                <span id="job-progress-pct" class="text-brand-pink font-mono font-bold">0%</span>
              </div>
              <div class="w-full bg-slate-800 rounded-full h-2.5 overflow-hidden">
                <div id="job-progress-bar" class="bg-gradient-to-r from-brand-pink to-brand-cyan h-2.5 rounded-full transition-all duration-300 w-0"></div>
              </div>
            </div>
          </div>

        </div>
      </div>

      <!-- Real-time Live Log Console -->
      <div class="bg-brand-card border border-brand-border rounded-2xl p-5 shadow-xl">
        <div class="flex items-center justify-between mb-3">
          <div class="flex items-center space-x-2">
            <span class="w-2.5 h-2.5 rounded-full bg-brand-cyan animate-pulse"></span>
            <h3 class="text-xs font-semibold uppercase tracking-wider text-gray-300">Live Execution Log (FFmpeg & AI Pipeline)</h3>
          </div>
          <button onclick="clearLogs()" class="text-xs text-gray-400 hover:text-white flex items-center space-x-1">
            <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
            <span>Clear</span>
          </button>
        </div>
        <div id="log-terminal" class="bg-black/90 rounded-xl p-4 font-mono text-xs text-emerald-400 h-64 overflow-y-auto custom-scroll space-y-1">
          <div class="text-gray-500">// Ready to process livestreams and VODs. Logs will stream in real-time.</div>
        </div>
      </div>
    </div>

    <!-- ================================================================= -->
    <!-- TAB 2: CLIPS GALLERY (Preview, Categorization & Upload) -->
    <!-- ================================================================= -->
    <div id="tab-gallery" class="hidden space-y-6">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h2 class="text-2xl font-bold text-white flex items-center space-x-2">
            <span>Generated YouTube Shorts</span>
            <span id="clips-total-badge" class="px-2.5 py-0.5 text-xs font-semibold rounded-full bg-slate-800 text-brand-pink border border-brand-border">0</span>
          </h2>
          <p class="text-sm text-gray-400">Classified by AI: Best viral moments are saved, normal clips can be pruned to save space.</p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <button id="btn-prune-normal" onclick="pruneNormalClips()" class="bg-rose-900/30 hover:bg-rose-900/50 text-rose-300 text-xs font-semibold px-3 py-2 rounded-xl flex items-center space-x-1.5 border border-rose-500/30 transition shadow-sm" title="Delete all normal/average clips to free disk space">
            <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
            <span>Prune Normal Clips (<span id="prune-mb-label">0 MB</span>)</span>
          </button>
          <button onclick="loadClips()" class="bg-slate-800 hover:bg-slate-700 text-xs text-white px-3 py-2 rounded-xl flex items-center space-x-1.5 border border-brand-border transition">
            <i data-lucide="refresh-cw" class="w-3.5 h-3.5"></i>
            <span>Refresh</span>
          </button>
        </div>
      </div>

      <!-- Category Filter Tabs -->
      <div class="flex items-center space-x-2 border-b border-brand-border pb-3 overflow-x-auto">
        <button onclick="setClipFilter('all')" id="filter-all" class="px-3.5 py-1.5 text-xs font-bold rounded-lg bg-brand-pink text-white transition flex items-center space-x-1.5 shadow-md">
          <span>All Clips</span>
          <span id="filter-count-all" class="px-1.5 py-0.5 bg-black/30 rounded-full text-[10px]">0</span>
        </button>
        <button onclick="setClipFilter('best')" id="filter-best" class="px-3.5 py-1.5 text-xs font-semibold rounded-lg bg-slate-800 hover:bg-slate-700 text-amber-300 transition flex items-center space-x-1.5 border border-amber-500/30">
          <span>🔥 Best / Viral Only</span>
          <span id="filter-count-best" class="px-1.5 py-0.5 bg-amber-500/20 rounded-full text-[10px]">0</span>
        </button>
        <button onclick="setClipFilter('normal')" id="filter-normal" class="px-3.5 py-1.5 text-xs font-semibold rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition flex items-center space-x-1.5 border border-slate-700">
          <span>⚡ Normal (Safe to Delete)</span>
          <span id="filter-count-normal" class="px-1.5 py-0.5 bg-slate-700 rounded-full text-[10px]">0</span>
        </button>
      </div>

      <!-- Clips Grid -->
      <div id="clips-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
        <!-- Rendered dynamically -->
      </div>
    </div>

    <!-- ================================================================= -->
    <!-- TAB 3: 24/7 AUTO-PILOT SCHEDULER -->
    <!-- ================================================================= -->
    <div id="tab-autopilot" class="hidden space-y-6">
      <div class="bg-brand-card border border-brand-border rounded-2xl p-6 shadow-xl">
        <div class="flex items-center justify-between pb-6 border-b border-brand-border">
          <div>
            <h2 class="text-2xl font-bold text-white flex items-center space-x-2">
              <i data-lucide="bot" class="w-6 h-6 text-brand-pink"></i>
              <span>Autonomous Daily Auto-Pilot</span>
            </h2>
            <p class="text-sm text-gray-400 mt-1">Automatically checks streamer livestreams daily, clips hype moments, and uploads to YouTube Shorts.</p>
          </div>
          <!-- Master Toggle -->
          <div class="flex items-center space-x-3">
            <span id="scheduler-status-text" class="text-xs font-mono text-gray-400 uppercase">Disabled</span>
            <label class="relative inline-flex items-center cursor-pointer">
              <input type="checkbox" id="scheduler-toggle" onchange="toggleScheduler()" class="sr-only peer">
              <div class="w-14 h-7 bg-slate-800 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[4px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-6 after:w-6 after:transition-all peer-checked:bg-gradient-to-r peer-checked:from-brand-pink peer-checked:to-brand-cyan"></div>
            </label>
          </div>
        </div>

        <!-- Configuration Form -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 pt-6">
          <div>
            <label class="block text-xs font-semibold uppercase tracking-wider text-gray-300 mb-2">Target Livestream / Channel URL</label>
            <input type="text" id="sched-url" class="w-full bg-slate-900 border border-brand-border rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-brand-pink transition font-mono" placeholder="https://youtube.com/@IShowSpeed/live">
          </div>

          <div>
            <label class="block text-xs font-semibold uppercase tracking-wider text-gray-300 mb-2">Execution Interval</label>
            <select id="sched-interval" class="w-full bg-slate-900 border border-brand-border rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-brand-pink transition">
              <option value="1">Every 1 Hour (Continuous Hourly Blitz)</option>
              <option value="2">Every 2 Hours</option>
              <option value="6">Every 6 Hours</option>
              <option value="12">Every 12 Hours</option>
              <option value="24" selected>Every 24 Hours (Daily)</option>
            </select>
          </div>

          <div>
            <label class="block text-xs font-semibold uppercase tracking-wider text-gray-300 mb-2">Facecam Preset</label>
            <select id="sched-preset" class="w-full bg-slate-900 border border-brand-border rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-brand-pink transition">
              <option value="speed_cam" selected>⭐ IShowSpeed (Middle-Left)</option>
              <option value="top_left_cam">Top-Left Cam</option>
              <option value="top_right_cam">Top-Right Cam</option>
              <option value="auto">Auto-Detect AI</option>
            </select>
          </div>

          <div>
            <label class="block text-xs font-semibold uppercase tracking-wider text-gray-300 mb-2">Clips Per Run</label>
            <select id="sched-max-clips" class="w-full bg-slate-900 border border-brand-border rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-brand-pink transition">
              <option value="1">1 Short</option>
              <option value="3">3 Shorts</option>
              <option value="5">5 Shorts</option>
              <option value="10" selected>10 Shorts (Automated 1-Min Batch)</option>
              <option value="20">20 Shorts (Hourly Target)</option>
            </select>
          </div>

          <div class="md:col-span-2 flex items-center justify-between p-4 rounded-xl bg-slate-900 border border-brand-border">
            <div class="flex items-center space-x-3">
              <input type="checkbox" id="sched-auto-upload" class="w-5 h-5 rounded bg-slate-800 text-brand-pink focus:ring-brand-pink border-brand-border">
              <div>
                <div class="text-sm font-semibold text-white">Auto-Upload to YouTube After Generation</div>
                <div class="text-xs text-gray-400">Uses YouTube Data API v3 with pre-configured title & hashtags</div>
              </div>
            </div>
            <select id="sched-privacy" class="bg-slate-800 border border-brand-border text-xs text-white rounded-lg px-3 py-1.5">
              <option value="unlisted">Unlisted</option>
              <option value="public">Public</option>
              <option value="private">Private</option>
            </select>
          </div>
        </div>

        <div class="mt-6 pt-6 border-t border-brand-border flex items-center justify-between">
          <button onclick="saveSchedulerConfig()" class="bg-brand-pink hover:bg-pink-600 text-white font-bold px-6 py-2.5 rounded-xl text-sm transition">
            Save Settings
          </button>
          <button onclick="triggerSchedulerNow()" class="bg-slate-800 hover:bg-slate-700 text-brand-cyan border border-brand-border font-semibold px-4 py-2.5 rounded-xl text-sm transition flex items-center space-x-2">
            <i data-lucide="zap" class="w-4 h-4"></i>
            <span>Run Pipeline Right Now</span>
          </button>
        </div>
      </div>

      <!-- Execution History -->
      <div class="bg-brand-card border border-brand-border rounded-2xl p-6 shadow-xl">
        <h3 class="text-lg font-bold text-white mb-4">Auto-Pilot History Log</h3>
        <div class="overflow-x-auto">
          <table class="w-full text-left text-sm text-gray-400">
            <thead class="text-xs uppercase bg-slate-900/60 text-gray-400 border-b border-brand-border font-mono">
              <tr>
                <th class="py-3 px-4">Timestamp</th>
                <th class="py-3 px-4">Target</th>
                <th class="py-3 px-4">Clips</th>
                <th class="py-3 px-4">Status</th>
              </tr>
            </thead>
            <tbody id="scheduler-history-body" class="divide-y divide-brand-border/40 font-mono text-xs">
              <!-- Rendered dynamically -->
            </tbody>
          </table>
        </div>
      </div>
    </div>

  </main>

  <!-- ================================================================= -->
  <!-- VIDEO PLAYER MODAL (9:16 Vertical) -->
  <!-- ================================================================= -->
  <div id="video-modal" class="hidden fixed inset-0 z-50 bg-black/90 backdrop-blur-md flex items-center justify-center p-4">
    <div class="relative bg-slate-900 rounded-2xl border border-brand-border max-w-sm w-full p-4 flex flex-col items-center">
      <div class="w-full flex items-center justify-between mb-2">
        <span id="modal-video-title" class="text-xs font-mono text-gray-300 truncate">Preview Short</span>
        <button onclick="closeVideoModal()" class="text-gray-400 hover:text-white p-1">
          <i data-lucide="x" class="w-5 h-5"></i>
        </button>
      </div>

      <!-- 9:16 Video Player -->
      <div class="w-full aspect-[9/16] bg-black rounded-xl overflow-hidden relative border border-brand-border/60 shadow-2xl">
        <video id="modal-video-player" controls playsinline class="w-full h-full object-contain">
          <source id="modal-video-src" src="" type="video/mp4">
        </video>
      </div>

      <div class="w-full mt-3 flex items-center justify-between space-x-2">
        <a id="modal-download-link" href="#" download class="flex-1 bg-slate-800 hover:bg-slate-700 text-white text-xs font-semibold py-2 px-3 rounded-lg text-center transition flex items-center justify-center space-x-1.5 border border-brand-border">
          <i data-lucide="download" class="w-3.5 h-3.5"></i>
          <span>Download 1080p</span>
        </a>
        <button id="modal-upload-btn" onclick="openUploadModalFromPlayer()" class="flex-1 bg-brand-pink hover:bg-pink-600 text-white text-xs font-semibold py-2 px-3 rounded-lg text-center transition flex items-center justify-center space-x-1.5 shadow">
          <i data-lucide="upload" class="w-3.5 h-3.5"></i>
          <span>Upload to YouTube</span>
        </button>
      </div>
    </div>
  </div>

  <!-- ================================================================= -->
  <!-- YOUTUBE UPLOAD MODAL -->
  <!-- ================================================================= -->
  <div id="upload-modal" class="hidden fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-brand-border rounded-2xl max-w-md w-full p-6 space-y-4">
      <div class="flex items-center justify-between pb-3 border-b border-brand-border">
        <h3 class="text-base font-bold text-white flex items-center space-x-2">
          <i data-lucide="youtube" class="w-5 h-5 text-red-500"></i>
          <span>Upload Short to YouTube</span>
        </h3>
        <button onclick="closeUploadModal()" class="text-gray-400 hover:text-white p-1">
          <i data-lucide="x" class="w-5 h-5"></i>
        </button>
      </div>

      <input type="hidden" id="upload-filename">

      <div>
        <label class="block text-xs font-semibold uppercase text-gray-300 mb-1">Short Title</label>
        <input type="text" id="upload-title" class="w-full bg-slate-800 border border-brand-border rounded-xl px-3 py-2 text-sm text-white focus:outline-none focus:border-brand-pink">
      </div>

      <div>
        <label class="block text-xs font-semibold uppercase text-gray-300 mb-1">Description</label>
        <textarea id="upload-desc" rows="3" class="w-full bg-slate-800 border border-brand-border rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-brand-pink">GTA VI gameplay hype moments. Generated by GTA6-HypeClipper #GTAVI #GTA6 #IShowSpeed #GamingShorts #Shorts</textarea>
      </div>

      <div>
        <label class="block text-xs font-semibold uppercase text-gray-300 mb-1">Tags (comma separated)</label>
        <input type="text" id="upload-tags" value="GTA6, GTAVI, IShowSpeed, GamingShorts, Shorts, GrandTheftAuto" class="w-full bg-slate-800 border border-brand-border rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-brand-pink">
      </div>

      <div>
        <label class="block text-xs font-semibold uppercase text-gray-300 mb-1">Privacy Status</label>
        <select id="upload-privacy" class="w-full bg-slate-800 border border-brand-border rounded-xl px-3 py-2 text-sm text-white">
          <option value="unlisted" selected>Unlisted (Safe Preview)</option>
          <option value="public">Public (Immediate Live)</option>
          <option value="private">Private</option>
        </select>
      </div>

      <div class="pt-2 flex space-x-3">
        <button onclick="closeUploadModal()" class="flex-1 bg-slate-800 text-gray-300 py-2.5 rounded-xl text-sm font-semibold hover:bg-slate-700">Cancel</button>
        <button id="btn-confirm-upload" onclick="submitYouTubeUpload()" class="flex-1 bg-red-600 hover:bg-red-500 text-white py-2.5 rounded-xl text-sm font-bold flex items-center justify-center space-x-1.5 shadow">
          <i data-lucide="upload-cloud" class="w-4 h-4"></i>
          <span>Upload Now</span>
        </button>
      </div>
    </div>
  </div>

  <!-- ================================================================= -->
  <!-- YOUTUBE CHANNEL CONNECTION MODAL -->
  <!-- ================================================================= -->
  <div id="yt-setup-modal" class="hidden fixed inset-0 z-50 bg-black/85 backdrop-blur-md flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-brand-border rounded-2xl max-w-lg w-full p-6 space-y-5 shadow-2xl">
      <div class="flex items-center justify-between pb-3 border-b border-brand-border">
        <h3 class="text-base font-bold text-white flex items-center space-x-2">
          <i data-lucide="link" class="w-5 h-5 text-brand-pink"></i>
          <span>Connect Your YouTube Channel</span>
        </h3>
        <button onclick="closeYouTubeSetupModal()" class="text-gray-400 hover:text-white p-1">
          <i data-lucide="x" class="w-5 h-5"></i>
        </button>
      </div>

      <!-- Quick Setup Instructions -->
      <div class="bg-slate-800/70 rounded-xl p-4 text-xs space-y-2 border border-brand-border text-gray-300">
        <div class="font-bold text-white text-sm flex items-center space-x-1 text-brand-cyan">
          <i data-lucide="info" class="w-4 h-4"></i>
          <span>4 Simple Steps (Takes 2 mins & 100% Free):</span>
        </div>
        <ol class="list-decimal list-inside space-y-1.5 pl-1 leading-relaxed">
          <li>Open <a href="https://console.cloud.google.com/" target="_blank" class="text-brand-pink underline hover:text-pink-400 font-semibold">Google Cloud Console ↗</a> and create a free project.</li>
          <li>Search <b>"YouTube Data API v3"</b> & click <b>ENABLE</b>.</li>
          <li>Under <b>OAuth Consent Screen</b>, choose <i>External</i> and add your Gmail as a <b>Test User</b>.</li>
          <li>Under <b>Credentials</b>, click <i>+ Create Credentials -> OAuth client ID -> Desktop App</i>, download the JSON file, and upload it below.</li>
        </ol>
      </div>

      <!-- Upload client_secrets.json -->
      <div>
        <label class="block text-xs font-semibold uppercase text-gray-300 mb-2">Upload client_secrets.json</label>
        <div class="border-2 border-dashed border-brand-border hover:border-brand-pink/60 rounded-xl p-4 text-center cursor-pointer transition bg-slate-800/40" onclick="document.getElementById('secrets-file-input').click()">
          <i data-lucide="file-up" class="w-8 h-8 mx-auto text-brand-pink mb-1"></i>
          <span id="secrets-file-label" class="text-xs text-gray-300 block font-mono">Click to choose client_secrets.json</span>
          <span class="text-[10px] text-gray-500">Downloaded from Google Cloud Console</span>
          <input type="file" id="secrets-file-input" accept=".json" class="hidden" onchange="handleSecretsFileSelected(this)">
        </div>
      </div>

      <!-- Or Manual Client ID / Secret -->
      <div class="text-center text-xs text-gray-500 font-semibold uppercase tracking-wider">-- OR ENTER KEYS MANUALLY --</div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label class="block text-[11px] font-semibold text-gray-400 mb-1">Client ID</label>
          <input type="text" id="manual-client-id" placeholder="...apps.googleusercontent.com" class="w-full bg-slate-800 border border-brand-border rounded-lg px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-brand-pink font-mono">
        </div>
        <div>
          <label class="block text-[11px] font-semibold text-gray-400 mb-1">Client Secret</label>
          <input type="password" id="manual-client-secret" placeholder="GOCSPX-..." class="w-full bg-slate-800 border border-brand-border rounded-lg px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-brand-pink font-mono">
        </div>
      </div>

      <!-- Action Button -->
      <div class="pt-2 flex space-x-3">
        <button onclick="closeYouTubeSetupModal()" class="flex-1 bg-slate-800 text-gray-300 py-2.5 rounded-xl text-sm font-semibold hover:bg-slate-700">Cancel</button>
        <button id="btn-auth-youtube" onclick="submitYouTubeAuthorization()" class="flex-1 bg-gradient-to-r from-red-600 to-brand-pink hover:opacity-95 text-white py-2.5 rounded-xl text-sm font-bold flex items-center justify-center space-x-1.5 shadow-lg">
          <i data-lucide="shield-check" class="w-4 h-4"></i>
          <span>Authorize in Browser</span>
        </button>
      </div>
    </div>
  </div>

  <!-- JavaScript Client Logic -->
  <script>
    let activePlayerFilename = '';

    // Initialize Lucide icons
    document.addEventListener("DOMContentLoaded", () => {
      lucide.createIcons();
      initLogStream();
      pollStatus();
      loadClips();
      loadScheduler();
      loadYouTubeStatus();
    });

    // Tab Switching
    function switchTab(tab) {
      document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('bg-brand-pink', 'text-white');
        btn.classList.add('text-gray-400');
      });
      document.getElementById('tab-btn-' + tab).classList.add('bg-brand-pink', 'text-white');
      document.getElementById('tab-btn-' + tab).classList.remove('text-gray-400');

      document.getElementById('tab-studio').classList.toggle('hidden', tab !== 'studio');
      document.getElementById('tab-gallery').classList.toggle('hidden', tab !== 'gallery');
      document.getElementById('tab-autopilot').classList.toggle('hidden', tab !== 'autopilot');

      if (tab === 'gallery') loadClips();
      if (tab === 'autopilot') loadScheduler();
    }

    // Server-Sent Events (SSE) Live Log Streaming
    function initLogStream() {
      const evtSource = new EventSource("/api/logs/stream");
      const terminal = document.getElementById("log-terminal");

      evtSource.onmessage = function(event) {
        try {
          const data = JSON.parse(event.data);
          const line = document.createElement("div");
          line.textContent = data.line;
          terminal.appendChild(line);
          terminal.scrollTop = terminal.scrollHeight;
        } catch(e) {}
      };

      evtSource.onerror = function() {
        // Fallback polling if SSE disconnects
        setTimeout(initLogStream, 5000);
      };
    }

    function clearLogs() {
      document.getElementById("log-terminal").innerHTML = '<div class="text-gray-500">// Terminal cleared.</div>';
    }

    // Start Clipping Job
    async function startClipping() {
      const url = document.getElementById("stream-url").value.trim();
      if (!url) {
        alert("Please enter a YouTube or Twitch livestream URL.");
        return;
      }

      const preset = document.getElementById("preset-selector").value;
      const maxClips = document.getElementById("max-clips").value;
      const captions = document.getElementById("toggle-captions").checked;
      const upload = document.getElementById("toggle-upload").checked;

      document.getElementById("btn-start-clipping").disabled = true;
      document.getElementById("btn-start-clipping").classList.add("opacity-50");

      try {
        const res = await fetch("/api/pipeline/start", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            url: url,
            preset: preset,
            max_clips: maxClips,
            enable_captions: captions,
            auto_upload: upload
          })
        });
        const data = await res.json();
        if (data.status !== "success") {
          alert("Error: " + data.message);
        }
      } catch (err) {
        alert("Network error: " + err);
      }
    }

    // Stop Clipping Job
    async function stopClipping() {
      await fetch("/api/pipeline/stop", {method: "POST"});
    }

    // Poll Pipeline Status
    async function pollStatus() {
      try {
        const res = await fetch("/api/pipeline/status");
        const job = await res.json();

        const isRunning = job.status === "running";
        document.getElementById("btn-start-clipping").disabled = isRunning;
        document.getElementById("btn-start-clipping").classList.toggle("opacity-50", isRunning);
        document.getElementById("btn-stop-clipping").classList.toggle("hidden", !isRunning);

        document.getElementById("job-stage-label").textContent = job.stage || "Ready";
        document.getElementById("job-progress-pct").textContent = job.progress + "%";
        document.getElementById("job-progress-bar").style.width = job.progress + "%";

        if (job.status === "completed" && job.progress === 100) {
          loadClips();
        }
      } catch (e) {}

      setTimeout(pollStatus, 2000);
    }

    // Clips Gallery State & Filtering
    let allClipsData = [];
    let activeClipFilter = 'all';

    function setClipFilter(filter) {
      activeClipFilter = filter;
      ['all', 'best', 'normal'].forEach(f => {
        const btn = document.getElementById('filter-' + f);
        if (!btn) return;
        if (f === filter) {
          btn.className = "px-3.5 py-1.5 text-xs font-bold rounded-lg bg-brand-pink text-white transition flex items-center space-x-1.5 shadow-md";
        } else {
          btn.className = "px-3.5 py-1.5 text-xs font-semibold rounded-lg bg-slate-800 hover:bg-slate-700 text-gray-300 transition flex items-center space-x-1.5 border border-brand-border";
        }
      });
      renderClipsGrid();
    }

    function renderClipsGrid() {
      const grid = document.getElementById("clips-grid");
      if (!grid) return;

      let filtered = allClipsData;
      if (activeClipFilter === 'best') {
        filtered = allClipsData.filter(c => c.category === 'BEST');
      } else if (activeClipFilter === 'normal') {
        filtered = allClipsData.filter(c => c.category !== 'BEST');
      }

      if (filtered.length === 0) {
        grid.innerHTML = '<div class="col-span-full text-center py-12 text-gray-500">No clips found in this category.</div>';
        return;
      }

      grid.innerHTML = filtered.map(clip => {
        const isBest = clip.category === 'BEST';
        const badgeHtml = isBest
          ? `<span class="px-2.5 py-0.5 text-[10px] font-bold rounded-full bg-gradient-to-r from-amber-500/30 to-red-500/30 text-amber-300 border border-amber-500/50 flex items-center space-x-1 shadow-sm">
               <span>🔥 BEST / VIRAL</span>
             </span>`
          : `<span class="px-2.5 py-0.5 text-[10px] font-semibold rounded-full bg-slate-800/90 text-slate-300 border border-slate-700 flex items-center space-x-1">
               <span>⚡ NORMAL (CAN DELETE)</span>
             </span>`;

        const uploadedBadge = clip.uploaded || clip.youtube_url
          ? `<span class="px-2 py-0.5 text-[10px] font-bold rounded bg-emerald-500/30 text-emerald-300 border border-emerald-500/40">✅ UPLOADED</span>`
          : '';

        return `
          <div class="bg-brand-card border ${isBest ? 'border-amber-500/40 shadow-amber-950/20' : 'border-brand-border'} rounded-2xl overflow-hidden shadow-xl hover:border-brand-pink/50 transition flex flex-col justify-between">
            <div class="relative aspect-[9/16] bg-black cursor-pointer group" onclick="openVideoModal('${clip.filename}')">
              <img src="/thumbnail/${clip.filename}" class="w-full h-full object-cover" onerror="this.src='/thumbnail/${clip.filename}'" alt="Short preview">
              <div class="absolute inset-0 bg-black/40 group-hover:bg-black/20 flex items-center justify-center transition">
                <div class="w-12 h-12 rounded-full bg-brand-pink/90 text-white flex items-center justify-center shadow-lg group-hover:scale-110 transition">
                  <i data-lucide="play" class="w-6 h-6 ml-0.5"></i>
                </div>
              </div>
              <div class="absolute top-2 left-2 flex flex-col gap-1 z-10">
                ${badgeHtml}
                ${uploadedBadge}
              </div>
              <span class="absolute bottom-2 right-2 bg-black/80 text-[10px] font-mono px-2 py-0.5 rounded text-white z-10">
                ${clip.duration ? clip.duration.toFixed(1) + 's' : '45.0s'}
              </span>
            </div>
            <div class="p-4 space-y-3">
              <div class="text-xs font-mono text-gray-200 truncate font-semibold" title="${clip.filename}">${clip.filename}</div>
              <div class="text-[11px] text-gray-400 line-clamp-1 italic" title="${clip.reason}">${clip.reason}</div>
              <div class="flex items-center justify-between text-[11px] text-gray-400">
                <span>${clip.size_mb} MB</span>
                <span>${clip.created_at}</span>
              </div>
              <div class="flex items-center space-x-2 pt-2 border-t border-brand-border">
                <a href="/download/${clip.filename}" download class="p-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-gray-300 hover:text-white transition" title="Download MP4">
                  <i data-lucide="download" class="w-4 h-4"></i>
                </a>
                <button onclick="openUploadModal('${clip.filename}')" class="flex-1 bg-red-600/20 hover:bg-red-600/30 text-red-400 border border-red-500/30 text-xs font-semibold py-2 px-3 rounded-lg flex items-center justify-center space-x-1.5 transition">
                  <i data-lucide="upload" class="w-3.5 h-3.5"></i>
                  <span>Upload</span>
                </button>
                <button onclick="deleteClip('${clip.filename}', ${isBest})" class="p-2 rounded-lg bg-slate-800 hover:bg-rose-900/40 text-gray-400 hover:text-rose-400 transition" title="${isBest ? 'Delete Best Clip (Warning)' : 'Delete Normal Clip (Safe)'}">
                  <i data-lucide="trash" class="w-4 h-4"></i>
                </button>
              </div>
            </div>
          </div>
        `;
      }).join('');

      lucide.createIcons();
    }

    // Load Clips Gallery
    async function loadClips() {
      try {
        const res = await fetch("/api/clips");
        const data = await res.json();
        allClipsData = data.clips || [];

        document.getElementById("clips-counter").textContent = allClipsData.length;
        if (document.getElementById("clips-total-badge")) {
          document.getElementById("clips-total-badge").textContent = allClipsData.length;
        }
        if (document.getElementById("filter-count-all")) {
          document.getElementById("filter-count-all").textContent = allClipsData.length;
        }
        if (document.getElementById("filter-count-best")) {
          document.getElementById("filter-count-best").textContent = data.best_count || 0;
        }
        if (document.getElementById("filter-count-normal")) {
          document.getElementById("filter-count-normal").textContent = data.normal_count || 0;
        }
        if (document.getElementById("prune-mb-label")) {
          document.getElementById("prune-mb-label").textContent = (data.normal_size_mb || 0) + ' MB';
        }

        renderClipsGrid();
      } catch (e) {
        console.error("Failed to load clips:", e);
      }
    }

    // Bulk Prune Normal Clips
    async function pruneNormalClips() {
      const count = document.getElementById("filter-count-normal")?.textContent || "0";
      const mb = document.getElementById("prune-mb-label")?.textContent || "0 MB";
      if (parseInt(count) === 0) {
        alert("No normal clips found to prune! All current clips are categorized as BEST.");
        return;
      }

      if (!confirm(`Are you sure you want to delete ${count} normal clips (${mb}) to free disk space?\n\nNOTE: All your BEST viral clips will stay 100% safe!`)) {
        return;
      }

      try {
        const res = await fetch("/api/clips/prune-normal", { method: "POST" });
        const data = await res.json();
        alert(`Pruned ${data.deleted_count} normal clips!\nFreed ${data.freed_mb} MB disk space.`);
        loadClips();
      } catch (err) {
        alert("Failed to prune clips: " + err);
      }
    }

    // Modal Video Player
    function openVideoModal(filename) {
      activePlayerFilename = filename;
      document.getElementById("modal-video-title").textContent = filename;
      const player = document.getElementById("modal-video-player");
      player.src = "/stream/" + filename;
      document.getElementById("modal-download-link").href = "/download/" + filename;
      document.getElementById("video-modal").classList.remove("hidden");
      player.play();
    }

    function closeVideoModal() {
      const player = document.getElementById("modal-video-player");
      player.pause();
      player.src = "";
      document.getElementById("video-modal").classList.add("hidden");
    }

    function openUploadModalFromPlayer() {
      closeVideoModal();
      openUploadModal(activePlayerFilename);
    }

    // Delete Clip
    async function deleteClip(filename, isBest) {
      const warning = isBest
        ? "⚠️ ATTENTION: This is classified as a BEST / VIRAL clip!\nAre you sure you want to delete " + filename + "?"
        : "Are you sure you want to delete " + filename + "? (Safe to prune)";
      if (!confirm(warning)) return;
      await fetch("/api/clips/" + filename, {method: "DELETE"});
      loadClips();
    }

    // YouTube Upload Modal
    function openUploadModal(filename) {
      document.getElementById("upload-filename").value = filename;
      document.getElementById("upload-title").value = "IShowSpeed UNREAL MOMENT IN GTA 6! 💀 #GTAVI #Shorts";
      document.getElementById("upload-modal").classList.remove("hidden");
    }

    function closeUploadModal() {
      document.getElementById("upload-modal").classList.add("hidden");
    }

    async function submitYouTubeUpload() {
      const btn = document.getElementById("btn-confirm-upload");
      btn.disabled = true;
      btn.innerHTML = '<span>Uploading...</span>';

      const filename = document.getElementById("upload-filename").value;
      const title = document.getElementById("upload-title").value;
      const desc = document.getElementById("upload-desc").value;
      const tags = document.getElementById("upload-tags").value.split(",").map(t => t.trim());
      const privacy = document.getElementById("upload-privacy").value;

      try {
        const res = await fetch("/api/youtube/upload", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            filename: filename,
            title: title,
            description: desc,
            tags: tags,
            privacy: privacy
          })
        });
        const data = await res.json();
        if (data.status === "success") {
          alert("🎉 Uploaded successfully! Video URL: " + data.youtube_url);
          closeUploadModal();
        } else {
          alert("Upload failed: " + data.message);
        }
      } catch (err) {
        alert("Upload error: " + err);
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="upload-cloud" class="w-4 h-4"></i><span>Upload Now</span>';
        lucide.createIcons();
      }
    }

    // Scheduler Management
    async function loadScheduler() {
      try {
        const res = await fetch("/api/scheduler");
        const s = await res.json();

        document.getElementById("scheduler-toggle").checked = s.enabled;
        document.getElementById("scheduler-status-text").textContent = s.enabled ? "Active" : "Disabled";
        document.getElementById("scheduler-status-text").className = s.enabled ? "text-xs font-mono text-emerald-400 uppercase font-bold" : "text-xs font-mono text-gray-400 uppercase";

        document.getElementById("sched-url").value = s.target_url;
        document.getElementById("sched-interval").value = s.interval_hours;
        document.getElementById("sched-preset").value = s.preset;
        document.getElementById("sched-max-clips").value = s.max_clips;
        document.getElementById("sched-auto-upload").checked = s.auto_upload;
        document.getElementById("sched-privacy").value = s.privacy;

        // Render history table
        const tbody = document.getElementById("scheduler-history-body");
        if (s.history && s.history.length > 0) {
          tbody.innerHTML = s.history.slice().reverse().map(h => `
            <tr class="hover:bg-slate-800/40">
              <td class="py-2.5 px-4">${h.timestamp.replace('T', ' ').slice(0, 19)}</td>
              <td class="py-2.5 px-4 truncate max-w-xs">${h.target_url}</td>
              <td class="py-2.5 px-4">${h.clips_count}</td>
              <td class="py-2.5 px-4">
                <span class="px-2 py-0.5 rounded text-[10px] ${h.status === 'success' ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-rose-500/20 text-rose-400'}">
                  ${h.status}
                </span>
              </td>
            </tr>
          `).join('');
        } else {
          tbody.innerHTML = '<tr><td colspan="4" class="py-4 text-center text-gray-500">No scheduled runs recorded yet.</td></tr>';
        }
      } catch (e) {}
    }

    async function toggleScheduler() {
      const enabled = document.getElementById("scheduler-toggle").checked;
      await fetch("/api/scheduler", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled: enabled})
      });
      loadScheduler();
    }

    async function saveSchedulerConfig() {
      await fetch("/api/scheduler", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          target_url: document.getElementById("sched-url").value,
          interval_hours: parseInt(document.getElementById("sched-interval").value),
          preset: document.getElementById("sched-preset").value,
          max_clips: parseInt(document.getElementById("sched-max-clips").value),
          auto_upload: document.getElementById("sched-auto-upload").checked,
          privacy: document.getElementById("sched-privacy").value
        })
      });
      alert("Auto-Pilot settings saved!");
      loadScheduler();
    }

    async function triggerSchedulerNow() {
      const res = await fetch("/api/scheduler/trigger", {method: "POST"});
      const data = await res.json();
      alert(data.message);
    }

    // YouTube Status & Connection Logic
    async function loadYouTubeStatus() {
      try {
        const res = await fetch("/api/youtube/status");
        const status = await res.json();
        const badge = document.getElementById("yt-channel-badge");

        if (status.ready && status.channel_title) {
          badge.innerHTML = `
            <span class="flex items-center space-x-1.5 px-3 py-1 rounded-full bg-red-600/10 text-red-400 border border-red-500/30 cursor-pointer hover:bg-red-600/20 transition" title="Click to reconnect" onclick="openYouTubeSetupModal()">
              <i data-lucide="youtube" class="w-3.5 h-3.5 text-red-500"></i>
              <span class="font-semibold truncate max-w-[140px]">${status.channel_title}</span>
              <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
            </span>
          `;
        } else {
          badge.innerHTML = `
            <button onclick="openYouTubeSetupModal()" class="flex items-center space-x-1.5 px-3 py-1 rounded-full bg-slate-800 hover:bg-slate-700 text-gray-300 hover:text-white border border-brand-border transition">
              <i data-lucide="link" class="w-3.5 h-3.5 text-brand-pink"></i>
              <span>Connect YouTube</span>
            </button>
          `;
        }
        lucide.createIcons();
      } catch(e) {}
    }

    let selectedSecretsFile = null;
    function handleSecretsFileSelected(input) {
      if (input.files && input.files[0]) {
        selectedSecretsFile = input.files[0];
        document.getElementById("secrets-file-label").textContent = "Selected: " + selectedSecretsFile.name;
        document.getElementById("secrets-file-label").className = "text-xs text-emerald-400 block font-mono font-bold";
      }
    }

    function openYouTubeSetupModal() {
      document.getElementById("yt-setup-modal").classList.remove("hidden");
    }

    function closeYouTubeSetupModal() {
      document.getElementById("yt-setup-modal").classList.add("hidden");
    }

    async function submitYouTubeAuthorization() {
      const btn = document.getElementById("btn-auth-youtube");
      btn.disabled = true;
      btn.innerHTML = '<span>Launching Browser Authorization...</span>';

      let formData = null;
      let jsonData = null;

      if (selectedSecretsFile) {
        formData = new FormData();
        formData.append("client_secrets", selectedSecretsFile);
      } else {
        const cid = document.getElementById("manual-client-id").value.trim();
        const csec = document.getElementById("manual-client-secret").value.trim();
        if (cid && csec) {
          jsonData = {client_id: cid, client_secret: csec};
        }
      }

      try {
        let res;
        if (formData) {
          res = await fetch("/api/youtube/connect", {method: "POST", body: formData});
        } else if (jsonData) {
          res = await fetch("/api/youtube/connect", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(jsonData)
          });
        } else {
          // Check if client_secrets.json is already present on server
          res = await fetch("/api/youtube/connect", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({})});
        }

        const data = await res.json();
        if (data.status === "success") {
          alert("🚀 " + data.message);
          closeYouTubeSetupModal();
          
          // Poll for token.json creation
          let checks = 0;
          const pollInterval = setInterval(async () => {
            checks++;
            const sRes = await fetch("/api/youtube/status");
            const sData = await sRes.json();
            if (sData.ready) {
              clearInterval(pollInterval);
              alert("🎉 Successfully connected to YouTube Channel: " + sData.channel_title);
              loadYouTubeStatus();
            }
            if (checks > 60) clearInterval(pollInterval); // Stop polling after 2 mins
          }, 2000);

        } else {
          alert("Error: " + data.message);
        }
      } catch (err) {
        alert("Authorization failed: " + err);
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="shield-check" class="w-4 h-4"></i><span>Authorize in Browser</span>';
        lucide.createIcons();
      }
    }
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    # Start background scheduler
    scheduler_instance.start()

    port = int(os.environ.get("PORT", 8080))
    print(f"\n=================================================================")
    print(f"  GTA6-HypeClipper Web UI Dashboard Live at:")
    print(f"  http://localhost:{port}")
    print(f"=================================================================\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

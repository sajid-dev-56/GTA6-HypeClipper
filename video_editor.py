"""
Module 3: Gaming Split-Screen Layout Engine (video_editor.py)
Part of GTA6-HypeClipper Pipeline.

Renders vertical 9:16 (1080x1920) 60fps YouTube Shorts using direct FFmpeg filtergraphs:
- Top 30% (1080x576): Streamer Facecam (preset or OpenCV auto-detection fallback).
- Bottom 70% (1080x1344): Main GTA VI Gameplay (9:16 center action crop).
- Stacked vertically via FFmpeg vstack.
- Burns Alex Hormozi dynamic ASS subtitles in a single-pass filtergraph for 60fps throughput.
"""

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("GTA6-HypeClipper.VideoEditor")


def escape_ffmpeg_filter_path(path: str) -> str:
    """
    Escape file path for FFmpeg filtergraph strings (e.g. subtitles filter).
    Handles colons (e.g. on Windows or absolute paths), backslashes, brackets, and quotes.
    """
    abs_path = os.path.abspath(path)
    # Convert Windows backslashes to forward slashes
    escaped = abs_path.replace("\\", "/")
    # Escape characters special to FFmpeg filter syntax
    escaped = escaped.replace(":", r"\:")
    escaped = escaped.replace("'", r"\'")
    escaped = escaped.replace("[", r"\[")
    escaped = escaped.replace("]", r"\]")
    return escaped


def find_ffmpeg_binary(configured_path: Optional[str] = None) -> str:
    """Locate the FFmpeg binary."""
    candidates = [
        configured_path,
        os.environ.get("FFMPEG_PATH"),
        shutil.which("ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        os.path.join(os.getcwd(), "bin", "ffmpeg"),
        os.path.join(os.path.dirname(__file__), "bin", "ffmpeg"),
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.abspath(c)
    return "ffmpeg"


def auto_detect_facecam_corner(
    video_path: str,
    width: int = 1920,
    height: int = 1080,
    sample_count: int = 8,
) -> Tuple[int, int, int, int]:
    """
    Sample video frames and detect which corner contains the streamer's Facecam
    using OpenCV Haar cascades. Falls back to Top-Left if OpenCV is unavailable
    or no face is detected.
    """
    default_bbox = (40, 40, int(width * 0.25), int(height * 0.35))

    try:
        import cv2
    except ImportError:
        logger.info("OpenCV not installed; using standard top_left_cam preset.")
        return default_bbox

    try:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        face_cascade = cv2.CascadeClassifier(cascade_path)
    except Exception as e:
        logger.warning(f"Could not load OpenCV face cascade: {e}")
        return default_bbox

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return default_bbox

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1000)
    frame_indices = [int(i * total_frames / (sample_count + 1)) for i in range(1, sample_count + 1)]

    # Corner & edge quadrant bounds: (x_min, x_max, y_min, y_max)
    quadrants = {
        "top_left": (0, int(width * 0.35), 0, int(height * 0.38)),
        "middle_left": (0, int(width * 0.35), int(height * 0.32), int(height * 0.68)),
        "bottom_left": (0, int(width * 0.35), int(height * 0.65), height),
        "top_right": (int(width * 0.65), width, 0, int(height * 0.38)),
        "middle_right": (int(width * 0.65), width, int(height * 0.32), int(height * 0.68)),
        "bottom_right": (int(width * 0.65), width, int(height * 0.65), height),
    }
    quadrant_votes = {k: 0 for k in quadrants}

    for f_idx in frame_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
        ret, frame = cap.read()
        if not ret or frame is None:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(60, 60))

        for (fx, fy, fw, fh) in faces:
            cx, cy = fx + fw // 2, fy + fh // 2
            for q_name, (qx1, qx2, qy1, qy2) in quadrants.items():
                if qx1 <= cx <= qx2 and qy1 <= cy <= qy2:
                    quadrant_votes[q_name] += 1

    cap.release()

    best_quadrant = max(quadrant_votes, key=quadrant_votes.get)
    max_votes = quadrant_votes[best_quadrant]

    logger.info(f"Facecam detector votes: {quadrant_votes} (Selected: {best_quadrant} with {max_votes} hits)")

    cam_w = int(width * 0.25)
    cam_h = int(height * 0.35)

    if best_quadrant == "middle_left":
        # Tuned specifically for middle-left streamer cam (e.g. IShowSpeed layout)
        return (0, int(height * 0.38), int(width * 0.24), int(height * 0.29))
    elif best_quadrant == "middle_right":
        return (width - cam_w, int(height * 0.38), cam_w, int(height * 0.29))
    elif best_quadrant == "top_left":
        return (40, 40, cam_w, cam_h)
    elif best_quadrant == "top_right":
        return (width - cam_w - 40, 40, cam_w, cam_h)
    elif best_quadrant == "bottom_left":
        return (40, height - cam_h - 40, cam_w, cam_h)
    else:  # bottom_right
        return (width - cam_w - 40, height - cam_h - 40, cam_w, cam_h)


def resolve_facecam_coordinates(
    preset_name: str,
    config: Optional[Dict[str, Any]] = None,
    video_path: Optional[str] = None,
    source_width: int = 1920,
    source_height: int = 1080,
) -> Tuple[int, int, int, int]:
    """
    Resolve Facecam crop box (x, y, w, h) from preset name or config.
    """
    cfg = config or {}
    l_cfg = cfg.get("layout", {})
    presets = l_cfg.get("presets", {})

    aliases = {
        "speed": "speed_cam",
        "ishowspeed": "speed_cam",
        "speedcam": "speed_cam",
        "middle_left": "middle_left_cam",
        "middle_right": "middle_right_cam",
        "top_left": "top_left_cam",
        "top_right": "top_right_cam",
        "bottom_left": "bottom_left_cam",
        "bottom_right": "bottom_right_cam",
    }
    normalized = aliases.get(preset_name.lower(), preset_name)

    if normalized == "auto" and video_path and l_cfg.get("auto_detect_facecam", True):
        return auto_detect_facecam_corner(video_path, source_width, source_height)

    if normalized in presets:
        p = presets[normalized]
        return (int(p.get("x", 40)), int(p.get("y", 40)), int(p.get("w", 480)), int(p.get("h", 360)))

    # Fallback to configured active preset or top_left_cam
    active = l_cfg.get("active_preset", "speed_cam")
    default_p = presets.get(active, presets.get("speed_cam", {"x": 0, "y": 415, "w": 460, "h": 310}))
    return (
        int(default_p.get("x", 40)),
        int(default_p.get("y", 40)),
        int(default_p.get("w", 480)),
        int(default_p.get("h", 360)),
    )


def build_split_screen_filtergraph(
    facecam_coords: Tuple[int, int, int, int],
    subtitles_path: Optional[str] = None,
    output_fps: int = 60,
) -> str:
    """
    Construct high-performance FFmpeg -filter_complex string:
    - Top: Facecam cropped to (x,y,w,h) and scaled to 1080x576
    - Bottom: Gameplay 9:16 center action crop scaled to 1080x1344
    - Stacked vertically to 1080x1920
    - Burns dynamic ASS subtitles if provided
    """
    fx, fy, fw, fh = facecam_coords

    filters: List[str] = []

    # Stream 1: Facecam (Top 30% -> 1080x576)
    # Crop streamer's camera, scale to cover 1080x576 without distortion, center-crop exact bounds
    facecam_filter = (
        f"[0:v]crop={fw}:{fh}:{fx}:{fy},"
        f"scale=1080:576:force_original_aspect_ratio=increase,"
        f"crop=1080:576,setsar=1,fps={output_fps}[facecam]"
    )
    filters.append(facecam_filter)

    # Stream 2: GTA VI Gameplay (Bottom 70% -> 1080x1344)
    # Crop center 9:16 action viewport (in 1080p: width 868, x offset 526)
    # Dynamic formula: width = trunc(ih * 1080 / 1344 / 2) * 2
    gameplay_filter = (
        f"[0:v]crop=trunc(ih*1080/1344/2)*2:ih:(iw-trunc(ih*1080/1344/2)*2)/2:0,"
        f"scale=1080:1344,setsar=1,fps={output_fps}[gameplay]"
    )
    filters.append(gameplay_filter)

    # Vertical stack
    stack_filter = "[facecam][gameplay]vstack=inputs=2[stacked]"
    filters.append(stack_filter)

    # Subtitle burn-in
    if subtitles_path and os.path.isfile(subtitles_path):
        escaped_sub = escape_ffmpeg_filter_path(subtitles_path)
        subtitle_filter = f"[stacked]subtitles=filename='{escaped_sub}'[v_out]"
        filters.append(subtitle_filter)
    else:
        filters.append("[stacked]copy[v_out]")

    return ";".join(filters)


def render_gaming_short(
    source_video_path: str,
    clip_info: Dict[str, Any],
    output_mp4_path: str,
    subtitles_ass_path: Optional[str] = None,
    facecam_preset: str = "top_left_cam",
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Render a single hype clip into 9:16 Vertical Gaming Split-Screen Shorts format.
    Uses direct FFmpeg subprocess execution for maximum 60fps throughput.
    """
    cfg = config or {}
    v_cfg = cfg.get("video_editor", {})

    ffmpeg_bin = find_ffmpeg_binary(v_cfg.get("ffmpeg_bin"))
    output_fps = int(v_cfg.get("output_fps", 60))
    crf = int(v_cfg.get("crf", 19))
    preset = v_cfg.get("preset", "veryfast")
    v_codec = v_cfg.get("video_codec", "libx264")
    a_codec = v_cfg.get("audio_codec", "aac")
    a_bitrate = v_cfg.get("audio_bitrate", "192k")

    start_time = float(clip_info["start_time"])
    end_time = float(clip_info["end_time"])
    duration = end_time - start_time

    # Resolve facecam coordinates
    facecam_coords = resolve_facecam_coordinates(
        facecam_preset,
        config=cfg,
        video_path=source_video_path,
    )
    logger.info(f"Rendering Clip #{clip_info.get('clip_id', 1)}: {start_time}s -> {end_time}s ({duration:.1f}s)")
    logger.info(f"Facecam coordinates: (x={facecam_coords[0]}, y={facecam_coords[1]}, w={facecam_coords[2]}, h={facecam_coords[3]})")

    # Build filtergraph
    filter_complex = build_split_screen_filtergraph(
        facecam_coords=facecam_coords,
        subtitles_path=subtitles_ass_path,
        output_fps=output_fps,
    )

    os.makedirs(os.path.dirname(os.path.abspath(output_mp4_path)), exist_ok=True)

    # Assemble FFmpeg command
    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss", str(start_time),
        "-to", str(end_time),
        "-i", source_video_path,
        "-filter_complex", filter_complex,
        "-map", "[v_out]",
        "-map", "0:a?",
        "-c:v", v_codec,
        "-preset", preset,
        "-crf", str(crf),
        "-r", str(output_fps),
        "-pix_fmt", "yuv420p",
        "-c:a", a_codec,
        "-b:a", a_bitrate,
        "-ar", "48000",
        "-movflags", "+faststart",
        output_mp4_path,
    ]

    logger.info(f"Executing FFmpeg render for {output_mp4_path}...")
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    except FileNotFoundError:
        raise RuntimeError(
            f"FFmpeg executable '{ffmpeg_bin}' not found. Please install FFmpeg (brew install ffmpeg / apt install ffmpeg)."
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg render failed with exit code {e.returncode}:")
        logger.error(f"Stderr: {e.stderr}")
        raise RuntimeError(f"FFmpeg render failed: {e.stderr}") from e

    if not os.path.isfile(output_mp4_path) or os.path.getsize(output_mp4_path) == 0:
        raise RuntimeError(f"Render failed: Output file missing or empty: {output_mp4_path}")

    logger.info(f"Successfully generated 9:16 Short: {output_mp4_path}")
    return os.path.abspath(output_mp4_path)

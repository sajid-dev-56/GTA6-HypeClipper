#!/usr/bin/env python3
"""
GTA6-HypeClipper - Main Pipeline Orchestrator (main.py)
Automated Gaming Livestream to YouTube Shorts Pipeline.

Usage:
  python main.py --url "<STREAM_OR_VOD_URL>" --max-clips 5 --preset "top_left_cam"
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

# Set up logging format
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("GTA6-HypeClipper")

# Import pipeline modules
import ingest
import hype_detector
import video_editor
import caption_generator
import uploader


def load_yaml_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """
    Load configuration from YAML file.
    Includes a fallback parser for basic structures if PyYAML is not yet installed.
    """
    if not os.path.isfile(config_path):
        logger.warning(f"Config file '{config_path}' not found. Using default configurations.")
        return {}

    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        logger.warning("PyYAML not installed. Attempting lightweight fallback config parsing...")
        # Very simple fallback parser for basic scalar keys
        fallback: Dict[str, Any] = {
            "ingest": {"target_resolution": "1080", "max_fps": 60, "temp_dir": "temp", "output_dir": "output"},
            "layout": {
                "active_preset": "top_left_cam",
                "auto_detect_facecam": True,
                "presets": {
                    "top_left_cam": {"x": 40, "y": 40, "w": 480, "h": 360},
                    "top_right_cam": {"x": 1400, "y": 40, "w": 480, "h": 360},
                    "bottom_left_cam": {"x": 40, "y": 680, "w": 480, "h": 360},
                    "bottom_right_cam": {"x": 1400, "y": 680, "w": 480, "h": 360},
                    "custom": {"x": 0, "y": 0, "w": 480, "h": 360},
                },
            },
            "hype_detection": {
                "window_seconds": 5.0, "hop_seconds": 1.0, "audio_threshold_std": 2.0,
                "chat_velocity_weight": 0.5, "audio_spike_weight": 0.5,
                "keyword_surge_multiplier": 1.5, "min_clip_spacing_seconds": 40.0,
                "clip_pre_peak_seconds": 20.0, "clip_post_peak_seconds": 25.0, "max_clips": 5,
            },
            "captions": {
                "enabled": True, "whisper_model": "small", "max_words_per_line": 3,
                "position_y": 620, "font_name": "Montserrat ExtraBold",
            },
            "video_editor": {
                "output_width": 1080, "output_height": 1920, "output_fps": 60,
                "crf": 19, "preset": "veryfast",
            },
            "uploader": {"enabled": False, "privacy_status": "unlisted"},
        }
        return fallback


def print_banner():
    banner = r"""
=============================================================================
   _____ _______       __     _    _                     _____ _ _                       
  / ____|__   __|/     \ \   | |  | |                   / ____| (_)                      
 | |  __   | |  /  /\  \ \  | |__| |_   _ _ __   ___  | |    | |_ _ __  _ __   ___ _ __ 
 | | |_ |  | | /  /  \  \ \ |  __  | | | | '_ \ / _ \ | |    | | | '_ \| '_ \ / _ \ '__|
 | |__| |  | |/  /____\  \ \| |  | | |_| | |_) |  __/ | |____| | | |_) | |_) |  __/ |   
  \_____|  |_/_/        \_\\_\_|  |_|\__, | .__/ \___|  \_____|_|_| .__/| .__/ \___|_|   
                                      __/ | |                      | |   | |              
                                     |___/|_|                      |_|   |_|              
           Automated Gaming Livestream / VOD to 9:16 Shorts Generator
=============================================================================
"""
    print(banner)


def check_prerequisites():
    """Verify system prerequisites (FFmpeg)."""
    ffmpeg_bin = video_editor.find_ffmpeg_binary()
    ffprobe_bin = shutil.which("ffprobe")

    if not shutil.which(ffmpeg_bin) and not os.path.isfile(ffmpeg_bin):
        logger.error("=" * 70)
        logger.error("CRITICAL ERROR: FFmpeg executable not found!")
        logger.error("FFmpeg is strictly required for audio extraction, filtergraphs, and video encoding.")
        logger.error("Installation instructions:")
        logger.error("  - macOS:   brew install ffmpeg")
        logger.error("  - Ubuntu:  sudo apt update && sudo apt install ffmpeg")
        logger.error("  - Windows: winget install Gyan.FFmpeg")
        logger.error("=" * 70)
        return False
    return True


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GTA6-HypeClipper: AI Gaming Stream to Vertical YouTube Shorts Pipeline."
    )
    parser.add_argument(
        "--url",
        type=str,
        required=True,
        help="YouTube or Twitch stream / VOD URL (or local video file path).",
    )
    parser.add_argument(
        "--max-clips",
        type=int,
        default=None,
        help="Maximum number of hype clips to generate (default: from config, e.g. 5).",
    )
    parser.add_argument(
        "--preset",
        type=str,
        default=None,
        choices=["top_left_cam", "top_right_cam", "bottom_left_cam", "bottom_right_cam", "auto", "custom"],
        help="Streamer facecam layout preset.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to YAML configuration file.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory to save generated YouTube Shorts.",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Auto-upload generated shorts to YouTube via YouTube Data API v3.",
    )
    parser.add_argument(
        "--privacy",
        type=str,
        choices=["unlisted", "public", "private"],
        default=None,
        help="YouTube Shorts upload privacy status.",
    )
    parser.add_argument(
        "--whisper-model",
        type=str,
        default=None,
        help="Whisper model size (base, small, medium, large-v3).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run audio/chat hype detection without rendering video clips.",
    )
    return parser.parse_args()


def display_summary_table(clips: List[Dict[str, Any]]):
    """Print formatted summary of generated clips."""
    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        table = Table(title="GTA6-HypeClipper - Generated YouTube Shorts", show_lines=True)
        table.add_column("Clip #", justify="center", style="cyan", no_wrap=True)
        table.add_column("Start (s)", justify="right", style="green")
        table.add_column("End (s)", justify="right", style="green")
        table.add_column("Duration", justify="right")
        table.add_column("Hype Score", justify="right", style="bold yellow")
        table.add_column("Output File", style="magenta")
        table.add_column("YouTube URL", style="blue")

        for c in clips:
            yt_url = c.get("youtube_url", "Not Uploaded")
            table.add_row(
                str(c["clip_id"]),
                f"{c['start_time']:.1f}s",
                f"{c['end_time']:.1f}s",
                f"{c['duration']:.1f}s",
                f"{c['hype_score']:.3f}",
                os.path.basename(c.get("output_path", "N/A")),
                yt_url,
            )
        console.print(table)
    except ImportError:
        # Standard ASCII table fallback
        print("\n" + "=" * 80)
        print("GTA6-HypeClipper - Generated YouTube Shorts Summary")
        print("=" * 80)
        header = f"{'Clip #':<8} {'Start':<10} {'End':<10} {'Duration':<10} {'Hype Score':<12} {'Output File':<25}"
        print(header)
        print("-" * 80)
        for c in clips:
            out_file = os.path.basename(c.get("output_path", "N/A"))
            print(
                f"{c['clip_id']:<8} {c['start_time']:<10.1f} {c['end_time']:<10.1f} "
                f"{c['duration']:<10.1f} {c['hype_score']:<12.3f} {out_file:<25}"
            )
        print("=" * 80 + "\n")


def run_pipeline(args: argparse.Namespace):
    """
    Main execution pipeline orchestrating all 6 modules:
    1. Ingestion & Chat Scraping
    2. Hype Moment Detection
    3. Faster-Whisper Word Transcription & Hormozi ASS Generation
    4. 60fps Split-Screen FFmpeg Rendering
    5. Optional YouTube Upload
    """
    print_banner()

    # Load configuration
    config = load_yaml_config(args.config)

    # CLI Overrides
    if args.max_clips:
        config.setdefault("hype_detection", {})["max_clips"] = args.max_clips
    if args.preset:
        config.setdefault("layout", {})["active_preset"] = args.preset
    if args.whisper_model:
        config.setdefault("captions", {})["whisper_model"] = args.whisper_model
    if args.privacy:
        config.setdefault("uploader", {})["privacy_status"] = args.privacy
    if args.upload:
        config.setdefault("uploader", {})["enabled"] = True

    temp_dir = config.get("ingest", {}).get("temp_dir", "temp")
    output_dir = args.output_dir or config.get("ingest", {}).get("output_dir", "output")
    facecam_preset = config.get("layout", {}).get("active_preset", "top_left_cam")

    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    logger.info(f"Input Target: {args.url}")
    logger.info(f"Facecam Preset: {facecam_preset}")
    logger.info(f"Max Clips Target: {config.get('hype_detection', {}).get('max_clips', 5)}")

    # Step 1: Ingestion & 16kHz Audio Extraction
    logger.info("\n>>> [STEP 1/5] Ingesting Stream/VOD & Extracting 16kHz Audio...")
    video_path, metadata = ingest.download_video(
        url=args.url,
        output_dir=temp_dir,
        target_resolution=config.get("ingest", {}).get("target_resolution", "1080"),
        max_fps=config.get("ingest", {}).get("max_fps", 60),
    )

    # Chat extraction
    chat_json_path = None
    if config.get("ingest", {}).get("download_chat", True):
        chat_json_path = ingest.download_live_chat(
            url=args.url,
            output_dir=temp_dir,
            video_id=metadata.get("id"),
        )

    # 16kHz Audio extraction for fast processing
    audio_16k_path = ingest.extract_audio_16k(video_path)

    # Step 2: Hype Moment Detection Engine
    logger.info("\n>>> [STEP 2/5] Running Hype Detection Engine (Audio Spikes + Chat Velocity)...")
    detected_clips = hype_detector.detect_hype_moments(
        audio_path=audio_16k_path,
        chat_json_path=chat_json_path,
        total_duration=metadata.get("duration", 0.0),
        config=config,
    )

    if not detected_clips:
        logger.warning("No hype moments detected meeting criteria. Exiting.")
        return

    if args.dry_run:
        logger.info("\n[DRY RUN] Hype clips detected successfully. Skipping video rendering:")
        for c in detected_clips:
            print(f"  - Clip #{c['clip_id']}: {c['start_time']}s -> {c['end_time']}s (Peak: {c['peak_time']}s, Score: {c['hype_score']})")
        return

    # Step 3 & 4: Clip Processing, Captioning & FFmpeg Rendering
    logger.info(f"\n>>> [STEP 3 & 4/5] Processing {len(detected_clips)} Clips with Hormozi Captions & Split-Screen...")
    rendered_clips: List[Dict[str, Any]] = []

    ffmpeg_bin = video_editor.find_ffmpeg_binary()

    for clip in detected_clips:
        c_id = clip["clip_id"]
        logger.info(f"\n--- Processing Clip #{c_id} ({clip['start_time']}s to {clip['end_time']}s) ---")

        clip_audio_slice = os.path.join(temp_dir, f"clip_{c_id}_audio.wav")
        clip_ass_path = os.path.join(temp_dir, f"clip_{c_id}_captions.ass")
        clip_output_mp4 = os.path.join(output_dir, f"GTA6_Short_Clip_{c_id:02d}.mp4")

        # 3a. Extract exact audio slice for this clip
        slice_cmd = [
            ffmpeg_bin,
            "-y",
            "-ss", str(clip["start_time"]),
            "-to", str(clip["end_time"]),
            "-i", audio_16k_path,
            "-acodec", "copy",
            clip_audio_slice,
        ]
        try:
            subprocess.run(slice_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except Exception as e:
            logger.warning(f"Audio slice copy failed; falling back to re-encode: {e}")
            subprocess.run([
                ffmpeg_bin, "-y", "-ss", str(clip["start_time"]), "-to", str(clip["end_time"]),
                "-i", audio_16k_path, "-acodec", "pcm_s16le", clip_audio_slice
            ], check=True)

        # 3b. Transcribe and generate Hormozi-style dynamic ASS captions
        captions_file = None
        if config.get("captions", {}).get("enabled", True):
            try:
                captions_file = caption_generator.create_captions_for_clip(
                    clip_audio_path=clip_audio_slice,
                    output_ass_path=clip_ass_path,
                    config=config,
                )
            except Exception as cap_err:
                logger.warning(f"Failed to generate captions for Clip #{c_id}: {cap_err}. Proceeding without burned captions.")

        # 4. Render 9:16 vertical split-screen with burned subtitles via FFmpeg
        final_mp4 = video_editor.render_gaming_short(
            source_video_path=video_path,
            clip_info=clip,
            output_mp4_path=clip_output_mp4,
            subtitles_ass_path=captions_file,
            facecam_preset=facecam_preset,
            config=config,
        )

        clip["output_path"] = final_mp4
        rendered_clips.append(clip)

    # Step 5: Optional YouTube Upload
    if config.get("uploader", {}).get("enabled", False):
        logger.info("\n>>> [STEP 5/5] Uploading Generated Shorts to YouTube...")
        rendered_clips = uploader.upload_all_generated_shorts(rendered_clips, config=config)

    # Print summary
    display_summary_table(rendered_clips)
    logger.info(f"GTA6-HypeClipper pipeline completed! Generated {len(rendered_clips)} YouTube Shorts in '{output_dir}/'.")


def main():
    args = parse_arguments()
    try:
        run_pipeline(args)
    except KeyboardInterrupt:
        logger.info("\nPipeline cancelled by user.")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Pipeline encountered a critical error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

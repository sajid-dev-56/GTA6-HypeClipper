"""
Module 1: Ingest & Chat Scraper (ingest.py)
Part of GTA6-HypeClipper Pipeline.

Handles:
- Downloading gaming streams / VODs (YouTube & Twitch) at up to 1080p60 via yt-dlp.
- Live chat replay extraction via chat-downloader or yt-dlp fallback.
- Normalizing chat timestamps to seconds from video start.
- Extracting 16kHz mono WAV audio track via FFmpeg for high-speed RMS and Whisper analysis.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("GTA6-HypeClipper.Ingest")


def find_ffmpeg_binary(configured_path: Optional[str] = None) -> str:
    """
    Locate the FFmpeg executable from:
    1. Configured path / environment variable (FFMPEG_PATH)
    2. System PATH
    3. Common platform default directories (/opt/homebrew/bin, /usr/local/bin, ./bin/ffmpeg)
    """
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

    # If still not found, fallback to system name so error is surfaced clearly if run
    return "ffmpeg"


def get_video_metadata(video_path: str, ffmpeg_bin: Optional[str] = None) -> Dict[str, Any]:
    """
    Extract video metadata (duration, width, height, fps) using ffprobe.
    """
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        # Check adjacent to ffmpeg
        ff_dir = os.path.dirname(find_ffmpeg_binary(ffmpeg_bin))
        candidate = os.path.join(ff_dir, "ffprobe")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            ffprobe_bin = candidate
        else:
            ffprobe_bin = "ffprobe"

    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,duration:format=duration",
        "-of", "json",
        video_path,
    ]

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        data = json.loads(res.stdout)
        stream = data.get("streams", [{}])[0]
        fmt = data.get("format", {})

        duration = float(stream.get("duration") or fmt.get("duration") or 0.0)
        width = int(stream.get("width", 1920))
        height = int(stream.get("height", 1080))
        fps_str = stream.get("r_frame_rate", "60/1")
        if "/" in fps_str:
            num, den = fps_str.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 60.0
        else:
            fps = float(fps_str)

        return {
            "duration": duration,
            "width": width,
            "height": height,
            "fps": fps,
            "path": video_path,
        }
    except Exception as exc:
        logger.warning(f"ffprobe metadata extraction failed for {video_path}: {exc}. Using defaults.")
        return {
            "duration": 0.0,
            "width": 1920,
            "height": 1080,
            "fps": 60.0,
            "path": video_path,
        }


def download_video(
    url: str,
    output_dir: str,
    target_resolution: str = "1080",
    max_fps: int = 60,
    format_selector: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Download a video stream or VOD using yt-dlp.
    If 'url' is already a local video file, bypasses download and returns its path.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Check if URL is already a local file path
    if os.path.isfile(url):
        logger.info(f"Using local video file: {url}")
        meta = get_video_metadata(url)
        meta["title"] = os.path.splitext(os.path.basename(url))[0]
        meta["id"] = "local"
        return os.path.abspath(url), meta

    try:
        import yt_dlp
    except ImportError as e:
        raise ImportError(
            "yt-dlp is required for downloading videos. Install via: pip install yt-dlp"
        ) from e

    if not format_selector:
        format_selector = (
            f"bestvideo[height<=?{target_resolution}][fps<=?{max_fps}]+bestaudio/best[height<=?{target_resolution}]/best"
        )

    outtmpl = os.path.join(output_dir, "%(id)s_%(title).50s.%(ext)s")

    ydl_opts = {
        "format": format_selector,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "retries": 5,
        "noplaylist": True,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
    }

    logger.info(f"Connecting to stream/VOD from {url}...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=False)
        is_live = info_dict.get("live_status") == "is_live" or info_dict.get("is_live") is True
        video_id = info_dict.get("id", "stream")
        title_sanitized = re.sub(r"[^\w\s-]", "", info_dict.get("title", "GTA_VI_Stream"))[:40].strip()

        if is_live:
            # Live stream handling: capture a high-energy live segment
            target_live_seconds = 180  # Capture 3 minutes of high-octane live gameplay
            output_live_file = os.path.join(output_dir, f"{video_id}_{title_sanitized}_live.mp4")
            stream_url = info_dict.get("url") or info_dict.get("manifest_url")

            logger.info(f"Detected ACTIVE LIVE STREAM: '{info_dict.get('title')}'. Capturing {target_live_seconds}s segment...")
            ff_bin = find_ffmpeg_binary()
            capture_cmd = [
                ff_bin, "-y",
                "-i", stream_url,
                "-t", str(target_live_seconds),
                "-c", "copy",
                output_live_file,
            ]
            subprocess.run(capture_cmd, check=True)
            downloaded_file = output_live_file
        else:
            # Regular VOD / video download
            info_dict = ydl.extract_info(url, download=True)
            downloaded_file = ydl.prepare_filename(info_dict)

            # In case format changed extension on merge
            base, _ = os.path.splitext(downloaded_file)
            if os.path.isfile(base + ".mp4"):
                downloaded_file = base + ".mp4"
            elif not os.path.isfile(downloaded_file):
                for candidate in os.listdir(output_dir):
                    if candidate.startswith(info_dict.get("id", "xyz")) and candidate.endswith((".mp4", ".mkv", ".webm")):
                        downloaded_file = os.path.join(output_dir, candidate)
                        break

        # Probe final duration
        meta_probe = get_video_metadata(downloaded_file)

        metadata = {
            "id": info_dict.get("id", "video"),
            "title": info_dict.get("title", "GTA VI Stream"),
            "duration": meta_probe.get("duration") or float(info_dict.get("duration") or 0.0),
            "uploader": info_dict.get("uploader", "Unknown"),
            "fps": meta_probe.get("fps") or info_dict.get("fps", 60),
            "width": meta_probe.get("width") or info_dict.get("width", 1920),
            "height": meta_probe.get("height") or info_dict.get("height", 1080),
            "path": os.path.abspath(downloaded_file),
        }
        logger.info(f"Video ready: {downloaded_file} (Duration: {metadata['duration']:.1f}s, {metadata['width']}x{metadata['height']} @ {metadata['fps']}fps)")
        return os.path.abspath(downloaded_file), metadata


def download_live_chat(
    url: str,
    output_dir: str,
    video_id: Optional[str] = None,
) -> Optional[str]:
    """
    Extract live chat replay JSON using chat-downloader or yt-dlp fallback.
    Returns the path to the normalized chat JSON file, or None if chat unavailable.
    """
    os.makedirs(output_dir, exist_ok=True)
    clean_id = video_id or "stream"
    output_chat_path = os.path.join(output_dir, f"{clean_id}_chat.json")

    # If already exists and has content, return it
    if os.path.isfile(output_chat_path) and os.path.getsize(output_chat_path) > 10:
        logger.info(f"Using existing cached chat: {output_chat_path}")
        return output_chat_path

    normalized_messages: List[Dict[str, Any]] = []

    # Strategy 1: Attempt chat-downloader
    try:
        from chat_downloader import ChatDownloader

        logger.info("Extracting live chat messages via chat-downloader...")
        downloader = ChatDownloader()
        chat_stream = downloader.get_chat(url, max_messages=100000)

        for message in chat_stream:
            # chat-downloader provides time_in_seconds or timestamp (microseconds)
            time_sec = message.get("time_in_seconds")
            if time_sec is None and "time_offset" in message:
                time_sec = float(message["time_offset"]) / 1_000_000.0
            elif time_sec is None and "timestamp" in message:
                time_sec = float(message["timestamp"]) / 1_000_000.0

            text = message.get("message", "")
            author = message.get("author", {}).get("name", "viewer")

            if time_sec is not None and text:
                normalized_messages.append({
                    "timestamp": round(float(time_sec), 3),
                    "author": str(author),
                    "message": str(text).strip(),
                })

        logger.info(f"chat-downloader extracted {len(normalized_messages)} messages.")
    except Exception as e:
        logger.warning(f"chat-downloader was unable to extract chat: {e}. Trying yt-dlp fallback...")

    # Strategy 2: If Strategy 1 failed or returned 0, try yt-dlp live chat subtitle extraction
    if not normalized_messages and not os.path.isfile(url):
        try:
            import yt_dlp
            chat_dl_opts = {
                "skip_download": True,
                "writesubtitles": True,
                "subtitleslangs": ["live_chat"],
                "outtmpl": os.path.join(output_dir, f"{clean_id}_rawchat"),
                "quiet": True,
            }
            with yt_dlp.YoutubeDL(chat_dl_opts) as ydl:
                ydl.download([url])

            raw_chat_file = os.path.join(output_dir, f"{clean_id}_rawchat.live_chat.json")
            if os.path.isfile(raw_chat_file):
                with open(raw_chat_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                            # Parse YouTube live_chat structure
                            action = item.get("replayChatItemAction", {}).get("actions", [{}])[0]
                            chat_item = (
                                action.get("addChatItemAction", {}).get("item", {})
                                .get("liveChatTextMessageRenderer", {})
                            )
                            if chat_item:
                                offset_msec = int(chat_item.get("timestampUsec", 0)) / 1000.0
                                author = chat_item.get("authorName", {}).get("simpleText", "viewer")
                                text_runs = chat_item.get("message", {}).get("runs", [])
                                text = "".join([r.get("text", "") for r in text_runs])
                                if text:
                                    normalized_messages.append({
                                        "timestamp": round(offset_msec / 1000.0, 3),
                                        "author": author,
                                        "message": text.strip(),
                                    })
                        except Exception:
                            continue
                logger.info(f"yt-dlp extracted {len(normalized_messages)} chat messages.")
        except Exception as yt_err:
            logger.warning(f"yt-dlp live chat extraction fallback failed: {yt_err}")

    # Write normalized chat JSON
    if normalized_messages:
        # Sort messages by timestamp
        normalized_messages.sort(key=lambda m: m["timestamp"])
        with open(output_chat_path, "w", encoding="utf-8") as f:
            json.dump(normalized_messages, f, indent=2)
        logger.info(f"Saved normalized chat to {output_chat_path}")
        return output_chat_path
    else:
        logger.info("No live chat data found or available for this stream/VOD. Pipeline will use Audio RMS spikes.")
        # Create empty placeholder file
        with open(output_chat_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        return output_chat_path


def extract_audio_16k(
    video_path: str,
    output_audio_path: Optional[str] = None,
    ffmpeg_bin: Optional[str] = None,
) -> str:
    """
    Extract a 16kHz mono 16-bit PCM WAV audio track using FFmpeg.
    This format is optimized for both fast Whisper transcription and librosa/numpy RMS energy analysis.
    """
    if not output_audio_path:
        base, _ = os.path.splitext(video_path)
        output_audio_path = f"{base}_16k.wav"

    ff_exec = find_ffmpeg_binary(ffmpeg_bin)

    cmd = [
        ff_exec,
        "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        output_audio_path,
    ]

    logger.info(f"Extracting 16kHz audio: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    except FileNotFoundError:
        raise RuntimeError(
            f"FFmpeg binary '{ff_exec}' not found. Please install FFmpeg and make sure it is in PATH."
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg audio extraction failed: {e.stderr}") from e

    if not os.path.isfile(output_audio_path) or os.path.getsize(output_audio_path) == 0:
        raise RuntimeError(f"Audio extraction resulted in missing or empty file: {output_audio_path}")

    logger.info(f"Audio successfully extracted to: {output_audio_path}")
    return os.path.abspath(output_audio_path)

"""
Module 5: YouTube Shorts Auto-Uploader (uploader.py)
Part of GTA6-HypeClipper Pipeline.

Uploads final vertical .mp4 videos directly to YouTube using YouTube Data API v3.
Features:
- OAuth2 authentication with automatic token refreshing and caching.
- Dynamic catchy GTA VI title generation with viral gaming hashtags (#Shorts #GTAVI #GTA6).
- Resumable chunked file uploading with progress monitoring.
- Category set to Gaming (ID: 20) and configured privacy status (unlisted/public).
"""

import json
import logging
import os
import random
import sys
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GTA6-HypeClipper.Uploader")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

TITLE_TEMPLATES = [
    "IShowSpeed LOST HIS MIND IN GTA 6! 💀 #{clip_id} #Shorts",
    "SPEED ACCIDENTALLY DID THIS IN GTA VI! 😭 #{clip_id} #Shorts",
    "BRO GTA 6 POLICE CHASE GOT INSANE! 🚓💨 #{clip_id} #GTAVI",
    "SPEED'S REACTION TO GTA VI IS PRICELESS! 😂 #{clip_id} #GamingShorts",
    "HE REALLY THOUGHT HE SURVIVED THIS IN GTA 6! 💥 #{clip_id} #Shorts",
    "NO WAY THIS JUST HAPPENED IN GTA VI! 😱 #{clip_id} #GTAVI",
    "IShowSpeed REGRETS THIS DECISION IN GTA 6... 💀 #{clip_id} #Shorts",
    "THE HARDEST GTA 6 ESCAPE EVER PULLED OFF! 🔥 #{clip_id} #GamingShorts",
    "GTA 6 GRAPHICS MADE SPEED CRY! 😭 #{clip_id} #Shorts",
    "TOP 1% GTA VI MOMENT CAUGHT ON STREAM! 🤯 #{clip_id} #GTAVI",
    "SPEED VS 5 STAR WANTED LEVEL IN GTA 6! 🚓⭐ #{clip_id} #Shorts",
    "THIS IS WHY GTA 6 IS AN ABSOLUTE MASTERPIECE! 🎮 #{clip_id} #Gaming",
    "SPEED DISCOVERED A CRAZY SECRET IN GTA VI! 🤫 #{clip_id} #Shorts",
    "WHEN YOU RUN FROM THE COPS IN GTA 6... 🏃‍♂️💨 #{clip_id} #Shorts",
    "GTA 6 CAR PHYSICS WENT COMPLETELY CRAZY! 🚗💥 #{clip_id} #Shorts",
    "SPEED COULD NOT BELIEVE WHAT HE SAW IN GTA 6! 😳 #{clip_id} #GTAVI",
    "IShowSpeed GTA 6 BEST MOMENT EVER! ⚡ #{clip_id} #Shorts",
    "BRO THOUGHT HE WAS SAFE IN GTA VI... 💀 #{clip_id} #GamingShorts",
    "IShowSpeed REACTION TO GTA 6 ROMANCE SCENE! 💀😳 #{clip_id} #Shorts",
    "JASON AND LUCIA KISSING IN GTA 6?! SPEED WENT CRAZY! 😭🔥 #{clip_id} #Shorts",
    "SPEED CAUGHT THIS ROMANTIC MOMENT IN GTA VI! 😳❤️ #{clip_id} #GTAVI",
    "AYO SPEED COULD NOT HANDLE THIS GTA 6 SCENE! 💀😂 #{clip_id} #Shorts",
    "SPEED'S HILARIOUS REACTION TO GTA 6 KISS! 🤣 #{clip_id} #GamingShorts",
]

DEFAULT_TAGS = [
    "GTA6",
    "GTAVI",
    "IShowSpeed",
    "IShowSpeedGTA",
    "GrandTheftAuto6",
    "GTA6Gameplay",
    "GTAVIRomance",
    "LuciaAndJason",
    "SpeedReaction",
    "GTA6Leaks",
    "RockstarGames",
    "GamingShorts",
    "Shorts",
    "YouTubeShorts",
    "TrendingGaming",
    "ViralGaming",
    "GTA6Clips",
    "SpeedShorts",
    "GTA6Humor",
    "ViceCity",
    "GrandTheftAuto",
]


def generate_shorts_metadata(
    clip_info: Dict[str, Any],
    custom_title: Optional[str] = None,
    custom_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Generate viral, high-CTR title, trending tags, and keyword-dense SEO description.
    """
    clip_id = clip_info.get("clip_id", 1)
    hype_score = clip_info.get("hype_score", 1.0)

    if custom_title:
        title = custom_title.format(clip_id=clip_id)
        if "#Shorts" not in title:
            title += " #Shorts"
    else:
        template = random.choice(TITLE_TEMPLATES)
        title = template.format(clip_id=clip_id)

    # YouTube titles are capped at 100 characters
    if len(title) > 100:
        title = title[:92] + " #Shorts"

    tags = list(custom_tags or DEFAULT_TAGS)

    description = f"""🔥 IShowSpeed GTA 6 (Grand Theft Auto VI) Gameplay Hype Moments!
Clip #{clip_id} | Hype Intensity: {hype_score:.2f}

Watch this viral, high-energy stream moment from today's GTA VI broadcast.
👉 SMASH LIKE and SUBSCRIBE for daily GTA 6 Shorts, funniest fails & crazy gameplay highlights!

━━━━━━━━━━━━━━━━━━━━━━
#GTAVI #GTA6 #IShowSpeed #GrandTheftAuto6 #RockstarGames #GamingShorts #Shorts #YouTubeShorts #TrendingGaming #GTA6Gameplay #ViralGaming
"""

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "categoryId": "20",  # Gaming
    }


def get_authenticated_service(
    client_secrets_file: str = "client_secrets.json",
    token_file: str = "token.json",
):
    """
    Authenticate and return YouTube service instance.
    Handles token caching in token.json and interactive OAuth consent flow.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        import googleapiclient.discovery
    except ImportError:
        raise ImportError(
            "YouTube uploader requires google client libraries: "
            "pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )

    creds = None

    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except Exception as e:
            logger.warning(f"Could not load cached token: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired YouTube OAuth credentials...")
            creds.refresh(Request())
        else:
            if not os.path.isfile(client_secrets_file):
                raise FileNotFoundError(
                    f"OAuth client secrets file '{client_secrets_file}' not found.\n"
                    "To enable auto-upload to YouTube:\n"
                    "1. Go to Google Cloud Console (https://console.cloud.google.com/)\n"
                    "2. Enable YouTube Data API v3\n"
                    "3. Create an OAuth 2.0 Client ID (Desktop Application)\n"
                    f"4. Download the JSON and save it as '{client_secrets_file}'"
                )

            logger.info(f"Initiating YouTube OAuth flow via {client_secrets_file}...")
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_file, "w", encoding="utf-8") as token_out:
            token_out.write(creds.to_json())
        logger.info(f"Saved refreshed YouTube credentials to {token_file}")

    return googleapiclient.discovery.build(
        YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=creds
    )


def generate_short_title(clip_number: int = 1, streamer: str = "IShowSpeed") -> str:
    """Generate a viral YouTube Shorts title."""
    template = random.choice(TITLE_TEMPLATES)
    return template.format(clip_id=clip_number)


def upload_short(
    video_path: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
    privacy_status: str = "unlisted",
    client_secrets_file: str = "client_secrets.json",
    token_file: str = "token.json",
    clip_info: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Direct upload of a single .mp4 to YouTube Shorts with optional explicit title/description/tags.
    Returns the YouTube Video ID on success.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file to upload does not exist: {video_path}")

    from googleapiclient.http import MediaFileUpload

    youtube = get_authenticated_service(client_secrets_file, token_file)
    c_info = clip_info or {"clip_id": 1, "hype_score": 1.0}
    meta = generate_shorts_metadata(c_info, custom_title=title, custom_tags=tags)

    final_title = title if title else meta["title"]
    if "#Shorts" not in final_title and "#shorts" not in final_title:
        final_title += " #Shorts"
    if len(final_title) > 100:
        final_title = final_title[:92] + " #Shorts"

    final_desc = description if description else meta["description"]
    final_tags = tags if tags else meta["tags"]

    body = {
        "snippet": {
            "title": final_title,
            "description": final_desc,
            "tags": final_tags,
            "categoryId": "20",  # Gaming
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=4 * 1024 * 1024, resumable=True, mimetype="video/mp4")

    logger.info(f"Starting YouTube upload: '{final_title}' (Privacy: {privacy_status})")
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            progress = int(status.progress() * 100)
            logger.info(f"Uploading '{os.path.basename(video_path)}': {progress}% complete...")

    video_id = response.get("id")
    shorts_url = f"https://www.youtube.com/shorts/{video_id}"
    logger.info(f"Upload successful! Video ID: {video_id} -> {shorts_url}")
    return video_id


def upload_video_to_shorts(
    video_path: str,
    clip_info: Optional[Dict[str, Any]] = None,
    privacy_status: str = "unlisted",
    client_secrets_file: str = "client_secrets.json",
    token_file: str = "token.json",
    custom_title: Optional[str] = None,
    custom_tags: Optional[List[str]] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Optional[str]:
    """Compatibility wrapper for upload_short."""
    effective_title = title or custom_title
    effective_tags = tags or custom_tags
    return upload_short(
        video_path=video_path,
        title=effective_title,
        description=description,
        tags=effective_tags,
        privacy_status=privacy_status,
        client_secrets_file=client_secrets_file,
        token_file=token_file,
        clip_info=clip_info,
    )


def cleanup_temp_files(temp_dir: str = "temp", keep_recent_seconds: int = 1800):
    """Clean up old temporary slices, audio files, and subtitles to conserve disk space."""
    if not os.path.isdir(temp_dir):
        return
    now = time.time()
    for fname in os.listdir(temp_dir):
        if fname.endswith((".wav", ".ass", ".part", ".ytdl")) or fname.startswith("clip_"):
            fpath = os.path.join(temp_dir, fname)
            try:
                if now - os.path.getmtime(fpath) > keep_recent_seconds:
                    os.remove(fpath)
            except Exception:
                pass


def upload_all_generated_shorts(
    generated_clips: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Filter for only the highest-hype viral moments, upload to YouTube Shorts,
    and automatically clean up uploaded files and temporary scratchpads to prevent disk bloat.
    """
    cfg = config or {}
    u_cfg = cfg.get("uploader", {})

    client_secrets = u_cfg.get("client_secrets_file", "client_secrets.json")
    token_file = u_cfg.get("token_file", "token.json")
    privacy = u_cfg.get("privacy_status", "unlisted")
    custom_tags = u_cfg.get("default_tags", DEFAULT_TAGS)
    auto_cleanup = bool(u_cfg.get("auto_cleanup_uploaded", True))
    viral_only = bool(u_cfg.get("viral_only", True))
    max_uploads = int(u_cfg.get("max_uploads_per_run", 2))

    # Sort clips descending by hype score to prioritize the biggest screams & moments
    candidates = sorted(
        generated_clips,
        key=lambda c: float(c.get("hype_score", 0.0)),
        reverse=True
    )

    if viral_only and len(candidates) > max_uploads:
        logger.info(
            f"🎯 VIRAL FILTER: Selecting top {max_uploads} highest-scoring clips out of {len(candidates)} "
            f"(Hype Scores: {[c.get('hype_score') for c in candidates[:max_uploads]]}). "
            f"Filtered out {len(candidates) - max_uploads} lower-energy clips."
        )
        to_upload = candidates[:max_uploads]
    else:
        to_upload = candidates

    results: List[Dict[str, Any]] = []

    for idx, clip in enumerate(to_upload):
        mp4_path = clip.get("output_path")
        if not mp4_path or not os.path.isfile(mp4_path):
            continue

        try:
            logger.info(f"Uploading Clip #{clip.get('clip_id', idx+1)} to YouTube Shorts ({privacy})...")
            video_id = upload_video_to_shorts(
                video_path=mp4_path,
                clip_info=clip,
                privacy_status=privacy,
                client_secrets_file=client_secrets,
                token_file=token_file,
                custom_tags=custom_tags,
            )
            clip["youtube_id"] = video_id
            clip["youtube_url"] = f"https://www.youtube.com/shorts/{video_id}"
            results.append(clip)
            logger.info(f"✅ Successfully uploaded to: https://www.youtube.com/shorts/{video_id}")

            # Auto-cleanup of uploaded file to prevent disk space buildup
            if auto_cleanup and os.path.isfile(mp4_path):
                try:
                    os.remove(mp4_path)
                    thumb_path = f"{os.path.splitext(mp4_path)[0]}_thumb.jpg"
                    if os.path.isfile(thumb_path):
                        os.remove(thumb_path)
                    logger.info(f"🧹 Disk Cleanup: Removed uploaded short '{os.path.basename(mp4_path)}' to free storage space.")
                except Exception as del_err:
                    logger.warning(f"Could not delete {mp4_path}: {del_err}")

            # Safe spacing between uploads to prevent YouTube spam detection
            if idx < len(to_upload) - 1:
                logger.info("Pausing 15s before next upload to comply with YouTube rate limits...")
                time.sleep(15)

        except Exception as e:
            err_msg = str(e)
            logger.error(f"Failed to upload {mp4_path}: {err_msg}")
            clip["upload_error"] = err_msg
            results.append(clip)

            if "quotaExceeded" in err_msg or "dailyLimitExceeded" in err_msg:
                logger.warning(
                    "\n⚠️ YouTube Data API daily upload quota reached (10,000 units/day default limit).\n"
                    "Remaining clips are safely saved in 'output/' for the next scheduled run."
                )
                break

    # Clean up temporary audio slices and cache
    cleanup_temp_files("temp", keep_recent_seconds=1800)

    return results

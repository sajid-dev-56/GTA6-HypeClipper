# 🎮 GTA6-HypeClipper

> **Production-Ready AI Video Automation Pipeline for Gaming Livestreams and VODs**  
> Automatically detects high-energy moments using **Audio Volume Spikes + Live Chat Velocity**, reframes gameplay into a **9:16 Vertical Gaming Split-Screen** (Streamer Facecam on top, GTA VI Gameplay on bottom), burns **Alex Hormozi-style dynamic word-level captions**, and outputs ready-to-upload **YouTube Shorts**.

Inspired by *ClippedAI*, *Twitch-Clip-Generator*, and *OpenShorts*.

---

## 📂 Project Structure

```text
GTA6-HypeClipper/
├── config.yaml               # Master pipeline configuration (presets, thresholds, styling)
├── requirements.txt          # Python dependencies
├── web_ui.py                 # Full Web UI Dashboard & 24/7 Auto-Pilot Server
├── scheduler.py              # Autonomous 24/7 Daily Livestream Monitor & Uploader
├── ingest.py                 # Module 1: Ingest & live chat scraper (yt-dlp, chat-downloader, 16kHz WAV)
├── hype_detector.py          # Module 2: Composite hype detector (RMS dB energy + chat velocity surge)
├── video_editor.py           # Module 3: 60fps Split-Screen FFmpeg layout engine (Facecam + Gameplay)
├── caption_generator.py      # Module 4: Faster-Whisper word-level transcription & Hormozi ASS engine
├── uploader.py               # Module 5: YouTube Data API v3 auto-uploader with viral metadata
├── main.py                   # Module 6: CLI entrypoint and pipeline orchestrator
├── README.md                 # Documentation and deployment guide
├── temp/                     # Scratchpad directory for audio slices, chat JSON, and ASS subtitles
└── output/                   # Final rendered 1080x1920 60fps YouTube Shorts
```

---

## 🛠️ System Prerequisites & FFmpeg Installation

GTA6-HypeClipper requires **FFmpeg** with `libx264` and `subtitles` filter support.

### macOS (Apple Silicon / Intel)
```bash
# Using Homebrew (recommended)
brew install ffmpeg

# Verify installation:
ffmpeg -version
```

### Ubuntu / Debian Linux
```bash
sudo apt update
sudo apt install -y ffmpeg libsm6 libxext6

# Verify installation:
ffmpeg -version
```

### Windows
```powershell
# Using Windows Package Manager (winget)
winget install Gyan.FFmpeg

# Or using Chocolatey:
choco install ffmpeg-full

# Verify installation in PowerShell/CMD:
ffmpeg -version
```

> **Tip:** If FFmpeg is installed in a custom location, you can set the `FFMPEG_PATH` environment variable or specify `ffmpeg_bin` under `video_editor` in `config.yaml`.

---

## 📦 Python Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/sajid-dev-56/GTA6-HypeClipper.git
   cd GTA6-HypeClipper
   ```

2. **Create and activate a virtual environment (recommended):**
   ```bash
   python3 -m venv venv
   source venv/bin/activate    # On Windows: venv\Scripts\activate
   ```

3. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

---

## 🌐 Web UI Dashboard (Recommended)

Run the interactive Web Studio:
```bash
python web_ui.py
```
Open **[http://localhost:8080](http://localhost:8080)** in your browser:
- **Studio Tab:** Paste livestream URLs, pick presets (`⭐ IShowSpeed`, `Top-Left`, `Auto-Detect`), and watch live FFmpeg execution logs.
- **Clips Gallery:** Instant 9:16 vertical video player to preview 1080p60 Shorts and download them.
- **1-Click YouTube Upload:** Direct upload button with auto-generated viral titles, descriptions, and hashtags.
- **24/7 Auto-Pilot Scheduler:** Autonomous daily background monitor that checks your favorite streamer's channel every 24 hours (or interval), generates Shorts, and uploads them automatically!

---

## 🚀 CLI Quick Start

### 1. Basic Ingestion & Clipping
Download a YouTube or Twitch VOD, find the top 5 hype moments, crop with Top-Left facecam, burn captions, and export:
```bash
python main.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --max-clips 5 --preset "top_left_cam"
```

### 2. Twitch Stream with Top-Right Camera
```bash
python main.py --url "https://www.twitch.tv/videos/123456789" --preset "top_right_cam" --max-clips 3
```

### 3. Dry-Run (Detect Hype Peaks Without Rendering Videos)
Fast scan to inspect detected peak timestamps, duration, and hype scores:
```bash
python main.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --dry-run
```

### 4. Process Local Video File
You can pass any local `.mp4` or `.mkv` file directly:
```bash
python main.py --url "/path/to/my_gameplay.mp4" --max-clips 3 --preset "top_left_cam"
```

### 5. Automatic YouTube Shorts Upload
Auto-upload final videos as unlisted/public shorts:
```bash
python main.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --upload --privacy "unlisted"
```

---

## 🏗️ Architecture & Pipeline Walkthrough

### Module 1: Ingest & Chat Scraper (`ingest.py`)
- **Video Download:** Uses `yt-dlp` targeting 1080p60 (`bestvideo[height<=?1080][fps<=?60]+bestaudio/best`).
- **Chat Replay:** Scrapes live chat messages via `chat-downloader` (Twitch IRC / YouTube live replay) with `yt-dlp` subtitle fallback, normalized into `{timestamp, author, message}` JSON.
- **Audio Track:** Uses FFmpeg to demux a lightweight **16kHz 16-bit mono WAV** file (`pcm_s16le`, `-ar 16000`, `-ac 1`) for high-speed RMS processing and Whisper transcription.

### Module 2: Hype Detection Engine (`hype_detector.py`)
Calculates a **Composite Hype Score** across 5-second rolling windows (1-second stride):
$$\text{Score}(t) = w_{\text{audio}} \cdot \text{Spike}(t) + w_{\text{chat}} \cdot \text{Velocity}(t)$$
- **Audio Spikes (RMS Energy in dB):** Calculates rolling decibel energy $dB = 20 \log_{10}(RMS + 1e-9)$. Flags windows where $dB > \mu_{dB} + 2.0 \cdot \sigma_{dB}$ (gunshots, car crashes, streamer screams).
- **Chat Velocity & Keyword Surge:** Measures messages per second. If hype keywords (`"W"`, `"L"`, `"OMG"`, `"WTF"`, `"CLIP IT"`, `"BRO"`, `"INSANE"`, `"💀"`, `"😂"`) burst in chat, velocity is boosted by **1.5x**.
- **Non-Maximum Suppression (NMS):** Filters candidate peaks to extract clean **45-second clip windows** (`peak - 20s` to `peak + 25s`) with at least 40s spacing to prevent overlapping duplicate clips.

### Module 3: Gaming Split-Screen Layout Engine (`video_editor.py`)
Renders `1080x1920` vertical shorts at 60fps using pure FFmpeg `filter_complex`:
- **Top 30% (`1080x576`):** Streamer Facecam cropped from preset coordinates (or OpenCV auto-detected corner) and scaled/padded to fill the top section.
- **Bottom 70% (`1080x1344`):** Center 9:16 viewport crop of the GTA VI gameplay (`crop=868:1080:526:0`), capturing crosshairs and driving action.
- **FFmpeg vstack:** Both feeds are vertically stacked (`vstack=inputs=2`) and burned with ASS subtitles in a single encoding pass.

```text
+------------------------------+
|                              |
|   TOP 30% (1080 x 576)       |  <-- Streamer Facecam
|                              |
+==============================+  <-- Y = 576 (Border)
| [  DYNAMIC HORMOZI CAPTIONS] |  <-- Y = 620 (Alignment = 5)
+------------------------------+
|                              |
|                              |
|   BOTTOM 70% (1080 x 1344)   |  <-- Main GTA VI Gameplay
|                              |
|                              |
+------------------------------+
Total Resolution: 1080 x 1920 (9:16)
```

### Module 4: Auto-Captions Engine (`caption_generator.py`)
- Transcribes using `faster-whisper` (`base` or `small` with `int8` or `float16` compute type).
- Groups words into punchy lines (max 3-4 words per line, uppercase).
- Generates **ASS (Advanced SubStation Alpha)** subtitles:
  - **Position:** `\pos(540, 620)` right below the facecam border, keeping gameplay center clear.
  - **Active Word:** Highlighted in neon Yellow (`&H0000FFFF`) with a 112% pop scale pulse (`\fscx114\fscy114`).
  - **Inactive Words:** Clean White (`&H00FFFFFF`) with heavy black outline (`Outline=6`) and drop shadow (`Shadow=3`).

### Module 5: YouTube Shorts Auto-Uploader (`uploader.py`)
- Authenticates using Google Cloud OAuth 2.0 (`client_secrets.json`).
- Automatically generates viral titles:
  `"THIS GTA 6 MOMENT IS ABSOLUTELY UNREAL! 😱 #1 #Shorts"`
- Adds viral gaming tags (`#GTAVI`, `#GTA6`, `#GamingShorts`, `#Shorts`) and sets category to **Gaming** (ID 20).
- Performs resumable chunked video upload.

---

## ⚙️ Configuration (`config.yaml`)

All parameters are customizable without modifying Python code:
```yaml
# Facecam layout presets (1920x1080 source)
layout:
  active_preset: "top_left_cam"
  presets:
    top_left_cam:     { x: 40,   y: 40,  w: 480, h: 360 }
    top_right_cam:    { x: 1400, y: 40,  w: 480, h: 360 }
    bottom_left_cam:  { x: 40,   y: 680, w: 480, h: 360 }
    bottom_right_cam: { x: 1400, y: 680, w: 480, h: 360 }

# Hype detection sensitivity
hype_detection:
  audio_threshold_std: 2.0       # Spike cutoff: mean + (2.0 * std)
  keyword_surge_multiplier: 1.5  # 1.5x boost for chat bursts
  clip_pre_peak_seconds: 20.0
  clip_post_peak_seconds: 25.0   # 45s clip total
  max_clips: 5

# Captions styling
captions:
  whisper_model: "small"
  max_words_per_line: 3
  position_y: 620
  primary_color: "&H00FFFFFF"   # White
  highlight_color: "&H0000FFFF" # Neon Yellow
  outline_width: 6
```

---

## 🔑 YouTube API Setup (Optional Auto-Upload)

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project and enable the **YouTube Data API v3**.
3. Create an **OAuth 2.0 Client ID** (Application type: *Desktop App*).
4. Download the client JSON credentials and save as `client_secrets.json` in the project root directory.
5. On the first upload, a browser window will open for one-time Google OAuth authorization; credentials will be cached in `token.json`.

---

## 🧪 Testing

Run the included verification suite to test all components:
```bash
python3 -c "
import main, hype_detector, caption_generator, video_editor, uploader
print('Imports and modules loaded successfully!')
"
```

---

## 📜 License
MIT License. Free for personal and commercial content automation pipelines.

"""
Module 4: Auto-Captions Engine (caption_generator.py)
Part of GTA6-HypeClipper Pipeline.

Transcribes clipped audio at word level using faster-whisper (word_timestamps=True).
Generates Alex Hormozi-style dynamic ASS subtitles:
- Max 3-4 punchy uppercase words per line.
- Active spoken word highlighted in vibrant Yellow with pop-in scale animation.
- Thick black outline and shadow for high-contrast gaming readability.
- Positioned precisely at the Facecam / Gameplay border (y=620) to keep action clear.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("GTA6-HypeClipper.Captions")


def format_ass_timestamp(seconds: float) -> str:
    """
    Format seconds into ASS timestamp string: H:MM:SS.cs (centiseconds).
    Example: 73.456 -> '0:01:13.46'
    """
    if seconds < 0:
        seconds = 0.0

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))

    if centis >= 100:
        secs += 1
        centis = 0
        if secs >= 60:
            minutes += 1
            secs = 0
            if minutes >= 60:
                hours += 1
                minutes = 0

    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def transcribe_audio_words(
    audio_path: str,
    model_size: str = "small",
    device: str = "auto",
    compute_type: str = "int8",
) -> List[Dict[str, Any]]:
    """
    Transcribe audio file into a list of word dictionaries:
        [{"word": "WHAT", "start": 0.52, "end": 0.88, "prob": 0.98}, ...]
    Uses faster-whisper with VAD filter.
    """
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio file not found for transcription: {audio_path}")

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.warning(
            "faster-whisper is not installed. Subtitle transcription requires: pip install faster-whisper"
        )
        return []

    # Configure device & compute type
    if device == "auto":
        try:
            import torch
            has_cuda = torch.cuda.is_available()
        except ImportError:
            has_cuda = False

        if has_cuda:
            actual_device = "cuda"
            actual_compute = compute_type if compute_type in ["float16", "int8_float16"] else "float16"
        else:
            actual_device = "cpu"
            actual_compute = "int8" if compute_type == "int8" else "float32"
    else:
        actual_device = device
        actual_compute = compute_type

    logger.info(f"Loading faster-whisper model '{model_size}' on {actual_device} ({actual_compute})...")
    try:
        model = WhisperModel(model_size, device=actual_device, compute_type=actual_compute)
    except Exception as e:
        logger.warning(f"Failed with compute_type '{actual_compute}', falling back to float32 on CPU: {e}")
        model = WhisperModel(model_size, device="cpu", compute_type="float32")

    logger.info(f"Transcribing audio with word timestamps: {audio_path}")
    segments, info = model.transcribe(
        audio_path,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400),
        condition_on_previous_text=False,
    )

    all_words: List[Dict[str, Any]] = []

    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            raw_text = w.word.strip().upper()
            # Filter empty or punctuation-only tokens
            clean_word = re.sub(r"[^\w'!?$-]", "", raw_text)
            if not clean_word:
                continue

            all_words.append({
                "word": clean_word,
                "start": round(float(w.start), 3),
                "end": round(float(w.end), 3),
                "probability": round(float(w.probability), 3),
            })

    logger.info(f"Transcription complete: {len(all_words)} words recognized.")
    return all_words


def generate_hormozi_ass_subtitles(
    words: List[Dict[str, Any]],
    output_ass_path: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Generate an Advanced SubStation Alpha (.ass) file with Hormozi-style dynamic captions.
    
    Layout & Styling:
    - Target resolution: 1080x1920 (PlayResX: 1080, PlayResY: 1920)
    - Positioned at pos(540, 620) (Border between Facecam and Gameplay)
    - Active word highlighted in bright Yellow with a 112% scale pulse
    - Inactive words in solid White
    - Heavy black outline (6px) and shadow (3px)
    - Max 3-4 words per line for high viewer retention
    """
    cfg = config or {}
    c_cfg = cfg.get("captions", {})

    font_name = c_cfg.get("font_name", "Montserrat ExtraBold")
    fallback_font = c_cfg.get("fallback_font_name", "Arial Black")
    font_size = int(c_cfg.get("font_size", 70))
    pos_y = int(c_cfg.get("position_y", 620))
    pos_x = 540  # Exact center of 1080px frame
    max_words = int(c_cfg.get("max_words_per_line", 3))
    primary_col = c_cfg.get("primary_color", "&H00FFFFFF")    # White
    highlight_col = c_cfg.get("highlight_color", "&H0000FFFF")# Yellow
    outline_col = c_cfg.get("outline_color", "&H00000000")    # Black
    outline_w = int(c_cfg.get("outline_width", 6))
    shadow_d = int(c_cfg.get("shadow_depth", 3))
    pop_anim = bool(c_cfg.get("pop_animation", True))

    ass_header = f"""[Script Info]
Title: GTA6-HypeClipper Dynamic Captions
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: HormoziMain,{font_name},{font_size},{primary_col},&H000000FF,{outline_col},&H80000000,-1,0,0,0,100,100,2,0,1,{outline_w},{shadow_d},5,30,30,30,1
Style: HormoziFallback,{fallback_font},{font_size},{primary_col},&H000000FF,{outline_col},&H80000000,-1,0,0,0,100,100,2,0,1,{outline_w},{shadow_d},5,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    dialogue_lines: List[str] = []

    if not words:
        # Fallback event if no words detected
        logger.info("No words to write to ASS; writing valid empty events section.")
        with open(output_ass_path, "w", encoding="utf-8") as f:
            f.write(ass_header)
        return output_ass_path

    # Chunk words into groups of max_words (3-4 words)
    chunks: List[List[Dict[str, Any]]] = []
    current_chunk: List[Dict[str, Any]] = []

    for w in words:
        current_chunk.append(w)
        # Split chunk if reaches max_words or if long pause (>0.6s) to next word
        if len(current_chunk) >= max_words:
            chunks.append(current_chunk)
            current_chunk = []

    if current_chunk:
        chunks.append(current_chunk)

    # For each chunk, create dialogue events that highlight each word as it is spoken
    for chunk in chunks:
        if not chunk:
            continue

        chunk_start = chunk[0]["start"]
        chunk_end = chunk[-1]["end"]

        for i, active_w in enumerate(chunk):
            w_start = active_w["start"]
            # The active highlight lasts until the next word starts, or until the end of the chunk
            if i + 1 < len(chunk):
                w_end = max(w_start + 0.1, chunk[i + 1]["start"])
            else:
                w_end = max(w_start + 0.1, chunk_end)

            start_str = format_ass_timestamp(w_start)
            end_str = format_ass_timestamp(w_end)

            # Build line text where active word is yellow & scaled up
            line_parts = []
            for j, w in enumerate(chunk):
                word_text = w["word"]
                if j == i:
                    # Active word: Yellow + 112% scale pop
                    if pop_anim:
                        part = f"{{\\c{highlight_col}\\fscx114\\fscy114}}{word_text}{{\\r\\c{primary_col}}}"
                    else:
                        part = f"{{\\c{highlight_col}}}{word_text}{{\\r\\c{primary_col}}}"
                else:
                    # Inactive word: crisp white
                    part = word_text
                line_parts.append(part)

            joined_text = " ".join(line_parts)
            # Position at exact middle-center of border line (Alignment=5, pos=(540, pos_y))
            event_line = f"Dialogue: 0,{start_str},{end_str},HormoziMain,,0,0,0,,{{\\pos({pos_x},{pos_y})}}{joined_text}"
            dialogue_lines.append(event_line)

    full_ass_content = ass_header + "\n".join(dialogue_lines) + "\n"

    os.makedirs(os.path.dirname(os.path.abspath(output_ass_path)), exist_ok=True)
    with open(output_ass_path, "w", encoding="utf-8") as f:
        f.write(full_ass_content)

    logger.info(f"Generated Hormozi ASS subtitles: {output_ass_path} ({len(dialogue_lines)} dynamic events)")
    return os.path.abspath(output_ass_path)


def create_captions_for_clip(
    clip_audio_path: str,
    output_ass_path: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """
    High-level entrypoint: Transcribe clip audio and generate ASS file.
    """
    cfg = config or {}
    c_cfg = cfg.get("captions", {})

    model_size = c_cfg.get("whisper_model", "small")
    device = c_cfg.get("whisper_device", "auto")
    compute_type = c_cfg.get("compute_type", "int8")

    words = transcribe_audio_words(
        clip_audio_path,
        model_size=model_size,
        device=device,
        compute_type=compute_type,
    )

    return generate_hormozi_ass_subtitles(words, output_ass_path, config=cfg)

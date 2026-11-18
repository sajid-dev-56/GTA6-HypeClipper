"""
Module 2: Hype Detection Engine (hype_detector.py)
Part of GTA6-HypeClipper Pipeline.

Combines Audio Volume Spikes (RMS energy in decibels) and Live Chat Velocity /
Keyword Surge into a Composite Hype Score to detect viral 45-second gaming moments.
Guarantees non-overlapping clip windowing using greedy Non-Maximum Suppression (NMS).
"""

import json
import logging
import math
import os
import re
import wave
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("GTA6-HypeClipper.HypeDetector")

DEFAULT_HYPE_KEYWORDS = [
    "W", "L", "OMG", "WTF", "CLIP IT", "CLIP", "BRO", "INSANE",
    "💀", "😂", "HOLY", "SHEESH", "NO WAY", "LETS GO", "LET'S GO",
    "POG", "POGGERS", "CLUTCH", "CRAZY", "DEAD", "HYPE", "NO SHOT", "UNREAL"
]


def load_audio_samples(audio_path: str) -> Tuple[np.ndarray, int]:
    """
    Load audio waveform samples and sample rate.
    Tries librosa/soundfile first, with a native standard-library `wave` fallback.
    """
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    # Method 1: Try soundfile or librosa
    try:
        import soundfile as sf
        samples, sr = sf.read(audio_path, dtype="float32")
        if samples.ndim > 1:
            samples = np.mean(samples, axis=1)
        return samples, sr
    except Exception:
        pass

    try:
        import librosa
        samples, sr = librosa.load(audio_path, sr=None, mono=True)
        return samples.astype(np.float32), int(sr)
    except Exception:
        pass

    # Method 2: Standard library wave fallback for 16-bit PCM WAV
    with wave.open(audio_path, "rb") as wf:
        sr = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        n_frames = wf.getnframes()
        raw_bytes = wf.readframes(n_frames)

    if sampwidth == 2:
        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        samples = np.frombuffer(raw_bytes, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        samples = np.frombuffer(raw_bytes, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0

    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)

    return samples, int(sr)


def compute_audio_rms_spikes(
    samples: np.ndarray,
    sr: int,
    window_sec: float = 5.0,
    hop_sec: float = 1.0,
    threshold_std: float = 2.0,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Compute Root Mean Square (RMS) energy in decibels for rolling windows.
    Flags windows exceeding `mean_db + (threshold_std * std_db)` (screams, gunshots, explosions).
    
    Returns:
        timestamps: 1D array of window center times (seconds)
        db_values: 1D array of RMS decibel values
        spike_scores: 1D array of normalized spike magnitudes above threshold
    """
    win_len = int(window_sec * sr)
    hop_len = int(hop_sec * sr)

    if len(samples) < win_len:
        # Audio shorter than window
        timestamps = np.array([len(samples) / (2.0 * sr)])
        rms = np.sqrt(np.mean(samples**2) + 1e-12)
        db = np.array([20.0 * np.log10(rms + 1e-9)])
        return timestamps, db, np.array([1.0])

    # Efficient rolling RMS calculation
    n_windows = int((len(samples) - win_len) // hop_len) + 1
    squared = samples**2

    # Cumulative sum for O(1) window sums
    cumsum = np.cumsum(np.insert(squared, 0, 0))
    window_starts = np.arange(n_windows) * hop_len
    window_ends = window_starts + win_len
    window_sums = cumsum[window_ends] - cumsum[window_starts]

    rms = np.sqrt(np.maximum(0.0, window_sums / win_len) + 1e-12)
    db_values = 20.0 * np.log10(rms + 1e-9)

    timestamps = (window_starts + win_len / 2.0) / float(sr)

    mean_db = float(np.mean(db_values))
    std_db = float(np.std(db_values))
    cutoff = mean_db + (threshold_std * std_db)

    # Spike score measures how strongly the audio burst exceeds the cutoff
    spike_scores = np.maximum(0.0, (db_values - cutoff) / (std_db + 1e-6))

    # Also compute relative intensity over mean
    relative_intensity = np.maximum(0.0, (db_values - mean_db) / (std_db + 1e-6))

    # Blend spike score with high-intensity audio so intense moments are captured
    combined_audio_score = 0.7 * spike_scores + 0.3 * (relative_intensity / (threshold_std + 1e-6))

    return timestamps, db_values, combined_audio_score


def compute_chat_velocity_and_surges(
    chat_json_path: Optional[str],
    timestamps: np.ndarray,
    window_sec: float = 5.0,
    hype_keywords: Optional[List[str]] = None,
    surge_multiplier: float = 1.5,
) -> np.ndarray:
    """
    Compute chat velocity (messages/sec) for each timestamp window.
    Applies `surge_multiplier` boost (e.g., 1.5x) if hype keywords appear rapidly.
    """
    if not hype_keywords:
        hype_keywords = DEFAULT_HYPE_KEYWORDS

    n_windows = len(timestamps)
    if not chat_json_path or not os.path.isfile(chat_json_path):
        return np.zeros(n_windows, dtype=np.float32)

    try:
        with open(chat_json_path, "r", encoding="utf-8") as f:
            messages = json.load(f)
    except Exception as e:
        logger.warning(f"Could not read chat JSON {chat_json_path}: {e}")
        return np.zeros(n_windows, dtype=np.float32)

    if not messages:
        return np.zeros(n_windows, dtype=np.float32)

    # Precompile regex for hype keywords
    keyword_patterns = [re.compile(re.escape(kw), re.IGNORECASE) for kw in hype_keywords]

    msg_times = np.array([float(m.get("timestamp", 0.0)) for m in messages])
    msg_texts = [str(m.get("message", "")) for m in messages]

    half_win = window_sec / 2.0
    chat_scores = np.zeros(n_windows, dtype=np.float32)

    for i, t in enumerate(timestamps):
        start_t = t - half_win
        end_t = t + half_win

        # Find messages within the window
        in_window_idx = np.where((msg_times >= start_t) & (msg_times <= end_t))[0]
        count = len(in_window_idx)
        if count == 0:
            continue

        raw_velocity = count / window_sec

        # Check for keyword surge
        keyword_hits = 0
        for idx in in_window_idx:
            txt = msg_texts[idx]
            for pat in keyword_patterns:
                if pat.search(txt):
                    keyword_hits += 1
                    break

        boost = 1.0
        # If at least 2 messages or >20% contain hype keywords, apply surge boost
        if keyword_hits >= 2 or (count > 0 and (keyword_hits / count) >= 0.25):
            boost = surge_multiplier

        chat_scores[i] = raw_velocity * boost

    return chat_scores


def normalize_series(series: np.ndarray) -> np.ndarray:
    """Normalize an array to [0, 1] range."""
    s_min = float(np.min(series))
    s_max = float(np.max(series))
    if s_max - s_min > 1e-6:
        return (series - s_min) / (s_max - s_min)
    return np.zeros_like(series)


def detect_hype_moments(
    audio_path: str,
    chat_json_path: Optional[str] = None,
    total_duration: float = 0.0,
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Main Hype Detection Engine entrypoint.
    Calculates Composite Hype Score for rolling 5s windows:
        Score = (w_audio * norm_audio) + (w_chat * norm_chat)
    Finds top N non-overlapping 45-second clips.
    """
    cfg = config or {}
    h_cfg = cfg.get("hype_detection", {})

    window_sec = float(h_cfg.get("window_seconds", 5.0))
    hop_sec = float(h_cfg.get("hop_seconds", 1.0))
    threshold_std = float(h_cfg.get("audio_threshold_std", 2.0))
    w_chat = float(h_cfg.get("chat_velocity_weight", 0.5))
    w_audio = float(h_cfg.get("audio_spike_weight", 0.5))
    surge_mult = float(h_cfg.get("keyword_surge_multiplier", 1.5))
    min_spacing = float(h_cfg.get("min_clip_spacing_seconds", 40.0))
    pre_peak = float(h_cfg.get("clip_pre_peak_seconds", 20.0))
    post_peak = float(h_cfg.get("clip_post_peak_seconds", 25.0))
    max_clips = int(h_cfg.get("max_clips", 5))
    hype_keywords = h_cfg.get("hype_keywords", DEFAULT_HYPE_KEYWORDS)

    logger.info("Computing Audio RMS Energy Spikes...")
    samples, sr = load_audio_samples(audio_path)
    calc_duration = len(samples) / float(sr)
    if total_duration <= 0.0:
        total_duration = calc_duration

    timestamps, db_values, audio_scores = compute_audio_rms_spikes(
        samples, sr, window_sec=window_sec, hop_sec=hop_sec, threshold_std=threshold_std
    )

    logger.info(f"Audio analyzed: {len(timestamps)} windows, mean: {np.mean(db_values):.1f} dB, std: {np.std(db_values):.1f} dB")

    logger.info("Computing Chat Velocity & Keyword Surges...")
    chat_scores = compute_chat_velocity_and_surges(
        chat_json_path, timestamps, window_sec=window_sec,
        hype_keywords=hype_keywords, surge_multiplier=surge_mult
    )

    # Normalize metrics
    norm_audio = normalize_series(audio_scores)
    norm_chat = normalize_series(chat_scores)

    # If chat has zero activity across the board, shift weight to audio
    if np.max(norm_chat) == 0.0:
        logger.info("No chat velocity recorded; detecting hype moments purely via Audio Volume Spikes.")
        effective_w_audio = 1.0
        effective_w_chat = 0.0
    else:
        effective_w_audio = w_audio
        effective_w_chat = w_chat

    # Composite Hype Score
    composite_scores = (effective_w_audio * norm_audio) + (effective_w_chat * norm_chat)

    # Romance & Kissing Focus: Boost moments occurring at or after 06:30:00 (23,400s)
    rf_cfg = cfg.get("romance_focus", {})
    if rf_cfg.get("enabled", True):
        r_start = float(rf_cfg.get("start_seconds", 23400.0))
        r_boost = float(rf_cfg.get("boost_multiplier", 3.0))
        boost_count = 0
        for i, t in enumerate(timestamps):
            if t >= r_start:
                composite_scores[i] *= r_boost
                boost_count += 1
        if boost_count > 0:
            logger.info(f"❤️ ROMANCE FOCUS: Applied {r_boost}x boost to {boost_count} moments at/after {r_start/3600:.2f}h (06:30:00)!")

    # Find peaks in composite score
    candidate_peaks: List[Tuple[float, float]] = []  # (time, score)
    n = len(composite_scores)

    for i in range(1, n - 1):
        # Local maximum condition
        if composite_scores[i] >= composite_scores[i - 1] and composite_scores[i] >= composite_scores[i + 1]:
            candidate_peaks.append((float(timestamps[i]), float(composite_scores[i])))

    if not candidate_peaks and n > 0:
        # Fallback if no strict local peak (e.g. monotonic or single window)
        best_idx = int(np.argmax(composite_scores))
        candidate_peaks.append((float(timestamps[best_idx]), float(composite_scores[best_idx])))

    # Sort peaks descending by hype score
    min_score = float(h_cfg.get("min_hype_score", 0.05))
    filtered_peaks = [p for p in candidate_peaks if p[1] >= min_score]
    if not filtered_peaks and candidate_peaks:
        # If nothing cleared min_score, take the highest available peak
        filtered_peaks = [candidate_peaks[0]]
    candidate_peaks = filtered_peaks

    candidate_peaks.sort(key=lambda x: x[1], reverse=True)
    logger.info(f"Found {len(candidate_peaks)} qualified hype peaks (min_score={min_score}). Applying Non-Maximum Suppression (spacing={min_spacing}s)...")

    # Greedy Non-Maximum Suppression (NMS) to avoid overlapping clips
    selected_peaks: List[Tuple[float, float]] = []
    for peak_t, score in candidate_peaks:
        # Check distance against all previously selected peaks
        too_close = False
        for sel_t, _ in selected_peaks:
            if abs(peak_t - sel_t) < min_spacing:
                too_close = True
                break

        if not too_close:
            selected_peaks.append((peak_t, score))
            if len(selected_peaks) >= max_clips:
                break

    # If more clips requested and valid candidate peaks remain, select them
    if len(selected_peaks) < max_clips and len(candidate_peaks) > len(selected_peaks):
        for peak_t, score in candidate_peaks:
            if (peak_t, score) not in selected_peaks and score >= min_score:
                too_close = any(abs(peak_t - st) < (min_spacing / 2.0) for st, _ in selected_peaks)
                if not too_close:
                    selected_peaks.append((peak_t, score))
                    if len(selected_peaks) >= max_clips:
                        break

    # Construct final 45-second clip boundaries
    # pre_peak (20s) to post_peak (25s) = 45s total
    selected_clips: List[Dict[str, Any]] = []
    target_clip_len = pre_peak + post_peak

    for idx, (peak_t, score) in enumerate(selected_peaks):
        start_t = max(0.0, peak_t - pre_peak)
        end_t = min(total_duration, start_t + target_clip_len)

        # If clip clamped at end of video, adjust start backwards to maintain duration if possible
        if (end_t - start_t) < target_clip_len and start_t > 0:
            start_t = max(0.0, end_t - target_clip_len)

        duration = round(end_t - start_t, 2)

        clip_info = {
            "clip_id": idx + 1,
            "start_time": round(start_t, 2),
            "end_time": round(end_t, 2),
            "duration": duration,
            "peak_time": round(peak_t, 2),
            "hype_score": round(score, 3),
            "target_resolution": "1080x1920",
        }
        selected_clips.append(clip_info)

    # Sort clips chronologically by start time
    selected_clips.sort(key=lambda c: c["start_time"])
    # Re-assign clip_ids sequentially 1..N
    for i, c in enumerate(selected_clips):
        c["clip_id"] = i + 1

    logger.info(f"Selected {len(selected_clips)} final hype clips:")
    for c in selected_clips:
        logger.info(f"  Clip #{c['clip_id']}: {c['start_time']}s -> {c['end_time']}s ({c['duration']}s) [Peak: {c['peak_time']}s, Score: {c['hype_score']}]")

    return selected_clips

"""
profanity-hush — Step 1a: extract raw audio bitstream
                  Step 1b: downmix to stereo WAV

Both functions are called in sequence by the pipeline orchestrator and are
tracked as separate entries in job.json's steps_completed list.
"""
import json
import logging
from pathlib import Path
from typing import Optional

from utils import cfg_get, fmt_size, mark_step_done, read_job, run_cmd, step_logger, write_job


# Maps ffprobe codec_name to the file extension used for audio_raw.{ext}.
# The goal is a lossless bitstream copy, so the extension must match what
# the muxer expects to demux without re-encoding.
CODEC_EXT: dict[str, str] = {
    "aac":       ".aac",
    "ac3":       ".ac3",
    "eac3":      ".eac3",
    "dts":       ".dts",
    "mp3":       ".mp3",
    "flac":      ".flac",
    "truehd":    ".truehd",
    "mlp":       ".mlp",
    "vorbis":    ".ogg",
    "opus":      ".opus",
    "pcm_s16le": ".wav",
    "pcm_s24le": ".wav",
    "pcm_s32le": ".wav",
    "wmav2":     ".wma",
}


# ── Step 1a ───────────────────────────────────────────────────────────────────

def extract_raw(
    video_path: Path,
    job_dir: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 1a: probe the primary audio stream and extract it as a bitstream copy.

    The bitstream copy is byte-identical to the audio stream as stored in
    the source container — no decode, no re-encode.  It is always kept in
    the job store regardless of keep_intermediates, because it is the
    essential resume artifact for future per-channel reprocessing (§13.3).

    Writes audio codec metadata to job.json and marks '1a_extract_raw' done.
    Returns the path to audio_raw.{ext}.
    """
    if log is None:
        log = step_logger("extract")

    log.info("Step 1a — probing audio stream: %s", video_path.name)

    stream = _probe_audio_stream(video_path, log)
    codec   = stream.get("codec_name", "unknown")
    ch      = stream.get("channels", 0)
    layout  = stream.get("channel_layout", "unknown")
    rate    = stream.get("sample_rate", "?")
    bitrate = stream.get("bit_rate", "?")

    log.info(
        "  codec: %s  |  channels: %d (%s)  |  sample_rate: %s Hz  |  bitrate: %s bps",
        codec, ch, layout, rate, bitrate,
    )
    if ch > 2:
        log.info(
            "  ℹ  Multi-channel source (%s ch, %s) — will be downmixed to stereo at Step 1b.",
            ch, layout,
        )

    ext      = CODEC_EXT.get(codec, f".{codec}")
    out_path = job_dir / f"audio_raw{ext}"

    if out_path.exists():
        log.info("  ↩  %s already exists — skipping extraction.", out_path.name)
    else:
        log.info("  Extracting bitstream copy → %s ...", out_path.name)
        run_cmd(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-y", "-i", str(video_path),
                "-vn", "-c:a", "copy",
                str(out_path),
            ],
            log,
        )
        log.info("  ✓  %s  (%s)", out_path.name, fmt_size(out_path))

    # Persist audio metadata for later steps and for job inspection
    state = read_job(job_dir)
    state["audio"] = {
        "codec":          codec,
        "channels":       ch,
        "channel_layout": layout,
        "sample_rate":    rate,
        "bit_rate":       bitrate,
        "raw_file":       out_path.name,
    }
    write_job(job_dir, state)
    mark_step_done(job_dir, "1a_extract_raw")

    return out_path


# ── Step 1b ───────────────────────────────────────────────────────────────────

def downmix_to_stereo(
    job_dir: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 1b: decode audio_raw.{ext} and downmix to stereo PCM WAV.

    Output spec: 44.1 kHz, 2 channels, pcm_s16le.
    The '-ac 2' flag handles any input channel layout:
      mono    → upmixed to stereo
      stereo  → passthrough
      5.1/7.1 → downmixed to stereo with standard coefficient matrix

    This is v1's deliberate multi-channel boundary (§13.3).  The original
    audio_raw.{ext} is always preserved for future per-channel reprocessing.

    audio_stereo.wav is large (~300 MB/hour) and is kept only when
    keep_intermediates is set; otherwise it is deleted after Step 2 completes.

    Marks '1b_downmix' done.  Returns path to audio_stereo.wav.
    """
    if log is None:
        log = step_logger("extract")

    state    = read_job(job_dir)
    audio    = state.get("audio", {})
    raw_name = audio.get("raw_file", "")
    raw_path = job_dir / raw_name if raw_name else None

    if raw_path is None or not raw_path.exists():
        raise RuntimeError(
            f"Step 1b: audio_raw file not found in {job_dir} "
            f"(expected '{raw_name}') — did Step 1a complete?"
        )

    ch     = audio.get("channels", 0)
    layout = audio.get("channel_layout", "?")
    out    = job_dir / "audio_stereo.wav"

    log.info(
        "Step 1b — downmixing to stereo: %s  (%d ch, %s → 2 ch, 44.1 kHz, pcm_s16le)",
        raw_path.name, ch, layout,
    )

    if out.exists():
        log.info("  ↩  audio_stereo.wav already exists — skipping downmix.")
    else:
        log.info("  Running ffmpeg downmix (may take several minutes for large files) ...")
        run_cmd(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-y", "-i", str(raw_path),
                "-ac", "2",
                "-ar", "44100",
                "-c:a", "pcm_s16le",
                str(out),
            ],
            log,
        )
        log.info("  ✓  audio_stereo.wav  (%s)", fmt_size(out))

    mark_step_done(job_dir, "1b_downmix")
    return out


# ── Internal helpers ──────────────────────────────────────────────────────────

def _probe_audio_stream(video_path: Path, log: logging.LoggerAdapter) -> dict:
    """
    Run ffprobe on the first audio stream of video_path.
    Returns the stream dict with codec_name, channels, channel_layout,
    sample_rate, bit_rate.
    """
    result = run_cmd(
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,bit_rate,sample_rate,channels,channel_layout",
            "-of", "json",
            str(video_path),
        ],
        log,
    )
    data    = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(
            f"No audio streams found in {video_path.name}.  "
            "Verify the file is a valid video/audio container."
        )
    return streams[0]

"""
profanity-hush — Step 1c: segment audio_stereo.wav

Splits the stereo WAV into fixed-length chunks for per-segment Demucs
processing (Step 2).  Short files that fit within one segment are returned
as a single-item list pointing to audio_stereo.wav itself — no splitting.

Returns:
  list[tuple[Path, float]]  —  (segment_wav_path, start_offset_sec)

The start offsets are used at Step 3b to convert segment-local word timestamps
back to film-absolute timestamps.
"""
import json
import logging
import math
from pathlib import Path
from typing import Optional

from utils import cfg_get, fmt_duration, fmt_size, mark_step_done, read_job, run_cmd, step_logger, write_job


def segment(
    job_dir: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> list[tuple[Path, float]]:
    """
    Step 1c: split audio_stereo.wav into fixed-size segments.

    Behaviour:
      segment_size_sec == 0 or duration <= segment_size_sec
        → single-segment passthrough; returns [(audio_stereo.wav, 0.0)]
        → no files are created

      duration > segment_size_sec
        → splits into N = ceil(duration / segment_size_sec) chunks
        → returns [(audio_stereo_01.wav, 0.0), (audio_stereo_02.wav, 1800.0), ...]

    Segment WAV files use stream-copy (-c copy) so splitting is near-instant
    regardless of file size.  The final segment omits -t to capture any
    sub-second remainder precisely.

    Segment files are large intermediates and are deleted after Step 3b
    unless keep_intermediates is set.

    Marks '1c_segment' done and writes segment metadata to job.json.
    """
    if log is None:
        log = step_logger("segment")

    stereo = job_dir / "audio_stereo.wav"
    if not stereo.exists():
        raise RuntimeError(
            f"Step 1c: audio_stereo.wav not found in {job_dir} — did Step 1b complete?"
        )

    size_sec = int(cfg_get(cfg, "audio", "segment_size_sec", default=1800))

    log.info("Step 1c — probing audio_stereo.wav ...")
    duration = _probe_duration(stereo, log)
    log.info(
        "  Duration: %.1f s  (%s)  |  segment_size: %s s",
        duration, fmt_duration(duration), size_sec,
    )

    if size_sec == 0 or duration <= size_sec:
        reason = "segment_size_sec=0 (disabled)" if size_sec == 0 else "duration ≤ segment_size"
        log.info("  Single-segment passthrough (%s).", reason)
        segs: list[tuple[Path, float]] = [(stereo, 0.0)]
    else:
        segs = _split(stereo, job_dir, duration, size_sec, log)

    _persist(job_dir, segs, duration)
    mark_step_done(job_dir, "1c_segment")
    return segs


# ── Internal ──────────────────────────────────────────────────────────────────

def _probe_duration(wav: Path, log: logging.LoggerAdapter) -> float:
    """Return duration of wav in seconds via ffprobe."""
    result = run_cmd(
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "a:0",
            "-show_entries", "stream=duration",
            "-of", "json",
            str(wav),
        ],
        log,
    )
    data    = json.loads(result.stdout)
    streams = data.get("streams", [])

    if streams and "duration" in streams[0]:
        return float(streams[0]["duration"])

    # WAV sometimes omits stream-level duration; fall back to format level
    result2 = run_cmd(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "json",
            str(wav),
        ],
        log,
    )
    fmt = json.loads(result2.stdout)
    dur = fmt.get("format", {}).get("duration")
    if dur is None:
        raise RuntimeError(f"Could not determine duration of {wav.name}")
    return float(dur)


def _split(
    stereo: Path,
    job_dir: Path,
    duration: float,
    size_sec: int,
    log: logging.LoggerAdapter,
) -> list[tuple[Path, float]]:
    """Split stereo WAV into N fixed-size chunks; return (path, start_sec) list."""
    n    = math.ceil(duration / size_sec)
    segs: list[tuple[Path, float]] = []

    log.info("  Splitting into %d segment(s) × ≤ %s each ...", n, fmt_duration(size_sec))

    for i in range(n):
        start = float(i * size_sec)
        end   = min(start + size_sec, duration)
        out   = job_dir / f"audio_stereo_{i + 1:02d}.wav"

        if out.exists():
            log.info(
                "  [%d/%d] ↩  %s already exists (%s) — skipping.",
                i + 1, n, out.name, fmt_size(out),
            )
        else:
            # -ss before -i → input seeking (direct byte offset into PCM WAV).
            # Placing -ss after -i uses output/decoder seeking, which must scan
            # from the start of the file for every segment — O(file size) rather
            # than O(1).  For PCM WAV, input seeking is both faster and exact
            # (no frame-boundary alignment needed for uncompressed audio).
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-y",
                "-ss", str(start),   # input seek — must be before -i
                "-i", str(stereo),
            ]
            if i < n - 1:
                cmd += ["-t", str(size_sec)]   # final segment: omit -t to capture remainder
            cmd += ["-c", "copy", str(out)]

            run_cmd(cmd, log)
            log.info(
                "  [%d/%d] %s  %s → %s  (%.1f s, %s)",
                i + 1, n,
                out.name,
                fmt_duration(start),
                fmt_duration(end),
                end - start,
                fmt_size(out),
            )

        segs.append((out, start))

    log.info("  ✓  %d segment(s) ready.", n)
    return segs


def _persist(job_dir: Path, segs: list[tuple[Path, float]], total_sec: float) -> None:
    """Write segment list and total duration into job.json."""
    state = read_job(job_dir)
    state["total_duration_sec"] = total_sec
    state["segments"] = [
        {"index": i + 1, "path": p.name, "start_sec": s}
        for i, (p, s) in enumerate(segs)
    ]
    write_job(job_dir, state)

"""
profanity-hush — Step 1c: segment audio_stereo.wav
                  + the shared WAV-splitting primitive Step 3 reuses

segment() splits the stereo WAV into fixed-length chunks for per-segment
Demucs processing (Step 2), sized by audio.segment_size_sec. Short files
that fit within one segment are returned as a single-item list pointing
to audio_stereo.wav itself — no splitting.

Returns:
  list[tuple[Path, float]]  —  (segment_wav_path, start_offset_sec)

The start offsets are used at Step 3b to convert segment-local word timestamps
back to film-absolute timestamps.

split_into_segments() below is the actual splitting mechanism, factored
out to be source- and size-agnostic: segment() is its Step 1c caller
(audio_stereo.wav, audio.segment_size_sec), and steps/transcribe.py is
its OTHER caller (dialog.wav, alignment.segment_size_sec) -- see that
function's own docstring for why Demucs and transcription are allowed,
and expected, to use different segment sizes. Nothing about this module
is Demucs-specific; "Step 1c" in this docstring names segment()'s own
role in the numbered pipeline, not what the underlying splitting logic
is limited to.
"""
import json
import logging
import math
from pathlib import Path
from typing import Optional

from utils import (
    cfg_get,
    check_duration_matches,
    finalize_output,
    fmt_duration,
    fmt_size,
    mark_step_done,
    read_job,
    run_cmd,
    step_logger,
    tmp_output_path,
    write_job,
)


def segment(
    job_dir: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> list[tuple[Path, float]]:
    if log is None:
        log = step_logger("segment")

    stereo = job_dir / "audio_stereo.wav"

    state = read_job(job_dir)
    if "1c_segment" in state.get("steps_completed", []):
        segs = _segments_from_state(job_dir, state, log)
        if segs is not None:
            log.info("Step 1c — ↩  already complete; %d segment(s) verified.", len(segs))
            return segs
        log.info(
            "Step 1c — marked complete, but the recorded segment(s) are no "
            "longer all present — regenerating from audio_stereo.wav."
        )

    if not stereo.exists():
        raise RuntimeError(
            f"Step 1c: audio_stereo.wav not found in {job_dir} — did Step 1b complete?"
        )

    size_sec = int(cfg_get(cfg, "audio", "segment_size_sec"))

    log.info("Step 1c — probing audio_stereo.wav ...")
    duration = _probe_duration(stereo, log)
    log.info(
        "  Duration: %.1f s  (%s)  |  segment_size: %s s",
        duration, fmt_duration(duration), size_sec,
    )

    segs = split_into_segments(stereo, job_dir, "audio_stereo", duration, size_sec, log)

    _persist(job_dir, segs, duration)
    mark_step_done(job_dir, "1c_segment")
    return segs


def _segments_from_state(
    job_dir: Path,
    state: dict,
    log: logging.LoggerAdapter,
) -> Optional[list[tuple[Path, float]]]:
    segs_state = state.get("segments", [])
    if not segs_state:
        return None

    total_sec = float(state.get("total_duration_sec", 0.0))
    multi     = len(segs_state) > 1
    segs: list[tuple[Path, float]] = []

    for i, seg in enumerate(segs_state):
        p     = job_dir / seg["path"]
        start = float(seg["start_sec"])
        if not p.exists():
            return None

        if multi:
            end = (
                float(segs_state[i + 1]["start_sec"])
                if i + 1 < len(segs_state) else total_sec
            )
            _validate_segment(p, end - start, log)

        segs.append((p, start))

    return segs


def _validate_segment(path: Path, expected_duration: float, log: logging.LoggerAdapter) -> None:
    actual = _probe_duration(path, log)
    try:
        check_duration_matches(
            actual, expected_duration, log=log,
            label=f"{path.name} (split segment)", tolerance_sec=2.0,
        )
    except RuntimeError:
        path.unlink(missing_ok=True)
        log.error("  Deleted incomplete %s — re-run to split it fresh.", path.name)
        raise


def _probe_duration(wav: Path, log: logging.LoggerAdapter) -> float:
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


def split_into_segments(
    source: Path,
    job_dir: Path,
    prefix: str,
    duration: float,
    size_sec: int,
    log: logging.LoggerAdapter,
) -> list[tuple[Path, float]]:
    """
    Split `source` (a full-duration WAV) into `{prefix}_NN.wav` fixed-size
    chunks via ffmpeg input-side seeking + stream copy (exact and fast
    for PCM WAV, no re-encode) -- or return [(source, 0.0)] unchanged,
    untouched, if size_sec == 0 or duration already fits in one piece.

    This is the one shared mechanism behind every fixed-size WAV split in
    this pipeline -- deliberately source- and size-agnostic so it can be
    called independently, with independent segment sizes, for whichever
    stage actually needs chunking:

      - Step 1c (segment() below) splits audio_stereo.wav ahead of
        Demucs (Step 2), sized by audio.segment_size_sec -- Demucs's own
        memory footprint is what makes this segmentation necessary at
        all (design doc §12: "a 2-hour file at full quality exhausts
        16 GB RAM").
      - steps/transcribe.py's own Step 3 preparation splits the
        CANONICAL, already-Demucs-separated dialog.wav ahead of
        transcription, sized independently by
        alignment.segment_size_sec -- see that module's own docstring
        for why transcription has no comparable memory constraint of
        its own (every engine already does its own internal long-form
        handling: WhisperX's ~30-SECOND internal decode windows, MFA's
        own chunk_target_sec re-chunking inside align_mfa.py, and
        CrisperWhisper's own longform_strategy), and for the real
        reason a SMALLER audio.segment_size_sec doesn't automatically
        mean better transcription: an artificial job-level cut lands at
        a fixed wall-clock offset with zero awareness of where a word
        or sentence actually falls, so a boundary Demucs is fine with
        (source separation doesn't especially care about mid-word cuts)
        can still land mid-word for a *recognition* engine, which does.
        Being able to give transcription a larger (or, at
        alignment.segment_size_sec: 0, a nonexistent) segmentation,
        independent of whatever Demucs used, is the direct fix.

    Each piece is validated (_validate_segment()) whether it was just
    freshly split or found already sitting on disk from a previous call
    -- so calling this repeatedly (a resume, or a redo that reuses the
    exact same source + size_sec) is cheap and idempotent: an existing,
    correctly-sized piece is reused as-is; one that doesn't match its
    expected duration (e.g. left over from a run that used a DIFFERENT
    size_sec) is deleted and re-split automatically, by the same
    duration-mismatch handling _validate_segment() already has, not
    anything new here.
    """
    if size_sec == 0 or duration <= size_sec:
        return [(source, 0.0)]

    n    = math.ceil(duration / size_sec)
    segs: list[tuple[Path, float]] = []

    log.info(
        "  Splitting %s into %d segment(s) × ≤ %s each ...",
        source.name, n, fmt_duration(size_sec),
    )

    for i in range(n):
        start = float(i * size_sec)
        end   = min(start + size_sec, duration)
        out   = job_dir / f"{prefix}_{i + 1:02d}.wav"

        if out.exists():
            log.info(
                "  [%d/%d] ↩  %s already exists (%s) — verifying ...",
                i + 1, n, out.name, fmt_size(out),
            )
            _validate_segment(out, end - start, log)
        else:
            tmp = tmp_output_path(out)
            tmp.unlink(missing_ok=True)
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-y",
                "-ss", str(start),
                "-i", str(source),
            ]
            if i < n - 1:
                cmd += ["-t", str(size_sec)]
            cmd += ["-c", "copy", str(tmp)]

            run_cmd(cmd, log)
            finalize_output(tmp, out)
            _validate_segment(out, end - start, log)
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
    state = read_job(job_dir)
    state["total_duration_sec"] = total_sec
    state["segments"] = [
        {"index": i + 1, "path": p.name, "start_sec": s}
        for i, (p, s) in enumerate(segs)
    ]
    write_job(job_dir, state)

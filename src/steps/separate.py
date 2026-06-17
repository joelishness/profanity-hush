"""
profanity-hush — Step 2: Demucs source separation

Runs Demucs on each segment WAV, producing two stems per segment:
  dialog_NN.wav    — vocals / dialog
  score_sfx_NN.wav — music + sound effects (everything else)

Single-segment passthrough files use no numeric suffix:
  dialog.wav / score_sfx.wav

Demucs is run once per segment with --two-stems=vocals, which is
substantially faster than separating all four stems (htdemucs default).

Resume support: if both output stems for a segment already exist the
segment is skipped.  A partially-completed demucs run (crash mid-segment)
leaves a .demucs_work/ subdirectory in the job dir; the next run cleans
that up and starts the segment fresh — Demucs has no internal resume.

Returns:
  list[tuple[Path, Path]]  —  (dialog_path, score_sfx_path) per segment,
                               in the same order as the input segments list.
"""
import shutil
import sys
import time
from pathlib import Path
from typing import Optional
import logging

from utils import (
    cfg_get,
    fmt_duration,
    fmt_size,
    mark_step_done,
    read_job,
    run_cmd,
    step_logger,
    write_job,
)

# Work directory created inside the job dir during separation.
# Using the job dir (bind-mounted to host) avoids /tmp size limits.
_WORK_DIR_NAME = ".demucs_work"


def separate(
    job_dir: Path,
    segments: list[tuple[Path, float]],
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> list[tuple[Path, Path]]:
    """
    Step 2: run Demucs on every segment; return (dialog, score_sfx) pairs.

    Skips segments whose output pair already exists.
    Marks '2_separate' done and writes separation metadata to job.json
    once all segments complete.
    """
    if log is None:
        log = step_logger("separate")

    # Already done?  Re-derive outputs from job.json and return immediately.
    state = read_job(job_dir)
    if "2_separate" in state.get("steps_completed", []):
        log.info("Step 2 — ↩  already complete, loading stems from job.json.")
        return _stems_from_state(job_dir, state)

    model  = cfg_get(cfg, "demucs", "model",  default="htdemucs_ft")
    shifts = int(cfg_get(cfg, "demucs", "shifts", default=1))
    device = cfg_get(cfg, "demucs", "device", default="cpu")
    n      = len(segments)

    log.info("Step 2 — Demucs source separation")
    log.info("  model=%s  shifts=%d  device=%s  segments=%d", model, shifts, device, n)

    # Segment durations from job.json (avoids a second ffprobe pass)
    durations = _segment_durations(state)

    results: list[dict] = []
    pairs:   list[tuple[Path, Path]] = []

    for i, (seg_path, _start_sec) in enumerate(segments):
        suffix    = seg_path.stem.removeprefix("audio_stereo")  # "" | "_01" | …
        dialog    = job_dir / f"dialog{suffix}.wav"
        score_sfx = job_dir / f"score_sfx{suffix}.wav"

        if dialog.exists() and score_sfx.exists():
            log.info(
                "  [%d/%d] ↩  dialog%s + score_sfx%s already exist (%s + %s) — skipping.",
                i + 1, n,
                suffix, suffix,
                fmt_size(dialog), fmt_size(score_sfx),
            )
            results.append({
                "index":     i + 1,
                "dialog":    dialog.name,
                "score_sfx": score_sfx.name,
                "skipped":   True,
            })
            pairs.append((dialog, score_sfx))
            continue

        dur_sec = durations.get(seg_path.name, 0.0)
        est_sec = dur_sec * 4 * shifts     # ~4× realtime per shift for htdemucs
        log.info(
            "  [%d/%d] %s  (%.0f s — est. ~%s wall-clock) ...",
            i + 1, n,
            seg_path.name,
            dur_sec,
            fmt_duration(est_sec),
        )

        # Clean up any leftover work dir from a prior crashed run
        work_dir = job_dir / _WORK_DIR_NAME
        if work_dir.exists():
            log.debug("  Removing stale %s from prior run.", _WORK_DIR_NAME)
            shutil.rmtree(work_dir)
        work_dir.mkdir()

        t0 = time.monotonic()
        try:
            run_cmd(
                [
                    sys.executable, "-m", "demucs",
                    "--two-stems=vocals",
                    "-n", model,
                    "--shifts", str(shifts),
                    "-d", device,
                    "-o", str(work_dir),
                    str(seg_path),
                ],
                log,
                heartbeat_sec=120,   # demucs runs minutes-to-hours per segment;
                                     # surface liveness at INFO without
                                     # promoting its \r-based progress bars
            )
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

        elapsed = time.monotonic() - t0

        # Demucs output layout:
        #   {work_dir}/{model}/vocals/{input_filename}
        #   {work_dir}/{model}/no_vocals/{input_filename}
        vocals_src    = work_dir / model / "vocals"    / seg_path.name
        no_vocals_src = work_dir / model / "no_vocals" / seg_path.name

        if not vocals_src.exists() or not no_vocals_src.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
            raise RuntimeError(
                f"Demucs finished but expected outputs were not found.\n"
                f"  vocals:    {vocals_src}\n"
                f"  no_vocals: {no_vocals_src}\n"
                f"Check AC_LOG_LEVEL=debug output for Demucs stderr."
            )

        shutil.move(str(vocals_src),    str(dialog))
        shutil.move(str(no_vocals_src), str(score_sfx))
        shutil.rmtree(work_dir)

        log.info(
            "  [%d/%d] ✓  dialog%s (%s)  score_sfx%s (%s)  %.0f s elapsed",
            i + 1, n,
            suffix, fmt_size(dialog),
            suffix, fmt_size(score_sfx),
            elapsed,
        )

        results.append({
            "index":       i + 1,
            "dialog":      dialog.name,
            "score_sfx":   score_sfx.name,
            "elapsed_sec": round(elapsed, 1),
        })
        pairs.append((dialog, score_sfx))

    # Persist and mark done
    state = read_job(job_dir)
    state["separation"] = {"model": model, "shifts": shifts, "segments": results}
    write_job(job_dir, state)
    mark_step_done(job_dir, "2_separate")

    log.info("  ✓  All segments separated.")
    return pairs


# ── Helpers ───────────────────────────────────────────────────────────────────

def _segment_durations(state: dict) -> dict[str, float]:
    """
    Build a map of segment filename → duration in seconds.

    Derived from the job.json segment list and total_duration_sec rather than
    a second ffprobe call — the information is already on disk.
    """
    segs      = state.get("segments", [])
    total_sec = float(state.get("total_duration_sec", 0.0))
    durations: dict[str, float] = {}

    for j, seg in enumerate(segs):
        if j + 1 < len(segs):
            dur = segs[j + 1]["start_sec"] - seg["start_sec"]
        else:
            dur = total_sec - seg["start_sec"]
        durations[seg["path"]] = dur

    return durations


def _stems_from_state(
    job_dir: Path, state: dict
) -> list[tuple[Path, Path]]:
    """
    Re-derive (dialog, score_sfx) pairs from the separation block in
    job.json, for use when Step 2 is already marked complete on a resume.
    """
    pairs: list[tuple[Path, Path]] = []
    for seg in state.get("separation", {}).get("segments", []):
        dialog    = job_dir / seg["dialog"]
        score_sfx = job_dir / seg["score_sfx"]
        if not dialog.exists() or not score_sfx.exists():
            raise RuntimeError(
                f"Step 2 is marked complete but stem files are missing:\n"
                f"  {dialog}\n  {score_sfx}\n"
                f"Delete the job directory and re-run to start fresh."
            )
        pairs.append((dialog, score_sfx))
    return pairs

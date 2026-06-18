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
import logging
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

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

_BAG_RE  = re.compile(r"a bag of (\d+) models")
_TQDM_RE = re.compile(r"^\s*(\d{1,3})%\|")


class _DemucsProgress:
    """
    Tracks per-segment Demucs progress by parsing its stdout/stderr lines
    in real time (fed via run_cmd's on_line hook).

    Demucs prints "Selected model is a bag of N models..." once per
    invocation; the N models then run sequentially, each producing its own
    tqdm bar from 0% to 100%.  We count completed bars to know which model
    is active, and use the latest bar's percentage to interpolate progress
    within that model.

    tqdm prints a bar's 100% line TWICE (once on completion, once again on
    close) before the next bar's first 0% line appears — confirmation that
    a model has truly finished is therefore the *next* bar's 0%, not the
    100% line itself.  At the very last model, no further 0% ever arrives
    (the process just exits after writing output); fraction() handles this
    by treating "last seen pct == 100" as completion of the current slot
    even without that confirmation, so progress correctly reaches 1.0
    instead of capping just under it.  Verified against both a synthetic
    4-model sequence and the verbatim debug log from job b9b7fddfd972.

    If demucs ever doesn't print "bag of N" (e.g. a single-model selection)
    or prints bars in a format this regex doesn't recognize, this degrades
    gracefully: n_models stays at 1 and fraction() stays at 0 until/unless
    something matches — the heartbeat still fires with elapsed time, just
    without fine-grained percentages.
    """
    def __init__(self) -> None:
        self.n_models     = 1
        self.model_index  = 0      # 0-based count of FULLY CONFIRMED models
        self.last_pct     = 0
        self._saw_100     = False

    def feed(self, stream: str, line: str) -> None:
        if stream == "out":
            m = _BAG_RE.search(line)
            if m:
                self.n_models = int(m.group(1))
            return

        m = _TQDM_RE.match(line)
        if not m:
            return
        pct = int(m.group(1))

        if pct == 0 and self._saw_100:
            self.model_index = min(self.model_index + 1, self.n_models - 1)
            self._saw_100    = False
            self.last_pct    = 0
        else:
            self.last_pct = pct
            if pct == 100:
                self._saw_100 = True

    def fraction(self) -> float:
        """Fraction (0.0-1.0) complete for the segment currently running."""
        return min(1.0, (self.model_index + self.last_pct / 100.0) / self.n_models)

    def model_label(self) -> str:
        """e.g. '2/4' — which model is currently active, out of how many."""
        shown = self.model_index + 1
        if self.last_pct == 100:
            shown = min(shown, self.n_models)
        return f"{shown}/{self.n_models}"


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
    total_sec = float(state.get("total_duration_sec", 0.0)) or sum(durations.values())

    # Running totals across THIS invocation only.  Skipped (pre-existing)
    # segments add their duration to duration_done_sec instantly but no
    # wall-clock time, so they don't skew the live processing-rate estimate
    # used for ETA below — only segments actually run through demucs in
    # this call contribute to wall_done_sec.
    duration_done_sec = 0.0
    wall_done_sec      = 0.0

    results: list[dict] = []
    pairs:   list[tuple[Path, Path]] = []

    for i, (seg_path, _start_sec) in enumerate(segments):
        suffix    = seg_path.stem.removeprefix("audio_stereo")  # "" | "_01" | …
        dialog    = job_dir / f"dialog{suffix}.wav"
        score_sfx = job_dir / f"score_sfx{suffix}.wav"
        dur_sec   = durations.get(seg_path.name, 0.0)

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
            duration_done_sec += dur_sec
            continue

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

        progress = _DemucsProgress()

        def _on_line(stream: str, line: str, _p=progress) -> None:
            _p.feed(stream, line)

        # Default-argument capture of the current iteration's values — not
        # strictly required since run_cmd blocks until the subprocess exits
        # (so no future-iteration values could leak in), but kept explicit
        # so this stays correct if run_cmd is ever made concurrent.
        def _heartbeat_msg(
            elapsed: float,
            _p=progress, _dur=dur_sec,
            _done_before=duration_done_sec, _wall_before=wall_done_sec,
            _i=i, _n=n,
        ) -> str:
            seg_frac   = _p.fraction()
            audio_done = _done_before + _dur * seg_frac
            wall_spent = _wall_before + elapsed
            rate       = audio_done / wall_spent if wall_spent > 0 else 0.0
            remaining  = max(0.0, total_sec - audio_done)
            eta_sec    = remaining / rate if rate > 0 else None
            overall_pct = (audio_done / total_sec * 100) if total_sec > 0 else 0.0
            eta_str = fmt_duration(eta_sec) if eta_sec is not None else "calculating…"

            return (
                f"model {_p.model_label()}  |  "
                f"segment {seg_frac * 100:5.1f}%  (seg {_i + 1}/{_n})  |  "
                f"overall {overall_pct:5.1f}%  |  "
                f"elapsed {fmt_duration(elapsed)}  |  "
                f"ETA {eta_str}"
            )

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
                heartbeat_msg=_heartbeat_msg,
                on_line=_on_line,
            )
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

        elapsed = time.monotonic() - t0
        duration_done_sec += dur_sec
        wall_done_sec      += elapsed

        # Demucs output layout (confirmed from an actual torchaudio write-path
        # error during testing — see job b9b7fddfd972 / Step 2 debug log):
        #   {work_dir}/{model}/{track_name}/vocals.wav
        #   {work_dir}/{model}/{track_name}/no_vocals.wav
        # track_name is the input filename WITHOUT extension (e.g.
        # "audio_stereo" or "audio_stereo_01"); the stem files are always
        # literally named vocals.wav / no_vocals.wav regardless of input
        # filename — demucs does not preserve the original name on the file,
        # only on the enclosing directory.
        track_name    = seg_path.stem
        vocals_src    = work_dir / model / track_name / "vocals.wav"
        no_vocals_src = work_dir / model / track_name / "no_vocals.wav"

        if not vocals_src.exists() or not no_vocals_src.exists():
            # Demucs exited 0 but our path assumption was wrong (this has
            # happened once already across a version difference) — dump what
            # actually exists under work_dir so the real layout is visible in
            # the error rather than requiring another debug-log round trip.
            found = sorted(p.relative_to(work_dir) for p in work_dir.rglob("*") if p.is_file())
            found_str = "\n".join(f"    {p}" for p in found) or "    (no files found)"
            shutil.rmtree(work_dir, ignore_errors=True)
            raise RuntimeError(
                f"Demucs finished but expected outputs were not found.\n"
                f"  expected vocals:    {vocals_src}\n"
                f"  expected no_vocals: {no_vocals_src}\n"
                f"  actual contents of {work_dir}:\n"
                f"{found_str}"
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

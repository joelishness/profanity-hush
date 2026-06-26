"""
profanity-hush — Step 5: mute dialog stem

Reads the word-list matches already flagged by Step 4b's flag phase
(matches.json) and, if present, the human review overrides (review.json),
then builds an ffmpeg `volume` filter that silences every approved
interval in the dialog stem.

This step deliberately does NOT call steps/matching.py or re-scan the
transcript. Step 4b's flag phase (steps/review.py:flag()) is the pipeline's
one and only find_matches() call (see design doc §4) — Step 5 only ever
consumes its persisted output. That means a word a human approved (or
rejected) during Step 4b's review phase is, by construction, exactly what
Step 5 acts on; there is no second scan that could ever disagree with the
first about what counts as a match.

Input  : matches.json (Step 4b flag phase, always present), review.json
         (Step 4b review phase, present only for interactive runs that had
         at least one candidate), dialog.wav
Output : dialog_censored.wav, censor_log.json

Logic:
  1. Load matches.json (this is the *only* source of candidate matches —
     never recomputed).
  2. If review.json exists: drop every match whose word_index has a "skip"
     override; add one interval per "add" override (these never came from
     matches.json at all -- they're the reviewer's manual corrections,
     already fully formed with their own start/end). Without review.json
     (unattended mode, or an interactive run with zero candidates), every
     match from step 1 is muted as-is.
  3. Pad every remaining interval by censoring.padding_ms on each side.
  4. Merge overlapping/touching padded intervals.
  5. method: mute (v1's only implemented method) -- build an ffmpeg
     `volume` filter:
       volume=enable='between(t,s1,e1)+between(t,s2,e2)+...':volume=0
     method: beep -- not yet implemented in v1 (Phase 4 polish item; see
     design doc §10) -- raises a clear, actionable error rather than
     silently falling back to mute or producing an output that's actually
     muted but labeled as beeped.
  6. Write censor_log.json with every individual muted word/addition (both
     raw and padded timestamps) -- a finer-grained record than the merged
     ffmpeg intervals, useful for review and for the future correction
     workflow (§13.4).

If zero intervals remain after overrides (no candidates were flagged, or
every candidate was rejected), dialog_censored.wav is a plain copy of
dialog.wav and a warning is logged.

Intermediate cleanup:
  dialog.wav (the uncensored stem) is fully consumed once
  dialog_censored.wav exists -- nothing downstream *within this run*
  needs it again. But unlike steps/merge.py's per-segment intermediates
  (which are genuinely useless once concatenated), dialog.wav is the one
  artifact that makes a future correction cheap: re-running Step 5 with
  an edited review.json (rejecting a false positive, adding a missed
  word) only needs dialog.wav -- never Step 2's ~hour-plus Demucs
  separation again. So dialog.wav is governed by its own setting,
  output.keep_correction_artifacts (default true), independent of the
  broader output.keep_intermediates (default false, governs the
  per-segment files and dialog_censored.wav/audio_censored.wav, none of
  which help a future correction since they're either fully superseded
  or trivially cheap to regenerate from dialog.wav). dialog.wav is
  deleted only if *both* settings are false. pipeline.py's --skip-index /
  --add-interval / --redo-review correction mode (see pipeline.py and
  steps/review.py's apply_corrections()) depends on dialog.wav still
  being on disk; if it was deleted, Step 5 fails with a clear error
  rather than silently regenerating it via a full re-separation. See
  design doc §6 and §13.4. pipeline.py's "Steps 1a-3b already complete"
  resume shortcut accounts for dialog.wav's absence not being an error
  once Step 5 has run, regardless of which setting caused the deletion.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

from utils import cfg_get, fmt_size, keep_intermediate, mark_step_done, read_job, run_cmd, step_logger, write_job


def mute(
    job_dir: Path,
    dialog_path: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 5: mute every approved interval in the dialog stem.

    Returns the path to dialog_censored.wav.
    """
    if log is None:
        log = step_logger("mute")

    state          = read_job(job_dir)
    done           = state.get("steps_completed", [])
    censored_out   = job_dir / "dialog_censored.wav"
    censor_log_out = job_dir / "censor_log.json"

    if "5_mute" in done:
        log.info("Step 5 — ↩  already complete; re-using %s.", censored_out.name)
        # censor_log.json is always kept (§6) -- its absence is always an
        # error. dialog_censored.wav, however, is a large WAV intermediate
        # that Step 6 deletes once audio_censored.wav exists (unless
        # keep_intermediates) -- so it's only *required* to still be on
        # disk if Step 6 hasn't run yet. Once Step 6 is done, its absence
        # is expected, not an error -- same reasoning as pipeline.py's
        # "Steps 1a-3b already complete" shortcut applies one step later
        # here, for this step's own output instead of its input.
        if not censor_log_out.exists():
            raise RuntimeError(
                f"Step 5 is marked complete but {censor_log_out} is missing.  "
                "Delete the job directory and re-run from scratch."
            )
        if "6_recombine" not in done and not censored_out.exists():
            raise RuntimeError(
                f"Step 5 is marked complete but {censored_out} is missing, and "
                "Step 6 (recombine) hasn't run yet to explain its absence.  "
                "Delete the job directory and re-run from scratch."
            )
        return censored_out

    matches_path = job_dir / "matches.json"
    if not matches_path.exists():
        raise RuntimeError(
            f"Step 5: {matches_path} not found — did Step 4b's flag phase "
            "complete?  Step 5 never scans the transcript itself; it only "
            "consumes Step 4b's matches.json (see design doc §4)."
        )
    if not dialog_path.exists():
        raise RuntimeError(
            f"Step 5: dialog stem not found at {dialog_path} — did Step 3b "
            "(merge) complete?"
        )

    method     = cfg_get(cfg, "censoring", "method", default="mute")
    padding_ms = float(cfg_get(cfg, "censoring", "padding_ms", default=50))

    log.info("Step 5 — mute dialog stem  (method=%s, padding=%.0fms)", method, padding_ms)

    matches_data = json.loads(matches_path.read_text())
    matches = matches_data.get("matches", [])
    log.info("  Loaded %d flagged match(es) from Step 4b.", len(matches))

    review_path = job_dir / "review.json"
    intervals, log_entries, n_skipped, n_added = _resolve_intervals(
        matches, review_path, padding_ms, log,
    )

    if not intervals:
        log.warning("  No intervals to mute — copying dialog.wav unmodified.")
        shutil.copyfile(dialog_path, censored_out)
        merged: list[tuple[float, float]] = []
    else:
        merged = _merge_intervals(intervals)
        log.info("  %d raw interval(s) → %d after merging overlaps.", len(intervals), len(merged))

        if method == "mute":
            enable_expr = "+".join(f"between(t,{s:.4f},{e:.4f})" for s, e in merged)
            run_cmd(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-y", "-i", str(dialog_path),
                    "-af", f"volume=enable='{enable_expr}':volume=0",
                    "-c:a", "pcm_s16le",
                    str(censored_out),
                ],
                log,
            )
            log.info(
                "  ✓  dialog_censored.wav  (%s)  %d muted interval(s)",
                fmt_size(censored_out), len(merged),
            )
        elif method == "beep":
            raise RuntimeError(
                "censoring.method: beep is not implemented yet in v1 — see design "
                "doc §10, Phase 4 (\"Beep replacement mode\"). Set censoring.method: "
                "mute in config.yaml to proceed."
            )
        else:
            raise RuntimeError(
                f"Step 5: unknown censoring.method '{method}' (expected 'mute' or 'beep')."
            )

    censor_log_out.write_text(json.dumps({"entries": log_entries}, indent=2, ensure_ascii=False))

    # dialog.wav (the uncensored stem) is fully consumed at this point --
    # nothing downstream ever needs it again, only dialog_censored.wav.
    # But it's also the one artifact that makes a future correction cheap
    # (see module docstring) -- so it's deleted only if keep_intermediate()
    # says no one wants it kept for either reason (see utils.py).
    if not keep_intermediate(cfg, correction_artifact=True):
        _unlink_if(dialog_path, log)

    state = read_job(job_dir)
    state["mute"] = {
        "method":          method,
        "padding_ms":      padding_ms,
        "candidates":      len(matches),
        "skipped":         n_skipped,
        "added":           n_added,
        "muted_intervals": len(merged),
    }
    write_job(job_dir, state)
    mark_step_done(job_dir, "5_mute")

    log.info("  ✓  Step 5 complete.  %d muted interval(s) logged to censor_log.json.", len(log_entries))
    return censored_out


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_intervals(
    matches: list[dict],
    review_path: Path,
    padding_ms: float,
    log: logging.LoggerAdapter,
) -> tuple[list[tuple[float, float]], list[dict], int, int]:
    """
    Apply review.json overrides on top of Step 4b's matches.json, then pad.

    Returns (intervals, log_entries, n_skipped, n_added):
      intervals   — list[(start_padded, end_padded)] in seconds, sorted by
                    start, unmerged
      log_entries — one dict per interval (same order/length as intervals),
                    for censor_log.json — kept at per-word granularity
                    even though intervals get merged for the ffmpeg filter
      n_skipped   — number of "skip" overrides applied
      n_added     — number of "add" overrides applied
    """
    skip_indices: set = set()
    add_overrides: list[dict] = []

    if review_path.exists():
        review_data = json.loads(review_path.read_text())
        for ov in review_data.get("overrides", []):
            if ov.get("action") == "skip":
                skip_indices.add(ov["word_index"])
            elif ov.get("action") == "add":
                add_overrides.append(ov)
        log.info(
            "  Applying review.json: %d skip override(s), %d add override(s).",
            len(skip_indices), len(add_overrides),
        )
    else:
        log.info(
            "  No review.json found — using all %d flagged match(es) as-is "
            "(unattended mode, or an interactive run with no candidates).",
            len(matches),
        )

    pad = padding_ms / 1000.0
    intervals: list[tuple[float, float]] = []
    log_entries: list[dict] = []

    for m in matches:
        if m["word_index"] in skip_indices:
            continue
        start, end = float(m["start"]), float(m["end"])
        p_start, p_end = max(0.0, start - pad), end + pad
        intervals.append((p_start, p_end))
        log_entries.append({
            "source":       "matched",
            "word":         m.get("matched_text"),
            "entry":        m.get("entry"),
            "word_index":   m.get("word_index"),
            "start":        round(start, 4),
            "end":          round(end, 4),
            "padded_start": round(p_start, 4),
            "padded_end":   round(p_end, 4),
            "score":        m.get("score"),
        })

    for ov in add_overrides:
        start, end = float(ov["start"]), float(ov["end"])
        p_start, p_end = max(0.0, start - pad), end + pad
        intervals.append((p_start, p_end))
        log_entries.append({
            "source":       "review_add",
            "word":         ov.get("text"),
            "entry":        None,
            "word_index":   ov.get("word_index"),
            "start":        round(start, 4),
            "end":          round(end, 4),
            "padded_start": round(p_start, 4),
            "padded_end":   round(p_end, 4),
            "score":        None,
        })

    # Stable sort: ties keep their original (matched-before-added) order in
    # both lists identically, since both are sorted by the same key.
    intervals.sort(key=lambda iv: iv[0])
    log_entries.sort(key=lambda e: e["padded_start"])

    return intervals, log_entries, len(skip_indices), len(add_overrides)


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/touching (start, end) tuples. Assumes input sorted by start."""
    if not intervals:
        return []
    merged = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _unlink_if(path: Path, log: logging.LoggerAdapter) -> None:
    """Delete a file if it exists; no-op and no error if absent."""
    if path.exists():
        path.unlink()
        log.debug("  Removed intermediate: %s", path.name)

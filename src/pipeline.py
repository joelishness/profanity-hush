#!/usr/bin/env python3
"""
profanity-hush — pipeline orchestrator

Phase 2b: Steps 1a, 1b, 1c, 2 (Demucs source separation).
Halts after Step 2 so separation results can be reviewed before
committing to the expensive transcription step.
"""
import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import utils
from utils import (
    cfg_get,
    find_job_dir,
    fmt_duration,
    mark_job_failed,
    read_job,
    setup_logging,
    step_logger,
    write_job,
)
from steps.extract  import extract_raw, downmix_to_stereo
from steps.segment  import segment  as run_segment
from steps.separate import separate as run_separate

# ── Fixed container paths ─────────────────────────────────────────────────────
JOBS_DIR    = Path("/jobs")
CONFIG_PATH = Path("/config/config.yaml")


# ── Job ID / directory ────────────────────────────────────────────────────────

def compute_job_id(video_path: Path) -> str:
    """
    Stable, content-independent job identifier: sha256[:12] of
    (absolute_path + ':' + mtime).

    Same path + mtime → same job_id → existing artifacts can be reused.
    File changes (new mtime) → new job_id → fresh job directory.
    """
    key = f"{video_path.resolve()}:{video_path.stat().st_mtime}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def make_job_dir_name(video: Path, job_id: str) -> str:
    """
    Build a descriptive, sortable job directory name:
      YYYYMMDD_HHMMSS_<slug>_<hex8>

    The slug is the video filename stem, lowercased with non-alphanumeric
    runs collapsed to a single hyphen, trimmed to ≤ 32 chars at a word
    boundary so the total directory name stays manageable.

    Examples:
      "When Love Is Gone.mkv"
        → 20260616_131611_when-love-is-gone_b9b7fddf

      "Captain America- Brave New World (2025).1080p.hevc.mkv"
        → 20260616_132242_captain-america-brave-new-world_c9b47bf5
    """
    ts   = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = Path(video.name).stem          # stop at last ".", drop extension(s)
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    if len(slug) > 32:
        # Trim at the last hyphen before the 32-char mark to avoid mid-word cuts
        slug = slug[:33].rsplit("-", 1)[0].rstrip("-")
    return f"{ts}_{slug}_{job_id[:8]}"


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    # ── CLI ───────────────────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="profanity-hush — automated movie profanity censoring",
    )
    parser.add_argument(
        "input_video",
        help="Path to the source video file inside the container (e.g. /input/movie.mkv)",
    )
    parser.add_argument(
        "subtitle_file",
        nargs="?",
        default=None,
        help="Optional SRT file for cross-reference — Phase 3, not yet implemented",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Pause for human review of flagged words before muting (Step 4b)",
    )
    parser.add_argument(
        "--no-interactive",
        dest="no_interactive",
        action="store_true",
        help="Force unattended mode (overrides config.yaml and AC_INTERACTIVE)",
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        metavar="PATH",
        help=f"Path to config.yaml inside the container (default: {CONFIG_PATH})",
    )
    args = parser.parse_args()

    # ── Config + logging ──────────────────────────────────────────────────────
    cfg       = utils.load_config(args.config)
    log_level = cfg_get(cfg, "output", "log_level", default="info")
    setup_logging(log_level)
    log = step_logger("pipeline")

    log.info("=" * 60)
    log.info("profanity-hush  (Phase 2b — Steps 1a / 1b / 1c / 2)")
    log.info("=" * 60)

    # ── Validate input ────────────────────────────────────────────────────────
    video = Path(args.input_video)
    if not video.exists():
        log.error("Input file not found: %s", video)
        sys.exit(1)

    if args.interactive and args.no_interactive:
        log.error("--interactive and --no-interactive are mutually exclusive.")
        sys.exit(1)

    if args.interactive:
        interactive = True
    elif args.no_interactive:
        interactive = False
    else:
        interactive = cfg_get(cfg, "interactive", "enabled", default=False)

    # ── Job store ─────────────────────────────────────────────────────────────
    job_id   = compute_job_id(video)
    job_dir  = find_job_dir(JOBS_DIR, job_id)
    resuming = job_dir is not None

    if not resuming:
        dir_name = make_job_dir_name(video, job_id)
        job_dir  = JOBS_DIR / dir_name
        job_dir.mkdir(parents=True, exist_ok=True)

    log.info("Job ID      : %s", job_id)
    log.info("Job dir     : %s", job_dir)
    log.info("Input       : %s", video)
    log.info("Config      : %s", args.config)
    log.info("Interactive : %s", interactive)

    state = read_job(job_dir)
    if not resuming:
        state = {
            "job_id":          job_id,
            "input_path":      str(video.resolve()),
            "input_filename":  video.name,
            "started_at":      datetime.now(tz=timezone.utc).isoformat(),
            "status":          "running",
            "steps_completed": [],
            "config_snapshot": cfg,
        }
        write_job(job_dir, state)
        log.info("Initialised job store → job.json")
    else:
        done = state.get("steps_completed", [])
        log.info("Resuming — steps already complete: %s", done or "(none)")

    # ── Step 1a: extract raw audio ────────────────────────────────────────────
    ext_log = step_logger("extract")
    try:
        extract_raw(video, job_dir, cfg, ext_log)
    except Exception as exc:
        ext_log.error("Step 1a failed: %s", exc)
        mark_job_failed(job_dir, "1a_extract_raw", exc)
        sys.exit(1)

    # ── Step 1b: downmix to stereo ────────────────────────────────────────────
    try:
        downmix_to_stereo(job_dir, cfg, ext_log)
    except Exception as exc:
        ext_log.error("Step 1b failed: %s", exc)
        mark_job_failed(job_dir, "1b_downmix", exc)
        sys.exit(1)

    # ── Step 1c: segment ──────────────────────────────────────────────────────
    seg_log = step_logger("segment")
    try:
        segments = run_segment(job_dir, cfg, seg_log)
    except Exception as exc:
        seg_log.error("Step 1c failed: %s", exc)
        mark_job_failed(job_dir, "1c_segment", exc)
        sys.exit(1)

    # ── Step 2: Demucs source separation ─────────────────────────────────────
    sep_log = step_logger("separate")
    try:
        stem_pairs = run_separate(job_dir, segments, cfg, sep_log)
    except Exception as exc:
        sep_log.error("Step 2 failed: %s", exc)
        mark_job_failed(job_dir, "2_separate", exc)
        sys.exit(1)

    # ── Phase 2b halt ─────────────────────────────────────────────────────────
    state = read_job(job_dir)
    state["status"] = "halted_after_2"
    write_job(job_dir, state)

    duration = state.get("total_duration_sec", 0.0)

    log.info("-" * 60)
    log.info("Steps 1a / 1b / 1c / 2 complete.")
    log.info("")
    log.info("  Input duration : %s  (%.1f s)", fmt_duration(duration), duration)
    log.info("  Segments       : %d", len(segments))
    for i, ((seg_path, _start), (dialog, score_sfx)) in enumerate(
        zip(segments, stem_pairs)
    ):
        log.info(
            "    [%d] %s  →  %s  +  %s",
            i + 1, seg_path.name, dialog.name, score_sfx.name,
        )
    log.info("")
    log.info("  Steps done  : %s", state.get("steps_completed", []))
    log.info("  Job store   : %s", job_dir)
    log.info("-" * 60)
    log.info("Pipeline halted — Step 3 (transcription) not yet implemented.")
    log.info("Review the dialog/score_sfx stems, then run again to re-use artifacts.")


if __name__ == "__main__":
    main()

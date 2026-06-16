#!/usr/bin/env python3
"""
profanity-hush — pipeline orchestrator

Phase 2a: implements Steps 1a, 1b, 1c then halts cleanly.
The halt lets us verify extraction and segmentation before
committing to the expensive Steps 2–7.
"""
import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import utils
from utils import (
    cfg_get,
    fmt_duration,
    mark_job_failed,
    mark_step_done,
    read_job,
    setup_logging,
    step_logger,
    write_job,
)
from steps.extract import extract_raw, downmix_to_stereo
from steps.segment import segment as run_segment

# ── Fixed container paths ─────────────────────────────────────────────────────
JOBS_DIR    = Path("/jobs")
CONFIG_PATH = Path("/config/config.yaml")


# ── Job ID ────────────────────────────────────────────────────────────────────

def compute_job_id(video_path: Path) -> str:
    """
    Stable, content-independent job identifier.

    sha256[:12] of (absolute_path + ':' + mtime) — identical file path and
    mtime → identical job_id → existing job store artifacts can be reused
    without re-running expensive steps.

    Changing the file (new mtime) → new job_id → fresh job directory.
    """
    key = f"{video_path.resolve()}:{video_path.stat().st_mtime}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


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
    log.info("profanity-hush  (Phase 2a — Steps 1a / 1b / 1c)")
    log.info("=" * 60)

    # ── Validate input ────────────────────────────────────────────────────────
    video = Path(args.input_video)
    if not video.exists():
        log.error("Input file not found: %s", video)
        sys.exit(1)

    if args.interactive and args.no_interactive:
        log.error("--interactive and --no-interactive are mutually exclusive.")
        sys.exit(1)

    # Resolve effective interactive mode (CLI flags override config/env)
    if args.interactive:
        interactive = True
    elif args.no_interactive:
        interactive = False
    else:
        interactive = cfg_get(cfg, "interactive", "enabled", default=False)

    # ── Job store ─────────────────────────────────────────────────────────────
    job_id  = compute_job_id(video)
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    log.info("Job ID      : %s", job_id)
    log.info("Job dir     : %s", job_dir)
    log.info("Input       : %s", video)
    log.info("Config      : %s", args.config)
    log.info("Interactive : %s", interactive)

    # Initialise or resume job.json
    state = read_job(job_dir)
    if not state:
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

    # ── Phase 2a halt ─────────────────────────────────────────────────────────
    state = read_job(job_dir)
    state["status"] = "halted_after_1c"
    write_job(job_dir, state)

    duration = state.get("total_duration_sec", 0.0)

    log.info("-" * 60)
    log.info("Steps 1a / 1b / 1c complete.")
    log.info("")
    log.info("  Input duration : %s  (%.1f s)", fmt_duration(duration), duration)
    log.info("  Segments       : %d", len(segments))
    for seg_path, start in segments:
        log.info("    %s  offset=%s", seg_path.name, fmt_duration(start))
    log.info("")
    log.info("  Steps done  : %s", state.get("steps_completed", []))
    log.info("  Job store   : %s", job_dir)
    log.info("-" * 60)
    log.info("Pipeline halted — Step 2 (source separation) not yet implemented.")
    log.info("Review job.json and the segment files, then run again to re-use artifacts.")


if __name__ == "__main__":
    main()

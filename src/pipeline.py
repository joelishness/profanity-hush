#!/usr/bin/env python3
"""
profanity-hush — pipeline orchestrator

Phase 2 complete: Steps 1a, 1b, 1c, 2, 3, 3b, 4b.
Step 4 (SRT alignment) is skipped for now — Step 4b runs directly against
transcript.json. Halts after Step 4b (or after 3b in unattended mode, where
4b doesn't run at all). Step 5 (mute) not yet implemented.
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
from steps.transcribe import transcribe as run_transcribe
from steps.merge      import merge     as run_merge
from steps.review     import review    as run_review, ReviewAborted
from steps.matching   import resolve_word_list_path

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

    log.info("==" * 30)
    log.info("profanity-hush  (Phase 2 — core pipeline)")
    log.info("==" * 30)

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

    if interactive and not sys.stdin.isatty():
        log.error(
            "Interactive mode is active, but stdin is not a TTY — there's no "
            "terminal to show Step 4b's review prompts. Failing now, before "
            "Steps 1-3b run, rather than hanging or crashing on the first "
            "prompt after hours of processing."
        )
        log.error(
            "If running via hush.sh, pass --interactive on the command line "
            "(it allocates a TTY automatically). If running docker directly, "
            "add -it to the docker run invocation."
        )
        sys.exit(1)

    # Step 4b (and, later, Step 5) need a word list. Resolved here — not
    # just inside steps/review.py — so the fallback (and any log line about
    # it) happens once, up front, rather than being silently re-derived
    # deep inside whichever step runs first. Falls back to the built-in
    # default baked into the image (see steps/matching.py) when the host's
    # /config/word_list.txt isn't present — this is what makes skipping
    # config file installation (README install step 3) actually work for
    # the word list, not just for config.yaml's scalar settings.
    word_list_path = Path(cfg_get(cfg, "censoring", "word_list", default="/config/word_list.txt"))
    word_list_path = resolve_word_list_path(word_list_path, log)
    cfg.setdefault("censoring", {})["word_list"] = str(word_list_path)

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
        # Clear bookkeeping from a prior failed attempt, if any.  Without
        # this, a job that failed once (e.g. a transient OOM kill) and then
        # succeeded on retry keeps a stale "failure" block in job.json
        # forever, alongside a status that says it completed — misleading
        # for anyone (or any future tool, see §13.4) reading job.json to
        # judge whether the job is currently healthy.
        if state.pop("failure", None) is not None or state.pop("failed_at", None) is not None:
            log.info("  Cleared stale failure record from a prior attempt.")
        state["status"] = "running"
        write_job(job_dir, state)

    done = state.get("steps_completed", [])

    if "3b_merge" in done:
        # Steps 1a-3b have nothing left to do: merge.py already produced the
        # canonical outputs, and — this is the important part — its cleanup
        # may have already deleted the per-segment intermediates
        # (dialog_NN.wav, score_sfx_NN.wav, audio_stereo_NN.wav) that
        # separate.py's own "already done" resume path would otherwise try
        # to reload. Calling separate()/transcribe()/merge() again here
        # would hit exactly that: each step's resume check trusts job.json
        # and assumes its own files are still on disk, which is no longer
        # true once a *later* step has cleaned them up. So once 3b_merge is
        # done, skip straight to the canonical files by fixed name — nothing
        # past this point ever needs the per-segment intermediates again.
        log.info("Steps 1a-3b already complete — skipping straight to Step 4b.")
        transcript_out = job_dir / "transcript.json"
        dialog_out     = job_dir / "dialog.wav"
        score_sfx_out  = job_dir / "score_sfx.wav"
        for p in (transcript_out, dialog_out, score_sfx_out):
            if not p.exists():
                log.error(
                    "Step 3b is marked complete but %s is missing.  "
                    "Delete the job directory and re-run from scratch.", p,
                )
                sys.exit(1)
        n_segments = len(state.get("segments", []))
    else:
        # ── Step 1a: extract raw audio ────────────────────────────────────────
        ext_log = step_logger("extract")
        try:
            extract_raw(video, job_dir, cfg, ext_log)
        except Exception as exc:
            ext_log.error("Step 1a failed: %s", exc)
            mark_job_failed(job_dir, "1a_extract_raw", exc)
            sys.exit(1)

        # ── Step 1b: downmix to stereo ─────────────────────────────────────────
        try:
            downmix_to_stereo(job_dir, cfg, ext_log)
        except Exception as exc:
            ext_log.error("Step 1b failed: %s", exc)
            mark_job_failed(job_dir, "1b_downmix", exc)
            sys.exit(1)

        # ── Step 1c: segment ────────────────────────────────────────────────────
        seg_log = step_logger("segment")
        try:
            segments = run_segment(job_dir, cfg, seg_log)
        except Exception as exc:
            seg_log.error("Step 1c failed: %s", exc)
            mark_job_failed(job_dir, "1c_segment", exc)
            sys.exit(1)

        # ── Step 2: Demucs source separation ───────────────────────────────────
        sep_log = step_logger("separate")
        try:
            stem_pairs = run_separate(job_dir, segments, cfg, sep_log)
        except Exception as exc:
            sep_log.error("Step 2 failed: %s", exc)
            mark_job_failed(job_dir, "2_separate", exc)
            sys.exit(1)

        # ── Step 3: WhisperX transcription ─────────────────────────────────────
        tr_log = step_logger("transcribe")
        try:
            transcript_paths = run_transcribe(job_dir, segments, stem_pairs, cfg, tr_log)
        except Exception as exc:
            tr_log.error("Step 3 failed: %s", exc)
            mark_job_failed(job_dir, "3_transcribe", exc)
            sys.exit(1)

        # ── Step 3b: merge transcripts + audio stems ───────────────────────────
        mg_log = step_logger("merge")
        try:
            transcript_out, dialog_out, score_sfx_out = run_merge(
                job_dir, segments, stem_pairs, transcript_paths, cfg, mg_log,
            )
        except Exception as exc:
            mg_log.error("Step 3b failed: %s", exc)
            mark_job_failed(job_dir, "3b_merge", exc)
            sys.exit(1)

        n_segments = len(segments)

    # ── Step 4: SRT alignment ─────────────────────────────────────────────────
    # Skipped for now — not yet implemented. Step 4b reads transcript.json
    # directly. Swapping this for transcript_aligned.json later needs no
    # change to Step 4b itself (same schema, see steps/review.py docstring).

    # ── Step 4b: interactive review (only when interactive mode is active) ──
    review_path = None
    if interactive:
        rv_log = step_logger("review")
        try:
            review_path = run_review(job_dir, transcript_out, cfg, rv_log)
        except ReviewAborted:
            rv_log.info("Step 4b aborted by user — no changes written. Re-run to try again.")
            sys.exit(0)
        except Exception as exc:
            rv_log.error("Step 4b failed: %s", exc)
            mark_job_failed(job_dir, "4b_review", exc)
            sys.exit(1)

    # ── Phase 2 halt (after Step 3b, or Step 4b when interactive) ────────────
    state = read_job(job_dir)
    state["status"] = "halted_after_4b" if review_path else "halted_after_3b"
    write_job(job_dir, state)

    duration = state.get("total_duration_sec", 0.0)
    n_words  = state.get("merge", {}).get("word_count", 0)

    steps_label = "1a / 1b / 1c / 2 / 3 / 3b" + (" / 4b" if review_path else "")

    log.info("-" * 60)
    log.info("Steps %s complete.", steps_label)
    log.info("")
    log.info("  Input duration   : %s  (%.1f s)", fmt_duration(duration), duration)
    log.info("  Segments         : %d", n_segments)
    log.info("  Total words      : %d", n_words)
    if review_path:
        rv = state.get("review", {})
        log.info(
            "  Review           : %d candidates, %d approved, %d rejected, %d added, %d auto-approved",
            rv.get("candidates", 0), rv.get("approved", 0), rv.get("rejected", 0),
            rv.get("added", 0), rv.get("auto_approved", 0),
        )
    log.info("")
    log.info("  Canonical outputs:")
    log.info("    %s", transcript_out)
    log.info("    %s", dialog_out)
    log.info("    %s", score_sfx_out)
    if review_path:
        log.info("    %s", review_path)
    log.info("")
    log.info("  Steps done : %s", state.get("steps_completed", []))
    log.info("  Job store  : %s", job_dir)
    log.info("-" * 60)
    log.info("Pipeline halted — Step 5 (mute) not yet implemented.")
    if review_path:
        log.info("Review review.json, then re-run to reuse existing artifacts.")
    else:
        log.info("Review transcript.json, then re-run to reuse existing artifacts.")


if __name__ == "__main__":
    main()

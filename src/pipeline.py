#!/usr/bin/env python3
"""
profanity-hush — pipeline orchestrator

v1 core pipeline: Steps 1a, 1b, 1c, 2, 3, 3b, 4b (flag + optional review),
5 (mute), 6 (recombine), 6b (encode), 7 (mux). Step 7 is the last step — a
successful run produces the final censored video in /output and marks the
job 'complete'.

Step 4 (SRT alignment) is skipped for now — Step 4b's flag phase reads
transcript.json directly. Step 4b's flag phase always runs (both
interactive and unattended); its interactive review phase only runs when
interactive mode is active. Step 5 reads Step 4b's flagged matches
directly from matches.json — it does not re-scan the transcript itself
(see design doc §4).

Correction mode (--skip-index / --add-interval / --redo-review, design
doc §13.4): re-running hush on the *same* input file (same path, same
mtime -- compute_job_id() naturally lands on the same job, no separate
job-id flag needed) with one of these flags edits review.json and forces
Steps 5, 6, 6b, and 7 to redo, without repeating Steps 1-4b. This is the
primary expected correction workflow: run unattended, watch the film, note
any mistakes (a word muted that shouldn't have been, or a miss), then
re-run with a targeted fix. It depends on dialog.wav and score_sfx.wav
still being on disk (output.keep_correction_artifacts, default true — see
steps/mute.py and steps/recombine.py); without them, a correction would
require re-running Step 2's Demucs separation from scratch.

--redo-step STEP is a separate, narrower tool: it forces exactly the
named step(s) (one of 4b_flag, 4b_review, 5_mute, 6_recombine, 6b_encode,
7_mux) to redo on an existing job, with no review.json involved at all.
For testing a change to a step's own implementation (e.g. switching
Step 7 from ffmpeg to mkvmerge) against a job that's already sitting on
disk, this is the supported alternative to hand-editing steps_completed
in job.json directly -- editing job.json works as far as the steps
themselves are concerned (each one only ever checks its own entry; see
steps/mute.py, steps/recombine.py, steps/encode.py, steps/mux.py), but a
syntax slip while editing it by hand (e.g. a stray trailing comma) makes
the whole file invalid JSON, which utils.find_job_dir() can no longer
match against job_id -- silently turning "resume this job" into "start a
fresh one," with hours of needless Steps 1a-3b work the only symptom.
--redo-step refuses outright if no existing job is found, rather than
falling through to a fresh run, and never writes job.json by hand.
"""
import argparse
import hashlib
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import utils
from utils import (
    cfg_get,
    find_job_dir,
    fmt_duration,
    mark_job_failed,
    read_job,
    retention_summary,
    setup_logging,
    step_logger,
    unmark_step_done,
    write_job,
)
from steps.extract  import extract_raw, downmix_to_stereo
from steps.segment  import segment  as run_segment
from steps.separate import separate as run_separate
from steps.transcribe import transcribe as run_transcribe
from steps.merge      import merge     as run_merge
from steps.review     import (
    flag             as run_flag,
    review           as run_review,
    apply_corrections,
    ReviewAborted,
)
from steps.mute       import mute      as run_mute
from steps.recombine  import recombine as run_recombine
from steps.encode     import encode    as run_encode
from steps.mux        import mux       as run_mux
from steps.matching   import resolve_word_list_path

# ── Fixed container paths ─────────────────────────────────────────────────────
JOBS_DIR    = Path("/jobs")
OUTPUT_DIR  = Path("/output")
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
    Build a descriptive job directory name:
      YYYYMMDD_HHMMSS_<slug>_<hex8>

    The slug is the video filename stem, lowercased with non-alphanumeric
    runs collapsed to a single hyphen, trimmed to ≤ 32 chars at a word
    boundary so the total directory name stays manageable.

    The leading timestamp uses utils.LOCAL_TZ -- the same host-offset-aware
    local time as every console log line and job.json's *_local fields --
    not UTC. This is one of the places a person is most likely to actually
    look (browsing ~/.local/share/profanity-hush/jobs/ directly), so it
    should read as what their own clock said, not require doing timezone
    arithmetic to make sense of. An earlier version of this kept the
    timestamp in UTC for monotonic, DST-safe `ls` ordering -- but nothing
    in this codebase actually depends on directory-name ordering for
    correctness (job resume scans job.json's contents via find_job_dir(),
    never the directory name itself -- see utils.py), so that was paying
    for a rare, cosmetic-only edge case (two jobs landing on the same
    wall-clock minute across a DST "fall back", which only changes their
    relative order in an `ls` listing, not anything the pipeline does)
    with confusion that a person would hit on literally every single job.

    Examples:
      "When Love Is Gone.mkv"
        → 20260616_131611_when-love-is-gone_b9b7fddf

      "Captain America- Brave New World (2025).1080p.hevc.mkv"
        → 20260616_132242_captain-america-brave-new-world_c9b47bf5
    """
    ts   = datetime.now(tz=utils.LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
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
    parser.add_argument(
        "--skip-index",
        type=int,
        action="append",
        default=None,
        metavar="N",
        help=(
            "Correction mode: reject the flagged match at this word_index "
            "(see matches.json or censor_log.json's 'word_index' field) so "
            "it's no longer muted. Repeatable. Requires a job that already "
            "completed Step 4b's flag phase for this exact input file; "
            "forces Steps 5, 6, 6b, and 7 to redo with the corrected review.json."
        ),
    )
    parser.add_argument(
        "--add-interval",
        nargs=3,
        action="append",
        default=None,
        metavar=("TEXT", "START", "END"),
        help=(
            "Correction mode: add a manual mute interval -- TEXT (your own "
            "note; not matched against anything), START and END as either "
            "raw seconds (e.g. 1203.14) or H:MM:SS.mmm (e.g. 0:20:03.140) -- "
            "see censor_log.json/matches.json's start_hms/end_hms fields for "
            "the same notation read back from a previous run. "
            "Repeatable. Forces Steps 5, 6, 6b, and 7 to redo."
        ),
    )
    parser.add_argument(
        "--redo-review",
        action="store_true",
        help=(
            "Correction mode: re-enter Step 4b's interactive review loop "
            "from scratch, even though it already ran (implies --interactive "
            "for this run). Re-presents every flagged match, not just new "
            "ones -- prefer --skip-index/--add-interval for a single "
            "targeted fix. Cannot be combined with --skip-index/--add-interval "
            "in the same invocation; the review loop rewrites review.json "
            "from scratch and would discard those direct edits."
        ),
    )
    parser.add_argument(
        "--redo-step",
        dest="redo_steps",
        action="append",
        default=None,
        metavar="STEP",
        choices=["4b_flag", "4b_review", "5_mute", "6_recombine", "6b_encode", "7_mux"],
        help=(
            "Force this step to redo on an existing job, even though it's "
            "already marked complete -- for re-testing a change to the step "
            "itself (a new muxer, a tuned mute padding, a fixed encode "
            "command) against a job that already exists, without rerunning "
            "everything before it. Repeatable. Unlike "
            "--skip-index/--add-interval/--redo-review (which edit "
            "review.json to fix a *content* mistake and always redo Steps "
            "5, 6, 6b, and 7 together), this only clears the named step(s) "
            "from steps_completed -- nothing else is touched, and only the "
            "step(s) named are redone. Steps 1a-3b aren't offered here: "
            "they're resumed as one atomic block (see the 'Steps 1a-3b "
            "already complete' check below) and their per-segment "
            "intermediates may already be deleted, so redoing one alone "
            "isn't safe. Requires a job that already exists for this exact "
            "input file (same path, same mtime) -- this is a targeted "
            "*redo*, not a way to start a fresh job, so it refuses outright "
            "rather than silently falling through to a full re-run if no "
            "existing job is found (e.g. because compute_job_id() landed on "
            "a different file, or because the existing job.json failed to "
            "parse -- see the warning utils.find_job_dir() logs in that "
            "case). Cannot be combined with --skip-index/--add-interval/"
            "--redo-review in the same invocation; run them separately."
        ),
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
    for line in utils.timezone_banner().splitlines():
        log.info("%s", line)

    # ── Validate input ────────────────────────────────────────────────────────
    video = Path(args.input_video)
    if not video.exists():
        log.error("Input file not found: %s", video)
        sys.exit(1)

    if args.interactive and args.no_interactive:
        log.error("--interactive and --no-interactive are mutually exclusive.")
        sys.exit(1)

    if args.redo_review and args.no_interactive:
        log.error(
            "--redo-review and --no-interactive are mutually exclusive "
            "(--redo-review needs the interactive loop it's asking to re-run)."
        )
        sys.exit(1)

    if args.redo_review and (args.skip_index or args.add_interval):
        log.error(
            "--redo-review cannot be combined with --skip-index/--add-interval "
            "in the same invocation -- the interactive loop rewrites review.json "
            "from scratch and would discard those direct edits. Run them in "
            "separate invocations instead."
        )
        sys.exit(1)

    if args.redo_steps and (args.skip_index or args.add_interval or args.redo_review):
        log.error(
            "--redo-step cannot be combined with --skip-index/--add-interval/"
            "--redo-review in the same invocation -- those edit review.json "
            "to fix a content mistake and always redo Steps 5, 6, 6b, and 7 "
            "together; --redo-step only forces the step(s) named. Run them "
            "in separate invocations instead."
        )
        sys.exit(1)

    if args.interactive:
        interactive = True
    elif args.no_interactive:
        interactive = False
    else:
        interactive = cfg_get(cfg, "interactive", "enabled", default=False)

    if args.redo_review:
        interactive = True  # correction mode forces this, regardless of config/other flags

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

    # Step 4b's flag phase needs a word list. Resolved here — not just
    # inside steps/review.py — so the fallback (and any log line about it)
    # happens once, up front, rather than being silently re-derived deep
    # inside whichever step runs first. Falls back to the built-in default
    # baked into the image (see steps/matching.py) when the host's
    # /config/word_list.txt isn't present — this is what makes skipping
    # config file installation (README install step 3) actually work for
    # the word list, not just for config.yaml's scalar settings. Step 5
    # (mute) no longer touches the word list at all — it only consumes
    # Step 4b's already-resolved matches.json.
    word_list_path = Path(cfg_get(cfg, "censoring", "word_list", default="/config/word_list.txt"))
    word_list_path = resolve_word_list_path(word_list_path, log)
    cfg.setdefault("censoring", {})["word_list"] = str(word_list_path)

    # ── Job store ─────────────────────────────────────────────────────────────
    job_id   = compute_job_id(video)
    job_dir  = find_job_dir(JOBS_DIR, job_id, log)
    resuming = job_dir is not None

    if not resuming:
        dir_name = make_job_dir_name(video, job_id)
        job_dir  = JOBS_DIR / dir_name
        job_dir.mkdir(parents=True, exist_ok=True)

    # From here on, every line also lands in job_dir/logs/{timestamp}.log --
    # see utils.attach_file_logging() for why this can't start any earlier
    # (job_dir itself isn't known until the lines just above), and why the
    # next handful of lines deliberately repeat Job ID/Job dir/Input (this
    # job had already announced them once, console-only, while resolving
    # job_dir) -- so the log file is self-contained and makes sense on its
    # own, without needing the console scrollback from a few lines earlier.
    log_path = utils.attach_file_logging(job_dir, log_level, log)

    log.info("Job ID      : %s", job_id)
    log.info("Job dir     : %s", job_dir)
    log.info("Input       : %s", video)
    log.info("Config      : %s", args.config)
    log.info("Interactive : %s", interactive)
    for line in retention_summary(cfg).splitlines():
        log.info("%s", line)

    state = read_job(job_dir)
    if not resuming:
        now = time.time()
        started_at_local, _ = utils.fmt_wall_clock(now)
        state = {
            "job_id":           job_id,
            "input_path":       str(video.resolve()),
            "input_filename":   video.name,
            "started_at":       datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "started_at_local": started_at_local,   # human convenience; started_at above is canonical
            "status":           "running",
            "steps_completed":  [],
            "config_snapshot":  cfg,
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
        cleared_failure = False
        for key in ("failure", "failed_at", "failed_at_local"):
            if state.pop(key, None) is not None:
                cleared_failure = True
        if cleared_failure:
            log.info("  Cleared stale failure record from a prior attempt.")
        # Backfill for job.json files written before started_at_local
        # existed -- a display convenience only (started_at, above, is
        # and remains the canonical UTC field), so it's fine for this to
        # reflect *this* run's resolved LOCAL_TZ rather than whatever (if
        # anything) was active when the job was first created. Rebuilt
        # rather than just assigned so it lands right after started_at
        # instead of tacked onto the end of the dict.
        if "started_at" in state and "started_at_local" not in state:
            started_at_local, _ = utils.fmt_wall_clock(
                utils.parse_iso_to_epoch(state["started_at"])
            )
            reordered = {}
            for k, v in state.items():
                reordered[k] = v
                if k == "started_at":
                    reordered["started_at_local"] = started_at_local
            state = reordered
        state["status"] = "running"
        write_job(job_dir, state)

    if "started_at" in state:
        started_local, started_utc = utils.fmt_wall_clock(utils.parse_iso_to_epoch(state["started_at"]))
        log.info("Started     : %s  (%s)", started_local, started_utc)

    done = state.get("steps_completed", [])

    correcting = bool(args.skip_index or args.add_interval or args.redo_review)
    if correcting:
        # Correction mode (design doc §13.4): the job must already have
        # flagged candidates to correct against. compute_job_id() is
        # path+mtime based, so re-running hush.sh on the same (unmodified)
        # input file naturally lands on this same job — no separate job-id
        # flag needed to find it.
        if not resuming or "4b_flag" not in done:
            log.error(
                "Correction flags (--skip-index / --add-interval / --redo-review) "
                "require a job that has already completed Step 4b's flag phase "
                "for this exact input file (same path, same mtime) -- there's "
                "nothing to correct yet. Run hush normally first."
            )
            sys.exit(1)

        cx_log = step_logger("correct")
        if args.skip_index or args.add_interval:
            try:
                apply_corrections(
                    job_dir,
                    skip_indices=args.skip_index or [],
                    add_intervals=args.add_interval or [],
                    log=cx_log,
                )
            except Exception as exc:
                cx_log.error("Applying corrections failed: %s", exc)
                sys.exit(1)

        if args.redo_review:
            unmark_step_done(job_dir, "4b_review")

        # Steps 5, 6, 6b, and 7 all depend, directly or indirectly, on
        # review.json -- invalidate every one of them so the normal step
        # machinery below redoes them with the corrected overrides, rather
        # than hitting their own "already complete" resume-checks. This is
        # also why output.keep_correction_artifacts (steps/mute.py,
        # steps/recombine.py) defaults to true: Step 5 needs dialog.wav to
        # still be on disk to actually redo, not just to be told it should.
        for step in ("5_mute", "6_recombine", "6b_encode", "7_mux"):
            unmark_step_done(job_dir, step)

        cx_log.info("Correction recorded -- Steps 5, 6, 6b, and 7 will redo to apply it.")
        if args.redo_review:
            cx_log.info("Step 4b's review loop will also re-run from scratch (--redo-review).")

        done = read_job(job_dir).get("steps_completed", [])

    if args.redo_steps:
        # Deliberately stricter than the rest of this function: if no
        # existing job was found for this exact input file, this is NOT
        # treated as "start a fresh job" the way a plain run would be.
        # --redo-step's entire point is to act on a job that's already
        # there; silently falling through to a full from-scratch run
        # instead is exactly the failure mode this flag exists to prevent
        # (e.g. a job that *does* exist on disk but wasn't matched because
        # its job.json failed to parse -- see the warning utils.find_job_dir()
        # logs above, near "Job ID").
        if not resuming:
            log.error(
                "--redo-step requires an existing job for this exact input "
                "file (same path, same mtime) -- none was found, so there's "
                "nothing to redo. If you expected one to be found, check "
                "the console output above (right after \"Job ID\") for a "
                "\"Skipping unreadable job file\" warning -- a job.json "
                "that fails to parse is treated the same as one that "
                "doesn't exist, on purpose, rather than guessing at how to "
                "fix it. Otherwise, run hush normally first to create the job."
            )
            sys.exit(1)

        rs_log = step_logger("redo-step")
        for step in args.redo_steps:
            unmark_step_done(job_dir, step)
        rs_log.info("Forcing redo of: %s", ", ".join(args.redo_steps))
        done = read_job(job_dir).get("steps_completed", [])

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

        if not transcript_out.exists():
            log.error(
                "Step 3b is marked complete but %s is missing.  "
                "Delete the job directory and re-run from scratch.", transcript_out,
            )
            sys.exit(1)
        # dialog.wav and score_sfx.wav are large intermediates that Steps 5
        # and 6 respectively delete once they're no longer needed -- but
        # only if BOTH keep_intermediates and keep_correction_artifacts are
        # false (see steps/mute.py and steps/recombine.py; the latter
        # defaults to true specifically so a future correction redo, like
        # this one, has something to redo with). Each is only *required*
        # to still be on disk if the step that consumes it hasn't run yet
        # -- or, in correction mode, if it ran under the old default
        # (before keep_correction_artifacts existed) or with that setting
        # explicitly disabled.
        if "5_mute" not in done and not dialog_out.exists():
            log.error(
                "%s is missing, and Step 5 (mute) hasn't completed yet to "
                "explain its absence.%s",
                dialog_out,
                "  Delete the job directory and re-run from scratch."
                if not correcting else
                "  This job's dialog.wav was already deleted by an earlier run "
                "(output.keep_correction_artifacts was false, or this predates "
                "that setting) -- correcting it now requires a full re-run from "
                "scratch, including Step 2's Demucs separation.",
            )
            sys.exit(1)
        if "6_recombine" not in done and not score_sfx_out.exists():
            log.error(
                "%s is missing, and Step 6 (recombine) hasn't completed yet to "
                "explain its absence.%s",
                score_sfx_out,
                "  Delete the job directory and re-run from scratch."
                if not correcting else
                "  This job's score_sfx.wav was already deleted by an earlier run "
                "(output.keep_correction_artifacts was false, or this predates "
                "that setting) -- correcting it now requires a full re-run from "
                "scratch, including Step 2's Demucs separation.",
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
    # Skipped for now — not yet implemented. Step 4b's flag phase reads
    # transcript.json directly. Swapping this for transcript_aligned.json
    # later needs no change to Step 4b itself (same schema, see
    # steps/review.py's docstring).

    # ── Step 4b: flag (always runs, both modes — see design doc §4) ──────────
    fl_log = step_logger("flag")
    try:
        matches_out = run_flag(job_dir, transcript_out, cfg, fl_log)
    except Exception as exc:
        fl_log.error("Step 4b (flag) failed: %s", exc)
        mark_job_failed(job_dir, "4b_flag", exc)
        sys.exit(1)

    # ── Step 4b: review (optional sequence run after flag) ────────────────────
    review_path = None
    if interactive:
        rv_log = step_logger("review")
        try:
            review_path = run_review(job_dir, matches_out, transcript_out, cfg, rv_log)
        except ReviewAborted:
            rv_log.info("Step 4b (review) aborted by user — no changes written. Re-run to try again.")
            sys.exit(0)
        except Exception as exc:
            rv_log.error("Step 4b (review) failed: %s", exc)
            mark_job_failed(job_dir, "4b_review", exc)
            sys.exit(1)

    # ── Step 5: mute dialog stem ───────────────────────────────────────────────
    # Reads matches_out (+ review_path, if it ran) directly — no re-scan.
    mu_log = step_logger("mute")
    try:
        dialog_censored_out = run_mute(job_dir, dialog_out, cfg, mu_log)
    except Exception as exc:
        mu_log.error("Step 5 failed: %s", exc)
        mark_job_failed(job_dir, "5_mute", exc)
        sys.exit(1)

    # ── Step 6: recombine dialog (censored) + score/SFX stems ─────────────────
    rc_log = step_logger("recombine")
    try:
        audio_censored_out = run_recombine(job_dir, dialog_censored_out, score_sfx_out, cfg, rc_log)
    except Exception as exc:
        rc_log.error("Step 6 failed: %s", exc)
        mark_job_failed(job_dir, "6_recombine", exc)
        sys.exit(1)

    # ── Step 6b: encode censored audio to match original codec ────────────────
    en_log = step_logger("encode")
    try:
        audio_encoded_out = run_encode(job_dir, video, audio_censored_out, cfg, en_log)
    except Exception as exc:
        en_log.error("Step 6b failed: %s", exc)
        mark_job_failed(job_dir, "6b_encode", exc)
        sys.exit(1)

    # ── Step 7: mux encoded audio into the original video ──────────────────────
    mx_log = step_logger("mux")
    try:
        output_video = run_mux(job_dir, video, audio_encoded_out, OUTPUT_DIR, cfg, mx_log)
    except Exception as exc:
        mx_log.error("Step 7 failed: %s", exc)
        mark_job_failed(job_dir, "7_mux", exc)
        sys.exit(1)

    # ── Pipeline complete ───────────────────────────────────────────────────────
    state = read_job(job_dir)
    state["status"] = "complete"
    completed_epoch = time.time()
    state["completed_at"] = datetime.fromtimestamp(completed_epoch, tz=timezone.utc).isoformat()
    state["completed_at_local"], _ = utils.fmt_wall_clock(completed_epoch)   # human convenience; completed_at above is canonical
    write_job(job_dir, state)

    duration = state.get("total_duration_sec", 0.0)
    n_words  = state.get("merge", {}).get("word_count", 0)
    flag_st  = state.get("flag", {})
    mute_st  = state.get("mute", {})
    encode_st = state.get("encode", {})

    steps_label = "1a / 1b / 1c / 2 / 3 / 3b / 4b (flag)"
    if review_path:
        steps_label += " + 4b (review)"
    steps_label += " / 5 (mute) / 6 (recombine) / 6b (encode) / 7 (mux)"

    log.info("=" * 60)
    log.info("Pipeline complete!  Steps %s.", steps_label)
    if correcting:
        log.info("  (Steps 5, 6, 6b, and 7 were redone to apply a correction; see job.json's history for prior runs.)")
    log.info("")
    if "started_at" in state:
        started_local, started_utc = utils.fmt_wall_clock(utils.parse_iso_to_epoch(state["started_at"]))
        log.info("  Started          : %s  (%s)", started_local, started_utc)
    finished_local, finished_utc = utils.fmt_wall_clock(completed_epoch)
    log.info("  Finished         : %s  (%s)", finished_local, finished_utc)
    log.info("  Input duration   : %s  (%.1f s)", fmt_duration(duration), duration)
    log.info("  Segments         : %d", n_segments)
    log.info("  Total words      : %d", n_words)
    log.info("  Flagged matches  : %d", flag_st.get("candidates", 0))
    if review_path:
        rv = state.get("review", {})
        log.info(
            "  Review           : %d candidates, %d approved, %d rejected, %d added, %d auto-approved",
            rv.get("candidates", 0), rv.get("approved", 0), rv.get("rejected", 0),
            rv.get("added", 0), rv.get("auto_approved", 0),
        )
    log.info(
        "  Muted intervals  : %d  (method=%s, padding=%sms)",
        mute_st.get("muted_intervals", 0), mute_st.get("method", "?"), mute_st.get("padding_ms", "?"),
    )
    if encode_st.get("fallback_reason"):
        log.info(
            "  Audio track      : %s @ %s bps  (FALLBACK — %s; verify sync/quality)",
            encode_st.get("encoder", "?"), encode_st.get("bitrate", "?"), encode_st.get("fallback_reason"),
        )
    else:
        log.info(
            "  Audio track      : %s @ %s bps  (matches original codec)",
            encode_st.get("encoder", "?"), encode_st.get("bitrate", "?"),
        )
    log.info("")
    log.info("  Final output     : %s", output_video)
    log.info("")
    log.info("  Other kept outputs:")
    # transcript*.json, matches.json, review.json, and censor_log.json are
    # always kept regardless of keep_intermediates (design doc §6) and are
    # safe to log unconditionally. So is this run's own log file --
    # logs/*.log is never deleted by any step, for the same reason
    # censor_log.json isn't (see utils.attach_file_logging). dialog.wav,
    # score_sfx.wav, dialog_censored.wav, audio_censored.wav, and
    # audio_encoded.mka are large intermediates that Steps 5/6/6b/7 each
    # delete by default once consumed (steps/mute.py, steps/recombine.py,
    # steps/encode.py, steps/mux.py) — only log them if they're actually
    # still on disk, i.e. keep_intermediates was set, rather than printing
    # a path that no longer exists.
    log.info("    %s", log_path)
    log.info("    %s", transcript_out)
    log.info("    %s", matches_out)
    if review_path:
        log.info("    %s", review_path)
    log.info("    %s", job_dir / "censor_log.json")
    kept_large_intermediates = [
        p for p in (dialog_out, score_sfx_out, dialog_censored_out, audio_censored_out, audio_encoded_out)
        if p.exists()
    ]
    for p in kept_large_intermediates:
        log.info("    %s", p)
    if len(kept_large_intermediates) < 5:
        log.info("  (large intermediate WAV/audio stems were deleted after use — pass --keep-tmp to retain them)")
    log.info("")
    log.info("  Steps done : %s", state.get("steps_completed", []))
    log.info("  Job store  : %s", job_dir)
    log.info("=" * 60)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
profanity-hush — pipeline orchestrator

v1 core pipeline: Steps 1a, 1b, 1c, 2, 2b, 3, 3b, 4b (flag + optional
review), 5 (mute), 6 (recombine), 6b (encode), 7 (mux). Step 7 is the
last step — a successful run produces the final censored video in
/output and marks the job 'complete'.

Step 4 (SRT alignment) is skipped for now — Step 4b's flag phase reads
transcript.json directly. Step 4b's flag phase always runs (both
interactive and unattended); its interactive review phase only runs when
interactive mode is active. Step 5 reads Step 4b's flagged matches
directly from matches.json — it does not re-scan the transcript itself
(see design doc §4).

── Step 2b, and why Step 3 no longer shares Step 2's own segmentation ───

steps/merge.py's audio-consolidation and transcript-consolidation used to
be one combined step ("3b_merge": per-segment dialog/score_sfx stems AND
per-segment transcripts, both merged together right after Step 3). They
are now two: Step 2b (merge_audio -- steps/merge.py's merge_audio())
consolidates Step 2's per-(Demucs-)segment dialog_NN.wav/score_sfx_NN.wav
into canonical dialog.wav/score_sfx.wav immediately after Step 2, and
Step 3b (merge_transcript -- steps/merge.py's merge_transcript(), keeping
the "3b_merge" job.json name the combined step always used) still
consolidates transcripts, unchanged, right after Step 3.

Moving the audio half earlier is what lets Step 3 (transcribe) compute
its OWN segmentation (alignment.segment_size_sec) against the canonical,
already-Demucs-separated dialog.wav, independent of whatever
audio.segment_size_sec Step 2's Demucs pass used -- see
steps/transcribe.py's own module docstring for why transcription
benefits from a different (typically larger, or zero at all) segment
size than Demucs's own memory-driven one. Once Step 2b succeeds,
dialog.wav/score_sfx.wav are the STABLE inputs every later step depends
on, and nothing downstream of Step 2b ever invalidates them -- which is
also what makes --redo-step 3_transcribe below simple: it never needs to
reach back past its own step, because Step 3 always starts from the same
unchanging dialog.wav, whether this is a first run or the tenth redo.

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

--redo-step STEP is a separate, narrower tool: it forces the named
step(s) (one of 3_transcribe, 4b_flag, 4b_review, 5_mute, 6_recombine,
6b_encode, 6c_transcript_srt, 7_mux) to redo on an existing job. Every
target except 3_transcribe involves no review.json at all.
3_transcribe is the one exception on both counts: naming it also
clears review.json (its "skip" overrides are word_index references into
the transcript being replaced -- see pipeline.py's own
_clear_transcript_redo_state()), and it's the one target that reaches
back further than its own numbered step's own output, by design -- see
"Step 2b" above for why that's still cheap (no Demucs re-run) rather
than something to avoid offering at all.
For testing a change to a step's own implementation (e.g. switching
Step 7 from ffmpeg to mkvmerge, or trying a different transcription
engine) against a job that's already sitting on disk, this is the
supported alternative to hand-editing steps_completed in job.json
directly -- editing job.json works as far as the steps themselves are
concerned (each one only ever checks its own entry; see steps/mute.py,
steps/recombine.py, steps/encode.py, steps/mux.py), but a syntax slip
while editing it by hand (e.g. a stray trailing comma) makes the whole
file invalid JSON, which utils.find_job_dir() can no longer match against
job_id -- silently turning "resume this job" into "start a fresh one,"
with hours of needless work the only symptom. --redo-step refuses
outright if no existing job is found, rather than falling through to a
fresh run, and never writes job.json by hand.

Naming an earlier step cascades to every step after it through 7_mux
(see _cascade_steps() below) -- redoing 5_mute alone also clears
6_recombine/6b_encode/6c_transcript_srt/7_mux, so a change always
propagates to the file actually delivered to /output rather than those
steps silently reusing stale files left over from before the change.
Redoing 3_transcribe cascades all the way through 3b_merge and
everything after it, for the same reason. --redo-step 7_mux alone clears
only 7_mux, since nothing in this pipeline is downstream of it.
--skip-index/--add-interval/--redo-review clear that exact same group
(5_mute/6_recombine/6b_encode/6c_transcript_srt/7_mux) -- see that
correction branch below. This used to exclude 6c_transcript_srt (its
output depended only on transcript.json, which a content correction
never touched), but no longer does: transcript_srt.hush (config.yaml)
now renders the authoritative SRT's text from censor_log.json too, which
is exactly what these corrections change -- see
steps/transcript_srt.py's docstring.
"""
import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import utils
from utils import (
    ALIGNMENT_ENGINE_NAMES,
    alignment_engines_summary,
    cfg_get,
    compute_job_id,
    find_job_dir,
    fmt_dir,
    fmt_duration,
    mark_job_failed,
    mark_job_interrupted,
    paths_banner,
    read_job,
    retention_summary,
    setup_logging,
    step_logger,
    unmark_step_done,
    validate_alignment_engines,
    write_job,
)
from steps.extract  import extract_raw, downmix_to_stereo
from steps.segment  import segment  as run_segment
from steps.separate import separate as run_separate
from steps.transcribe import transcribe as run_transcribe
from steps.merge      import merge_audio as run_merge_audio, merge_transcript as run_merge_transcript
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
from steps.mux        import _output_path
from steps.transcript_srt import export_srt as run_srt_export, validate_hush_config
from steps.matching   import resolve_word_list_path

# ── Fixed container paths ─────────────────────────────────────────────────────
OUTPUT_DIR  = Path("/output")
CONFIG_PATH = Path("/config/config.yaml")


# ── Job ID / directory ────────────────────────────────────────────────────────

def make_job_dir_name(video: Path, job_id: str) -> str:
    """
    Build a descriptive job directory name:
      YYYYMMDD_HHMMSS_<slug>_<hex8>
    """
    ts   = datetime.now(tz=utils.LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
    stem = Path(video.name).stem          # stop at last ".", drop extension(s)
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    if len(slug) > 32:
        # Trim at the last hyphen before the 32-char mark to avoid mid-word cuts
        slug = slug[:33].rsplit("-", 1)[0].rstrip("-")
    return f"{ts}_{slug}_{job_id[:8]}"


# ── --redo-step cascading ─────────────────────────────────────────────────────

# Canonical order of the steps --redo-step can target. Steps 1a, 1b, 1c,
# 2, and 2b are NOT included -- they're resumed as one atomic block (see
# the dispatch logic below) and are never valid --redo-step targets
# (enforced by argparse's choices= on --redo-step itself): 1a/1b/1c/2's
# own per-segment intermediates get cleaned up by Step 2b, so redoing one
# of them alone isn't safe once that's happened.
#
# 3_transcribe IS included, unlike those -- it's the one case where
# "redo this step" doesn't need any of Steps 1a-2b to run again at all
# (see this module's own docstring, "Step 2b" section): Step 3 always
# starts from the same stable, never-invalidated dialog.wav, so naming
# 3_transcribe here just needs the normal cascade (through 3b_merge and
# everything after it), plus the one extra bit of cleanup
# _clear_transcript_redo_state() handles before the cascade even runs
# (see the --redo-step handling below) -- clearing stale transcript
# output and review.json that neither steps/transcribe.py's nor
# steps/merge.py's own "already exists" resume checks would otherwise
# know to discard on their own.
STEP_ORDER = [
    "3_transcribe", "3b_merge",
    "4b_flag", "4b_review", "5_mute", "6_recombine", "6b_encode",
    "6c_transcript_srt", "7_mux",
]


def _cascade_steps(named_steps) -> "list[str]":
    """
    Expand the step names passed to --redo-step into the full set that
    must actually be cleared from steps_completed: each named step,
    plus everything after it in STEP_ORDER, in canonical order.

    This matters because every step's own resume-check only asks "is my
    own name in steps_completed?" -- it never checks whether the file
    it's about to reuse is newer than, or was built from, whatever it's
    being handed this run. Clearing only the literal name(s) passed on
    the command line (an earlier version of this did exactly that)
    meant redoing an early step alone -- e.g. --redo-step 5_mute to test
    a new padding value -- would regenerate dialog_censored.wav, then
    immediately hit steps/recombine.py's "already complete" branch,
    which returns the *old* audio_censored.wav without even looking at
    the freshly-passed dialog_censored_path argument. Cascading through
    every step after the named one mirrors exactly what the
    --skip-index/--add-interval/--redo-review path already does for
    5_mute/6_recombine/6b_encode/6c_transcript_srt/7_mux as a fixed
    group (see the `correcting` branch below) -- it just needs to work
    from whichever point --redo-step names. Naming --redo-step 7_mux
    alone still clears only 7_mux, since nothing in this pipeline is
    downstream of it.

    3_transcribe needs one thing beyond this cascade that no other
    target does: clearing steps_completed alone isn't enough to force a
    genuinely fresh transcript, because steps/transcribe.py's own
    per-segment resume check and steps/merge.py's own
    _merge_transcript_source() are BOTH existence-based, not
    steps_completed-based -- see _clear_transcript_redo_state() below,
    called from the --redo-step handling before this cascade runs.
    """
    to_clear = set()
    for step in named_steps:
        idx = STEP_ORDER.index(step)
        to_clear.update(STEP_ORDER[idx:])
    return [s for s in STEP_ORDER if s in to_clear]


def _clear_transcript_redo_state(job_dir: Path, state: dict, log) -> None:
    """
    Delete every file that must not survive a Step 3 (transcribe) redo --
    called once, from --redo-step 3_transcribe's own handling in main(),
    before _cascade_steps()'s unmark_step_done() calls run. Three
    different files have three different reasons for needing this:

      transcript.json / transcript_<engine>.json (steps/merge.py) --
        _merge_transcript_source() reuses whichever one already exists
        rather than re-deriving it from fresh per-segment sources (see
        that function's own docstring) -- without clearing these first,
        a fresh Step 3 pass with a new engine would silently produce a
        transcript.json Step 4b never actually sees, because
        merge_transcript() would just re-adopt the stale one.

      transcript_NN.json / transcript_<engine>_NN.json, and this job's
        own dialog_transcribe_NN.wav (steps/transcribe.py) -- the
        transcript files' per-segment resume check is existence-based,
        not steps_completed-based (there's no per-segment entry in that
        list at all), so a leftover per-segment file from a DIFFERENT
        engine's earlier attempt would be silently skipped rather than
        re-transcribed. dialog_transcribe_NN.wav is cleared for a
        related but distinct reason: if alignment.segment_size_sec also
        changed since the last run, a stale piece's own duration
        mismatch IS caught by steps/segment.py's split_into_segments()
        (via _validate_segment()) -- but only one piece at a time, each
        one costing a separate failed run to discover (confirmed
        directly while building this feature: going from one segment
        count to another took as many raise-then-retry cycles as there
        were stale leftover pieces). Globbing all of them away up front
        avoids that entirely, in exchange for a cheap, lossless re-split
        from dialog.wav the next time Step 3 runs -- see that function's
        own docstring.

      review.json (steps/review.py) -- its "skip" overrides are
        word_index references into the OLD transcript's own words[]
        array; a new engine's word count/segmentation makes those
        indices meaningless at best, and silently wrong (skipping some
        OTHER word that happens to land on the same index) at worst.
        review.json is dropped outright rather than partially preserved
        -- its "add" overrides technically carry their own start/end and
        COULD survive, but splitting that out adds real complexity for
        a case (re-adding a manual correction after switching engines)
        that's easy enough to just redo by hand afterward.

    matches.json is deliberately NOT included here: steps/review.py's
    flag() always overwrites it unconditionally once "4b_flag" is
    unmarked (no existence-bypass to defend against), so there's nothing
    to clear.

    Uses glob patterns, not a bounded index range, for every per-segment
    file: alignment.segment_size_sec may have changed since the run
    being redone, so the number of segments this redo produces can
    honestly differ from the number the previous attempt left behind,
    and every leftover piece -- not just ones the new count happens to
    overlap -- needs to go.
    """
    victims = [job_dir / "transcript.json"]
    victims += [job_dir / f"transcript_{name}.json" for name in ALIGNMENT_ENGINE_NAMES]
    victims += sorted(job_dir.glob("transcript_[0-9][0-9].json"))
    for name in ALIGNMENT_ENGINE_NAMES:
        victims += sorted(job_dir.glob(f"transcript_{name}_[0-9][0-9].json"))
    victims += sorted(job_dir.glob("dialog_transcribe_*.wav"))

    removed = [p.name for p in victims if p.exists()]
    for p in victims:
        p.unlink(missing_ok=True)

    review_path = job_dir / "review.json"
    if review_path.exists():
        review_path.unlink()
        removed.append(review_path.name)
        log.info(
            "  Removed review.json -- its 'skip' entries refer to word "
            "positions in the transcript being replaced and can't carry "
            "over to a new one. Re-run with --interactive or "
            "--redo-review afterward if you want to review the fresh "
            "transcript's matches."
        )

    if removed:
        log.info(
            "  Cleared %d stale transcript-related file(s) ahead of the "
            "redo: %s", len(removed), ", ".join(sorted(removed)),
        )


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
        choices=[
            "3_transcribe", "4b_flag", "4b_review", "5_mute", "6_recombine",
            "6b_encode", "6c_transcript_srt", "7_mux",
        ],
        help=(
            "Force this step to redo on an existing job, even though it's "
            "already marked complete -- for re-testing a change to the step "
            "itself (a new transcription engine, a new muxer, a tuned mute "
            "padding, a fixed encode command, a different "
            "transcript_srt.karaoke_color) against a job that already "
            "exists, without rerunning everything before it. Repeatable. "
            "Unlike --skip-index/--add-interval/--redo-review (which edit "
            "review.json to fix a *content* mistake and always redo Steps "
            "5, 6, 6b, 6c, and 7 together), this clears only the named "
            "step(s) plus everything after them through 7_mux -- e.g. "
            "naming 5_mute also clears 6_recombine/6b_encode/"
            "6c_transcript_srt/7_mux, so the change actually reaches the "
            "file delivered to /output instead of those steps silently "
            "reusing files left over from before the change. Naming 7_mux "
            "by itself clears only 7_mux, since nothing here is downstream "
            "of it. "
            "3_transcribe is the one target that reaches back past Step "
            "4b: naming it also clears 3b_merge, and forces a fresh Step 3 "
            "pass against a NEW per-segment split of the already-separated "
            "dialog.wav/score_sfx.wav, at whatever alignment."
            "segment_size_sec currently says -- Steps 1a/1b/1c/2/2b "
            "(including Step 2's own Demucs separation, normally this "
            "pipeline's single most expensive step) are skipped entirely, "
            "not redone, so iterating on alignment.engines config is "
            "cheap. It also drops review.json (its word_index entries "
            "can't survive a transcript swap -- see this module's own "
            "_clear_transcript_redo_state()). Requires dialog.wav/"
            "score_sfx.wav still on disk (kept by default -- "
            "output.keep_correction_artifacts); refuses with a clear error "
            "if either was cleaned up. Steps 1a/1b/1c/2/2b themselves are "
            "still not offered as --redo-step targets on their own -- "
            "they're resumed as one atomic block, and (short of the "
            "dialog.wav/score_sfx.wav path 3_transcribe uses) their own "
            "per-segment intermediates may already be deleted, so redoing "
            "one alone isn't generally safe. Requires a job that already "
            "exists for this exact input file (same path, same mtime) -- "
            "this is a targeted *redo*, not a way to start a fresh job, so "
            "it refuses outright rather than silently falling through to "
            "a full re-run if no existing job is found (e.g. because "
            "compute_job_id() landed on a different file, or because the "
            "existing job.json failed to parse -- see the warning "
            "utils.find_job_dir() logs in that case). Cannot be combined "
            "with --skip-index/--add-interval/--redo-review in the same "
            "invocation; run them separately."
        ),
    )
    args = parser.parse_args()

    # ── Config + logging ──────────────────────────────────────────────────────
    cfg = utils.load_config(args.config)
    utils.validate_config(cfg)
    validate_hush_config(cfg)
    validate_alignment_engines(cfg)
    log_level = cfg_get(cfg, "output", "log_level")
    setup_logging(log_level)
    log = step_logger("pipeline")

    jobs_dir = Path(cfg_get(cfg, "storage", "jobs_dir"))

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
        interactive = cfg_get(cfg, "interactive", "enabled")

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
    word_list_path = Path(cfg_get(cfg, "censoring", "word_list"))
    word_list_path = resolve_word_list_path(word_list_path, log)
    cfg.setdefault("censoring", {})["word_list"] = str(word_list_path)

    # ── Job store ─────────────────────────────────────────────────────────────
    job_id   = compute_job_id(video)
    job_dir  = find_job_dir(jobs_dir, job_id, log)
    resuming = job_dir is not None

    if not resuming:
        dir_name = make_job_dir_name(video, job_id)
        job_dir  = jobs_dir / dir_name
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
    for line in paths_banner(cfg).splitlines():
        log.info("%s", line)
    for line in alignment_engines_summary(cfg).splitlines():
        log.info("%s", line)
    for line in utils.censoring_summary(cfg).splitlines():
        log.info("%s", line)

    state = read_job(job_dir)
    if not resuming:
        now = time.time()
        started_at_local, _ = utils.fmt_wall_clock(now)
        # input_path is a *directory*, not the full path to the file --
        # input_filename (bare, below) already carries the filename. When
        # AC_INPUT_HOST_DIR reached the container (hush.sh/compose set it;
        # see paths_banner() above), this is the real, host-navigable
        # directory the video lives in (e.g. a NAS path) -- otherwise it
        # falls back to this container's own view of that same directory
        # (normally /input), which is still accurate, just not something
        # a person could actually navigate to outside the container.
        input_host_dir = cfg_get(cfg, "paths", "input_host_dir", default=None)
        input_dir_display = (
            fmt_dir(input_host_dir) if input_host_dir else fmt_dir(video.resolve().parent)
        )
        state = {
            "job_id":           job_id,
            "input_path":       input_dir_display,
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
        # Same idea, for a job that was Ctrl-C'd (see mark_job_interrupted())
        # rather than failed outright -- otherwise a resumed-and-now-running
        # job would sit there still claiming 'interrupted' from whichever
        # step got Ctrl-C'd last time.
        cleared_interruption = False
        for key in ("interruption", "interrupted_at", "interrupted_at_local"):
            if state.pop(key, None) is not None:
                cleared_interruption = True
        if cleared_interruption:
            log.info("  Cleared stale interruption record from a prior attempt.")
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
            except KeyboardInterrupt:
                cx_log.error("Interrupted (Ctrl-C) while applying corrections.")
                mark_job_interrupted(job_dir, "correct")
                sys.exit(130)
            except Exception as exc:
                cx_log.error("Applying corrections failed: %s", exc)
                sys.exit(1)

        if args.redo_review:
            unmark_step_done(job_dir, "4b_review")

        # Steps 5, 6, 6b, 6c, and 7 all depend, directly or indirectly, on
        # review.json -- invalidate every one of them so the normal step
        # machinery below redoes them with the corrected overrides, rather
        # than hitting their own "already complete" resume-checks. (6c
        # depends on it only through transcript_srt.hush, config.yaml --
        # the authoritative SRT's own redacted text, via censor_log.json
        # -- not through anything about which per-stage transcripts
        # exist; see steps/transcript_srt.py's docstring.) This is also
        # why output.keep_correction_artifacts (steps/mute.py,
        # steps/recombine.py) defaults to true: Step 5 needs dialog.wav to
        # still be on disk to actually redo, not just to be told it should.
        for step in ("5_mute", "6_recombine", "6b_encode", "6c_transcript_srt", "7_mux"):
            unmark_step_done(job_dir, step)

        cx_log.info("Correction recorded -- Steps 5, 6, 6b, 6c, and 7 will redo to apply it.")
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
        try:
            steps_to_clear = _cascade_steps(args.redo_steps)

            if "3_transcribe" in steps_to_clear:
                dialog_check    = job_dir / "dialog.wav"
                score_sfx_check = job_dir / "score_sfx.wav"
                if not dialog_check.exists() or not score_sfx_check.exists():
                    rs_log.error(
                        "--redo-step 3_transcribe needs dialog.wav and "
                        "score_sfx.wav (Step 2b's already-separated audio) "
                        "still on disk, and at least one is missing. "
                        "Either Step 2b (merge_audio) never completed for "
                        "this job, or output.keep_correction_artifacts and "
                        "output.keep_intermediates were both false and "
                        "they were cleaned up -- there's no way to redo "
                        "transcription without re-running Step 2's Demucs "
                        "separation in that case. Delete the job directory "
                        "and re-run from scratch."
                    )
                    sys.exit(1)
                _clear_transcript_redo_state(job_dir, state, rs_log)

            for step in steps_to_clear:
                unmark_step_done(job_dir, step)
        except KeyboardInterrupt:
            rs_log.error("Interrupted (Ctrl-C) while processing --redo-step.")
            mark_job_interrupted(job_dir, "redo-step")
            sys.exit(130)
        extra = [s for s in steps_to_clear if s not in args.redo_steps]
        if extra:
            rs_log.info(
                "Forcing redo of: %s  (cascaded from %s so the change "
                "actually reaches /output -- see --redo-step --help)",
                ", ".join(steps_to_clear), ", ".join(args.redo_steps),
            )
        else:
            rs_log.info("Forcing redo of: %s", ", ".join(steps_to_clear))
        done = read_job(job_dir).get("steps_completed", [])

    if "2b_merge_audio" in done or "3b_merge" in done:
        # Steps 1a-2b have nothing left to do: merge_audio() already
        # produced the canonical dialog.wav/score_sfx.wav, and — this is
        # the important part — its cleanup may have already deleted the
        # per-segment intermediates (dialog_NN.wav, score_sfx_NN.wav,
        # audio_stereo_NN.wav) that separate.py's own "already done"
        # resume path would otherwise try to reload. Calling
        # separate()/merge_audio() again here would hit exactly that:
        # each step's resume check trusts job.json and assumes its own
        # files are still on disk, which is no longer true once a
        # *later* step has cleaned them up. So skip straight to the
        # canonical files by fixed name — nothing past this point ever
        # needs Demucs's own per-segment intermediates again.
        #
        # "3b_merge" is checked too, not just "2b_merge_audio" -- a job
        # whose audio+transcript merge both completed under an earlier
        # version of this pipeline (before this split existed) only has
        # the old, combined marker; steps/merge.py's merge_audio() itself
        # backfills "2b_merge_audio" the first time it's asked about such
        # a job (see that function's own docstring), but this dispatch
        # check has to recognize the old marker as sufficient BEFORE that
        # backfill has had a chance to happen.
        log.info("Steps 1a-2b already complete — skipping straight to Step 3.")
        dialog_out     = job_dir / "dialog.wav"
        score_sfx_out  = job_dir / "score_sfx.wav"

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
            extract_raw(video, job_dir, ext_log)
        except KeyboardInterrupt:
            ext_log.error("Step 1a interrupted by user (Ctrl-C).")
            mark_job_interrupted(job_dir, "1a_extract_raw")
            sys.exit(130)
        except Exception as exc:
            ext_log.error("Step 1a failed: %s", exc)
            mark_job_failed(job_dir, "1a_extract_raw", exc)
            sys.exit(1)

        # ── Step 1b: downmix to stereo ─────────────────────────────────────────
        try:
            downmix_to_stereo(job_dir, ext_log)
        except KeyboardInterrupt:
            ext_log.error("Step 1b interrupted by user (Ctrl-C).")
            mark_job_interrupted(job_dir, "1b_downmix")
            sys.exit(130)
        except Exception as exc:
            ext_log.error("Step 1b failed: %s", exc)
            mark_job_failed(job_dir, "1b_downmix", exc)
            sys.exit(1)

        # ── Step 1c: segment ────────────────────────────────────────────────────
        seg_log = step_logger("segment")
        try:
            segments = run_segment(job_dir, cfg, seg_log)
        except KeyboardInterrupt:
            seg_log.error("Step 1c interrupted by user (Ctrl-C).")
            mark_job_interrupted(job_dir, "1c_segment")
            sys.exit(130)
        except Exception as exc:
            seg_log.error("Step 1c failed: %s", exc)
            mark_job_failed(job_dir, "1c_segment", exc)
            sys.exit(1)

        # ── Step 2: Demucs source separation ───────────────────────────────────
        sep_log = step_logger("separate")
        try:
            stem_pairs = run_separate(job_dir, segments, cfg, sep_log)
        except KeyboardInterrupt:
            sep_log.error("Step 2 interrupted by user (Ctrl-C).")
            mark_job_interrupted(job_dir, "2_separate")
            sys.exit(130)
        except Exception as exc:
            sep_log.error("Step 2 failed: %s", exc)
            mark_job_failed(job_dir, "2_separate", exc)
            sys.exit(1)

        # ── Step 2b: merge audio stems into canonical dialog.wav/score_sfx.wav ──
        mga_log = step_logger("merge")
        try:
            dialog_out, score_sfx_out = run_merge_audio(
                job_dir, segments, stem_pairs, cfg, mga_log,
            )
        except KeyboardInterrupt:
            mga_log.error("Step 2b interrupted by user (Ctrl-C).")
            mark_job_interrupted(job_dir, "2b_merge_audio")
            sys.exit(130)
        except Exception as exc:
            mga_log.error("Step 2b failed: %s", exc)
            mark_job_failed(job_dir, "2b_merge_audio", exc)
            sys.exit(1)

        n_segments = len(segments)

    # ── Step 3: transcription (own segmentation) + Step 3b: merge transcript ──
    # Called unconditionally, every run -- including one where both are
    # already fully done, in which case each returns near-instantly from
    # its own "already complete" check (job.json + a file-existence
    # check, no engine touched, no ffmpeg run). This is the same pattern
    # every step from 4b onward already follows in this file; Step 3/3b
    # can now join it too because -- unlike before Step 2b existed --
    # neither one depends on any per-(Demucs-)segment intermediate that a
    # LATER step might have cleaned up. dialog_out (Step 2b's own,
    # unconditionally stable output, set in either branch above) is the
    # only audio input Step 3 ever reads.
    tr_log = step_logger("transcribe")
    try:
        transcript_paths, transcribe_segments = run_transcribe(job_dir, dialog_out, cfg, tr_log)
    except KeyboardInterrupt:
        tr_log.error("Step 3 interrupted by user (Ctrl-C).")
        mark_job_interrupted(job_dir, "3_transcribe")
        sys.exit(130)
    except Exception as exc:
        tr_log.error("Step 3 failed: %s", exc)
        mark_job_failed(job_dir, "3_transcribe", exc)
        sys.exit(1)

    mg_log = step_logger("merge")
    try:
        transcript_out = run_merge_transcript(
            job_dir, transcribe_segments, transcript_paths, cfg, mg_log,
        )
    except KeyboardInterrupt:
        mg_log.error("Step 3b interrupted by user (Ctrl-C).")
        mark_job_interrupted(job_dir, "3b_merge")
        sys.exit(130)
    except Exception as exc:
        mg_log.error("Step 3b failed: %s", exc)
        mark_job_failed(job_dir, "3b_merge", exc)
        sys.exit(1)

    n_transcribe_segments = len(transcribe_segments)

    # ── Step 4: SRT alignment ─────────────────────────────────────────────────
    # Skipped for now — not yet implemented. Step 4b's flag phase reads
    # transcript.json directly. Swapping this for transcript_aligned.json
    # later needs no change to Step 4b itself (same schema, see
    # steps/review.py's docstring).

    # ── Step 4b: flag (always runs, both modes — see design doc §4) ──────────
    fl_log = step_logger("flag")
    try:
        matches_out = run_flag(job_dir, transcript_out, cfg, fl_log)
    except KeyboardInterrupt:
        fl_log.error("Step 4b (flag) interrupted by user (Ctrl-C).")
        mark_job_interrupted(job_dir, "4b_flag")
        sys.exit(130)
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
        except KeyboardInterrupt:
            rv_log.error("Step 4b (review) interrupted by user (Ctrl-C).")
            mark_job_interrupted(job_dir, "4b_review")
            sys.exit(130)
        except Exception as exc:
            rv_log.error("Step 4b (review) failed: %s", exc)
            mark_job_failed(job_dir, "4b_review", exc)
            sys.exit(1)

    # ── Step 5: mute dialog stem ───────────────────────────────────────────────
    # Reads matches_out (+ review_path, if it ran) directly — no re-scan.
    mu_log = step_logger("mute")
    try:
        dialog_censored_out = run_mute(job_dir, dialog_out, cfg, mu_log)
    except KeyboardInterrupt:
        mu_log.error("Step 5 interrupted by user (Ctrl-C).")
        mark_job_interrupted(job_dir, "5_mute")
        sys.exit(130)
    except Exception as exc:
        mu_log.error("Step 5 failed: %s", exc)
        mark_job_failed(job_dir, "5_mute", exc)
        sys.exit(1)

    # ── Step 6: recombine dialog (censored) + score/SFX stems ─────────────────
    rc_log = step_logger("recombine")
    try:
        audio_censored_out = run_recombine(job_dir, dialog_censored_out, score_sfx_out, cfg, rc_log)
    except KeyboardInterrupt:
        rc_log.error("Step 6 interrupted by user (Ctrl-C).")
        mark_job_interrupted(job_dir, "6_recombine")
        sys.exit(130)
    except Exception as exc:
        rc_log.error("Step 6 failed: %s", exc)
        mark_job_failed(job_dir, "6_recombine", exc)
        sys.exit(1)

    # ── Step 6b: encode censored audio to match original codec ────────────────
    en_log = step_logger("encode")
    try:
        audio_encoded_out = run_encode(job_dir, video, audio_censored_out, cfg, en_log)
    except KeyboardInterrupt:
        en_log.error("Step 6b interrupted by user (Ctrl-C).")
        mark_job_interrupted(job_dir, "6b_encode")
        sys.exit(130)
    except Exception as exc:
        en_log.error("Step 6b failed: %s", exc)
        mark_job_failed(job_dir, "6b_encode", exc)
        sys.exit(1)

    # ── Step 6c: export recognized-word transcript as SRT ──────────────────────
    out_format = str(cfg_get(cfg, "output", "format")).lower()
    output_video_path = _output_path(video, OUTPUT_DIR, cfg, out_format)

    sr_log = step_logger("srt")
    srt_sources: list = []
    try:
        srt_sources = run_srt_export(job_dir, output_video_path, cfg, sr_log)
    except KeyboardInterrupt:
        sr_log.error("Step 6c interrupted by user (Ctrl-C).")
        mark_job_interrupted(job_dir, "6c_transcript_srt")
        sys.exit(130)
    except Exception as exc:
        sr_log.warning(
            "Step 6c failed (%s) -- continuing without any transcript "
            "subtitle tracks this run; every other output is unaffected. "
            "Re-run (or --redo-step 6c_transcript_srt) after investigating "
            "to try again without repeating Steps 1a-6b.",
            exc,
        )

    if (
        srt_sources
        and "6c_transcript_srt" not in done
        and "7_mux" in done
    ):
        sr_log.info(
            "Step 6c produced %d transcript SRT(s) for the first time on a "
            "job whose Step 7 (mux) already completed under a version of "
            "this pipeline from before Step 6c existed -- forcing Step 7 "
            "to redo once so the new subtitle track(s) actually reach the "
            "output video.",
            len(srt_sources),
        )
        unmark_step_done(job_dir, "7_mux")

    # ── Step 7: mux encoded audio into the original video ──────────────────────
    mx_log = step_logger("mux")
    try:
        output_video = run_mux(
            job_dir, video, audio_encoded_out, OUTPUT_DIR, cfg, mx_log,
            subtitle_sources=srt_sources,
        )
    except KeyboardInterrupt:
        mx_log.error("Step 7 interrupted by user (Ctrl-C).")
        mark_job_interrupted(job_dir, "7_mux")
        sys.exit(130)
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
    transcribe_st = state.get("transcription", {})

    steps_label = "1a / 1b / 1c / 2 / 2b / 3 / 3b / 4b (flag)"
    if review_path:
        steps_label += " + 4b (review)"
    steps_label += " / 5 (mute) / 6 (recombine) / 6b (encode) / 6c (srt) / 7 (mux)"

    log.info("=" * 60)
    log.info("Pipeline complete!  Steps %s.", steps_label)
    if correcting:
        log.info("  (Steps 5, 6, 6b, and 7 were redone to apply a correction; see job.json's history for prior runs.)")
    if args.redo_steps and "3_transcribe" in args.redo_steps:
        log.info(
            "  (Step 3's transcription was redone from already-separated "
            "audio -- Steps 1a-2b were skipped, no Demucs re-run.)"
        )
    log.info("")
    if "started_at" in state:
        started_local, started_utc = utils.fmt_wall_clock(utils.parse_iso_to_epoch(state["started_at"]))
        log.info("  Started          : %s  (%s)", started_local, started_utc)
    finished_local, finished_utc = utils.fmt_wall_clock(completed_epoch)
    log.info("  Finished         : %s  (%s)", finished_local, finished_utc)
    log.info("  Input duration   : %s  (%.1f s)", fmt_duration(duration), duration)
    log.info("  Segments         : %d  (Step 2's own, for Demucs)", n_segments)
    log.info(
        "  Transcription    : %d segment(s)  (Step 3's own, independent -- "
        "see alignment.segment_size_sec)", n_transcribe_segments,
    )
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
    mfa_fallback_segments = transcribe_st.get("mfa_fallback_segments", 0)
    if mfa_fallback_segments:
        log.info(
            "  MFA fallback     : %d segment(s) had a whole-segment MFA "
            "failure and fell back to WhisperX's own timing there -- see "
            "the per-segment WARN lines above for which, and why.",
            mfa_fallback_segments,
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
    srt_st = state.get("transcript_srt", {})
    srt_keys_reported = sorted(
        srt_st.keys(),
        key=lambda k: (
            k == "final",
            ALIGNMENT_ENGINE_NAMES.index(k) if k in ALIGNMENT_ENGINE_NAMES else len(ALIGNMENT_ENGINE_NAMES),
        ),
    )
    if srt_keys_reported:
        for key in srt_keys_reported:
            b = srt_st[key]
            log.info(
                "  Transcript SRT (%s): %d word(s) in %d group(s), %d cue(s)  (karaoke=%s)",
                b.get("label", key), b.get("words", 0), b.get("groups", 0), b.get("cues", 0),
                b.get("karaoke"),
            )
    elif "6c_transcript_srt" not in state.get("steps_completed", []):
        log.info("  Transcript SRT   : failed this run -- see the [srt] WARN line above; every other output is unaffected.")
    elif bool(cfg_get(cfg, "transcript_srt", "enabled")):
        log.info("  Transcript SRT   : none (no configured engine or the authoritative transcript had a word with usable alignment timing)")
    else:
        log.info("  Transcript SRT   : disabled (transcript_srt.enabled: false)")
    log.info("")
    log.info("  Final output     : %s", output_video)
    log.info("")
    log.info("  Other kept outputs:")
    log.info("    %s", log_path)
    log.info("    %s", transcript_out)
    for source in srt_sources:
        log.info("    %s", source.job_dir_path)
        if source.sidecar_path is not None:
            log.info("    %s", source.sidecar_path)
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

    # ── Compact, machine-readable result line -- stdout, not stderr ────────────
    notable: list[str] = []
    if mfa_fallback_segments:
        notable.append(f"MFA fallback: {mfa_fallback_segments} segment(s)")
    if encode_st.get("fallback_reason"):
        notable.append(f"audio fallback: {encode_st['fallback_reason']}")
    warning_note = utils.warning_summary()
    if warning_note:
        notable.append(warning_note)
    print("AC_RESULT ok" if not notable else f"AC_RESULT warnings :: {' | '.join(notable)}", flush=True)


if __name__ == "__main__":
    main()

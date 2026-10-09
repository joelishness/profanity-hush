#!/usr/bin/env bash
# hush.sh — profanity-hush host-side entry point
# =============================================================================
# Resolves paths, creates required directories, and launches the profanity-hush
# Docker container with the correct volume mounts.
#
# Log timestamps automatically match this machine's local clock (the host's
# current UTC offset is detected via `date` and forwarded into the
# container); they fall back to clearly-labelled UTC if that detection ever
# fails. See AC_TZ_OFFSET / AC_TZ_NAME below if running the container some
# other way (e.g. `docker compose`) and you want the same behaviour.
#
# job.json's input_path/mux.output_path likewise report the real,
# host-navigable directories resolved below (INPUT_DIR/OUTPUT_DIR),
# forwarded as AC_INPUT_HOST_DIR/AC_OUTPUT_HOST_DIR; they fall back to
# this container's own /input //output mount points if that forwarding
# ever fails to reach it some other way.
#
# Usage:
#   hush.sh [OPTIONS] <input_video> [subtitle_file]
#   hush.sh --batch [--recursive] [OPTIONS] <input_dir>
#
# Options:
#   -o, --output DIR      Output directory (default: same directory as input)
#   -c, --config DIR      Config directory (default: ~/.config/profanity-hush)
#       --cache  DIR      Model cache directory (default: ~/.cache/profanity-hush)
#       --jobs   DIR      Job history directory (default: ~/.local/share/profanity-hush/jobs)
#       --interactive     Pause for review of flagged words before muting
#       --no-interactive  Force unattended mode (overrides config.yaml)
#       --keep-tmp        Retain large intermediate WAV stems after the run
#   -b, --batch           Process every video file directly inside <input_dir>,
#                         one after another. For movies (and anything under
#                         naming_style: suffix), skips any file that already
#                         has a censored output in place (judged by output
#                         filename -- not local job history alone, since a
#                         file may have been censored on a different
#                         machine). TV episodes are always reprocessed --
#                         see -r below for why an existence check isn't
#                         done for these -- so an already-done episode's
#                         output just gets overwritten with an equivalent
#                         result, the same as re-running a single movie
#                         file whose job history was deleted. Ctrl-C stops
#                         the batch after the file in progress finishes its
#                         current step; re-running the same command resumes
#                         -- already-done movies are skipped automatically,
#                         and a part-finished file (movie or TV) resumes
#                         from its last completed step (see pipeline.py's
#                         existing job resume logic). Cannot combine with
#                         --skip-index/--add-interval/--redo-review/
#                         --redo-step (those target one already-completed
#                         job, not a directory). --interactive works, but
#                         pauses for review on every file in the queue, one
#                         after another. Writes a high-level overview to
#                         <jobs_dir>/batch-logs/ -- plan-phase results, each
#                         file's start/end/duration, and a short note if
#                         pipeline.py flagged anything (e.g. an MFA alignment
#                         falling back to whisperx.align() for one segment).
#                         Deliberately NOT each file's own full step-by-step
#                         transcript -- that already lives in that job's own
#                         job_dir/logs/*.log, so this stays a quick, scannable
#                         summary across 100+ files instead of growing as long
#                         as reading through every job individually.
#                         AC_LOG_LEVEL=debug also names the specific files the
#                         planning pass skipped or queued, not just counts.
#   -r, --recursive       With --batch, also descend into subdirectories (e.g.
#                         Season 01/, Season 02/, Specials/). Off by default --
#                         a bare --batch only processes files directly inside
#                         <input_dir>. Movie output mirrors each file's
#                         subdirectory under --output (or <input_dir> itself,
#                         by default). TV episode output does NOT mirror in
#                         place -- Plex has no per-episode edition concept
#                         (see https://support.plex.tv/articles/multiple-
#                         editions-tv-shows/); instead the whole *show*
#                         gets redirected to a sibling directory, computed
#                         from each file's own real path in bash:
#                           Psych (2006)/Season 02/... - s02e01 - ....mkv
#                           -> Psych (2006) {edition-Hushed}/Season 02/... - s02e01 - ....mkv
#                         (untagged filename, season/specials structure
#                         preserved underneath). Because that target is a
#                         sibling of the show folder rather than a
#                         descendant of wherever --batch was pointed, it
#                         can't be reliably existence-checked from the
#                         planning pass -- see -b above for what that means
#                         in practice.
#       --skip-index N    Correction: un-mute the flagged match at this word_index
#                         (see censor_log.json). Repeatable. Re-runs Steps 5-7 only.
#       --add-interval TEXT START END
#                         Correction: add a manual mute interval -- START/END
#                         as raw seconds (1203.14) or H:MM:SS.mmm (0:20:03.140).
#                         Repeatable. Re-runs Steps 5-7 only.
#       --redo-review     Correction: re-enter interactive review from scratch
#                         on an already-completed job (implies --interactive).
#       --redo-step STEP  Force this step to redo on an existing job, with no
#                         review.json involved (e.g. --redo-step 7_mux after
#                         changing the muxer). Repeatable. One of: 4b_flag,
#                         4b_review, 5_mute, 6_recombine, 6b_encode,
#                         6c_transcript_srt, 7_mux.
#                         Cannot combine with --skip-index/--add-interval/
#                         --redo-review. Requires the job to already exist --
#                         refuses rather than starting a fresh one if not.
#       --redo-audio      Re-run the audio chain on an existing job from its always-kept
#                         original audio: Steps 1b-2b (downmix incl. the current
#                         audio_processing.center_boost_db, Demucs), then 5-7, re-applying the
#                         already-known hushes (matches.json + review.json). Does NOT redo
#                         transcription, flagging, review or subtitles. Slow (Demucs: hours); for
#                         audio_processing night_mode/loudness changes use --redo-step 6_recombine
#                         (minutes). If interrupted, re-run without the flag to resume.
#                         Cannot combine with --skip-index/--add-interval/--redo-review/
#                         --redo-step/--batch.
#       --dry-run         Print the docker command without executing it
#   -h, --help            Show this help message
#
# Examples:
#   hush.sh movie.mkv
#   hush.sh --interactive movie.mkv movie.srt
#   hush.sh -o ~/censored/ movie.mkv
#   hush.sh --dry-run movie.mkv movie.srt
#   hush.sh --skip-index 4856 movie.mkv                     # un-mute a false positive
#   hush.sh --add-interval "missed word" 1203.1 1203.5 movie.mkv
#   hush.sh --add-interval "missed word" 0:20:03.1 0:20:03.5 movie.mkv  # same, H:MM:SS.mmm
#   hush.sh --redo-step 7_mux movie.mkv                      # re-test a muxer change only
#   hush.sh --batch "Psych (2006)/Season 02"                 # one season, top-level files only
#   hush.sh --batch --recursive "Psych (2006)"               # whole show, every season + Specials
# =============================================================================
set -euo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────────

SCRIPT_NAME="$(basename "$0")"

usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [OPTIONS] <input_video> [subtitle_file]
       ${SCRIPT_NAME} --batch [--recursive] [OPTIONS] <input_dir>

Options:
  -o, --output DIR      Output directory (default: same directory as input)
  -c, --config DIR      Config directory (default: ~/.config/profanity-hush)
      --cache  DIR      Model cache directory (default: ~/.cache/profanity-hush)
      --jobs   DIR      Job history directory (default: ~/.local/share/profanity-hush/jobs)
      --interactive     Pause for review of flagged words before muting
      --no-interactive  Force unattended mode (overrides config.yaml)
      --keep-tmp        Retain large intermediate WAV stems after the run
  -b, --batch           Process every video file directly inside <input_dir>,
                        one after another. For movies (and anything under
                        naming_style: suffix), skips any file that already
                        has a censored output in place (judged by output
                        filename -- not local job history alone, since a
                        file may have been censored on a different
                        machine). TV episodes are always reprocessed -- see
                        -r below for why -- so an already-done episode's
                        output just gets overwritten with an equivalent
                        result. Ctrl-C stops the batch after the file in
                        progress finishes its current step; re-running the
                        same command resumes -- already-done movies are
                        skipped automatically, and a part-finished file
                        (movie or TV) resumes from its last completed step.
                        Cannot combine with --skip-index/--add-interval/
                        --redo-review/--redo-step (those target one already-
                        completed job, not a directory). --interactive works,
                        but pauses for review on every file in the queue, one
                        after another. Writes a high-level overview to
                        <jobs_dir>/batch-logs/ -- plan-phase results, each
                        file's start/end/duration, and a short note if
                        pipeline.py flagged anything notable (e.g. an MFA
                        alignment falling back to whisperx.align()) -- not
                        each file's own full transcript (already in that
                        job's own job_dir/logs/*.log), so this stays a quick
                        summary across 100+ files. AC_LOG_LEVEL=debug also
                        names the specific files the planning pass skipped
                        or queued.
  -r, --recursive       With --batch, also descend into subdirectories (e.g.
                        Season 01/, Season 02/, Specials/). Off by default --
                        a bare --batch only processes files directly inside
                        <input_dir>. Movie output mirrors each file's
                        subdirectory under --output (or <input_dir> itself,
                        by default). TV episode output does NOT mirror in
                        place -- Plex has no per-episode edition concept, so
                        the whole show gets redirected to a sibling
                        directory instead (computed from each file's own
                        real path, in bash):
                          Psych (2006)/Season 02/... - s02e01 - ....mkv
                          -> Psych (2006) {edition-Hushed}/Season 02/... - s02e01 - ....mkv
                        See https://support.plex.tv/articles/multiple-
                        editions-tv-shows/. That target is a sibling of the
                        show folder, not a descendant of wherever --batch
                        was pointed, which is why it can't be reliably
                        existence-checked during planning -- see -b above.
      --skip-index N    Correction: un-mute the flagged match at this word_index
                        (see censor_log.json). Repeatable. Re-runs Steps 5-7 only.
      --add-interval TEXT START END
                        Correction: add a manual mute interval -- START/END
                        as raw seconds (1203.14) or H:MM:SS.mmm (0:20:03.140).
                        Repeatable. Re-runs Steps 5-7 only.
      --redo-review     Correction: re-enter interactive review from scratch
                        on an already-completed job (implies --interactive).
      --redo-step STEP  Force this step to redo on an existing job, with no
                        review.json involved (e.g. --redo-step 7_mux after
                        changing the muxer). Repeatable. One of: 4b_flag,
                        4b_review, 5_mute, 6_recombine, 6b_encode,
                        6c_transcript_srt, 7_mux.
                        Cannot combine with --skip-index/--add-interval/
                        --redo-review. Requires the job to already exist --
                        refuses rather than starting a fresh one if not.
      --redo-audio      Re-run the audio chain on an existing job from its always-kept
                        original audio: Steps 1b-2b (downmix incl. the current
                        audio_processing.center_boost_db, Demucs), then 5-7, re-applying the
                        already-known hushes (matches.json + review.json). Does NOT redo
                        transcription, flagging, review or subtitles. Slow (Demucs: hours); for
                        audio_processing night_mode/loudness changes use --redo-step 6_recombine
                        (minutes). If interrupted, re-run without the flag to resume.
                        Cannot combine with --skip-index/--add-interval/--redo-review/
                        --redo-step/--batch.
      --dry-run         Print the docker command without executing it
  -h, --help            Show this help message

Examples:
  ${SCRIPT_NAME} movie.mkv
  ${SCRIPT_NAME} --interactive movie.mkv movie.srt
  ${SCRIPT_NAME} -o ~/censored/ movie.mkv
  ${SCRIPT_NAME} --dry-run --interactive movie.mkv movie.srt
  ${SCRIPT_NAME} --skip-index 4856 movie.mkv
  ${SCRIPT_NAME} --add-interval "missed word" 1203.1 1203.5 movie.mkv
  ${SCRIPT_NAME} --add-interval "missed word" 0:20:03.1 0:20:03.5 movie.mkv  # same, H:MM:SS.mmm
  ${SCRIPT_NAME} --redo-step 7_mux movie.mkv                # re-test a muxer change only
  ${SCRIPT_NAME} --batch "Psych (2006)/Season 02"           # one season, top-level files only
  ${SCRIPT_NAME} --batch --recursive "Psych (2006)"         # whole show, every season + Specials
EOF
}

die() {
    echo "${SCRIPT_NAME}: error: $*" >&2
    exit 1
}

# Resolve a path to absolute form; the path does not need to exist yet
# (unlike realpath --canonicalize-existing).
abs_path() {
    local p="$1"
    # Expand leading ~ manually (bash doesn't expand it inside variable assignment)
    p="${p/#\~/$HOME}"
    if [[ "$p" != /* ]]; then
        p="$(pwd)/${p}"
    fi
    echo "$p"
}

# realpath equivalent that works even when the target doesn't exist yet.
#
# The naive approach — cd into dirname, then pwd — silently returns an empty
# string when dirname doesn't exist yet, collapsing the whole path to just
# /basename (e.g. /jobs instead of ~/.local/share/profanity-hush/jobs).
# Instead we walk up the tree to the nearest existing ancestor, canonicalise
# that with cd/pwd, then reattach the non-existent trailing components.
resolve_path() {
    local p trailing=()
    p="$(abs_path "$1")"

    # Walk up until we find an existing directory (/ is always a backstop)
    while [[ ! -d "$p" ]]; do
        trailing=("$(basename "$p")" ${trailing[@]+"${trailing[@]}"})
        local parent
        parent="$(dirname "$p")"
        [[ "$parent" == "$p" ]] && break   # reached filesystem root; stop
        p="$parent"
    done

    # Canonicalise the existing ancestor (resolves symlinks, removes . and ..)
    [[ -d "$p" ]] && p="$(cd "$p" && pwd)"

    # Reattach the non-existent trailing components
    local part
    for part in ${trailing[@]+"${trailing[@]}"}; do
        p="${p%/}/$part"
    done

    echo "$p"
}

# Turns an arbitrary directory name into a short, filesystem-friendly slug
# for the batch log filename -- mirrors pipeline.py's make_job_dir_name()
# slug closely enough to read as "the same kind of name" next to job
# directories, without needing bash to match it byte-for-byte.
#   "Season 02"     -> "season-02"
#   "Psych (2006)"  -> "psych-2006"
slugify() {
    local s="${1,,}"                       # lowercase
    s="$(echo "$s" | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
    echo "${s:0:32}"
}

# Only used by --batch (see below) -- emits a line shaped like utils.py's
# own _StepFormatter ("2026-06-25 16:25:41 -0700 [INFO ] [hush.sh  ] ...")
# so hush.sh's own batch-loop lines read as part of the same continuous
# log as pipeline.py's own output, and writes that same line to both the
# terminal and $BATCH_LOG_FILE directly -- a high-level overview only,
# deliberately: this is the only thing that goes into the batch log,
# never a per-file docker run's own stderr (see "Batch log" below for
# why). DEBUG lines are gated on AC_LOG_LEVEL, same contract as the
# container's own logging -- set once, respected on both sides.
BATCH_LOG_LEVEL="${AC_LOG_LEVEL:-info}"
batch_log() {
    local level="$1"; shift
    if [[ "$level" == "DEBUG" ]]; then
        [[ "${BATCH_LOG_LEVEL,,}" == "debug" ]] || return 0
    fi
    local line
    line="$(printf '%s [%-5s] [%-9s] %s' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$level" "hush.sh" "$*")"
    echo "$line" >&2
    [[ -n "${BATCH_LOG_FILE:-}" ]] && echo "$line" >> "$BATCH_LOG_FILE"
}

# HH:MM:SS from a whole-seconds count -- matches utils.fmt_duration()'s own
# format closely enough for a person reading both in the same log, without
# needing to shell out to Python just to format an integer.
fmt_hms() {
    local total="$1"
    printf '%02d:%02d:%02d' $((total/3600)) $((total%3600/60)) $((total%60))
}

# Mirrors batch_plan.py's _is_tv_episode() -- keep the two in sync if
# this heuristic ever changes. Duplicated rather than shared because
# each side genuinely needs its own copy: this one decides where to
# mount /output for the real per-file run (see redirect_for_tv_edition
# below), which depends on real host directory names nothing running
# inside a container ever sees; batch_plan.py's copy decides whether to
# skip its own existence check (see that file's module docstring).
#
# $1 = file basename, $2 = its immediate parent directory's basename
is_tv_episode() {
    local base_lc="${1,,}" parent_lc="${2,,}"

    # Primary signal: an sNNeNN marker in the filename.
    [[ "$base_lc" =~ s[0-9]{1,2}e[0-9]{1,3} ]] && return 0

    # Secondary signal: Plex's date-based episode naming (some shows use
    # "2011-11-15" instead of "s02e01") -- but only when corroborated by
    # sitting in a season-style folder. A bare date pattern alone is too
    # easy to collide with a movie whose own title happens to contain
    # one; requiring the season-folder context is what disambiguates it.
    if [[ "$base_lc" =~ [0-9]{4}-[0-9]{2}-[0-9]{2} || "$base_lc" =~ [0-9]{2}-[0-9]{2}-[0-9]{4} ]]; then
        is_season_dir_name "$parent_lc" && return 0
    fi

    return 1
}

# $1 = a directory basename, already lowercased by the caller
is_season_dir_name() {
    [[ "$1" =~ ^season[[:space:]]*[0-9]{1,2}$ || "$1" == "specials" ]]
}

# Redirects a mirrored output directory to Plex's TV-editions sibling
# show directory -- "Show (Year)" -> "Show (Year) {edition-Name}",
# season/specials subpath preserved underneath -- per
# https://support.plex.tv/articles/multiple-editions-tv-shows/. Only
# ever called when naming_style is plex_edition and is_tv_episode
# matched. Pure path-string arithmetic on an already-resolved, real host
# path -- no filesystem access needed, so this works identically
# whether or not anything actually exists yet at the target (nothing
# here checks -- see batch_plan.py's module docstring for why TV
# episodes are never existence-checked at all, unlike movies).
#   $1 = the mirrored output directory hush.sh would otherwise use as-is
#   $2 = configured edition_name
redirect_for_tv_edition() {
    local mirrored_dir="$1" edition_name="$2"
    local leaf leaf_lc show_root

    leaf="$(basename "$mirrored_dir")"
    leaf_lc="${leaf,,}"

    if is_season_dir_name "$leaf_lc"; then
        # mirrored_dir is itself a season/specials folder -- the show
        # root is one level up; redirect that, then re-append this
        # season leaf underneath it.
        show_root="$(dirname "$mirrored_dir")"
        echo "$(dirname "$show_root")/$(basename "$show_root") {edition-${edition_name}}/${leaf}"
    else
        # No season subfolder -- mirrored_dir itself is the show root.
        echo "$(dirname "$mirrored_dir")/${leaf} {edition-${edition_name}}"
    fi
}

# ── Argument defaults ─────────────────────────────────────────────────────────

OUTPUT_DIR=""
CONFIG_DIR="${HOME}/.config/profanity-hush"
CACHE_DIR="${HOME}/.cache/profanity-hush"
JOBS_DIR="${HOME}/.local/share/profanity-hush/jobs"
INTERACTIVE=""
NO_INTERACTIVE=""
KEEP_TMP=""
DRY_RUN=""
INPUT_VIDEO=""
SUBTITLE_FILE=""
IMAGE_NAME="${HUSH_IMAGE:-profanity-hush}"
SKIP_INDICES=()
ADD_INTERVALS=()   # flattened in groups of 3: TEXT START END, TEXT START END, ...
REDO_REVIEW=""
REDO_STEPS=()
REDO_AUDIO=""
BATCH=""
RECURSIVE=""
NAMING_STYLE=""
EDITION_NAME=""
OUT_FORMAT=""

# Saved before the parsing loop below shifts through it -- used only for the
# batch log header (see --batch), so the log file is self-contained: what
# ran, not just what happened. Not otherwise re-parsed or re-used.
ORIGINAL_ARGS=("$@")

# ── Argument parsing ──────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)
            [[ -n "${2:-}" ]] || die "--output requires a directory argument"
            OUTPUT_DIR="$2"; shift 2 ;;
        -c|--config)
            [[ -n "${2:-}" ]] || die "--config requires a directory argument"
            CONFIG_DIR="$2"; shift 2 ;;
        --cache)
            [[ -n "${2:-}" ]] || die "--cache requires a directory argument"
            CACHE_DIR="$2"; shift 2 ;;
        --jobs)
            [[ -n "${2:-}" ]] || die "--jobs requires a directory argument"
            JOBS_DIR="$2"; shift 2 ;;
        --interactive)
            INTERACTIVE=1; shift ;;
        --no-interactive)
            NO_INTERACTIVE=1; shift ;;
        --keep-tmp)
            KEEP_TMP=1; shift ;;
        --skip-index)
            [[ -n "${2:-}" ]] || die "--skip-index requires a word_index argument"
            SKIP_INDICES+=("$2"); shift 2 ;;
        --add-interval)
            [[ -n "${2:-}" && -n "${3:-}" && -n "${4:-}" ]] \
                || die "--add-interval requires three arguments: TEXT START END"
            ADD_INTERVALS+=("$2" "$3" "$4"); shift 4 ;;
        --redo-review)
            REDO_REVIEW=1; shift ;;
        --redo-step)
            [[ -n "${2:-}" ]] || die "--redo-step requires a step name argument"
            REDO_STEPS+=("$2"); shift 2 ;;
        --redo-audio)
            REDO_AUDIO=1; shift ;;
        -b|--batch)
            BATCH=1; shift ;;
        -r|--recursive)
            RECURSIVE=1; shift ;;
        --dry-run)
            DRY_RUN=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        --)
            shift; break ;;
        -*)
            die "unknown option: $1 (try --help)" ;;
        *)
            # Collect positional arguments
            if [[ -z "$INPUT_VIDEO" ]]; then
                INPUT_VIDEO="$1"
            elif [[ -z "$SUBTITLE_FILE" ]]; then
                SUBTITLE_FILE="$1"
            else
                die "unexpected argument: $1 (only one video and one subtitle file accepted)"
            fi
            shift ;;
    esac
done

# ── Validate required arguments ───────────────────────────────────────────────

[[ -n "$INPUT_VIDEO" ]] || { usage >&2; echo; die "input_video is required"; }

if [[ -n "$BATCH" ]]; then
    [[ -d "$INPUT_VIDEO" ]] || die "--batch requires a directory: ${INPUT_VIDEO}"
    [[ -z "$SUBTITLE_FILE" ]] \
        || die "a trailing subtitle_file argument isn't supported with --batch (${SUBTITLE_FILE}) -- SRT cross-reference isn't implemented yet regardless (Phase 3)."
else
    [[ -f "$INPUT_VIDEO" ]] || die "input file not found: ${INPUT_VIDEO}"
fi

# ── Resolve all paths to absolute (Docker requires absolute paths for -v) ─────

if [[ -n "$BATCH" ]]; then
    # The directory itself is what gets mounted at /input -- batch_plan.py
    # (and, per file, the same per-file mount the single-file path already
    # uses) walks it from there. No VIDEO_BASENAME/dirname split needed --
    # there's no single file yet, that's the whole point of planning first.
    INPUT_DIR="$(resolve_path "$INPUT_VIDEO")"
else
    INPUT_VIDEO_ABS="$(resolve_path "$INPUT_VIDEO")"
    INPUT_DIR="$(dirname "$INPUT_VIDEO_ABS")"
    VIDEO_BASENAME="$(basename "$INPUT_VIDEO_ABS")"
fi

# Output defaults to the same directory as the input
OUTPUT_DIR="${OUTPUT_DIR:-$INPUT_DIR}"
OUTPUT_DIR="$(resolve_path "$OUTPUT_DIR")"
CONFIG_DIR="$(resolve_path "$CONFIG_DIR")"
CACHE_DIR="$(resolve_path "$CACHE_DIR")"
JOBS_DIR="$(resolve_path "$JOBS_DIR")"

# Handle optional subtitle file
SRT_BASENAME=""
if [[ -n "$SUBTITLE_FILE" ]]; then
    [[ -f "$SUBTITLE_FILE" ]] || die "subtitle file not found: ${SUBTITLE_FILE}"
    SRT_ABS="$(resolve_path "$SUBTITLE_FILE")"
    SRT_DIR="$(dirname "$SRT_ABS")"
    SRT_BASENAME="$(basename "$SRT_ABS")"
    # Both files must be in the same directory so a single /input mount covers both
    [[ "$SRT_DIR" == "$INPUT_DIR" ]] \
        || die "subtitle file must be in the same directory as the input video.
  Video : ${INPUT_DIR}
  SRT   : ${SRT_DIR}
Move one of the files, or symlink it, so they share a directory."
fi

# ── Validate mutual exclusions ────────────────────────────────────────────────

if [[ -n "$INTERACTIVE" && -n "$NO_INTERACTIVE" ]]; then
    die "--interactive and --no-interactive are mutually exclusive"
fi

if [[ -n "$REDO_REVIEW" && -n "$NO_INTERACTIVE" ]]; then
    die "--redo-review and --no-interactive are mutually exclusive (--redo-review needs the interactive loop it's asking to re-run)"
fi

if [[ -n "$REDO_REVIEW" && ( ${#SKIP_INDICES[@]} -gt 0 || ${#ADD_INTERVALS[@]} -gt 0 ) ]]; then
    die "--redo-review cannot be combined with --skip-index/--add-interval in the same invocation
  The interactive loop rewrites review.json from scratch and would discard those direct edits.
  Run them in separate invocations instead."
fi

if [[ ${#REDO_STEPS[@]} -gt 0 && ( -n "$REDO_REVIEW" || ${#SKIP_INDICES[@]} -gt 0 || ${#ADD_INTERVALS[@]} -gt 0 ) ]]; then
    die "--redo-step cannot be combined with --skip-index/--add-interval/--redo-review in the same invocation
  Those edit review.json to fix a content mistake and always redo Steps 5, 6, 6b, and 7 together;
  --redo-step only forces the step(s) named. Run them in separate invocations instead."
fi

if [[ -n "$REDO_AUDIO" && ( -n "$REDO_REVIEW" || ${#SKIP_INDICES[@]} -gt 0 || ${#ADD_INTERVALS[@]} -gt 0 || ${#REDO_STEPS[@]} -gt 0 ) ]]; then
    die "--redo-audio cannot be combined with --skip-index/--add-interval/--redo-review/--redo-step in the same invocation
  Run them in separate invocations instead."
fi

if [[ -n "$REDO_AUDIO" && -n "$BATCH" ]]; then
    die "--redo-audio cannot be combined with --batch (it targets one already-completed job, not a directory).
  Run it against that one file directly instead, without --batch."
fi

if [[ -n "$RECURSIVE" && -z "$BATCH" ]]; then
    die "--recursive only applies with --batch"
fi

if [[ -n "$BATCH" && ( -n "$REDO_REVIEW" || ${#SKIP_INDICES[@]} -gt 0 || ${#ADD_INTERVALS[@]} -gt 0 || ${#REDO_STEPS[@]} -gt 0 ) ]]; then
    die "--batch cannot be combined with --skip-index/--add-interval/--redo-review/--redo-step
  Those target one already-completed job, not a directory of files. Run the correction
  against that one file directly instead, without --batch."
fi

# ── Check Docker is available ─────────────────────────────────────────────────

command -v docker >/dev/null 2>&1 \
    || die "docker not found in PATH; install Docker and try again"

if [[ -z "$DRY_RUN" ]]; then
    docker info >/dev/null 2>&1 \
        || die "Docker daemon is not running (or current user lacks permission)"
fi

# ── Resolve naming_style / edition_name, and redirect TV output ───────────────
#
# Needed to decide whether (and how) to redirect a TV episode's output
# into Plex's sibling "Show (Year) {edition-Name}" directory (see
# redirect_for_tv_edition above) -- a decision made here, in bash, since
# only bash has visibility into the real, un-mounted directory names
# above wherever /input ends up scoped to for a given file. One quick,
# read-only container call, run once per invocation, not per file. Has
# to happen before "Create host-side directories" below, so that step
# creates the right (possibly redirected) directory instead of the
# plain mirrored one.
#
# Skipped entirely for --dry-run: dry-run doesn't require Docker to even
# be running today (see the skipped `docker info` check just above), and
# this would be the first thing to break that. The dry-run output further
# down notes this explicitly rather than silently showing an unredirected
# path for what might be a TV episode.
if [[ -z "$DRY_RUN" ]]; then
    NAMING_OUT="$(docker run --rm --entrypoint python --user "$(id -u):$(id -g)" \
        -v "${CONFIG_DIR}:/config:ro" "${IMAGE_NAME}" \
        /app/resolve_naming.py --config /config/config.yaml)" \
        || die "could not read naming config (see any error above)."
    while IFS='=' read -r key value; do
        case "$key" in
            naming_style) NAMING_STYLE="$value" ;;
            edition_name) EDITION_NAME="$value" ;;
            format)       OUT_FORMAT="$value" ;;
        esac
    done <<< "$NAMING_OUT"

    # Single-file mode: batch mode's equivalent lives in the per-file
    # loop further down, where each file's own directory context is
    # known (see FILE_OUTPUT_DIR there).
    if [[ -z "$BATCH" && "$NAMING_STYLE" == "plex_edition" ]] \
        && is_tv_episode "$VIDEO_BASENAME" "$(basename "$INPUT_DIR")"; then
        OUTPUT_DIR="$(redirect_for_tv_edition "$OUTPUT_DIR" "$EDITION_NAME")"
    fi
fi

# ── Create host-side directories if they don't exist ─────────────────────────

for dir in "$OUTPUT_DIR" "$CONFIG_DIR" "$CACHE_DIR" "$JOBS_DIR"; do
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir" \
            || die "could not create directory: ${dir}"
    fi
done

# Warn if config directory is empty — the pipeline will run on the
# config.yaml baked into the image at build time (see Dockerfile /
# utils.load_config()), but you'll want your own word list for a real run.
if [[ -z "$(ls -A "$CONFIG_DIR" 2>/dev/null)" ]]; then
    echo "${SCRIPT_NAME}: warning: config directory is empty: ${CONFIG_DIR}" >&2
    echo "  Copy config/config.yaml and config/word_list.txt from the repo into that directory," >&2
    echo "  or leave it empty to use the image's built-in config.yaml as-is." >&2
fi

# ── Shared docker-invocation pieces (same for every file, single or batch) ────

# TTY: allocate only for interactive review so the terminal works correctly.
# In unattended mode, no TTY is needed and --detach would be valid, but we
# keep it attached so log output appears in the terminal.
TTY_ARGS=()
if [[ -n "$INTERACTIVE" || -n "$REDO_REVIEW" ]]; then
    TTY_ARGS=(-it)
elif [[ -z "$NO_INTERACTIVE" && -n "${AC_INTERACTIVE:-}" ]]; then
    # AC_INTERACTIVE alone (no --interactive flag) also activates Step 4b
    # inside the container, which needs a TTY for its prompts just the same.
    # This still can't see interactive.enabled: true set only in
    # config.yaml — hush.sh doesn't parse YAML — but pipeline.py fails fast
    # with a clear error in that case instead of hanging or crashing on the
    # first prompt after hours of processing (see pipeline.py's isatty check).
    TTY_ARGS=(-it)
fi

# Run as the invoking host user, not root.  Without this, every file the
# container writes into /jobs, /cache, and /output (bind mounts onto real
# host directories) ends up owned by root, which then needs sudo to delete,
# move, or re-process.  The container has no /etc/passwd entry for this
# UID/GID — it doesn't need one; see the Dockerfile's HOME/bytecode notes.
USER_ARGS=(--user "$(id -u):$(id -g)")

# Environment variables that don't vary per file -- AC_INPUT_HOST_DIR /
# AC_OUTPUT_HOST_DIR are the only two that do (a batch run mounts a
# different /input //output pair per file when --recursive spans multiple
# subdirectories), so those are added inside build_docker_cmd() below
# instead of here.
BASE_ENV_ARGS=()
# --keep-tmp (CLI flag) takes priority; AC_KEEP_INTERMEDIATES from the host
# env is the fallback when --keep-tmp wasn't passed. Both forward the same
# variable into the container -- there's no separate flag for "off" since
# this one defaults to false already.
if [[ -n "$KEEP_TMP" ]]; then
    BASE_ENV_ARGS+=(-e "AC_KEEP_INTERMEDIATES=1")
elif [[ -n "${AC_KEEP_INTERMEDIATES:-}" ]]; then
    BASE_ENV_ARGS+=(-e "AC_KEEP_INTERMEDIATES=${AC_KEEP_INTERMEDIATES}")
fi
# AC_KEEP_CORRECTION_ARTIFACTS defaults to true inside the container (see
# utils.load_config), so unlike the other AC_ vars here, passing it through
# only matters when someone wants to turn it *off* (=0) -- but forwarding
# unconditionally whenever it's set on the host (1 or 0) is simplest and
# correct either way; load_config() handles both values explicitly.
[[ -n "${AC_KEEP_CORRECTION_ARTIFACTS:-}" ]] && BASE_ENV_ARGS+=(-e "AC_KEEP_CORRECTION_ARTIFACTS=${AC_KEEP_CORRECTION_ARTIFACTS}")
[[ -n "${AC_LOG_LEVEL:-}" ]]    && BASE_ENV_ARGS+=(-e "AC_LOG_LEVEL=${AC_LOG_LEVEL}")
[[ -n "${AC_SEGMENT_SIZE:-}" ]] && BASE_ENV_ARGS+=(-e "AC_SEGMENT_SIZE=${AC_SEGMENT_SIZE}")

# Containers default to UTC with no idea what the host's wall clock says.
# Capture the host's current UTC offset (respects an exported TZ in this
# shell, since `date` itself does) and forward it so the pipeline's log
# timestamps -- and job.json's started_at/finished_at companions -- match
# the clock on this machine instead of silently running several hours
# "ahead" for anyone not physically in UTC. A numeric offset (not a named
# zone like "America/Los_Angeles") is forwarded deliberately -- it needs
# no timezone database inside the image and no agreement between host and
# container about one; see utils.py's "Timezone resolution" section for
# the Python side of this. AC_TZ_NAME is the abbreviation, cosmetic only.
BASE_ENV_ARGS+=(-e "AC_TZ_OFFSET=$(date +%z)")
HOST_TZ_NAME="$(date +%Z)"
[[ -n "$HOST_TZ_NAME" ]] && BASE_ENV_ARGS+=(-e "AC_TZ_NAME=${HOST_TZ_NAME}")
# AC_INTERACTIVE from the host env is only honoured when --interactive /
# --no-interactive were not already set on the command line (those flags
# translate directly into --interactive / --no-interactive pipeline args).
if [[ -z "$INTERACTIVE" && -z "$NO_INTERACTIVE" && -n "${AC_INTERACTIVE:-}" ]]; then
    BASE_ENV_ARGS+=(-e "AC_INTERACTIVE=${AC_INTERACTIVE}")
fi

# Builds DOCKER_CMD (global array) for one file. Only the four arguments
# below ever differ between a plain single-file run and one iteration of
# a --batch run -- everything else these read is one of the shared
# pieces resolved once, above (TTY_ARGS/USER_ARGS/BASE_ENV_ARGS/
# CONFIG_DIR/CACHE_DIR/JOBS_DIR/IMAGE_NAME) or a flag that applies
# uniformly across the whole invocation, batch or not
# (INTERACTIVE/NO_INTERACTIVE/REDO_REVIEW/SKIP_INDICES/ADD_INTERVALS/
# REDO_STEPS -- the last four are already validated above to never
# coexist with --batch, but are harmless to include unconditionally here
# since the single-file path is what actually uses them).
build_docker_cmd() {
    local file_input_dir="$1" file_video_basename="$2" file_srt_basename="$3" file_output_dir="$4"

    local volume_args=(
        -v "${file_input_dir}:/input:ro"
        -v "${file_output_dir}:/output"
        -v "${CONFIG_DIR}:/config:ro"
        -v "${CACHE_DIR}:/cache"
        -v "${JOBS_DIR}:/jobs"
    )

    # job.json logs input_path/mux.output_path as *directories* a human
    # could actually navigate to (see utils.paths_banner()) rather than
    # this container's own /input //output mount points, which mean
    # nothing outside it.
    local env_args=("${BASE_ENV_ARGS[@]+"${BASE_ENV_ARGS[@]}"}")
    env_args+=(-e "AC_INPUT_HOST_DIR=${file_input_dir}")
    env_args+=(-e "AC_OUTPUT_HOST_DIR=${file_output_dir}")

    local pipeline_args=("/input/${file_video_basename}")
    [[ -n "$file_srt_basename" ]] && pipeline_args+=("/input/${file_srt_basename}")
    [[ -n "$INTERACTIVE" ]]       && pipeline_args+=("--interactive")
    [[ -n "$NO_INTERACTIVE" ]]    && pipeline_args+=("--no-interactive")
    [[ -n "$REDO_REVIEW" ]]       && pipeline_args+=("--redo-review")
    [[ -n "$REDO_AUDIO" ]]        && pipeline_args+=("--redo-audio")
    local idx
    for idx in "${SKIP_INDICES[@]+"${SKIP_INDICES[@]}"}"; do
        pipeline_args+=("--skip-index" "$idx")
    done
    if [[ ${#ADD_INTERVALS[@]} -gt 0 ]]; then
        local i
        for ((i = 0; i < ${#ADD_INTERVALS[@]}; i += 3)); do
            pipeline_args+=("--add-interval" "${ADD_INTERVALS[$i]}" "${ADD_INTERVALS[$i+1]}" "${ADD_INTERVALS[$i+2]}")
        done
    fi
    local step
    for step in "${REDO_STEPS[@]+"${REDO_STEPS[@]}"}"; do
        pipeline_args+=("--redo-step" "$step")
    done

    DOCKER_CMD=(
        docker run --rm
        "${TTY_ARGS[@]+"${TTY_ARGS[@]}"}"
        "${USER_ARGS[@]}"
        "${volume_args[@]}"
        "${env_args[@]+"${env_args[@]}"}"
        "${IMAGE_NAME}"
        "${pipeline_args[@]}"
    )
}

# ── Batch mode ─────────────────────────────────────────────────────────────────

if [[ -n "$BATCH" ]]; then
    PLAN_ARGS=(/app/batch_plan.py /input --config /config/config.yaml)
    [[ -n "$RECURSIVE" ]] && PLAN_ARGS+=(--recursive)

    # Read-only across the board -- the planning pass only ever looks,
    # never writes; --entrypoint bypasses entrypoint.sh's UID/passwd
    # patching (see Dockerfile), which nothing here needs (that exists
    # solely for PostgreSQL's initdb, on the alignment.backend: mfa path).
    # BASE_ENV_ARGS is forwarded here too -- easy to miss, since this isn't
    # a build_docker_cmd() call, but without it AC_LOG_LEVEL=debug would
    # silently never reach batch_plan.py, regardless of what the person
    # actually set on the host.
    PLAN_CMD=(
        docker run --rm
        --entrypoint python
        "${USER_ARGS[@]}"
        "${BASE_ENV_ARGS[@]+"${BASE_ENV_ARGS[@]}"}"
        -v "${INPUT_DIR}:/input:ro"
        -v "${OUTPUT_DIR}:/output:ro"
        -v "${CONFIG_DIR}:/config:ro"
        -v "${JOBS_DIR}:/jobs:ro"
        "${IMAGE_NAME}"
        "${PLAN_ARGS[@]}"
    )

    if [[ -n "$DRY_RUN" ]]; then
        echo "# profanity-hush dry run (--batch) — planning command that runs first:"
        printf '%q \\\n' "${PLAN_CMD[@]}" | sed '$ s/ \\$//'
        echo
        echo "# It prints which files under ${INPUT_DIR} still need processing."
        echo "# Movies already censored are skipped; TV episodes are always queued"
        echo "# (and their output overwritten if it already exists) -- see"
        echo "# batch_plan.py's module docstring for why. Each queued file is then"
        echo "# run exactly like a plain, non-batch --dry-run invocation against that"
        echo "# single file would be -- same volume/env/pipeline-arg shape, just"
        echo "# looped, with /input and /output mounted to that file's own directory."
        echo "#"
        echo "# Note: --dry-run never touches Docker, so the naming_style lookup"
        echo "# that would redirect a TV episode's /output to a sibling"
        echo "# \"Show (Year) {edition-Name}\" directory (Plex's TV-editions"
        echo "# convention) doesn't run here either -- a real run may redirect it."
        echo "#"
        echo "# A real run also writes a batch log under \${JOBS_DIR}/batch-logs/ --"
        echo "# nothing is written for --dry-run itself."
        exit 0
    fi

    # ── Batch log ──────────────────────────────────────────────────────────
    #
    # One plain-text file per --batch invocation, under the existing jobs
    # directory (no new flag/host directory needed -- --jobs already exists
    # and is already mounted). A high-level overview only, on purpose: the
    # plan phase's own summary, plus one line per file (start, outcome,
    # duration, and -- see the loop below -- a short "notable" note if
    # pipeline.py flagged one, e.g. an MFA alignment falling back to
    # whisperx.align()) from batch_log(). A per-file docker run's full
    # stderr is deliberately NOT captured here -- it goes only to the
    # terminal, same as any single-file run -- since that full step-by-
    # step transcript already lives in that job's own job_dir/logs/*.log,
    # and duplicating it here on top would make this file just as long as
    # reading through every job individually, defeating the point of a
    # quick, scannable overview across 100+ files. The "notable" note
    # comes from stdout instead (see build_docker_cmd()/the loop below) --
    # pipeline.py writes exactly one compact line there, specifically for
    # this, completely separate from its normal stderr logging.
    PLAN_OUT=""
    PLAN_ERR=""
    FILE_STDOUT=""
    trap 'rm -f "${PLAN_OUT:-}" "${PLAN_ERR:-}" "${FILE_STDOUT:-}"' EXIT

    BATCH_LOG_DIR="${JOBS_DIR}/batch-logs"
    mkdir -p "$BATCH_LOG_DIR" || die "could not create directory: ${BATCH_LOG_DIR}"
    BATCH_LOG_FILE="${BATCH_LOG_DIR}/$(date +%Y%m%d_%H%M%S)_$(slugify "$(basename "$INPUT_DIR")").log"

    {
        echo "=== profanity-hush batch run ==="
        echo "Started : $(date '+%Y-%m-%d %H:%M:%S %z')"
        echo "Input   : ${INPUT_DIR}$( [[ -n "$RECURSIVE" ]] && echo " (recursive)" )"
        echo "Output  : ${OUTPUT_DIR}  (base root -- TV episodes redirect to a sibling"
        echo "          \"Show (Year) {edition-Name}\" directory; see each file's own"
        echo "          START line below for exactly where it went)"
        echo "Command : ${SCRIPT_NAME} ${ORIGINAL_ARGS[*]+"${ORIGINAL_ARGS[*]}"}"
        echo "================================="
    } > "$BATCH_LOG_FILE"

    echo "${SCRIPT_NAME}: batch log: ${BATCH_LOG_FILE}" >&2
    batch_log INFO "planning batch run over ${INPUT_DIR}$( [[ -n "$RECURSIVE" ]] && echo " (recursive)" )..."

    # A real temp file, not command substitution -- bash strings can't
    # hold embedded NUL bytes, so `$(...)` would silently corrupt the
    # NUL-delimited list batch_plan.py prints (needed because filenames
    # in a real media library routinely contain everything else: spaces,
    # commas, apostrophes, colons, "..."). Reading from a file (rather
    # than `< <(...)` process substitution) also means the plan command's
    # own exit code is checked explicitly below, instead of `set -e`
    # silently not noticing a failed planning pass because nothing
    # downstream of a process substitution propagates its exit status.
    #
    # batch_plan.py's own stderr (its INFO summary, and at debug the
    # per-file filenames -- still just as concise as before, this isn't
    # what "duplication" meant above) goes to a second temp file, then
    # gets replayed to both the terminal and the log file right after the
    # command finishes, rather than streamed live through a tee. Planning
    # is a quick, one-shot, read-only pass, so that's indistinguishable
    # from live in practice, and it avoids a second background `tee` (and
    # the explicit wait its flush timing would need) just for this one
    # command.
    PLAN_OUT="$(mktemp)"
    PLAN_ERR="$(mktemp)"

    PLAN_RC=0
    "${PLAN_CMD[@]}" > "$PLAN_OUT" 2> "$PLAN_ERR" || PLAN_RC=$?
    cat "$PLAN_ERR" >&2
    cat "$PLAN_ERR" >> "$BATCH_LOG_FILE"

    if [[ "$PLAN_RC" -ne 0 ]]; then
        die "batch planning failed (see the error above)."
    fi

    FILES=()
    while IFS= read -r -d '' f; do
        FILES+=("$f")
    done < "$PLAN_OUT"

    TOTAL=${#FILES[@]}
    if [[ "$TOTAL" -eq 0 ]]; then
        batch_log INFO "nothing to do — see the counts above."
        exit 0
    fi

    SUCCEEDED=0
    SKIPPED=0
    FAILED_FILES=()
    INTERRUPTED=0
    # A plain variable assignment, not `exit` -- lets the docker run for
    # the file currently in progress finish (Ctrl-C is forwarded to it by
    # docker same as any foreground container; pipeline.py's own
    # KeyboardInterrupt handling marks that job "interrupted" and exits
    # 130) rather than killing it mid-step. Checked right after each
    # per-file run, below, to stop the loop before starting the next one.
    trap 'INTERRUPTED=1' INT

    FILE_STDOUT="$(mktemp)"

    N=0
    for CONTAINER_PATH in "${FILES[@]}"; do
        N=$((N + 1))
        REL="${CONTAINER_PATH#/input/}"
        FILE_BASENAME="$(basename "$REL")"
        REL_DIR="$(dirname "$REL")"
        if [[ "$REL_DIR" == "." ]]; then
            FILE_INPUT_DIR="$INPUT_DIR"
            FILE_OUTPUT_DIR="$OUTPUT_DIR"
        else
            FILE_INPUT_DIR="${INPUT_DIR}/${REL_DIR}"
            FILE_OUTPUT_DIR="${OUTPUT_DIR}/${REL_DIR}"
        fi

        if [[ "$NAMING_STYLE" == "plex_edition" ]] \
            && is_tv_episode "$FILE_BASENAME" "$(basename "$FILE_INPUT_DIR")"; then
            FILE_OUTPUT_DIR="$(redirect_for_tv_edition "$FILE_OUTPUT_DIR" "$EDITION_NAME")"

            # batch_plan.py deliberately never existence-checks a TV
            # episode (see its own module docstring for why) -- do the
            # equivalent check here instead, now that the real redirect
            # target is known. Safe to do in plain bash, unlike the movie
            # case: a TV filename needs no transformation at all (see
            # mux.py's _output_path()), just the out_format extension
            # swap, so there's no risk of this drifting out of sync with
            # what Step 7 actually names the file the way replicating
            # movies' fuller naming logic here would risk.
            PREDICTED_NAME="${FILE_BASENAME%.*}.${OUT_FORMAT}"
            if [[ -f "${FILE_OUTPUT_DIR}/${PREDICTED_NAME}" ]]; then
                batch_log INFO "[${N}/${TOTAL}] SKIP   already done  ${REL}  ->  ${FILE_OUTPUT_DIR}"
                SKIPPED=$((SKIPPED + 1))
                continue
            fi
        fi

        mkdir -p "$FILE_OUTPUT_DIR" || die "could not create directory: ${FILE_OUTPUT_DIR}"

        batch_log INFO "[${N}/${TOTAL}] START  ${REL}  ->  ${FILE_OUTPUT_DIR}"
        FILE_STARTED=$(date +%s)

        # Only stdout is redirected -- stderr (all of pipeline.py's normal
        # step-by-step logging) is left completely alone, going only to
        # the terminal same as any single-file run, same as always. stdout
        # is where pipeline.py prints exactly one line, right at the end
        # of a run that reaches that point: "AC_RESULT ok" or "AC_RESULT
        # warnings :: ...". That's the only thing captured here.
        : > "$FILE_STDOUT"
        build_docker_cmd "$FILE_INPUT_DIR" "$FILE_BASENAME" "" "$FILE_OUTPUT_DIR"
        RC=0
        "${DOCKER_CMD[@]}" > "$FILE_STDOUT" || RC=$?

        FILE_ELAPSED=$(( $(date +%s) - FILE_STARTED ))

        if [[ "$INTERRUPTED" -eq 1 || "$RC" -eq 130 ]]; then
            batch_log WARN "[${N}/${TOTAL}] INTERRUPTED after $(fmt_hms "$FILE_ELAPSED")  ${REL}"
            batch_log WARN "batch interrupted (Ctrl-C) after ${N}/${TOTAL} files."
            batch_log WARN "  ${SUCCEEDED} succeeded, ${SKIPPED} already done, ${#FAILED_FILES[@]} failed before the interrupt."
            batch_log WARN "  Re-run the same command to resume -- already-done files are skipped"
            batch_log WARN "  automatically, and a part-finished file resumes from its last completed step."
            exit 130
        elif [[ "$RC" -ne 0 ]]; then
            batch_log WARN "[${N}/${TOTAL}] FAILED (exit ${RC}) after $(fmt_hms "$FILE_ELAPSED")  ${REL}"
            FAILED_FILES+=("$REL")
        else
            AC_RESULT_LINE="$(grep -m1 '^AC_RESULT ' "$FILE_STDOUT" || true)"
            if [[ "$AC_RESULT_LINE" == "AC_RESULT warnings"* ]]; then
                batch_log WARN "[${N}/${TOTAL}] DONE   in $(fmt_hms "$FILE_ELAPSED")  ${REL}  (${AC_RESULT_LINE#AC_RESULT warnings :: })"
            else
                batch_log INFO "[${N}/${TOTAL}] DONE   in $(fmt_hms "$FILE_ELAPSED")  ${REL}"
            fi
            SUCCEEDED=$((SUCCEEDED + 1))
        fi
    done
    trap - INT

    batch_log INFO "batch complete — ${SUCCEEDED}/${TOTAL} succeeded, ${SKIPPED} already done."
    if [[ ${#FAILED_FILES[@]} -gt 0 ]]; then
        batch_log WARN "  ${#FAILED_FILES[@]} failed:"
        for f in "${FAILED_FILES[@]}"; do
            batch_log WARN "    - ${f}"
        done
        batch_log WARN "  Each failure's own job log has the detail (see ${JOBS_DIR}). Re-running the"
        batch_log WARN "  same --batch command later will retry only the ones that didn't succeed."
        exit 1
    fi
    exit 0
fi

# ── Single-file mode ────────────────────────────────────────────────────────────

build_docker_cmd "$INPUT_DIR" "$VIDEO_BASENAME" "$SRT_BASENAME" "$OUTPUT_DIR"

if [[ -n "$DRY_RUN" ]]; then
    # Print the command in a readable multi-line form
    echo "# profanity-hush dry run — command that would be executed:"
    printf '%q \\\n' "${DOCKER_CMD[@]}" | sed '$ s/ \\$//'
    echo
    echo "# Volume mappings:"
    echo "#   ${INPUT_DIR}  →  /input  (ro)"
    echo "#   ${OUTPUT_DIR}  →  /output"
    echo "#   ${CONFIG_DIR}  →  /config  (ro)"
    echo "#   ${CACHE_DIR}  →  /cache"
    echo "#   ${JOBS_DIR}  →  /jobs"
    echo "#"
    echo "# Note: --dry-run never touches Docker (no daemon required), so the"
    echo "# naming_style/edition_name lookup that decides whether this is a TV"
    echo "# episode -- and, if so, redirects /output to a sibling"
    echo "# \"Show (Year) {edition-Name}\" directory per Plex's TV-editions"
    echo "# convention -- doesn't run either. The /output path above is exactly"
    echo "# what a plain movie (or non-plex_edition) run would use; a real TV"
    echo "# episode run may redirect it."
    exit 0
fi

exec "${DOCKER_CMD[@]}"

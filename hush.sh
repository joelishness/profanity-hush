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
# Usage:
#   hush.sh [OPTIONS] <input_video> [subtitle_file]
#
# Options:
#   -o, --output DIR      Output directory (default: same directory as input)
#   -c, --config DIR      Config directory (default: ~/.config/profanity-hush)
#       --cache  DIR      Model cache directory (default: ~/.cache/profanity-hush)
#       --jobs   DIR      Job history directory (default: ~/.local/share/profanity-hush/jobs)
#       --interactive     Pause for review of flagged words before muting
#       --no-interactive  Force unattended mode (overrides config.yaml)
#       --keep-tmp        Retain large intermediate WAV stems after the run
#       --skip-index N    Correction: un-mute the flagged match at this word_index
#                         (see censor_log.json). Repeatable. Re-runs Steps 5-7 only.
#       --add-interval TEXT START END
#                         Correction: add a manual mute interval (seconds).
#                         Repeatable. Re-runs Steps 5-7 only.
#       --redo-review     Correction: re-enter interactive review from scratch
#                         on an already-completed job (implies --interactive).
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
# =============================================================================
set -euo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────────

SCRIPT_NAME="$(basename "$0")"

usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [OPTIONS] <input_video> [subtitle_file]

Options:
  -o, --output DIR      Output directory (default: same directory as input)
  -c, --config DIR      Config directory (default: ~/.config/profanity-hush)
      --cache  DIR      Model cache directory (default: ~/.cache/profanity-hush)
      --jobs   DIR      Job history directory (default: ~/.local/share/profanity-hush/jobs)
      --interactive     Pause for review of flagged words before muting
      --no-interactive  Force unattended mode (overrides config.yaml)
      --keep-tmp        Retain large intermediate WAV stems after the run
      --skip-index N    Correction: un-mute the flagged match at this word_index
                        (see censor_log.json). Repeatable. Re-runs Steps 5-7 only.
      --add-interval TEXT START END
                        Correction: add a manual mute interval (seconds).
                        Repeatable. Re-runs Steps 5-7 only.
      --redo-review     Correction: re-enter interactive review from scratch
                        on an already-completed job (implies --interactive).
      --dry-run         Print the docker command without executing it
  -h, --help            Show this help message

Examples:
  ${SCRIPT_NAME} movie.mkv
  ${SCRIPT_NAME} --interactive movie.mkv movie.srt
  ${SCRIPT_NAME} -o ~/censored/ movie.mkv
  ${SCRIPT_NAME} --dry-run --interactive movie.mkv movie.srt
  ${SCRIPT_NAME} --skip-index 4856 movie.mkv
  ${SCRIPT_NAME} --add-interval "missed word" 1203.1 1203.5 movie.mkv
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

[[ -f "$INPUT_VIDEO" ]] || die "input file not found: ${INPUT_VIDEO}"

# ── Resolve all paths to absolute (Docker requires absolute paths for -v) ─────

INPUT_VIDEO_ABS="$(resolve_path "$INPUT_VIDEO")"
INPUT_DIR="$(dirname "$INPUT_VIDEO_ABS")"
VIDEO_BASENAME="$(basename "$INPUT_VIDEO_ABS")"

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

# ── Create host-side directories if they don't exist ─────────────────────────

for dir in "$OUTPUT_DIR" "$CONFIG_DIR" "$CACHE_DIR" "$JOBS_DIR"; do
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir" \
            || die "could not create directory: ${dir}"
    fi
done

# Warn if config directory is empty — the pipeline will use its built-in defaults
# but the user should supply their own word list for a real run.
if [[ -z "$(ls -A "$CONFIG_DIR" 2>/dev/null)" ]]; then
    echo "${SCRIPT_NAME}: warning: config directory is empty: ${CONFIG_DIR}" >&2
    echo "  Copy config/config.yaml and config/word_list.txt from the repo into that directory." >&2
fi

# ── Check Docker is available ─────────────────────────────────────────────────

command -v docker >/dev/null 2>&1 \
    || die "docker not found in PATH; install Docker and try again"

if [[ -z "$DRY_RUN" ]]; then
    docker info >/dev/null 2>&1 \
        || die "Docker daemon is not running (or current user lacks permission)"
fi

# ── Build docker command ──────────────────────────────────────────────────────

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

# Volume mounts
VOLUME_ARGS=(
    -v "${INPUT_DIR}:/input:ro"
    -v "${OUTPUT_DIR}:/output"
    -v "${CONFIG_DIR}:/config:ro"
    -v "${CACHE_DIR}:/cache"
    -v "${JOBS_DIR}:/jobs"
)

# Run as the invoking host user, not root.  Without this, every file the
# container writes into /jobs, /cache, and /output (bind mounts onto real
# host directories) ends up owned by root, which then needs sudo to delete,
# move, or re-process.  The container has no /etc/passwd entry for this
# UID/GID — it doesn't need one; see the Dockerfile's HOME/bytecode notes.
USER_ARGS=(--user "$(id -u):$(id -g)")

# Environment variables passed to the container
ENV_ARGS=()
# --keep-tmp (CLI flag) takes priority; AC_KEEP_INTERMEDIATES from the host
# env is the fallback when --keep-tmp wasn't passed. Both forward the same
# variable into the container -- there's no separate flag for "off" since
# this one defaults to false already.
if [[ -n "$KEEP_TMP" ]]; then
    ENV_ARGS+=(-e "AC_KEEP_INTERMEDIATES=1")
elif [[ -n "${AC_KEEP_INTERMEDIATES:-}" ]]; then
    ENV_ARGS+=(-e "AC_KEEP_INTERMEDIATES=${AC_KEEP_INTERMEDIATES}")
fi
# AC_KEEP_CORRECTION_ARTIFACTS defaults to true inside the container (see
# utils.load_config), so unlike the other AC_ vars here, passing it through
# only matters when someone wants to turn it *off* (=0) -- but forwarding
# unconditionally whenever it's set on the host (1 or 0) is simplest and
# correct either way; load_config() handles both values explicitly.
[[ -n "${AC_KEEP_CORRECTION_ARTIFACTS:-}" ]] && ENV_ARGS+=(-e "AC_KEEP_CORRECTION_ARTIFACTS=${AC_KEEP_CORRECTION_ARTIFACTS}")
[[ -n "${AC_LOG_LEVEL:-}" ]]    && ENV_ARGS+=(-e "AC_LOG_LEVEL=${AC_LOG_LEVEL}")
[[ -n "${AC_SEGMENT_SIZE:-}" ]] && ENV_ARGS+=(-e "AC_SEGMENT_SIZE=${AC_SEGMENT_SIZE}")

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
ENV_ARGS+=(-e "AC_TZ_OFFSET=$(date +%z)")
HOST_TZ_NAME="$(date +%Z)"
[[ -n "$HOST_TZ_NAME" ]] && ENV_ARGS+=(-e "AC_TZ_NAME=${HOST_TZ_NAME}")
# AC_INTERACTIVE from the host env is only honoured when --interactive /
# --no-interactive were not already set on the command line (those flags
# translate directly into --interactive / --no-interactive pipeline args).
if [[ -z "$INTERACTIVE" && -z "$NO_INTERACTIVE" && -n "${AC_INTERACTIVE:-}" ]]; then
    ENV_ARGS+=(-e "AC_INTERACTIVE=${AC_INTERACTIVE}")
fi

# Arguments forwarded to pipeline.py inside the container
PIPELINE_ARGS=("/input/${VIDEO_BASENAME}")
[[ -n "$SRT_BASENAME" ]]   && PIPELINE_ARGS+=("/input/${SRT_BASENAME}")
[[ -n "$INTERACTIVE" ]]    && PIPELINE_ARGS+=("--interactive")
[[ -n "$NO_INTERACTIVE" ]] && PIPELINE_ARGS+=("--no-interactive")
[[ -n "$REDO_REVIEW" ]]    && PIPELINE_ARGS+=("--redo-review")
for idx in "${SKIP_INDICES[@]+"${SKIP_INDICES[@]}"}"; do
    PIPELINE_ARGS+=("--skip-index" "$idx")
done
if [[ ${#ADD_INTERVALS[@]} -gt 0 ]]; then
    for ((i = 0; i < ${#ADD_INTERVALS[@]}; i += 3)); do
        PIPELINE_ARGS+=("--add-interval" "${ADD_INTERVALS[$i]}" "${ADD_INTERVALS[$i+1]}" "${ADD_INTERVALS[$i+2]}")
    done
fi

# Assemble final command
DOCKER_CMD=(
    docker run --rm
    "${TTY_ARGS[@]+"${TTY_ARGS[@]}"}"
    "${USER_ARGS[@]}"
    "${VOLUME_ARGS[@]}"
    "${ENV_ARGS[@]+"${ENV_ARGS[@]}"}"
    "${IMAGE_NAME}"
    "${PIPELINE_ARGS[@]}"
)

# ── Execute (or print for --dry-run) ──────────────────────────────────────────

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
    exit 0
fi

exec "${DOCKER_CMD[@]}"

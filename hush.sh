#!/usr/bin/env bash
# hush.sh — profanity-hush host-side entry point
# =============================================================================
# Resolves paths, creates required directories, and launches the profanity-hush
# Docker container with the correct volume mounts.
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
#       --dry-run         Print the docker command without executing it
#   -h, --help            Show this help message
#
# Examples:
#   hush.sh movie.mkv
#   hush.sh --interactive movie.mkv movie.srt
#   hush.sh -o ~/censored/ movie.mkv
#   hush.sh --dry-run movie.mkv movie.srt
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
      --dry-run         Print the docker command without executing it
  -h, --help            Show this help message

Examples:
  ${SCRIPT_NAME} movie.mkv
  ${SCRIPT_NAME} --interactive movie.mkv movie.srt
  ${SCRIPT_NAME} -o ~/censored/ movie.mkv
  ${SCRIPT_NAME} --dry-run --interactive movie.mkv movie.srt
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

# realpath equivalent that works even when the target doesn't exist yet
resolve_path() {
    local p
    p="$(abs_path "$1")"
    # Collapse any . or .. components
    echo "$(cd "$(dirname "$p")" 2>/dev/null && pwd)/$(basename "$p")" 2>/dev/null \
        || echo "$p"
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
if [[ -n "$INTERACTIVE" ]]; then
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

# Environment variables passed to the container
ENV_ARGS=()
[[ -n "$KEEP_TMP" ]]            && ENV_ARGS+=(-e "AC_KEEP_INTERMEDIATES=1")
[[ -n "${AC_LOG_LEVEL:-}" ]]    && ENV_ARGS+=(-e "AC_LOG_LEVEL=${AC_LOG_LEVEL}")
[[ -n "${AC_SEGMENT_SIZE:-}" ]] && ENV_ARGS+=(-e "AC_SEGMENT_SIZE=${AC_SEGMENT_SIZE}")
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

# Assemble final command
DOCKER_CMD=(
    docker run --rm
    "${TTY_ARGS[@]+"${TTY_ARGS[@]}"}"
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

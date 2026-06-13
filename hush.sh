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
# =============================================================================
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [OPTIONS] <input_video> [subtitle_file]

Options:
  -o, --output DIR      Output directory (default: same as input video directory)
  -c, --config DIR      Config directory (default: ~/.config/profanity-hush)
      --cache  DIR      Model cache directory (default: ~/.cache/profanity-hush)
      --jobs   DIR      Job history directory (default: ~/.local/share/profanity-hush/jobs)
      --interactive     Pause for human review of flagged words before muting
      --no-interactive  Force unattended mode (overrides config.yaml)
      --keep-tmp        Retain large intermediate WAV stems after the run
      --dry-run         Print the docker command without executing it
  -h, --help            Show this help message
EOF
    exit 1
}

# ── Parse Arguments ──────────────────────────────────────────────────────────

OUTPUT_DIR_OPT=""
CONFIG_DIR_OPT=""
CACHE_DIR_OPT=""
JOBS_DIR_OPT=""
INTERACTIVE=""
NO_INTERACTIVE=""
KEEP_TMP=""
DRY_RUN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)         [[ $# -lt 2 ]] && usage; OUTPUT_DIR_OPT="$2"; shift 2 ;;
        -c|--config)         [[ $# -lt 2 ]] && usage; CONFIG_DIR_OPT="$2"; shift 2 ;;
        --cache)             [[ $# -lt 2 ]] && usage; CACHE_DIR_OPT="$2"; shift 2 ;;
        --jobs)              [[ $# -lt 2 ]] && usage; JOBS_DIR_OPT="$2"; shift 2 ;;
        --interactive)       INTERACTIVE=1; shift ;;
        --no-interactive)    NO_INTERACTIVE=1; shift ;;
        --keep-tmp)          KEEP_TMP=1; shift ;;
        --dry-run)           DRY_RUN=1; shift ;;
        -h|--help)           usage ;;
        -*)                  echo "Error: Unknown option $1" >&2; usage ;;
        *)                   break ;; # End of options, remaining are positional
    esac
done

# We need at least the input video file
if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Error: Invalid number of positional arguments." >&2
    usage
fi

INPUT_VIDEO_RAW="$1"
INPUT_SRT_RAW="${2:-}"

# ── Path Resolution ──────────────────────────────────────────────────────────

if [[ ! -f "$INPUT_VIDEO_RAW" ]]; then
    echo "Error: Input video file does not exist: $INPUT_VIDEO_RAW" >&2
    exit 1
fi

INPUT_DIR="$(cd "$(dirname "$INPUT_VIDEO_RAW")" && pwd)"
VIDEO_BASENAME="$(basename "$INPUT_VIDEO_RAW")"

SRT_BASENAME=""
if [[ -n "$INPUT_SRT_RAW" ]]; then
    if [[ ! -f "$INPUT_SRT_RAW" ]]; then
        echo "Error: Subtitle file does not exist: $INPUT_SRT_RAW" >&2
        exit 1
    fi
    SRT_DIR="$(cd "$(dirname "$INPUT_SRT_RAW")" && pwd)"
    if [[ "$SRT_DIR" != "$INPUT_DIR" ]]; then
        echo "Error: Subtitle file must be in the same directory as the video file." >&2
        echo "  Video dir:    $INPUT_DIR" >&2
        echo "  Subtitle dir: $SRT_DIR" >&2
        exit 1
    fi
    SRT_BASENAME="$(basename "$INPUT_SRT_RAW")"
fi

# Fallbacks matching config defaults
OUTPUT_DIR="${OUTPUT_DIR_OPT:-$INPUT_DIR}"
CONFIG_DIR="${CONFIG_DIR_OPT:-$HOME/.config/profanity-hush}"
CACHE_DIR="${CACHE_DIR_OPT:-$HOME/.cache/profanity-hush}"
JOBS_DIR="${JOBS_DIR_OPT:-$HOME/.local/share/profanity-hush/jobs}"

# Ensure directories exist
if [[ -z "$DRY_RUN" ]]; then
    mkdir -p "$OUTPUT_DIR"
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$CACHE_DIR"
    mkdir -p "$JOBS_DIR"
fi

IMAGE_NAME="profanity-hush:latest"

# ── Container Parameter Assembly ─────────────────────────────────────────────

TTY_ARGS=()
# Use a pseudo-TTY explicitly when interactive tracking flags are present
if [[ -n "${INTERACTIVE:-}" ]]; then
    TTY_ARGS=(-it)
fi

VOLUME_ARGS=(
    -v "${INPUT_DIR}:/input:ro"
    -v "${OUTPUT_DIR}:/output"
    -v "${CONFIG_DIR}:/config:ro"
    -v "${CACHE_DIR}:/cache"
    -v "${JOBS_DIR}:/jobs"
)

ENV_ARGS=()
[[ -n "$KEEP_TMP" ]] && ENV_ARGS+=(-e "AC_KEEP_INTERMEDIATES=1")

# Build target script arguments forwarding core paths explicitly
PIPELINE_ARGS=("/input/${VIDEO_BASENAME}")
[[ -n "$SRT_BASENAME" ]]   && PIPELINE_ARGS+=("/input/${SRT_BASENAME}")
[[ -n "$INTERACTIVE" ]]    && PIPELINE_ARGS+=("--interactive")
[[ -n "$NO_INTERACTIVE" ]] && PIPELINE_ARGS+=("--no-interactive")

# Force pipeline.py to use the standard file location mounted into the volume
PIPELINE_ARGS+=("--config" "/config/config.yaml")

DOCKER_CMD=(
    docker run --rm
    "${TTY_ARGS[@]+\"${TTY_ARGS[@]}\"}"
    "${VOLUME_ARGS[@]}"
    "${ENV_ARGS[@]+\"${ENV_ARGS[@]}\"}"
    "${IMAGE_NAME}"
    "${PIPELINE_ARGS[@]}"
)

# ── Execution ────────────────────────────────────────────────────────────────

if [[ -n "$DRY_RUN" ]]; then
    echo "# profanity-hush — Engine Run Command Execution Summary"
    echo "${DOCKER_CMD[*]}"
    exit 0
fi

exec "${DOCKER_CMD[@]}"

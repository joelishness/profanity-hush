# profanity-hush — CPU-only Docker image
# =========================================================================
# Single build target; no CUDA, no nvidia-container-toolkit needed on host.
#
# Build:
#   docker build -t profanity-hush .
#
# Image size is ~1.1 GB (PyTorch CPU + audio ML stack).
# Model weights (~2-3 GB total) are downloaded on first run and stored in
# the /cache volume — always mount it to avoid re-downloading every run.
# =========================================================================

FROM python:3.11-slim

# Silence debconf "unable to initialize frontend" warnings that appear when
# apt-get runs without a TTY.  Noninteractive is the correct mode for Docker
# builds; this just stops debconf from loudly trying the others first.
ENV DEBIAN_FRONTEND=noninteractive

# ── System packages ────────────────────────────────────────────────────────
# ffmpeg   : audio extraction, muting, muxing (steps 1, 5, 6, 7)
# git      : needed by some pip packages that install from VCS at build time
# libsndfile1 : required by soundfile / librosa (demucs, whisperx deps)
# libgomp1 : OpenMP runtime; demucs benefits from multi-threaded CPU ops
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        libsndfile1 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# ── CPU-only PyTorch ────────────────────────────────────────────────────────
# Install torch/torchaudio BEFORE demucs and whisperx so pip does not pull
# in the much-larger CUDA wheels as a transitive dependency.
# CPU wheels live at a separate index URL; --extra-index-url is needed.
RUN pip install --no-cache-dir \
        torch \
        torchaudio \
        --extra-index-url https://download.pytorch.org/whl/cpu

# ── ML pipeline packages ────────────────────────────────────────────────────
# demucs      : audio source separation (step 2); htdemucs_ft model at runtime
# whisperx    : word-level transcription (step 3); wraps faster-whisper + wav2vec2
# faster-whisper : explicit install to ensure the PyPI version is used, not
#                  a pinned older version pulled by whisperx
# soundfile   : Python wrapper around libsndfile1 (already in the apt layer).
#               torchaudio 2.6+ uses a backend dispatcher for torchaudio.save();
#               without soundfile registered as a backend it raises
#               "Couldn't find appropriate backend to handle uri *.wav".
#               Note: torchaudio 2.9 will switch to torchcodec and won't need
#               soundfile for saving — but having it present won't break anything.
RUN pip install --no-cache-dir \
        demucs \
        whisperx \
        faster-whisper \
        soundfile

# ── Utility packages ────────────────────────────────────────────────────────
# pysrt     : SRT subtitle parsing (step 4, align_srt)
# rapidfuzz : fuzzy string matching for SRT cross-reference
# tqdm      : progress bars for long CPU runs
# pyyaml    : config.yaml parsing
RUN pip install --no-cache-dir \
        pysrt \
        rapidfuzz \
        tqdm \
        pyyaml

# ── Redirect all ML cache dirs to /cache (bind-mounted from host) ───────────
# This ensures model weights survive container restarts and aren't
# re-downloaded on each run.  The host path is configured in hush.sh.
#
# Demucs (via torch.hub) → /cache/torch
ENV TORCH_HOME=/cache/torch
# faster-whisper + wav2vec2 alignment models (via huggingface_hub) → /cache/huggingface
ENV HF_HOME=/cache/huggingface
# NLTK punkt tokenizer (used by whisperx internally)
ENV NLTK_DATA=/cache/nltk_data
# Catch-all for any other XDG-respecting cache users
ENV XDG_CACHE_HOME=/cache

# ── Support running as an arbitrary host UID/GID ────────────────────────────
# hush.sh runs the container with `--user "$(id -u):$(id -g)"` so that files
# written into the bind-mounted /jobs, /cache, and /output volumes land on
# the host already owned by the invoking user instead of root.  That UID/GID
# has no /etc/passwd entry inside the image, so three things need handling:
#   1. $HOME must point somewhere writable regardless of UID — anything that
#      isn't already redirected above (matplotlib font cache, stray configs)
#      falls back to $HOME.  World-writable + sticky bit, same pattern as
#      /tmp, so it's safe for any UID without needing a real account.
#   2. Python must not try to write __pycache__/*.pyc next to the read-only,
#      root-owned /app source tree — harmless either way (it silently skips
#      on PermissionError), but disabling it outright is cleaner and avoids
#      depending on that silent-failure behavior.
#   3. Every file under /app must actually be *readable*, and every
#      directory under it *traversable*, by an arbitrary non-root UID/GID —
#      see the chmod after the COPY instructions below.
RUN mkdir -p /home/hush && chmod 1777 /home/hush
ENV HOME=/home/hush
ENV PYTHONDONTWRITEBYTECODE=1

# ── Application source ──────────────────────────────────────────────────────
# /app is the root of the Python source tree; steps/ imports utils from here.
ENV PYTHONPATH=/app
WORKDIR /app
COPY src/ /app/

# ── Built-in default word list ──────────────────────────────────────────────
# config.yaml's absence is already handled by Python-side defaults
# (cfg_get(..., default=...) throughout the code) — but a word list is a
# whole file's worth of content, not a single scalar, so it needs an actual
# fallback *file*, not just a fallback value. Baking in a copy of the repo's
# own config/word_list.txt is what makes "you can skip installing config
# files entirely and the container uses its built-in defaults" (README
# install step 3 / hush.sh's startup warning) true for the word list too,
# not just for config.yaml's tunable settings. steps/matching.py falls back
# to this path when /config/word_list.txt isn't present on the host.
COPY config/word_list.txt /app/defaults/word_list.txt

# `COPY` preserves the exact file-mode bits each source file has in the
# build context — it does NOT guarantee they're world-readable. That
# depends on the contributor's umask, editor, or however the file was last
# saved/transferred on whichever machine `docker build` ran on, and is not
# something this Dockerfile controls. Everything under /app is owned by
# root (no --chown above), so if any file or directory in the build context
# ended up without an "other" read/traverse bit (e.g. mode 600 instead of
# 644), the arbitrary non-root UID from `--user` (point 3 above) gets
# `PermissionError` trying to open it — including, fatally, the entrypoint
# script itself. Force it explicitly rather than relying on every
# contributor's filesystem to happen to produce world-readable files:
#   a+rX  →  read for everyone on files; +traverse (x) only on entries that
#            already have an execute bit somewhere (i.e. directories),
#            so plain .py files don't spuriously become "executable".
RUN chmod -R a+rX /app

# Declare mount points (documentation only — actual bind mounts are in hush.sh)
VOLUME ["/input", "/output", "/config", "/cache", "/jobs"]

ENTRYPOINT ["python", "/app/pipeline.py"]

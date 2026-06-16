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
# ctranslate2 : CTranslate2 inference engine; faster-whisper's backend;
#               pin to a CPU-safe version
RUN pip install --no-cache-dir \
        demucs \
        whisperx \
        faster-whisper

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

# ── Application source ──────────────────────────────────────────────────────
# /app is the root of the Python source tree; steps/ imports utils from here.
ENV PYTHONPATH=/app
WORKDIR /app
COPY src/ /app/

# Declare mount points (documentation only — actual bind mounts are in hush.sh)
VOLUME ["/input", "/output", "/config", "/cache", "/jobs"]

ENTRYPOINT ["python", "/app/pipeline.py"]

# AutoCensor — Design Document

**Project:** Automated movie profanity censoring pipeline  
**Version:** 0.1 (draft)  
**Status:** Pre-implementation

---

## 1. Goals & Scope

Automate the end-to-end process of censoring profanity from a video file, replacing the current manual workflow of: watch film → identify curse words → mute in audio editor → recombine.

**In scope:**
- Full pipeline from raw video file to censored output video file
- Configurable word list
- Optional SRT/subtitle cross-reference for improved accuracy
- CPU-only processing; quality-optimized models; designed for unattended overnight runs
- Runs on Manjaro (Arch) and Ubuntu server via Docker

**Out of scope (v1):**
- Streaming or real-time processing
- GUI
- Subtitle file generation or modification
- Non-Latin-script languages

---

## 2. Constraints & Environment

| Machine | OS | Runtimes available | GPU |
|---|---|---|---|
| Workstation | Manjaro (Arch + AUR) | Docker, Flatpak, Nix, AUR | None |
| Server | Ubuntu | Docker only | None |

**Hard constraints:**
- No `pip` in the host environment. All Python dependency management happens *inside* Docker.
- Must be portable between both machines with no per-machine configuration.
- No Snap or Flatpak on the Ubuntu server.

**Resulting architecture decision:** Docker is the single deployment target. The host only needs a shell script wrapper and Docker itself. `pip` is an implementation detail inside the container and invisible to the user.

---

## 3. Technology Stack

### 3.1 Deployment
- **Docker** — primary runtime on both machines; CPU-only, no NVIDIA runtime required
- **Shell wrapper script** — host-side entry point; handles volume mounts and path resolution

### 3.2 Pipeline Tools (all inside container)

| Step | Tool | Notes |
|---|---|---|
| Extract audio | `ffmpeg` | system package in container |
| Separate dialog from score/SFX | `demucs` (`htdemucs_ft` model) | pip inside container; MIT licensed |
| Transcribe + word timestamps | `whisperx` | pip inside container; wraps faster-whisper + wav2vec2 |
| SRT cross-reference | custom Python module | uses `pysrt` + fuzzy matching |
| Mute profanity in dialog stem | `ffmpeg` volume filter | generated filter string from transcript |
| Recombine dialog + score/SFX | `ffmpeg` | simple amix |
| Mux audio back to video | `ffmpeg` | copy video stream, replace audio |

### 3.3 Rationale: Demucs over Spleeter
Spleeter has had no major updates since 2019 and is effectively unmaintained. Demucs (Meta Research) is actively developed, MIT licensed, significantly higher quality, and pip-installable inside the container. The `htdemucs_ft` model (fine-tuned Hybrid Transformer v4) is the quality-optimized variant. The `--two-stems=vocals` flag produces exactly two output stems: `vocals.wav` (dialog) and `no_vocals.wav` (score + sound effects), which maps cleanly onto this pipeline.

### 3.4 Rationale: WhisperX over plain Whisper
WhisperX provides **word-level timestamps** (not just segment-level), which is essential for precise muting. It uses `faster-whisper` under the hood for speed, plus a `wav2vec2` phoneme alignment pass for accurate per-word start/end times. Plain Whisper only provides segment-level timestamps, which would require muting entire phrases.

---

## 4. Pipeline Architecture

```
INPUT: video.mkv  [+ optional: subtitles.srt]
          │
          ▼
┌─────────────────────┐
│  STEP 1: Extract    │  ffmpeg → audio_raw.wav (16-bit PCM, 44.1kHz stereo)
└─────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 2: Separate   │  demucs htdemucs_ft --two-stems=vocals
│                     │  → dialog.wav
│                     │  → score_sfx.wav
└─────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 3: Transcribe │  whisperx dialog.wav → transcript.json
│  (word timestamps)  │  [word, start_sec, end_sec, confidence]
└─────────────────────┘
          │
          ▼
┌─────────────────────┐   (optional)
│  STEP 4: SRT align  │  cross-reference transcript.json ↔ subtitles.srt
│                     │  → transcript_aligned.json
└─────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 5: Flag &     │  match words against word_list.txt
│  mute dialog stem   │  ffmpeg volume filter → dialog_censored.wav
└─────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 6: Recombine  │  ffmpeg amix dialog_censored.wav + score_sfx.wav
│  audio              │  → audio_censored.wav
└─────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 7: Mux to     │  ffmpeg -c:v copy → video_censored.mkv
│  video              │
└─────────────────────┘

OUTPUT: video_censored.mkv
```

**Why mute only the dialog stem (not the full mix)?**  
Muting at the separation stage means background music and sound effects continue uninterrupted during censored moments, resulting in more natural-sounding output. This was the user's existing approach and is preserved here.

---

## 5. Docker Strategy

### 5.1 Image Design

Single CPU-only image. No CUDA, no nvidia-container-toolkit required on either host machine. The image is leaner as a result — PyTorch CPU-only wheels are ~200 MB vs. 2+ GB for CUDA builds.

```
Dockerfile
├── FROM python:3.11-slim
├── apt: ffmpeg, git, libsndfile1
├── pip: torch (cpu build), demucs, whisperx, pysrt, tqdm
└── COPY src/ /app/
    ENTRYPOINT ["python", "/app/pipeline.py"]
```

Since speed is not a concern, both Demucs and WhisperX are configured to run at their
highest quality settings by default (see §7.1). Processing a feature-length film
will take several hours on CPU; this is expected and acceptable for overnight runs.

### 5.2 Volume Mounts

The container is stateless. Files are passed in/out via mounts:

```
/input   ← host directory containing the video (and optional .srt)
/output  ← host directory for censored output
/config  ← host directory containing config.yaml and word_list.txt
/cache   ← host directory for model weight cache (persist between runs!)
```

Persisting `/cache` is important — Demucs and WhisperX models are several GB and should not be re-downloaded on each run.

### 5.3 Wrapper Script (`censor.sh`)

No GPU detection needed. The wrapper resolves absolute paths (Docker requires them for `-v`)
and launches the container:

```bash
docker run --rm \
    -v "$INPUT_DIR:/input:ro" \
    -v "$OUTPUT_DIR:/output" \
    -v "$CONFIG_DIR:/config:ro" \
    -v "$CACHE_DIR:/cache" \
    autocensor "$@"
```

---

## 6. Repository Structure

```
autocensor/
├── Dockerfile
├── docker-compose.yml          # convenience wrapper (workstation use)
├── censor.sh                   # host-side entry point
│
├── config/
│   ├── config.yaml             # pipeline settings (see §7)
│   └── word_list.txt           # one word/phrase per line, case-insensitive
│
├── src/
│   ├── pipeline.py             # orchestrator; runs steps 1–7 in order
│   ├── steps/
│   │   ├── extract.py          # step 1: ffmpeg audio extraction
│   │   ├── separate.py         # step 2: demucs wrapper
│   │   ├── transcribe.py       # step 3: whisperx wrapper
│   │   ├── align_srt.py        # step 4: optional SRT cross-reference
│   │   ├── mute.py             # step 5: flag words, generate ffmpeg filter
│   │   ├── recombine.py        # step 6: amix stems
│   │   └── mux.py              # step 7: mux audio to video
│   └── utils.py                # shared helpers (logging, temp file mgmt)
│
├── tests/
│   ├── samples/                # short test clips (5–10s)
│   └── test_pipeline.py
│
└── README.md
```

---

## 7. Configuration Specification

### 7.1 `config/config.yaml`

```yaml
# Model settings
demucs:
  model: htdemucs_ft            # htdemucs | htdemucs_ft | htdemucs_6s
                                # htdemucs_ft: fine-tuned 4-stem, best quality (default)
                                # htdemucs_6s: 6-stem, may give better dialog isolation
                                #   on dense action-film mixes; significantly slower
  device: cpu
  shifts: 4                     # random temporal shift averaging; higher = better quality
                                # at proportional compute cost. 4 is a good overnight default.

whisperx:
  model: large-v2               # large-v2 recommended for reliability
                                # large-v3 available but has known regression cases on some audio
  language: en                  # ISO 639-1; null for auto-detect
  batch_size: 4                 # lower than GPU default; tunes CPU memory pressure
  beam_size: 5                  # beam search width; higher = more accurate, slower
  device: cpu

# Alignment (step 4)
srt:
  enabled: true                 # use SRT file if present alongside input video
  strategy: whisperx_primary    # whisperx_primary | srt_primary | highest_confidence
  fuzzy_threshold: 85           # 0–100; minimum string similarity to accept SRT match

# Censoring behavior
censoring:
  method: mute                  # mute | beep
  beep_frequency_hz: 1000       # only used when method: beep
  padding_ms: 50                # ms of silence/beep added before and after each word
  word_list: /config/word_list.txt

# Output
output:
  suffix: _censored             # appended to input filename before extension
  format: mkv                   # mkv | mp4
  keep_intermediates: false     # keep temp WAV files after run (useful for debugging)
  log_level: info               # debug | info | warning
```

### 7.2 `config/word_list.txt`

Plain text, one entry per line. Case-insensitive. Lines beginning with `#` are ignored. Entries may be single words or short phrases.

```
# word_list.txt — customize to taste
fuck
fucking
shit
...
```

### 7.3 Environment Variables (override config.yaml)

| Variable | Description |
|---|---|
| `AC_LOG_LEVEL` | `debug`, `info`, `warning` |
| `AC_KEEP_INTERMEDIATES` | `1` to keep temp WAV files after run |

---

## 8. Module Specifications

### `pipeline.py`
- Parses CLI arguments (input path, optional SRT path, optional config override)
- Creates a per-run temp directory under `/output/.autocensor_tmp/`
- Calls each step module in sequence, passing the temp dir as working space
- Handles step failures: log error, clean up temp dir, exit with non-zero code
- On success: moves final output to `/output/`, removes temp dir (unless `keep_intermediates`)

### `steps/extract.py`
- **Input:** video file path
- **Output:** `audio_raw.wav` (44100 Hz, stereo, PCM 16-bit)
- **Tool:** `ffmpeg -i input -vn -ar 44100 -ac 2 -c:a pcm_s16le audio_raw.wav`
- Validates that input file exists and ffmpeg can read it before proceeding

### `steps/separate.py`
- **Input:** `audio_raw.wav`
- **Output:** `dialog.wav`, `score_sfx.wav`
- **Tool:** `python -m demucs --two-stems=vocals -n {model} --shifts {shifts} -d cpu -o {tmpdir} audio_raw.wav`
- `--shifts` averaging is set to 4 by default (config) for best quality on overnight runs
- Demucs outputs to a subdirectory named after the model; this module renames/moves to flat expected paths
- Logs estimated completion time based on file duration (rough: ~2–3× realtime per shift on modern CPU)

### `steps/transcribe.py`
- **Input:** `dialog.wav`
- **Output:** `transcript.json`
- **Format:**
```json
{
  "language": "en",
  "words": [
    { "word": "example", "start": 12.34, "end": 12.78, "score": 0.97 }
  ]
}
```
- **Tool:** whisperx Python API
- Uses `align()` for word-level timestamps after initial transcription pass
- Lowercases all words before writing to JSON

### `steps/align_srt.py`
- **Input:** `transcript.json`, `subtitles.srt` (optional)
- **Output:** `transcript_aligned.json` (same schema; updated timings where SRT confidence wins)
- **Logic:** For each word in transcript flagged as a potential profanity match, check if an SRT segment covers that time range. If fuzzy text match score ≥ threshold, update timing using SRT segment boundaries. This is a refinement step, not a replacement.
- **Skipped entirely** if no SRT file is provided or `srt.enabled: false`

### `steps/mute.py`
- **Input:** `transcript_aligned.json` (or `transcript.json`), `dialog.wav`
- **Output:** `dialog_censored.wav`, `censor_log.json`
- **Logic:**
  1. Load word list (lowercase, stripped)
  2. Walk word list from transcript; match against profanity list (exact match, case-insensitive)
  3. For each match, compute `[start - padding_ms, end + padding_ms]` interval
  4. Merge overlapping intervals
  5. Build ffmpeg `volume` filter expression:
     `volume=enable='between(t,s1,e1)+between(t,s2,e2)+...':volume=0`
  6. For `beep` method: additionally mix in a sine tone over the same intervals
- **`censor_log.json`** records every word muted with its timestamp — useful for review
- If zero matches found, `dialog_censored.wav` is a copy of `dialog.wav` and a warning is logged

### `steps/recombine.py`
- **Input:** `dialog_censored.wav`, `score_sfx.wav`
- **Output:** `audio_censored.wav`
- **Tool:** `ffmpeg -i dialog_censored.wav -i score_sfx.wav -filter_complex amix=inputs=2:duration=first:normalize=0 audio_censored.wav`
- `normalize=0` preserves original relative levels

### `steps/mux.py`
- **Input:** original video file, `audio_censored.wav`
- **Output:** `{original_name}{suffix}.{format}` in `/output/`
- **Tool:** `ffmpeg -i video.mkv -i audio_censored.wav -c:v copy -c:a aac -b:a 320k -map 0:v:0 -map 1:a:0 output.mkv`
- Video stream is copied bitstream-exact (no re-encode)
- Audio is encoded to AAC 320k (or passthrough if source was AAC and no quality loss acceptable — TBD)

---

## 9. Host-Side CLI (`censor.sh`)

```
Usage: censor.sh [OPTIONS] <input_video> [subtitle_file]

Options:
  -o, --output DIR     Output directory (default: same as input)
  -c, --config DIR     Config directory (default: ~/.config/autocensor)
  --cache DIR          Model cache directory (default: ~/.cache/autocensor)
  --keep-tmp           Keep intermediate WAV files after run
  --dry-run            Print the docker command without running it
  -h, --help

Examples:
  censor.sh movie.mkv
  censor.sh movie.mkv movie.srt
  censor.sh -o ~/censored/ movie.mkv movie.srt
```

The script resolves absolute paths before mounting — Docker requires absolute paths for `-v`.

---

## 10. Deliverables

### Phase 1 — Docker Foundation
- [ ] `Dockerfile` (CPU-only, single target)
- [ ] `docker-compose.yml` for workstation convenience
- [ ] `censor.sh` wrapper script
- [ ] `config/config.yaml` with documented defaults
- [ ] `config/word_list.txt` with a reasonable default English list
- [ ] `README.md`: build, install, basic usage, expected runtimes

### Phase 2 — Core Pipeline
- [ ] `steps/extract.py`
- [ ] `steps/separate.py`
- [ ] `steps/transcribe.py`
- [ ] `steps/mute.py`
- [ ] `steps/recombine.py`
- [ ] `steps/mux.py`
- [ ] `pipeline.py` orchestrator
- [ ] `censor_log.json` output per run
- [ ] End-to-end test with a short sample clip

### Phase 3 — SRT Integration
- [ ] `steps/align_srt.py`
- [ ] SRT auto-detection (look for `.srt` alongside input video)
- [ ] Config options for SRT strategy and fuzzy threshold

### Phase 4 — Polish
- [ ] Beep replacement mode (sine tone)
- [ ] `--dry-run` flag (show what would be muted without writing output)
- [ ] Progress reporting (step names + estimated time)
- [ ] Batch processing support (`censor.sh *.mkv`)

---

## 11. Open Questions

| # | Question | Impact | Notes |
|---|---|---|---|
| 1 | ~~GPU available on workstation?~~ | ~~Determines default model size and expected runtimes~~ | **Resolved: no GPU on either machine. CPU-only.** |
| 2 | Audio encode on mux: AAC re-encode or copy? | Quality vs. compatibility | Copy only works if source is already AAC; most MKVs use AC3 or DTS — re-encode to AAC 320k is the safe default |
| 3 | Demucs quality on heavy film mixes? | May need `htdemucs_6s` (6-stem) for better dialog isolation in dense action scenes | Test on representative clips; add as a config option |
| 4 | ~~WhisperX model size vs. accuracy tradeoff~~ | ~~`large-v2` recommended but slower; `medium` may suffice~~ | **Resolved: quality > speed. Default to `large-v2`. `large-v3` is an available option in config but has known regression cases.** |
| 5 | False negatives acceptable? | If Whisper mishears a word, it won't be censored | SRT cross-reference in Phase 3 mitigates this |
| 6 | How to handle foreign-language films? | Whisper supports many languages; word list would need translation | Config `language` field supports this; out of scope v1 |
| 7 | Padding duration on muted words | Too short = audible clipping; too long = mutes adjacent dialog | Default 50ms; may need tuning per film |

---

## 12. Known Limitations (v1)

- **Separation artifacts:** Demucs is excellent but not perfect. Some bleed between dialog and score/SFX stems will occur, especially in scenes with overlapping dialog and dramatic music. The recombined audio will not be bit-for-bit identical to the original even in uncensored sections.
- **Homophone/mishearing false positives:** WhisperX may occasionally transcribe an innocent word as a profanity match. The `censor_log.json` exists to let you spot-check this.
- **Proper nouns:** Word list matching is simple string equality. A character named "Damm" would not be falsely muted, but slang that doesn't appear in the word list won't be caught.
- **Overlapping dialog:** Scenes where multiple people speak simultaneously will have reduced Whisper accuracy.
- **Processing time:** CPU-only is slow by design. Rough estimates for a 2-hour film: Demucs `htdemucs_ft` with `--shifts 4` ≈ 4–8 hours; WhisperX `large-v2` ≈ 30–60 minutes. Total wall-clock time of 5–10 hours is expected and by design — runs are queued overnight.

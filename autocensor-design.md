# profanity-hush — Design Document
 
**Project:** Automated movie profanity censoring pipeline
**Version:** 0.5
**Status:** Phase 1 tentatively complete; Phase 2 pending
 
---
 
## 1. Goals & Scope
 
Automate the end-to-end process of censoring profanity from a video file, replacing the current manual workflow of: watch film → identify curse words → mute in audio editor → recombine.
 
**In scope:**
- Full pipeline from raw video file to censored output video file
- Configurable word list
- Optional SRT/subtitle cross-reference for improved accuracy
- Interactive review mode: inspect and approve/reject flagged words before processing
- CPU-only processing; quality-optimized models; designed for unattended overnight runs
- Runs on Manjaro (Arch) and Ubuntu server via Docker
**Out of scope (v1):**
- Streaming or real-time processing
- GUI
- SRT file editing (substitution, correction) — deferred; see §13.1
- Context-aware profanity detection — deferred; see §13.2
- Audio word substitution (TTS/voice replacement) — deferred; see §13.5
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
| Extract audio | `ffmpeg` | system package in container; downmixes multi-channel to stereo |
| Separate dialog from score/SFX | `demucs` (`htdemucs_ft` model) | pip inside container; MIT licensed |
| Transcribe + word timestamps | `whisperx` | pip inside container; wraps faster-whisper + wav2vec2 |
| SRT cross-reference | custom Python module | uses `pysrt` + fuzzy matching |
| Interactive review | custom Python module | terminal UI; present flagged words for approval before muting |
| Mute profanity in dialog stem | `ffmpeg` volume filter | generated filter string from approved transcript entries |
| Recombine dialog + score/SFX | `ffmpeg` | simple amix |
| Mux audio back to video | `ffmpeg` | copy video stream, replace audio |
 
### 3.3 V1 Implementation: Demucs
Spleeter has had no major updates since 2019 and is effectively unmaintained. Demucs (Meta Research) is actively developed, MIT licensed, significantly higher quality, and pip-installable inside the container. The `htdemucs_ft` model (fine-tuned Hybrid Transformer v4) is the quality-optimized variant. The `--two-stems=vocals` flag produces exactly two output stems: `vocals.wav` (dialog) and `no_vocals.wav` (score + sound effects), which maps cleanly onto this pipeline.
 
Demucs is the initial implementation chosen for v1. The pipeline architecture intentionally treats audio separation as a replaceable backend.
 
### 3.4 V1 Implementation: WhisperX
WhisperX provides **word-level timestamps** (not just segment-level), which is essential for precise muting. It uses `faster-whisper` under the hood for speed, plus a `wav2vec2` phoneme alignment pass for accurate per-word start/end times. Plain Whisper only provides segment-level timestamps, which would require muting entire phrases.
 
WhisperX is the initial transcription backend selected for v1. Future implementations may replace it provided they can produce equivalent word-level timestamp data.
 
### 3.5 Backend Abstraction
 
The specific tools used for audio separation and transcription are implementation choices rather than architectural requirements.
 
The pipeline is designed around stable interfaces between stages:
 
| Function | Interface Requirement | V1 Implementation |
|----------|----------------------|-------------------|
| Audio Separation | Produce dialog stem and background stem from source audio | Demucs |
| Speech Recognition | Produce transcript with word-level timestamps and confidence scores | WhisperX |
| Subtitle Alignment | Produce corrected transcript timing data | Custom Python module |
 
Future versions may substitute alternative implementations provided they satisfy the same interface contracts.
 
Examples:
 
- UVR
- MDX-Net
- Future dialogue-specific source separation models
- Alternative timestamp-capable speech recognition systems
This abstraction ensures that improvements in underlying ML tooling do not require architectural redesign.
 
---
 
## 4. Pipeline Architecture
 
```
INPUT: video.mkv  [+ optional: subtitles.srt]
          │
          ▼
┌─────────────────────┐
│  STEP 1a: Extract   │  ffmpeg -c:a copy → audio_raw.{ext}
│  raw audio          │  Bitstream copy; native codec, native channels.
│                     │  Saved to job store. Always kept.
└─────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 1b: Downmix   │  ffmpeg → audio_stereo.wav (44.1kHz, stereo, PCM 16-bit)
│  to stereo          │  Decoded and downmixed from audio_raw for all subsequent steps.
│                     │  Disposable intermediate (fast to regenerate from audio_raw).
└─────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 2: Separate   │  demucs htdemucs_ft --two-stems=vocals
│                     │  → dialog.wav      (stereo)
│                     │  → score_sfx.wav   (stereo)
└─────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 3: Transcribe │  whisperx dialog.wav → transcript.json
│  (word timestamps)  │  [word, start_sec, end_sec, confidence]
│                     │  WhisperX converts to mono 16kHz internally.
└─────────────────────┘
          │
          ▼
┌─────────────────────┐   (optional)
│  STEP 4: SRT align  │  cross-reference transcript.json ↔ subtitles.srt
│                     │  → transcript_aligned.json
└─────────────────────┘
          │
          ▼
┌─────────────────────┐   (optional; skipped in unattended mode)
│  STEP 4b: Review    │  present flagged words in terminal
│                     │  user approves / rejects / adds entries
│                     │  → review.json
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
 
OUTPUT: video_censored.mkv  [+ job record in jobs store]
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
├── pip: torch (cpu build), demucs, whisperx, faster-whisper, pysrt, rapidfuzz, tqdm, pyyaml
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
/jobs    ← host directory for job history, intermediate files, and logs
```
 
Persisting `/cache` is important — Demucs and WhisperX models are several GB and should not be re-downloaded on each run. Persisting `/jobs` is important for the correction workflow (§13.4): intermediate files stored there allow future re-runs to skip the expensive extract/separate/transcribe steps.
 
### 5.3 Wrapper Script (`hush.sh`)
 
No GPU detection needed. The wrapper resolves absolute paths (Docker requires them for `-v`)
and launches the container:
 
```bash
docker run --rm \
    -v "$INPUT_DIR:/input:ro" \
    -v "$OUTPUT_DIR:/output" \
    -v "$CONFIG_DIR:/config:ro" \
    -v "$CACHE_DIR:/cache" \
    -v "$JOBS_DIR:/jobs" \
    profanity-hush "$@"
```
 
---
 
## 6. Repository Structure
 
```
profanity-hush/
├── Dockerfile
├── docker-compose.yml          # convenience wrapper (workstation use)
├── hush.sh                     # host-side entry point
│
├── config/
│   ├── config.yaml             # pipeline settings (see §7)
│   └── word_list.txt           # word/phrase match list; see §7.2 for format notation
│
├── src/
│   ├── pipeline.py             # orchestrator; runs steps 1–7 in order; manages job state
│   ├── steps/
│   │   ├── extract.py          # step 1a+1b: bitstream copy + stereo downmix
│   │   ├── separate.py         # step 2: demucs wrapper
│   │   ├── transcribe.py       # step 3: whisperx wrapper
│   │   ├── align_srt.py        # step 4: optional SRT cross-reference
│   │   ├── review.py           # step 4b: interactive terminal review
│   │   ├── mute.py             # step 5: flag words, generate ffmpeg filter
│   │   ├── recombine.py        # step 6: amix stems
│   │   └── mux.py              # step 7: probe codec, mux audio to video
│   └── utils.py                # shared helpers (logging, job state, path mgmt)
│
├── tests/
│   ├── samples/                # short test clips (5–10s)
│   └── test_pipeline.py
│
└── README.md
```
 
### Job Store Design Principle
 
Expensive processing stages should only be performed once.
 
Audio extraction, source separation, transcription, subtitle alignment, and review outputs are preserved in the job store so that future reprocessing can reuse existing artifacts rather than repeating computationally expensive operations.
 
This principle is particularly important for CPU-only deployments where a full pipeline run may take several hours.
 
**Job store** (on the host, mounted at `/jobs` inside the container):
```
~/.local/share/profanity-hush/jobs/
└── {job_id}/                   # job_id = sha256[:12] of input file path + mtime
    ├── job.json                # metadata: input file, config snapshot, step completion status
    ├── audio_raw.{ext}         # bitstream copy of original audio; always kept; ext = source codec
    ├── transcript.json         # whisperx output; always kept
    ├── transcript_aligned.json # post-SRT alignment; always kept (if applicable)
    ├── review.json             # post-interactive-review; always kept (if applicable)
    ├── censor_log.json         # record of every word muted, with timestamps; always kept
    ├── audio_stereo.wav        # stereo downmix used for demucs (large; keep_intermediates only)
    ├── dialog.wav              # demucs dialog stem (large; keep_intermediates only)
    └── score_sfx.wav           # demucs score+SFX stem (large; keep_intermediates only)
```
 
`audio_raw.{ext}` and all `transcript*.json` / `censor_log.json` files are always kept regardless of `keep_intermediates` — they are small (or in `audio_raw`'s case, already compressed in its native codec) and are the essential resume artifacts. Large decoded WAV intermediates are kept only when explicitly requested.
 
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
 
# Audio handling
audio:
  # v1: multi-channel sources are always downmixed to stereo for processing.
  # The original audio is preserved as-is in the job store (bitstream copy).
  # Future: downmix may be replaced by intelligent per-channel splitting (§13.3).
 
# Alignment (step 4)
srt:
  enabled: true                 # use SRT file if present alongside input video
  strategy: whisperx_primary    # whisperx_primary | srt_primary | highest_confidence
  fuzzy_threshold: 85           # 0–100; minimum string similarity to accept SRT match
 
# Interactive review (step 4b)
interactive:
  enabled: false                # true to pause and review flagged words before muting
                                # override with --interactive flag on the CLI
  show_context_words: 8         # words of surrounding context to display per flagged entry
  min_confidence_for_prompt: 0.0 # only prompt for entries at or below this confidence
                                 # 0.0 = prompt for all; 1.0 = never prompt (same as disabled)
 
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
  keep_intermediates: false     # keep large WAV stems after run (transcript JSONs always kept)
  log_level: info               # debug | info | warning
 
# Job storage
storage:
  jobs_dir: /jobs               # mount point; maps to host ~/.local/share/profanity-hush/jobs
```
 
### 7.2 `config/word_list.txt`
 
Plain text, one entry per line. Lines beginning with `#` and blank lines are ignored. Entries may be single words or multi-word phrases.
 
#### Match Method Notation
 
Each entry may carry an optional prefix and/or suffix that controls how it is matched against transcript tokens. The default (no decoration) is a case-insensitive exact token match.
 
| Notation | Match type | Example | Matches |
|---|---|---|---|
| `word` | Exact, case-insensitive | `crap` | crap, Crap, CRAP |
| `=word` | Exact, case-sensitive | `=dick` | dick only — not Dick |
| `=Word` | Exact, case-sensitive | `=Dick` | Dick only — not dick |
| `word*` | Starts-with, case-insensitive | `crap*` | crap, crapy, craphead, … |
| `*word*` | Contains / substring, case-insensitive | `*freak*` | freak, freaking, motherfreaker, freaktard, … |
 
Notations may be combined: `=Crap*` is a case-sensitive starts-with match, though in practice this is rarely needed.
 
**Phrase entries** (entries containing spaces) support `=` prefix for case-sensitive phrase matching. The `*` suffix/contains notation is not supported on individual words within a phrase in v1 — the entire phrase is matched as a case-insensitive exact token sequence by default, or case-sensitive with `=`.
 
**Implementation note for `steps/mute.py`:** exact entries use `==`, `word*` uses `str.startswith()`, and `*word*` uses the root string as a substring check (`root in token`). Case-insensitive comparisons fold both sides to lowercase before comparing; `=` entries compare the token's original casing against the entry's casing as written.
 
#### Case-Sensitive Matching and Transcript Casing
 
WhisperX capitalizes proper nouns and sentence-initial words naturally. For example, a character named Dick will generally appear as `Dick` in the transcript, while the profane usage will appear as `dick`. This makes `=`-prefixed entries a reliable mechanism for distinguishing proper nouns from profanity.
 
**Important:** case-sensitive matching requires `steps/transcribe.py` to preserve WhisperX's original word casing in `transcript.json`. See the `steps/transcribe.py` spec in §8 — the prior behavior of lowercasing all words before writing to JSON must be dropped.
 
#### When to Use Each Notation
 
- **`word` (default)** — use for most entries where capitalization is not meaningful.
- **`=word`** — use when the lowercase form is profane but the title-case form is a common proper noun. The canonical example is `=dick` (body part) vs. `Dick` (name).
- **`word*`** — use for roots where creative inflections are likely in speech but the root rarely starts innocent words. Good candidates: `jerk*`, `idiot*`, `bench*`, `crap*`.
- **`*word*`** — use sparingly, only for roots that can appear embedded mid-word AND have no false-positive risk. The canonical example is `*freak*` (no common English word contains "freak" outside of profanity).
#### Example entries
 
```
# Exact, case-insensitive (default)
butt
heck
what
 
# Exact, case-sensitive — catches the profane form, not the proper noun
=dick
=dicks
 
# Starts-with, case-insensitive
crap*          # catches crap, crapy, craphead, craping, ...
bench*         # catches bench, benches, benching, ...
jerk*       # catches jerk, jerks, ...
 
# Contains / substring, case-insensitive
*freak*         # catches freak, freaking, motherfreaker, freaktard, freakalicious, ...
 
# Phrase (exact token sequence, case-insensitive)
son of a bench
holy crap
```
 
### 7.3 Environment Variables (override config.yaml)
 
| Variable | Description |
|---|---|
| `AC_LOG_LEVEL` | `debug`, `info`, `warning` |
| `AC_KEEP_INTERMEDIATES` | `1` to keep large WAV stem files after run |
| `AC_INTERACTIVE` | `1` to enable interactive review mode |
 
---
 
## 8. Module Specifications
 
### `pipeline.py`
- Parses CLI arguments (input path, optional SRT path, optional config override)
- Computes a `job_id` = `sha256[:12]` of the input file's absolute path + mtime; creates a job directory under `storage.jobs_dir/{job_id}/`
- Writes `job.json` at job start with: input path, config snapshot, timestamp, and a `steps_completed: []` list
- Calls each step module in sequence, updating `steps_completed` in `job.json` after each successful step
- Handles step failures: log error with step name and exception, update `job.json` with failure info, exit with non-zero code
- On success: moves final output to `/output/`, marks job complete in `job.json`
- Always preserves transcript JSON files in the job directory; removes large WAV stems unless `keep_intermediates` is set
**Groundwork for future correction workflow (§13.4):** The `steps_completed` field in `job.json`, combined with preserved transcript files, is the foundation for a future `--resume` mode that can skip the expensive Steps 1–4 and re-run only from Step 5 onward with a manually edited transcript.
 
### `steps/extract.py` *(Steps 1a and 1b)*
 
Two distinct functions, called in sequence by the pipeline orchestrator and tracked as separate entries in `steps_completed`.
 
**`extract_raw(video_path, job_dir)`** *(Step 1a)*
- Probes audio codec and channel layout via `ffprobe`; records both in `job.json`
- Extracts audio as a bitstream copy — no decode, no re-encode:
  ```bash
  ffmpeg -i video.mkv -vn -c:a copy {job_dir}/audio_raw.{ext}
  ```
- The output extension is determined by the probed codec (e.g., `.ac3`, `.dts`, `.aac`, `.truehd`)
- Result is byte-identical to the audio stream as stored in the container
- Always written to the job store; never deleted
**`downmix_to_stereo(job_dir)`** *(Step 1b)*
- Decodes `audio_raw.{ext}` and downmixes to stereo PCM:
  ```bash
  ffmpeg -i audio_raw.{ext} -ac 2 -ar 44100 -c:a pcm_s16le audio_stereo.wav
  ```
- `-ac 2` handles any channel layout (stereo passthrough, mono upmix, 5.1/7.1 downmix)
- Output is a temporary intermediate; regenerable from `audio_raw` in seconds
- Kept only if `keep_intermediates` is set; otherwise deleted after Step 2 completes
- This is v1's deliberate boundary for multi-channel handling — see §13.3 for the future path
### `steps/separate.py`
- **Input:** `audio_stereo.wav` (the stereo downmix from Step 1b)
- **Output:** `dialog.wav`, `score_sfx.wav`
- **Tool:** `python -m demucs --two-stems=vocals -n {model} --shifts {shifts} -d cpu -o {tmpdir} audio_stereo.wav`
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
    { "word": "example", "start": 12.34, "end": 12.78, "confidence": 0.97 }
  ]
}
```
- **Tool:** whisperx Python API
- Uses `align()` for word-level timestamps after initial transcription pass
- Preserves WhisperX's original word casing in the JSON output. **Do not lowercase words before writing.** Original casing is required for case-sensitive word list entries (see §7.2). WhisperX naturally capitalizes proper nouns and sentence-initial words, which is the signal used by `=`-prefixed entries in the word list to distinguish proper nouns from profanity (e.g. `Dick` vs `dick`).
### `steps/align_srt.py`
- **Input:** `transcript.json`, `subtitles.srt` (optional)
- **Output:** `transcript_aligned.json` (same schema; updated timings where SRT confidence wins)
- **Logic:** For each word in transcript flagged as a potential profanity match, check if an SRT segment covers that time range. If fuzzy text match score ≥ threshold, update timing using SRT segment boundaries. This is a refinement step, not a replacement.
- **Skipped entirely** if no SRT file is provided or `srt.enabled: false`
### `steps/review.py` *(Step 4b — Interactive Review)*
- **Input:** `transcript_aligned.json` (or `transcript.json`); word list
- **Output:** `review.json` (same schema; entries may be added, removed, or marked `skip: true`)
- **Activated by:** `--interactive` CLI flag or `interactive.enabled: true` in config
- **Skipped entirely** in unattended mode; `transcript_aligned.json` is used directly by Step 5
**Terminal UI behavior:**
 
For each word in the transcript that matches the word list, display a review entry:
 
```
[3 of 11]  Word: "crap"  |  Confidence: 0.94  |  Time: 00:23:14.8 – 00:23:15.1
Context: "...and then he said crap right in front of..."
Action? [Y]es / [N]o / [A]dd word / [S]kip rest / [Q]uit  >
```
 
- **Y (default):** approve; word will be muted
- **N:** reject; word will not be muted; entry marked `skip: true` in reviewed transcript
- **A:** prompt for an additional word/timestamp to add (manual false-negative correction)
- **S:** approve all remaining flagged entries without prompting
- **Q:** abort the run without writing output (no changes made)
After the review loop, a summary is printed:
```
Review complete: 9 approved, 2 rejected, 1 added.
Proceeding to mute step.
```
 
**`show_context_words`** (config): controls how many words of surrounding transcript are shown on each side of the flagged entry.
 
**`min_confidence_for_prompt`** (config): if set above 0.0, entries with confidence *above* the threshold are auto-approved without prompting; only lower-confidence entries require human review. Useful for reducing review burden when most detections are high-confidence.
 
**`review.json` contents**
 
{
  "overrides": [
    {
      "word_index": 412,
      "action": "skip"
    },
    {
      "word_index": 913,
      "action": "add",
      "start": 1203.14,
      "end": 1203.48
    }
  ]
}
 
**Note on audio playback:** Displaying a playable audio snippet during review is a natural future enhancement (§13.4) but is out of scope for v1 due to the complexity of audio output from inside a Docker container.
- **Input:** `transcript_aligned.json` (or `transcript.json`), `dialog.wav`
- **Output:** `dialog_censored.wav`, `censor_log.json`
- **Logic:**
  1. Load and parse word list. For each entry, determine match type from notation:
     - No prefix/suffix → exact, case-insensitive
     - `=` prefix → exact, case-sensitive
     - `word*` (trailing `*`, no leading `*`) → starts-with, case-insensitive
     - `*word*` (leading and trailing `*`) → substring/contains, case-insensitive
     - `=word*` → starts-with, case-sensitive (uncommon)
  2. Walk transcript words. For each token, test against all word list entries using the entry's match type:
     - Case-insensitive exact: `token.lower() == entry.lower()`
     - Case-sensitive exact: `token == entry` (as written, no case folding)
     - Starts-with: `token.lower().startswith(entry_root.lower())`
     - Contains: `entry_root.lower() in token.lower()`
  3. Phrase entries are matched by checking contiguous token sequences against the phrase's token sequence using the same case rules.
  4. For each match, compute `[start - padding_ms, end + padding_ms]` interval
  5. Merge overlapping intervals
  6. Build ffmpeg `volume` filter expression:
     `volume=enable='between(t,s1,e1)+between(t,s2,e2)+...':volume=0`
  7. For `beep` method: additionally mix in a sine tone over the same intervals
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
- **Video stream:** copied bitstream-exact (`-c:v copy`), no re-encode
- **Audio stream:** re-encoded to match the original audio track's codec, bitrate, sample
  rate, channel layout, and any audio delay offset — to minimize risk of A/V sync drift
**Probe phase** (runs before encoding):
 
```bash
ffprobe -v quiet -select_streams a:0 \
  -show_entries stream=codec_name,bit_rate,sample_rate,channels,channel_layout \
  -show_entries stream_tags=DELAY \
  -of json input.mkv
```
 
This determines the encoding parameters for the output. The following fields are captured
and passed to the encode phase:
 
| Field | Used for |
|---|---|
| `codec_name` | select ffmpeg encoder (see codec map below) |
| `bit_rate` | `-b:a` target bitrate |
| `sample_rate` | `-ar` resample target if WAV sample rate differs |
| `channels` / `channel_layout` | `-ac` / `-channel_layout` |
| `DELAY` tag | `-metadata:s:a:0 DELAY={value}` to preserve offset |
 
**Codec map** (original codec → ffmpeg encoder):
 
| Original codec | Encoder used | Notes |
|---|---|---|
| `aac` | `aac` (or `libfdk_aac` if available) | Most common in MP4/M4V |
| `ac3` | `ac3` | Common in MKV rips |
| `eac3` | `eac3` | Enhanced AC-3 |
| `dts` | `dca` | If unavailable in build, fall back to `ac3` |
| `mp3` | `libmp3lame` | |
| `flac` | `flac` | Lossless; preserves quality |
| `truehd` | *(fallback)* | Cannot be re-encoded by ffmpeg; fall back to `ac3` at highest original bitrate |
| `dts-hd ma` | *(fallback)* | Same; fall back to `ac3` |
 
When a fallback is used, a warning is logged clearly:
`[mux] WARNING: Original codec 'truehd' cannot be re-encoded. Falling back to ac3 at {bitrate}. Verify sync and quality before use.`
 
**Final ffmpeg command** (example for AC3 source):
```bash
ffmpeg \
  -i video.mkv \
  -i audio_censored.wav \
  -c:v copy \
  -c:a ac3 \
  -b:a {original_bitrate} \
  -ar {original_sample_rate} \
  -channel_layout {original_layout} \
  -map 0:v:0 \
  -map 1:a:0 \
  output.mkv
```
 
**Note on multiple audio tracks:** v1 processes only the primary audio stream (`a:0`).
All other audio streams (commentary tracks, alternate language, etc.) in the source
container are dropped in the output. Multi-track preservation is a future consideration.
 
---
 
## 9. Host-Side CLI (`hush.sh`)
 
```
Usage: hush.sh [OPTIONS] <input_video> [subtitle_file]
 
Options:
  -o, --output DIR     Output directory (default: same as input)
  -c, --config DIR     Config directory (default: ~/.config/profanity-hush)
  --cache DIR          Model cache directory (default: ~/.cache/profanity-hush)
  --jobs DIR           Job history directory (default: ~/.local/share/profanity-hush/jobs)
  --interactive        Pause for review of flagged words before muting
  --no-interactive     Override config; force unattended mode
  --keep-tmp           Keep intermediate WAV stem files after run
  --dry-run            Print the docker command without running it
  -h, --help
 
Examples:
  hush.sh movie.mkv
  hush.sh --interactive movie.mkv movie.srt
  hush.sh -o ~/censored/ movie.mkv movie.srt
```
 
The script resolves absolute paths before mounting — Docker requires absolute paths for `-v`.
 
---
 
## 10. Deliverables
 
### Phase 1 — Docker Foundation
- [x] `Dockerfile` (CPU-only, single target)
- [x] `docker-compose.yml` for workstation convenience
- [x] `hush.sh` wrapper script (with `--interactive` and `--jobs` flags)
- [x] `config/config.yaml` with documented defaults
- [x] `config/word_list.txt` with default English list (merged APF + orig; see §7.2 for format)
- [ ] `README.md`: build, install, basic usage, expected runtimes
### Phase 2 — Core Pipeline
- [ ] `steps/extract.py` (with multichannel downmix)
- [ ] `steps/separate.py`
- [ ] `steps/transcribe.py`
- [ ] `steps/review.py` (interactive terminal review)
- [ ] `steps/mute.py`
- [ ] `steps/recombine.py`
- [ ] `steps/mux.py` (with codec probing)
- [ ] `pipeline.py` orchestrator with job state management
- [ ] Job store: `job.json`, `transcript.json`, `censor_log.json` written per run
- [ ] End-to-end test with a short sample clip (unattended mode)
- [ ] End-to-end test with a short sample clip (interactive mode)
### Phase 3 — SRT Integration
- [ ] `steps/align_srt.py`
- [ ] SRT auto-detection (look for `.srt` alongside input video)
- [ ] Config options for SRT strategy and fuzzy threshold
### Phase 4 — Polish
- [ ] Beep replacement mode (sine tone)
- [ ] `--dry-run` flag (show what would be muted without writing output)
- [ ] Progress reporting (step names + estimated time)
- [ ] Batch processing support (`hush.sh *.mkv`)
---
 
## 11. Open Questions
 
| # | Question | Impact | Notes |
|---|---|---|---|
| 1 | ~~GPU available on workstation?~~ | ~~Determines default model size and expected runtimes~~ | **Resolved: no GPU on either machine. CPU-only.** |
| 2 | ~~Audio encode on mux: AAC re-encode or copy?~~ | ~~Quality vs. compatibility~~ | **Resolved: probe original codec with `ffprobe` and re-encode to match. See `steps/mux.py` spec. Lossless/obscure codecs (TrueHD, DTS-HD MA) fall back to AC3 with a logged warning.** |
| 3 | Demucs quality on heavy film mixes? | May need `htdemucs_6s` (6-stem) for better dialog isolation in dense action scenes | Test on representative clips; add as a config option |
| 4 | ~~WhisperX model size vs. accuracy tradeoff~~ | ~~`large-v2` recommended but slower; `medium` may suffice~~ | **Resolved: quality > speed. Default to `large-v2`. `large-v3` is an available option in config but has known regression cases.** |
| 5 | False negatives acceptable? | If Whisper mishears a word, it won't be censored | Interactive review mode (Step 4b) is the v1 mitigation; SRT cross-reference (Phase 3) adds a second layer |
| 6 | How to handle foreign-language films? | Whisper supports many languages; word list would need translation | Config `language` field supports this; out of scope v1 |
| 7 | Padding duration on muted words | Too short = audible clipping; too long = mutes adjacent dialog | Default 50ms; may need tuning per film |
| 8 | Multiple audio tracks in source container | v1 drops all non-primary audio tracks (commentary, alt languages); see mux.py note | Acceptable for personal use; multi-track preservation is a future consideration (see §13.3) |
 
---
 
## 12. Known Limitations (v1)
 
- **Separation artifacts:** Demucs is excellent but not perfect. Some bleed between dialog and score/SFX stems will occur, especially in scenes with overlapping dialog and dramatic music. The recombined audio will not be bit-for-bit identical to the original even in uncensored sections.
- **Multi-channel audio downmixed to stereo:** Source files with surround sound (5.1, 7.1, Atmos, etc.) are downmixed to stereo for processing. The output audio will be stereo regardless of the original channel count. The original audio stream is preserved bit-for-bit in the job store, so future per-channel reprocessing is possible without re-extracting from the video. See §13.3 for the intended future approach.
- **Homophone/mishearing false positives:** WhisperX may occasionally transcribe an innocent word as a profanity match. Interactive review mode exists specifically to catch these before they result in a muted output.
- **Context-blind matching:** Word list matching has no understanding of usage context. This is partially mitigated by case-sensitive (`=`) entries — for example, `=dick` flags the lowercase profane form while leaving `Dick` (a name, which WhisperX capitalizes) unflagged. However, this heuristic only works for words whose profane and proper-noun forms differ in capitalization. "God" (in prayer vs. as an expletive), "butt" (donkey vs. insult), and similar cases remain indistinguishable at the word-list level. Interactive review is the immediate mitigation for these; context-aware detection (§13.2) is the long-term solution.
- **Overlapping dialog:** Scenes where multiple people speak simultaneously will have reduced Whisper accuracy.
- **Processing time:** CPU-only is slow by design. Rough estimates for a 2-hour film: Demucs `htdemucs_ft` with `--shifts 4` ≈ 4–8 hours; WhisperX `large-v2` ≈ 30–60 minutes. Total wall-clock time of 5–10 hours is expected and by design — runs are queued overnight.
---
 
## 13. Future Work & Roadmap
 
Items in this section are explicitly outside v1 scope but are anticipated future directions. The v1 architecture is designed not to foreclose them. Where relevant, architectural notes describe what v1 already puts in place to make a future feature easier.
 
---
 
### 13.1 SRT Editing (v2 Candidate)
 
Two distinct but related capabilities; either can be implemented independently.
 
#### 13.1.1 Profanity Substitution in Subtitles
 
When a word is muted in the audio, the corresponding subtitle entry currently still displays the original word. A viewer with subtitles on would read the censored word even though they cannot hear it.
 
**Proposed behavior:** For each word muted in the audio, find the matching SRT cue and replace the flagged word with a configurable substitution. Examples:
 
| Original | Substitution options |
|---|---|
| freak | [censored] / frick / f*** |
| crap | [censored] / shoot / s*** |
| goddang | [censored] / dang / g****** |
 
The substitution strategy (euphemism, asterisk-redaction, or bracketed tag) would be configurable, either globally or per-word. A `substitutions` section would be added to `config.yaml`, with a fallback of `[censored]` for any flagged word not explicitly listed.
 
**Architectural note:** The v1 pipeline already identifies which words are flagged and at what timestamps. The SRT cue containing that timestamp can be located using the same interval-matching logic already in `steps/align_srt.py`. The SRT substitution step would be a natural addition after Step 5 (mute), writing a modified `.srt` file alongside the censored video.
 
#### 13.1.2 SRT Correction / Reconciliation Against Transcript
 
Published subtitle files are not always accurate. Discrepancies arise from:
- Ad-lib performance diverging from the shooting script (the source of many subtitle files)
- Transcription errors in the original subtitle authoring
- Localization or region-specific subtitle variants that don't match the audio
**Proposed behavior:** Use the WhisperX transcript (which reflects what was actually spoken) to identify and flag divergences from the SRT file. Output options:
 
- **Report mode:** Write a diff file listing SRT segments where the subtitle text and transcript diverge significantly, for human review.
- **Auto-correct mode:** Replace SRT cue text with the WhisperX transcription where confidence exceeds a threshold and divergence is detected. Preserve original timing unless forced to adjust.
- **Hybrid mode:** Auto-correct high-confidence divergences; flag low-confidence ones for review.
This is a materially more complex feature than audio censoring — SRT cue boundaries don't map 1:1 to WhisperX word-level segments, and resolving multi-word realignments requires careful diff logic. It is best treated as a standalone sub-project built on top of the transcript data v1 already produces.
 
**Architectural note:** `transcript.json` (WhisperX output) and the parsed SRT are both already present in the pipeline by Step 4. No new data collection is needed; SRT reconciliation is a new consumer of existing pipeline outputs.
 
---
 
### 13.2 Context-Aware Profanity Detection (Stretch Goal)
 
V1 uses simple word-list matching: if the transcribed word appears in `word_list.txt`, it is flagged. This approach has well-understood failure modes:
 
- **False positives on proper nouns and names:** "Beaver" (a place name), "butt" (donkey), "dang" (in expressions of admiration) may be flagged incorrectly. "Dick" (a given name) is now handled by the `=dick` case-sensitive entry in the default word list, which leaves the capitalized form unflagged — but this only works because WhisperX capitalizes proper nouns. Words where the profane and innocent forms are always the same case cannot be distinguished this way.
- **False positives on religious context:** "Oh my God" in a prayer or worship scene carries different intent than the same phrase used as an expletive. Similarly "Jesus" spoken reverently vs. used as a curse.
- **False negatives on euphemisms and slang:** Words not in the list but used with clear profane intent will be missed entirely.
Context-aware detection replaces or augments the word-list pass with a model that evaluates the surrounding context before making a flag/no-flag decision.
 
#### Implementation Approaches (to be decided)
 
**Option A — Local LLM via inference server (e.g., Ollama)**
For each candidate word (one that appears in the word list), pass a context window of surrounding transcript text to a local language model with a prompt asking it to classify the usage as profane or non-profane. Advantages: high accuracy, nuanced reasoning, no cloud dependency. Disadvantages: adds another substantial runtime dependency; increases processing time; classification is non-deterministic.
 
**Option B — Embedding similarity / classifier**
Train or fine-tune a small classifier on labeled examples of profane vs. non-profane usage of ambiguous words. Lighter weight than a full LLM. Disadvantages: requires labeled training data; less generalizable to novel cases.
 
**Option C — Rule-based context heuristics**
For known ambiguous words, define simple surrounding-context rules. For example: if "God" is preceded within 3 words by "thank", "praise", "dear", or "oh dear", do not flag. Advantages: deterministic, fast, no additional model. Disadvantages: brittle, requires manual rule authoring per word, won't generalize.
 
**Recommended path:** Option C as a near-term improvement within v1's architecture (just an extension of `steps/mute.py`), with Option A as the longer-term target when a local LLM is available in the environment. Option B is only worth pursuing if a suitable labeled dataset can be sourced.
 
**Architectural note:** The WhisperX transcript already includes surrounding word context. No change to earlier pipeline steps is needed. Context-aware detection is a drop-in replacement for the word-list matching logic in `steps/mute.py`.
 
---
 
### 13.3 Multi-Channel Audio Processing (Future Consideration)
 
V1 downmixes all source audio to stereo at Step 1b and processes from there. The v1 architecture is intentionally structured so that Step 1b is the only place this decision is made — replacing the downmix with a per-channel split is a contained change that doesn't touch Steps 2–7.
 
#### Film Audio Channel Conventions
 
Before designing a per-channel approach, it helps to understand how professional film audio is mixed. For 5.1:
 
| Channel | Label | Content |
|---|---|---|
| 1 | L (Left) | Music, wide ambience, some dialog bleed |
| 2 | R (Right) | Music, wide ambience, some dialog bleed |
| 3 | C (Center) | **Dialog — almost exclusively, by industry convention** |
| 4 | LFE | Bass, explosions, rumble. No dialog. |
| 5 | Ls (Left Surround) | Ambience, diffuse effects |
| 6 | Rs (Right Surround) | Ambience, diffuse effects |
 
Dialog is deliberately anchored to the center channel to keep it locked to the screen regardless of listener position or speaker placement. A curse word will be on the center channel. It may bleed slightly into L/R, but will not be isolated to a surround or LFE channel.
 
#### Why Demucs Must Still Run on Every Channel
 
Even given the above convention, running Demucs on all channels remains the correct approach for muting, not just on the center channel. The reason: **muting should only affect the dialog stem of each channel, not music or SFX stems**.
 
If a curse word is on the center channel, that moment likely also has music or effects playing on L/R/Ls/Rs. Simply muting a timestamp across all channels would silence that music and those effects too — the same problem Demucs was introduced to solve in v1. By running Demucs per-channel, you get a dialog/non-dialog split for each, and muting is applied only to dialog stems. The music and SFX stems are untouched and recombined as-is.
 
#### Future Per-Channel Pipeline
 
```
1a: Extract raw audio (bitstream copy) — same as v1
1b*: Split to N mono channel files (e.g., 6 files for 5.1)
     instead of downmixing to stereo
 
For EACH channel i:
    2i: Demucs → dialog_i.wav + sfx_i.wav
 
3: WhisperX on center channel (channel 3 for 5.1)
   → transcript.json with word timestamps
   (center channel is the cleanest dialog source;
    running WhisperX on all channels would yield
    near-identical results at N× the compute cost)
 
4, 4b: SRT alignment, interactive review — same as v1
 
For EACH channel i:
    5i: Mute dialog_i.wav at approved intervals → dialog_censored_i.wav
 
For EACH channel i:
    6i: Recombine dialog_censored_i + sfx_i → channel_censored_i.wav
 
7a: Interleave N channel_censored files → audio_censored (multi-channel)
7b: Re-encode to original codec at original channel count
7c: Mux back to video — same as v1
```
 
This multiplies Demucs processing time by the channel count (×6 for 5.1, ×8 for 7.1), which is significant but acceptable for overnight runs.
 
#### Architectural Note
 
The v1 job store preserves `audio_raw.{ext}` in its native multi-channel format. When per-channel processing is implemented, a job from a 5.1 source can be re-run from Step 1b* without re-extracting from the video. The transcript JSON files are also reusable since WhisperX output does not change between v1 and this approach — Step 3 still runs only on the center channel.
 
---
 
### 13.4 Correction Workflow & Resume (Future Work)
 
The interactive review in Step 4b is v1's primary quality mechanism. However, errors may only become apparent after watching the output — a missed word (false negative) or a wrongly muted moment (false positive) discovered at viewing time. Correcting these currently means reprocessing from scratch.
 
**Proposed correction workflow:** Given a completed job in the job store, allow the user to:
 
1. Open the job's `censor_log.json` and/or `review.json` in a text editor or future TUI
2. Add entries (false negative correction) or mark entries `skip: true` (false positive correction)
3. Run `hush.sh --resume {job_id} movie.mkv` to re-run from Step 5 onward using the edited transcript, skipping the expensive Steps 1–4
This is why v1 preserves transcript JSON files in the job store regardless of `keep_intermediates`. The data needed to resume from Step 5 is always available.
 
**V1 groundwork already in place:**
- Job store with `job_id`, `steps_completed`, and preserved transcript files
- `censor_log.json` with full word/timestamp records
- `review.json` has a `skip` field on each entry, designed to support this
**Future interactive enhancement:** Extend the review step to optionally play the audio snippet surrounding each flagged word directly in the terminal or a companion player, so decisions during review don't require re-watching the film.
 
**Multi-track audio note:** Future multi-track preservation (§13.3) would also need to be considered here — correcting a word that appears only in a surround channel.
 
---
 
### 13.5 Audio Word Substitution via TTS (Stretch Goal)
 
Currently, flagged words are either muted (silence) or replaced with a beep. A more natural-sounding result would substitute the censored word with a spoken euphemism — matching the speaker's voice, tone, and cadence so the substitution is seamless.
 
**Proposed approach:** For each muted word, use a voice cloning TTS model to synthesize a replacement word in the speaker's voice and splice it into the dialog stem at the muted interval.
 
**Why this is a stretch goal:** The technology exists (XTTS v2, Bark, ElevenLabs, etc.) but the quality bar for seamless in-context substitution is very high. Matching prosody, tempo, and emotional tone in addition to voice timbre is an unsolved problem at the consumer level for arbitrary in-the-wild speech. The result is more likely to be noticeable than a clean mute, at least until the technology matures further.
 
**Feasibility dependencies:**
- A suitable local TTS/voice-cloning model that can run on CPU (or modest GPU)
- Per-speaker voice embedding extracted from clean dialog segments in the same film
- Timing alignment: the synthesized word must fit within the original word's duration, or the surrounding audio must be time-stretched slightly to accommodate
**Architectural note:** This would be an optional post-processing pass on `dialog_censored.wav` before Step 6 (recombine). The mute intervals from `censor_log.json` already provide the precise timestamps needed to locate insertion points.
 
### 13.6 Confidence-Guided Review
 
WhisperX provides confidence scores for recognized words.
 
Future versions may use these scores to reduce review burden by automatically approving high-confidence matches and presenting only low-confidence detections for human review.
 
Example:
 
```yaml
interactive:
  min_confidence_for_prompt: 0.70
```
 
### 13.7 Analysis Mode
 
A future `analyze` command may perform transcript generation and profanity detection without producing a censored output file.
 
Example:
 
```bash
hush.sh --analyze movie.mkv
```
 
>Potential output:
>
>Detected terms:
>  dang: 14
>  heck: 8
>  crap: 6
>
>Low-confidence matches:
>  5
>
>Estimated processing time:
>  Demucs: 6h
>  WhisperX: 45m
 
This would allow users to review likely results before committing to a full render.

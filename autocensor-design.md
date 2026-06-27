# profanity-hush — Design Document
 
**Project:** Automated movie profanity censoring pipeline
**Version:** 0.13.0
**Status:** v1 core pipeline complete — Steps 1a/1b/1c/2/3/3b/4b(flag)/4b(review, optional)/5/6/6b/7 are all implemented and validated end-to-end against real production data (multiple full-length films), including the final mux to a playable output video. Step 5 consumes Step 4b's flagged matches directly rather than re-scanning the transcript itself — see §4. The correction workflow (§13.4) is also implemented: `--skip-index` / `--add-interval` / `--redo-review` let a mistake noticed after watching the film be fixed by redoing only Steps 5, 6, 6b, and 7 (a few minutes), not a full re-run — backed by `output.keep_correction_artifacts` (default **`true`**), which keeps `dialog.wav`/`score_sfx.wav` on disk specifically to make this cheap. Step 6b (added after a real-world A/V sync bug — see §8) splits the censored audio's re-encode out of what used to be a single combined copy+encode mux command into its own standalone, single-input ffmpeg step, producing `audio_encoded.mka`; Step 7 then becomes a pure stream-copy mux (`-c:v copy -c:a copy`) of the original video and that already-finalized audio file. Large intermediate WAV/audio stems are each deleted by whichever step finishes consuming them, governed by `keep_intermediates` (default `false`) or `keep_correction_artifacts` depending on the file, both now resolved through a single shared function (`utils.keep_intermediate()`) rather than each step computing it independently — see §6, which also covers a `hush.sh` bug (now fixed) where a host-set `AC_KEEP_INTERMEDIATES` env var was silently never forwarded into the container unless `--keep-tmp` was also passed. Step 7 also preserves embedded subtitle/chapter/attachment streams for `mkv` output (chapters only for `mp4`) — an extension beyond §8's literal example command, not in the original plan, added because the literal command would otherwise silently drop them. Output filenames default to a Plex-friendly `{edition-Hushed}` tag (`output.naming_style: plex_edition`) rather than the original plan's plain suffix, which is now an opt-in alternative (`output.naming_style: suffix`) — see §8. Step 4 (SRT alignment, Phase 3) deliberately deferred — Step 4b's flag phase currently reads `transcript.json` directly; this needs no code change when Step 4 lands, since `transcript_aligned.json` is the same schema. `censoring.method: beep` is accepted in config but not yet implemented (Phase 4 polish item) — Step 5 raises a clear error if it's selected; `mute` is the only implemented method in v1. Logging is now timezone-aware (§6.1): every timestamp used to be silently UTC with no label, which made console output, `job.json`, and job directory names all look like local time while actually running however many hours off — `hush.sh` now forwards the host's offset (`AC_TZ_OFFSET`/`AC_TZ_NAME`), and console lines, `job.json`'s new `*_local` fields, and the job directory's leading timestamp itself all render in it (falling back to explicitly-labelled UTC if it's absent) — job folders are one of the places someone is most likely to actually look, so that timestamp reads as the wall clock said rather than needing offset arithmetic, the same reasoning applied throughout. This pass also corrected several stale cross-references found during review: the job directory was documented (§6, §8) as a bare `{job_id}/`, which hasn't matched `pipeline.py`'s actual human-readable naming scheme for some time; `steps/merge.py`'s logging spec (§8) still described a per-segment "flagged word count" left over from before Step 4b's flag phase was split into its own module; §8's `pipeline.py` correction-workflow note still described `--skip-index`/`--add-interval`/`--redo-review` as groundwork for a future `--resume` mode rather than the shipped feature it now is; and §7.3's env var table was missing `AC_KEEP_CORRECTION_ARTIFACTS`, despite this section's own text already describing it as implemented.
 
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
| Segment audio | `ffmpeg` | splits stereo WAV into fixed-size segments if duration exceeds threshold; passthrough if not |
| Separate dialog from score/SFX | `demucs` (`htdemucs_ft` model) | pip inside container; MIT licensed; runs per-segment |
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
│  STEP 1c: Segment   │  If duration > segment_size_sec: split into N segments.
│                     │  → audio_stereo_01.wav, audio_stereo_02.wav, ...
│                     │  Single-segment case: passthrough; no file splitting performed.
│                     │  Logs total duration, segment count, and per-segment offsets.
└─────────────────────┘
          │
          ▼  ┌─── repeat for each segment NN ──────────────────────────────────────┐
             │                                                                       │
┌────────────┴────────┐                                                             │
│  STEP 2: Separate   │  demucs htdemucs_ft --two-stems=vocals                     │
│                     │  → dialog_NN.wav      (stereo)                             │
│                     │  → score_sfx_NN.wav   (stereo)                             │
│                     │  Logs per-segment duration and wall-clock time.            │
└─────────────────────┘                                                             │
          │                                                                          │
          ▼                                                                          │
┌─────────────────────┐                                                             │
│  STEP 3: Transcribe │  whisperx dialog_NN.wav → transcript_NN.json               │
│  (word timestamps)  │  Word timestamps are segment-local (0-based).              │
│                     │  WhisperX converts to mono 16kHz internally.               │
└─────────────────────┘                                                             │
          └─────────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 3b: Merge     │  For each segment_NN, add start_offset to every word
│                     │  timestamp: word.start += offset; word.end += offset
│                     │  Concatenate dialog_NN.wav stems → dialog.wav
│                     │  Concatenate score_sfx_NN.wav stems → score_sfx.wav
│                     │  → transcript.json (global timestamps throughout)
│                     │  Logs per-segment and total word counts.
└─────────────────────┘
          │
          ▼
┌─────────────────────┐   (optional)
│  STEP 4: SRT align  │  cross-reference transcript.json ↔ subtitles.srt
│                     │  → transcript_aligned.json
│                     │  Not yet implemented — currently a no-op; Step 4b
│                     │  reads transcript.json directly until this exists
│                     │  (same schema either way, see §8).
└─────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 4b: Flag &    │  Flag (always runs, both modes): steps/matching.py
│  Review             │  finds every candidate against word_list.txt
│                     │  → matches.json. This is the pipeline's one and only
│                     │  scan — Step 5 never repeats it.
│                     │
│                     │  Review (optional sequence run after Flag; skipped
│                     │  in unattended mode): present matches.json's
│                     │  candidates in the terminal; user approves / rejects
│                     │  / adds entries → review.json (sparse overrides on
│                     │  top of matches.json; see §8)
└─────────────────────┘
          │
          ▼
┌─────────────────────┐
│  STEP 5: Mute       │  Reads matches.json (Step 4b's flag output) directly
│  dialog stem        │  — no re-scan. Applies review.json overrides if
│                     │  present (drops "skip" word_indexes; adds "add"
│                     │  entries' intervals outright). Pads + merges
│                     │  intervals; ffmpeg volume filter →
│                     │  dialog_censored.wav + censor_log.json.
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
│  STEP 7: Mux to     │  ffmpeg -c:v copy → Movie (Year) {edition-Hushed}.mkv
│  video              │  (output.naming_style: suffix → Movie_censored.mkv)
└─────────────────────┘
 
OUTPUT: the final censored video, in /output  [+ job record in jobs store]
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

**Ownership:** The container runs as the invoking host user (`--user "$(id -u):$(id -g)"`, set in `hush.sh`), not root — otherwise every file written into these bind mounts would land on the host owned by root, requiring `sudo` to delete, move, or re-process later. That UID/GID has no `/etc/passwd` entry inside the image; this works fine for this pipeline's needs (no component requires a real user account), but the Dockerfile sets `$HOME` to a dedicated world-writable scratch directory and disables Python bytecode writing to avoid the few places that would otherwise fall back to `$HOME` or try to write next to the read-only source tree. `docker-compose.yml` users must set `HOST_UID`/`HOST_GID` in `.env` to get the same behaviour, since plain `UID` is a readonly bash builtin and compose can't shell out to `id -u` inline the way `hush.sh` does.
 
### 5.3 Wrapper Script (`hush.sh`)
 
No GPU detection needed. The wrapper resolves absolute paths (Docker requires them for `-v`)
and launches the container:
 
```bash
docker run --rm \
    --user "$(id -u):$(id -g)" \
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
│   │   ├── segment.py          # step 1c: split audio_stereo.wav into segments
│   │   ├── separate.py         # step 2: demucs wrapper (per-segment)
│   │   ├── transcribe.py       # step 3: whisperx wrapper (per-segment)
│   │   ├── merge.py            # step 3b: apply global offsets; concatenate stems + transcripts
│   │   ├── align_srt.py        # step 4: optional SRT cross-reference
│   │   ├── matching.py         # shared word-list parsing + transcript matching; called once,
│   │   │                       #   by review.py's flag phase below — mute.py never calls it
│   │   ├── review.py           # step 4b: flag phase (always runs) + optional interactive
│   │   │                       #   review phase, run in sequence — see §4
│   │   ├── mute.py             # step 5: mutes the dialog stem from review.py's flagged
│   │   │                       #   matches (no transcript re-scan); generates the ffmpeg filter
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
└── {YYYYMMDD_HHMMSS}_{slug}_{hex8}/    # human-readable job folder name --
    │                                   #   see "Job directory naming" below
    ├── job.json                # metadata: input file, config snapshot, step completion status
    ├── audio_raw.{ext}         # bitstream copy of original audio; always kept; ext = source codec
    ├── transcript_01.json      # per-segment whisperx output; always kept
    ├── transcript_02.json      # (one file per segment; single-segment jobs have only _01)
    ├── transcript.json         # merged transcript with global timestamps; always kept
    ├── transcript_aligned.json # post-SRT alignment; always kept (if applicable)
    ├── matches.json            # Step 4b flag-phase output — every candidate found against
    │                           #   word_list.txt; always kept. Step 5 reads this directly and
    │                           #   never re-scans the transcript itself (see §4).
    ├── review.json             # post-interactive-review; always kept (if applicable)
    ├── censor_log.json         # record of every word muted, with timestamps; always kept
    ├── audio_stereo.wav        # full stereo downmix (large; keep_intermediates only)
    ├── audio_stereo_01.wav     # per-segment stereo files (large; keep_intermediates only)
    ├── audio_stereo_02.wav     # (only present if segmentation was performed)
    ├── dialog_01.wav           # per-segment demucs dialog stems (large; keep_intermediates only)
    ├── dialog_02.wav
    ├── dialog.wav              # concatenated dialog stem (large; kept by default — see
    │                           #   keep_correction_artifacts below; deleted only if that AND
    │                           #   keep_intermediates are both false)
    ├── score_sfx_01.wav        # per-segment demucs score+SFX stems (large; keep_intermediates only)
    ├── score_sfx_02.wav
    ├── score_sfx.wav           # concatenated score+SFX stem (large; kept by default — same
    │                           #   keep_correction_artifacts rule as dialog.wav above)
    ├── dialog_censored.wav     # Step 5 output (large; keep_intermediates only — deleted by
    │                           #   Step 6 once audio_censored.wav exists)
    ├── audio_censored.wav      # Step 6 output: dialog_censored.wav + score_sfx.wav recombined
    │                           #   (large; keep_intermediates only — deleted by Step 6b once
    │                           #   audio_encoded.mka exists)
    └── audio_encoded.mka       # Step 6b output: audio_censored.wav re-encoded to match the
                                 #   original audio codec/bitrate/channel layout (large;
                                 #   keep_intermediates only — deleted by Step 7 once the final
                                 #   muxed video exists)
```
 
`audio_raw.{ext}`, all `transcript*.json`, `matches.json`, `review.json`, and `censor_log.json` are always kept regardless of `keep_intermediates` — they are small (or in `audio_raw`'s case, already compressed in its native codec) and are the essential resume artifacts. Large decoded WAV/audio intermediates are governed by two independent settings: `keep_intermediates` (default `false`) covers the per-segment stems, `audio_stereo*.wav`, `dialog_censored.wav`, `audio_censored.wav`, and `audio_encoded.mka` — all either fully superseded once consumed or trivially cheap to regenerate. `dialog.wav` and `score_sfx.wav` are governed by `keep_correction_artifacts` (default **`true`**) instead — deleted only if that setting *and* `keep_intermediates` are both false — because they're what makes §13.4's correction workflow (`--skip-index`/`--add-interval`/`--redo-review`) cheap: without them, correcting a mistake noticed after watching the film would require re-running Step 2's Demucs separation from scratch. Each large intermediate is deleted by whichever step first finishes consuming it (Step 3b/merge for the per-segment stems and `audio_stereo*.wav`, Step 5/mute for `dialog.wav`, Step 6/recombine for `score_sfx.wav` and `dialog_censored.wav`, Step 6b/encode for `audio_censored.wav`, Step 7/mux for `audio_encoded.mka`) — never by the step that *produced* it, since the producing step has no way to know yet whether anything downstream still needs it.

**Single source of truth:** every one of those deletion decisions goes through `utils.keep_intermediate(cfg, correction_artifact=...)` — no step reads `output.keep_intermediates`/`output.keep_correction_artifacts` directly. This was a deliberate fix, not the original design: each step originally computed its own `bool(cfg_get(...))` locally, which is exactly the kind of duplication that let `hush.sh`'s forwarding bug (below) go unnoticed — the policy was correct in the design doc and in `config.yaml`'s comments the whole time, but nothing checked that the *host* setting actually reached the container. `pipeline.py` now also logs the fully-resolved retention settings once at startup (`utils.retention_summary()`), specifically so a setting that silently failed to arrive is visible in the first few lines of output instead of discoverable only after the run completes and an expected file isn't there.

**`hush.sh` bug (fixed):** `AC_LOG_LEVEL` and `AC_SEGMENT_SIZE` were forwarded from the host shell's environment into the container; `AC_KEEP_INTERMEDIATES` was not — it was only ever set when `--keep-tmp` was passed on the command line, silently ignoring the documented `AC_KEEP_INTERMEDIATES=1` host env var form (`config.yaml`'s own comment said this was supported). Anyone using the env var directly (rather than `--keep-tmp`) got the opposite of what they asked for, with no error or warning. Fixed by forwarding the host env var as a fallback when `--keep-tmp` isn't given, matching the precedence pattern `AC_INTERACTIVE`/`--interactive` already used. `AC_KEEP_CORRECTION_ARTIFACTS` (`1`/`0`) was added as the equivalent override for the newer setting.

**Naming note (single-segment jobs):** `dialog.wav` / `score_sfx.wav` (no numeric suffix) are produced *directly* by Step 2 in the single-segment case — there is no intermediate `dialog_01.wav`. This is because Step 1c's passthrough re-uses `audio_stereo.wav` (also unsuffixed) as the sole segment file, and Step 2 derives its output suffix from the segment filename it's given (`steps/separate.py`: `seg_path.stem.removeprefix("audio_stereo")` → `""` when unsuffixed). `transcript_01.json`, by contrast, is **always** numbered by segment index regardless of segmentation — `steps/transcribe.py` names its output from the segment's loop position, not from the dialog filename's suffix. Step 3b (merge) always reads `transcript_01.json` (and `_02`, …) and writes the canonical un-suffixed `transcript.json`, even when there is only one segment.

**Job directory naming (corrected from earlier drafts of this doc):** the job directory is **not** literally `{job_id}/`, despite what the tree above and §8's `pipeline.py` spec used to say. `job_id` (`sha256[:12]` of the input path + mtime) is still computed exactly as documented and is still what `find_job_dir()` matches on to resume a job — but the directory itself is named `{YYYYMMDD_HHMMSS}_{slug}_{hex8}` (`pipeline.py:make_job_dir_name()`), where `slug` is a filesystem-safe, length-capped version of the input filename and `hex8` is just `job_id`'s first 8 characters, not the full 12. This makes job folders recognisable by `ls` (e.g. `20260625_163638_top-gun-1986-1080p-h264_64c7d2b6`) without opening `job.json` to figure out which one is which. Because the directory name is no longer derivable from `job_id` alone, resuming a job scans `jobs_dir/*/job.json` for a matching `job_id` field (`utils.find_job_dir()`) rather than constructing the path directly — a few tens of entries at most in practice, so the scan is negligible. See "Logging & Timestamps" immediately below for why that leading timestamp is local time, not UTC.

### 6.1 Logging & Timestamps

Every timestamp in the pipeline — console log lines, `job.json`'s `started_at`/`failed_at`/`completed_at`, and the job directory's leading timestamp above — was originally computed with `datetime.now(tz=timezone.utc)` and printed with no indication that it was UTC. Inside a container (which defaults to UTC with no idea what the host's wall clock says), this made every timestamp look exactly like unlabelled local time while actually running however many hours ahead/behind UTC the host's real time zone is — for example, a job that started at 16:36 local time on the US west coast (UTC−7) showed up everywhere as 23:36, which reads as "in the future" to anyone not doing the offset arithmetic in their head while reading an overnight log the next morning.

**Fix:** `hush.sh` captures the host's current UTC offset at invocation time (`date +%z`, e.g. `-0700`) and forwards it into the container as `AC_TZ_OFFSET`, plus a cosmetic abbreviation (`date +%Z`, e.g. `PDT`) as `AC_TZ_NAME` — see §7.3. A numeric offset, not a named zone like `America/Los_Angeles`, is deliberate: it needs no timezone database (`tzdata`) inside the image, and no agreement between host and container about one, at the cost of not auto-tracking a DST transition mid-run — irrelevant for a process that runs for hours, not months. `utils.py` resolves these into `LOCAL_TZ` once at import time (`utils._resolve_local_tz()`); every per-line console timestamp (`utils._StepFormatter`) renders in `LOCAL_TZ` with its own explicit `%z` offset, so it's self-describing even in the fallback case where `AC_TZ_OFFSET` was never forwarded (plain UTC, now honestly labelled `+0000` instead of bare and ambiguous). `pipeline.py` logs which case applies once at startup (`utils.timezone_banner()`), the same pattern §6 above already uses for `utils.retention_summary()` — the actually-resolved behaviour visible immediately, not discoverable only after every timestamp in a finished run looks wrong.

`job.json`'s `started_at`/`failed_at`/`completed_at` fields stay in UTC ISO 8601 (unchanged) — the canonical, machine-comparable record, regardless of which (if any) offset a given run happened to log in — with `*_local` companions (`started_at_local`, etc.) added purely for a human reading the file directly. The job directory's leading timestamp (above), by contrast, switched fully to `LOCAL_TZ` — the same local time as everything else in this section — rather than staying in UTC. An earlier version of this kept it UTC for monotonic, DST-safe `ls` ordering, but that's the wrong tradeoff for what's actually the most-looked-at timestamp in the whole pipeline: a person browsing `~/.local/share/profanity-hush/jobs/` directly should see what their own clock said, not something requiring offset arithmetic, on literally every job — paid for by avoiding a sort-order quirk that (a) only arises if two jobs happen to straddle a DST "fall back" transition, and (b) wouldn't break anything even then, since nothing in this codebase resumes a job by parsing its directory name — `find_job_dir()` scans `job.json`'s contents instead (see "Job directory naming" above).

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
  shifts: 1                     # random temporal shift averaging; higher = better quality
                                # at proportional compute cost.
                                # Measured (§12 / Open Question #11): shifts=1 runs at
                                # ~1.09x realtime -- ~2.2 hrs for a 2-hour film, not the
                                # ~9-10 hrs originally estimated here pre-validation.
                                # shifts=4 is NOT yet directly measured; IF runtime scales
                                # linearly with shifts (unverified), it extrapolates to
                                # ~8.7 hrs for a 2-hour film -- see Open Question #9.
 
whisperx:
  model: large-v2               # large-v2 recommended for reliability
                                # large-v3 available but has known regression cases on some audio
  language: en                  # ISO 639-1; null for auto-detect
  batch_size: 4                 # lower than GPU default; tunes CPU memory pressure
  beam_size: 5                  # beam search width; higher = more accurate, slower
  compute_type: int8            # int8 recommended on CPU (faster, lower memory);
                                # float32 fallback; float16 is GPU-only
  device: cpu
 
# Audio handling
audio:
  segment_size_sec: 1800        # split audio into segments of this length before processing.
                                # 1800 = 30 minutes (default). Set to 0 to disable segmentation.
                                # Segmentation is required for large files due to Demucs memory
                                # usage: a 2-hour file at full quality exhausts 16 GB RAM.
                                # Reduce if OOM errors occur; increase only if memory permits.
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
 
**Implementation note for `steps/matching.py`:** exact entries use `==`, `word*` uses `str.startswith()`, and `*word*` uses the root string as a substring check (`root in token`). Case-insensitive comparisons fold both sides to lowercase before comparing; `=` entries compare the token's original casing against the entry's casing as written. (This is `find_matches()`'s logic, called exactly once — from `steps/review.py`'s flag phase. `steps/mute.py` never calls it; see §4 and §8.)
 
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
| `AC_KEEP_CORRECTION_ARTIFACTS` | `0` to also delete `dialog.wav`/`score_sfx.wav` (these default to kept — see §6) |
| `AC_INTERACTIVE` | `1` to enable interactive review mode |
| `AC_SEGMENT_SIZE` | override `audio.segment_size_sec`; seconds; `0` to disable segmentation |
| `AC_TZ_OFFSET` / `AC_TZ_NAME` | host UTC offset (e.g. `-0700`) / cosmetic abbreviation (e.g. `PDT`) for log timestamps — not a `config.yaml` setting, since it's host-environment information rather than pipeline behaviour; `hush.sh` sets both automatically (see §6.1) |
 
---
 
## 8. Module Specifications
 
### `pipeline.py`
- Parses CLI arguments (input path, optional SRT path, optional config override)
- Computes a `job_id` = `sha256[:12]` of the input file's absolute path + mtime; creates a job directory under `storage.jobs_dir/{YYYYMMDD_HHMMSS}_{slug}_{hex8}/` (`make_job_dir_name()` — **not** a bare `{job_id}/`; see §6's "Job directory naming" for why, and §6.1 for why the timestamp is local time rather than UTC)
- Writes `job.json` at job start with: input path, config snapshot, a canonical UTC `started_at` timestamp (plus a `started_at_local` companion — see §6.1), and a `steps_completed: []` list
- Calls Steps 1a, 1b, and 1c in sequence; receives the segment list (paths + start offsets) from Step 1c
- Calls Step 2 (separate) across all segments, then Step 3 (transcribe) across all segments; each step marks one `steps_completed` entry (`2_separate`, `3_transcribe`) once *all* its segments are done. Per-segment resume is handled inside each step by checking whether that segment's own output file(s) already exist (see `steps/separate.py`, `steps/transcribe.py`) — not via finer-grained job-state entries.
- **Once `3b_merge` is in `steps_completed`, Steps 1a-3b are skipped entirely on every subsequent run** — `pipeline.py` derives `transcript.json`/`dialog.wav`/`score_sfx.wav` by their fixed canonical names directly, rather than calling `extract_raw`/`segment`/`separate`/`transcribe`/`merge` again. This isn't just an optimization: `steps/merge.py`'s own cleanup deletes the per-segment intermediates (`dialog_NN.wav`, `score_sfx_NN.wav`, `audio_stereo_NN.wav`) once they're consolidated, and `steps/separate.py`'s "already done" resume path assumes those files are still on disk — calling it again after Step 3b's cleanup has run throws a missing-file error even though nothing is actually wrong. The fix is structural, not a patch to `separate.py`'s resume check: once Step 3b is done, nothing downstream ever needs the per-segment files again, so the orchestrator should never ask for them again either.
- Calls Step 3b (merge) once all segments are complete
- Calls Steps 4, 4b, 5, 6, 7 in sequence on the merged artifacts, as before
- Handles step failures: log error with step name and exception, update `job.json` with failure info (`status`, `failed_at`/`failed_at_local`, `failure.step`/`.error`/`.traceback`), exit with non-zero code
- On success: moves final output to `/output/`, marks job complete in `job.json` (`status`, plus `completed_at`/`completed_at_local`)
- Always preserves all `transcript_NN.json` and `transcript.json` files; removes large WAV stems unless `keep_intermediates` is set

**Correction workflow (§13.4 — implemented, not just groundwork):** `steps_completed`, combined with the preserved transcript/match/review files above, is what `--skip-index`/`--add-interval`/`--redo-review` build on to invalidate and redo only Steps 5, 6, 6b, and 7 rather than the full pipeline — see §13.4 for the mechanism. (An earlier draft of this doc described this only as future groundwork for a planned `--resume` mode; that mode has since shipped under the flag names above, not as a separate `--resume` flag.)
 
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
- Output feeds Step 1c (segmentation); regenerable from `audio_raw` if deleted
- Kept only if `keep_intermediates` is set; otherwise deleted after Step 2 completes
- This is v1's deliberate boundary for multi-channel handling — see §13.3 for the future path
### `steps/segment.py` *(Step 1c)*
- **Input:** `audio_stereo.wav`
- **Output:** list of `(path, start_offset_sec)` tuples; segment WAV files in job dir
- **Logic:**
  1. Probe duration of `audio_stereo.wav` via `ffprobe`
  2. If `segment_size_sec == 0` or `duration <= segment_size_sec`: return `[("audio_stereo.wav", 0.0)]` — no splitting performed, single-segment passthrough
  3. Otherwise compute `N = ceil(duration / segment_size_sec)` and split:
     ```bash
     ffmpeg -i audio_stereo.wav -ss {start} -t {segment_size_sec} -c copy audio_stereo_NN.wav
     # final segment omits -t to capture any sub-second remainder
     ```
  4. Return `[("audio_stereo_01.wav", 0.0), ("audio_stereo_02.wav", 1800.0), ...]`
- **Logging (info level):**
  - Total duration (seconds and HH:MM:SS)
  - Segment count and segment size
  - Per-segment: index, start offset, end offset, file path
- **Segment files** are kept only if `keep_intermediates` is set; otherwise deleted after Step 3b completes

### `steps/separate.py`
- **Input:** `audio_stereo_NN.wav` (one segment)
- **Output:** `dialog_NN.wav`, `score_sfx_NN.wav`
- **Tool:** `python -m demucs --two-stems=vocals -n {model} --shifts {shifts} -d cpu -o {tmpdir} audio_stereo_NN.wav`
- `--shifts` averaging defaults to 1 (config `demucs.shifts`); increase to 4 for improved quality at ~4× compute cost
- Demucs outputs to a subdirectory named after the model; this module renames/moves to flat expected paths with the segment suffix
- **Logging (info level):**
  - Segment index and duration
  - Wall-clock time on completion
  - Cumulative progress (e.g. `[2/4 segments separated]`)
- Logs estimated completion time based on segment duration (rough: ~4× realtime per shift on modern CPU based on observed test data; see §12)
### `steps/transcribe.py`
- **Input:** `dialog_NN.wav` (one segment)
- **Output:** `transcript_NN.json`
- **Format:**
```json
{
  "language": "en",
  "segment_index": 1,
  "segment_start_offset": 1800.0,
  "words": [
    { "word": "example,", "start": 12.34, "end": 12.78, "score": 0.97 }
  ]
}
```
- **Field notes:**
  - `score` is WhisperX's per-word confidence (0–1). This is the field used by `min_confidence_for_prompt` in interactive review. It is **not** renamed to `confidence` — use the field name as WhisperX produces it.
  - `segment_start_offset` records the segment's global start position in seconds; used by Step 3b to compute global timestamps.
  - `word` values preserve WhisperX's original casing and include attached punctuation (e.g. `"shit,"`, `"warning."`). **Do not lowercase.** Punctuation is stripped at match time in `steps/matching.py` (called once, from `steps/review.py`'s flag phase — see §4), not at write time here.
- **Tool:** whisperx Python API
- Uses `align()` for word-level timestamps after initial transcription pass
- Preserves WhisperX's original word casing in the JSON output. **Do not lowercase words before writing.** Original casing is required for case-sensitive word list entries (see §7.2). WhisperX naturally capitalizes proper nouns and sentence-initial words, which is the signal used by `=`-prefixed entries in the word list to distinguish proper nouns from profanity (e.g. `Dick` vs `dick`).
- **Logging (info level):**
  - Segment index and duration
  - Wall-clock time on completion
  - Word count in segment
  - Cumulative progress (e.g. `[2/4 segments transcribed]`)
### `steps/merge.py` *(Step 3b)*
- **Input:** list of `transcript_NN.json` files and their segment offsets; `dialog_NN.wav` files; `score_sfx_NN.wav` files
- **Output:** `transcript.json` (global timestamps), `dialog.wav`, `score_sfx.wav`

**Transcript merge:**
1. For each `transcript_NN.json`, add `segment_start_offset` to every word's `start` and `end`
2. Concatenate the adjusted word lists in segment order into a single `words` array
3. Write `transcript.json` — same schema as a single-segment transcript but with global timestamps throughout and no `segment_index` / `segment_start_offset` fields at the top level

**Audio stem concatenation** (lossless PCM concat):
```bash
ffmpeg -i "concat:dialog_01.wav|dialog_02.wav|..." -c copy dialog.wav
ffmpeg -i "concat:score_sfx_01.wav|score_sfx_02.wav|..." -c copy score_sfx.wav
```

**Logging (info level):**
- Per-segment: segment index, start offset, end offset, word count
- Total: overall duration, total word count
- All per-segment timestamp logging is at `info` level; can be moved to `debug` once the pipeline is stable
- **No flagged-word-count logging here** (corrected from an earlier draft of this spec, written before Step 4b's flag phase was split out into its own module): `merge.py` has no knowledge of `word_list.txt` and performs no matching — see §4, "this is the pipeline's one and only scan" against the word list happens exactly once, in `steps/matching.py`, called from Step 4b. Logging a flagged count here would mean scanning twice.

**Note:** In the single-segment passthrough case, `dialog.wav` and `score_sfx.wav` are already present under their canonical (un-suffixed) names — Step 2 produced them directly, since Step 1c's passthrough segment file (`audio_stereo.wav`) carries no numeric suffix either (see the job-store naming note in §6). This step's audio work in that case is therefore a no-op (verified present, not copied). The transcript side still does real work: `transcript_01.json` → `transcript.json` (offset is 0.0, so only the filename changes, not the timestamps). The step still runs unconditionally in both cases, so the canonical filenames that all downstream steps depend on are guaranteed to exist.

### `steps/align_srt.py`
- **Input:** `transcript.json`, `subtitles.srt` (optional)
- **Output:** `transcript_aligned.json` (same schema; updated timings where SRT confidence wins)
- **Logic:** For each word in transcript flagged as a potential profanity match, check if an SRT segment covers that time range. If fuzzy text match score ≥ threshold, update timing using SRT segment boundaries. This is a refinement step, not a replacement.
- **Skipped entirely** if no SRT file is provided or `srt.enabled: false`
### `steps/matching.py` *(called once, from Step 4b's flag phase)*
- **Purpose:** word-list parsing and transcript matching. Used by `steps/review.py`'s flag phase to scan the transcript and produce `matches.json` — this is the pipeline's **only** call to `find_matches()`. Step 5 (`steps/mute.py`) does not import this module at all; it consumes `matches.json` directly. Keeping the scan in exactly one place means there is no second code path that could ever disagree with the first about what counts as a match — a word a human approved (or rejected) during Step 4b's review phase is, by construction, one of the exact candidates Step 5 acts on, because Step 5 never independently re-derives that set.
- `load_word_list(path) -> list[WordListEntry]` — parses `word_list.txt` per the notation table in §7.2. Malformed entries (e.g. a lone leading `*` with no trailing `*`, or `*` notation on a multi-word phrase) are skipped with a warning rather than silently mis-parsed — a stray character producing an unintended broad match is a worse failure mode for a profanity filter than dropping one entry.
- `find_matches(words, entries) -> list[Match]` — walks the transcript's flat `words` array (from `transcript.json` or, once Step 4 exists, `transcript_aligned.json` — same schema) and returns every match, each with `word_index`, `span` (1 for a single word, >1 for a phrase), `matched_text` (original casing + punctuation, for display), the word-list `entry` that matched, global `start`/`end`, and `score` (the minimum confidence across a phrase's words, for the conservative case).
  - Words with no alignment timing (`start`/`end` null — see `steps/transcribe.py`) are excluded: there is nothing to review or mute about a word with no timestamp.
  - Matches are **not** de-duplicated or merged across overlapping spans (e.g. a single-word entry `ass` and a phrase entry `kiss my ass` can both independently match the same audio) — that is Step 5's job (see its "merge overlapping intervals" logic below), which already has to merge regardless of how many separate matches produced the overlap.
- `steps/review.py`'s flag phase serializes the returned `list[Match]` verbatim (via `dataclasses.asdict`) to `matches.json` as `{"matches": [...]}`. Step 5 reads that file back into the same shape and never needs this module to do so.

### `steps/review.py` *(Step 4b — Flag & Review)*

Two phases, both implemented in this module and invoked separately by `pipeline.py`:

**Flag phase — `flag(job_dir, transcript_path, cfg) -> matches_path`**
- **Input:** `transcript_aligned.json` (or `transcript.json`); word list
- **Output:** `matches.json` — `{"matches": [...]}`, the full, unfiltered result of `steps/matching.py`'s `find_matches()` (§8 above), serialized verbatim
- **Always runs**, in both interactive and unattended modes — flagging is not optional; only the review *prompting* below is. This is the pipeline's one and only call to `find_matches()` (see §4).
- Resumable independently of the review phase below: if `4b_flag` is already marked complete, the existing `matches.json` is reused as-is.

**Review phase — `review(job_dir, matches_path, transcript_path, cfg) -> review_path`**
- **Input:** `matches.json` (from the flag phase above — matches are **not** re-derived from the transcript here)
- **Output:** `review.json` — a **sparse list of overrides**, not a full transcript or matches copy (see schema below). A match with no override is implicitly approved (the default outcome once a word matches the list is to mute it).
- **Activated by:** `--interactive` CLI flag, `--no-interactive` to force it off, or `interactive.enabled: true` in config as the fallback when neither flag is given. Resolved once in `pipeline.py`; this phase has no on/off switch of its own — whether to call it at all is the caller's decision. The flag phase above runs unconditionally either way.
- **Skipped entirely** in unattended mode; Step 5 reads `matches.json` directly with no `review.json` to apply.
- **Requires a TTY.** `pipeline.py` checks `sys.stdin.isatty()` as soon as interactive mode is resolved — before Steps 1–3b run — and exits immediately with a clear error if it's missing, rather than letting an unattended multi-hour run reach the review phase's first prompt and hang or crash on EOF. `hush.sh` allocates one (`-it`) automatically for `--interactive` and for `AC_INTERACTIVE` (when no CLI flag overrides it); it cannot do this for `interactive.enabled: true` set only in `config.yaml`, since `hush.sh` doesn't parse YAML — the `pipeline.py` check is what catches that case.
**Terminal UI behavior** (review phase):
 
For each word in `matches.json` that the flag phase found, display a review entry:
 
```
[3 of 11]  Word: "crap"  |  Confidence: 0.94  |  Time: 00:23:14.8 – 00:23:15.1
Context: "...and then he said crap right in front of..."
Action? [Y]es / [N]o / [A]dd word / [S]kip rest / [Q]uit  >
```
 
- **Y (default):** approve; word will be muted
- **N:** reject; recorded as a `skip` override; word will not be muted
- **A:** prompt for an additional word/phrase to add (manual false-negative correction). No audio playback in v1 (see note below), so this searches the transcript text for the word/phrase typed: if found once, it's used directly; if found multiple times, the candidates are listed (with context and timestamps) for the reviewer to pick from; if not found at all (mis-transcribed, or never said in a way Whisper caught), it falls back to manual `start`/`end` entry in seconds or `HH:MM:SS.mmm`. After an add, the *same* candidate is re-shown for its own Y/N/A/S/Q decision — adding doesn't consume a turn.
- **S:** approve this and all remaining flagged entries without further prompting
- **Q:** abort the run; **nothing is written**, including `review.json` itself — re-running re-enters the review phase from scratch. (`matches.json` from the flag phase is unaffected and is *not* re-scanned on the retry.)
After the review loop, a summary is printed:
```
Review complete: 9 approved, 2 rejected, 1 added.
Proceeding to mute step.
```
(Entries auto-approved via `min_confidence_for_prompt`, below, are reported separately — see `review.json contents` — since no human reviewed them; they're not counted in "approved" above.)
 
**`show_context_words`** (config): controls how many words of surrounding transcript are shown on each side of the flagged entry.
 
**`min_confidence_for_prompt`** (config): if set above 0.0, entries with confidence *above* the threshold are auto-approved without ever being shown; only lower-confidence entries require human review. Useful for reducing review burden when most detections are high-confidence. Once a human presses **S**, all remaining entries count toward "approved" (the explicit bulk decision), even ones that would have separately qualified for auto-approval.
 
**`review.json` contents** — sparse: only entries that differ from the default ("matched the word list → will be muted") are recorded.
```json
{
  "overrides": [
    {
      "action": "skip",
      "word_index": 412,
      "text": "crap"
    },
    {
      "action": "add",
      "word_index": 913,
      "text": "bastard",
      "start": 1203.14,
      "end": 1203.48
    }
  ]
}
```
`word_index` on a `skip` override is required — it identifies which auto-flagged match (by index into the transcript's flat `words` array) is being rejected. On an `add` override it is informational only: present (and authoritative for display) when the reviewer found the word/phrase by searching the transcript, `null` when it was a true manual entry with no matching transcript word at all. Either way, `start`/`end` are self-sufficient for Step 5 to build a mute interval — it never needs to resolve `word_index` back through the transcript for an `add`. `text` records what was added or rejected, so a future correction tool (§13.4) can read `review.json` on its own without cross-referencing `transcript.json`.
 
**Note on audio playback:** Displaying a playable audio snippet during review is a natural future enhancement (§13.4) but is out of scope for v1 due to the complexity of audio output from inside a Docker container.

### `steps/mute.py` *(Step 5)*
- **Input:** `matches.json` (Step 4b's flag-phase output), `review.json` (if Step 4b's review phase ran), `dialog.wav`
- **Output:** `dialog_censored.wav`, `censor_log.json`
- **Does not call `steps/matching.py` or re-scan the transcript.** This is the point of splitting flagging out of Step 4b (see §4): Step 5 trusts `matches.json` completely and only ever applies `review.json` on top of it.
- **Logic:**
  1. Load `matches.json` (always present — the flag phase is not optional).
  2. If `review.json` exists, apply its overrides: drop any match whose `word_index` has a `skip` override; add a mute interval for every `add` override's `start`/`end` (these don't come from `matches.json` at all — they're the reviewer's manual corrections). Without `review.json` (unattended mode, or an interactive run with zero candidates), every match from step 1 is muted as-is.
  3. For each remaining match/addition, compute `[start - padding_ms, end + padding_ms]` interval
  4. Merge overlapping intervals
  5. `method: mute` (v1's only implemented method) — build ffmpeg `volume` filter expression:
     `volume=enable='between(t,s1,e1)+between(t,s2,e2)+...':volume=0`
  6. `method: beep` — **not yet implemented in v1** (§10, Phase 4 polish item). Step 5 raises a clear, actionable error rather than silently falling back to `mute` or producing an output that's actually muted but labeled as beeped.
- **`censor_log.json`** records every individual word/addition muted with its timestamp (both raw and padded) — useful for review and for the future correction workflow (§13.4)
- If zero intervals remain after overrides (no candidates were flagged, or all were rejected), `dialog_censored.wav` is a copy of `dialog.wav` and a warning is logged
### `steps/recombine.py` *(Step 6)*
- **Input:** `dialog_censored.wav` (Step 5), `score_sfx.wav` (Step 3b)
- **Output:** `audio_censored.wav`
- **Tool:** `ffmpeg -i dialog_censored.wav -i score_sfx.wav -filter_complex amix=inputs=2:duration=first:normalize=0 -c:a pcm_s16le audio_censored.wav`
  - `normalize=0` preserves original relative levels — `amix`'s default scales every input down by `1/N` to leave headroom, which with two already-mixed, full-range stems would quietly halve both the dialog and score/SFX levels relative to the original mix.
  - `-c:a pcm_s16le` is explicit, even though it doesn't appear in `amix`'s bare invocation above: left unset, the `.wav` muxer takes whatever sample format `amix` happens to negotiate internally (often float), and every other WAV in this pipeline is 44.1kHz/16-bit PCM (`steps/merge.py`'s concatenation depends on that being uniform) — pinning it here keeps that invariant.
- Both inputs are fully consumed once `audio_censored.wav` exists — Step 6b only ever needs the recombined file, not either stem again — so both are deleted here unless `keep_intermediates` is set, matching `steps/merge.py`'s and `steps/mute.py`'s cleanup pattern for their own now-superseded intermediates.
### `steps/encode.py` *(Step 6b)*
- **Input:** original video file (probed only — never opened as an ffmpeg input here), `audio_censored.wav` (Step 6)
- **Output:** `audio_encoded.mka`

**Why this step exists as its own pass, separate from muxing:** v1 originally re-encoded the censored audio *inside* Step 7's mux command — one ffmpeg invocation with the original video as one input (`-c:v copy`) and `audio_censored.wav` as a second input (`-c:a <encoder>`). Real-world testing against full-length films surfaced a bug on a subset of long, multi-subtitle-track Blu-ray rips: the final output played back "mostly silent," with audio present only in scattered spots — despite the audio data itself, extracted standalone, being complete and correct throughout (confirmed via `ffprobe -show_entries packet=pts_time` showing every single packet evenly spaced end-to-end, no gaps or jumps). The actual fault was a *timestamp-origin* mismatch: `ffprobe` on the affected outputs showed the copied video stream starting at the source's original `start_time` (e.g. `+0.023s`, a common Blu-ray-rip muxing artifact), while the same-command-encoded audio stream started at the *negative* of that same value (`-0.023s`). ffmpeg's cross-input timestamp reconciliation uses the first input's `start_time` as the zero-reference for any stream routed through its encode/filter pipeline, but leaves `-c:v copy` streams' original timestamps untouched — so a copy stream from one input and an encoded stream from another, combined in the same command, structurally end up on different timelines. Splitting the encode into its own single-input step removes the second input entirely from that command, so the freshly encoded audio has nothing else's `start_time` to get rebased against; Step 7 then becomes a pure stream-copy mux of two already-finalized files, going through ffmpeg's simpler, more predictable copy-only path on both sides.

**Probe phase, codec map, and fallback rules** are unchanged from the original Step 7 design below — only *where* they run has moved, one step earlier, to this module:

```bash
ffprobe -v quiet -select_streams a:0 \
  -show_entries stream=codec_name,profile,bit_rate,sample_rate,channels,channel_layout \
  -show_entries stream_tags=DELAY \
  -of json input.mkv
```

(`profile` is fetched in addition to the fields above — it's the only way to distinguish plain DTS from DTS-HD MA, since ffprobe reports `codec_name: "dts"` for both; see the codec map below.)

| Field | Used for |
|---|---|
| `codec_name` (+ `profile` for DTS) | select ffmpeg encoder (see codec map below) |
| `bit_rate` | `-b:a` target bitrate (falls back to a sensible per-encoder default if the source didn't report one — common for lossless/VBR codecs) |
| `sample_rate` | `-ar` resample target if WAV sample rate differs |
| `channel_layout` (falls back to a default by channel count if missing/`"unknown"`) | `-channel_layout` (implies channel count on its own — no separate `-ac` needed) |
| `DELAY` tag | `-metadata:s:a:0 DELAY={value}` to preserve offset, only if the tag is present (most files don't have one) |

**Codec map** (original codec → ffmpeg encoder):

| Original codec | Encoder used | Notes |
|---|---|---|
| `aac` | `aac` | Most common in MP4/M4V |
| `ac3` | `ac3` | Common in MKV rips |
| `eac3` | `eac3` | Enhanced AC-3 |
| `dts` (profile without "MA") | `dca` | Core/HRA DTS; ffmpeg's only DTS encoder, lossy |
| `mp3` | `libmp3lame` | |
| `flac` | `flac` | Lossless; preserves quality; no bitrate target |
| `vorbis` | `libvorbis` | Not in the original plan's table — `steps/extract.py`'s `CODEC_EXT` already anticipates this as a possible source codec, so the encoder map handles it rather than falling through to the "unrecognised codec" row below |
| `opus` | `libopus` | Same rationale as `vorbis` |
| `wmav2` | `wmav2` | Same rationale |
| `pcm_s16le`/`pcm_s24le`/`pcm_s32le` | `pcm_s16le` | Same rationale; lossless, no bitrate target |
| `truehd` / `mlp` | *(fallback)* | No ffmpeg encoder exists for either; fall back to `ac3` at the original bitrate capped at `ac3`'s 640kbps ceiling (or 640kbps outright if the source didn't report a bitrate, common for these formats) |
| `dts`, profile contains `"MA"` (DTS-HD MA) | *(fallback)* | `dca` can only produce the lossy DTS core layer, never the lossless MA extension — re-encoding under the original `dts` name would silently misrepresent what's actually in the output, so this falls back to `ac3` instead, same as `truehd` |
| anything else (unrecognised `codec_name`) | *(fallback)* | Defensive: an unrecognised codec shouldn't crash an otherwise-successful run; falls back to `ac3` with a warning |

When a fallback is used, a warning is logged clearly:
`WARNING: original codec '{codec}' cannot be re-encoded ({reason}). Falling back to ac3 at {bitrate} bps. Verify sync and quality before use.`

**ffmpeg command** (AC3 source with no `DELAY` tag, as an example):
```bash
ffmpeg \
  -i audio_censored.wav \
  -c:a ac3 -b:a {original_bitrate} -ar {original_sample_rate} -channel_layout {original_layout} \
  -avoid_negative_ts make_zero \
  -f matroska \
  audio_encoded.mka
```
`-avoid_negative_ts make_zero` is defensive only — a single-input encode has no other file's `start_time` to get pulled negative against in the first place — but it's pinned explicitly rather than left to ffmpeg's default, and costs nothing.

**Why `.mka` (Matroska Audio) regardless of which encoder was picked:** it's one generic, codec-agnostic container that every entry in the codec map above fits into without per-codec wrapper logic — AAC normally wants an ADTS/M4A wrapper, Vorbis/Opus normally want Ogg, WMA wants ASF, and raw AC3/DTS/MP3 streams have their own quirks as bare files. Using `.mka` for all of them means Step 7 never needs to know or care which encoder Step 6b picked; it only ever stream-copies whatever single audio track is inside.

**Intermediate cleanup:** `audio_censored.wav` is fully consumed once `audio_encoded.mka` exists — nothing downstream needs the raw PCM again — so it's deleted here unless `keep_intermediates` is set (the same cleanup `audio_censored.wav` used to get from Step 7, before this split). `audio_encoded.mka` itself is deleted by Step 7, once it's no longer needed there — never by this step, matching the convention that every step cleans up only its own *input*.
### `steps/mux.py` *(Step 7 — final step of v1's core pipeline)*
- **Input:** original video file, `audio_encoded.mka` (Step 6b)
- **Output:** `/output/{filename per output.naming_style}` — two supported styles:
  - `plex_edition` (default) — a Plex-friendly `{edition-Name}` tag (see [Plex's multi-edition docs](https://support.plex.tv/articles/multiple-editions/)), inserted right after the `(YYYY)` release-year portion of the filename if present, so Plex shows the censored file as a selectable Edition of the same movie instead of an unrelated second item: `"Movie (1986).sd.hevc.mkv"` → `"Movie (1986) {edition-Hushed}.sd.hevc.mkv"` (edition name configurable via `output.edition_name`). Falls back to appending the tag at the very end — still valid Plex syntax, since Plex's own docs say tag order doesn't matter to its parser — if no `(YYYY)` pattern is found in the filename at all.
  - `suffix` — the original plan's behavior: a plain suffix appended before the extension, no Plex Edition semantics. `Path(name).stem` strips only the final extension either way, so `"movie.sd.hevc.mkv"` → `"movie.sd.hevc_censored.mkv"`, not `"movie_censored.sd.hevc.mkv"`.
- **Both streams:** copied bitstream-exact (`-c:v copy -c:a copy`) — neither is re-encoded here. The encoding decision (which encoder, what bitrate, matching the original codec) now happens one step earlier, in `steps/encode.py` (Step 6b) — see that section above for why the split happened.

**ffmpeg command** (mkv example):
```bash
ffmpeg \
  -i video.mkv \
  -i audio_encoded.mka \
  -map 0:v:0 -map 1:a:0 -map 0:s? -map 0:t? -map_chapters 0 \
  -c:v copy -c:s copy -c:t copy -c:a copy \
  -avoid_negative_ts make_zero \
  -f matroska \
  output_censored.mkv.tmp.mkv   # renamed to output_censored.mkv only after ffmpeg exits 0
```
`-avoid_negative_ts make_zero` is defensive here too, for the same reason as Step 6b's — both streams are now plain copies of already-finalized files, so neither should have a reason to need this, but it costs nothing to pin explicitly.

**Subtitle/chapter/attachment preservation:** an extension beyond a bare `-map 0:v:0 -map 1:a:0`, which would silently drop every embedded subtitle track, chapter marker, and attachment (e.g. embedded fonts for ASS subtitles) — not just "other audio tracks" (see the note below, which is the only thing the original plan called out as dropped). For `format: mkv` (the default, and the option `config.yaml` already recommends specifically for "flexible codec support, no re-mux needed"), all three are cheap, lossless stream copies, so they're preserved (`-map 0:s? -map 0:t? -map_chapters 0`, with `-c:s copy -c:t copy`; the `?` means "include if present, skip without erroring if not" — no separate existence probe needed). For `format: mp4`, subtitle/attachment stream-copy compatibility is much less reliable — PGS/VOBSUB bitmap subtitles in particular generally aren't valid in MP4 at all, and attempting the copy would make ffmpeg fail outright rather than just producing a censored file without subtitles — so `mp4` output carries chapters forward (MP4 represents them internally as a hidden data track; `ffmpeg` already does this by default for a single input, but `-map_chapters 0` is kept explicit rather than relying on that default) but does not attempt subtitle/attachment passthrough.
 
**Crash safety:** the muxed file is written to a `.tmp` sibling that preserves the real extension (`movie_censored.tmp.mkv`, not `movie_censored.mkv.tmp` — ffmpeg's muxer auto-detection is extension-based and a trailing `.tmp` defeats it) inside `/output`, with an explicit `-f matroska`/`-f mp4` muxer flag as a second, non-extension-dependent safeguard, and is only renamed to its final name once ffmpeg exits `0`. This matches `utils.write_job`'s write-then-rename pattern, applied here because `/output`, unlike `/jobs`, is the one place in this pipeline a half-written file would be directly user-visible and easy to mistake for a finished one.
 
**Intermediate cleanup:** `audio_encoded.mka` is deleted once the final muxed video exists, unless `keep_intermediates` is set — see §6. `audio_raw.{ext}` is untouched by this step; it's always kept regardless of this setting (§6), for future per-channel reprocessing (§13.3).
 
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
  --skip-index N       Correction: un-mute the flagged match at this word_index
                       (see censor_log.json). Repeatable. Re-runs Steps 5, 6, 6b, 7 only.
  --add-interval TEXT START END
                       Correction: add a manual mute interval (seconds).
                       Repeatable. Re-runs Steps 5, 6, 6b, 7 only.
  --redo-review        Correction: re-enter interactive review from scratch
                       on an already-completed job (implies --interactive).
  --dry-run            Print the docker command without running it
  -h, --help
 
Examples:
  hush.sh movie.mkv
  hush.sh --interactive movie.mkv movie.srt
  hush.sh -o ~/censored/ movie.mkv movie.srt
  hush.sh --skip-index 4856 movie.mkv
  hush.sh --add-interval "missed word" 1203.1 1203.5 movie.mkv
```
 
The script resolves absolute paths before mounting — Docker requires absolute paths for `-v`.
 
The correction flags (`--skip-index`, `--add-interval`, `--redo-review`) are §13.4's correction workflow — see that section for the full design rationale, including why it's cheap (a few minutes, not a full re-run) rather than just a documented manual `review.json` edit.
 
---
 
## 10. Deliverables
 
### Phase 1 — Docker Foundation
- [x] `Dockerfile` (CPU-only, single target)
- [x] `docker-compose.yml` for workstation convenience
- [x] `hush.sh` wrapper script (with `--interactive` and `--jobs` flags)
- [x] `config/config.yaml` with documented defaults
- [x] `config/word_list.txt` with default English list (merged APF + orig; see §7.2 for format)
- [x] `README.md`: build, install, basic usage, expected runtimes
### Phase 2 — Core Pipeline
- [x] `steps/extract.py` (with multichannel downmix)
- [x] `steps/segment.py` (audio segmentation; passthrough for short files)
- [x] `steps/separate.py` (per-segment)
- [x] `steps/transcribe.py` (per-segment; global offset stored in per-segment JSON)
- [x] `steps/merge.py` (global timestamp application; stem concatenation)
- [x] `steps/matching.py` (shared word-list parsing + transcript matching; not in the original plan as its own module — called exactly once, from Step 4b's flag phase; Step 5 never calls it — see §4)
- [x] `steps/review.py` (Step 4b: flag phase always runs against the word list and writes `matches.json`; interactive review phase runs after it, conditional on `--interactive` / config)
- [x] `steps/mute.py` (Step 5: consumes Step 4b's `matches.json` + `review.json` directly — no transcript re-scan; `mute` method only — `beep` deferred to Phase 4)
- [x] `steps/recombine.py` (Step 6: amix `dialog_censored.wav` + `score_sfx.wav` → `audio_censored.wav`, `normalize=0`; deletes both inputs unless `keep_intermediates`)
- [x] `steps/encode.py` (Step 6b: codec probing + fallback map, standalone single-input re-encode of `audio_censored.wav` → `audio_encoded.mka`; split out of `mux.py` after a real-world A/V sync bug traced to combining a copy stream and a same-command encoded stream from two different inputs — see §8; deletes `audio_censored.wav` unless `keep_intermediates`)
- [x] `steps/mux.py` (Step 7: pure stream-copy mux — `-c:v copy -c:a copy` — of the original video and `audio_encoded.mka`, mkv subtitle/chapter/attachment preservation, atomic write-then-rename into `/output`; deletes `audio_encoded.mka` unless `keep_intermediates`) — **v1 core pipeline is now end-to-end complete**, Steps 1a through 7
- [x] `pipeline.py` orchestrator with segment loop and job state management
- [x] `utils.py` shared helpers (logging, config, job state, subprocess runner)
- [x] Job store: `job.json`, `transcript_NN.json`, `transcript.json`, `matches.json`, `review.json` (interactive runs only), `censor_log.json` written per run
- [x] Correction workflow (§13.4): `--skip-index` / `--add-interval` / `--redo-review`, backed by `output.keep_correction_artifacts` (default `true`) so Steps 5, 6, 6b, and 7 can redo without re-running Demucs
- [ ] End-to-end test with a short sample clip (unattended mode) — Steps 1a–6 validated against a full-length production film; Step 7 implemented and exercised against synthetic fixtures (AC3/DTS/TrueHD audio, embedded subtitles, chapters) but not yet run against real production data
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
| 9 | ~~Optimal default for `demucs.shifts`?~~ **REOPENED — see Open Question #11** | Original resolution assumed `--shifts 4` would cost ~32 hours for a 2-hour film, based on pre-implementation manual test data. Open Question #11's production data shows `--shifts 1` measures ~1.09× realtime, not the assumed ~4× — roughly a 4× gap. If runtime scales linearly with shifts (unverified — each shift re-runs inference once more per pass and averages; the 4 progress bars seen per segment are the bag-of-4-models ensemble, a separate axis from shifts), `--shifts 4` would extrapolate to ~4.4× realtime ≈ ~8.7 hrs for a 2-hour film — back in overnight-feasible territory, not the originally-assumed ~32 hrs. | **Default unchanged: `shifts: 1`.** The quality-vs-time tradeoff deferred in the original resolution is now far cheaper to actually test. A direct `shifts=4` timing run on one real segment would confirm or refute the linear-scaling assumption above before any default change is considered. |
| 10 | Optimal segment size? | 30 min was chosen based on memory exhaustion at full-film scale on 16 GB RAM. Smaller segments = more overhead (Demucs model load per segment); larger = more memory pressure. | 30 min appears safe at 16 GB; may be tunable upward on machines with more RAM. |
| 11 | ~~Does measured Step 2 runtime match the documented estimate?~~ **RESOLVED** | Production run on job `c9b47bf562dc` (real 2-hour film, 4 segments of 1800/1800/1800/1712 s) measured 1993/1954/1972/1852 s wall-clock respectively — a tight 1.08×–1.11× realtime range across all four independently-timed segments. Combined: 7771 s wall-clock for 7112 s of audio = **1.09× realtime**, consistent with the earlier 237 s clip's 1.09× (at INFO level) / 1.18× (at DEBUG level, full per-tick progress logging — adds measurable overhead, recommend INFO for real runs). The documented ~4× realtime / ~2 hrs-per-30-min-segment figure (this table's original Open Question #9 source data) is confirmed wrong by roughly 4×. | **Runtime tables revised — see §12.** `shifts=1` takes ~2.2 hours for a 2-hour film, not ~8 hours. This reopens the `shifts=4` question (Open Question #9) — see that row for the extrapolated (unmeasured) shifts=4 estimate. |
 
---
 
## 12. Known Limitations (v1)
 
- **Separation artifacts:** Demucs is excellent but not perfect. Some bleed between dialog and score/SFX stems will occur, especially in scenes with overlapping dialog and dramatic music. The recombined audio will not be bit-for-bit identical to the original even in uncensored sections.
- **Multi-channel audio downmixed to stereo:** Source files with surround sound (5.1, 7.1, Atmos, etc.) are downmixed to stereo for processing. The output audio will be stereo regardless of the original channel count. The original audio stream is preserved bit-for-bit in the job store, so future per-channel reprocessing is possible without re-extracting from the video. See §13.3 for the intended future approach.
- **Memory: segmentation required for large files:** Demucs (`htdemucs_ft`) processing a full-length film in one pass exhausts 16 GB of system RAM, causing OOM failure. Segmentation (Step 1c) is the mitigation. The default segment size of 30 minutes has been validated on a 16 GB machine. Segments are processed serially; peak memory per segment is bounded.
- **Homophone/mishearing false positives:** WhisperX may occasionally transcribe an innocent word as a profanity match. Interactive review mode exists specifically to catch these before they result in a muted output.
- **Context-blind matching:** Word list matching has no understanding of usage context. This is partially mitigated by case-sensitive (`=`) entries — for example, `=dick` flags the lowercase profane form while leaving `Dick` (a name, which WhisperX capitalizes) unflagged. However, this heuristic only works for words whose profane and proper-noun forms differ in capitalization. "God" (in prayer vs. as an expletive), "butt" (donkey vs. insult), and similar cases remain indistinguishable at the word-list level. Interactive review is the immediate mitigation for these; context-aware detection (§13.2) is the long-term solution.
- **Overlapping dialog:** Scenes where multiple people speak simultaneously will have reduced Whisper accuracy.
- **Processing time:** CPU-only is slow by design, but considerably faster than first assumed. Verified against production data (`htdemucs_ft`, `--shifts 1`, 16 GB RAM, INFO-level logging): a real 2-hour film (4 segments of 1800/1800/1800/1712 s) measured **1.09× realtime** overall (7771 s wall-clock for 7112 s of audio), consistent across all four independently-timed segments (range 1.08×–1.11×) and matching an earlier short-clip test. **A 2-hour film takes approximately 2.2 hours for Step 2 at the default `shifts: 1`** — not the ~8 hours previously documented here (see Open Question #11; the original ~4× realtime figure came from pre-implementation manual testing and is now superseded). `--shifts 4` has not been directly measured; if runtime scales linearly with shifts (unverified), it would extrapolate to ~4.4× realtime ≈ ~8.7 hours for a 2-hour film, which reopens the shifts=4 feasibility question (Open Question #9) — substantially cheaper than the ~32 hours originally assumed when `shifts: 1` was chosen as the default. WhisperX `large-v2` adds approximately 30–60 minutes per film regardless of segment count (still an estimate — Steps 3/3b are now implemented but have not yet been timed against a real production film; update this figure once a real run completes). Steps 1a–1c (extract, downmix, segment) are comparatively negligible — under a minute total even for a multi-GB source file, since both the raw-audio extraction and the segment split use stream copy rather than re-encoding. Note: running with `AC_LOG_LEVEL=debug` measured a ~8% time premium on the one clip tested (1.18× vs 1.09× realtime) from the volume of per-tick progress-bar logging — recommend INFO level for real overnight runs.
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
 
**Recommended path:** Option C as a near-term improvement within v1's architecture (just an extension of `steps/matching.py`'s `find_matches()`, called from `steps/review.py`'s flag phase), with Option A as the longer-term target when a local LLM is available in the environment. Option B is only worth pursuing if a suitable labeled dataset can be sourced.
 
**Architectural note:** The WhisperX transcript already includes surrounding word context. No change to earlier pipeline steps is needed. Context-aware detection is a drop-in replacement for the word-list matching logic in `steps/matching.py` — `steps/mute.py` (Step 5) is unaffected either way, since it only ever consumes the flag phase's `matches.json` output, regardless of how those matches were produced.
 
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
 
4, 4b: SRT alignment, flag & review — same as v1
 
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
 
### 13.4 Correction Workflow & Resume — **Implemented**
 
The interactive review in Step 4b is v1's primary quality mechanism, but it can only catch what a human notices *during* that review pass — re-reading the same transcript context Step 4b already showed. Some errors only become apparent later, when actually watching the finished film: a word muted that shouldn't have been (false positive — e.g. WhisperX mis-transcribing "Ned! Land!" as "What the hell"), or a word that should have been caught but wasn't (false negative — not in the word list, or a transcription variant the word list didn't anticipate). This is `pipeline.py`'s correction mode, for exactly that case.
 
**The primary expected workflow:**
 
1. Run `hush.sh movie.mkv` normally (unattended or interactive)
2. Watch the film, ideally with the people it was censored for
3. Note any mistakes — for a false positive, the muted moment's approximate timestamp is enough to find the `word_index` in `censor_log.json` (every muted entry records its source word/timestamp); for a false negative, note the timestamp and what was actually said
4. Re-run `hush.sh` on the **same** input file with a correction flag:
 
```bash
# False positive: un-mute the flagged match at this word_index
hush.sh --skip-index 4856 movie.mkv
 
# False negative: add a manual mute interval (seconds)
hush.sh --add-interval "missed word" 1203.14 1203.48 movie.mkv
 
# Both flags are repeatable and combinable in one invocation
hush.sh --skip-index 4856 --skip-index 412 --add-interval "oops" 88.0 88.4 movie.mkv
```
 
Because `compute_job_id()` is path+mtime based, re-running on the unmodified input file naturally lands on the same job — there's no separate `--resume {job_id}` flag to remember. The correction edits `review.json` (additively — repeated correction runs accumulate, and a duplicate `--skip-index` is detected and skipped rather than recorded twice) and invalidates `5_mute`, `6_recombine`, `6b_encode`, and `7_mux` in `steps_completed`, so the normal step machinery redoes exactly those four steps — typically a few minutes, not the hours Steps 1–3b took. Step 4b's `flag()` phase is untouched: `matches.json` doesn't change, only what's layered on top of it.
 
**`--redo-review`** is the alternative, fuller-pass option: it re-enters Step 4b's interactive Y/N/A/S/Q loop from scratch (implying `--interactive` for that run), re-presenting *every* flagged match, not just the one that was wrong. Useful for a more thorough re-pass; `--skip-index`/`--add-interval` are faster for a single targeted fix and are what the primary workflow above is built around. The two approaches can't be combined in one invocation — `--redo-review` rewrites `review.json` from scratch and would discard direct edits made moments earlier in the same run, so `pipeline.py` rejects the combination outright rather than silently dropping one of them.
 
**What makes this cheap rather than a full re-run:** `dialog.wav` and `score_sfx.wav` — the two canonical pre-mute audio stems — are kept by default (`output.keep_correction_artifacts: true`), independent of `output.keep_intermediates` (default `false`, which still governs the per-segment files and the more trivially-regenerable `dialog_censored.wav`/`audio_censored.wav`/`audio_encoded.mka`). This is *the* reason correction mode can redo Steps 5, 6, 6b, and 7 in minutes instead of needing Step 2's Demucs separation — often the single most expensive step in the whole pipeline — all over again. If a job was run with `keep_correction_artifacts: false`, or predates this setting, correction mode fails with a clear error (rather than silently falling back to re-separating) explaining that a full re-run from scratch is needed instead.
 
**Implementation:** `steps/review.py`'s `apply_corrections()` (the scriptable counterpart to its interactive `review()`) writes the overrides directly; `utils.unmark_step_done()` is the bookkeeping primitive that forces a step to redo. See `pipeline.py`'s module docstring and `steps/mute.py`/`steps/recombine.py` for the retention-policy half of this.
 
**Remaining future work:**
- **Audio playback during review:** extending the interactive loop (or a companion tool) to play the audio snippet around each flagged word directly, so deciding doesn't require re-watching the film. `--skip-index`/`--add-interval` reduce how often this matters (the person already watched the film once to find the issue), but wouldn't replace it for `--redo-review`'s fuller pass.
- **Multi-track audio:** future multi-track preservation (§13.3) would need this workflow extended to correcting a word that appears only in a surround channel.
- **Interactive re-entry efficiency:** `--redo-review` re-presents every match from scratch rather than only ones without an existing decision — acceptable for the matches-in-the-tens-to-low-hundreds range v1 has been validated against, but a future enhancement could pre-load prior decisions and only prompt for what's new.
 
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

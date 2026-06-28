# profanity-hush

Automatically censor profanity from movie files. Feed it a video; get back a censored copy. Designed for unattended overnight CPU-only runs — no GPU required.

---

## How it works

1. **Extract** — Pull the raw audio track from the video (bitstream copy, no re-encode)
2. **Separate** — Demucs (`htdemucs_ft`) splits the audio into a dialog stem and a music/SFX stem
3. **Transcribe** — WhisperX produces word-level timestamps from the dialog stem
4. **Match** — Words are compared against a configurable word list with exact, starts-with, and substring matching
5. **Mute** — Flagged words are silenced only in the dialog stem; music and sound effects play through uninterrupted
6. **Recombine** — The stems are mixed back together and muxed into the output video (video stream is a bit-for-bit copy)

Optionally: cross-reference an SRT subtitle file (Phase 3) or pause for interactive review before muting (available now via `--interactive`).

---

## Requirements

- **Docker** — tested on Linux (Manjaro/Arch and Ubuntu). Docker Desktop on macOS and Windows should work but is untested.
- **~3 GB of free disk** for model weights (downloaded on first run, cached afterward)
- **16 GB RAM** recommended — Demucs is memory-hungry. See [Expected Runtimes](#expected-runtimes).
- No GPU needed.

---

## Installation

### 1. Build the Docker image

```bash
git clone https://github.com/yourname/profanity-hush.git
cd profanity-hush
docker build -t profanity-hush .
```

Image size is approximately 1.1 GB. Model weights (~2–3 GB) are downloaded on the first run and cached — always use a persistent cache directory (see below).

Using `docker compose`:

```bash
docker compose build
```

### 2. Make `hush.sh` executable

```bash
chmod +x hush.sh
```

### 3. (Optional) Install config files

The pipeline ships with working defaults. To customize, copy the config to your user config directory:

```bash
mkdir -p ~/.config/profanity-hush
cp config/config.yaml ~/.config/profanity-hush/
cp config/word_list.txt ~/.config/profanity-hush/
```

If you skip this step, `hush.sh` will warn that the config directory is empty and the container will use its built-in defaults. For a real run you'll want your own `word_list.txt`.

---

## Usage

```
hush.sh [OPTIONS] <input_video> [subtitle_file]

Options:
  -o, --output DIR      Output directory (default: same directory as input)
  -c, --config DIR      Config directory (default: ~/.config/profanity-hush)
      --cache  DIR      Model cache directory (default: ~/.cache/profanity-hush)
      --jobs   DIR      Job history directory (default: ~/.local/share/profanity-hush/jobs)
      --interactive     Pause for review of flagged words before muting
      --no-interactive  Force unattended mode (overrides config.yaml)
      --keep-tmp        Keep large intermediate WAV stems after the run
      --skip-index N    Correct a false positive — see "Correcting Mistakes" below
      --add-interval TEXT START END
                        Correct a false negative — see "Correcting Mistakes" below
      --redo-review     Re-enter interactive review on an already-completed job
      --redo-step STEP  Force a single step to redo on an existing job — see
                        "Re-running a Single Step" below
      --dry-run         Print the docker command without executing it
  -h, --help
```

### Examples

```bash
# Basic — censor a film, output alongside the input
./hush.sh movie.mkv

# With an SRT file for improved accuracy (Phase 3, coming soon)
./hush.sh movie.mkv movie.srt

# Review flagged words before committing to a muted output
./hush.sh --interactive movie.mkv

# Send output to a specific directory
./hush.sh -o ~/censored/ movie.mkv

# Preview what docker command would run (no processing)
./hush.sh --dry-run movie.mkv
```

By default, the output file is named for Plex's `{edition-Name}` convention, inserted right after the release year so Plex shows it as a selectable Edition of the same movie:
`Movie (1986).sd.hevc.mkv` → `Movie (1986) {edition-Hushed}.sd.hevc.mkv`
Set `output.naming_style: suffix` in `config.yaml` for a plain suffix instead: `movie.mkv` → `movie_censored.mkv`.

### Using docker compose (workstation)

```bash
# Copy and edit the environment file
cp .env.example .env
# Edit .env — at minimum set INPUT_DIR to the directory containing your video

# Run
docker compose run --rm hush /input/movie.mkv
docker compose run --rm hush /input/movie.mkv --interactive
```

---

## Expected Runtimes

CPU-only processing is intentionally slow — runs are queued overnight.

Measured on a 16 GB machine, `htdemucs_ft`, `large-v2` WhisperX:

| Stage | Per segment (30 min) | For a 2-hour film |
|---|---|---|
| Demucs `--shifts 1` (default) | ~33 min | ~2.2 hours (4 segments) |
| Demucs `--shifts 4` (quality) | ~2 hours (estimated†) | ~8.7 hours (estimated†) |
| WhisperX `large-v2` | — | ~30–60 min total |
| **Total (shifts=1)** | | **~3–4 hours** |

† `shifts=4` runtime is a linear extrapolation from the measured `shifts=1` figure;
  it has not yet been directly timed.

`shifts=1` is the default. It produces good quality output. `shifts=4` may improve
quality further at roughly 4× the compute cost — use it only if you have time to
spare and have validated the quality difference is meaningful on your content
(see `config.yaml`).

**Memory:** Peak memory is bounded per segment by the 30-minute segment size (default).
Reduce `audio.segment_size_sec` in `config.yaml` if you see OOM errors on machines
with less than 16 GB RAM.

---

## Configuration

### `config/config.yaml`

The main settings file. Key options:

```yaml
demucs:
  model: htdemucs_ft   # htdemucs | htdemucs_ft | htdemucs_6s
  shifts: 1            # 1 = fast/default, 4 = quality, 10 = maximum

whisperx:
  model: large-v2      # large-v2 (recommended) | medium | small
  language: en         # ISO 639-1; null for auto-detect

audio:
  segment_size_sec: 1800   # 30 min per segment; reduce if OOM

censoring:
  method: mute         # mute | beep
  padding_ms: 50       # silence added before/after each word (ms)

output:
  naming_style: plex_edition   # plex_edition | suffix -- see Usage examples above
  format: mkv                  # mkv | mp4
  keep_intermediates: false
  keep_correction_artifacts: true   # keeps dialog.wav/score_sfx.wav so corrections stay cheap
```

See the full file at `config/config.yaml` for all options and their documentation.

### Environment variable overrides

| Variable | Effect |
|---|---|
| `AC_LOG_LEVEL` | `debug` / `info` / `warning` |
| `AC_KEEP_INTERMEDIATES=1` | Keep all large WAV stems after run (same as `--keep-tmp`) |
| `AC_KEEP_CORRECTION_ARTIFACTS=0` | Don't keep `dialog.wav`/`score_sfx.wav` either (these default to kept — see [Correcting Mistakes](#correcting-mistakes)) |
| `AC_INTERACTIVE=1` | Enable interactive review (same as `--interactive`) |
| `AC_SEGMENT_SIZE` | Override `audio.segment_size_sec` in seconds; `0` disables segmentation |
| `AC_TZ_OFFSET` / `AC_TZ_NAME` | Host UTC offset (e.g. `-0700`) / cosmetic abbreviation (e.g. `PDT`) used for log timestamps. `hush.sh` sets both automatically from the host's clock — see [Logging](#logging) below — only needed by hand if you're running the container some other way. |

```bash
AC_LOG_LEVEL=debug ./hush.sh movie.mkv
AC_SEGMENT_SIZE=900 ./hush.sh movie.mkv   # 15-min segments
```

### `config/word_list.txt`

One entry per line. Comments (`#`) and blank lines are ignored. Supports exact, starts-with, substring, and case-sensitive matching:

| Notation | Matches |
|---|---|
| `word` | Exact, case-insensitive |
| `=word` | Exact, case-sensitive — useful for distinguishing profanity from proper nouns |
| `word*` | Starts-with, case-insensitive |
| `*word*` | Substring, case-insensitive |

WhisperX capitalizes proper nouns naturally, so `=dick` catches the profane usage while leaving `Dick` (a name) untouched.

---

## Logging

Console timestamps automatically match this machine's local clock: `hush.sh` detects your current UTC offset (`date +%z`/`%Z`) and forwards it into the container, so every log line is stamped in your own time zone instead of the container's default UTC. If that detection ever fails (or you're running the container some other way — see `AC_TZ_OFFSET`/`AC_TZ_NAME` above), timestamps fall back to UTC, and are clearly labelled as such rather than looking like local time that's quietly several hours off.

```
2026-06-25 16:25:41 -0700 [INFO ] [separate ] [2/4] dialog.wav (412 MB) ...
```

`job.json`'s `started_at`/`failed_at`/`completed_at` fields stay in UTC (ISO 8601, with a `+00:00` offset) — useful for comparing job records regardless of which time zone a given run happened to log in — alongside `*_local` companions for convenience when reading the file directly. The job folder's own leading timestamp (see [Job History](#job-history) below) uses the same local time as everything else above, for the same reason: it's a place you're likely to actually look (browsing the jobs folder directly), so it should read as what your clock said, not require doing offset arithmetic.

---

## Job History

Every run creates a job record under `~/.local/share/profanity-hush/jobs/`, in a folder named `YYYYMMDD_HHMMSS_<movie-slug>_<hex8>` (the timestamp is your local time — see [Logging](#logging) above — and the slug makes it easy to spot the right job by filename without opening anything). Transcript JSON files and the censor log are always preserved, along with `dialog.wav` and `score_sfx.wav` (the pre-mute audio stems) — together these are what makes [correcting a mistake](#correcting-mistakes) after watching the film fast, without repeating the expensive separation and transcription steps.

Large intermediate WAV files are deleted by default once each is no longer needed. Pass `--keep-tmp` to retain all of them (including ones not needed for corrections); see `output.keep_correction_artifacts` in `config.yaml` to control just the two needed for corrections independently.

Files are written owned by the user who ran `hush.sh`, not root. If you have job, cache, or output files from before this was fixed, they'll still be owned by root — clean them up once with:

```bash
sudo chown -R "$(id -u):$(id -g)" \
    ~/.local/share/profanity-hush \
    ~/.cache/profanity-hush
```

---

## Interactive Review

Pass `--interactive` to pause before muting and review each flagged word:

```
[3 of 11]  Word: "crap"  |  Confidence: 0.94  |  Time: 00:23:14.8 – 00:23:15.1
Context: "...and then he said crap right in front of..."
Action? [Y]es / [N]o / [A]dd word / [S]kip rest / [Q]uit  >
```

- **Y** — approve; word will be muted (default)
- **N** — reject; word will not be muted
- **A** — add a missed word/phrase: searches the transcript for it first (picks automatically if there's one match, lets you choose if there are several); falls back to manual timestamp entry if it's not found at all
- **S** — approve all remaining without prompting
- **Q** — abort without writing output

Requires a real terminal. `hush.sh --interactive` allocates one automatically; running the container directly needs `-it` on `docker run`.

---

## Correcting Mistakes

This is the expected day-to-day workflow: run unattended, watch the film (maybe with the people it was censored for), and fix anything wrong afterward — without waiting through separation and transcription again.

**False positive** (a word got muted that shouldn't have been — e.g. WhisperX mis-hearing dialogue): find the entry in that job's `censor_log.json` (under `~/.local/share/profanity-hush/jobs/<job-folder>/` — see [Job History](#job-history) for the folder naming) by its approximate timestamp and note its `word_index`:

```json
{
  "source": "matched",
  "word": "hell",
  "entry": "hell",
  "word_index": 4856,
  "start": 5275.01,
  "end": 5275.23
}
```

(In this real example, WhisperX had transcribed the line "Ned! Land!" as "What the hell" — a transcription error, not a word-list problem; the word list correctly matched the literal text WhisperX produced.) Then:

```bash
./hush.sh --skip-index 4856 movie.mkv
```

**False negative** (something that should have been muted wasn't): note the timestamp while watching, then:

```bash
./hush.sh --add-interval "missed word" 1203.14 1203.48 movie.mkv
```

Both flags are repeatable and combinable in one run:

```bash
./hush.sh --skip-index 4856 --skip-index 412 --add-interval "oops" 88.0 88.4 movie.mkv
```

Re-running on the same input file (same path, unchanged) automatically finds the existing job — no job ID to look up or pass. Only the muting, recombining, and muxing steps (5–7) redo; typically a couple of minutes, not the original multi-hour run. This depends on `dialog.wav`/`score_sfx.wav` still being in the job directory, which is the default (`output.keep_correction_artifacts: true`) — if you've set that to `false`, or `--keep-tmp` wasn't used before that setting existed, the fix needs a full re-run instead, and `hush.sh` will say so clearly rather than silently doing the expensive thing.

**Prefer a fuller second look instead?** `--redo-review` re-enters the interactive review loop from scratch (every flagged word, not just the one you noticed) on an already-completed job:

```bash
./hush.sh --redo-review movie.mkv
```

This can't be combined with `--skip-index`/`--add-interval` in the same run — it rewrites the review file from scratch and would discard direct edits made moments earlier.

---

## Re-running a Single Step

`--skip-index`/`--add-interval`/`--redo-review` above are for fixing a *content* mistake — the word list or transcript got something wrong. `--redo-step` is for a different situation: you've changed how a **step itself** works (swapped the muxer, tuned a mute padding value, fixed an encode setting) and want to re-test that change against a job that already finished, without re-running everything before it:

```bash
./hush.sh --redo-step 7_mux movie.mkv
```

Repeatable, and valid for `4b_flag`, `4b_review`, `5_mute`, `6_recombine`, `6b_encode`, and `7_mux`. Steps 1a–3b aren't offered: they're resumed as a single atomic block, and their per-segment intermediates may already be gone, so redoing one of them alone isn't safe. `--redo-step` never touches `review.json` and can't be combined with `--skip-index`/`--add-interval`/`--redo-review` in the same run.

It also requires the job to actually be found first: if `compute_job_id()` doesn't land on an existing job for this input file (same path, unchanged), `hush.sh` refuses with a clear error rather than silently falling through to a full from-scratch run. This is also why hand-editing `steps_completed` in `job.json` directly isn't recommended, even though each step does check its own entry independently and the edit can appear to work: a single stray character (a trailing comma is the classic one) makes the whole file invalid JSON, and an unparseable `job.json` looks identical to "no job exists yet" to the code that's trying to find it — the visible symptom is a full multi-hour re-run with no explanation, not an error. `--redo-step` is the safe, validated way to get the same result.

---

## Limitations

- **Stereo output only (v1):** Multi-channel surround audio (5.1, 7.1) is downmixed to stereo for processing. The original audio is preserved in the job store; per-channel processing is planned for a future version.
- **Separation artifacts:** Demucs is excellent but not perfect — some bleed between stems is expected, especially in dense action scenes.
- **Context-blind matching:** The word list has no understanding of usage context. `=dick` / `Dick` case distinction is the primary mitigation; interactive review handles the rest.
- **v1 processes only the primary audio track.** Commentary tracks and alternate language tracks in the source container are dropped.

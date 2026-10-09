"""
profanity-hush — Step 1a: extract raw audio bitstream
                  Step 1b: downmix to stereo WAV

Both functions are called in sequence by the pipeline orchestrator and are
tracked as separate entries in job.json's steps_completed list -- and, as of
the integrity checks below, that list is actually consulted on resume rather
than only recorded into. See _validate_audio_raw()'s docstring for the
incident that made that distinction matter (job 1d55099e2bb7): an
interrupted `ffmpeg -c:a copy` left a truncated audio_raw.mp3 on disk, and
because the old resume check here was just "does the file exist," the next
run silently accepted 7:51 of a ~100-minute film as a finished extraction
and carried on into the downmix, segmentation, and a Demucs pass, all on
data that was never complete to begin with.
"""
import json
import logging
from pathlib import Path
from typing import Optional

import audio_processing
from utils import (
    check_duration_matches,
    finalize_output,
    fmt_duration,
    fmt_size,
    mark_step_done,
    probe_duration_sec,
    read_job,
    run_cmd,
    sha256_file,
    step_logger,
    tmp_output_path,
    unmark_step_done,
    write_job,
)


# Maps ffprobe codec_name to the file extension used for audio_raw.{ext}.
# The goal is a lossless bitstream copy, so the extension must match what
# the muxer expects to demux without re-encoding.
CODEC_EXT: dict[str, str] = {
    "aac":       ".aac",
    "ac3":       ".ac3",
    "eac3":      ".eac3",
    "dts":       ".dts",
    "mp3":       ".mp3",
    "flac":      ".flac",
    "truehd":    ".truehd",
    "mlp":       ".mlp",
    "vorbis":    ".ogg",
    "opus":      ".opus",
    "pcm_s16le": ".wav",
    "pcm_s24le": ".wav",
    "pcm_s32le": ".wav",
    "wmav2":     ".wma",
}


# ── Step 1a ───────────────────────────────────────────────────────────────────

def extract_raw(
    video_path: Path,
    job_dir: Path,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 1a: probe the primary audio stream and extract it as a bitstream copy.

    The bitstream copy is byte-identical to the audio stream as stored in
    the source container — no decode, no re-encode.  It is always kept in
    the job store regardless of keep_intermediates, because it is the
    essential resume artifact for future per-channel reprocessing (§13.3)
    and for --redo-audio (audio_processing.py): it is THE unaltered copy
    every what-if audio redo starts from.

    Resume support:
      Gated on '1a_extract_raw' in job.json's steps_completed, not on
      audio_raw.{ext}'s mere existence -- and either way (fresh extraction
      or resumed), the result is run through _validate_audio_raw() before
      being trusted. The ffmpeg call itself writes to a temp path and is
      only published under audio_raw.{ext} via finalize_output() once it
      has actually succeeded, so a run interrupted mid-extraction leaves
      nothing under the final name at all for a future resume to
      misidentify as complete.

    Writes audio codec metadata (including the source title tag, when
    present) to job.json and marks '1a_extract_raw' done.
    Returns the path to audio_raw.{ext}.
    """
    if log is None:
        log = step_logger("extract")

    state = read_job(job_dir)

    if "1a_extract_raw" in state.get("steps_completed", []):
        audio    = state.get("audio", {})
        raw_name = audio.get("raw_file", "")
        out_path = job_dir / raw_name if raw_name else None
        if not raw_name or not out_path.exists():
            raise RuntimeError(
                f"Step 1a is marked complete but its output file is missing "
                f"(expected '{raw_name or '?'}' in {job_dir}).  "
                "Delete the job directory and re-run from scratch."
            )
        log.info("Step 1a — ↩  already complete; verifying %s ...", out_path.name)
        expected_source_duration = audio.get("source_duration_sec")
        if expected_source_duration is None:
            # job.json predates this field (a job directory from before this
            # integrity check existed -- the old code marked '1a_extract_raw'
            # done unconditionally, whether or not the file it pointed to
            # was actually complete). Fall back to re-probing video_path
            # directly rather than skipping this half of the check outright.
            expected_source_duration = probe_duration_sec(video_path, log)
        _validate_audio_raw(job_dir, out_path, expected_source_duration, log)
        log.info("  ✓  %s passed integrity check — reusing.", out_path.name)
        return out_path

    log.info("Step 1a — probing audio stream: %s", video_path.name)

    stream  = _probe_audio_stream(video_path, log)
    codec   = stream.get("codec_name", "unknown")
    ch      = stream.get("channels", 0)
    layout  = stream.get("channel_layout", "unknown")
    rate    = stream.get("sample_rate", "?")
    bitrate = stream.get("bit_rate", "?")
    title   = stream.get("tags", {}).get("title")

    # Used by _validate_audio_raw() below and persisted to job.json so a
    # later resume can re-validate without needing video_path (which may
    # sit on a NAS/network mount not guaranteed reachable at resume time)
    # to still be around at all.
    raw_duration    = stream.get("duration")
    source_duration = float(raw_duration) if raw_duration else None
    if source_duration is None:
        # Rare -- most containers report per-stream duration -- but fall
        # back to the container's own duration rather than skip
        # validation outright.
        source_duration = probe_duration_sec(video_path, log)

    log.info(
        "  codec: %s  |  channels: %d (%s)  |  sample_rate: %s Hz  |  bitrate: %s bps",
        codec, ch, layout, rate, bitrate,
    )
    # The stream title is a free-text label a human sees in a media
    # player's audio-track picker (e.g. "Surround 7.1") -- it's set once
    # at encode time and never validated against the stream's actual
    # codec/channels/bitrate, so the two can silently disagree (a title
    # promising "Surround 7.1" on a stream ffprobe reports as 2-channel
    # mp3 -- exactly the case above -- means the file was downmixed at
    # some point and the title was never updated to match it). Logged
    # unconditionally, not only when present, so a missing title is just
    # as visible here as one that contradicts the probed values, rather
    # than either kind only being noticed by ear in a media player.
    log.info("  title: %s", title if title else "(none)")
    if ch > 2:
        log.info(
            "  ℹ  Multi-channel source (%s ch, %s) — will be downmixed to stereo at Step 1b.",
            ch, layout,
        )

    ext      = CODEC_EXT.get(codec, f".{codec}")
    out_path = job_dir / f"audio_raw{ext}"
    tmp_path = tmp_output_path(out_path)

    log.info("  Extracting bitstream copy → %s ...", out_path.name)
    tmp_path.unlink(missing_ok=True)   # clear a partial attempt from an interrupted prior run
    run_cmd(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-y", "-i", str(video_path),
            "-vn", "-c:a", "copy",
            str(tmp_path),
        ],
        log,
    )
    finalize_output(tmp_path, out_path)
    log.info("  ✓  %s  (%s)", out_path.name, fmt_size(out_path))

    _validate_audio_raw(job_dir, out_path, source_duration, log)
    log.info("  ✓  %s passed integrity check.", out_path.name)

    # A durable provenance record, not an active gate: -c:a copy makes
    # out_path a verbatim bitstream copy of the source's audio stream, so
    # this hash is -- for practical purposes -- a hash of "the audio we
    # started from," computed here (once, against the local file we just
    # wrote) rather than by re-reading video_path itself, which may sit on
    # a slower NAS/network mount. It's logged and persisted so a person
    # can confirm later whether a source file is "the same" one a given
    # job was built from; it isn't re-verified automatically on every
    # resume (that would reintroduce a dependency on the source still
    # being reachable, which source_duration_sec's fallback above
    # deliberately avoids) and a failure to compute it never blocks the
    # step -- see sha256_file()'s own docstring. (--redo-audio DOES verify
    # it, once, before spending Demucs hours on this file: see
    # audio_processing.prepare_audio_redo().)
    source_hash = sha256_file(out_path, log)
    log.info("  sha256: %s", source_hash or "(could not be computed)")

    # Persist audio metadata for later steps and for job inspection
    state["audio"] = {
        "codec":               codec,
        "channels":            ch,
        "channel_layout":      layout,
        "sample_rate":         rate,
        "bit_rate":            bitrate,
        "title":               title,
        "raw_file":            out_path.name,
        "source_duration_sec": source_duration,
        "source_audio_sha256": source_hash,
    }
    write_job(job_dir, state)
    mark_step_done(job_dir, "1a_extract_raw")

    return out_path


# ── Step 1b ───────────────────────────────────────────────────────────────────

def downmix_to_stereo(
    job_dir: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 1b: decode audio_raw.{ext} and downmix to stereo PCM WAV.

    Output spec: 44.1 kHz, 2 channels, pcm_s16le.
    The '-ac 2' flag handles any input channel layout:
      mono    → upmixed to stereo
      stereo  → passthrough
      5.1/7.1 → downmixed to stereo with standard coefficient matrix

    audio_processing.center_boost_db (config.yaml; see audio_processing.py's module
    docstring for the reasoning and measurements): when audio_processing is enabled,
    the boost is non-zero, and the source layout is one audio_processing has a verified
    center matrix for, '-ac 2' is replaced by an explicit `pan` downmix with the center
    channel that much louder relative to the other channels (matrix renormalized to unit
    row sum -- the same no-clip guarantee '-ac 2' gives). Everything else -- disabled,
    0 dB, stereo/mono, an unsupported layout -- runs plain '-ac 2' exactly as before. What
    was used is recorded in job.json's "downmix" block (method, filter, requested vs
    applied boost, source layout, matrix, note); audio_processing.drift_messages()
    compares it to config.yaml on later runs. The boost is baked into everything
    downstream of this step (the Demucs stems), so changing it later is --redo-audio.

    This is v1's deliberate multi-channel boundary (§13.3).  The original
    audio_raw.{ext} is always preserved for future per-channel reprocessing.

    audio_stereo.wav is large (~300 MB/hour) and is kept only when
    keep_intermediates is set; otherwise it is deleted after Step 3b (merge)
    completes -- not Step 2. Step 2 (separate.py) only reads this file (or
    its per-segment audio_stereo_NN.wav splits) as Demucs input and never
    deletes it; the actual cleanup happens in steps/merge.py, alongside the
    per-segment splits, once they're no longer needed. See steps/merge.py's
    module docstring and design doc §6.

    Resume support:
      Gated on '1b_downmix' in job.json's steps_completed, not on
      audio_stereo.wav's mere existence, with the same
      write-to-temp-then-finalize treatment as extract_raw() above and an
      integrity check (duration vs. audio_raw, which extract_raw() already
      validated before this function ever sees it) applied whether this is
      a fresh downmix or a resumed one.

    Marks '1b_downmix' done.  Writes a 'downmix' block to job.json naming
    the output file (mirrors '1a_extract_raw's own "audio" block above --
    kept separate rather than folded into it, since this describes a
    structurally different artifact: always 2ch/44.1kHz/pcm_s16le,
    regardless of the source's own codec/channels/bitrate) and the downmix
    settings actually used.
    Returns path to audio_stereo.wav.
    """
    if log is None:
        log = step_logger("extract")

    state    = read_job(job_dir)
    audio    = state.get("audio", {})
    raw_name = audio.get("raw_file", "")
    raw_path = job_dir / raw_name if raw_name else None

    if raw_path is None or not raw_path.exists():
        raise RuntimeError(
            f"Step 1b: audio_raw file not found in {job_dir} "
            f"(expected '{raw_name}') — did Step 1a complete?"
        )

    ch     = audio.get("channels", 0)
    layout = audio.get("channel_layout", "?")
    out    = job_dir / "audio_stereo.wav"

    if "1b_downmix" in state.get("steps_completed", []):
        if not out.exists():
            raise RuntimeError(
                f"Step 1b is marked complete but {out} is missing.  "
                "Delete the job directory and re-run from scratch."
            )
        log.info("Step 1b — ↩  already complete; verifying audio_stereo.wav ...")
        _validate_audio_stereo(job_dir, raw_path, out, log)
        log.info("  ✓  audio_stereo.wav passed integrity check — reusing.")
        return out

    log.info(
        "Step 1b — downmixing to stereo: %s  (%d ch, %s → 2 ch, 44.1 kHz, pcm_s16le)",
        raw_path.name, ch, layout,
    )

    plan = audio_processing.plan_downmix(cfg, layout, ch)
    if plan["notable"]:
        (log.warning if plan["warn"] else log.info)("  %s", plan["note"])
    if plan["filter"]:
        log.info("  Center boost %+.1f dB: explicit downmix matrix, relative to the other channels "
                 "(unit row sum -- same no-clip guarantee as -ac 2):",
                 plan["center_boost_db_applied"])
        log.info("    %s", plan["filter"])
        mix_args = ["-af", plan["filter"]]       # pan=stereo|... also sets the 2-channel output layout
    else:
        mix_args = ["-ac", "2"]

    tmp = tmp_output_path(out)
    log.info("  Running ffmpeg downmix (may take several minutes for large files) ...")
    tmp.unlink(missing_ok=True)   # clear a partial attempt from an interrupted prior run
    run_cmd(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-y", "-i", str(raw_path),
            *mix_args,
            "-ar", "44100",
            "-c:a", "pcm_s16le",
            str(tmp),
        ],
        log,
    )
    finalize_output(tmp, out)
    log.info("  ✓  audio_stereo.wav  (%s)", fmt_size(out))

    _validate_audio_stereo(job_dir, raw_path, out, log)
    log.info("  ✓  audio_stereo.wav passed integrity check.")

    state["downmix"] = {
        "channels":    2,
        "sample_rate": 44100,
        "file":        out.name,
        "method":      plan["method"],
        "filter":      plan["filter"],
        "center_boost_db_requested": plan["center_boost_db_requested"],
        "center_boost_db_applied":   plan["center_boost_db_applied"],
        "source_layout": layout,
        "note":        plan["note"],
    }
    if plan.get("matrix"):
        state["downmix"]["matrix"] = plan["matrix"]
    write_job(job_dir, state)
    mark_step_done(job_dir, "1b_downmix")
    return out


# ── Internal helpers ──────────────────────────────────────────────────────────

def _probe_audio_stream(video_path: Path, log: logging.LoggerAdapter) -> dict:
    """
    Run ffprobe on the first audio stream of video_path.
    Returns the stream dict with codec_name, channels, channel_layout,
    sample_rate, bit_rate, duration, and tags (a nested dict holding
    'title' when the source sets one -- see extract_raw() for why that's
    logged). duration is used to validate the extraction (see
    _validate_audio_raw()) and is persisted to job.json so a later resume
    can re-validate without re-probing video_path.
    """
    result = run_cmd(
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "a:0",
            "-show_entries",
            "stream=codec_name,bit_rate,sample_rate,channels,channel_layout,duration:stream_tags=title",
            "-of", "json",
            str(video_path),
        ],
        log,
    )
    data    = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(
            f"No audio streams found in {video_path.name}.  "
            "Verify the file is a valid video/audio container."
        )
    return streams[0]


def _validate_audio_raw(
    job_dir: Path,
    out_path: Path,
    expected_source_duration: Optional[float],
    log: logging.LoggerAdapter,
) -> None:
    """
    Integrity check for audio_raw.* -- catches a truncated bitstream copy
    left behind by an interrupted previous run (Ctrl-C, OOM-kill, host
    shutdown mid-extraction) before it can propagate into Step 1b's
    downmix, Step 1c's segmentation, and Step 2's (potentially
    hours-long) Demucs pass on data that was never complete to begin
    with.

    This is what would have caught job 1d55099e2bb7's audio_raw.mp3:
    extraction was interrupted 7:51 into a source video whose audio
    stream itself reports roughly 100 minutes, and the truncated result
    was accepted as a finished extraction purely because it existed at
    all under the expected filename.

    The duration check (below) is the only one of these that gates
    success or failure. Its embedded-chapters cross-check used to also
    raise on a mismatch, but that signal isn't as trustworthy as it looks:
    a chapter list is metadata riding along in the same file, not an
    independent remeasurement, so a genuine authoring mistake in the
    *source* (a chapter placed past its actual runtime, unrelated to
    anything this pipeline does) would reproduce byte-for-byte on every
    retry. Failing hard on the duration check is safe -- a retry either
    reproduces the same correct measurement or fixes a real truncation --
    but failing hard here could turn one mis-authored source file into a
    permanently unprocessable one, which is worse than the bug this
    function exists to catch. So a chapters/duration mismatch is now
    logged as a warning rather than treated as failure; see below.

    The one signal that gates success or failure:

      out_path's own probed duration vs. expected_source_duration (the
      source video's audio-stream duration, recorded in job.json at
      extraction time so a resume never needs the original video file to
      still be reachable). probe_duration_sec() measures this by actually
      demuxing the file through to its end rather than trusting embedded
      metadata (see that function's own docstring for why that
      distinction matters -- confirmed by testing, not theoretical), so a
      `-c:a copy` bitstream copy that's genuinely intact reproduces the
      source's duration almost exactly; a real discrepancy here is tens
      of seconds to hours, not the container-level rounding a truly
      intact copy might show. Skipped if expected_source_duration is
      unavailable (rare, but validation should degrade gracefully rather
      than block a run over a duration ffprobe couldn't determine for the
      *source*).

    A second, informational-only signal:

      out_path's own embedded chapter list, if it has one (ffmpeg's
      bitstream copy carries the source container's chapters along with
      it), vs. out_path's own duration. Chapters are normally written
      once from the complete source material, so a chapter ending after
      the file's own measured duration usually does mean the audio data
      was cut short after the chapter metadata was already in place --
      this is exactly the shape job 1d55099e2bb7's file was found in
      (chapters ran to 6008s / Chapter 20; actual duration 471.8s), and
      it's still logged prominently for that reason. It just isn't
      trusted *on its own* to fail the job the way the duration check is
      -- if the duration check above already passed, that's independent,
      ground-truth confirmation the extraction itself is complete, which
      makes a lingering chapters mismatch a source-authoring quirk to
      note rather than something to act on. A file with no chapters at
      all just skips this half of the check -- it isn't required for a
      file to be valid, only informative when present.

    On a duration-check failure: deletes out_path and unmarks
    '1a_extract_raw' from steps_completed, so the next run doesn't get
    stuck re-validating (or worse, refusing to touch) the same bad file
    -- just re-running hush.sh regenerates it cleanly. pipeline.py's
    caller marks Step 1a failed and exits; the next invocation redoes the
    extraction from scratch.
    """
    actual_duration = probe_duration_sec(out_path, log)

    if expected_source_duration:
        try:
            check_duration_matches(
                actual_duration, expected_source_duration, log=log,
                label=f"{out_path.name} vs. source video duration",
                tolerance_sec=5.0,
            )
        except RuntimeError:
            out_path.unlink(missing_ok=True)
            unmark_step_done(job_dir, "1a_extract_raw")
            log.error("  Deleted incomplete %s — re-run to extract it fresh.", out_path.name)
            raise

    max_chapter_end = _max_chapter_end_sec(out_path, log)
    if max_chapter_end is not None and max_chapter_end > actual_duration + 2.0:
        log.warning(
            "  ⚠  %s carries chapter markers extending to %s (%.1fs), but "
            "the file itself is only %s (%.1fs) long. This can mean a "
            "previous run was interrupted mid-extraction -- but if the "
            "duration check above passed, the extraction itself has "
            "already been confirmed complete against the source, so this "
            "more likely just means the source's own chapter metadata "
            "doesn't match its actual runtime (an authoring mistake "
            "upstream, not something re-running this pipeline can fix). "
            "Not treated as a failure; proceeding.",
            out_path.name, fmt_duration(max_chapter_end), max_chapter_end,
            fmt_duration(actual_duration), actual_duration,
        )


def _validate_audio_stereo(job_dir: Path, raw_path: Path, out: Path, log: logging.LoggerAdapter) -> None:
    """
    Integrity check for audio_stereo.wav -- same rationale as
    _validate_audio_raw() above, one step later in the pipeline. The
    downmix decodes and re-encodes (not a stream copy), but still
    preserves sample count exactly, so a truncation here shows up the
    same way: out's own duration falling well short of what audio_raw
    (already validated by extract_raw() before this function's caller
    ever runs) itself measures.

    On failure: deletes out and unmarks '1b_downmix' from steps_completed
    -- see _validate_audio_raw()'s docstring for why both matter together.
    """
    expected = probe_duration_sec(raw_path, log)
    try:
        check_duration_matches(
            probe_duration_sec(out, log), expected, log=log,
            label="audio_stereo.wav vs. audio_raw", tolerance_sec=2.0,
        )
    except RuntimeError:
        out.unlink(missing_ok=True)
        unmark_step_done(job_dir, "1b_downmix")
        log.error("  Deleted incomplete %s — re-run to downmix it fresh.", out.name)
        raise


def _max_chapter_end_sec(path: Path, log: logging.LoggerAdapter) -> Optional[float]:
    """
    Return the latest chapter end time, in seconds, embedded in path, or
    None if it has no chapters at all -- most extracted audio bitstreams
    won't; it's only ever present because ffmpeg's bitstream copy carries
    the source container's own chapter list along with it. See
    _validate_audio_raw() above for how this is used.

    Reads each chapter's 'end_time' field, not 'end': ffprobe reports
    'end' as a raw integer tick count in units of that chapter's own
    'time_base' (which varies by file -- 1/1000, 1/90000, whatever the
    source container used -- and isn't necessarily seconds at all), while
    'end_time' is the same value ffprobe has already converted to decimal
    seconds for you. Using 'end' directly as if it were already seconds
    is a real bug, not a hypothetical one -- caught by testing against a
    file with a 1/1000 time_base, where it read a chapter actually ending
    at 24.977s as ending at 24977 seconds (about 6h56m) instead.
    """
    result = run_cmd(
        ["ffprobe", "-v", "quiet", "-show_chapters", "-of", "json", str(path)],
        log,
    )
    data     = json.loads(result.stdout)
    chapters = data.get("chapters", [])
    ends     = [float(c["end_time"]) for c in chapters if c.get("end_time") is not None]
    return max(ends) if ends else None

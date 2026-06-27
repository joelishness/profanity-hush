"""
profanity-hush — Step 6b: encode censored audio to match the original codec

Re-encodes audio_censored.wav (Step 6's raw PCM output) into a standalone
compressed audio file, matching the ORIGINAL audio track's codec, bitrate,
sample rate, and channel layout as closely as ffmpeg's available encoders
allow — as its own single-input ffmpeg invocation, with no video file
involved at all.

Why this exists as a separate step (split out of steps/mux.py):

Previously, Step 7 (mux) ran ONE ffmpeg command with two inputs — the
original video file and audio_censored.wav — mapping `-c:v copy` from the
first and `-c:a <encoder>` from the second. Diagnosing a real-world bug
("audio mostly silent on playback, present only in scattered spots" on a
subset of long, multi-subtitle-track Blu-ray rips) traced back to exactly
that combination: ffprobe on the affected outputs showed the video stream
(copied) starting at its original `start_time` (e.g. +0.023s — a common
artifact of how the source Blu-ray rip itself was muxed), while the audio
stream (freshly encoded in the same command) started at the *negative* of
that same value (e.g. -0.023s). ffmpeg's cross-input timestamp
reconciliation uses the first input's start_time as the zero-reference for
any stream that goes through its encode/filter pipeline, but leaves
`-c:v copy` streams' original timestamps untouched — so combining a copy
stream from one input with an encoded stream from another, in the same
command, structurally produces two streams that don't share a timeline
origin. (A `ffprobe -show_entries packet=pts_time` dump of the affected
audio track showed every single packet evenly spaced with zero gaps or
jumps end-to-end — so this is a *timestamp-origin* mismatch between the two
streams, not data loss or a mid-file discontinuity.)

Splitting the encode out fixes this at the source: this step's ffmpeg
invocation has exactly one input (audio_censored.wav) and nothing else to
reconcile against, so the freshly encoded audio naturally starts at PTS 0
with no other file's start_time pulling it negative. Step 7 then becomes a
PURE stream-copy mux of two already-finalized files (`-c:v copy -c:a
copy`), which is both simpler and far less likely to trigger this class of
bug than a mixed copy+encode command ever was. `-avoid_negative_ts
make_zero` is set explicitly on both this step's command and Step 7's,
purely as a defensive belt-and-suspenders measure — it should be a no-op
given the above, but costs nothing to pin explicitly rather than rely on
whatever a given ffmpeg version's default happens to be.

Input  : original video file (probed only — never opened as an ffmpeg
         input here), audio_censored.wav (Step 6)
Output : audio_encoded.mka

Why .mka (Matroska Audio) regardless of the chosen encoder: it's a single
generic, codec-agnostic container that every entry in CODEC_MAP below can
land in without per-codec wrapper logic — AAC normally wants an ADTS/M4A
wrapper, Vorbis/Opus normally want Ogg, WMA wants ASF, and raw AC3/DTS/MP3
streams have their own quirks as bare files. Using .mka for all of them
means Step 7 never needs to know or care which encoder Step 6b picked; it
only ever stream-copies whatever single audio track is inside.

Probe phase, codec map, and the ac3-fallback rules below are unchanged
from the pre-split design (still per design doc §8) — only *where* they
run has moved, from inside the mux command to here, one step earlier.

Intermediate cleanup (conditional on keep_intermediates):
  audio_censored.wav is fully consumed once audio_encoded.mka exists — it
  was the last consumer of dialog_censored.wav/score_sfx.wav, and nothing
  downstream needs the raw PCM again — so it's deleted here unless
  keep_intermediates is set. This is the same cleanup audio_censored.wav
  used to get from steps/mux.py before the split; it just happens one step
  earlier now, governed by the same utils.keep_intermediate() policy.
  audio_encoded.mka itself is deleted by steps/mux.py (Step 7), once it's
  no longer needed there — never by this step, which only ever cleans up
  its own *input*, matching every other step's convention in this
  pipeline.

Marks '6b_encode' done. Returns the path to audio_encoded.mka.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from utils import cfg_get, fmt_size, keep_intermediate, mark_step_done, read_job, run_cmd, step_logger, write_job

# ffprobe codec_name -> ffmpeg encoder for a normal (non-fallback) re-encode.
# "dca" is ffmpeg's DTS encoder (lossy core DTS only — see _pick_encoder
# for the DTS-HD MA special case, which deliberately bypasses this entry).
CODEC_MAP: dict[str, str] = {
    "aac":       "aac",
    "ac3":       "ac3",
    "eac3":      "eac3",
    "dts":       "dca",
    "mp3":       "libmp3lame",
    "flac":      "flac",
    "vorbis":    "libvorbis",
    "opus":      "libopus",
    "wmav2":     "wmav2",
    "pcm_s16le": "pcm_s16le",
    "pcm_s24le": "pcm_s16le",
    "pcm_s32le": "pcm_s16le",
}

# Codecs ffmpeg has no encoder for at all -- always fall back to ac3.
NO_REENCODE = {"truehd", "mlp"}

# Per-encoder default bitrate (bps), used only when ffprobe didn't report
# one for the original stream (happens for some lossless/VBR sources).
DEFAULT_BITRATE: dict[str, int] = {
    "aac":        192_000,
    "ac3":        448_000,
    "eac3":       448_000,
    "dca":      1_509_000,
    "libmp3lame": 320_000,
    "libvorbis":  192_000,
    "libopus":    128_000,
    "wmav2":      192_000,
}

AC3_MAX_BITRATE = 640_000  # ffmpeg's ac3 encoder ceiling -- used for fallbacks
DEFAULT_CHANNEL_LAYOUT = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}


def encode(
    job_dir: Path,
    video_path: Path,
    audio_censored_path: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 6b: encode audio_censored.wav (Step 6) to audio_encoded.mka,
    matching the original video's audio codec/bitrate/sample rate/channel
    layout — as a standalone, single-input ffmpeg command.

    Returns the path to audio_encoded.mka.
    """
    if log is None:
        log = step_logger("encode")

    state       = read_job(job_dir)
    done        = state.get("steps_completed", [])
    encoded_out = job_dir / "audio_encoded.mka"

    if "6b_encode" in done:
        log.info("Step 6b — ↩  already complete; re-using %s.", encoded_out.name)
        # audio_encoded.mka is a large intermediate that Step 7 (mux)
        # deletes once the final muxed video exists (unless
        # keep_intermediates) -- so it's only *required* to still be on
        # disk if Step 7 hasn't run yet. Same reasoning as
        # steps/recombine.py's own resume-check applies one step later here.
        if "7_mux" not in done and not encoded_out.exists():
            raise RuntimeError(
                f"Step 6b is marked complete but {encoded_out} is missing, "
                "and Step 7 (mux) hasn't run yet to explain its absence.  "
                "Delete the job directory and re-run from scratch."
            )
        return encoded_out

    if not audio_censored_path.exists():
        raise RuntimeError(
            f"Step 6b: censored audio not found at {audio_censored_path} — "
            "did Step 6 (recombine) complete?"
        )
    if not video_path.exists():
        raise RuntimeError(f"Step 6b: original video not found at {video_path}.")

    log.info("Step 6b — encode censored audio to match original codec")

    stream = _probe_audio_stream(video_path, log)
    encoder, bitrate, fallback_reason = _pick_encoder(stream, log)

    ch         = stream.get("channels") or 2
    raw_layout = stream.get("channel_layout")
    layout     = raw_layout if raw_layout and raw_layout.lower() != "unknown" \
        else DEFAULT_CHANNEL_LAYOUT.get(ch, "stereo")
    rate       = stream.get("sample_rate") or "44100"
    delay      = (stream.get("tags") or {}).get("DELAY")

    log.info(
        "  original audio: codec=%s  channels=%s (%s)  sample_rate=%s Hz  bitrate=%s",
        stream.get("codec_name", "unknown"), ch, layout, rate, stream.get("bit_rate", "?"),
    )
    if fallback_reason:
        log.warning(
            "  WARNING: original codec '%s' cannot be re-encoded (%s). "
            "Falling back to ac3 at %d bps. Verify sync and quality before use.",
            stream.get("codec_name", "unknown"), fallback_reason, bitrate,
        )
    else:
        log.info("  → re-encoding censored audio to %s%s", encoder,
                  f" at {bitrate} bps" if bitrate else " (lossless, no bitrate target)")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(audio_censored_path),
        "-c:a", encoder,
    ]
    if bitrate:  # 0 for lossless encoders (flac, pcm_*) -- no -b:a target
        cmd += ["-b:a", str(bitrate)]
    cmd += ["-ar", str(rate), "-channel_layout", str(layout)]
    if delay:
        cmd += ["-metadata:s:a:0", f"DELAY={delay}"]
    # Defensive only -- a single-input encode has no other file's
    # start_time to get pulled negative against in the first place (see
    # module docstring) -- but pinning this explicitly costs nothing and
    # documents the intent rather than relying on ffmpeg's default.
    cmd += ["-avoid_negative_ts", "make_zero"]
    cmd += ["-f", "matroska", str(encoded_out)]

    run_cmd(cmd, log)
    log.info("  ✓  %s  (%s)", encoded_out.name, fmt_size(encoded_out))

    # audio_censored.wav: fully consumed now that audio_encoded.mka exists
    # -- nothing downstream needs the raw PCM again.
    if not keep_intermediate(cfg, correction_artifact=False):
        _unlink_if(audio_censored_path, log)

    state = read_job(job_dir)
    state["encode"] = {
        "output":           encoded_out.name,
        "encoder":          encoder,
        "bitrate":          bitrate,
        "fallback_reason":  fallback_reason,
    }
    write_job(job_dir, state)
    mark_step_done(job_dir, "6b_encode")

    log.info("  ✓  Step 6b complete.")
    return encoded_out


# ── Helpers ───────────────────────────────────────────────────────────────────

def _probe_audio_stream(video_path: Path, log: logging.LoggerAdapter) -> dict:
    """
    Probe the original video's primary audio stream for the fields Step 6b
    needs: codec_name, profile (only to distinguish DTS-HD MA from plain
    DTS -- see _pick_encoder), bit_rate, sample_rate, channels,
    channel_layout, and the DELAY tag (A/V sync offset, present only on
    some mkvmerge-authored files).

    A private, step-local duplicate of steps/extract.py's own probe
    rather than a shared import -- matches this codebase's existing
    convention (e.g. each step's own private _unlink_if) of keeping every
    step module self-contained rather than coupling steps through
    "private" (leading-underscore) helpers in another step's module. This
    was steps/mux.py's _probe_audio_stream verbatim before the Step 6b
    split; only its home moved.
    """
    result = run_cmd(
        [
            "ffprobe", "-v", "quiet",
            "-select_streams", "a:0",
            "-show_entries",
            "stream=codec_name,profile,bit_rate,sample_rate,channels,channel_layout",
            "-show_entries", "stream_tags=DELAY",
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


def _pick_encoder(
    stream: dict, log: logging.LoggerAdapter,
) -> tuple[str, int, Optional[str]]:
    """
    Decide which ffmpeg audio encoder to use, and at what bitrate, given
    the original stream's probed metadata.

    Returns (encoder, bitrate_bps, fallback_reason). bitrate_bps is 0 for
    lossless encoders (flac, pcm_*) where a bitrate target doesn't apply.
    fallback_reason is None for a normal codec-preserving re-encode, or a
    short human-readable string when falling back to ac3 because the
    original codec can't be matched -- the caller logs a warning using it.
    """
    codec        = (stream.get("codec_name") or "unknown").lower()
    profile      = stream.get("profile") or ""
    orig_bitrate = _safe_int(stream.get("bit_rate"))

    is_dts_hd_ma = codec == "dts" and "ma" in profile.lower()  # e.g. "DTS-HD MA"

    if codec in NO_REENCODE or is_dts_hd_ma:
        reason = f"{codec}{f' ({profile})' if profile else ''} has no lossless ffmpeg encoder"
        bitrate = min(orig_bitrate, AC3_MAX_BITRATE) if orig_bitrate else AC3_MAX_BITRATE
        return "ac3", bitrate, reason

    encoder = CODEC_MAP.get(codec)
    if encoder is None:
        log.warning("  Unrecognised source audio codec '%s'.", codec)
        reason = f"codec '{codec}' is not in the known codec map"
        bitrate = min(orig_bitrate, AC3_MAX_BITRATE) if orig_bitrate else AC3_MAX_BITRATE
        return "ac3", bitrate, reason

    if encoder == "flac" or encoder.startswith("pcm_"):
        return encoder, 0, None  # lossless -- no bitrate knob

    bitrate = orig_bitrate or DEFAULT_BITRATE.get(encoder, 192_000)
    return encoder, bitrate, None


def _safe_int(value) -> Optional[int]:
    """ffprobe sometimes reports bit_rate as the string 'N/A', or omits it
    entirely for some lossless/VBR codecs; coerce cleanly to None rather
    than letting a ValueError propagate from a bare int() call."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unlink_if(path: Path, log: logging.LoggerAdapter) -> None:
    """Delete a file if it exists; no-op and no error if absent."""
    if path.exists():
        path.unlink()
        log.debug("  Removed intermediate: %s", path.name)

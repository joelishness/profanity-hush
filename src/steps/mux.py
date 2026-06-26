"""
profanity-hush — Step 7: mux censored audio into the original video

Combines the original video's bitstream-exact video stream with the fully
recombined censored audio (audio_censored.wav, Step 6) into the final
output file, written to /output. This is the last step of v1's core
pipeline (§4) — once this succeeds, the job is done.

Input  : original video file, audio_censored.wav (Step 6)
Output : /output/{filename per output.naming_style} -- see _output_path
         for the two supported styles (plex_edition, the default, and the
         original v1 suffix style)

Video stream: copied bitstream-exact (-c:v copy), never re-encoded.

Audio stream: re-encoded to match the ORIGINAL audio track's codec,
bitrate, sample rate, and channel layout as closely as ffmpeg's available
encoders allow. Matching — not just "some compressed format" — minimizes
audible quality step-down and keeps the output's size/compatibility
expectations close to what the original suggested.

Probe phase (runs before encoding) — ffprobe on stream a:0 of the
ORIGINAL VIDEO, not of audio_censored.wav (which is just raw 44.1kHz PCM
and carries none of this metadata):
  ffprobe -v quiet -select_streams a:0 \
    -show_entries stream=codec_name,profile,bit_rate,sample_rate,channels,channel_layout \
    -show_entries stream_tags=DELAY \
    -of json original_video

  ('profile' is fetched in addition to the design doc's §8 literal probe
  command — it's the only way to distinguish plain DTS from DTS-HD MA,
  since ffprobe reports codec_name "dts" for both; see _pick_encoder.)

Codec map (original codec_name -> ffmpeg encoder), per design doc §8,
extended slightly:
  aac, ac3, eac3, dts, mp3, flac   — as specified in §8
  vorbis, opus, wmav2, pcm_*       — not in §8's literal table, but
                                      steps/extract.py's CODEC_EXT already
                                      anticipates these as possible source
                                      codecs, so the encoder map handles
                                      them too rather than silently
                                      falling through to the "unrecognised
                                      codec" branch below
  truehd, mlp                      — *fallback*: ac3 at the highest usable
                                      bitrate (ffmpeg has no encoder for
                                      either)
  dts, profile contains "MA"       — *fallback*: ac3. ffmpeg's "dca"
                                      encoder can only produce the lossy
                                      DTS core layer, never the lossless
                                      MA extension, so re-encoding under
                                      the original "dts" name would
                                      silently misrepresent what's
                                      actually in the file; falling back
                                      to ac3 is the honest version of the
                                      same quality loss
  anything else (unrecognised)     — *fallback*: ac3, with a warning —
                                      defensive: an unrecognised codec
                                      shouldn't crash an otherwise-
                                      successful run

When a fallback is used, a warning is logged clearly, per §8:
  "Original codec '{codec}' cannot be re-encoded. Falling back to ac3 at
  {bitrate}. Verify sync and quality before use."

Subtitle/chapter/attachment preservation — an extension beyond the design
doc's bare `-map 0:v:0 -map 1:a:0` example, which (taken literally) would
silently drop every embedded subtitle track, chapter marker, and
attachment (e.g. embedded fonts for ASS subtitles) from the source
container — not just "other audio tracks", which is the only thing §8's
own note calls out as dropped. For `format: mkv` (the default, and the
option config.yaml already recommends specifically for "flexible codec
support, no re-mux needed" — see config/config.yaml), all three are
cheap, lossless stream copies, so they're preserved:
  -map 0:s? -c:s copy       (subtitles, if any)
  -map 0:t? -c:t copy       (attachments, if any — e.g. ASS fonts)
  -map_chapters 0           (chapters, if any)
For `format: mp4`, subtitle/attachment stream-copy compatibility is much
less reliable — PGS/VOBSUB bitmap subtitles in particular generally aren't
valid in MP4 at all, and attempting the copy would make ffmpeg fail
outright rather than just producing a censored file without subtitles —
so mp4 output carries chapters forward (MP4 supports them natively via a
different mechanism, handled the same way by ffmpeg's -map_chapters) but
does not attempt subtitle/attachment passthrough. This isn't in the
design doc's literal mux.py spec but follows directly from its own
config.yaml rationale for recommending mkv.

Crash safety: the muxed file is written to a `.tmp` sibling inside
/output and only renamed to its final name once ffmpeg exits 0 — matching
utils.write_job's write-then-rename pattern, applied here because
/output, unlike /jobs, is the one place in this pipeline a half-written
file would be directly user-visible and easy to mistake for a finished
one.

Intermediate cleanup (conditional on keep_intermediates):
  audio_censored.wav is fully consumed once the final muxed video exists
  — it was the last consumer of dialog_censored.wav/score_sfx.wav, and
  nothing in v1 needs it again — so it's deleted here unless
  keep_intermediates is set, matching every earlier step's cleanup
  pattern for its own now-superseded intermediates. audio_raw.{ext} is
  NOT touched here: it's always kept (§6), independent of this step, for
  future per-channel reprocessing (§13.3).

Marks '7_mux' done. Returns the path to the final output file in /output.
"""

import json
import logging
import re
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

# ffmpeg muxer name for each supported output.format. Passed explicitly via
# -f rather than relying on the output filename's extension, because the
# crash-safety temp file (see module docstring) is renamed into place only
# after a successful encode -- and ffmpeg's extension-based format
# auto-detection has no way to know ".mkv.tmp" means "matroska" rather
# than failing to recognize the container at all (which it does -- see
# _output_path / the tmp-naming scheme below for the other half of this).
MUXER_FORMAT = {"mkv": "matroska", "mp4": "mp4"}


def mux(
    job_dir: Path,
    video_path: Path,
    audio_censored_path: Path,
    output_dir: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 7: mux audio_censored.wav into the original video's container.

    Returns the path to the final output file in /output.
    """
    if log is None:
        log = step_logger("mux")

    state      = read_job(job_dir)
    out_format = str(cfg_get(cfg, "output", "format", default="mkv")).lower()
    if out_format not in ("mkv", "mp4"):
        raise RuntimeError(
            f"Step 7: unknown output.format '{out_format}' (expected 'mkv' or 'mp4')."
        )
    out_path = _output_path(video_path, output_dir, cfg, out_format)

    if "7_mux" in state.get("steps_completed", []):
        log.info("Step 7 — ↩  already complete; re-using %s.", out_path.name)
        if not out_path.exists():
            raise RuntimeError(
                f"Step 7 is marked complete but {out_path} is missing.  "
                "Delete the job directory and re-run from scratch."
            )
        return out_path

    if not audio_censored_path.exists():
        raise RuntimeError(
            f"Step 7: censored audio not found at {audio_censored_path} — "
            "did Step 6 (recombine) complete?"
        )
    if not video_path.exists():
        raise RuntimeError(f"Step 7: original video not found at {video_path}.")

    log.info("Step 7 — mux censored audio into video  (format=%s)", out_format)

    stream = _probe_audio_stream(video_path, log)
    encoder, bitrate, fallback_reason = _pick_encoder(stream, log)

    ch     = stream.get("channels") or 2
    raw_layout = stream.get("channel_layout")
    layout = raw_layout if raw_layout and raw_layout.lower() != "unknown" \
        else DEFAULT_CHANNEL_LAYOUT.get(ch, "stereo")
    rate   = stream.get("sample_rate") or "44100"
    delay  = (stream.get("tags") or {}).get("DELAY")

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
        "-i", str(video_path),
        "-i", str(audio_censored_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
    ]

    if out_format == "mkv":
        # MKV tolerates arbitrary subtitle codecs and attachments as
        # lossless stream copies -- see module docstring for why mp4
        # doesn't get the same treatment. '?' on a -map means "include
        # if present, don't error if not" -- no separate existence probe
        # needed for either of these.
        cmd += ["-map", "0:s?", "-map", "0:t?", "-map_chapters", "0"]
        cmd += ["-c:v", "copy", "-c:s", "copy", "-c:t", "copy"]
    else:
        cmd += ["-map_chapters", "0"]
        cmd += ["-c:v", "copy"]

    cmd += ["-c:a", encoder]
    if bitrate:  # 0 for lossless encoders (flac, pcm_*) -- no -b:a target
        cmd += ["-b:a", str(bitrate)]
    cmd += ["-ar", str(rate), "-channel_layout", str(layout)]
    if delay:
        cmd += ["-metadata:s:a:0", f"DELAY={delay}"]

    output_dir.mkdir(parents=True, exist_ok=True)
    # ".tmp" goes *before* the real extension (video_censored.tmp.mkv, not
    # video_censored.mkv.tmp) -- ffmpeg's muxer auto-detection is
    # extension-based, and a trailing ".tmp" defeats it ("Unable to choose
    # an output format"). The explicit -f below makes this belt-and-
    # suspenders rather than load-bearing, but keeping a real extension on
    # the temp file is also just more useful if a crash ever leaves one
    # behind for a human to find.
    tmp_path = out_path.with_name(f"{out_path.stem}.tmp{out_path.suffix}")
    cmd += ["-f", MUXER_FORMAT[out_format], str(tmp_path)]

    run_cmd(cmd, log)
    tmp_path.replace(out_path)
    log.info("  ✓  %s  (%s)", out_path.name, fmt_size(out_path))

    if not keep_intermediate(cfg, correction_artifact=False):
        _unlink_if(audio_censored_path, log)

    state = read_job(job_dir)
    state["mux"] = {
        "output":           str(out_path),
        "format":           out_format,
        "encoder":          encoder,
        "bitrate":          bitrate,
        "fallback_reason":  fallback_reason,
    }
    write_job(job_dir, state)
    mark_step_done(job_dir, "7_mux")

    log.info("  ✓  Step 7 complete.  Final output: %s", out_path)
    return out_path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _output_path(video_path: Path, output_dir: Path, cfg: dict, out_format: str) -> Path:
    """
    Build the final output filename, per output.naming_style:

    plex_edition (default) -- a Plex-friendly {edition-Name} tag (see
      https://support.plex.tv/articles/multiple-editions/), inserted right
      after the "(YYYY)" release-year portion of the filename if one is
      present, so Plex shows the censored file as a selectable Edition of
      the same movie instead of an unrelated second item:
        "Movie (1986).sd.hevc.mkv" -> "Movie (1986) {edition-Hushed}.sd.hevc.mkv"
      Plex's own docs note tag order doesn't matter to its parser, but
      placing it right after the year (rather than at the very end) is
      the clearer convention when other dot-separated tags follow. Falls
      back to appending the tag at the very end -- still valid Plex
      syntax -- if no "(YYYY)" pattern is found at all.

    suffix -- the original v1 behaviour: a plain suffix appended before
      the extension, no Plex Edition semantics.
        "movie.mkv" -> "movie_censored.mkv"

    Path(name).stem strips only the final extension, so a filename like
    "movie.sd.hevc.mkv" keeps everything after the first dot intact in
    either style above.
    """
    naming_style = str(cfg_get(cfg, "output", "naming_style", default="plex_edition")).lower()
    stem = Path(video_path.name).stem

    if naming_style == "plex_edition":
        edition_name = str(cfg_get(cfg, "output", "edition_name", default="Hushed"))
        tag = f"{{edition-{edition_name}}}"
        year_match = re.search(r"\(\d{4}\)", stem)
        if year_match:
            new_stem = f"{stem[:year_match.end()]} {tag}{stem[year_match.end():]}"
        else:
            new_stem = f"{stem} {tag}"
    elif naming_style == "suffix":
        suffix = str(cfg_get(cfg, "output", "suffix", default="_censored"))
        new_stem = f"{stem}{suffix}"
    else:
        raise RuntimeError(
            f"Step 7: unknown output.naming_style '{naming_style}' "
            "(expected 'plex_edition' or 'suffix')."
        )

    return output_dir / f"{new_stem}.{out_format}"


def _probe_audio_stream(video_path: Path, log: logging.LoggerAdapter) -> dict:
    """
    Probe the original video's primary audio stream for the fields Step 7
    needs: codec_name, profile (only to distinguish DTS-HD MA from plain
    DTS -- see _pick_encoder), bit_rate, sample_rate, channels,
    channel_layout, and the DELAY tag (A/V sync offset, present only on
    some mkvmerge-authored files).

    A private, step-local duplicate of steps/extract.py's own probe
    rather than a shared import -- matches this codebase's existing
    convention (e.g. each step's own private _unlink_if) of keeping every
    step module self-contained rather than coupling steps through
    "private" (leading-underscore) helpers in another step's module.
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

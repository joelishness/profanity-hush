"""
profanity-hush — Step 7: mux censored audio into the original video

Combines the original video's bitstream-exact video stream with the
already-encoded censored audio (audio_encoded.mka, Step 6b) into the final
output file, written to /output. This is the last step of v1's core
pipeline (§4) — once this succeeds, the job is done.

Input  : original video file, audio_encoded.mka (Step 6b)
Output : /output/{filename per output.naming_style} -- see _output_path
         for the two supported styles (plex_edition, the default, and the
         original v1 suffix style)

Both streams: copied bitstream-exact (-c:v copy -c:a copy). Neither is
re-encoded here — that decision (which encoder, what bitrate, matching the
original codec) now happens one step earlier, in steps/encode.py (Step
6b). See that module's docstring for why the split happened: combining a
`-c:v copy` stream from one input with a `-c:a <encoder>` stream encoded
live from a *second* input, in a single ffmpeg command, was found to
produce two streams with different timestamp origins (ffprobe showed the
copied video starting at the source's original start_time, e.g. +0.023s,
while the same-command-encoded audio started at -0.023s — the negative of
that same value) on a subset of long, multi-subtitle-track Blu-ray rips,
manifesting as "audio mostly silent, present only in scattered spots" on
playback even though the audio data itself, extracted standalone, was
complete and correct throughout. Now that both inputs (the original video,
and Step 6b's already-finalized audio_encoded.mka) are combined with pure
stream copies on both sides, there's no encode/filter pipeline for either
stream to be timestamp-rebased through — both keep their own native
timestamps verbatim, exactly the property a stream copy is supposed to
have. `-avoid_negative_ts make_zero` is set explicitly here too, purely as
a defensive measure — costs nothing, and documents the intent rather than
relying on whatever a given ffmpeg version's default happens to be.

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
  audio_encoded.mka is fully consumed once the final muxed video exists —
  nothing in v1 needs it again — so it's deleted here unless
  keep_intermediates is set, matching every earlier step's cleanup
  pattern for its own now-superseded intermediates (this is the same
  cleanup audio_censored.wav used to get here, before the Step 6b split —
  see steps/encode.py). audio_raw.{ext} is NOT touched here: it's always
  kept (§6), independent of this step, for future per-channel
  reprocessing (§13.3).

Marks '7_mux' done. Returns the path to the final output file in /output.
"""

import re
from pathlib import Path
from typing import Optional
import logging

from utils import cfg_get, fmt_size, keep_intermediate, mark_step_done, read_job, run_cmd, step_logger, write_job

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
    audio_encoded_path: Path,
    output_dir: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 7: mux audio_encoded.mka (Step 6b) into the original video's
    container -- a pure stream copy on both sides.

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

    if not audio_encoded_path.exists():
        raise RuntimeError(
            f"Step 7: encoded audio not found at {audio_encoded_path} — "
            "did Step 6b (encode) complete?"
        )
    if not video_path.exists():
        raise RuntimeError(f"Step 7: original video not found at {video_path}.")

    log.info("Step 7 — mux encoded audio into video  (format=%s)", out_format)

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_path),
        "-i", str(audio_encoded_path),
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

    cmd += ["-c:a", "copy"]
    # Defensive only -- see module docstring for why a pure copy+copy mux
    # shouldn't need this, but it's cheap insurance against either
    # stream's native timestamps starting below zero ending up dropped or
    # mishandled by some downstream player/muxer combination.
    cmd += ["-avoid_negative_ts", "make_zero"]

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
        _unlink_if(audio_encoded_path, log)

    state = read_job(job_dir)
    state["mux"] = {
        "output": str(out_path),
        "format": out_format,
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


def _unlink_if(path: Path, log: logging.LoggerAdapter) -> None:
    """Delete a file if it exists; no-op and no error if absent."""
    if path.exists():
        path.unlink()
        log.debug("  Removed intermediate: %s", path.name)

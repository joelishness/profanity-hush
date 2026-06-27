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

**The actual muxing tool depends on output.format** — this is the one step
in the pipeline that doesn't use ffmpeg for its primary job:

  format: mkv (the default, and the one config.yaml recommends) → mkvmerge
  format: mp4                                                   → ffmpeg

Why mkvmerge for mkv, when every other step in this pipeline is ffmpeg:
splitting the audio re-encode out of this step (steps/encode.py, Step 6b)
fixed one real bug (a cross-input timestamp-origin mismatch — see that
module's docstring) but, against a real production file, did *not* fix
the actual symptom: a subset of long, multi-subtitle-track Blu-ray rips
still played back "audio mostly silent, present only in scattered spots"
in VLC and multiple mpv-based players even with both streams now pure
copies on both sides — and Plex didn't merely play it wrong, it refused
to load the file at all (stuck on its loading spinner indefinitely). Four
independent player codebases struggling on the same file pointed at a
genuine structural defect, not a timestamp nuance and not a per-player
quirk. The isolating test: re-muxing the exact same (already-confirmed-
correct) audio_encoded.mka against the original video with subtitles,
chapters, and attachments stripped out (`-map 0:v:0 -map 1:a:0` only, no
`-map 0:s?`/`-map 0:t?`/`-map_chapters`) played back correctly everywhere
tested. The original video's PGS subtitle tracks are exactly the ones
ffmpeg's own demuxer already warns it can't fully analyze on input
(`Could not find codec parameters ... unspecified size`) — and copying
tracks ffmpeg itself admits it couldn't fully parse, via ffmpeg's own
matroska muxer, is exactly the kind of operation likely to produce a
malformed result. mkvmerge — a different, Matroska-specific muxer
implementation, not built on the same generic libavformat probing ffmpeg
uses — was tested against the *identical* source file with subtitles and
chapters fully included (the exact tracks ffmpeg's demuxer warned about),
produced no analogous warning at all, and played back correctly in every
player tested, including Plex. That's a clean enough A/B (same input
bytes, same audio track, only the muxer implementation differs) to make
the muxer itself, not the subtitle data, the confirmed fault — so Step 7
uses mkvmerge for the format (mkv) that's actually affected, rather than
ffmpeg with the subtitle/chapter/attachment copying removed (which would
"fix" this by silently dropping content from every future output, not by
fixing the actual defect).

mp4 output stays on ffmpeg because mkvmerge can only produce Matroska or
WebM — there's no mkvmerge equivalent to ask for here. This isn't a gap in
practice: mp4 output already never attempts subtitle/attachment copying
in the first place (see below), which is the exact category of content
implicated above, so the mkvmerge fix's motivating case doesn't apply to
the mp4 path to begin with.

**mkvmerge command** (mkv path):
```
mkvmerge -o output.mkv.tmp.mkv --no-audio video.mkv audio_encoded.mka
```
mkvmerge's default behaviour, absent any flag saying otherwise, is to
copy *everything* from each input file — video, every subtitle track,
chapters, attachments, tags. `--no-audio` applies to the file named
immediately after it (`video.mkv`) and only suppresses that file's own
(now-uncensored, soon to be replaced) audio track[s] — it does not affect
`audio_encoded.mka`, named with no flag of its own, which contributes its
one audio track unmodified. This single line is therefore already
"video + every subtitle + chapters + attachments from the original, audio
from Step 6b" with no further flags needed — confirmed by hand against
this exact file, with subtitles and chapters both included, before this
module was switched over to it.

mkvmerge's exit codes are not the universal 0=success/nonzero=failure
convention every other tool in this pipeline follows: 0 is a clean run,
1 means it completed successfully but logged at least one warning (e.g.
a track it couldn't fully identify some metadata for, muxed correctly
regardless), and only 2 is an actual failure. `run_cmd(..., ok_exit_codes=
frozenset({0, 1}))` is what keeps a warning-only run from being treated
as a Step 7 failure — see utils.run_cmd's docstring.

`-avoid_negative_ts make_zero`, used on the ffmpeg/mp4 path below for the
same defensive reasons as steps/encode.py, has no mkvmerge equivalent
(and no evidence from testing that it's needed there) — mkvmerge computes
its own track timing from the source files' own block timestamps and was
confirmed correct as-is.

**Subtitle/chapter/attachment preservation for mp4 output:** unlike the
mkvmerge/mkv path above (which preserves all of this by default with no
extra flags), mp4 output does not attempt subtitle or attachment
passthrough at all — PGS/VOBSUB bitmap subtitles in particular generally
aren't valid in MP4, and attempting the copy would make ffmpeg fail
outright rather than just producing a censored file without subtitles.
Chapters are carried forward (`-map_chapters 0`; MP4 supports them
natively via a different mechanism than Matroska, but ffmpeg already
does this by default for a single input — kept explicit here rather than
relying on that default).

Crash safety: the muxed file is written to a `.tmp` sibling inside
/output and only renamed to its final name once the muxing tool exits
with one of ok_exit_codes — matching utils.write_job's write-then-rename
pattern, applied here because /output, unlike /jobs, is the one place in
this pipeline a half-written file would be directly user-visible and easy
to mistake for a finished one.

Intermediate cleanup (conditional on keep_intermediates):
  audio_encoded.mka is fully consumed once the final muxed video exists —
  nothing in v1 needs it again — so it's deleted here unless
  keep_intermediates is set, matching every earlier step's cleanup
  pattern for its own now-superseded intermediates. audio_raw.{ext} is
  NOT touched here: it's always kept (§6), independent of this step, for
  future per-channel reprocessing (§13.3).

Marks '7_mux' done. Returns the path to the final output file in /output.
"""

import re
from pathlib import Path
from typing import Optional
import logging

from utils import cfg_get, fmt_size, keep_intermediate, mark_step_done, read_job, run_cmd, step_logger, write_job


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
    container -- mkvmerge for mkv output, ffmpeg for mp4 (see module
    docstring for why these differ).

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

    tool = "mkvmerge" if out_format == "mkv" else "ffmpeg"
    log.info("Step 7 — mux encoded audio into video  (format=%s, tool=%s)", out_format, tool)

    output_dir.mkdir(parents=True, exist_ok=True)
    # ".tmp" goes *before* the real extension (video_censored.tmp.mkv, not
    # video_censored.mkv.tmp) -- both muxing tools below auto-detect their
    # output format from the extension, and a trailing ".tmp" defeats
    # that ("Unable to choose an output format" from ffmpeg; mkvmerge is
    # more forgiving here, but there's no reason to rely on the
    # difference). Keeping a real extension on the temp file is also just
    # more useful if a crash ever leaves one behind for a human to find.
    tmp_path = out_path.with_name(f"{out_path.stem}.tmp{out_path.suffix}")

    if out_format == "mkv":
        cmd = [
            "mkvmerge", "-o", str(tmp_path),
            "--no-audio", str(video_path),
            str(audio_encoded_path),
        ]
        # 0 = clean, 1 = succeeded with warnings, 2 = real failure --
        # see utils.run_cmd's ok_exit_codes docstring.
        run_cmd(cmd, log, ok_exit_codes=frozenset({0, 1}))
    else:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_path),
            "-i", str(audio_encoded_path),
            "-map", "0:v:0", "-map", "1:a:0", "-map_chapters", "0",
            "-c:v", "copy", "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
            "-f", "mp4",
            str(tmp_path),
        ]
        run_cmd(cmd, log)

    tmp_path.replace(out_path)
    log.info("  ✓  %s  (%s)", out_path.name, fmt_size(out_path))

    if not keep_intermediate(cfg, correction_artifact=False):
        _unlink_if(audio_encoded_path, log)

    state = read_job(job_dir)
    state["mux"] = {
        "output": str(out_path),
        "format": out_format,
        "tool":   tool,
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

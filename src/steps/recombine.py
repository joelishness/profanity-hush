"""
profanity-hush — Step 6: recombine dialog + score/SFX stems

Mixes the censored dialog stem back together with the (untouched)
score/SFX stem from Step 3b's merge, restoring a single full audio track —
now with the flagged words silenced and the music/sound effects playing
through uninterrupted underneath. This is the whole reason muting happens
on the isolated dialog stem instead of the full mix (see design doc §4).

Input  : dialog_censored.wav (Step 5), score_sfx.wav (Step 3b)
Output : audio_censored.wav

Tool (ffmpeg's amix filter):
  ffmpeg -i dialog_censored.wav -i score_sfx.wav \
      -filter_complex amix=inputs=2:duration=first:normalize=0 \
      -c:a pcm_s16le \
      audio_censored.wav

  duration=first  — output length follows dialog_censored.wav. The two
                    inputs come from the same source audio via Demucs and
                    should already be the same length; this just pins the
                    behavior explicitly rather than leaving it to amix's
                    "longest" default in case they ever differ by a
                    sample or two.
  normalize=0     — amix's default behavior scales every input down by
                    1/N to leave headroom for summing; with two
                    already-mixed, full-range stems that would quietly
                    halve both the dialog and the score/SFX levels
                    relative to the original mix. normalize=0 preserves
                    the source levels as recombined.
  -c:a pcm_s16le  — explicit, even though the design doc's reference
                    command (§8) omits it: amix negotiates its own
                    internal sample format (often float), and left
                    unspecified, the .wav muxer would just take whatever
                    that happens to be. Every other WAV in this pipeline
                    is 44.1kHz/16-bit PCM (see steps/merge.py's
                    concatenation, which depends on that being uniform);
                    pinning the format here keeps that invariant instead
                    of silently widening audio_censored.wav for Step 7 to
                    deal with later.

Intermediate cleanup (conditional on keep_intermediates):
  dialog_censored.wav and score_sfx.wav are both fully consumed once
  audio_censored.wav exists — nothing downstream (Step 7) needs either of
  them again — so both are deleted here unless keep_intermediates is set,
  matching steps/merge.py's and steps/mute.py's cleanup pattern for their
  own now-superseded intermediates. pipeline.py's "Steps 1a-3b already
  complete" resume shortcut accounts for this: it no longer treats
  score_sfx.wav's absence as an error once Step 6 has run.

Marks '6_recombine' done.
Returns the path to audio_censored.wav.
"""

import logging
from pathlib import Path
from typing import Optional

from utils import cfg_get, fmt_size, mark_step_done, read_job, run_cmd, step_logger, write_job


def recombine(
    job_dir: Path,
    dialog_censored_path: Path,
    score_sfx_path: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 6: mix dialog_censored.wav + score_sfx.wav into audio_censored.wav.

    Returns the path to audio_censored.wav.
    """
    if log is None:
        log = step_logger("recombine")

    state               = read_job(job_dir)
    audio_censored_out  = job_dir / "audio_censored.wav"

    if "6_recombine" in state.get("steps_completed", []):
        log.info("Step 6 — ↩  already complete; re-using %s.", audio_censored_out.name)
        if not audio_censored_out.exists():
            raise RuntimeError(
                f"Step 6 is marked complete but {audio_censored_out} is missing.  "
                "Delete the job directory and re-run from scratch."
            )
        return audio_censored_out

    if not dialog_censored_path.exists():
        raise RuntimeError(
            f"Step 6: censored dialog stem not found at {dialog_censored_path} — "
            "did Step 5 (mute) complete?"
        )
    if not score_sfx_path.exists():
        raise RuntimeError(
            f"Step 6: score/SFX stem not found at {score_sfx_path} — did Step 3b "
            "(merge) complete?"
        )

    keep = bool(cfg_get(cfg, "output", "keep_intermediates", default=False))

    log.info("Step 6 — recombine dialog + score/SFX stems")

    run_cmd(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-y",
            "-i", str(dialog_censored_path),
            "-i", str(score_sfx_path),
            "-filter_complex", "amix=inputs=2:duration=first:normalize=0",
            "-c:a", "pcm_s16le",
            str(audio_censored_out),
        ],
        log,
    )
    log.info("  ✓  audio_censored.wav  (%s)", fmt_size(audio_censored_out))

    # Both inputs are fully consumed at this point -- Step 7 (mux) only
    # ever needs audio_censored.wav, not either stem again. Same cleanup
    # pattern as steps/merge.py and steps/mute.py apply to their own
    # now-superseded intermediates.
    if not keep:
        _unlink_if(dialog_censored_path, log)
        _unlink_if(score_sfx_path, log)

    state = read_job(job_dir)
    state["recombine"] = {"output": audio_censored_out.name}
    write_job(job_dir, state)
    mark_step_done(job_dir, "6_recombine")

    log.info("  ✓  Step 6 complete.")
    return audio_censored_out


# ── Helpers ───────────────────────────────────────────────────────────────────

def _unlink_if(path: Path, log: logging.LoggerAdapter) -> None:
    """Delete a file if it exists; no-op and no error if absent."""
    if path.exists():
        path.unlink()
        log.debug("  Removed intermediate: %s", path.name)

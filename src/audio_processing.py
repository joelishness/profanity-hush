"""
profanity-hush — audio processing: center boost, mix & master, settings log, audio-only redo

Everything behind config.yaml's `audio_processing:` block lives here, because its
jobs share one question -- "what was this job's audio actually built with, and
what would it take to build it differently?" -- that no single numbered step
should have to answer on its own:

  plan_downmix()        Step 1b   center_boost_db          (called by steps/extract.py)
  mix_and_master()      Step 6    night_mode, loudness.*   (called by steps/recombine.py)
  summary(), drift_messages(), completion_lines()
                        startup banner / "config changed since this job was built"
                        warnings / end-of-run recap          (called by pipeline.py)
  prepare_audio_redo()  --redo-audio: redo Steps 1b-2b and 5-7 from the original
                        audio, leaving transcript/matches/review/SRTs alone
                                                             (called by pipeline.py)

WHY THE OUTPUT IS QUIET (measured, ffmpeg 6.1.1, center-only test tone)
Step 1b's `-ac 2 ... pcm_s16le` makes swresample scale the whole downmix matrix so
that a worst-case sum of every contributing channel can't clip. Cost: a 5.1 source's
center/dialog drops 7.6 dB, a 7.1 source's 9.9 dB (the same downmix written as float:
0 dB). Stereo sources have no matrix to normalize, so lose nothing. A uniform gain
undoes it -- that is what loudness.target_lufs is for.

WHAT EACH SETTING COSTS TO CHANGE (and why each lives where it does)
  center_boost_db     Step 1b, before Demucs -> baked into dialog.wav/score_sfx.wav.
                      Changing it = --redo-audio (Demucs: hours; no recognition).
  night_mode,         Step 6, on the recombined mix.
  loudness.*          Changing them = --redo-step 6_recombine (minutes).

WHY NOT BOOST THE DIALOG STEM
Demucs' dialog stem carries a lot of bleed (applause, engines, whooshes, brass). The
two stems sum back to the mix however the separator splits things -- but only at unity
gain. Give them different gains and every misassigned sound becomes an audible level
error (a component with fraction b in the dialog stem gets amplitude gain 1 + b(G-1),
and b follows the separator's frame-by-frame decisions). Only gain applied to the
mix, or to a whole *channel*, is safe -- hence a center boost in the downmix, and
mix-level loudness/compression.

center_boost_db SEMANTICS
RELATIVE, not absolute: the center channel's weight versus the other channels in the
stereo downmix, with the matrix renormalized to unit row sum -- the same worst-case
no-clip guarantee ffmpeg's own `-ac 2` gives, so the s16 stems can't clip. On 5.1,
+6 dB means center +3.8 dB and everything else -2.2 dB in absolute terms; use
loudness.target_lufs for the absolute level. With the block disabled, or at 0 dB,
Step 1b runs plain `-ac 2` exactly as before. At 0 dB the explicit matrix would
reproduce `-ac 2` to 1 LSB (tests/test_audio_processing.py checks every layout in
_LAYOUT_CHANNELS). Layouts without a center channel (mono, stereo, quad) or outside
that table: not applicable -- logged, never an error.

night_mode / loudness (Step 6)
Analysis is ffmpeg's ebur128 (BS.1770 integrated loudness + true peak). The compressor's
threshold is set relative to the mix's own measured loudness, so a preset behaves the
same on a quiet 7.1 rip and a louder stereo episode. Gain is raise-only: when the mix
is already at/above target_lufs nothing is applied -- and then the output is
bit-identical to the plain mix. night_mode alone keeps the average loudness where it
was (makeup gain = what the compressor took off); with a target, the target wins.
Whenever gain was added, alimiter catches the peaks. It is a *sample*-peak limiter
(a -2.0 dBFS ceiling measured -1.6 dBTP in one render), delays its output by its attack
time, and -- with its default `level` on -- renormalizes the output to 0 dBFS. So it runs
with level=0 and its delay is MEASURED at run time (measure_limiter_delay) and trimmed,
keeping A/V sync and the frame count exactly.

THE UNALTERED COPY FOR WHAT-IF REDOS is audio_raw.* -- Step 1a's bit-exact stream copy,
always kept, hash recorded in job.json (audio.source_audio_sha256). Not audio_stereo.wav:
it already has the center folded in, is deleted after Step 2b by default, and regenerates
in under a minute.

SETTINGS LOG
job.json records what was actually used: "downmix" (method, the ffmpeg filter, requested
vs applied boost, source layout, the matrix) and "recombine"."audio_processing" (the
effective settings plus measurements: loudness in/out, gain, limiter headroom). Nothing
is redone automatically when config.yaml later disagrees with those records --
drift_messages() just says so at startup and names the command that would apply it.

A YAML TRAP: PyYAML (YAML 1.1) reads a bare `off` as the boolean False, so `night_mode: off`
arrives as False; settings() maps it back to "off".
"""
from __future__ import annotations

import array
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils import ConfigError, cfg_get, read_job, run_cmd, sha256_file, unmark_step_done, write_job


# ── Settings ─────────────────────────────────────────────────────────────────

NIGHT_MODES = ("off", "low", "medium", "high")

# night_mode -> (compressor ratio, threshold in dB ABOVE the mix's own measured
# integrated loudness). Starting points -- not yet auditioned on real films.
NIGHT_PRESETS = {"low": (2.0, 6.0), "medium": (3.0, 3.0), "high": (4.0, 0.0)}

CENTER_BOOST_RANGE_DB = (-12.0, 12.0)
TARGET_LUFS_RANGE = (-40.0, -10.0)
CEILING_DBFS_RANGE = (-12.0, -0.1)


@dataclass(frozen=True)
class Settings:
    """
    audio_processing.* from config.yaml. The *_requested fields are what the file
    says; the properties below are what actually happens -- everything is inert
    while `enabled` is false, so "turn it off" is one switch and job.json can
    record the effective values.
    """
    enabled: bool
    center_boost_db_requested: float
    night_mode_requested: str
    target_lufs_requested: Optional[float]
    ceiling_dbfs: float

    @property
    def center_boost_db(self) -> float:
        return self.center_boost_db_requested if self.enabled else 0.0

    @property
    def night_mode(self) -> str:
        return self.night_mode_requested if self.enabled else "off"

    @property
    def target_lufs(self) -> Optional[float]:
        return self.target_lufs_requested if self.enabled else None

    @property
    def mastering_active(self) -> bool:
        """Does Step 6 need to do anything beyond the plain unity-gain amix?"""
        return self.night_mode != "off" or self.target_lufs is not None


def _norm_night(value) -> str:
    # YAML 1.1: a bare `off` parses as False (and `on` as True) -- see module docstring.
    if value is False or value is None:
        return "off"
    return str(value).strip().lower()


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def settings(cfg: dict) -> Settings:
    target = cfg_get(cfg, "audio_processing", "loudness", "target_lufs", allow_null=True)
    return Settings(
        enabled=bool(cfg_get(cfg, "audio_processing", "enabled")),
        center_boost_db_requested=float(cfg_get(cfg, "audio_processing", "center_boost_db")),
        night_mode_requested=_norm_night(cfg_get(cfg, "audio_processing", "night_mode")),
        target_lufs_requested=None if target is None else float(target),
        ceiling_dbfs=float(cfg_get(cfg, "audio_processing", "loudness", "ceiling_dbfs")),
    )


def validate(cfg: dict) -> None:
    """
    Fail fast, before Step 1a, on an audio_processing block the pipeline can't act on
    (called once from pipeline.py's main(), next to validate_alignment_engines()).
    utils.validate_config() only checks that keys are PRESENT; this checks values.
    """
    problems: list[str] = []

    enabled = cfg_get(cfg, "audio_processing", "enabled")
    if not isinstance(enabled, bool):
        problems.append(f"audio_processing.enabled must be true or false, got {enabled!r}")

    boost = cfg_get(cfg, "audio_processing", "center_boost_db")
    lo, hi = CENTER_BOOST_RANGE_DB
    if not _is_number(boost) or not lo <= boost <= hi:
        problems.append(f"audio_processing.center_boost_db must be a number in [{lo:g}, {hi:g}] dB, got {boost!r}")

    night = cfg_get(cfg, "audio_processing", "night_mode")
    if night is True or _norm_night(night) not in NIGHT_MODES:
        problems.append(f"audio_processing.night_mode must be one of {'/'.join(NIGHT_MODES)}, got {night!r}")

    target = cfg_get(cfg, "audio_processing", "loudness", "target_lufs", allow_null=True)
    lo, hi = TARGET_LUFS_RANGE
    if target is not None and (not _is_number(target) or not lo <= target <= hi):
        problems.append(f"audio_processing.loudness.target_lufs must be null or a number in [{lo:g}, {hi:g}] LUFS, got {target!r}")

    ceiling = cfg_get(cfg, "audio_processing", "loudness", "ceiling_dbfs")
    lo, hi = CEILING_DBFS_RANGE
    if not _is_number(ceiling) or not lo <= ceiling <= hi:
        problems.append(f"audio_processing.loudness.ceiling_dbfs must be a number in [{lo:g}, {hi:g}] dBFS, got {ceiling!r}")

    if problems:
        raise ConfigError(
            f"config.yaml has {len(problems)} audio_processing problem(s):\n  " + "\n  ".join(problems)
        )


def summary(cfg: dict) -> str:
    """Startup banner (same idea as utils.retention_summary()/censoring_summary())."""
    s = settings(cfg)
    if not s.enabled:
        return "Audio proc. : disabled  (plain -ac 2 downmix at Step 1b, plain unity-gain mix at Step 6)"
    lines = ["Audio proc. : enabled"]
    if s.center_boost_db:
        lines.append(f"              center boost  {s.center_boost_db:+.1f} dB at Step 1b  (changing it later: --redo-audio, Demucs hours)")
    else:
        lines.append("              center boost  off")
    lines.append(f"              night mode    {s.night_mode}")
    if s.target_lufs is None:
        lines.append("              loudness      off")
    else:
        lines.append(f"              loudness      raise to {s.target_lufs:.1f} LUFS, never lower")
    if s.mastering_active:
        lines.append(f"              limiter       ceiling {s.ceiling_dbfs:.1f} dBFS (only when gain is added)")
    if not (s.center_boost_db or s.mastering_active):
        lines.append("              (nothing selected: output identical to disabled)")
    return "\n".join(lines)


# ── Step 1b: center boost in the downmix ─────────────────────────────────────

# Channels per ffprobe layout name. The table doubles as the allow-list of layouts a
# center matrix is built for: tests/test_audio_processing.py checks, per entry, that the
# explicit matrix at 0 dB reproduces ffmpeg's own -ac 2 downmix to within 1 LSB.
_LAYOUT_CHANNELS = {
    "3.0":       ("FL", "FR", "FC"),
    "5.0":       ("FL", "FR", "FC", "BL", "BR"),
    "5.0(side)": ("FL", "FR", "FC", "SL", "SR"),
    "5.1":       ("FL", "FR", "FC", "LFE", "BL", "BR"),
    "5.1(side)": ("FL", "FR", "FC", "LFE", "SL", "SR"),
    "6.1":       ("FL", "FR", "FC", "LFE", "BC", "SL", "SR"),
    "7.0":       ("FL", "FR", "FC", "BL", "BR", "SL", "SR"),
    "7.1":       ("FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"),
}

# swresample's default stereo downmix weights (center_mix_level = surround_mix_level =
# M_SQRT1_2; a back-center feeds both sides at surround * M_SQRT1_2; LFE is dropped).
_CENTER_BASE = math.sqrt(0.5)
_SURROUND = math.sqrt(0.5)
_BACK_CENTER = 0.5


def build_downmix_filter(layout: str, boost_db: float) -> tuple[str, dict]:
    """
    Explicit stereo downmix for a layout in _LAYOUT_CHANNELS, with the center channel
    `boost_db` louder relative to the others, as an ffmpeg `pan` filter plus the matrix
    (for job.json). Each output row is divided by its own coefficient sum, so every row
    sums to exactly 1: the same worst-case no-clip normalization `-ac 2` applies.
    """
    chans = _LAYOUT_CHANNELS[layout]
    g = 10.0 ** (boost_db / 20.0)

    def row(front: str, surrounds: tuple[str, str]) -> dict:
        terms = {front: 1.0, "FC": _CENTER_BASE * g}
        for name in surrounds:
            if name in chans:
                terms[name] = _SURROUND
        if "BC" in chans:
            terms["BC"] = _BACK_CENTER
        total = sum(terms.values())
        return {name: round(c / total, 6) for name, c in terms.items()}

    left, right = row("FL", ("BL", "SL")), row("FR", ("BR", "SR"))

    def expr(out: str, r: dict) -> str:
        return out + "=" + "+".join(f"{c:.6f}*{name}" for name, c in r.items())

    return f"pan=stereo|{expr('FL', left)}|{expr('FR', right)}", {"FL": left, "FR": right}


def plan_downmix(cfg: dict, layout: Optional[str], channels: int) -> dict:
    """
    Decide how Step 1b downmixes this source. Returns a dict steps/extract.py acts on
    and records in job.json:

      method / filter                 "ffmpeg_default" (None: plain -ac 2) or
                                      "pan_center_boost" (an explicit -af filter)
      center_boost_db_requested       what config.yaml says
      center_boost_db_applied         what this downmix actually has (0.0 unless the pan path)
      source_layout, note, matrix     for human review
      notable, warn                   True when a boost was asked for but couldn't be applied
                                      (warn: the source is multichannel, so it's worth a WARN)
    """
    s = settings(cfg)
    plan = {
        "method": "ffmpeg_default",
        "filter": None,
        "center_boost_db_requested": s.center_boost_db_requested,
        "center_boost_db_applied": 0.0,
        "source_layout": layout,
        "note": "",
        "notable": False,
        "warn": False,
    }
    if not s.enabled:
        plan["note"] = "audio_processing.enabled is false: plain ffmpeg -ac 2 downmix"
    elif s.center_boost_db == 0:
        plan["note"] = "center_boost_db is 0: plain ffmpeg -ac 2 downmix"
    elif layout not in _LAYOUT_CHANNELS:
        plan["notable"] = True
        plan["warn"] = channels > 2
        why = "has no center channel" if channels <= 2 else "is not a layout this module has a verified center matrix for"
        plan["note"] = (
            f"center_boost_db {s.center_boost_db:+.1f} requested but not applicable: source layout "
            f"{layout!r} ({channels} ch) {why} -- plain ffmpeg -ac 2 downmix used"
        )
    else:
        filt, matrix = build_downmix_filter(layout, s.center_boost_db)
        plan.update(
            method="pan_center_boost", filter=filt, matrix=matrix,
            center_boost_db_applied=s.center_boost_db,
            note=f"center boost {s.center_boost_db:+.1f} dB, relative to the other channels (matrix renormalized to unit row sum)",
        )
    return plan


# ── Step 6: mix & master ─────────────────────────────────────────────────────

_MIX = "[0:a][1:a]amix=inputs=2:duration=first:normalize=0"
_EBUR128 = "ebur128=peak=true:framelog=quiet"
_SR = 44100                     # every WAV in this pipeline is 44.1 kHz (design doc §8)
_LIM_ATTACK_MS = 5
_LIM_RELEASE_MS = 100
_MAX_MAKEUP_DB = 12.0


def _r(x: Optional[float], nd: int = 2) -> Optional[float]:
    """Round for job.json; non-finite (silence) becomes null rather than invalid JSON."""
    return None if x is None or not math.isfinite(x) else round(x, nd)


def _analyse(inputs: list, graph: Optional[str], log) -> tuple[float, float, float]:
    """(integrated LUFS, true peak dBTP, LRA LU) of `graph` over `inputs` (or of one plain input)."""
    af = ["-filter_complex", f"{graph},{_EBUR128}"] if graph else ["-af", _EBUR128]
    res = run_cmd(
        ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", *inputs, *af, "-f", "null", "-"], log,
    )
    err = res.stderr
    if "Summary:" not in err:
        raise RuntimeError("ebur128 produced no Summary block -- cannot measure loudness:\n" + err[-600:])
    summ = err[err.rindex("Summary:"):]
    found = [re.search(pat, summ) for pat in (
        r"\bI:\s+(-?[\d.]+|-inf) LUFS", r"\bPeak:\s+(-?[\d.]+|-inf) dBFS", r"\bLRA:\s+(-?[\d.]+|-inf) LU",
    )]
    if not all(found):
        raise RuntimeError("could not parse ebur128's Summary block:\n" + summ[-600:])
    return tuple(float(m.group(1)) for m in found)  # type: ignore[return-value]


def measure_limiter_delay(attack_ms: int, release_ms: int, sr: int, log) -> int:
    """
    alimiter delays its output by (about) its attack time. Measure it on this ffmpeg
    rather than hard-coding it (219 samples at 5 ms / 44.1 kHz on 6.1.1): push one impulse
    through the same limiter settings and see where it lands.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"aevalsrc='if(eq(n,{sr}),0.5,0)':s={sr}:d=2",
        "-af", f"alimiter=limit=0.9:attack={attack_ms}:release={release_ms}:level=0",
        "-f", "s16le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("could not measure alimiter's latency:\n" + proc.stderr.decode(errors="replace")[-600:])
    samples = array.array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    delay = max(range(len(samples)), key=lambda i: abs(samples[i])) - sr
    if not 0 < delay <= 2 * int(attack_ms * sr / 1000):
        raise RuntimeError(f"measured an implausible alimiter latency ({delay} samples at attack={attack_ms} ms)")
    log.debug("  alimiter latency measured: %d samples", delay)
    return delay


def _record_settings(s: Settings) -> dict:
    return {
        "enabled": s.enabled,
        "night_mode": s.night_mode,
        "target_lufs": s.target_lufs,
        "ceiling_dbfs": s.ceiling_dbfs if s.mastering_active else None,
    }


def plain_record(s: Settings) -> dict:
    """job.json record for the unchanged Step 6: a plain unity-gain amix."""
    return {"mastering": False, **_record_settings(s),
            "note": "plain unity-gain amix (processing disabled, or nothing selected for Step 6)"}


def mix_and_master(dialog_path: Path, score_path: Path, out_path: Path, s: Settings, log) -> dict:
    """
    Recombine the two stems and apply night_mode / loudness (see module docstring).
    Writes out_path (16-bit WAV, same length as the plain mix) and returns the record
    steps/recombine.py stores under state["recombine"]["audio_processing"].
    """
    inputs = ["-i", str(dialog_path), "-i", str(score_path)]
    rec = {"mastering": True, **_record_settings(s)}

    i0, tp0, lra0 = _analyse(inputs, _MIX, log)
    rec.update(mix_lufs=_r(i0), mix_true_peak_dbtp=_r(tp0), mix_lra=_r(lra0))
    log.info("  mix as recombined: I=%.1f LUFS  TP=%.1f dBTP  LRA=%.1f LU", i0, tp0, lra0)

    if not math.isfinite(i0) or i0 <= -69.9:
        log.warning(
            "  Measured loudness (%.1f LUFS) is at/below BS.1770's absolute gate -- no programme "
            "to normalize. Writing the plain mix instead.", i0,
        )
        run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex",
                 "amix=inputs=2:duration=first:normalize=0", "-c:a", "pcm_s16le", str(out_path)], log)
        rec.update(mastering=False, note="no measurable programme loudness; plain mix written")
        return rec

    chain, i1, tp1 = _MIX, i0, tp0
    if s.night_mode != "off":
        ratio, offset = NIGHT_PRESETS[s.night_mode]
        thr = min(1.0, max(0.000976563, 10.0 ** ((i0 + offset) / 20.0)))
        chain += f",acompressor=threshold={thr:.6f}:ratio={ratio:g}:attack=20:release=300:knee=4:makeup=1:detection=rms"
        i1, tp1, lra1 = _analyse(inputs, chain, log)
        rec["compressor"] = {"ratio": ratio, "threshold_db_above_mix_lufs": offset, "threshold_linear": round(thr, 6)}
        rec.update(compressed_lufs=_r(i1), compressed_true_peak_dbtp=_r(tp1), compressed_lra=_r(lra1))
        log.info("  night_mode=%s (%g:1 from I%+.0f dB): I=%.1f LUFS  TP=%.1f dBTP  LRA=%.1f LU",
                 s.night_mode, ratio, offset, i1, tp1, lra1)

    if s.target_lufs is not None:
        gain, reason = max(0.0, s.target_lufs - i1), "target"
    elif s.night_mode != "off":
        gain, reason = min(max(0.0, i0 - i1), _MAX_MAKEUP_DB), "night_mode_makeup"   # keep the average where it was
    else:
        gain, reason = 0.0, None

    if gain >= 0.05:
        delay = measure_limiter_delay(_LIM_ATTACK_MS, _LIM_RELEASE_MS, _SR, log)
        ceiling_lin = 10.0 ** (s.ceiling_dbfs / 20.0)
        chain += (
            f",volume={gain:.3f}dB"
            f",alimiter=limit={ceiling_lin:.5f}:attack={_LIM_ATTACK_MS}:release={_LIM_RELEASE_MS}:level=0"
            f",atrim=start_sample={delay},asetpts=N/SR/TB,apad=pad_len={delay}"
        )
        pre_tp = tp1 + gain
        shave = max(0.0, pre_tp - s.ceiling_dbfs)
        rec.update(gain_db=round(gain, 2), gain_reason=reason, limiter_delay_samples=delay,
                   pre_limiter_true_peak_dbtp=_r(pre_tp), limiter_removes_up_to_db=_r(shave))
        log.info("  gain %+.1f dB (%s); pre-limiter TP %+.1f dBTP vs ceiling %.1f dBFS -> limiter removes up to %.1f dB",
                 gain, reason, pre_tp, s.ceiling_dbfs, shave)
        if shave > 6.0:
            log.warning("  The limiter has to take up to %.1f dB off the loudest peaks -- consider night_mode: "
                        "low/medium or a lower loudness.target_lufs.", shave)
    else:
        gain = 0.0
        rec.update(gain_db=0.0, gain_reason=reason)
        if s.target_lufs is not None:
            log.info("  already at/above %.1f LUFS (%.1f) -- no gain applied (raise-only).", s.target_lufs, i1)

    run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", chain,
             "-c:a", "pcm_s16le", str(out_path)], log)
    o_i, o_tp, o_lra = _analyse(["-i", str(out_path)], None, log)
    rec.update(output_lufs=_r(o_i), output_true_peak_dbtp=_r(o_tp), output_lra=_r(o_lra))
    log.info("  output:            I=%.1f LUFS  TP=%.1f dBTP  LRA=%.1f LU", o_i, o_tp, o_lra)
    return rec


# ── Settings log, drift detection, recap ─────────────────────────────────────

def _master_key(rec: dict) -> dict:
    """The part of a settings record that decides what Step 6 produces (floats rounded for comparison)."""
    def num(x):
        return None if x is None else round(float(x), 3)
    return {"night_mode": rec.get("night_mode") or "off", "target_lufs": num(rec.get("target_lufs")),
            "ceiling_dbfs": num(rec.get("ceiling_dbfs"))}


def drift_messages(state: dict, cfg: dict, done: list) -> list[str]:
    """
    Compare what a job's finished stages were BUILT with (the records in job.json) to what
    config.yaml says now. Only stages that are still marked complete are compared -- a stage
    that is about to be redone has nothing to warn about. Never redoes anything.
    """
    msgs: list[str] = []
    s = settings(cfg)

    if "1b_downmix" in done:
        audio = state.get("audio", {})
        plan = plan_downmix(cfg, audio.get("channel_layout"), int(audio.get("channels") or 0))
        was = float((state.get("downmix") or {}).get("center_boost_db_applied") or 0.0)
        now = plan["center_boost_db_applied"]
        if abs(was - now) > 1e-9:
            msgs.append(
                f"Audio settings changed since this job's Step 1b downmix was built: center boost "
                f"{was:+.1f} dB -> {now:+.1f} dB. NOT applied (nothing was redone). To apply: hush.sh "
                f"--redo-audio <input>  (re-downmix + Demucs: hours; no recognition)."
            )

    if "6_recombine" in done:
        recorded = (state.get("recombine") or {}).get("audio_processing") or {}
        was, now = _master_key(recorded), _master_key(_record_settings(s))
        if was != now:
            msgs.append(
                f"Audio settings changed since this job's Step 6 mix was built: {_fmt_master(was)} -> "
                f"{_fmt_master(now)}. NOT applied (nothing was redone). To apply: hush.sh "
                f"--redo-step 6_recombine <input>  (minutes)."
            )
    return msgs


def _fmt_master(k: dict) -> str:
    target = "none" if k["target_lufs"] is None else f"{k['target_lufs']:.1f}"
    ceiling = "" if k["ceiling_dbfs"] is None else f", ceiling {k['ceiling_dbfs']:.1f}"
    return f"night_mode={k['night_mode']}, target_lufs={target}{ceiling}"


def completion_lines(state: dict) -> list[str]:
    """End-of-run recap lines (pipeline.py's 'Pipeline complete' summary)."""
    dm = state.get("downmix") or {}
    ap = (state.get("recombine") or {}).get("audio_processing") or {}
    boost = dm.get("center_boost_db_applied") or 0.0
    down = (f"center boost {boost:+.1f} dB at Step 1b ({dm.get('source_layout')}, explicit matrix)"
            if boost else "plain ffmpeg -ac 2 (no center boost)")
    if ap.get("mastering"):
        parts = []
        if ap.get("night_mode") not in (None, "off"):
            parts.append(f"night_mode={ap['night_mode']}")
        if ap.get("gain_db"):
            parts.append(f"gain {ap['gain_db']:+.1f} dB")
        if ap.get("target_lufs") is not None:
            parts.append(f"target {ap['target_lufs']:.1f} LUFS")
        if ap.get("output_lufs") is not None and ap.get("output_true_peak_dbtp") is not None:
            parts.append(f"result {ap['output_lufs']:.1f} LUFS / {ap['output_true_peak_dbtp']:.1f} dBTP")
        if ap.get("limiter_removes_up_to_db"):
            parts.append(f"limiter up to {ap['limiter_removes_up_to_db']:.1f} dB")
        mix = ", ".join(parts) or "mastered"
    else:
        mix = "plain unity-gain mix"
    return [f"Audio processing : downmix: {down}", f"                   mix: {mix}"]


# ── --redo-audio ─────────────────────────────────────────────────────────────

class RedoAudioError(RuntimeError):
    """--redo-audio can't run against this job (clear message; nothing was changed)."""


# Steps whose outputs depend on the downmix. 3/3b/4b (recognition, flagging, review) and 6c
# (SRTs: transcript + censor_log.json only) are deliberately NOT here.
_AUDIO_STEPS = ("1b_downmix", "1c_segment", "2_separate", "2b_merge_audio",
                "5_mute", "6_recombine", "6b_encode", "7_mux")


def redo_pending(state: dict) -> bool:
    return bool(state.get("audio_redo_pending"))


def audio_merged(done: list, state: dict) -> bool:
    """
    Is the canonical dialog.wav/score_sfx.wav stage (Step 2b) done? "3b_merge" alone also
    counts -- for a job from before Steps 2b and 3b were split, that marker meant both
    halves -- EXCEPT while a --redo-audio is pending: it unmarks 2b_merge_audio but leaves
    3b_merge (the transcript merge is untouched), and the legacy shortcut must not hide that.
    """
    if "2b_merge_audio" in done:
        return True
    return "3b_merge" in done and not redo_pending(state)


def clear_redo_pending(job_dir: Path) -> None:
    state = read_job(job_dir)
    if state.pop("audio_redo_pending", None) is not None:
        write_job(job_dir, state)


def prepare_audio_redo(job_dir: Path, state: dict, log) -> list[str]:
    """
    --redo-audio: put an existing job back to "audio not built yet, everything about the words
    already known". Verifies the preconditions (including the original audio's hash, before
    anyone spends Demucs hours on it), deletes the audio chain's outputs, unmarks Steps 1b-2b
    and 5-7, and leaves job.json's audio_redo_pending marker so a plain re-run resumes the
    chain if this one is interrupted (see audio_merged()).

    Existence-based resume checks in split_into_segments(), separate() and merge_audio() would
    otherwise happily reuse stale stems, so those files are removed here, the same way
    pipeline.py's _clear_transcript_redo_state() handles transcripts. NOT touched:
    audio_raw.*, transcript*.json, matches.json, review.json, censor_log.json, SRTs.
    Returns the names of the files removed.
    """
    done = state.get("steps_completed", [])
    problems = [f"Step {step} hasn't completed for this job" for step in ("3b_merge", "4b_flag") if step not in done]
    problems += [f"{name} is missing" for name in ("transcript.json", "matches.json") if not (job_dir / name).exists()]
    audio = state.get("audio", {})
    raw = job_dir / audio["raw_file"] if audio.get("raw_file") else None
    if raw is None or not raw.exists():
        problems.append("the original audio (audio_raw.*, always kept) is missing")
    if problems:
        raise RedoAudioError(
            "--redo-audio needs a job that already has its words (transcript, flagged matches) and its original audio:\n  "
            + "\n  ".join(problems)
        )

    expected = audio.get("source_audio_sha256")
    if expected:
        log.info("  Verifying %s against the hash recorded at extraction ...", raw.name)
        actual = sha256_file(raw, log)
        if actual is not None and actual != expected:
            raise RedoAudioError(
                f"{raw.name} no longer matches the SHA-256 recorded when it was extracted "
                f"({actual[:16]}... vs {expected[:16]}...). Refusing to spend Demucs hours on a changed file."
            )
    else:
        log.warning("  No recorded hash for %s (job predates it) -- relying on the duration check only.", raw.name)

    victims = [job_dir / n for n in ("audio_stereo.wav", "dialog.wav", "score_sfx.wav", "dialog_censored.wav",
                                     "audio_censored.wav", "audio_encoded.mka")]
    for pattern in ("audio_stereo_[0-9][0-9].wav", "dialog_[0-9][0-9].wav", "score_sfx_[0-9][0-9].wav"):
        victims += sorted(job_dir.glob(pattern))
    removed = [p.name for p in victims if p.exists()]
    for p in victims:
        p.unlink(missing_ok=True)
    shutil.rmtree(job_dir / ".demucs_work", ignore_errors=True)

    for step in _AUDIO_STEPS:
        unmark_step_done(job_dir, step)
    st = read_job(job_dir)
    st["audio_redo_pending"] = {"started_at": datetime.now(tz=timezone.utc).isoformat(), "source": raw.name}
    write_job(job_dir, st)

    log.info("  --redo-audio: rebuilding the audio from %s (Steps 1b-2b, then 5-7). Kept as-is: transcript, "
             "matches.json, review.json, censor_log.json, SRTs.", raw.name)
    if removed:
        log.info("  Removed the previous audio chain's files: %s", ", ".join(sorted(removed)))
    log.info("  Demucs is the slow part (about realtime at shifts=1). If interrupted, re-run WITHOUT --redo-audio to resume.")
    return removed

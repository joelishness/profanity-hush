"""
profanity-hush — shared utilities

Imported by pipeline.py and every steps/ module.
"""
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback as _traceback
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional


# ── Timezone resolution ────────────────────────────────────────────────────────
#
# Containers default to UTC with no awareness of the host's wall clock. Every
# timestamp in this pipeline used to be computed with tz=timezone.utc and
# printed with no indication of that fact -- which, for anyone not physically
# in UTC, makes console timestamps (and job.json's started_at/failed_at, and
# the job directory's leading YYYYMMDD_HHMMSS) look like unlabelled local
# time while actually running several hours ahead of the user's own clock.
#
# Fix: hush.sh captures the host's current UTC offset at invocation time
# (`date +%z`, e.g. "-0700") and forwards it as AC_TZ_OFFSET, plus an
# optional cosmetic abbreviation (`date +%Z`, e.g. "PDT") as AC_TZ_NAME.
# A numeric offset -- not a named zone like "America/Los_Angeles" -- is
# deliberate: it works with no timezone database (tzdata) inside the image,
# and with no dependency on the host and container agreeing on one. The
# tradeoff is that it's captured once, at job start, rather than tracking a
# DST transition mid-run -- irrelevant for a process that runs for hours,
# not months.
#
# LOCAL_TZ is resolved once, at import time (the env var is set by the
# container's entrypoint before Python even starts, so this isn't racy).
# Falls back to plain UTC -- logged explicitly via timezone_banner(), not
# silently -- if AC_TZ_OFFSET was never forwarded (e.g. the container was
# run directly, without hush.sh).

_TZ_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def _resolve_local_tz() -> tuple[timezone, bool]:
    """
    Parse AC_TZ_OFFSET (and, optionally, AC_TZ_NAME for display) into a
    timezone object.  Returns (tz, explicit) -- explicit is False when
    AC_TZ_OFFSET was absent/unparseable and tz is just timezone.utc, so
    callers (timezone_banner()) can tell "really UTC" apart from
    "defaulted to UTC because nothing else was provided".
    """
    raw = os.environ.get("AC_TZ_OFFSET", "").strip()
    m = _TZ_OFFSET_RE.match(raw)
    if not m:
        return timezone.utc, False
    sign, hh, mm = m.groups()
    delta = timedelta(hours=int(hh), minutes=int(mm))
    if sign == "-":
        delta = -delta
    name = os.environ.get("AC_TZ_NAME", "").strip() or None
    return timezone(delta, name=name), True


LOCAL_TZ, LOCAL_TZ_EXPLICIT = _resolve_local_tz()


# ── Logging ───────────────────────────────────────────────────────────────────

class _StepFormatter(logging.Formatter):
    """
    Produces lines like:
      2026-06-25 16:25:41 -0700 [INFO ] [extract  ] Probing audio stream — movie.mkv

    Timestamps render in LOCAL_TZ (above) -- the host's wall-clock offset,
    forwarded by hush.sh -- rather than the container's own default UTC
    clock, and every line carries its own numeric UTC offset (%z) so the
    timestamp is self-describing regardless of what it resolved to. This
    replaces the previous behaviour, where every timestamp was silently
    UTC with no label at all, indistinguishable from (but several hours
    off from) local time for anyone not in UTC. If AC_TZ_OFFSET was never
    forwarded, this still prints "+0000" -- now an honest, labelled UTC
    rather than an unlabelled one -- see timezone_banner() for the
    once-per-run startup note covering that case.
    """
    _LABELS = {
        logging.DEBUG:   "DEBUG",
        logging.INFO:    "INFO ",
        logging.WARNING: "WARN ",
        logging.ERROR:   "ERROR",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=LOCAL_TZ).strftime(
            "%Y-%m-%d %H:%M:%S %z"
        )
        level = self._LABELS.get(record.levelno, record.levelname[:5])
        step = getattr(record, "step", record.name)
        return f"{ts} [{level}] [{step:<9}] {record.getMessage()}"


class _WarningCollectingHandler(logging.Handler):
    """
    Silently taps every WARNING-and-above record logged through the
    'hush' logger for the lifetime of this process, purely as a side
    channel -- it never formats or emits anything itself, so it has zero
    effect on what actually reaches the console or the per-job log file
    (setup_logging() attaches it alongside those, not instead of them).

    Why this exists: pipeline.py's end-of-run summary used to decide
    what's "notable" enough to surface in the AC_RESULT line (-> hush.sh
    --batch's own batch log) by checking a small, hand-maintained list of
    specific state fields -- transcribe.py's mfa_fallback_segments, is
    encode.py's fallback_reason. Each of those is real and still gets its
    own specific, human-readable entry (see pipeline.py). But that
    approach silently under-covers by construction: any OTHER
    log.warning() call anywhere in this pipeline (a step's own edge case,
    or a new one added later) has no state field wired through to
    pipeline.py at all, so it scrolled past on the console and into this
    job's own logs/*.log, but never reached the batch log -- indistinguishable
    there from a run with nothing to report. This handler catches all of
    those too, generically, so a new warning shows up in the batch log
    the same day it starts happening rather than only once someone
    notices the gap and adds bespoke plumbing for that one case.

    One instance for the module's lifetime (see _WARNING_COLLECTOR
    below), not reset between jobs -- doesn't need to be, since
    setup_logging() is itself only ever called once per process, and
    hush.sh --batch runs each file as a fresh container/process (see
    hush.sh's own comments on why only stdout, not stderr, is captured
    per file in that loop). A resumed/skipped segment or step logs
    nothing new this run, so it correctly contributes nothing here either
    -- see e.g. transcribe.py's per-segment resume branch.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def summary(self) -> str:
        """
        e.g. '5 warning(s) logged (transcribe×4, matching×1) -- see this
        run's own log file for detail', or '' if nothing was collected.
        Grouped by step (the same tag _StepFormatter prints as
        '[transcribe]' etc.), not by exact message text -- this feeds one
        scannable batch-log line, not a second copy of the messages
        themselves; those already live in job_dir/logs/*.log (see
        pipeline.py's own AC_RESULT comment for why the batch log
        deliberately stays this thin).
        """
        if not self.records:
            return ""
        by_step: dict[str, int] = {}
        for r in self.records:
            step = getattr(r, "step", r.name)
            by_step[step] = by_step.get(step, 0) + 1
        breakdown = ", ".join(
            f"{step}×{n}" for step, n in sorted(by_step.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        return (
            f"{len(self.records)} warning(s) logged ({breakdown}) -- "
            "see this run's own log file for detail"
        )


# Module-level singleton, not one per setup_logging() call -- see
# _WarningCollectingHandler's own docstring for why process lifetime is
# exactly the right scope here.
_WARNING_COLLECTOR = _WarningCollectingHandler()


def warning_summary() -> str:
    """
    Short, scannable summary of every WARNING-and-above record logged
    through the 'hush' logger so far this process -- '' if none. See
    _WarningCollectingHandler's docstring and pipeline.py's end-of-run
    "notable" comment for the full picture of where this feeds into.
    """
    return _WARNING_COLLECTOR.summary()


def setup_logging(level_name: str) -> logging.Logger:
    """Configure and return the root 'hush' logger."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_StepFormatter())
    logger = logging.getLogger("hush")
    logger.setLevel(level)
    if not logger.handlers:
        logger.addHandler(handler)
        logger.addHandler(_WARNING_COLLECTOR)
    logger.propagate = False
    return logger


def attach_file_logging(job_dir: Path, level_name: str, log: Optional[logging.LoggerAdapter] = None) -> Path:
    """
    Add a second handler to the 'hush' logger that mirrors console output
    to job_dir/logs/{YYYYMMDD_HHMMSS}.log -- same _StepFormatter, same
    level as setup_logging()'s console handler, so the file is a faithful,
    complete copy of whatever actually scrolled past on the console from
    this point forward (deliberately the *same* level as the console, not
    always DEBUG regardless of it -- this mirrors what you saw, rather
    than silently capturing more than that).

    "From this point forward" is the one real limitation: this can't be
    called until job_dir itself is known, and job_dir isn't resolved until
    partway into main() (compute_job_id() needs the input file validated
    first, and finding-or-creating job_dir is what tells a fresh run apart
    from a resumed one) -- so the startup banner and the handful of lines
    pipeline.py logs while still locating/creating job_dir are necessarily
    console-only. Call this as early as job_dir allows, then have the
    caller re-log a short recap of the essentials (job ID, input path,
    resuming-or-fresh) right after, so the file is still self-contained
    and doesn't require the console scrollback for context (pipeline.py
    does this immediately after calling here).

    One file per invocation, not one growing file per job: a job that's
    resumed, corrected (§13.4), or retried after a failure gets one
    logs/*.log per attempt, timestamped at the moment that attempt
    started, rather than a single file that either keeps growing
    unbounded across a job's whole lifetime or gets silently clobbered the
    way this pipeline's other outputs use -y/overwrite-in-place semantics.
    That history is exactly what's useful for debugging *why* a given run
    behaved the way it did (e.g. comparing the log from a run that didn't
    resume the way it was expected to, against the job's other run logs).

    Unlike job.json -- structured, meant to stay small, and not a sensible
    place for what could be many megabytes of DEBUG-level ffmpeg/Demucs
    output across a multi-hour run -- or the large intermediate WAV/audio
    stems, log files are always kept regardless of output.keep_intermediates:
    they're the one artifact this pipeline produces specifically *because*
    something might need debugging later, so deleting them by default
    would defeat the point. No step ever deletes anything under logs/.

    Returns the path to the new log file.
    """
    if log is None:
        log = step_logger("pipeline")

    logs_dir = job_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    ts       = datetime.now(tz=LOCAL_TZ).strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"{ts}.log"

    level   = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(_StepFormatter())
    handler.setLevel(level)

    logger = logging.getLogger("hush")
    logger.addHandler(handler)
    # Defensive only: in normal use this matches the level setup_logging()
    # already set on the logger itself (both calls read the same
    # output.log_level), so this is a no-op -- but the logger's own level
    # gates every handler before any individual handler's level is even
    # considered, so if that ever weren't true, widening it here is what
    # actually makes DEBUG records reach this handler instead of being
    # dropped before either handler sees them.
    if level < logger.level:
        logger.setLevel(level)

    log.info("Log file    : %s", log_path)
    return log_path


def step_logger(name: str) -> logging.LoggerAdapter:
    """Return a LoggerAdapter that tags every message with the given step name."""
    return logging.LoggerAdapter(logging.getLogger("hush"), {"step": name})


# ── Config ────────────────────────────────────────────────────────────────────

class ConfigError(RuntimeError):
    """
    Raised when the resolved config (built-in config.yaml + optional host
    override + env vars -- see load_config()) is missing a setting the
    pipeline needs, or has it set to something that setting doesn't accept
    (an explicit null on a field that isn't allow_null=True in cfg_get()).

    Deliberately not silently papered over with a hardcoded Python value --
    that used to be exactly how a config.yaml edit could go unnoticed, with
    the pipeline quietly running on a stale literal nobody remembered was
    there. See cfg_get()'s docstring.
    """


# config.yaml baked into the image at build time (see Dockerfile) -- the
# base layer for every setting, and the only "default" left in the system.
# To change a default: edit config/config.yaml and rebuild the image.
# Nothing in src/*.py should ever need a matching edit again.
DEFAULT_CONFIG_PATH = Path("/app/defaults/config.yaml")


# Every transcription/alignment engine this pipeline knows how to run --
# the single shared registry utils.py's own validate_alignment_engines()/
# alignment_engines_summary() below and steps/transcribe.py's per-segment
# dispatch both key off of, so the two can never drift out of sync about
# which engines exist. Adding a new engine: one more name here, a branch
# in steps/transcribe.py's per-segment loop to actually run it, its own
# alignment.engines.<name> block in config.yaml, and (if it has one) a
# display label in steps/transcribe.py's own _ENGINE_LABELS. Nothing else
# needs to change -- steps/merge.py, steps/transcript_srt.py, and
# steps/mux.py all discover engines from job.json's own "alignment_engines"
# list (written once, by steps/transcribe.py, at the end of a run), never
# by importing this tuple directly.
ALIGNMENT_ENGINE_NAMES = ("whisperx", "mfa", "crisperwhisper")


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge `override` onto `base`, returning a new dict.

    A key present in `override` always wins, at whatever nesting level it
    appears -- including an explicit `null`, which is a real, intentional
    value for the handful of settings that treat it that way (see
    cfg_get's allow_null). Only when BOTH sides have a dict at the same
    key do we recurse and merge key-by-key; anything else (scalar, list,
    or a type mismatch) is a full replacement of that key, not a merge.

    This is what lets a host-mounted config.yaml override just the one or
    two settings someone actually wants to change, without needing to be
    a full copy of the template -- everything it doesn't mention inherits
    from `base` (the built-in config.yaml).
    """
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: "str | Path") -> dict[str, Any]:
    """
    Load config.yaml and apply environment variable overrides.

    config/config.yaml (this repo) is the single source of truth for every
    tunable default -- there is no parallel set of hardcoded Python
    literals to keep in sync with it any more. In order:

      1. The built-in config.yaml baked into the image at build time
         (DEFAULT_CONFIG_PATH, a copy of config/config.yaml as of the last
         build -- see Dockerfile) is always loaded first, as the base
         layer. This is the only "default" left in the system.
      2. If a config.yaml is ALSO found at `config_path` (normally the
         host's bind-mounted /config/config.yaml -- see hush.sh /
         docker-compose.yml), it's deep-merged on top of the base layer
         (see _deep_merge()). A host config.yaml only needs to specify
         the settings it wants to override; everything else inherits from
         the built-in one.
      3. AC_* environment variables are applied last, same as before.

    Override precedence (highest wins):
      environment variables  >  host-mounted config.yaml (config_path)
        >  built-in config.yaml baked into the image

    Env vars applied:
      AC_LOG_LEVEL                  → output.log_level
      AC_KEEP_INTERMEDIATES         → output.keep_intermediates          (1 = True)
      AC_KEEP_CORRECTION_ARTIFACTS  → output.keep_correction_artifacts   (1 = True, 0 = False --
                                       this one defaults to True, so unlike the others, explicitly
                                       turning it *off* needs its own value, not just absence)
      AC_INTERACTIVE                → interactive.enabled                (1 = True)
      AC_SEGMENT_SIZE               → audio.segment_size_sec             (seconds, int)
      AC_INPUT_HOST_DIR             → paths.input_host_dir               (see paths_banner() below)
      AC_OUTPUT_HOST_DIR            → paths.output_host_dir              (see paths_banner() below)

    The last two have no config.yaml equivalent, same reasoning as
    AC_TZ_OFFSET/AC_TZ_NAME (see "Timezone resolution" above): a host
    directory is host-environment information, not pipeline behaviour,
    so there's nothing meaningful to put in a static config file -- only
    hush.sh (or docker-compose.yml, or a person invoking `docker run` by
    hand) can know it, at invocation time.

    Raises ConfigError if even the built-in config.yaml is missing or
    fails to parse -- that should never happen in a correctly-built image
    (see Dockerfile); it means the image needs rebuilding from a repo
    checkout that still has a valid config/config.yaml.
    """
    def _read(p: Path) -> dict[str, Any]:
        with p.open() as f:
            return yaml.safe_load(f) or {}

    try:
        cfg: dict[str, Any] = _read(DEFAULT_CONFIG_PATH)
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(
            f"built-in config.yaml ({DEFAULT_CONFIG_PATH}) is missing or "
            f"invalid -- this should not happen in a correctly-built "
            f"image: {e}"
        ) from e

    path = Path(config_path)
    if path.exists():
        try:
            cfg = _deep_merge(cfg, _read(path))
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(f"could not read config.yaml at {path}: {e}") from e

    # Environment variable overrides
    if v := os.environ.get("AC_LOG_LEVEL"):
        cfg.setdefault("output", {})["log_level"] = v
    if os.environ.get("AC_KEEP_INTERMEDIATES") == "1":
        cfg.setdefault("output", {})["keep_intermediates"] = True
    if os.environ.get("AC_KEEP_CORRECTION_ARTIFACTS") == "1":
        cfg.setdefault("output", {})["keep_correction_artifacts"] = True
    elif os.environ.get("AC_KEEP_CORRECTION_ARTIFACTS") == "0":
        cfg.setdefault("output", {})["keep_correction_artifacts"] = False
    if os.environ.get("AC_INTERACTIVE") == "1":
        cfg.setdefault("interactive", {})["enabled"] = True
    if v := os.environ.get("AC_SEGMENT_SIZE"):
        cfg.setdefault("audio", {})["segment_size_sec"] = int(v)
    if v := os.environ.get("AC_INPUT_HOST_DIR"):
        cfg.setdefault("paths", {})["input_host_dir"] = v
    if v := os.environ.get("AC_OUTPUT_HOST_DIR"):
        cfg.setdefault("paths", {})["output_host_dir"] = v

    return cfg


def keep_intermediate(cfg: dict, *, correction_artifact: bool = False) -> bool:
    """
    Whether a large intermediate WAV file should be KEPT on disk (not
    deleted) once the step that produced it is no longer the bottleneck.

    Single source of truth for the retention policy described in design
    doc §6: every step that deletes a large intermediate (steps/merge.py,
    steps/mute.py, steps/recombine.py, steps/encode.py, steps/mux.py)
    calls this rather than reading output.keep_intermediates /
    output.keep_correction_artifacts directly, specifically so the policy
    can never drift between steps the way it could when each one
    re-implemented its own condition.

    correction_artifact=False (default): the file is fully superseded once
      consumed downstream (per-segment stems, audio_stereo*.wav,
      dialog_censored.wav, audio_censored.wav, audio_encoded.mka,
      transcript_NN.json) -- kept only if output.keep_intermediates is
      true. Most of these are also cheaply regenerable; transcript_NN.json
      is the one exception (re-transcribing costs real WhisperX time), but
      it's still fully superseded by transcript.json once Step 3b succeeds,
      which is what actually governs its deletion here -- see
      steps/merge.py's module docstring.

    correction_artifact=True: the file is one of the two artifacts
      (dialog.wav, score_sfx.wav) that make the --skip-index / --add-interval
      / --redo-review correction workflow (design doc §13.4) possible
      without re-running Step 2's Demucs separation -- kept if EITHER
      output.keep_intermediates OR output.keep_correction_artifacts
      (default true) is true.
    """
    keep_intermediates = bool(cfg_get(cfg, "output", "keep_intermediates"))
    if not correction_artifact:
        return keep_intermediates
    keep_correction = bool(cfg_get(cfg, "output", "keep_correction_artifacts"))
    return keep_intermediates or keep_correction


def retention_summary(cfg: dict) -> str:
    """
    Multi-line, human-readable summary of the *resolved* retention
    settings -- meant to be logged once, at startup, at INFO level.

    The motivating failure mode: a host-side --keep-tmp flag or
    AC_KEEP_INTERMEDIATES env var that silently never reached the
    container (e.g. hush.sh forwarding it incorrectly) was previously
    only discoverable hours later, when an expected intermediate file
    turned out not to be there. Logging the settings pipeline.py actually
    resolved -- not what the user thinks they asked for on the host --
    makes that mismatch visible immediately instead.
    """
    ki = bool(cfg_get(cfg, "output", "keep_intermediates"))
    kc = bool(cfg_get(cfg, "output", "keep_correction_artifacts"))
    return (
        f"Retention   : keep_intermediates={ki}  keep_correction_artifacts={kc}\n"
        f"  transcript.json, matches.json, review.json, censor_log.json : always kept\n"
        f"  dialog.wav, score_sfx.wav                                   : "
        f"{'kept' if (ki or kc) else 'deleted after use'}\n"
        f"  audio_stereo*.wav, dialog_censored.wav, audio_censored.wav,\n"
        f"  audio_encoded.mka, transcript_NN.json (per-segment)         : "
        f"{'kept' if ki else 'deleted after use'}"
    )


def paths_banner(cfg: dict) -> str:
    """
    Two-line, human-readable summary of whether host-side directories
    were resolved for input/output -- meant to be logged once, at
    startup, at INFO level. Same motivation as retention_summary() and
    timezone_banner(): a host path that silently failed to reach the
    container (the exact same failure shape as the hush.sh
    AC_KEEP_INTERMEDIATES forwarding bug -- see design doc §6) should be
    visible in the first few lines of output, not discoverable only
    after a multi-hour run finishes and job.json's input_path/
    mux.output_path still show a container mount point instead of
    something host-navigable.

    Does not raise or fail the run either way -- unresolved host paths
    degrade job.json's readability, not the pipeline's correctness, so
    this only ever informs, matching AC_TZ_OFFSET's fallback philosophy
    (an honest, clearly-labelled container path, not a placeholder that
    could be mistaken for a real one).
    """
    input_host  = cfg_get(cfg, "paths", "input_host_dir", default=None)
    output_host = cfg_get(cfg, "paths", "output_host_dir", default=None)

    if input_host:
        input_line = f"Paths       : input  = {input_host}"
    else:
        input_line = (
            "Paths       : input  = (unresolved — AC_INPUT_HOST_DIR not set; "
            "job.json will show the container path /input instead)"
        )
    if output_host:
        output_line = f"              output = {output_host}"
    else:
        output_line = (
            "              output = (unresolved — AC_OUTPUT_HOST_DIR not set; "
            "job.json will show the container path /output instead)"
        )
    return f"{input_line}\n{output_line}"


def alignment_engines_summary(cfg: dict) -> str:
    """
    Multi-line, human-readable summary of the *resolved* alignment engine
    toggles -- meant to be logged once, at startup, at INFO level. Same
    motivation as retention_summary()/paths_banner()/censoring_summary()
    above: which engines actually run this job, which (if any) get
    exported as debug/comparison subtitles, and which one is
    authoritative should be visible in the first few lines of output,
    not something a person has to open job.json to confirm after the
    fact.
    """
    tags: list[str] = []
    debug_engines: list[str] = []

    for name in ALIGNMENT_ENGINE_NAMES:
        enabled = bool(cfg_get(cfg, "alignment", "engines", name, "enabled"))
        final   = bool(cfg_get(cfg, "alignment", "engines", name, "final"))
        debug   = bool(cfg_get(cfg, "alignment", "engines", name, "debug_subtitle"))
        tag = "on" if enabled else "off"
        if final:
            tag += "(final)"
        tags.append(f"{name}={tag}")
        if enabled and debug:
            debug_engines.append(name)

    debug_str = ", ".join(debug_engines) if debug_engines else "(none)"
    return (
        f"Alignment   : {'  '.join(tags)}\n"
        f"              debug subtitles: {debug_str}"
    )


def censoring_summary(cfg: dict) -> str:
    """
    Two-line, human-readable summary of the *resolved* censoring settings
    -- meant to be logged once, at startup, at INFO level, every run,
    regardless of resume state. Same motivation as retention_summary() /
    paths_banner() / timezone_banner() above.

    Without this, method/padding_ms are only ever logged from inside
    steps/mute.py's own "Step 5 -- mute dialog stem" line, and the word
    list path only from inside steps/review.py's flag() -- both of which
    only run their logging branch on a fresh execution of that step. On
    a resumed job (5_mute / 4b_flag already in steps_completed), neither
    ever prints, so nothing in the log confirms which word list or
    padding was actually in effect for *this* invocation -- exactly the
    kind of "did my setting actually take effect" gap the other three
    banners exist to close for retention/paths/timezone.
    """
    method     = cfg_get(cfg, "censoring", "method")
    padding_ms = cfg_get(cfg, "censoring", "padding_ms")
    word_list  = cfg_get(cfg, "censoring", "word_list")
    return (
        f"Censoring   : method={method}  padding={padding_ms}ms\n"
        f"              word_list = {word_list}"
    )


_UNSET = object()  # sentinel: distinguishes "no default given" from "default=None"


def cfg_get(cfg: dict, *keys: str, default: Any = _UNSET, allow_null: bool = False) -> Any:
    """
    Safely navigate nested config keys.

    If `default` is omitted, a missing key -- or, unless allow_null=True,
    an explicit `null` -- raises ConfigError rather than silently
    returning some Python-side value. config.yaml (built-in + optional
    host override, see load_config()) is expected to define every setting
    the pipeline actually reads; a key that's truly missing means
    config.yaml itself is incomplete or has a typo, which is worth
    surfacing immediately and clearly (see validate_config(), which does
    this once at startup rather than however far into a run the first
    reader of that key happens to be) rather than papering over with a
    hardcoded fallback that can silently drift out of sync with
    config.yaml over time -- which is exactly the failure mode this
    function used to have, and the reason it no longer does.

    Pass an explicit `default=` only for settings that are legitimately
    optional and have no entry in config.yaml's schema at all -- e.g.
    paths.input_host_dir / paths.output_host_dir, which are populated
    solely from AC_INPUT_HOST_DIR / AC_OUTPUT_HOST_DIR and were never
    meant to be config.yaml keys (see load_config()'s docstring). That's
    a genuinely different situation from "this config.yaml setting has a
    fallback value" -- there's no config.yaml value to fall back to.

    allow_null=True: treat an explicit `null` as a real, meaningful value
    rather than "missing" -- e.g. whisperx.language: null means
    "auto-detect", distinct from the key being absent (an error, same as
    any other required setting) or set to a real language code. Only the
    final key in the path gets this treatment; a missing or null
    *intermediate* section still raises/returns default regardless, since
    the isinstance(node, dict) check on the next iteration catches that
    before allow_null ever comes into play.

    Examples:
      cfg_get(cfg, "demucs", "shifts")                          # required
      cfg_get(cfg, "whisperx", "language", allow_null=True)     # required, null is meaningful
      cfg_get(cfg, "paths", "input_host_dir", default=None)     # genuinely optional
    """
    def _missing() -> Any:
        if default is _UNSET:
            raise ConfigError(
                f"config.yaml is missing required setting "
                f"'{'.'.join(keys)}' -- check it against the shipped "
                f"template at config/config.yaml."
            )
        return default

    node: Any = cfg
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return _missing()
        node = node[k]
    if node is None and not allow_null:
        return _missing()
    return node


# The settings where an explicit `null` is a real value rather than
# "missing" -- kept in sync with the actual allow_null=True call sites
# (steps/transcribe.py's alignment.engines.whisperx.language; steps/
# align_mfa.py's alignment.engines.mfa.g2p_model, to disable G2P fallback
# entirely and rely on the dictionary alone; steps/transcribe.py's
# alignment.engines.crisperwhisper.language -- see config.yaml's own
# comment on that one for why null there means "default to English", not
# true auto-detection the way it does for whisperx), so validate_config()
# below checks each leaf the same way its real reader will.
_ALLOW_NULL_KEYS = {
    ("alignment", "engines", "whisperx", "language"),
    ("alignment", "engines", "mfa", "g2p_model"),
    ("alignment", "engines", "crisperwhisper", "language"),
    ("audio_processing", "loudness", "target_lufs"),     # null = loudness stage off
}


def validate_config(cfg: dict) -> None:
    """
    Confirm the resolved config has a usable value everywhere the built-in
    config.yaml (the always-complete base layer -- see load_config())
    defines one, raising a single ConfigError listing everything missing
    at once if not.

    Deliberately self-maintaining: it walks whatever config/config.yaml's
    own structure currently is (via DEFAULT_CONFIG_PATH) rather than
    checking against a separate hand-written list of "required keys" --
    there's nothing to remember to update when a setting is added,
    renamed, or removed from config.yaml. The shipped config.yaml file
    *is* the schema.

    Called once, at startup, by pipeline.py's main() -- right after
    load_config() and before anything else runs -- specifically so a
    config.yaml edit that drops or misspells a key fails in the first
    second of a run instead of hours in, at whichever step happens to be
    the first reader of that key. This pipeline is explicitly designed
    for unattended overnight runs; finding out about a bad config.yaml at
    Step 5 after Steps 1-4 already burned CPU-hours is a far worse
    experience than finding out before Step 1 starts.
    """
    with DEFAULT_CONFIG_PATH.open() as f:
        base = yaml.safe_load(f) or {}

    missing: list[str] = []

    def _walk(section: dict, path: tuple) -> None:
        for key, value in section.items():
            full_path = path + (key,)
            if isinstance(value, dict):
                _walk(value, full_path)
            else:
                try:
                    cfg_get(cfg, *full_path, allow_null=full_path in _ALLOW_NULL_KEYS)
                except ConfigError:
                    missing.append(".".join(full_path))

    _walk(base, ())

    if missing:
        raise ConfigError(
            f"config.yaml is missing {len(missing)} required setting(s):\n  "
            + "\n  ".join(missing)
            + "\ncheck it against the shipped template at config/config.yaml."
        )


def validate_alignment_engines(cfg: dict) -> None:
    """
    Semantic checks validate_config() itself can't do (it only checks
    presence -- see its own docstring) for alignment.engines.* -- called
    once from pipeline.py's main(), immediately after validate_config()/
    validate_hush_config(), for the same "fail in the first second of a
    run, not hours in at whichever step first reads a bad value" reasoning
    both of those already use.

    Every engine in ALIGNMENT_ENGINE_NAMES is fully independent and
    separately toggled (see config.yaml's own alignment.engines comment)
    -- there is no cascade or implicit "this one depends on that one
    being on" relationship a person configuring this needs to reason
    about, with one documented exception that ISN'T a config-validity
    concern: alignment.engines.mfa.enabled: true always makes WhisperX's
    own recognition + alignment run internally too (MFA re-times
    WhisperX's recognized text; it never recognizes speech itself), REGARDLESS
    of alignment.engines.whisperx.enabled -- see steps/transcribe.py's
    module docstring. That's a real compute-cost implication worth
    knowing, but it's not something this function checks or blocks,
    since alignment.engines.whisperx.enabled: false + alignment.engines.
    mfa.enabled: true is a perfectly valid, common configuration (MFA
    without also exposing WhisperX's own raw result).

    What IS checked here, across every registered engine:
      - exactly one ENABLED engine has final: true -- Step 4b/5 need
        exactly one authoritative transcript to flag and mute words
        against; zero means nothing would ever get censored (almost
        certainly not intended), more than one is ambiguous about which
        was actually meant.
      - debug_subtitle: true and final: true both require enabled: true
        on that SAME engine -- an engine that never runs has nothing to
        export a subtitle from, and can't be authoritative for anything.

    Does NOT check that a final/debug_subtitle engine's own runtime
    dependencies are actually satisfiable (crisperwhisper installed, MFA's
    conda env present, ...) -- those are checked lazily, at first real
    use, by each engine's own module (steps/transcribe_crisperwhisper.py's
    load_crisperwhisper_model(), steps/align_mfa.py's align_with_mfa())
    with its own clear, actionable error either way. This function is
    only about whether the *configuration itself* is coherent, not
    whether the environment can deliver on it.
    """
    problems: list[str] = []
    final_engines: list[str] = []

    for name in ALIGNMENT_ENGINE_NAMES:
        enabled        = bool(cfg_get(cfg, "alignment", "engines", name, "enabled"))
        debug_subtitle = bool(cfg_get(cfg, "alignment", "engines", name, "debug_subtitle"))
        final          = bool(cfg_get(cfg, "alignment", "engines", name, "final"))

        if debug_subtitle and not enabled:
            problems.append(
                f"alignment.engines.{name}.debug_subtitle is true but "
                f"alignment.engines.{name}.enabled is false -- there's "
                "nothing to render a debug subtitle from."
            )
        if final and not enabled:
            problems.append(
                f"alignment.engines.{name}.final is true but "
                f"alignment.engines.{name}.enabled is false -- an engine "
                "that never runs can't be the authoritative one."
            )
        if final:
            final_engines.append(name)

    if not final_engines:
        problems.append(
            "none of alignment.engines.{" + ", ".join(ALIGNMENT_ENGINE_NAMES) + "}"
            ".final is true -- exactly one must be, so Step 4b/5 know "
            "which transcript to flag and mute words against."
        )
    elif len(final_engines) > 1:
        problems.append(
            "more than one alignment.engines.*.final is true "
            f"({', '.join(final_engines)}) -- exactly one must be. Pick one "
            "engine to be authoritative; the others can still run with "
            "debug_subtitle: true for comparison."
        )

    if problems:
        raise ConfigError(
            f"config.yaml has {len(problems)} alignment.engines problem(s):\n  "
            + "\n  ".join(problems)
        )


# ── Job state ─────────────────────────────────────────────────────────────────

def compute_job_id(video_path: Path) -> str:
    """
    Stable, content-independent job identifier: sha256[:12] of
    (absolute_path + ':' + mtime).

    Same path + mtime → same job_id → existing artifacts can be reused.
    File changes (new mtime) → new job_id → fresh job directory.

    Lives in utils.py (moved from pipeline.py) rather than there so it can
    be imported on its own -- e.g. by batch_plan.py, to check a candidate
    file's local job history -- without pulling in pipeline.py's own
    top-level imports of every steps/ module (whisperx, torch, demucs),
    which a lightweight planning pass has no need to load at all.
    """
    key = f"{video_path.resolve()}:{video_path.stat().st_mtime}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def write_job(job_dir: Path, state: dict[str, Any]) -> None:
    """
    Atomically overwrite job.json (write-then-rename).
    Safe against crashes mid-write — the old file is never partially overwritten.
    """
    tmp = job_dir / "job.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(job_dir / "job.json")


def read_job(job_dir: Path) -> dict[str, Any]:
    """Read and return job.json, or {} if the file does not exist."""
    p = job_dir / "job.json"
    return json.loads(p.read_text()) if p.exists() else {}


def find_job_dir(
    jobs_dir: Path,
    job_id: str,
    log: Optional[logging.LoggerAdapter] = None,
) -> "Path | None":
    """
    Scan JOBS_DIR for a subdirectory whose job.json contains a matching job_id.

    Job directories now carry human-readable names (YYYYMMDD_HHMMSS_slug_hex8)
    so the path can no longer be derived directly from the hash — we scan
    instead.  In practice JOBS_DIR has at most tens of entries so this is
    negligible overhead.

    A job.json that can't be read or parsed is skipped -- but, unlike the
    original version of this function, *not silently*: if `log` is given,
    each skip is reported as a WARNING before scanning continues. This
    matters because a job.json that fails to parse and one that simply
    doesn't exist are otherwise indistinguishable to every caller of this
    function: both end up returning None, which main() in pipeline.py
    treats as "no prior job — start a fresh one," running Steps 1a-7 from
    scratch with zero indication that an existing job was actually sitting
    right there, just unreadable. The motivating case: a person hand-edits
    steps_completed (e.g. to remove "7_mux" and force a re-test of a new
    muxer) and leaves a stray trailing comma behind — syntactically tiny,
    but it makes the file invalid JSON. Without this warning, the only
    visible symptom is a multi-hour full re-run with no explanation; with
    it, the very first lines of console output name the broken file and
    say why it didn't count as a match, in time to Ctrl-C before Step 1a
    even starts.

    Returns the directory Path on match, or None if no prior job exists.
    """
    if not jobs_dir.exists():
        return None
    for job_json in sorted(jobs_dir.glob("*/job.json")):
        try:
            state = json.loads(job_json.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            if log is not None:
                log.warning(
                    "Skipping unreadable job file while looking for "
                    "job_id=%s: %s (%s). If this is the job you expected "
                    "to resume, it was NOT matched -- fix the file by hand "
                    "(it must be valid JSON) and re-run, rather than "
                    "letting this fall through to a fresh job.",
                    job_id, job_json, exc,
                )
            continue
        if state.get("job_id") == job_id:
            return job_json.parent
    return None


def mark_step_done(job_dir: Path, step: str) -> None:
    """
    Append step to job.json steps_completed list (idempotent).
    Used by each step module on successful completion.
    """
    state = read_job(job_dir)
    done: list = state.setdefault("steps_completed", [])
    if step not in done:
        done.append(step)
    write_job(job_dir, state)


def unmark_step_done(job_dir: Path, step: str) -> None:
    """
    Remove step from job.json steps_completed list, if present (idempotent
    no-op if it's already absent).

    The inverse of mark_step_done — used by pipeline.py's correction mode
    (--skip-index / --add-interval / --redo-review) to force a step (and,
    by removing several, everything from that point onward) to actually
    re-run instead of hitting its own "already complete" resume-check.
    Also used by pipeline.py's --redo-step handling, which cascades this
    call from the named step through 7_mux for the same reason (see
    pipeline.py's module docstring and its _steps_from() helper).
    Does NOT delete or touch the step's output file on disk; it only
    clears the bookkeeping flag, so the step's normal logic runs fresh
    and naturally overwrites (-y) whatever was there before.
    """
    state = read_job(job_dir)
    done: list = state.setdefault("steps_completed", [])
    if step in done:
        done.remove(step)
    write_job(job_dir, state)


def mark_job_failed(job_dir: Path, step: str, exc: Exception) -> None:
    """Record failure details in job.json (status, step, error, traceback)."""
    state = read_job(job_dir)
    state["status"] = "failed"
    now = time.time()
    state["failed_at"] = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    state["failed_at_local"], _ = fmt_wall_clock(now)   # human convenience; failed_at above is canonical
    state["failure"] = {
        "step": step,
        "error": str(exc),
        "traceback": _traceback.format_exc(),
    }
    write_job(job_dir, state)


def mark_job_interrupted(job_dir: Path, step: str) -> None:
    """
    Record that the job was deliberately stopped (Ctrl-C / SIGINT) while
    `step` was in progress -- pipeline.py's counterpart to
    mark_job_failed() above, for a stop that wasn't an error.

    Distinguishing this from a plain 'running' status matters: without
    it, a job sitting mid-Ctrl-C looks identical in job.json to one still
    genuinely executing in another terminal or tmux pane -- nothing
    tells "safe to resume this" apart from "don't, something else
    already has it." It's kept separate from 'failed' too: an
    interruption is an expected, intentional stop, not something to
    investigate, so it gets its own status and its own small metadata
    block rather than overloading 'failure' -- pipeline.py's "Resuming"
    branch clears a stale record of either kind on the next attempt (the
    same way it already cleared a stale 'failure' block).

    `step` is deliberately never added to steps_completed here -- it was,
    by definition, still in progress when the interrupt landed, so the
    next run's resume logic redoes it from scratch, exactly as if it had
    never started.
    """
    state = read_job(job_dir)
    state["status"] = "interrupted"
    now = time.time()
    state["interrupted_at"] = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    state["interrupted_at_local"], _ = fmt_wall_clock(now)
    state["interruption"] = {"step": step}
    write_job(job_dir, state)


# ── Subprocess helper ─────────────────────────────────────────────────────────

# Matches tqdm's default bar format: "  45%|████...| 107.9/239.85 [...]".
# Shared by run_cmd (to keep progress-bar noise out of the "err" debug tag —
# stderr is just tqdm's conventional output stream, not a sign of trouble)
# and by steps/separate.py (to parse the percentage for heartbeat progress).
TQDM_PROGRESS_RE = re.compile(r"^\s*(\d{1,3})%\|")


def run_cmd(
    cmd: list,
    log: logging.LoggerAdapter,
    *,
    heartbeat_sec: float = 0,
    heartbeat_msg: Optional[Callable[[float], str]] = None,
    on_line: Optional[Callable[[str, str], None]] = None,
    ok_exit_codes: frozenset = frozenset({0}),
) -> subprocess.CompletedProcess:
    """
    Run a subprocess, streaming stdout/stderr line-by-line as they're produced.

    At DEBUG log level:
      - Logs the exact command line with shell-safe quoting
      - Logs every non-empty line of stdout/stderr as it arrives (not
        buffered until exit — each line carries its own real timestamp)

    heartbeat_sec > 0:
      Emits an INFO-level line at that interval for as long as the command
      runs, regardless of log level.  Use this for long unattended
      *subprocess* steps (demucs is the current example) so a run isn't
      silent for hours at the default INFO level — without promoting the
      tool's own (often very noisy / \\r-based) output to INFO.  WhisperX
      (steps/transcribe.py) is called in-process via its Python API rather
      than as a subprocess, so it doesn't go through run_cmd at all.

    heartbeat_msg(elapsed_sec) -> str:
      Optional. Called each time the heartbeat fires; its return value is
      logged instead of the generic "... still running (Ns elapsed)" line.
      Lets a caller report rich, tool-specific progress (e.g. parsed from
      on_line callbacks) instead of a bare liveness ping.

    on_line(stream_tag, line):
      Optional. Called for every line from either stream as it arrives —
      stream_tag is "out" or "err" — independent of DEBUG logging and
      independent of heartbeat_sec.  Lets a caller maintain its own parsed
      progress state (e.g. tqdm percentages) in real time, for use by
      heartbeat_msg or for any other purpose.  Exceptions raised inside
      on_line are caught and logged at DEBUG rather than crashing the
      reader thread, since a parsing bug shouldn't take down the actual
      subprocess being supervised.

    ok_exit_codes:
      Exit codes treated as success rather than failure. Defaults to {0}
      -- the universal convention every tool in this pipeline follows
      except one: mkvmerge (steps/mux.py, Step 7) uses 0 for a clean run,
      1 for "completed successfully but issued at least one warning"
      (e.g. a track whose metadata it couldn't fully determine, muxed
      correctly regardless), and 2 for an actual failure. Without this,
      a perfectly fine mkvmerge run that happened to warn about something
      would be indistinguishable from a real failure to every caller here
      — steps/mux.py passes ok_exit_codes={0, 1} specifically to restore
      that distinction.

    On an exit code outside ok_exit_codes: raises RuntimeError with the
    command and the tail of combined output. The exception message is
    kept concise; full output was already streamed at DEBUG level as it
    happened.

    The caller can access result.stdout when the command produces parseable
    output (e.g. ffprobe -of json) — captured in full regardless of log level.
    """
    log.debug("$ %s", " ".join(shlex.quote(str(c)) for c in cmd))

    proc = subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,             # line-buffered
    )

    out_lines: list[str] = []
    err_lines: list[str] = []
    debug_on = log.isEnabledFor(logging.DEBUG)
    start    = time.monotonic()
    next_beat = start + heartbeat_sec if heartbeat_sec > 0 else None

    # Two reader threads so stdout and stderr are each drained continuously —
    # a single-stream approach risks deadlock if one pipe fills while we're
    # blocked reading the other.
    #
    # Note on thread safety: on_line is invoked from these reader threads,
    # while heartbeat_msg (below) is invoked from the main thread's polling
    # loop.  If a caller's on_line mutates plain attributes (ints, strings)
    # that heartbeat_msg later reads, the GIL makes each individual
    # read/write atomic, so there's no risk of corruption — at worst a
    # heartbeat reads a value that's one line out of date, which is
    # cosmetically harmless for a progress display.  A caller combining
    # multiple fields into one invariant should use its own lock.
    def _reader(stream, sink: list[str], tag: str) -> None:
        for raw_line in stream:
            line = raw_line.rstrip("\n")
            sink.append(line)
            if on_line is not None:
                try:
                    on_line(tag, line)
                except Exception as exc:
                    log.debug("  on_line callback raised %r on line: %s", exc, line)
            if debug_on and line.strip():
                # tqdm and similar tools write progress bars to stderr purely
                # by convention (keeps stdout clean for piping) — that's not
                # an error, so don't tag it "err" alongside lines that
                # genuinely might be (tracebacks, real error messages).
                display_tag = "bar" if tag == "err" and TQDM_PROGRESS_RE.match(line) else tag
                log.debug("  %s: %s", display_tag, line)
        stream.close()

    t_out = threading.Thread(target=_reader, args=(proc.stdout, out_lines, "out"), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, err_lines, "err"), daemon=True)
    t_out.start()
    t_err.start()

    # Poll for exit so we can interleave heartbeat emission without
    # blocking on the reader threads (which run independently above).
    try:
        while proc.poll() is None:
            if next_beat is not None and time.monotonic() >= next_beat:
                elapsed = time.monotonic() - start
                if heartbeat_msg is not None:
                    try:
                        msg = heartbeat_msg(elapsed)
                    except Exception as exc:
                        log.debug("  heartbeat_msg callback raised %r", exc)
                        msg = f"... still running ({fmt_duration(elapsed)} elapsed)"
                else:
                    msg = f"... still running ({fmt_duration(elapsed)} elapsed)"
                log.info("  %s", msg)
                next_beat += heartbeat_sec
            time.sleep(0.5)
    except KeyboardInterrupt:
        # Ctrl-C at an attached terminal delivers SIGINT to this whole
        # process group, so proc has very likely already received it
        # directly and is on its way out. But this call site doesn't get
        # to assume that: if it's ever reached somewhere that signal
        # isn't forwarded to children, terminate proc explicitly rather
        # than abandoning it to finish (or fail to) unsupervised after
        # this function has already unwound -- best-effort, a short grace
        # period for a clean exit, then an unconditional kill.
        log.warning("  Interrupted — terminating subprocess (pid %d) ...", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("  Subprocess did not exit within 5s — killing (pid %d).", proc.pid)
            proc.kill()
            proc.wait()
        raise

    t_out.join()
    t_err.join()

    result = subprocess.CompletedProcess(
        cmd, proc.returncode,
        stdout="\n".join(out_lines),
        stderr="\n".join(err_lines),
    )

    if result.returncode not in ok_exit_codes:
        raw = (result.stderr + result.stdout).strip()
        tail = ("…" + raw[-600:]) if len(raw) > 600 else raw
        raise RuntimeError(
            f"Command failed (exit {result.returncode})\n"
            f"  cmd: {' '.join(shlex.quote(str(c)) for c in cmd)}\n"
            f"  out: {tail or '(no output)'}"
        )

    return result


# ── Resumable-output helpers ──────────────────────────────────────────────────
#
# Every subprocess invocation in this pipeline whose output is later trusted
# via `<path>.exists()` on resume needs two things: the write itself must be
# atomic (so a process killed mid-write can never leave a truncated file
# sitting under the name resume logic looks for), and the result should be
# validated against what it was expected to contain (so resume logic doesn't
# have to take "it exists" on faith even when the write *was* atomic --
# e.g. a job directory populated before this pair of functions existed).
#
# tmp_output_path()/finalize_output() below are the first half of that;
# probe_duration_sec()/check_duration_matches() are the second. Together they
# would have caught job 1d55099e2bb7's audio_raw.mp3: Step 1a's
# `ffmpeg -c:a copy` was interrupted 7:51 into a source whose own audio stream
# reports roughly 100 minutes, and the truncated result was later treated as
# a finished extraction purely because it existed at all under the expected
# filename (see steps/extract.py, which was the one caller not yet using
# either mechanism).

def tmp_output_path(final_path: Path) -> Path:
    """
    Temp sibling path a subprocess should write to before being published
    under final_path via finalize_output().

    Inserts '.part' before the real extension (audio_raw.mp3 becomes
    audio_raw.part.mp3) rather than appending it (audio_raw.mp3.part).
    ffmpeg -- and most other media tools -- infer their output container
    format from the filename's own extension, so a temp path needs to
    keep a recognizable one as its actual suffix, or the write fails
    outright before there's anything to even worry about renaming
    ("Unable to choose an output format for '...audio_raw.mp3.part'").

    Always placed alongside final_path -- same directory, so
    finalize_output()'s rename is guaranteed same-filesystem -- with a
    name that can't collide with any real pipeline filename and sorts
    visibly next to its target in a directory listing. Callers should
    remove any leftover file at this path (from a previous interrupted
    attempt) before starting a fresh one, e.g.:

        tmp = tmp_output_path(out_path)
        tmp.unlink(missing_ok=True)
        run_cmd([..., str(tmp)], log)
        finalize_output(tmp, out_path)
    """
    return final_path.with_name(final_path.stem + ".part" + final_path.suffix)


def finalize_output(tmp_path: Path, final_path: Path) -> None:
    """
    Atomically publish a subprocess's completed output under its final
    name -- the write-then-rename counterpart to tmp_output_path() above,
    the same idiom write_job() already uses for job.json.

    os.replace() rather than Path.rename(): both are atomic on POSIX when
    source and destination share a filesystem (guaranteed here --
    tmp_output_path() always places the temp file next to its target),
    but os.replace() also overwrites atomically on Windows, where
    Path.rename() raises FileExistsError instead of replacing. Not
    load-bearing for this project's Linux-only container today, but free
    to get right.
    """
    os.replace(tmp_path, final_path)


def probe_duration_sec(path: Path, log: logging.LoggerAdapter) -> float:
    """
    Return the duration of path's first audio stream in seconds, measured
    by actually reading the bitstream through to its end -- stream-copied
    into the null muxer, so this costs a fast demux pass, not a real
    decode -- rather than trusting the container's own self-reported
    duration metadata.

    That distinction is not theoretical: some formats embed a header
    that declares a duration up front, specifically to let a player seek
    without scanning the whole file first (MP3's Xing/LAME VBR header
    and FLAC's STREAMINFO block both do this), and a demuxer that trusts
    it outright keeps reporting the *original* duration even after the
    file's tail has been chopped off by an interrupted write. Confirmed
    by testing against this exact fix: an MP3 and a FLAC file, each
    truncated to a fifth of their size, both still reported their full,
    pre-truncation duration from a plain `ffprobe -show_entries
    format=duration` -- silently defeating the entire integrity check
    this function exists to support. (AAC and AC3 happened not to
    exhibit this in the same test, but nothing about that generalizes to
    every codec steps/extract.py might encounter, so the robust method
    is used unconditionally rather than per-codec.) WAV -- Steps
    1b/1c/3b's format throughout the rest of the pipeline -- computes
    duration from actual data-chunk bytes present and reflected
    truncation correctly in the same test, but is measured the same way
    here regardless, both for consistency and because actually reading
    the file through is a fast demux either way.

    Raises RuntimeError if ffmpeg can't read the file at all (rather
    than returning 0.0), so a caller comparing against an expected value
    never mistakes "unreadable" for "empty."
    """
    try:
        result = run_cmd(
            [
                "ffmpeg", "-v", "error",
                "-i", str(path),
                "-map", "0:a:0",
                "-c", "copy",
                "-f", "null",
                "-progress", "pipe:1",
                "-nostats",
                "-",
            ],
            log,
        )
    except RuntimeError as exc:
        raise RuntimeError(f"Could not read {path} to determine its duration: {exc}") from None

    out_time_us = None
    for line in result.stdout.splitlines():
        if line.startswith("out_time_us="):
            value = line.split("=", 1)[1].strip()
            if value not in ("", "N/A"):
                out_time_us = int(value)

    if out_time_us is None:
        raise RuntimeError(
            f"ffmpeg could not determine a duration for {path} -- it may "
            "be empty, corrupt, or an unsupported format."
        )
    return out_time_us / 1_000_000


def check_duration_matches(
    actual_sec: float,
    expected_sec: float,
    *,
    label: str,
    tolerance_sec: float = 5.0,
    log: Optional[logging.LoggerAdapter] = None,
) -> None:
    """
    Raise RuntimeError if actual_sec doesn't match expected_sec within
    tolerance_sec -- the shared compare-and-raise half of this pipeline's
    duration integrity checks. Takes already-measured durations rather
    than probing internally, so each caller is free to choose whichever
    ffprobe strategy suits its own file type (probe_duration_sec() above
    for most things; steps/segment.py's own _probe_duration() for WAV)
    and to log around the call however fits its own step's style.

    tolerance_sec is a flat, absolute value rather than a percentage of
    expected_sec: a lossless bitstream copy, PCM downmix, split, or
    concat should reproduce its source's duration to within a small,
    constant margin (container-level timestamp rounding, slightly
    different stream start references) regardless of how long the
    source itself is -- there's no mechanism by which that margin would
    legitimately grow proportionally with duration the way, say,
    frame-rate drift over a long recording might. A truncation caused by
    an interrupted run is typically tens of seconds to hours short, so
    even a fairly tight flat tolerance has enormous margin against a
    real corruption while comfortably tolerating benign container
    quirks in a genuinely intact file.

    label identifies what's being compared, purely for the error message
    (e.g. "audio_raw.mp3 vs. source video") -- this function has no
    other use for it.
    """
    delta = abs(actual_sec - expected_sec)
    if delta > tolerance_sec:
        raise RuntimeError(
            f"Integrity check failed for {label}:\n"
            f"  measured   : {fmt_duration(actual_sec)}  ({actual_sec:.1f}s)\n"
            f"  expected   : {fmt_duration(expected_sec)}  ({expected_sec:.1f}s)\n"
            f"  difference : {fmt_duration(delta)}  ({delta:.1f}s, tolerance {tolerance_sec:.0f}s)\n"
            "This usually means the file was left truncated by a previous "
            "run interrupted mid-write (Ctrl-C, OOM-kill, host shutdown, "
            "etc.)."
        )
    if log is not None:
        log.debug(
            "  ✓  duration check passed for %s (%.1fs vs expected %.1fs, "
            "Δ%.1fs ≤ tolerance %.1fs)",
            label, actual_sec, expected_sec, delta, tolerance_sec,
        )


def sha256_file(
    path: Path,
    log: Optional[logging.LoggerAdapter] = None,
    chunk_size: int = 1024 * 1024,
) -> Optional[str]:
    """
    Return path's SHA-256 hex digest, reading it in fixed-size chunks so
    this works for large files without loading them fully into memory.

    Best-effort and provenance-only: returns None (and logs a warning, if
    a logger is given) rather than raising on an I/O error, since this is
    an audit aid for confirming "is this exactly the file we started
    from" later, not a correctness gate the way check_duration_matches()
    is -- a hash that fails to compute shouldn't be able to fail a step
    on its own.
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        if log is not None:
            log.warning("  Could not hash %s: %s", path, exc)
        return None


def verify_stem_before_reuse(
    path: Path,
    expected_duration_sec: Optional[float],
    expected_sha256: Optional[str],
    log: logging.LoggerAdapter,
    *,
    label: str,
    written_by: str = "Step 3b (merge)",
    regenerate_hint: Optional[str] = None,
) -> None:
    """
    Re-verify a long-lived stem file against the duration and hash the
    step that produced it recorded (via verify_and_hash_before_publish()
    below), immediately before the step that consumes it next actually
    does so. That consumption may happen much later and in an entirely
    separate invocation than the one that wrote it -- originally this
    was only true of dialog.wav/score_sfx.wav via pipeline.py's
    --skip-index/--add-interval/--redo-review correction workflow (see
    that module's docstring), but it is equally true of
    dialog_censored.wav/audio_censored.wav/audio_encoded.mka whenever
    --redo-step names a *middle* step (e.g. --redo-step 6_recombine
    alone, which cascades to 6b_encode/7_mux but leaves 5_mute's
    dialog_censored.wav exactly as it was) -- so this function is
    shared by steps/mute.py, steps/recombine.py, steps/encode.py, and
    steps/mux.py rather than being specific to any one pair of files.
    See the write-time half, verify_and_hash_before_publish(), below.

    written_by/regenerate_hint let each caller describe *its own*
    upstream step and remediation accurately in the raised error --
    defaults describe the original dialog.wav/score_sfx.wav case, where
    the fix genuinely does require re-running Step 2's Demucs
    separation from scratch. That's specifically NOT true of the other
    three files (regenerating any of them is exactly what --redo-step
    is for), so their callers pass a written_by/regenerate_hint that
    says so instead of repeating guidance that would be wrong for them.

    Deliberately does NOT delete path or unmark any step on a mismatch,
    unlike every other integrity check in this pipeline (including this
    function's own write-time counterpart). Silently deleting a
    multi-hour artifact like dialog.wav and triggering its own
    regeneration off the back of one failed check would be a far bigger,
    more surprising action than anything else this pipeline does on its
    own -- so this raises with clear, actionable guidance instead, and
    leaves the job directory exactly as it found it for a person to
    decide what to do next. This holds even for the three cheaper-to-
    regenerate files: --redo-step is a deliberate, explicit action a
    person takes, not something this check should trigger on their
    behalf.

    Skips whichever half of the check it doesn't have data for -- no
    recorded duration/hash (a job directory from before this check
    existed) just means less confidence, not a hard block over data that
    predates the feature that would have produced it.
    """
    problems: list[str] = []

    if expected_duration_sec:
        actual_duration = probe_duration_sec(path, log)
        delta = abs(actual_duration - expected_duration_sec)
        if delta > 5.0:
            problems.append(
                f"duration is {fmt_duration(actual_duration)} ({actual_duration:.1f}s), "
                f"expected {fmt_duration(expected_duration_sec)} ({expected_duration_sec:.1f}s) "
                f"— Δ{delta:.1f}s"
            )

    if expected_sha256:
        actual_hash = sha256_file(path, log)
        if actual_hash is not None and actual_hash != expected_sha256:
            problems.append(f"sha256 is {actual_hash[:16]}…, expected {expected_sha256[:16]}…")

    if problems:
        if regenerate_hint is None:
            regenerate_hint = (
                "dialog.wav and score_sfx.wav are kept specifically to avoid "
                "re-running Step 2's Demucs separation, so this is deliberately "
                "not auto-corrected -- redoing that work automatically, "
                "unasked, over a single failed check is a bigger action than "
                "this pipeline should take on its own. There is currently no "
                "supported way to redo just Steps 2/3b (pipeline.py's "
                "--redo-step explicitly excludes them); if this file is "
                "genuinely bad, the safe fix is to delete the job directory "
                "and re-run from scratch."
            )
        raise RuntimeError(
            f"Integrity check failed for {label} ({path}):\n"
            + "\n".join(f"  - {p}" for p in problems) + "\n"
            f"This file has changed since {written_by} wrote it -- "
            "possibly corruption, possibly something else touched it. "
            + regenerate_hint
        )

    log.debug("  ✓  %s passed integrity check (duration + hash vs. %s's record).", label, written_by)


def verify_and_hash_before_publish(
    path: Path,
    label: str,
    expected_duration_sec: float,
    log: logging.LoggerAdapter,
) -> Optional[str]:
    """
    Confirm path matches expected_duration_sec, then return its SHA-256
    hex digest -- the write-time half of this pipeline's stem-integrity
    checks; verify_stem_before_reuse() above is the read-time half. Used
    by steps/merge.py (dialog.wav/score_sfx.wav), steps/mute.py
    (dialog_censored.wav), steps/recombine.py (audio_censored.wav), and
    steps/encode.py (audio_encoded.mka) -- each records what "good"
    looks like for whichever step consumes its output next, which may
    happen much later and in a separate invocation.

    expected_duration_sec of 0 (state had no recorded total_duration_sec
    at all -- shouldn't happen in practice, but see steps/extract.py's
    analogous "skip gracefully rather than block a run" handling) skips
    the duration half and only hashes.

    On a duration mismatch: deletes path and re-raises, the same
    delete-then-raise pattern used throughout this pipeline's other
    integrity checks, so a redo doesn't get stuck re-validating the same
    bad file. Hashing failures are not fatal at all -- see sha256_file()'s
    own docstring.
    """
    if expected_duration_sec:
        try:
            check_duration_matches(
                probe_duration_sec(path, log), expected_duration_sec, log=log,
                label=f"{label} vs. recorded total_duration_sec",
                tolerance_sec=5.0,
            )
        except RuntimeError:
            path.unlink(missing_ok=True)
            log.error("  Deleted incomplete %s — re-run to produce it fresh.", label)
            raise
    return sha256_file(path, log)


# ── Wall-clock timestamps (local + UTC) ─────────────────────────────────────────

def fmt_wall_clock(epoch: Optional[float] = None) -> tuple[str, str]:
    """
    Return (local_str, utc_str) describing one moment in time -- local in
    LOCAL_TZ (see timezone resolution, top of this module) and explicitly
    in UTC alongside, each self-labelled with its own offset/suffix, e.g.:
      ("2026-06-25 16:36:38 -0700", "2026-06-25 23:36:38 UTC")

    epoch defaults to now (time.time()). Used wherever a timestamp is
    worth showing both ways at once -- the pipeline's startup/completion
    banners and job.json's started_at/failed_at/completed_at companions
    (see mark_job_failed() below and pipeline.py) -- so a reader can
    correlate against another UTC-based record (or just sanity-check the
    two against each other) without doing the arithmetic themselves.
    Every *per-line* log timestamp (_StepFormatter above) only shows the
    local form -- it's already self-labelled with its own offset, and
    showing both on every line would be noise at DEBUG-level subprocess
    output volumes.
    """
    ts = epoch if epoch is not None else time.time()
    local_str = datetime.fromtimestamp(ts, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %z")
    utc_str   = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return local_str, utc_str


def timezone_banner() -> str:
    """
    One- or two-line, human-readable summary of which timezone log
    timestamps are using -- meant to be logged once, at startup, at INFO
    level. Same motivation as retention_summary() above: the actually-
    resolved behaviour should be visible in the first few lines of
    output, not discoverable only after noticing every timestamp in an
    overnight run looks several hours off from the wall clock.
    """
    local_str, utc_str = fmt_wall_clock()
    if LOCAL_TZ_EXPLICIT:
        return f"Timezone    : local {local_str}  (UTC {utc_str.removesuffix(' UTC')})"
    return (
        f"Timezone    : AC_TZ_OFFSET not set — timestamps below are UTC ({utc_str}).\n"
        f"              Run via hush.sh (auto-detects the host's offset) or set "
        f"AC_TZ_OFFSET yourself (e.g. AC_TZ_OFFSET=-0700) for local wall-clock times."
    )


def parse_iso_to_epoch(value: str) -> float:
    """
    Parse an isoformat() string -- as written by this module's own
    datetime.now(tz=timezone.utc).isoformat() calls (job.json's
    started_at/failed_at/completed_at) -- back to a Unix epoch float.

    Used to redisplay a job's recorded timestamp in *this* run's
    resolved LOCAL_TZ via fmt_wall_clock(), which may differ from
    whatever AC_TZ_OFFSET (if any) was in effect when the job was
    originally created -- e.g. a job started under one AC_TZ_OFFSET and
    resumed days later under another, or under none at all.
    """
    return datetime.fromisoformat(value).timestamp()


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_duration(seconds: float) -> str:
    """
    Format a duration in seconds as HH:MM:SS.

    Examples:
      0       → '00:00:00'
      90      → '00:01:30'
      3661    → '01:01:01'
      7389.9  → '02:03:09'
    """
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def fmt_timestamp(seconds: float) -> str:
    """
    Format a position in seconds as a media-player-friendly timestamp:
    H:MM:SS.mmm -- hours unpadded (no leading zero, but always present,
    even for sub-hour positions: "0:23:14.800" not "23:14.800"), minutes
    and seconds zero-padded to two digits, milliseconds zero-padded to
    three. This is the format shown in matches.json/review.json/
    censor_log.json's *_hms companion fields (alongside the canonical
    float-seconds value, which stays machine-readable for arithmetic) and
    in steps/review.py's interactive review prompts.

    Deliberately not "HH:MM:SS" (hours zero-padded to two digits): the
    motivating examples (e.g. "1:28:00.267") are unpadded, and most media
    players' own seek bars/goto-time fields render the same way. Always
    keeping the hour field present (even at 0) -- rather than dropping it
    for sub-hour positions, the way an on-screen player clock often does --
    keeps every timestamp in a given file the same shape regardless of
    where in the film it falls, which matters more here than it does on a
    player's live, single-position display.

    Computed via integer milliseconds (not repeated float division) so
    carries (e.g. 59.9996s -> 1:00:00.000, not 0:59:100.000 or a
    floating-point-rounding-induced "60" in the seconds field) are exact.

    Round-trips through parse_timestamp() below: fmt_timestamp(parse_timestamp(s))
    reproduces the same string for any s already in this format.

    Examples:
      0.0      -> '0:00:00.000'
      75.5     -> '0:01:15.500'
      5280.267 -> '1:28:00.267'
    """
    total_ms = int(round(max(0.0, seconds) * 1000))
    ms, total_s = total_ms % 1000, total_ms // 1000
    s,  total_m = total_s % 60,    total_s // 60
    m,  h       = total_m % 60,    total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


def parse_timestamp(raw: str) -> Optional[float]:
    """
    Parse a human-entered time string into seconds (float). Accepts:
      - raw seconds:                  "1203.14", "90"
      - "M:SS[.mmm]" / "MM:SS[.mmm]"   (no hour field)
      - "H:MM:SS[.mmm]" / "HH:MM:SS[.mmm]"

    This is the input-side counterpart to fmt_timestamp() above -- it
    accepts that function's own output verbatim (so a timestamp copied
    out of matches.json/review.json/censor_log.json's *_hms fields can be
    pasted straight back in), as well as the zero-padded "HH:" two-digit-
    hour style most media players' own goto-time dialogs use, and bare
    seconds for scripted/programmatic callers. Leading/trailing whitespace
    is stripped; the hour field, if present, may be any number of digits
    (not just one or two), since a long-enough file would need it.

    Used by steps/review.py's interactive manual-entry fallback (the "A"
    action when a typed word/phrase isn't found in the transcript) and by
    pipeline.py's --add-interval START/END arguments (via
    steps/review.py's apply_corrections()) -- both accept either notation
    interchangeably.

    Returns None if the string is empty, malformed, or negative -- callers
    are expected to report that back to whoever typed it rather than
    silently substituting a default.
    """
    s = raw.strip()
    if not s:
        return None

    if ":" not in s:
        try:
            value = float(s)
        except ValueError:
            return None
        return value if value >= 0 else None

    parts = s.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None

    if len(nums) == 2:
        h, m, sec = 0.0, nums[0], nums[1]
    else:
        h, m, sec = nums
    return h * 3600 + m * 60 + sec


def fmt_size(path: Path) -> str:
    """
    Return a human-readable file size for the given path.

    Examples: '450.0 MB', '1.2 GB', '312 B'
    """
    n = float(path.stat().st_size)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_dir(path: "str | Path") -> str:
    """
    Format a directory as a display string with exactly one trailing
    slash, regardless of whether the input already had one.

    Used for job.json's input_path/mux.output_path (and the startup
    paths_banner() that mirrors them) so a directory reads as
    unambiguously a directory -- not a file path missing its filename --
    at a glance, whether it came from a host env var (AC_INPUT_HOST_DIR/
    AC_OUTPUT_HOST_DIR, which may or may not include a trailing slash
    depending on how it was set) or a container Path object (whose
    str() form never does).
    """
    return str(path).rstrip("/") + "/"

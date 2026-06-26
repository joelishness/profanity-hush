"""
profanity-hush — shared utilities

Imported by pipeline.py and every steps/ module.
"""
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


def setup_logging(level_name: str) -> logging.Logger:
    """Configure and return the root 'hush' logger."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_StepFormatter())
    logger = logging.getLogger("hush")
    logger.setLevel(level)
    if not logger.handlers:
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def step_logger(name: str) -> logging.LoggerAdapter:
    """Return a LoggerAdapter that tags every message with the given step name."""
    return logging.LoggerAdapter(logging.getLogger("hush"), {"step": name})


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(config_path: "str | Path") -> dict[str, Any]:
    """
    Load config.yaml and apply environment variable overrides.

    Override precedence (highest wins):
      environment variables > config.yaml values > pipeline built-in defaults

    Env vars applied:
      AC_LOG_LEVEL                  → output.log_level
      AC_KEEP_INTERMEDIATES         → output.keep_intermediates          (1 = True)
      AC_KEEP_CORRECTION_ARTIFACTS  → output.keep_correction_artifacts   (1 = True, 0 = False --
                                       this one defaults to True, so unlike the others, explicitly
                                       turning it *off* needs its own value, not just absence)
      AC_INTERACTIVE                → interactive.enabled                (1 = True)
      AC_SEGMENT_SIZE               → audio.segment_size_sec             (seconds, int)

    Returns an empty dict if the config file is absent — the pipeline uses
    its own defaults in that case (same behaviour as config/config.yaml defaults).
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None

    path = Path(config_path)
    if path.exists() and yaml is not None:
        with path.open() as f:
            cfg: dict[str, Any] = yaml.safe_load(f) or {}
    else:
        cfg = {}

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

    return cfg


def keep_intermediate(cfg: dict, *, correction_artifact: bool = False) -> bool:
    """
    Whether a large intermediate WAV file should be KEPT on disk (not
    deleted) once the step that produced it is no longer the bottleneck.

    Single source of truth for the retention policy described in design
    doc §6: every step that deletes a large intermediate (steps/merge.py,
    steps/mute.py, steps/recombine.py, steps/mux.py) calls this rather
    than reading output.keep_intermediates / output.keep_correction_artifacts
    directly, specifically so the policy can never drift between steps the
    way it could when each one re-implemented its own condition.

    correction_artifact=False (default): the file is fully superseded once
      consumed downstream, and at best only cheaply regenerable anyway
      (per-segment stems, audio_stereo*.wav, dialog_censored.wav,
      audio_censored.wav) -- kept only if output.keep_intermediates is true.

    correction_artifact=True: the file is one of the two artifacts
      (dialog.wav, score_sfx.wav) that make the --skip-index / --add-interval
      / --redo-review correction workflow (design doc §13.4) possible
      without re-running Step 2's Demucs separation -- kept if EITHER
      output.keep_intermediates OR output.keep_correction_artifacts
      (default true) is true.
    """
    keep_intermediates = bool(cfg_get(cfg, "output", "keep_intermediates", default=False))
    if not correction_artifact:
        return keep_intermediates
    keep_correction = bool(cfg_get(cfg, "output", "keep_correction_artifacts", default=True))
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
    ki = bool(cfg_get(cfg, "output", "keep_intermediates", default=False))
    kc = bool(cfg_get(cfg, "output", "keep_correction_artifacts", default=True))
    return (
        f"Retention   : keep_intermediates={ki}  keep_correction_artifacts={kc}\n"
        f"  transcript*.json, matches.json, review.json, censor_log.json : always kept\n"
        f"  dialog.wav, score_sfx.wav                                    : "
        f"{'kept' if (ki or kc) else 'deleted after use'}\n"
        f"  audio_stereo*.wav, dialog_censored.wav, audio_censored.wav   : "
        f"{'kept' if ki else 'deleted after use'}"
    )


def cfg_get(cfg: dict, *keys: str, default: Any = None) -> Any:
    """
    Safely navigate nested config keys.
    Returns default if any key is missing or the value is None.

    Example:
      cfg_get(cfg, "demucs", "shifts", default=1)
    """
    node: Any = cfg
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k)
        if node is None:
            return default
    return node


# ── Job state ─────────────────────────────────────────────────────────────────

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


def find_job_dir(jobs_dir: Path, job_id: str) -> "Path | None":
    """
    Scan JOBS_DIR for a subdirectory whose job.json contains a matching job_id.

    Job directories now carry human-readable names (YYYYMMDD_HHMMSS_slug_hex8)
    so the path can no longer be derived directly from the hash — we scan
    instead.  In practice JOBS_DIR has at most tens of entries so this is
    negligible overhead.

    Returns the directory Path on match, or None if no prior job exists.
    """
    if not jobs_dir.exists():
        return None
    for job_json in sorted(jobs_dir.glob("*/job.json")):
        try:
            state = json.loads(job_json.read_text())
            if state.get("job_id") == job_id:
                return job_json.parent
        except (OSError, json.JSONDecodeError):
            continue
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

    On non-zero exit: raises RuntimeError with the command and the tail of
    combined output.  The exception message is kept concise; full output
    was already streamed at DEBUG level as it happened.

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

    t_out.join()
    t_err.join()

    result = subprocess.CompletedProcess(
        cmd, proc.returncode,
        stdout="\n".join(out_lines),
        stderr="\n".join(err_lines),
    )

    if result.returncode != 0:
        raw = (result.stderr + result.stdout).strip()
        tail = ("…" + raw[-600:]) if len(raw) > 600 else raw
        raise RuntimeError(
            f"Command failed (exit {result.returncode})\n"
            f"  cmd: {' '.join(shlex.quote(str(c)) for c in cmd)}\n"
            f"  out: {tail or '(no output)'}"
        )

    return result


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

"""
profanity-hush — shared utilities

Imported by pipeline.py and every steps/ module.
"""
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
import traceback as _traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


# ── Logging ───────────────────────────────────────────────────────────────────

class _StepFormatter(logging.Formatter):
    """
    Produces lines like:
      2024-01-15 22:30:01 [INFO ] [extract  ] Probing audio stream — movie.mkv
    """
    _LABELS = {
        logging.DEBUG:   "DEBUG",
        logging.INFO:    "INFO ",
        logging.WARNING: "WARN ",
        logging.ERROR:   "ERROR",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
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
      AC_LOG_LEVEL           → output.log_level
      AC_KEEP_INTERMEDIATES  → output.keep_intermediates  (1 = True)
      AC_INTERACTIVE         → interactive.enabled        (1 = True)
      AC_SEGMENT_SIZE        → audio.segment_size_sec     (seconds, int)

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
    if os.environ.get("AC_INTERACTIVE") == "1":
        cfg.setdefault("interactive", {})["enabled"] = True
    if v := os.environ.get("AC_SEGMENT_SIZE"):
        cfg.setdefault("audio", {})["segment_size_sec"] = int(v)

    return cfg


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


def mark_job_failed(job_dir: Path, step: str, exc: Exception) -> None:
    """Record failure details in job.json (status, step, error, traceback)."""
    state = read_job(job_dir)
    state["status"] = "failed"
    state["failed_at"] = datetime.now(tz=timezone.utc).isoformat()
    state["failure"] = {
        "step": step,
        "error": str(exc),
        "traceback": _traceback.format_exc(),
    }
    write_job(job_dir, state)


# ── Subprocess helper ─────────────────────────────────────────────────────────

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
      runs, regardless of log level.  Use this for long unattended steps
      (demucs, whisperx) so a run isn't silent for hours at the default INFO
      level — without promoting the tool's own (often very noisy /
      \\r-based) output to INFO.

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
                log.debug("  %s: %s", tag, line)
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

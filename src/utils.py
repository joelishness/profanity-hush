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
import traceback as _traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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

def run_cmd(cmd: list, log: logging.LoggerAdapter) -> subprocess.CompletedProcess:
    """
    Run a subprocess, capturing stdout and stderr.

    At DEBUG log level:
      - Logs the exact command line with shell-safe quoting
      - Logs every non-empty line of captured stdout/stderr

    On non-zero exit: raises RuntimeError with the command and the tail of
    stderr (or stdout if stderr is empty).  The exception message is kept
    concise; the full output is available at DEBUG level.

    The caller can access result.stdout when the command produces parseable
    output (e.g. ffprobe -of json).
    """
    log.debug("$ %s", " ".join(shlex.quote(str(c)) for c in cmd))

    result = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if log.isEnabledFor(logging.DEBUG):
        for line in (result.stdout or "").splitlines():
            if line.strip():
                log.debug("  out: %s", line)
        for line in (result.stderr or "").splitlines():
            if line.strip():
                log.debug("  err: %s", line)

    if result.returncode != 0:
        raw = ((result.stderr or "") + (result.stdout or "")).strip()
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

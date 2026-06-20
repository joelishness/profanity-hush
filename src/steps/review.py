"""
profanity-hush — Step 4b: interactive terminal review

Presents every word-list match found in the transcript to a human for
approval/rejection, and lets them manually add words the automatic scan
missed. Only runs when interactive mode is active — pipeline.py decides
whether to call this step at all (see design doc §8: "skipped entirely in
unattended mode"); this module has no internal on/off switch of its own.

Input  : transcript.json (or, once Step 4 exists, transcript_aligned.json —
         same schema, so the caller can swap the path with no code change
         here), config/word_list.txt
Output : review.json — a *sparse* list of overrides, not a full transcript
         copy: a word-list match with no override is implicitly approved
         (the default outcome once a word matches the list is to mute it).
         Only entries the human changed need to be recorded:
           {"action": "skip", "word_index": 412, "text": "crap"}
           {"action": "add",  "word_index": 913, "text": "bastard",
            "start": 1203.14, "end": 1203.48}
         "word_index" on an "add" entry is informational (it records which
         transcript word the addition was resolved to, when the user found
         one by searching); a manually-timed add with no matching transcript
         word has word_index: null and only start/end are authoritative.

Step 5 (mute.py, not yet implemented) is expected to: run the same
find_matches() scan steps/matching.py provides, then apply these overrides
on top — dropping any word_index with action "skip", and adding a mute
interval for each "add" entry's start/end.

Terminal UI (design doc §8):
    [3 of 11]  Word: "crap"  |  Confidence: 0.94  |  Time: 00:23:14.8 – 00:23:15.1
    Context: "...and then he said crap right in front of..."
    Action? [Y]es / [N]o / [A]dd word / [S]kip rest / [Q]uit  >

  Y (default) — approve; word will be muted
  N           — reject; recorded as a "skip" override
  A           — search the transcript for a missed word/phrase to add, or
                enter exact start/end timestamps manually if not found
                (no audio playback in v1 — see design doc §13.4)
  S           — approve this and all remaining candidates without prompting
  Q           — abort the whole run; nothing is written, including
                review.json itself (raises ReviewAborted)

min_confidence_for_prompt (config): entries with confidence *above* this
threshold are auto-approved without ever being shown, and are not counted
toward the "approved" total in the final summary (tracked separately as
auto_approved) since no human reviewed them.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from utils import cfg_get, mark_step_done, read_job, step_logger, write_job
from steps.matching import Match, find_matches, load_word_list, resolve_word_list_path, strip_punct


class ReviewAborted(Exception):
    """Raised when the user presses Q. No output is written; caller must not
    treat this as a step failure — it's a deliberate, clean stop."""


def review(
    job_dir: Path,
    transcript_path: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 4b: interactively review word-list matches before muting.

    Returns the path to review.json. Raises ReviewAborted if the user quits
    mid-review (caller should treat this distinctly from a step failure).
    """
    if log is None:
        log = step_logger("review")

    state = read_job(job_dir)
    review_path = job_dir / "review.json"

    if "4b_review" in state.get("steps_completed", []):
        log.info("Step 4b — ↩  already complete; re-using %s.", review_path.name)
        if not review_path.exists():
            raise RuntimeError(
                f"Step 4b is marked complete but {review_path} is missing.  "
                "Delete the job directory and re-run from scratch."
            )
        return review_path

    word_list_path  = Path(cfg_get(cfg, "censoring", "word_list", default="/config/word_list.txt"))
    word_list_path  = resolve_word_list_path(word_list_path, log)
    show_context     = int(cfg_get(cfg, "interactive", "show_context_words", default=8))
    min_conf_prompt   = float(cfg_get(cfg, "interactive", "min_confidence_for_prompt", default=0.0))

    log.info("Step 4b — interactive review")
    log.info("  word list: %s", word_list_path)

    transcript_data = json.loads(transcript_path.read_text())
    words = transcript_data.get("words", [])

    entries = load_word_list(word_list_path, log)
    log.info("  Loaded %d word list entries.", len(entries))

    matches = find_matches(words, entries, log)
    log.info("  Found %d candidate match(es) in %d words.", len(matches), len(words))

    if not matches:
        log.info("  No matches found — nothing to review.")
        print()
        print("profanity-hush — interactive review")
        print("No word-list matches found in the transcript. Nothing to review.")
        print()
        summary = {"candidates": 0, "approved": 0, "rejected": 0, "added": 0, "auto_approved": 0}
        _write_review_json(review_path, [])
        state = read_job(job_dir)
        state["review"] = summary
        write_job(job_dir, state)
        mark_step_done(job_dir, "4b_review")
        return review_path

    overrides, summary = _run_review_loop(words, matches, show_context, min_conf_prompt, log)

    _write_review_json(review_path, overrides)
    state = read_job(job_dir)
    state["review"] = summary
    write_job(job_dir, state)
    mark_step_done(job_dir, "4b_review")

    log.info(
        "  ✓  Review complete: %d approved, %d rejected, %d added, %d auto-approved.",
        summary["approved"], summary["rejected"], summary["added"], summary["auto_approved"],
    )
    return review_path


# ── Review loop ────────────────────────────────────────────────────────────────

def _run_review_loop(
    words: list[dict],
    matches: list[Match],
    show_context: int,
    min_conf_prompt: float,
    log: logging.LoggerAdapter,
) -> tuple[list[dict], dict]:
    overrides: list[dict] = []
    approved = rejected = added = auto_approved = 0
    approve_rest = False
    total = len(matches)

    print()
    print(f"profanity-hush — interactive review ({total} candidate{'s' if total != 1 else ''} found)")
    print()

    for idx, m in enumerate(matches, start=1):
        if approve_rest:
            approved += 1
            continue

        if min_conf_prompt > 0.0 and m.score > min_conf_prompt:
            auto_approved += 1
            continue  # no override needed — default outcome is "muted"

        print(
            f'[{idx} of {total}]  Word: "{m.matched_text}"  |  '
            f'Confidence: {m.score:.2f}  |  '
            f'Time: {_fmt_time(m.start)} – {_fmt_time(m.end)}'
        )
        print(f"Context: {_context_str(words, m.word_index, m.span, show_context)}")

        while True:
            try:
                resp = input("Action? [Y]es / [N]o / [A]dd word / [S]kip rest / [Q]uit  > ").strip().lower()
            except EOFError:
                raise ReviewAborted("EOF while reading input")

            if resp in ("", "y"):
                approved += 1
                break
            elif resp == "n":
                rejected += 1
                overrides.append({
                    "action": "skip", "word_index": m.word_index, "text": m.matched_text,
                })
                break
            elif resp == "a":
                add_override = _prompt_add(words, log)
                if add_override is not None:
                    overrides.append(add_override)
                    added += 1
                continue  # re-show the action prompt for *this* candidate
            elif resp == "s":
                approve_rest = True
                approved += 1
                remaining = total - idx
                print(f"  → Approving this entry and all {remaining} remaining without further prompts.")
                break
            elif resp == "q":
                print()
                print("Review aborted — no changes have been written.  Re-run to try again.")
                raise ReviewAborted("user pressed Q")
            else:
                print("  Please enter Y, N, A, S, or Q.")
        print()

    summary = {
        "candidates":    total,
        "approved":      approved,
        "rejected":      rejected,
        "added":         added,
        "auto_approved": auto_approved,
    }
    tail = f" ({auto_approved} auto-approved above confidence threshold)" if auto_approved else ""
    print(f"Review complete: {approved} approved, {rejected} rejected, {added} added.{tail}")
    print("Proceeding to mute step.")
    print()
    return overrides, summary


# ── "Add" sub-flow ─────────────────────────────────────────────────────────────

def _prompt_add(words: list[dict], log: logging.LoggerAdapter) -> Optional[dict]:
    """
    Handle the 'A' action: search the transcript for a word/phrase the
    automatic scan missed, or fall back to manual timestamp entry.

    Returns an override dict, or None if the user cancels.
    """
    try:
        text = input("  Word or phrase to add (blank to cancel): ").strip()
    except EOFError:
        raise ReviewAborted("EOF while reading input")
    if not text:
        print("  Cancelled.")
        return None

    query_tokens = [t.lower() for t in text.split()]
    span = len(query_tokens)
    if span == 0:
        print("  Cancelled.")
        return None

    stripped = [strip_punct(w.get("word", "") or "").lower() for w in words]

    hits: list[int] = []
    for i in range(0, len(words) - span + 1):
        if any(words[i + j].get("start") is None for j in range(span)):
            continue
        if all(stripped[i + j] == query_tokens[j] for j in range(span)):
            hits.append(i)

    if not hits:
        print(f"  \"{text}\" not found in the transcript (or has no alignment timing).")
        return _prompt_add_manual(text)

    if len(hits) == 1:
        chosen = hits[0]
    else:
        print(f"  Found {len(hits)} occurrences:")
        for n, i in enumerate(hits, start=1):
            first, last = words[i], words[i + span - 1]
            ctx = _context_str(words, i, span, 6)
            print(f"    [{n}] {_fmt_time(first['start'])} – {_fmt_time(last['end'])}   {ctx}")
        try:
            choice = input(f"  Pick [1-{len(hits)}] (blank to cancel, 'm' for manual entry): ").strip()
        except EOFError:
            raise ReviewAborted("EOF while reading input")
        if not choice:
            print("  Cancelled.")
            return None
        if choice.lower() == "m":
            return _prompt_add_manual(text)
        try:
            n = int(choice)
            if not (1 <= n <= len(hits)):
                raise ValueError
        except ValueError:
            print("  Invalid choice — cancelled.")
            return None
        chosen = hits[n - 1]

    first, last = words[chosen], words[chosen + span - 1]
    override = {
        "action":     "add",
        "word_index": chosen,
        "text":       " ".join(words[chosen + j].get("word", "") for j in range(span)),
        "start":      float(first["start"]),
        "end":        float(last["end"]),
    }
    print(f"  Added: \"{override['text']}\"  {_fmt_time(override['start'])} – {_fmt_time(override['end'])}")
    return override


def _prompt_add_manual(text: str) -> Optional[dict]:
    """Manual start/end entry — used when the searched word/phrase isn't
    found in the transcript at all (mis-transcribed or truly missing)."""
    print("  Enter the exact time manually (seconds, e.g. 1203.14, or HH:MM:SS.mmm).")
    try:
        start_raw = input("  Start time (blank to cancel): ").strip()
    except EOFError:
        raise ReviewAborted("EOF while reading input")
    if not start_raw:
        print("  Cancelled.")
        return None
    start = _parse_time_input(start_raw)
    if start is None:
        print(f"  Could not parse '{start_raw}' — cancelled.")
        return None

    try:
        end_raw = input("  End time (blank to cancel): ").strip()
    except EOFError:
        raise ReviewAborted("EOF while reading input")
    if not end_raw:
        print("  Cancelled.")
        return None
    end = _parse_time_input(end_raw)
    if end is None:
        print(f"  Could not parse '{end_raw}' — cancelled.")
        return None

    if end <= start:
        print("  End time must be after start time — cancelled.")
        return None

    override = {"action": "add", "word_index": None, "text": text, "start": start, "end": end}
    print(f"  Added: \"{text}\"  {_fmt_time(start)} – {_fmt_time(end)}")
    return override


def _parse_time_input(s: str) -> Optional[float]:
    """Accept raw seconds ('1203.14') or HH:MM:SS[.mmm] / MM:SS[.mmm]."""
    s = s.strip()
    if not s:
        return None
    if ":" not in s:
        try:
            return float(s)
        except ValueError:
            return None
    parts = s.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        h, m, sec = 0.0, nums[0], nums[1]
    else:
        h, m, sec = nums
    return h * 3600 + m * 60 + sec


# ── Display helpers ────────────────────────────────────────────────────────────

def _fmt_time(seconds: float) -> str:
    """HH:MM:SS.d — one decimal place, for precise review-prompt display.

    A separate helper from utils.fmt_duration (whole seconds only) rather
    than changing that shared function's output format for every other
    caller that doesn't want sub-second precision.
    """
    total_ds = int(round(seconds * 10))
    s, ds = divmod(total_ds, 10)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}.{ds}"


def _context_str(words: list[dict], index: int, span: int, show_context: int) -> str:
    """Build the '...N words before MATCH N words after...' display line."""
    before  = words[max(0, index - show_context):index]
    matched = words[index:index + span]
    after   = words[index + span:index + span + show_context]
    text = " ".join(w.get("word", "") for w in (*before, *matched, *after))
    return f'"...{text}..."'


def _write_review_json(path: Path, overrides: list[dict]) -> None:
    """Atomic write-then-rename, matching utils.write_job's crash-safety pattern."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"overrides": overrides}, indent=2, ensure_ascii=False))
    tmp.replace(path)

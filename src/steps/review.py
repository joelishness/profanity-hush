"""
profanity-hush — Step 4b: Flag & Review

Two phases, both implemented here and called separately by pipeline.py:

  flag()   — scans the transcript against the word list and persists every
             candidate to matches.json. ALWAYS runs, in both interactive
             and unattended modes. This is the pipeline's one and only
             call to steps/matching.py's find_matches() — Step 5
             (steps/mute.py) never re-scans the transcript itself; it only
             ever reads matches.json back (see design doc §4).

  review() — the interactive terminal loop: presents matches.json's
             candidates to a human for approval/rejection, and lets them
             manually add words the automatic scan missed. Only called
             when interactive mode is active — pipeline.py decides
             whether to call this phase at all; this module has no
             internal on/off switch of its own. Reads matches.json rather
             than recomputing matches, so a word shown here is guaranteed
             to be one of the exact candidates Step 5 will otherwise mute
             outright.

  apply_corrections() — the scriptable, non-interactive counterpart to
             review()'s prompts: directly edits review.json from
             caller-supplied word_index/timestamp corrections, with no
             terminal loop at all. Called by pipeline.py's correction
             mode (--skip-index / --add-interval), for the common case
             where the person already knows exactly what's wrong (they
             watched the film and noted a censor_log.json entry or a
             timestamp) and shouldn't have to step back through every
             previously-decided match just to fix one. See design doc
             §13.4.

Data flow:
  transcript.json (or, once Step 4 exists, transcript_aligned.json — same
  schema, so the caller can swap the path with no code change here) +
  config/word_list.txt
      │
      ▼  flag()
  matches.json   — {"matches": [...]}; every find_matches() result,
                   verbatim, unfiltered. Always written, always kept. Each
                   match's start/end (seconds, machine-readable) carries a
                   start_hms/end_hms companion (media-player-friendly
                   H:MM:SS.mmm — see utils.fmt_timestamp()), for finding
                   the moment in a player while reviewing by hand.
      │
      ▼  review()   (only if interactive)
  review.json    — a *sparse* list of overrides, not a full matches copy:
                   a match with no override is implicitly approved (the
                   default outcome once a word matches the list is to
                   mute it). Only entries the human changed need recording:
                     {"action": "skip", "word_index": 412, "text": "crap"}
                     {"action": "add",  "word_index": 913, "text": "bastard",
                      "start": 1203.14, "start_hms": "0:20:03.140",
                      "end": 1203.48, "end_hms": "0:20:03.480"}
                   "word_index" on an "add" entry is informational (it
                   records which transcript word the addition was resolved
                   to, when the user found one by searching); a manually-
                   timed add with no matching transcript word has
                   word_index: null and only start/end (+ their _hms
                   companions) are authoritative. A "skip" entry has no
                   start/end of its own — its timing lives in matches.json,
                   keyed by word_index.

Step 5 (mute.py) is expected to: load matches.json, then apply review.json
on top of it if present — dropping any word_index with action "skip", and
adding a mute interval for each "add" entry's start/end. It never calls
find_matches() itself.

Terminal UI (design doc §8), review phase only:
    [3 of 11]  Word: "crap"  |  Confidence: 0.94  |  Time: 0:23:14.800 – 0:23:15.100
    Context: "...and then he said crap right in front of..."
    Action? [Y]es / [N]o / [A]dd word / [S]kip rest / [Q]uit  >

  Y (default) — approve; word will be muted
  N           — reject; recorded as a "skip" override
  A           — search the transcript for a missed word/phrase to add, or
                enter exact start/end timestamps manually if not found
                (no audio playback in v1 — see design doc §13.4). Manual
                entry accepts either raw seconds ("1203.14") or
                H:MM:SS.mmm / M:SS.mmm ("0:20:03.140") — see
                utils.parse_timestamp().
  S           — approve this and all remaining candidates without prompting
  Q           — abort the whole run; nothing is written, including
                review.json itself (raises ReviewAborted). matches.json
                from the flag phase is untouched and is *not* re-scanned
                on a retry.

min_confidence_for_prompt (config): entries with confidence *above* this
threshold are auto-approved without ever being shown, and are not counted
toward the "approved" total in the final summary (tracked separately as
auto_approved) since no human reviewed them.
"""

import dataclasses
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from utils import cfg_get, fmt_timestamp, mark_step_done, parse_timestamp, read_job, step_logger, write_job
from steps.matching import Match, find_matches, load_word_list, resolve_word_list_path, strip_punct


class ReviewAborted(Exception):
    """Raised when the user presses Q. No output is written; caller must not
    treat this as a step failure — it's a deliberate, clean stop."""


# ── Flag phase (always runs) ────────────────────────────────────────────────

def flag(
    job_dir: Path,
    transcript_path: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 4b, flag phase: scan the transcript for word-list matches.

    Always runs — unlike review() below, this is not conditional on
    interactive mode. Step 5 needs matches.json to exist either way. This
    is the *only* place find_matches() is called; Step 5 never re-scans.

    Returns the path to matches.json.
    """
    if log is None:
        log = step_logger("flag")

    state = read_job(job_dir)
    matches_path = job_dir / "matches.json"

    if "4b_flag" in state.get("steps_completed", []):
        log.info("Step 4b (flag) — ↩  already complete; re-using %s.", matches_path.name)
        if not matches_path.exists():
            raise RuntimeError(
                f"Step 4b (flag) is marked complete but {matches_path} is missing.  "
                "Delete the job directory and re-run from scratch."
            )
        return matches_path

    word_list_path = Path(cfg_get(cfg, "censoring", "word_list", default="/config/word_list.txt"))
    word_list_path = resolve_word_list_path(word_list_path, log)

    log.info("Step 4b — flag candidates against word list")
    log.info("  word list: %s", word_list_path)

    transcript_data = json.loads(transcript_path.read_text())
    words = transcript_data.get("words", [])

    entries = load_word_list(word_list_path, log)
    log.info("  Loaded %d word list entries.", len(entries))

    matches = find_matches(words, entries, log)
    log.info("  Found %d candidate match(es) in %d words.", len(matches), len(words))

    _write_matches_json(matches_path, matches)

    state = read_job(job_dir)
    state["flag"] = {"word_count": len(words), "candidates": len(matches)}
    write_job(job_dir, state)
    mark_step_done(job_dir, "4b_flag")

    return matches_path


def _write_matches_json(path: Path, matches: list[Match]) -> None:
    """Atomic write-then-rename, matching utils.write_job's crash-safety pattern."""
    tmp = path.with_suffix(".json.tmp")
    payload = {"matches": [dataclasses.asdict(m) for m in matches]}
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(path)


# ── Review phase (optional; only when interactive) ──────────────────────────

def review(
    job_dir: Path,
    matches_path: Path,
    transcript_path: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 4b, review phase: interactively review the candidates the flag
    phase already found.

    Reads matches_path (matches.json) rather than re-scanning the
    transcript — the flag phase above is the single source of truth for
    what counts as a candidate. transcript_path is still needed here, but
    only to render surrounding context for the terminal UI, not to find
    matches.

    Returns the path to review.json. Raises ReviewAborted if the user
    quits mid-review (caller should treat this distinctly from a step
    failure).
    """
    if log is None:
        log = step_logger("review")

    state = read_job(job_dir)
    review_path = job_dir / "review.json"

    if "4b_review" in state.get("steps_completed", []):
        log.info("Step 4b (review) — ↩  already complete; re-using %s.", review_path.name)
        if not review_path.exists():
            raise RuntimeError(
                f"Step 4b (review) is marked complete but {review_path} is missing.  "
                "Delete the job directory and re-run from scratch."
            )
        return review_path

    show_context    = int(cfg_get(cfg, "interactive", "show_context_words", default=8))
    min_conf_prompt = float(cfg_get(cfg, "interactive", "min_confidence_for_prompt", default=0.0))

    matches_data = json.loads(matches_path.read_text())
    # SimpleNamespace gives dot-access (m.word_index, m.score, ...) matching
    # the Match dataclass shape the review loop below was written against,
    # without recomputing anything — these are exactly flag()'s results.
    matches = [SimpleNamespace(**d) for d in matches_data.get("matches", [])]

    transcript_data = json.loads(transcript_path.read_text())
    words = transcript_data.get("words", [])

    log.info("Step 4b — interactive review (%d candidate%s flagged)",
             len(matches), "s" if len(matches) != 1 else "")

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


# ── Direct corrections (non-interactive; pipeline.py --skip-index / --add-interval) ─

def apply_corrections(
    job_dir: Path,
    skip_indices: list,
    add_intervals: list,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Edit review.json directly with caller-supplied overrides, without
    going through the interactive terminal loop at all.

    This is the scriptable counterpart to review()'s Y/N/A/S/Q prompts —
    for the common real-world correction case where the person already
    knows exactly what's wrong (they watched the film, noted a timestamp
    or a censor_log.json entry) and shouldn't have to step back through
    every previously-decided match just to fix one. pipeline.py's
    --skip-index and --add-interval flags call this; --redo-review uses
    the interactive review() above instead, for when a fuller re-pass is
    actually wanted.

    skip_indices   — list[int] of word_index values to reject (see
                      matches.json or censor_log.json for the right
                      index). Looked up against matches.json so the
                      recorded "text" is meaningful, but a word_index not
                      currently flagged is still accepted (just with a
                      warning) rather than rejected outright — the
                      reviewer's note about a timestamp/word from the
                      film is more authoritative than what the automatic
                      scan happened to find.
    add_intervals  — list[(text, start_raw, end_raw)] manual mute
                      intervals, exactly like an "A" + manual-entry answer
                      in the interactive loop. start_raw/end_raw accept
                      either raw seconds ("1203.14") or H:MM:SS.mmm /
                      M:SS.mmm ("0:20:03.140") — see utils.parse_timestamp().

    Merges onto whatever overrides already exist in review.json (additive,
    not a fresh overwrite — repeated correction runs accumulate); skip
    entries are de-duplicated by word_index. Always marks '4b_review' done,
    since this is a direct file edit, not a literal completion of the
    interactive loop — there is no interactive loop to be "incomplete".

    Returns the path to review.json. Does not touch matches.json or
    re-run find_matches() — flag()'s output is untouched by corrections,
    only what's layered on top of it.
    """
    if log is None:
        log = step_logger("correct")

    review_path  = job_dir / "review.json"
    matches_path = job_dir / "matches.json"

    overrides = []
    if review_path.exists():
        overrides = json.loads(review_path.read_text()).get("overrides", [])

    matches_by_index = {}
    if matches_path.exists():
        for m in json.loads(matches_path.read_text()).get("matches", []):
            matches_by_index[m["word_index"]] = m

    existing_skip = {o["word_index"] for o in overrides if o.get("action") == "skip"}
    for idx in skip_indices:
        if idx in existing_skip:
            log.info("  --skip-index %d already recorded in review.json — skipping duplicate.", idx)
            continue
        m = matches_by_index.get(idx)
        if m is None:
            log.warning(
                "  --skip-index %d is not one of matches.json's currently-flagged "
                "candidates — recording the override anyway (double-check the index "
                "against censor_log.json if this wasn't intentional).",
                idx,
            )
        text = m["matched_text"] if m else None
        overrides.append({"action": "skip", "word_index": idx, "text": text})
        existing_skip.add(idx)
        log.info("  skip  word_index=%-6d %s", idx, f'"{text}"' if text else "(not in matches.json)")

    for text, start_raw, end_raw in add_intervals:
        start = parse_timestamp(str(start_raw))
        end   = parse_timestamp(str(end_raw))
        if start is None:
            raise RuntimeError(
                f"Step 4b correction: --add-interval start {start_raw!r} for "
                f"{text!r} isn't a valid time — use raw seconds (e.g. "
                f"'1203.14') or H:MM:SS.mmm (e.g. '0:20:03.140')."
            )
        if end is None:
            raise RuntimeError(
                f"Step 4b correction: --add-interval end {end_raw!r} for "
                f"{text!r} isn't a valid time — use raw seconds (e.g. "
                f"'1203.48') or H:MM:SS.mmm (e.g. '0:20:03.480')."
            )
        if end <= start:
            raise RuntimeError(
                f"Step 4b correction: --add-interval end ({end}) must be after "
                f"start ({start}) for {text!r}."
            )
        overrides.append({
            "action": "add", "word_index": None, "text": text,
            "start": start, "start_hms": fmt_timestamp(start),
            "end": end, "end_hms": fmt_timestamp(end),
        })
        log.info("  add   %-22r %s - %s", text, fmt_timestamp(start), fmt_timestamp(end))

    _write_review_json(review_path, overrides)
    mark_step_done(job_dir, "4b_review")

    log.info("  ✓  review.json updated — %d total override(s).", len(overrides))
    return review_path


# ── Review loop ────────────────────────────────────────────────────────────────

def _run_review_loop(
    words: list[dict],
    matches: list,
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
            f'Time: {fmt_timestamp(m.start)} – {fmt_timestamp(m.end)}'
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
            print(f"    [{n}] {fmt_timestamp(first['start'])} – {fmt_timestamp(last['end'])}   {ctx}")
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
    start, end = float(first["start"]), float(last["end"])
    override = {
        "action":     "add",
        "word_index": chosen,
        "text":       " ".join(words[chosen + j].get("word", "") for j in range(span)),
        "start":      start,
        "start_hms":  fmt_timestamp(start),
        "end":        end,
        "end_hms":    fmt_timestamp(end),
    }
    print(f"  Added: \"{override['text']}\"  {override['start_hms']} – {override['end_hms']}")
    return override


def _prompt_add_manual(text: str) -> Optional[dict]:
    """Manual start/end entry — used when the searched word/phrase isn't
    found in the transcript at all (mis-transcribed or truly missing)."""
    print("  Enter the exact time manually (seconds, e.g. 1203.14, or H:MM:SS.mmm, e.g. 0:20:03.140).")
    try:
        start_raw = input("  Start time (blank to cancel): ").strip()
    except EOFError:
        raise ReviewAborted("EOF while reading input")
    if not start_raw:
        print("  Cancelled.")
        return None
    start = parse_timestamp(start_raw)
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
    end = parse_timestamp(end_raw)
    if end is None:
        print(f"  Could not parse '{end_raw}' — cancelled.")
        return None

    if end <= start:
        print("  End time must be after start time — cancelled.")
        return None

    override = {
        "action": "add", "word_index": None, "text": text,
        "start": start, "start_hms": fmt_timestamp(start),
        "end": end, "end_hms": fmt_timestamp(end),
    }
    print(f"  Added: \"{text}\"  {override['start_hms']} – {override['end_hms']}")
    return override


# ── Display helpers ────────────────────────────────────────────────────────────

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

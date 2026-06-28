"""
profanity-hush — shared word-list parsing and transcript matching

Called exactly once in the pipeline: by steps/review.py's flag() function
(Step 4b's flag phase, which always runs, in both interactive and
unattended modes). steps/mute.py (Step 5) does not import this module —
it consumes the flag phase's persisted matches.json directly instead of
re-scanning. Keeping the scan in one place means there is no second code
path that could ever disagree with the first about what counts as a
match — see design doc §4.

Word list notation (config/word_list.txt, see design doc §7.2):
  word      exact, case-insensitive
  =word     exact, case-sensitive (compares as written)
  word*     starts-with, case-insensitive
  =word*    starts-with, case-sensitive
  *word*    contains/substring, case-insensitive
  =*word*   contains/substring, case-sensitive
  multi word phrase     exact case-insensitive token sequence
  =Multi Word Phrase     exact case-sensitive token sequence
  ('*' notation on phrases is not supported — see _parse_entry)

Matching rules (this module's own logic; see find_matches() below):
  - Attached punctuation is stripped from each transcript token before
    comparison (WhisperX writes "shit," / "warning."); the *original* word
    dict (with punctuation intact) is what gets returned in a Match for
    display purposes, but stripped text drives the comparison.
  - Words with no alignment timing (start/end is null — see
    steps/transcribe.py) are excluded from matching entirely: there's
    nothing to review or mute about a word with no timestamp.
"""

import logging
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from utils import fmt_timestamp

_PUNCT = string.punctuation

# Baked into the image by the Dockerfile (a copy of the repo's own
# config/word_list.txt) so the pipeline works out of the box with no host
# config directory at all — see resolve_word_list_path().
DEFAULT_WORD_LIST_PATH = Path("/app/defaults/word_list.txt")


@dataclass
class WordListEntry:
    raw:            str          # original line, verbatim, for display/debug
    tokens:         list[str]    # one token (single word) or several (phrase)
    match_type:     str          # "exact" | "startswith" | "contains"
    case_sensitive: bool
    line_no:        int


@dataclass(kw_only=True)
class Match:
    word_index:   int     # index of the first word in the transcript's words[] list
    span:         int     # number of consecutive words covered (1 for single words)
    matched_text: str     # original transcript text (with punctuation), for display
    entry:        str     # the word_list.txt entry (raw) that produced this match
    start:        float   # seconds, same timestamp scale as the input words list
    start_hms:    str = field(init=False, default="")   # media-player-friendly H:MM:SS.mmm companion to `start`
    end:          float
    end_hms:      str = field(init=False, default="")   # media-player-friendly H:MM:SS.mmm companion to `end`
    score:        float   # confidence; for a phrase, the minimum across its words

    def __post_init__(self) -> None:
        # kw_only=True (all call sites already construct this with keyword
        # arguments) so start_hms/end_hms can sit immediately after the
        # seconds value they're derived from in field-declaration order --
        # and therefore in dataclasses.asdict()'s output, which
        # _write_matches_json() (steps/review.py) serializes verbatim --
        # without tripping the usual "no required field after a defaulted
        # one" dataclass rule. See utils.fmt_timestamp() for the format
        # itself and why it's the one used throughout matches.json/
        # review.json/censor_log.json and the interactive review prompts.
        self.start_hms = fmt_timestamp(self.start)
        self.end_hms   = fmt_timestamp(self.end)


# ── Word list resolution ──────────────────────────────────────────────────────

def resolve_word_list_path(
    configured_path: "str | Path",
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Resolve the word list to actually use, falling back to the built-in
    default baked into the image (DEFAULT_WORD_LIST_PATH) when the
    configured path — normally the host's bind-mounted /config/word_list.txt
    — doesn't exist.

    Every *other* config value already has a Python-side default via
    cfg_get(..., default=...): missing config.yaml is fine, every setting
    just falls back to a built-in value. A word list can't work that way —
    it's a whole file's worth of content, not one scalar — so without this,
    skipping config file installation (README install step 3, explicitly
    documented as optional) would silently break the moment anything tried
    to use the word list. This is what makes that promise actually true.
    """
    configured_path = Path(configured_path)
    if configured_path.exists():
        return configured_path

    if DEFAULT_WORD_LIST_PATH.exists():
        if log:
            log.info(
                "  %s not found — using the built-in default word list (%s).",
                configured_path, DEFAULT_WORD_LIST_PATH,
            )
            log.info(
                "  Copy config/word_list.txt from the repo into your config "
                "directory to customize it."
            )
        return DEFAULT_WORD_LIST_PATH

    # Should not happen in a correctly-built image — the Dockerfile always
    # copies one in. Surfacing this clearly rather than a bare FileNotFoundError
    # from load_word_list() if it's somehow missing.
    raise FileNotFoundError(
        f"No word list found at {configured_path}, and the built-in default "
        f"({DEFAULT_WORD_LIST_PATH}) is missing too — this should not happen "
        "in a correctly-built image. Rebuild it, or copy config/word_list.txt "
        "from the repo into your config directory."
    )


# ── Word list parsing ─────────────────────────────────────────────────────────

def load_word_list(path: "str | Path", log: Optional[logging.LoggerAdapter] = None) -> list[WordListEntry]:
    """
    Parse a word list file (resolved via resolve_word_list_path()) into a
    list of WordListEntry.

    Blank lines and lines starting with '#' are ignored. Malformed entries
    are skipped with a warning rather than silently mis-parsed — a stray
    character producing an unintended broad match is a worse failure mode
    for a profanity filter than dropping one entry.

    Raises FileNotFoundError with an actionable message if the file is
    missing — the most likely cause is an empty host config directory
    (hush.sh already warns about this at startup, but that warning can
    scroll off-screen during a long resumed run).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Word list not found: {path}\n"
            "Copy config/word_list.txt from the repo into your host config "
            "directory (the one mounted to /config — hush.sh prints its path "
            "and warns at startup if it's empty), or set censoring.word_list "
            "in config.yaml to point somewhere else."
        )

    entries: list[WordListEntry] = []
    text = path.read_text(encoding="utf-8")

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entry = _parse_entry(raw_line, line_no, log)
        if entry is not None:
            entries.append(entry)

    if not entries and log:
        log.warning(
            "  %s contains zero usable entries — nothing will ever be "
            "flagged. If this is unexpected, check the file isn't empty or "
            "entirely comments.",
            path,
        )

    return entries


def _parse_entry(
    raw_line: str, line_no: int, log: Optional[logging.LoggerAdapter],
) -> Optional[WordListEntry]:
    text = raw_line.strip()

    case_sensitive = text.startswith("=")
    if case_sensitive:
        text = text[1:]

    leading_star  = text.startswith("*")
    if leading_star:
        text = text[1:]
    trailing_star = text.endswith("*")
    if trailing_star:
        text = text[:-1]

    root = text.strip()
    if not root:
        if log:
            log.warning(
                "word_list.txt line %d: empty entry after stripping notation — skipped: %r",
                line_no, raw_line,
            )
        return None

    if leading_star and not trailing_star:
        if log:
            log.warning(
                "word_list.txt line %d: leading '*' without a trailing '*' is not a "
                "supported notation (only 'word*' starts-with and '*word*' contains "
                "are defined) — skipped: %r",
                line_no, raw_line,
            )
        return None

    match_type = "contains" if (leading_star and trailing_star) else \
                 "startswith" if trailing_star else "exact"

    tokens = root.split()
    if not tokens:
        if log:
            log.warning(
                "word_list.txt line %d: entry has no tokens after splitting — skipped: %r",
                line_no, raw_line,
            )
        return None

    if len(tokens) > 1 and (match_type != "exact" or "*" in root):
        # Covers both a leading/trailing '*' on the whole phrase (caught via
        # match_type above) and a '*' embedded inside one of the phrase's
        # tokens (e.g. "kiss my *ss") — the latter would otherwise silently
        # become a dead entry, since real transcript text never contains a
        # literal asterisk, with no warning that it will never match.
        if log:
            log.warning(
                "word_list.txt line %d: '*' notation is not supported on multi-word "
                "phrases — treating as a literal exact-sequence match instead: %r",
                line_no, raw_line,
            )
        match_type = "exact"

    return WordListEntry(
        raw=raw_line.strip(),
        tokens=tokens,
        match_type=match_type,
        case_sensitive=case_sensitive,
        line_no=line_no,
    )


# ── Transcript matching ───────────────────────────────────────────────────────

def strip_punct(word: str) -> str:
    """Strip leading/trailing punctuation only — mid-word punctuation (don't, y'all) is kept."""
    return word.strip(_PUNCT)


def find_matches(
    words: list[dict],
    entries: list[WordListEntry],
    log: Optional[logging.LoggerAdapter] = None,
) -> list[Match]:
    """
    Walk a transcript's flat words[] list and return every word-list match,
    sorted by word_index.

    words   — transcript.json's "words" array (or transcript_aligned.json's,
              once Step 4 exists — same schema). Each word dict has
              word/start/end/score; start/end/score may be None for
              un-alignable tokens (see steps/transcribe.py), in which case
              the word is skipped — there's nothing to mute without timing.
    entries — from load_word_list().

    Note: matches are not de-duplicated or merged across overlapping spans
    (e.g. a single-word entry "ass" and a phrase entry "kiss my ass" can
    both independently match the same audio). That's left to steps/mute.py,
    which already has to merge overlapping mute intervals for the ffmpeg
    filter graph (§8) regardless of how many separate matches produced
    them — mute.py reads this function's output back from matches.json
    rather than calling find_matches() itself (see module docstring above).
    """
    n = len(words)
    stripped = [strip_punct(w.get("word", "") or "") for w in words]

    exact_ci:   dict[str, list[WordListEntry]] = {}
    exact_cs:   dict[str, list[WordListEntry]] = {}
    prefix_entries:   list[WordListEntry] = []
    contains_entries: list[WordListEntry] = []
    phrase_entries:   list[WordListEntry] = []

    for e in entries:
        if len(e.tokens) > 1:
            phrase_entries.append(e)
        elif e.match_type == "exact":
            key = e.tokens[0] if e.case_sensitive else e.tokens[0].lower()
            (exact_cs if e.case_sensitive else exact_ci).setdefault(key, []).append(e)
        elif e.match_type == "startswith":
            prefix_entries.append(e)
        else:
            contains_entries.append(e)

    matches: list[Match] = []
    skipped_no_timing = 0

    # ── Single-token entries ───────────────────────────────────────────────────
    for i, w in enumerate(words):
        if w.get("start") is None or w.get("end") is None:
            skipped_no_timing += 1
            continue

        tok = stripped[i]
        if not tok:
            continue
        tok_lower = tok.lower()

        hit: Optional[WordListEntry] = None
        for e in exact_ci.get(tok_lower, []):
            hit = e
            break
        if hit is None:
            for e in exact_cs.get(tok, []):
                hit = e
                break
        if hit is None:
            for e in prefix_entries:
                root = e.tokens[0]
                if (tok.startswith(root) if e.case_sensitive else tok_lower.startswith(root.lower())):
                    hit = e
                    break
        if hit is None:
            for e in contains_entries:
                root = e.tokens[0]
                if (root in tok if e.case_sensitive else root.lower() in tok_lower):
                    hit = e
                    break

        if hit is not None:
            matches.append(Match(
                word_index=i,
                span=1,
                matched_text=w.get("word", ""),
                entry=hit.raw,
                start=float(w["start"]),
                end=float(w["end"]),
                score=float(w["score"]) if w.get("score") is not None else 0.0,
            ))

    # ── Phrase entries (separate pass — needs lookahead across words) ─────────
    for e in phrase_entries:
        span = len(e.tokens)
        for i in range(0, n - span + 1):
            ok = True
            for j in range(span):
                w = words[i + j]
                if w.get("start") is None or w.get("end") is None:
                    ok = False
                    break
                t = stripped[i + j]
                if not t:
                    ok = False
                    break
                if e.case_sensitive:
                    if t != e.tokens[j]:
                        ok = False
                        break
                else:
                    if t.lower() != e.tokens[j].lower():
                        ok = False
                        break
            if not ok:
                continue

            first, last = words[i], words[i + span - 1]
            scores = [
                float(words[i + j]["score"]) if words[i + j].get("score") is not None else 0.0
                for j in range(span)
            ]
            matches.append(Match(
                word_index=i,
                span=span,
                matched_text=" ".join(words[i + j].get("word", "") for j in range(span)),
                entry=e.raw,
                start=float(first["start"]),
                end=float(last["end"]),
                score=min(scores),
            ))

    matches.sort(key=lambda m: m.word_index)

    if log and skipped_no_timing:
        log.debug(
            "  %d word(s) excluded from matching (no alignment timing).",
            skipped_no_timing,
        )

    return matches

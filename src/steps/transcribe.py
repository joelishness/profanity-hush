"""
profanity-hush — Step 3: transcription with word-level timestamps

Runs whichever transcription/alignment engines are configured
(alignment.engines.* in config.yaml) against dialog.wav -- the canonical,
already-Demucs-separated stem steps/merge.py's merge_audio() (Step 2b)
produces -- and writes per-segment transcript JSON files with
segment-local (0-based) word timestamps. steps/merge.py's own
merge_transcript() (Step 3b) consumes these and produces the canonical,
film-absolute-timestamped files this module's own per-segment ones are
named after.

── This step now owns its own segmentation, independent of Demucs's ─────

Earlier versions of this pipeline had Step 3 consume whatever per-segment
dialog_NN.wav Step 2 (Demucs) happened to produce -- meaning transcription
was silently forced to inherit Demucs's own audio.segment_size_sec
chunking (default 1800s / 30 minutes), which exists ONLY because Demucs's
source-separation network needs its peak memory bounded (design doc §12:
"a 2-hour file at full quality exhausts 16 GB RAM"). Nothing about that
reason applies to transcription:

  - WhisperX's own faster-whisper call already does its own internal
    ~30-SECOND decode-window chunking regardless of what it's handed
    (this is the actual mechanism behind the drift bug
    docs/timestamp-drift-investigation.md documents -- a different,
    much finer-grained layer than this module's own segmentation, and
    one this module has never controlled).
  - MFA already does its own internal re-chunking within whatever
    segment it's handed (steps/align_mfa.py's own chunk_target_sec,
    25 SECONDS by default) -- it doesn't care what size segment arrives
    at its door either.
  - CrisperWhisper has its own longform_strategy for handling audio of
    any length (steps/transcribe_crisperwhisper.py's own module
    docstring).

What a SMALLER audio.segment_size_sec actually buys Demucs -- bounded
memory -- has nothing to do with transcription accuracy, and a fixed,
wall-clock-offset job-level cut has no awareness of where a word or
sentence actually falls: a boundary Demucs shrugs off can still land a
recognition engine mid-word, silently producing exactly the kind of
false negative the correction workflow (--skip-index/--add-interval)
exists to catch after the fact. Decoupling the two lets transcription
use a LARGER segment size than Demucs (fewer boundaries, less
word-splitting risk), all the way up to alignment.segment_size_sec: 0
(no segmentation at all -- the whole film transcribed in one call per
engine, the default this config ships with; see config.yaml's own
comment on that setting for the resumability trade-off that comes with
it) -- while Demucs keeps whatever smaller size its own memory budget
actually needs.

Mechanically: this step probes dialog.wav's own duration and splits it
via steps/segment.py's split_into_segments() -- the same shared,
source-/size-agnostic primitive Step 1c (segment.py's own segment())
uses for audio_stereo.wav, just pointed at a different file and a
different config value (alignment.segment_size_sec, not
audio.segment_size_sec) -- into dialog_transcribe_NN.wav pieces (or, at
segment_size_sec: 0 or a duration that already fits in one piece, no
split at all: dialog.wav is used directly, same convention every other
single-segment case in this pipeline already follows). This segmentation
is entirely this module's own concern: it is computed fresh on every run
that isn't already fully cached (never reused from, or coupled to,
Step 1c's own state["segments"]), and persisted separately as
state["transcribe_segments"] so a later resume can recover it without
re-touching any file.

One direct consequence: this is also what makes pipeline.py's
--redo-step 3_transcribe simple. Because Step 3 now depends only on the
STABLE dialog.wav (never invalidated by anything Step 3 itself does),
redoing transcription with a new engine config never needs to reach back
past Steps 1a-2b at all -- it only ever needs to clear this step's OWN
stale output (and 3b_merge's) and let the normal per-segment resume
machinery below do the rest. See pipeline.py's own module docstring.

── Engines ────────────────────────────────────────────────────────────────

Every engine in utils.ALIGNMENT_ENGINE_NAMES ("whisperx", "mfa",
"crisperwhisper") is fully independent and separately toggled via its own
alignment.engines.<name> block:

    enabled          -- does this engine run at all this job.
    debug_subtitle    -- export this engine's own, unedited transcript as
                        a per-engine comparison subtitle (steps/
                        transcript_srt.py).
    final             -- is this engine's transcript THE authoritative one
                        -- the one Step 4b flags words against and Step 5
                        mutes/beeps from. Exactly one enabled engine must
                        have this true (checked once, at startup, by
                        utils.validate_alignment_engines() -- this module
                        assumes it's already true by the time it runs).
    final_subtitle    -- only consulted on whichever engine is final:
                        whether to also export that transcript as the
                        plain, non-karaoke "final" subtitle.
    embed_subtitle    -- whether to mux this engine's own subtitle
                        output(s) into the delivered output video.

The authoritative transcript for a segment is engine_words[final_engine]
for that segment, full stop, with exactly ONE documented exception below
(MFA's own fallback_to_whisperx).

── The one structural dependency: MFA needs WhisperX's own pass ─────────

MFA (steps/align_mfa.py) never performs its own speech recognition -- it
re-times WhisperX's own recognized text by searching for it across the
audio it's handed. So whenever alignment.engines.mfa.enabled is true,
WhisperX's own recognition (model.transcribe()) AND its own wav2vec2/CTC
alignment pass (whisperx.align()) both run internally too, REGARDLESS of
alignment.engines.whisperx.enabled -- there would be nothing for MFA to
re-time otherwise. What alignment.engines.whisperx.enabled actually
controls is narrower than "does the recognition run": it's "is that
recognition ALSO exposed" -- written to its own transcript_whisperx_NN.json,
eligible for debug_subtitle, eligible to be final.

── MFA's own fallback_to_whisperx, and why it's not a cascade ───────────

alignment.engines.mfa.fallback_to_whisperx (default true): if
align_with_mfa() raises MFAError for a WHOLE segment, MFA's own
transcript_mfa_NN.json for that segment stays genuinely empty/absent, but
IF mfa is the final engine, the AUTHORITATIVE transcript.json for that
segment's span uses WhisperX's own raw alignment instead (already
computed as MFA's own required input) -- this is the one and only case
in this module where a segment's authoritative words don't come directly
from engine_words[final_engine].

── Casing/punctuation policy (applies to every engine) ──────────────────

Word casing is preserved exactly as each engine produces it. Do NOT
lowercase. Punctuation attached to words is preserved here; stripping
happens at match time in steps/matching.py.

── Resume support ────────────────────────────────────────────────────────

If transcript_NN.json already exists for a segment it is skipped -- its
per-engine sibling files (transcript_<engine>_NN.json) are checked
directly against utils.ALIGNMENT_ENGINE_NAMES.

If '3_transcribe' is already marked done in job.json, the step returns
immediately with paths (and this job's own transcribe_segments)
recovered from job.json -- no file access at all.

Marks '3_transcribe' done once all segments complete.
"""

import gc
import json
import logging
import time
from pathlib import Path
from typing import Optional

from utils import (
    ALIGNMENT_ENGINE_NAMES as _ENGINES,
    cfg_get,
    fmt_duration,
    mark_step_done,
    probe_duration_sec,
    read_job,
    step_logger,
    write_job,
)
from steps.align_mfa import align_with_mfa, MFAError
from steps.segment import split_into_segments
from steps.transcribe_crisperwhisper import load_crisperwhisper_model, transcribe_with_crisperwhisper


_ENGINE_LABELS = {
    "whisperx":       "WhisperX",
    "mfa":            "MFA",
    "crisperwhisper": "CrisperWhisper",
}


def _engine_label(name: str) -> str:
    return _ENGINE_LABELS.get(name, name)


def _engine_toggles(cfg: dict, name: str) -> dict:
    return {
        "enabled":        bool(cfg_get(cfg, "alignment", "engines", name, "enabled")),
        "debug_subtitle": bool(cfg_get(cfg, "alignment", "engines", name, "debug_subtitle")),
        "final":          bool(cfg_get(cfg, "alignment", "engines", name, "final")),
        "final_subtitle": bool(cfg_get(cfg, "alignment", "engines", name, "final_subtitle")),
        "embed_subtitle": bool(cfg_get(cfg, "alignment", "engines", name, "embed_subtitle")),
    }


def transcribe(
    job_dir: Path,
    dialog_path: Path,
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> tuple[list[Path], list[tuple[Path, float]]]:
    """
    Step 3: run every configured engine against dialog.wav, using this
    step's OWN segmentation (see module docstring) -- independent of
    whatever segmentation Step 2's Demucs pass used.

    dialog_path -- the canonical, full-duration dialog.wav produced by
    steps/merge.py's merge_audio() (Step 2b). This is the ONLY audio
    input this function ever reads; it never touches per-(Demucs-)
    segment stems.

    Returns (transcript_paths, transcribe_segments):
      transcript_paths    -- list of transcript_NN.json paths (one per
                              this step's OWN segment, in order) -- the
                              AUTHORITATIVE per-segment files.
      transcribe_segments -- list[(dialog_transcribe_NN.wav, start_offset_sec)],
                              this step's own segmentation -- handed to
                              steps/merge.py's merge_transcript() so it
                              knows each per-segment file's own global
                              offset without re-deriving anything.

    Each engine's own transcript_<engine>_NN.json is a side effect, not
    part of this return value -- callers that want the canonical
    per-engine merge look for job_dir / f"transcript_{name}.json"
    directly, once steps/merge.py has produced it.
    """
    if log is None:
        log = step_logger("transcribe")

    # ── Resume check ──────────────────────────────────────────────────────────
    state = read_job(job_dir)
    if "3_transcribe" in state.get("steps_completed", []):
        log.info("Step 3 — ↩  already complete; loading transcript paths from job.json.")
        return _transcripts_from_state(job_dir, state)

    # ── This job's own transcription segmentation ────────────────────────────
    # Computed fresh every non-cached run (never reused from, or coupled
    # to, Step 1c/Demucs's own state["segments"]) and persisted
    # separately below so a later resume never needs to re-probe or
    # re-split anything -- see module docstring.
    size_sec = int(cfg_get(cfg, "alignment", "segment_size_sec"))
    duration = probe_duration_sec(dialog_path, log)
    transcribe_segments = split_into_segments(
        dialog_path, job_dir, "dialog_transcribe", duration, size_sec, log,
    )
    _persist_transcribe_segments(job_dir, transcribe_segments, duration)
    n = len(transcribe_segments)

    # ── Resolve engine config ────────────────────────────────────────────────
    toggles = {name: _engine_toggles(cfg, name) for name in _ENGINES}

    final_candidates = [name for name in _ENGINES if toggles[name]["final"]]
    if len(final_candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one alignment.engines.*.final: true, found "
            f"{len(final_candidates)} ({final_candidates!r}). This should "
            "have been caught by utils.validate_alignment_engines() at "
            "startup -- see pipeline.py's main()."
        )
    final_engine = final_candidates[0]

    whisperx_enabled       = toggles["whisperx"]["enabled"]
    mfa_enabled             = toggles["mfa"]["enabled"]
    crisperwhisper_enabled  = toggles["crisperwhisper"]["enabled"]
    need_whisperx_pass      = whisperx_enabled or mfa_enabled

    mfa_fallback_allowed = (
        bool(cfg_get(cfg, "alignment", "engines", "mfa", "fallback_to_whisperx"))
        if mfa_enabled else False
    )

    log.info("Step 3 — transcription")
    log.info(
        "  segmentation: %d segment(s)  (alignment.segment_size_sec=%s, "
        "independent of Step 2's own audio.segment_size_sec)",
        n, size_sec,
    )
    log.info(
        "  engines enabled: %s  |  final (authoritative): %s",
        ", ".join(name for name in _ENGINES if toggles[name]["enabled"]) or "(none)",
        final_engine,
    )
    if mfa_enabled and not whisperx_enabled:
        log.info(
            "  alignment.engines.mfa.enabled is true and alignment.engines."
            "whisperx.enabled is false -- WhisperX's own recognition + "
            "alignment will still run every segment as MFA's required "
            "input, just not be written out or exposed as its own "
            "transcript/subtitle -- see this module's own docstring."
        )

    # ── Config for whichever engines are actually in play ────────────────────
    wx_model_name   = cfg_get(cfg, "alignment", "engines", "whisperx", "model")
    wx_language     = cfg_get(cfg, "alignment", "engines", "whisperx", "language", allow_null=True)
    wx_batch_size   = int(cfg_get(cfg, "alignment", "engines", "whisperx", "batch_size"))
    wx_beam_size    = int(cfg_get(cfg, "alignment", "engines", "whisperx", "beam_size"))
    wx_device       = cfg_get(cfg, "alignment", "engines", "whisperx", "device")
    wx_compute_type = cfg_get(cfg, "alignment", "engines", "whisperx", "compute_type")

    cw_language = cfg_get(cfg, "alignment", "engines", "crisperwhisper", "language", allow_null=True)

    # ── Load models (only what's actually needed) ────────────────────────────
    whisperx = None
    wx_model = None
    if need_whisperx_pass:
        try:
            import whisperx  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "whisperx is not installed inside the container, but it's "
                "needed this run -- alignment.engines.whisperx.enabled "
                "and/or alignment.engines.mfa.enabled is true. Ensure the "
                "Dockerfile pip-installs whisperx."
            ) from exc

        log.info(
            "  Loading Whisper model '%s'  (language=%s batch_size=%d "
            "beam_size=%d device=%s compute_type=%s) ...",
            wx_model_name, wx_language or "auto", wx_batch_size, wx_beam_size,
            wx_device, wx_compute_type,
        )
        t_load = time.monotonic()
        wx_model = whisperx.load_model(
            wx_model_name,
            wx_device,
            compute_type=wx_compute_type,
            language=wx_language,
            asr_options={"beam_size": wx_beam_size},
            vad_method="silero",
        )
        log.info("  ✓  Model loaded in %.1f s.", time.monotonic() - t_load)

    cw_model = None
    if crisperwhisper_enabled:
        cw_model = load_crisperwhisper_model(cfg, log)

    # ── Alignment model cache (per language, whisperx only) ──────────────────
    align_model:    object = None
    align_metadata: object = None
    loaded_lang:    str    = ""

    transcript_paths: list[Path] = []
    segment_results:  list[dict] = []

    for i, (dialog, start_offset) in enumerate(transcribe_segments):
        seg_idx = i + 1   # 1-based; transcript files always use _NN suffix
        t_path  = job_dir / f"transcript_{seg_idx:02d}.json"

        # ── Per-segment resume ────────────────────────────────────────────────
        if t_path.exists():
            log.info(
                "  [%d/%d] ↩  %s already exists — skipping.",
                seg_idx, n, t_path.name,
            )
            transcript_paths.append(t_path)
            engines_with_data = [
                name for name in _ENGINES
                if (job_dir / f"transcript_{name}_{seg_idx:02d}.json").exists()
            ]
            try:
                existing = json.loads(t_path.read_text())
                word_count = len(existing.get("words", []))
            except (OSError, json.JSONDecodeError):
                word_count = None
            segment_results.append({
                "index":      seg_idx,
                "transcript": t_path.name,
                "word_count": word_count,
                "skipped":    True,
                "mfa_fallback_reason": None,
                "engines_with_data": engines_with_data,
            })
            continue

        dur_sec = (
            float(transcribe_segments[i + 1][1]) - start_offset
            if i + 1 < n else duration - start_offset
        )
        log.info(
            "  [%d/%d] Transcribing %s  (%.0f s, global offset %.1f s) ...",
            seg_idx, n, dialog.name, dur_sec, start_offset,
        )

        t0 = time.monotonic()
        mfa_fallback_reason: Optional[str] = None
        engine_words: dict[str, list[dict]] = {}
        whisperx_words: list[dict] = []
        whisperx_lang = wx_language or "en"

        # ── WhisperX recognition + its own alignment (shared by whisperx
        #    and mfa) ────────────────────────────────────────────────────
        if need_whisperx_pass:
            audio = whisperx.load_audio(str(dialog))
            result = wx_model.transcribe(audio, batch_size=wx_batch_size, language=wx_language)
            whisperx_lang = result.get("language") or wx_language or "en"
            segs_out = result.get("segments", [])
            log.debug(
                "    Whisper pass: %d segments, detected language=%s",
                len(segs_out), whisperx_lang,
            )

            if not segs_out:
                log.warning(
                    "  [%d/%d] WhisperX found no speech in %s — no "
                    "whisperx/mfa data for this segment.",
                    seg_idx, n, dialog.name,
                )
            else:
                align_model, align_metadata, loaded_lang = _ensure_align_model(
                    align_model, align_metadata, loaded_lang, whisperx_lang,
                    wx_device, whisperx, log,
                )
                t_stage = time.monotonic()
                aligned = whisperx.align(
                    segs_out, align_model, align_metadata, audio, wx_device,
                    return_char_alignments=False,
                )
                whisperx_words = _collect_whisperx_words(aligned)
                log.debug(
                    "    WhisperX alignment: %d words in %.1fs for %s.",
                    len(whisperx_words), time.monotonic() - t_stage, dialog.name,
                )

                if whisperx_enabled:
                    engine_words["whisperx"] = whisperx_words

                if mfa_enabled:
                    try:
                        t_stage = time.monotonic()
                        mfa_words = align_with_mfa(dialog, dur_sec, segs_out, whisperx_words, cfg, log)
                        engine_words["mfa"] = mfa_words
                        log.debug(
                            "    MFA alignment: %d words in %.1fs for %s.",
                            len(mfa_words), time.monotonic() - t_stage, dialog.name,
                        )
                    except MFAError as exc:
                        if final_engine == "mfa" and not mfa_fallback_allowed:
                            raise RuntimeError(
                                f"MFA alignment failed for {dialog.name} and "
                                "alignment.engines.mfa.fallback_to_whisperx "
                                "is false (config.yaml) -- not falling back."
                            ) from exc
                        mfa_fallback_reason = str(exc)
                        log.warning(
                            "  [%d/%d] MFA alignment failed for %s%s.  "
                            "Reason: %s",
                            seg_idx, n, dialog.name,
                            " -- the authoritative transcript will use "
                            "WhisperX's own timing for this segment instead"
                            if final_engine == "mfa" else
                            " -- no MFA data for this segment's comparison "
                            "transcript",
                            exc,
                        )

            del audio
            gc.collect()

        # ── CrisperWhisper -- fully independent, runs regardless of
        #    whether WhisperX found anything ──────────────────────────────
        if crisperwhisper_enabled:
            try:
                t_stage = time.monotonic()
                cw_words = transcribe_with_crisperwhisper(cw_model, dialog, cw_language, cfg, log)
                engine_words["crisperwhisper"] = cw_words
                log.debug(
                    "    CrisperWhisper transcription: %d words in %.1fs for %s.",
                    len(cw_words), time.monotonic() - t_stage, dialog.name,
                )
            except Exception as exc:  # noqa: BLE001 -- never fatal, see below
                log.warning(
                    "  [%d/%d] CrisperWhisper failed for %s%s.  Reason: %s",
                    seg_idx, n, dialog.name,
                    " -- the authoritative transcript has no data for "
                    "this segment" if final_engine == "crisperwhisper" else
                    " -- no CrisperWhisper data for this segment's "
                    "comparison transcript",
                    exc,
                )

        elapsed = time.monotonic() - t0

        # ── Resolve the authoritative words for this segment ─────────────────
        if final_engine in engine_words:
            words = engine_words[final_engine]
        elif final_engine == "mfa" and mfa_fallback_reason is not None and mfa_fallback_allowed:
            words = whisperx_words
        else:
            words = []

        lang_by_engine = {
            "whisperx":       whisperx_lang,
            "mfa":            whisperx_lang,
            "crisperwhisper": cw_language or "en",
        }
        final_lang = lang_by_engine.get(final_engine, whisperx_lang)

        # ── Write JSON: authoritative, then each enabled engine's own ────────
        t_path = _write_transcript_variant(
            job_dir, seg_idx, "", words, final_lang, start_offset,
        )
        for name, w in engine_words.items():
            if toggles[name]["enabled"]:
                _write_transcript_variant(
                    job_dir, seg_idx, f"_{name}", w,
                    lang_by_engine.get(name, final_lang), start_offset,
                )

        log.info(
            "  [%d/%d] ✓  %s  words=%d  elapsed=%s  [%d/%d segments transcribed]",
            seg_idx, n, t_path.name, len(words),
            fmt_duration(elapsed), seg_idx, n,
        )

        transcript_paths.append(t_path)
        segment_results.append({
            "index":       seg_idx,
            "transcript":  t_path.name,
            "word_count":  len(words),
            "elapsed_sec": round(elapsed, 1),
            "mfa_fallback_reason": mfa_fallback_reason,
            "engines_with_data": sorted(engine_words.keys()),
        })

    # ── Cleanup models ────────────────────────────────────────────────────────
    if wx_model is not None:
        del wx_model
    if align_model is not None:
        del align_model, align_metadata
    if cw_model is not None:
        del cw_model
    gc.collect()

    # ── Persist metadata and mark done ────────────────────────────────────────
    state = read_job(job_dir)
    state["alignment_engines"] = [
        {
            "engine": name,
            "label":  _engine_label(name),
            **toggles[name],
            "segments_with_data": sum(
                1 for r in segment_results if name in r.get("engines_with_data", [])
            ),
        }
        for name in _ENGINES
    ]
    state["transcription"] = {
        "final_engine": final_engine,
        "segments":     segment_results,
        "mfa_fallback_segments": sum(
            1 for s in segment_results if s.get("mfa_fallback_reason")
        ),
    }
    write_job(job_dir, state)
    mark_step_done(job_dir, "3_transcribe")

    total_words = sum(s.get("word_count") or 0 for s in segment_results)
    log.info("  ✓  All segments transcribed.  Total words: %d", total_words)
    return transcript_paths, transcribe_segments


# ── Helpers ───────────────────────────────────────────────────────────────────

def _persist_transcribe_segments(
    job_dir: Path, segs: list[tuple[Path, float]], duration: float,
) -> None:
    """
    Persist this step's own segmentation to job.json's
    "transcribe_segments" block -- same {"index", "path", "start_sec"}
    shape steps/segment.py's own _persist() already uses for Step 1c's
    "segments" block, kept as a genuinely separate key: these describe a
    DIFFERENT segmentation, over a different source file, and nothing
    should ever conflate the two. Written BEFORE the per-segment
    transcribe loop starts (unlike segment_results, built up entry by
    entry as each segment finishes) so a resumed/interrupted run's
    "already done" fast path (_transcripts_from_state() below) can
    recover it without depending on the loop having reached any
    particular segment.
    """
    state = read_job(job_dir)
    state["total_duration_sec"] = state.get("total_duration_sec") or duration
    state["transcribe_segments"] = [
        {"index": i + 1, "path": p.name, "start_sec": s}
        for i, (p, s) in enumerate(segs)
    ]
    write_job(job_dir, state)


def _ensure_align_model(
    align_model: object,
    align_metadata: object,
    loaded_lang: str,
    detected_lang: str,
    device: str,
    whisperx,
    log: logging.LoggerAdapter,
) -> tuple[object, object, str]:
    if align_model is None or loaded_lang != detected_lang:
        if align_model is not None:
            log.debug(
                "    Language changed %s→%s; reloading alignment model.",
                loaded_lang, detected_lang,
            )
            del align_model, align_metadata
            gc.collect()
        log.debug(
            "    Loading alignment model for language '%s' ...", detected_lang
        )
        align_model, align_metadata = whisperx.load_align_model(
            language_code=detected_lang,
            device=device,
        )
        loaded_lang = detected_lang
    return align_model, align_metadata, loaded_lang


def _collect_whisperx_words(aligned: dict) -> list[dict]:
    words = []
    for seg in aligned.get("segments", []):
        for w in seg.get("words", []):
            word_text = w.get("word", "")
            if not word_text:
                continue
            words.append({
                "word":  word_text,
                "start": w.get("start"),
                "end":   w.get("end"),
                "score": w.get("score"),
            })
    return words


def _write_transcript_variant(
    job_dir: Path,
    seg_idx: int,
    suffix: str,
    words: list[dict],
    detected_lang: str,
    start_offset: float,
) -> Path:
    path = job_dir / f"transcript{suffix}_{seg_idx:02d}.json"
    path.write_text(json.dumps({
        "language":             detected_lang,
        "segment_index":        seg_idx,
        "segment_start_offset": start_offset,
        "words":                words,
    }, indent=2, ensure_ascii=False))
    return path


def _transcripts_from_state(
    job_dir: Path, state: dict,
) -> tuple[list[Path], list[tuple[Path, float]]]:
    """
    Recover (transcript_paths, transcribe_segments) from job.json.

    Used on resume when '3_transcribe' is already marked complete --
    both lists are reconstructed from persisted metadata alone (this
    step's own "transcription" block for transcript_paths, its
    "transcribe_segments" block for the segmentation), with no file
    access beyond the existence check on each transcript path itself.

    Raises RuntimeError if any listed transcript file is missing, or if
    job.json predates "transcribe_segments" (a job whose Step 3 last ran
    under an earlier version of this pipeline, before segmentation moved
    under this step's own control) -- delete the job directory and
    re-run in that case, same as every other "predates this bookkeeping"
    case in this pipeline.
    """
    paths: list[Path] = []
    for seg in state.get("transcription", {}).get("segments", []):
        p = job_dir / seg["transcript"]
        if not p.exists():
            raise RuntimeError(
                f"Step 3 is marked complete but transcript file is missing: {p}\n"
                "Delete the job directory and re-run from scratch."
            )
        paths.append(p)

    recorded_segs = state.get("transcribe_segments")
    if not recorded_segs:
        raise RuntimeError(
            "Step 3 is marked complete but job.json has no recorded "
            "'transcribe_segments' -- this job's transcription last ran "
            "under an earlier version of this pipeline, before Step 3 "
            "owned its own segmentation. Delete the job directory and "
            "re-run from scratch."
        )
    transcribe_segments = [
        (job_dir / seg["path"], float(seg["start_sec"])) for seg in recorded_segs
    ]

    return paths, transcribe_segments

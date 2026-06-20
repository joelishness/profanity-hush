"""
profanity-hush — Step 3: transcription with word-level timestamps

Runs WhisperX on each dialog stem and produces per-segment transcript JSON
files (transcript_01.json, transcript_02.json, …) with segment-local
(0-based) word timestamps.  Steps/merge.py consumes these and produces the
canonical transcript.json with global timestamps.

WhisperX pipeline (run in sequence, per-segment):
  1. model.transcribe() — batched Whisper inference → segment-level timestamps
  2. whisperx.align()  — wav2vec2 forced alignment  → word-level timestamps

Both the Whisper model and the alignment model are loaded once for the whole
job (not per segment) to avoid repeated multi-minute load times.

Casing policy:
  Word casing is preserved exactly as WhisperX produces it.  Do NOT
  lowercase.  Original casing is required for case-sensitive (=) word list
  entries, compared in steps/matching.py.  WhisperX capitalises proper
  nouns and sentence-initial words; this is the signal used to distinguish
  e.g. "Dick" (name) from "dick" (profanity).

Punctuation policy:
  Punctuation attached to words (e.g. "shit,", "warning.") is preserved
  here; stripping happens at match time in steps/matching.py.

Unaligned words:
  Some tokens cannot be aligned (numerals, currency symbols, punctuation-
  only tokens).  These appear in the transcript with start/end/score set to
  null.  Downstream steps skip null-timestamped words at mute time.

VAD:
  Uses Silero VAD (vad_method="silero"), which is free and requires no
  HuggingFace token.  The default WhisperX VAD backend (pyannote) requires
  token-authenticated model downloads, which conflicts with the project's
  CPU-only, no-cloud-dependency constraint.  The dialog stems from Demucs
  are already relatively clean, so VAD quality is a secondary concern.

Resume support:
  If transcript_NN.json already exists for a segment it is skipped.
  If '3_transcribe' is already marked done in job.json, the step returns
  immediately with paths recovered from job.json.

Marks '3_transcribe' done once all segments complete.
"""

import gc
import json
import logging
import time
from pathlib import Path
from typing import Optional

from utils import (
    cfg_get,
    fmt_duration,
    mark_step_done,
    read_job,
    step_logger,
    write_job,
)


def transcribe(
    job_dir: Path,
    segments: list[tuple[Path, float]],   # (audio_stereo_NN.wav, start_offset_sec)
    stem_pairs: list[tuple[Path, Path]],  # (dialog_NN.wav, score_sfx_NN.wav)
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> list[Path]:
    """
    Step 3: transcribe each dialog stem with WhisperX.

    Returns a list of transcript_NN.json paths (one per segment, in order).

    segments   — (audio_stereo_NN.wav, start_offset_sec) from Step 1c.
                 Provides the segment start offsets stored in the JSON output
                 so that Step 3b can apply them as global-timestamp offsets.
    stem_pairs — (dialog_NN.wav, score_sfx_NN.wav) from Step 2.
                 Only the dialog stem (first element) is used here.
    """
    if log is None:
        log = step_logger("transcribe")

    # ── Resume check ──────────────────────────────────────────────────────────
    state = read_job(job_dir)
    if "3_transcribe" in state.get("steps_completed", []):
        log.info("Step 3 — ↩  already complete; loading transcript paths from job.json.")
        return _transcripts_from_state(job_dir, state)

    # ── Config ────────────────────────────────────────────────────────────────
    model_name   = cfg_get(cfg, "whisperx", "model",        default="large-v2")
    language     = cfg_get(cfg, "whisperx", "language",     default="en") or None
    batch_size   = int(cfg_get(cfg, "whisperx", "batch_size",   default=4))
    beam_size    = int(cfg_get(cfg, "whisperx", "beam_size",    default=5))
    device       = cfg_get(cfg, "whisperx", "device",       default="cpu")
    # compute_type: int8 for CPU (faster inference, lower RAM); float16 for GPU.
    # Derived from device unless overridden in config.
    compute_type = cfg_get(cfg, "whisperx", "compute_type",
                           default="int8" if device == "cpu" else "float16")

    n = len(stem_pairs)
    log.info("Step 3 — WhisperX transcription")
    log.info(
        "  model=%s  language=%s  batch_size=%d  beam_size=%d"
        "  device=%s  compute_type=%s  segments=%d",
        model_name, language or "auto",
        batch_size, beam_size, device, compute_type, n,
    )

    # ── Import whisperx ───────────────────────────────────────────────────────
    try:
        import whisperx  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "whisperx is not installed inside the container.  "
            "Ensure the Dockerfile pip-installs whisperx."
        ) from exc

    # ── Load Whisper model (once for all segments) ────────────────────────────
    log.info("  Loading Whisper model '%s' ...", model_name)
    t_load = time.monotonic()
    wx_model = whisperx.load_model(
        model_name,
        device,
        compute_type=compute_type,
        language=language,
        asr_options={"beam_size": beam_size},
        # Silero VAD: no HuggingFace token required.  See module docstring.
        vad_method="silero",
    )
    log.info("  ✓  Model loaded in %.1f s.", time.monotonic() - t_load)

    # ── Alignment model cache (per language) ──────────────────────────────────
    # Reload only if detected language changes across segments (rare in practice
    # for a single film, but handle it gracefully).
    align_model:    object = None
    align_metadata: object = None
    loaded_lang:    str    = ""

    transcript_paths: list[Path] = []
    segment_results:  list[dict] = []

    for i, ((dialog, _score_sfx), (seg_wav, start_offset)) in enumerate(
        zip(stem_pairs, segments)
    ):
        seg_idx = i + 1   # 1-based; transcript files always use _NN suffix
        t_path  = job_dir / f"transcript_{seg_idx:02d}.json"

        # ── Per-segment resume ────────────────────────────────────────────────
        if t_path.exists():
            log.info(
                "  [%d/%d] ↩  %s already exists — skipping.",
                seg_idx, n, t_path.name,
            )
            transcript_paths.append(t_path)
            try:
                existing = json.loads(t_path.read_text())
                segment_results.append({
                    "index":      seg_idx,
                    "transcript": t_path.name,
                    "word_count": len(existing.get("words", [])),
                    "skipped":    True,
                })
            except (OSError, json.JSONDecodeError):
                segment_results.append({
                    "index":      seg_idx,
                    "transcript": t_path.name,
                    "skipped":    True,
                })
            continue

        dur_sec = _seg_duration(state, seg_wav.name)
        log.info(
            "  [%d/%d] Transcribing %s  (%.0f s, global offset %.1f s) ...",
            seg_idx, n, dialog.name, dur_sec, start_offset,
        )

        t0 = time.monotonic()

        # ── Load audio ────────────────────────────────────────────────────────
        # whisperx.load_audio() handles stereo→mono and 44.1kHz→16kHz
        # conversion internally; the 44.1kHz PCM WAV from Demucs is fine as-is.
        audio = whisperx.load_audio(str(dialog))

        # ── Transcribe ────────────────────────────────────────────────────────
        result = wx_model.transcribe(
            audio,
            batch_size=batch_size,
            language=language,
        )

        detected_lang = result.get("language") or language or "en"
        segs_out = result.get("segments", [])
        log.debug(
            "    Whisper pass: %d segments, detected language=%s",
            len(segs_out), detected_lang,
        )

        # ── Forced alignment ──────────────────────────────────────────────────
        if not segs_out:
            # No speech detected (silent or noise-only segment).  Write an
            # empty transcript rather than calling align() on an empty list.
            log.warning(
                "  [%d/%d] WhisperX found no speech in %s — writing empty transcript.",
                seg_idx, n, dialog.name,
            )
            words: list[dict] = []
        else:
            # Lazy-load or reload alignment model when language changes.
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

            aligned = whisperx.align(
                segs_out,
                align_model,
                align_metadata,
                audio,
                device,
                return_char_alignments=False,
            )

            # ── Collect words ─────────────────────────────────────────────────
            # Timestamps are segment-local (0-based).  Step 3b applies the
            # global start_offset to produce film-absolute timestamps.
            # Words that couldn't be aligned have start/end/score = None;
            # include them so the full word count is preserved in the JSON.
            words = []
            for seg in aligned.get("segments", []):
                for w in seg.get("words", []):
                    word_text = w.get("word", "")
                    if not word_text:
                        continue   # skip empty tokens (defensive)
                    words.append({
                        "word":  word_text,          # original casing + punctuation
                        "start": w.get("start"),     # None if alignment failed
                        "end":   w.get("end"),
                        "score": w.get("score"),
                    })

        # Release the numpy audio array before the next segment loads its own.
        del audio
        gc.collect()

        elapsed = time.monotonic() - t0

        # ── Write JSON ────────────────────────────────────────────────────────
        transcript: dict = {
            "language":             detected_lang,
            "segment_index":        seg_idx,
            "segment_start_offset": start_offset,
            "words":                words,
        }
        t_path.write_text(json.dumps(transcript, indent=2, ensure_ascii=False))

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
        })

    # ── Cleanup models ────────────────────────────────────────────────────────
    del wx_model
    if align_model is not None:
        del align_model, align_metadata
    gc.collect()

    # ── Persist metadata and mark done ────────────────────────────────────────
    state = read_job(job_dir)
    state["transcription"] = {
        "model":    model_name,
        "language": language or "auto",
        "segments": segment_results,
    }
    write_job(job_dir, state)
    mark_step_done(job_dir, "3_transcribe")

    total_words = sum(s.get("word_count", 0) for s in segment_results)
    log.info("  ✓  All segments transcribed.  Total words: %d", total_words)
    return transcript_paths


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seg_duration(state: dict, seg_wav_name: str) -> float:
    """
    Return the audio duration of a segment from job.json metadata.

    Avoids a second ffprobe call — the information is already on disk from
    Step 1c.  Returns 0.0 if the segment isn't found (should not occur in
    normal operation but handled gracefully).
    """
    segs      = state.get("segments", [])
    total_sec = float(state.get("total_duration_sec", 0.0))

    for j, seg in enumerate(segs):
        if seg.get("path") == seg_wav_name:
            if j + 1 < len(segs):
                return float(segs[j + 1]["start_sec"]) - float(seg["start_sec"])
            return total_sec - float(seg["start_sec"])

    # Single-segment passthrough: job.json has path="audio_stereo.wav" but
    # the lookup above should always find it.  Fall back to total duration.
    return total_sec if len(segs) == 1 else 0.0


def _transcripts_from_state(job_dir: Path, state: dict) -> list[Path]:
    """
    Recover the ordered list of transcript paths from job.json.

    Used on resume when '3_transcribe' is already marked complete.
    Raises RuntimeError if any listed file is missing.
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
    return paths

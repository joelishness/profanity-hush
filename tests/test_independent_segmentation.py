"""
Regression tests for Step 3 (transcribe)'s independent segmentation --
see steps/transcribe.py's own module docstring and pipeline.py's
--redo-step 3_transcribe for the full design writeup. Three things this
covers that reasoning alone didn't fully settle without running real
ffmpeg against real WAV files (see individual test docstrings for what
each one actually caught):

  1. steps/segment.py's split_into_segments() genuinely works as a
     shared, source-/size-agnostic primitive -- the same function backs
     both Step 1c (audio_stereo.wav, audio.segment_size_sec) and Step 3's
     own segmentation (dialog.wav, alignment.segment_size_sec), and the
     two can disagree in size and count with no interference.

  2. steps/transcribe.py's new signature (dialog_path in, no more
     externally-supplied segments/stem_pairs) correctly computes and
     persists its own segmentation, and a resumed call makes genuinely
     ZERO calls into any ASR engine -- confirmed by making the mocked
     engine raise if touched at all on the second call.

  3. steps/merge.py's merge_audio()/merge_transcript() split correctly
     backward-compatibility-resumes a job whose audio+transcript merge
     both completed under the OLD, combined "3b_merge" step (before this
     split existed) -- with zero re-derivation from per-segment sources
     a real such job would already have cleaned up.

Also documents a real, narrow finding from building this: going from one
alignment.segment_size_sec to another, with stale leftover
dialog_transcribe_NN.wav pieces from the old size still on disk, needs as
many raise-then-retry cycles as there are stale pieces UNLESS they're
proactively cleared first (steps/segment.py's split_into_segments() stops
at the first duration mismatch it finds per call, it doesn't scan and fix
every stale piece in one pass) -- this is exactly why pipeline.py's
--redo-step 3_transcribe handling globs them all away up front rather
than leaning on that validation to sort itself out.

Run with:  PYTHONPATH=src python3 tests/test_independent_segmentation.py
Needs ffmpeg on PATH (already a hard requirement of this pipeline).
"""
import json
import math
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import utils  # noqa: E402
from steps.segment import split_into_segments  # noqa: E402
from steps.merge import merge_audio, merge_transcript  # noqa: E402
from steps import transcribe as tr  # noqa: E402

log = utils.step_logger("test")


def _run(fn):
    fn()
    print(f"  ok  {fn.__name__}")


def _make_tone_wav(path: Path, duration: float, freq: float = 220.0, sr: int = 44100) -> None:
    n = int(sr * duration)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            t = i / sr
            val = int(12000 * math.sin(2 * math.pi * freq * t))
            frames += struct.pack("<hh", val, val)
        w.writeframes(bytes(frames))


def _duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


# ── steps/segment.py: split_into_segments() as a shared primitive ────────

def test_split_into_segments_passthrough_at_zero_or_when_it_fits():
    tmp = Path(tempfile.mkdtemp())
    try:
        src = tmp / "dialog.wav"
        _make_tone_wav(src, 5.0)
        assert split_into_segments(src, tmp, "dialog", 5.0, 0, log) == [(src, 0.0)]
        assert split_into_segments(src, tmp, "dialog", 5.0, 1800, log) == [(src, 0.0)]
    finally:
        shutil.rmtree(tmp)


def test_two_independent_segment_sizes_on_the_same_source():
    """
    The property this whole feature depends on: the SAME canonical
    source can be independently re-segmented at two DIFFERENT sizes for
    two DIFFERENT callers (Demucs's own audio_stereo.wav path vs.
    transcribe's own dialog.wav path), with neither affecting the other.
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        src = tmp / "dialog.wav"
        _make_tone_wav(src, 12.0)

        demucs_style = split_into_segments(src, tmp, "audio_stereo", 12.0, 4, log)
        assert len(demucs_style) == 3
        assert [round(s, 3) for _, s in demucs_style] == [0.0, 4.0, 8.0]

        transcribe_style = split_into_segments(src, tmp, "dialog_transcribe", 12.0, 6, log)
        assert len(transcribe_style) == 2
        assert [round(s, 3) for _, s in transcribe_style] == [0.0, 6.0]

        assert (tmp / "audio_stereo_03.wav").exists()
        assert (tmp / "dialog_transcribe_02.wav").exists()
    finally:
        shutil.rmtree(tmp)


def test_size_change_without_proactive_cleanup_needs_one_retry_per_stale_piece():
    """
    Confirms the actual finding, not just the reasoning behind it:
    split_into_segments() stops at the FIRST duration mismatch it finds
    (delete-then-raise, matching this codebase's existing
    _validate_segment/_validate_audio_raw pattern everywhere else) and
    does not keep scanning the rest of a call's still-stale pieces.
    Going from a 2-piece 6s scheme to a 4-piece 3s scheme -- with no
    proactive cleanup -- genuinely needs more than one call to converge.
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        src = tmp / "dialog.wav"
        _make_tone_wav(src, 12.0)
        split_into_segments(src, tmp, "dialog_transcribe", 12.0, 6, log)

        attempts = 0
        result = None
        while result is None:
            attempts += 1
            assert attempts <= 10, "did not converge"
            try:
                result = split_into_segments(src, tmp, "dialog_transcribe", 12.0, 3, log)
            except RuntimeError as exc:
                assert "Integrity check failed" in str(exc)
        assert attempts > 1, "expected more than one attempt without proactive cleanup"
        assert len(result) == 4

        # The fix: proactively glob-clear before changing size again --
        # exactly what pipeline.py's --redo-step 3_transcribe cleanup does.
        for p in tmp.glob("dialog_transcribe_*.wav"):
            p.unlink()
        clean = split_into_segments(src, tmp, "dialog_transcribe", 12.0, 2, log)
        assert len(clean) == 6
    finally:
        shutil.rmtree(tmp)


# ── steps/transcribe.py: independent segmentation + resume ───────────────

def _base_cfg(segment_size_sec: int) -> dict:
    off = {"enabled": False, "debug_subtitle": False, "final": False, "final_subtitle": False, "embed_subtitle": False}
    return {"alignment": {
        "segment_size_sec": segment_size_sec,
        "engines": {
            "whisperx": {**off, "model": "large-v2", "language": "en", "batch_size": 4,
                         "beam_size": 5, "device": "cpu", "compute_type": "int8"},
            "mfa": {**off, "acoustic_model": "english_mfa", "dictionary": "english_mfa",
                    "g2p_model": "english_us_mfa", "beam": 400, "retry_beam": 1000,
                    "chunk_target_sec": 25, "chunk_edge_margin_sec": 0.3,
                    "chunk_timeout_sec": 120, "fallback_to_whisperx": True},
            "crisperwhisper": {"enabled": True, "debug_subtitle": True, "final": True,
                                "final_subtitle": True, "embed_subtitle": False,
                                "model": "large", "language": "en", "backend": "ct2",
                                "device": "cpu", "compute_type": "float32"},
        },
    }}


def _fake_cw(model, dialog_wav, language, cfg, log):
    return [{"word": f"seg-{dialog_wav.name}", "start": 0.1, "end": 0.4, "score": None}]


def test_transcribe_computes_its_own_segmentation_independent_of_demucs():
    tmp = Path(tempfile.mkdtemp())
    try:
        job_dir = tmp
        dialog = job_dir / "dialog.wav"
        _make_tone_wav(dialog, 12.0)
        # Demucs's own, totally different scheme -- must have zero effect.
        utils.write_job(job_dir, {
            "job_id": "t", "steps_completed": [], "total_duration_sec": 12.0,
            "segments": [{"index": i + 1, "path": f"audio_stereo_{i+1:02d}.wav", "start_sec": i * 3.0}
                         for i in range(4)],
        })
        cfg = _base_cfg(segment_size_sec=6)

        with patch.object(tr, "load_crisperwhisper_model", return_value=object()), \
             patch.object(tr, "transcribe_with_crisperwhisper", side_effect=_fake_cw):
            paths, segs = tr.transcribe(job_dir, dialog, cfg, log)

        assert len(segs) == 2, segs  # transcribe's own 6s scheme, not Demucs's 4
        assert [p.name for p, _ in segs] == ["dialog_transcribe_01.wav", "dialog_transcribe_02.wav"]
        state = utils.read_job(job_dir)
        assert len(state["segments"]) == 4               # Demucs's own, untouched
        assert len(state["transcribe_segments"]) == 2     # transcribe's own, independent
    finally:
        shutil.rmtree(tmp)


def test_transcribe_resume_makes_zero_engine_calls():
    """The whole point: a second call against an already-'3_transcribe'
    job recovers everything from job.json alone."""
    tmp = Path(tempfile.mkdtemp())
    try:
        job_dir = tmp
        dialog = job_dir / "dialog.wav"
        _make_tone_wav(dialog, 12.0)
        utils.write_job(job_dir, {"job_id": "t", "steps_completed": []})
        cfg = _base_cfg(segment_size_sec=5)

        with patch.object(tr, "load_crisperwhisper_model", return_value=object()), \
             patch.object(tr, "transcribe_with_crisperwhisper", side_effect=_fake_cw):
            first = tr.transcribe(job_dir, dialog, cfg, log)

        def _boom(*a, **k):
            raise AssertionError("engine/file access on a resumed run")

        with patch.object(tr, "load_crisperwhisper_model", side_effect=_boom), \
             patch.object(tr, "transcribe_with_crisperwhisper", side_effect=_boom), \
             patch.object(tr, "probe_duration_sec", side_effect=_boom), \
             patch.object(tr, "split_into_segments", side_effect=_boom):
            second = tr.transcribe(job_dir, dialog, cfg, log)

        assert second == first
    finally:
        shutil.rmtree(tmp)


def test_transcribe_zero_segment_size_is_the_whole_file_one_piece():
    """alignment.segment_size_sec: 0 (this config's default) -> dialog.wav
    used directly, no dialog_transcribe_*.wav split files created."""
    tmp = Path(tempfile.mkdtemp())
    try:
        job_dir = tmp
        dialog = job_dir / "dialog.wav"
        _make_tone_wav(dialog, 12.0)
        utils.write_job(job_dir, {"job_id": "t", "steps_completed": []})
        cfg = _base_cfg(segment_size_sec=0)

        with patch.object(tr, "load_crisperwhisper_model", return_value=object()), \
             patch.object(tr, "transcribe_with_crisperwhisper", side_effect=_fake_cw):
            paths, segs = tr.transcribe(job_dir, dialog, cfg, log)

        assert segs == [(dialog, 0.0)]
        assert not list(job_dir.glob("dialog_transcribe_*.wav"))
    finally:
        shutil.rmtree(tmp)


# ── steps/merge.py: merge_audio()/merge_transcript() split ───────────────

def _merge_cfg(keep_intermediates=False) -> dict:
    return {"output": {"keep_intermediates": keep_intermediates, "keep_correction_artifacts": True}}


def test_merge_audio_backward_compat_with_old_combined_3b_merge():
    """
    A job whose audio+transcript merge BOTH completed under the OLD,
    combined "3b_merge" step (before this split existed) has no
    "2b_merge_audio" marker at all. merge_audio() must recognize
    "3b_merge" as an equally-valid "already done" signal and backfill
    its own bookkeeping -- never trying to re-derive dialog.wav/
    score_sfx.wav from per-segment sources a real such job would have
    already cleaned up.
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        job_dir = tmp
        dialog_out = job_dir / "dialog.wav"
        score_sfx_out = job_dir / "score_sfx.wav"
        _make_tone_wav(dialog_out, 12.0, 330.0)
        _make_tone_wav(score_sfx_out, 12.0, 110.0)
        utils.write_job(job_dir, {
            "job_id": "t",
            "steps_completed": ["1a_extract_raw", "1b_downmix", "1c_segment", "2_separate", "3b_merge"],
            "total_duration_sec": 12.0,
        })

        # Deliberately-wrong inputs pointing at files that don't exist --
        # if this ever fell through to actually using them instead of the
        # backward-compat short-circuit, this would fail loudly.
        fake_segs = [(job_dir / "audio_stereo_01.wav", 0.0)]
        fake_pairs = [(job_dir / "dialog_01.wav", job_dir / "score_sfx_01.wav")]

        result_dialog, result_score = merge_audio(job_dir, fake_segs, fake_pairs, _merge_cfg(), log)
        assert result_dialog == dialog_out and result_score == score_sfx_out

        state = utils.read_job(job_dir)
        assert "2b_merge_audio" in state["steps_completed"]
    finally:
        shutil.rmtree(tmp)


def test_merge_transcript_offsets_and_never_deletes_single_segment_dialog_wav():
    tmp = Path(tempfile.mkdtemp())
    try:
        job_dir = tmp
        dialog = job_dir / "dialog.wav"
        dialog.write_bytes(b"canonical dialog content")
        t_path = job_dir / "transcript_01.json"
        t_path.write_text(json.dumps({"language": "en", "words": [{"word": "hi", "start": 0.1, "end": 0.4, "score": 0.9}]}))
        utils.write_job(job_dir, {
            "job_id": "t", "steps_completed": [], "total_duration_sec": 5.0, "alignment_engines": [],
        })

        transcript_out = merge_transcript(job_dir, [(dialog, 0.0)], [t_path], _merge_cfg(keep_intermediates=False), log)
        data = json.loads(transcript_out.read_text())
        assert [w["word"] for w in data["words"]] == ["hi"]

        # Single-segment jobs: the one "segment" IS dialog.wav itself --
        # merge_transcript() must never delete it (merge_audio() owns
        # that file's lifecycle, not this step).
        assert dialog.exists() and dialog.read_bytes() == b"canonical dialog content"
        assert not t_path.exists()  # per-segment transcript IS this step's to clean up
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    for fn in [
        test_split_into_segments_passthrough_at_zero_or_when_it_fits,
        test_two_independent_segment_sizes_on_the_same_source,
        test_size_change_without_proactive_cleanup_needs_one_retry_per_stale_piece,
        test_transcribe_computes_its_own_segmentation_independent_of_demucs,
        test_transcribe_resume_makes_zero_engine_calls,
        test_transcribe_zero_segment_size_is_the_whole_file_one_piece,
        test_merge_audio_backward_compat_with_old_combined_3b_merge,
        test_merge_transcript_offsets_and_never_deletes_single_segment_dialog_wav,
    ]:
        _run(fn)
    print("\nALL TESTS PASSED")

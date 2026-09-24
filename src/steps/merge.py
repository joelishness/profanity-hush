"""
profanity-hush — Step 2b: merge audio stems, Step 3b: merge transcript

Two independent consolidation steps that USED to be one ("Step 3b: merge
per-segment transcripts and audio stems"). Splitting them, and moving the
audio half earlier, is what makes Step 3 (transcribe) able to use its own
segmentation -- alignment.segment_size_sec, independent of Step 2's own
audio.segment_size_sec -- instead of being forced to inherit whatever
chunking Demucs happened to use. See steps/transcribe.py's own module
docstring for the full "why" (short version: a job-level segment cut, at
a fixed wall-clock offset, has no idea where a word or sentence actually
falls -- fine for Demucs, a real recognition-accuracy risk for a
transcription engine, and none of the three engines this pipeline
supports need job-level segmentation for their own memory reasons the
way Demucs does).

── merge_audio() -- Step "2b_merge_audio" ─────────────────────────────────

Consolidates Step 2's per-(Demucs-)segment dialog_NN.wav/score_sfx_NN.wav
into canonical, full-duration dialog.wav/score_sfx.wav. Runs immediately
after Step 2, BEFORE Step 3 -- this is the one piece of what used to be
"Step 3b" that had to move earlier, since Step 3 now needs a full-
duration, already-Demucs-separated file to independently re-segment,
before it can run at all. Cleans up audio_stereo*.wav and Step 2's own
per-segment dialog_NN.wav/score_sfx_NN.wav once consumed (same
keep_intermediates policy as before -- see utils.keep_intermediate()).

Once this succeeds, Steps 1a-2b never need to run again for this job --
dialog.wav/score_sfx.wav are the STABLE artifacts every later step
(Step 3, Step 5, Step 6) depends on, and nothing downstream of Step 2b
ever invalidates them: unlike the old, combined "Step 3b", a
pipeline.py --redo-step 3_transcribe now never needs to reach back past
its own step at all -- see pipeline.py's own module docstring.

── merge_transcript() -- Step "3b_merge" ──────────────────────────────────

Consolidates Step 3's per-(transcribe-)segment transcript_NN.json (+ each
enabled engine's own transcript_<engine>_NN.json) into the canonical
transcript.json (+ transcript_<engine>.json). Kept under the SAME job.json
step name ("3b_merge") the combined step always used -- there's no reason
to churn that name now that its scope has simply narrowed to transcripts
only, and keeping it means a job.json written by an EARLIER version of
this pipeline (where "3b_merge" covered both halves) still means "nothing
left to do here" under this one, with no migration step needed; the
now-separate "2b_merge_audio" gracefully backfills itself the same way on
first resume -- see merge_audio()'s own resume check.

Cleans up Step 3's own per-segment dialog_transcribe_NN.wav (its
one-and-only consumer, steps/transcribe.py, is done with them the moment
every segment's transcript_NN.json exists) alongside transcript_NN.json/
transcript_<engine>_NN.json, same keep_intermediates policy as before.

── Shared machinery, unchanged from the combined step ─────────────────────

_merge_transcript_source() is untouched in spirit: still the one function
that offsets and concatenates a set of per-segment transcript JSON files
into one canonical file, still shared between the primary transcript.json
merge and each enabled engine's own comparison transcript. It's now fed
whichever `segments` list its caller is actually merging (Step 2's own
Demucs-segments, when relevant -- it isn't any more, transcript merging
never touches those -- or Step 3's own transcribe-segments), which is why
its per-segment duration display no longer looks anything up in job.json
by filename: the list handed to it already carries every offset needed,
segment to segment, and the caller's own total_duration_sec covers the
last one -- see this function's own docstring for what changed and why.

── job.json bookkeeping ────────────────────────────────────────────────────

state["merge_audio"] -- new: {"segments", "files": {"dialog", "score_sfx"},
"dialog_sha256", "score_sfx_sha256"}. steps/mute.py and steps/recombine.py
read dialog_sha256/score_sfx_sha256 from here now (moved from state["merge"]
-- see each of those modules' own small update).

state["merge"] -- unchanged key name, narrowed content: {"segments"
(now Step 3's OWN transcribe-segment count, not Demucs's),
"word_count", "files": {"transcript", "transcript_<engine>"...}}. No
longer carries "dialog"/"score_sfx" entries -- those moved to
state["merge_audio"]["files"].
"""

import json
import logging
from pathlib import Path
from typing import Optional

from utils import (
    finalize_output,
    fmt_duration,
    fmt_size,
    keep_intermediate,
    mark_step_done,
    read_job,
    run_cmd,
    step_logger,
    tmp_output_path,
    verify_and_hash_before_publish,
    write_job,
)


# ── Step 2b: merge audio stems ──────────────────────────────────────────────

def merge_audio(
    job_dir: Path,
    segments: list[tuple[Path, float]],    # (audio_stereo_NN.wav, start_offset_sec) -- Step 1c/Demucs's own
    stem_pairs: list[tuple[Path, Path]],   # (dialog_NN.wav, score_sfx_NN.wav) -- Step 2's own
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> tuple[Path, Path]:
    """
    Step 2b: consolidate Step 2's per-(Demucs-)segment stems into
    canonical dialog.wav/score_sfx.wav.

    Returns (dialog.wav, score_sfx.wav).
    """
    if log is None:
        log = step_logger("merge")

    state = read_job(job_dir)
    dialog_out    = job_dir / "dialog.wav"
    score_sfx_out = job_dir / "score_sfx.wav"

    # Backward-compatible resume: a job whose audio consolidation
    # completed under an earlier version of this pipeline -- before this
    # step existed as its own thing -- has "3b_merge" marked done instead
    # (that step used to cover both halves; see this module's own
    # docstring), never "2b_merge_audio". Treating either marker as
    # sufficient means an old, already-fully-processed job resumes
    # cleanly with no migration step of its own: the first time this
    # runs against such a job it just verifies the (already correct)
    # canonical files and backfills its own "2b_merge_audio" bookkeeping.
    done = state.get("steps_completed", [])
    if "2b_merge_audio" in done or "3b_merge" in done:
        log.info("Step 2b — ↩  already complete.")
        if not dialog_out.exists() or not score_sfx_out.exists():
            raise RuntimeError(
                "Step 2b is marked complete but dialog.wav/score_sfx.wav "
                "are missing. Delete the job directory and re-run from "
                "scratch."
            )
        if "2b_merge_audio" not in done:
            mark_step_done(job_dir, "2b_merge_audio")
        return dialog_out, score_sfx_out

    n = len(segments)
    log.info("Step 2b — merging %d segment(s) of separated audio into canonical stems.", n)

    dialogs    = [d for (d, _) in stem_pairs]
    score_sfxs = [s for (_, s) in stem_pairs]

    if n == 1:
        if not dialog_out.exists():
            raise RuntimeError(f"Single-segment merge: {dialog_out} not found.")
        if not score_sfx_out.exists():
            raise RuntimeError(f"Single-segment merge: {score_sfx_out} not found.")
        log.info(
            "  ✓  Single-segment passthrough — dialog.wav (%s)  score_sfx.wav (%s)",
            fmt_size(dialog_out), fmt_size(score_sfx_out),
        )
    else:
        if dialog_out.exists() and score_sfx_out.exists():
            log.info(
                "  ↩  dialog.wav + score_sfx.wav already exist — verifying "
                "(resumed after a prior interrupted run) ..."
            )
        else:
            log.info("  Concatenating %d dialog stems ...", n)
            _ffmpeg_concat(dialogs, dialog_out, log)
            log.info("  Concatenating %d score/SFX stems ...", n)
            _ffmpeg_concat(score_sfxs, score_sfx_out, log)
            log.info(
                "  ✓  dialog.wav (%s)  score_sfx.wav (%s)",
                fmt_size(dialog_out), fmt_size(score_sfx_out),
            )

    total_sec      = float(state.get("total_duration_sec", 0.0))
    dialog_hash    = verify_and_hash_before_publish(dialog_out, "dialog.wav", total_sec, log)
    score_sfx_hash = verify_and_hash_before_publish(score_sfx_out, "score_sfx.wav", total_sec, log)
    log.info("  ✓  dialog.wav + score_sfx.wav passed integrity check.")

    if not keep_intermediate(cfg, correction_artifact=False):
        for seg_wav, _ in segments:
            _unlink_if(seg_wav, log)
        _unlink_if(job_dir / "audio_stereo.wav", log)
        if n > 1:
            for d, s in stem_pairs:
                _unlink_if(d, log)
                _unlink_if(s, log)

    state = read_job(job_dir)
    state["merge_audio"] = {
        "segments": n,
        "files": {"dialog": dialog_out.name, "score_sfx": score_sfx_out.name},
        "dialog_sha256":    dialog_hash,
        "score_sfx_sha256": score_sfx_hash,
    }
    write_job(job_dir, state)
    mark_step_done(job_dir, "2b_merge_audio")

    log.info("  ✓  Step 2b complete.")
    return dialog_out, score_sfx_out


# ── Step 3b: merge transcript ───────────────────────────────────────────────

def merge_transcript(
    job_dir: Path,
    transcribe_segments: list[tuple[Path, float]],   # (dialog_transcribe_NN.wav, start_offset_sec) -- Step 3's own
    transcript_paths: list[Path],                     # transcript_NN.json from Step 3
    cfg: dict,
    log: Optional[logging.LoggerAdapter] = None,
) -> Path:
    """
    Step 3b: consolidate Step 3's per-(transcribe-)segment transcripts
    into the canonical transcript.json (and, per enabled engine, its own
    transcript_<engine>.json).

    Returns transcript.json.
    """
    if log is None:
        log = step_logger("merge")

    state = read_job(job_dir)
    if "3b_merge" in state.get("steps_completed", []):
        log.info("Step 3b — ↩  already complete.")
        t_out = job_dir / "transcript.json"
        if not t_out.exists():
            raise RuntimeError(
                "Step 3b is marked complete but transcript.json is "
                "missing. Delete the job directory and re-run from "
                "scratch."
            )
        return t_out

    n = len(transcribe_segments)
    log.info("Step 3b — merging %d transcription segment(s) into canonical transcript.", n)

    transcript_out = job_dir / "transcript.json"
    total_words = _merge_transcript_source(
        transcript_paths, transcribe_segments, transcript_out, state, n, log,
    )

    # Per-engine comparison transcripts -- same reasoning as the combined
    # step always used: react to which engines actually produced
    # per-segment data (job.json's own "alignment_engines" list, written
    # by steps/transcribe.py), not to which ones happen to be enabled in
    # the config this exact invocation is reading.
    stage_outputs: dict[str, Path] = {}
    for engine in state.get("alignment_engines", []):
        if not engine.get("enabled"):
            continue
        name      = engine["engine"]
        out_path  = job_dir / f"transcript_{name}.json"
        seg_paths = [job_dir / f"transcript_{name}_{i+1:02d}.json" for i in range(n)]
        seg_paths = [p if p.exists() else None for p in seg_paths]
        if out_path.exists() or any(p is not None for p in seg_paths):
            _merge_transcript_source(seg_paths, transcribe_segments, out_path, state, n, log)
            stage_outputs[name] = out_path

    if not keep_intermediate(cfg, correction_artifact=False):
        # dialog_transcribe_NN.wav -- Step 3's own per-segment input --
        # is fully consumed the moment every segment's transcript_NN.json
        # exists and has been merged here; nothing downstream ever reads
        # it again (it's cheaply re-derivable from dialog.wav, kept by
        # merge_audio(), should it ever be needed again -- see
        # steps/transcribe.py's own module docstring). A single-segment
        # job's "segment" IS dialog.wav itself (steps/transcribe.py's own
        # split_into_segments() short-circuit) -- never delete that.
        if n > 1:
            for seg_wav, _ in transcribe_segments:
                _unlink_if(seg_wav, log)

        for t_path in transcript_paths:
            _unlink_if(t_path, log)

        for engine in state.get("alignment_engines", []):
            if not engine.get("enabled"):
                continue
            for i in range(n):
                _unlink_if(job_dir / f"transcript_{engine['engine']}_{i+1:02d}.json", log)

    state = read_job(job_dir)
    files = {"transcript": transcript_out.name}
    for name, path in stage_outputs.items():
        files[f"transcript_{name}"] = path.name
    state["merge"] = {
        "segments":   n,
        "word_count": total_words,
        "files": files,
    }
    write_job(job_dir, state)
    mark_step_done(job_dir, "3b_merge")

    log.info(
        "  ✓  Step 3b complete.  Total words: %d  Duration: %s",
        total_words,
        fmt_duration(float(state.get("total_duration_sec", 0.0))),
    )
    return transcript_out


# ── Helpers ───────────────────────────────────────────────────────────────────

def _merge_transcript_source(
    per_segment_paths: list[Optional[Path]],
    segments: list[tuple[Path, float]],
    out_path: Path,
    state: dict,
    n: int,
    log: logging.LoggerAdapter,
) -> int:
    """
    Merge one alignment source's per-segment transcript files (offset-
    adjusted, concatenated in segment order) into one canonical file at
    out_path. Shared by the primary transcript.json merge and the
    per-engine comparison merges in merge_transcript() above.

    `segments` is whatever list the CALLER is merging against -- always
    Step 3's own transcribe-segments now (merge_audio() above never calls
    this at all; only merge_transcript() does). Each segment's own
    duration, for the per-segment log line below, is computed directly
    from consecutive entries of THIS SAME list (segments[i+1]'s own start,
    or state["total_duration_sec"] for the last one) rather than looked
    up by filename against job.json's "segments" block -- that block
    only ever records Step 1c/Demucs's OWN segmentation, which Step 3's
    transcribe-segments have no reason to share a naming scheme with any
    more (see steps/transcribe.py's own module docstring). Computing it
    from the list already in hand is also simply less code than a lookup
    that needs to work regardless of which segmentation scheme is in
    play.

    A None entry in per_segment_paths means this segment has no data for
    this particular source -- contributes zero words for that segment's
    span: a real, correctly-reported gap in that source's transcript,
    not a merge failure.

    Guarded by an out_path existence check: Step 3b's cleanup deletes
    per_segment_paths once a merge succeeds, so a crash between "this
    write succeeded" and mark_step_done() would otherwise need to redo
    the merge on the next run with its sources already gone.

    Returns the total word count merged.
    """
    if out_path.exists():
        log.info(
            "  ↩  %s already exists — verifying (resumed after a prior "
            "interrupted run) ...", out_path.name,
        )
        return len(json.loads(out_path.read_text()).get("words", []))

    total_sec = float(state.get("total_duration_sec", 0.0))
    all_words:     list[dict] = []
    detected_lang: str        = "en"
    total_words = 0

    for i, (t_path, (seg_wav, start_offset)) in enumerate(zip(per_segment_paths, segments)):
        seg_idx = i + 1
        if t_path is None:
            log.info(
                "  [%d/%d] %s — no data for this segment.",
                seg_idx, n, out_path.name,
            )
            continue

        data = json.loads(t_path.read_text())
        detected_lang = data.get("language", detected_lang)
        seg_words     = data.get("words", [])

        # Apply global offset to each word's timestamps.
        # Words with null timestamps (alignment failures) are preserved as-is.
        adjusted: list[dict] = []
        for w in seg_words:
            aw = dict(w)
            if w.get("start") is not None:
                aw["start"] = round(float(w["start"]) + start_offset, 4)
            if w.get("end") is not None:
                aw["end"]   = round(float(w["end"])   + start_offset, 4)
            adjusted.append(aw)

        seg_end_sec = float(segments[i + 1][1]) if i + 1 < n else total_sec
        log.info(
            "  [%d/%d] %s  offset=%s  words=%d  (%s → %s)",
            seg_idx, n, t_path.name,
            f"{start_offset:.1f}s",
            len(seg_words),
            fmt_duration(start_offset),
            fmt_duration(seg_end_sec),
        )
        all_words.extend(adjusted)
        total_words += len(seg_words)

    transcript_data: dict = {
        "language": detected_lang,
        "words":    all_words,
    }
    out_path.write_text(
        json.dumps(transcript_data, indent=2, ensure_ascii=False)
    )
    log.info("  ✓  %s  words=%d", out_path.name, total_words)
    return total_words


def _ffmpeg_concat(sources: list[Path], dest: Path, log: logging.LoggerAdapter) -> None:
    """
    Concatenate PCM WAV files using the ffmpeg concat demuxer (stream copy).

    All sources must have identical format (sample rate, bit depth, channels).
    The 44.1 kHz 16-bit stereo PCM constraint throughout the pipeline
    guarantees this.
    """
    list_path = dest.parent / f".concat_{dest.stem}.txt"
    tmp = tmp_output_path(dest)
    try:
        list_path.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in sources) + "\n"
        )
        tmp.unlink(missing_ok=True)
        run_cmd(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_path),
                "-c", "copy",
                str(tmp),
            ],
            log,
        )
        finalize_output(tmp, dest)
    finally:
        if list_path.exists():
            list_path.unlink()


def _unlink_if(path: Path, log: logging.LoggerAdapter) -> None:
    """Delete a file if it exists; no-op and no error if absent."""
    if path.exists():
        path.unlink()
        log.debug("  Removed intermediate: %s", path.name)

"""
Tests for audio_processing.py and its two step integrations (steps/extract.py's
downmix_to_stereo(), steps/recombine.py's recombine()) -- center boost at Step 1b,
mix & master at Step 6, settings log / drift, and the --redo-audio preparation.

Real ffmpeg, synthetic audio (noise / tones), same spirit as the other tests/.
NOT covered here: pipeline.py's main() and hush.sh (they need the whole stack).

Run with:  PYTHONPATH=src python3 tests/test_audio_processing.py
In the image (no host python needed):
  docker run --rm --entrypoint python -v "$PWD/tests:/tests:ro" profanity-hush /tests/test_audio_processing.py
"""
import array
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml  # noqa: E402

import audio_processing as ap  # noqa: E402
import utils  # noqa: E402
from steps import extract, recombine  # noqa: E402

log = logging.getLogger("test_audio_processing")
log.addHandler(logging.NullHandler())
alog = utils.step_logger("test")


def _run(fn):
    fn()
    print(f"  ok  {fn.__name__}")


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(enabled=True, boost=0, night="off", target=None, ceiling=-2.0, **extra):
    cfg = {"audio_processing": {"enabled": enabled, "center_boost_db": boost, "night_mode": night,
                                "loudness": {"target_lufs": target, "ceiling_dbfs": ceiling}},
           "output": {"keep_intermediates": True, "keep_correction_artifacts": True}}
    cfg.update(extra)
    return cfg


def _ff(*args):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def _multichannel(path, layout, seconds=4, tone_on=None, tone_hz=1000):
    """Layout file: uncorrelated pink noise on every channel, or (tone_on=NAME) a tone on one channel only."""
    names = ap._LAYOUT_CHANNELS[layout] if layout in ap._LAYOUT_CHANNELS else layout.split("|")
    layout_name = layout if layout in ap._LAYOUT_CHANNELS else "7.1(wide)"
    ins, maps = [], []
    for i, n in enumerate(names):
        if tone_on:
            src = f"sine=f={tone_hz}:r=48000:d={seconds}" if n == tone_on else f"anullsrc=r=48000:cl=mono:d={seconds}"
        else:
            src = f"anoisesrc=c=pink:r=48000:d={seconds}:s={i + 1}:a=0.2"
        ins += ["-f", "lavfi", "-i", src]
        maps.append(f"{i}.0-{n}")
    _ff(*ins, "-filter_complex", f"join=inputs={len(names)}:channel_layout={layout_name}:map={'|'.join(maps)}",
        "-c:a", "pcm_s24le", str(path))


def _lufs(path):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true:framelog=quiet",
                        "-f", "null", "-"], capture_output=True, text=True)
    s = p.stderr[p.stderr.rindex("Summary:"):]
    return (float(re.search(r"\bI:\s+(-?[\d.]+) LUFS", s).group(1)),
            float(re.search(r"\bPeak:\s+(-?[\d.]+) dBFS", s).group(1)),
            float(re.search(r"\bLRA:\s+(-?[\d.]+) LU", s).group(1)))


def _pcm(path):
    w = wave.open(str(path))
    a = array.array("h")
    a.frombytes(w.readframes(w.getnframes()))
    return a


def _max_diff(p, q):
    a, b = _pcm(p), _pcm(q)
    assert len(a) == len(b), (len(a), len(b))
    return max(abs(x - y) for x, y in zip(a, b))


def _stems(d, seconds=40):
    """Film-ish stems: speech-like bursts of noise + a quiet bed with one loud 'explosion'. 16-bit stereo 44.1 kHz."""
    _ff("-f", "lavfi", "-i", f"anoisesrc=c=pink:r=44100:d={seconds}:s=11:a=0.5",
        "-af", "volume='0.30*if(lt(mod(t,5),2.2),1,0)':eval=frame", "-ac", "2", "-c:a", "pcm_s16le", str(d / "dialog_censored.wav"))
    _ff("-f", "lavfi", "-i", f"anoisesrc=c=brown:r=44100:d={seconds}:s=12:a=0.5",
        "-af", f"volume='0.12+0.84*between(t,{seconds * 0.4},{seconds * 0.4 + 3})':eval=frame", "-ac", "2", "-c:a", "pcm_s16le", str(d / "score_sfx.wav"))
    return d / "dialog_censored.wav", d / "score_sfx.wav"


def _job(d, **state):
    d.mkdir(parents=True, exist_ok=True)
    utils.write_job(d, {"job_id": "t", "steps_completed": [], **state})
    return d


# ── settings / validation ────────────────────────────────────────────────────

def test_yaml_bare_off_is_false_and_maps_back():
    assert yaml.safe_load("night_mode: off")["night_mode"] is False        # the trap
    for raw in ("off", "Off", "OFF", "false", "no"):
        cfg = _cfg(); cfg["audio_processing"]["night_mode"] = yaml.safe_load(f"x: {raw}")["x"]
        assert ap.settings(cfg).night_mode == "off", raw
    ap.validate(cfg)


def test_validate_rejects_bad_values():
    bad = [dict(enabled="yes"), dict(boost=20), dict(boost="loud"), dict(night="extreme"), dict(night=True),
           dict(target=-5), dict(target="x"), dict(ceiling=0), dict(ceiling=-30)]
    for kw in bad:
        try:
            ap.validate(_cfg(**kw))
        except utils.ConfigError:
            continue
        raise AssertionError(f"{kw} should have been rejected")
    ap.validate(_cfg(boost=6, night="medium", target=-24))
    ap.validate(_cfg(target=None))


def test_disabled_means_everything_inert():
    cfg = _cfg(enabled=False, boost=6, night="high", target=-20)
    s = ap.settings(cfg)
    assert (s.center_boost_db, s.night_mode, s.target_lufs, s.mastering_active) == (0.0, "off", None, False)
    assert ap.plan_downmix(cfg, "5.1(side)", 6)["method"] == "ffmpeg_default"
    assert "disabled" in ap.summary(cfg)


# ── Step 1b: the downmix matrix ──────────────────────────────────────────────

def test_matrix_rows_sum_to_one_and_boost_is_relative():
    for layout in ap._LAYOUT_CHANNELS:
        for db in (0, 3, 6, -3):
            _, m = ap.build_downmix_filter(layout, db)
            for row in m.values():
                assert abs(sum(row.values()) - 1.0) < 1e-5, (layout, db, row)
            ratio = m["FL"]["FC"] / m["FL"]["FL"]
            assert abs(ratio - 0.70710678 * 10 ** (db / 20)) < 1e-4, (layout, db, ratio)


def test_zero_boost_matrix_reproduces_ffmpegs_own_downmix():
    """The reason _LAYOUT_CHANNELS is an allow-list: every entry must match plain -ac 2 to within 1-2 LSB."""
    tmp = Path(tempfile.mkdtemp())
    try:
        for layout in ap._LAYOUT_CHANNELS:
            src = tmp / "src.wav"
            _multichannel(src, layout)
            _ff("-i", str(src), "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(tmp / "a.wav"))
            filt, _ = ap.build_downmix_filter(layout, 0.0)
            _ff("-i", str(src), "-af", filt, "-ar", "44100", "-c:a", "pcm_s16le", str(tmp / "b.wav"))
            d = _max_diff(tmp / "a.wav", tmp / "b.wav")
            assert d <= 2, f"{layout}: explicit matrix differs from -ac 2 by {d} LSB"
    finally:
        shutil.rmtree(tmp)


def test_boost_is_relative_and_matches_the_documented_numbers():
    """5.1(side), +6 dB: center +3.8 dB, front L/R -2.2 dB in absolute terms, 6.0 dB apart relative to each other."""
    tmp = Path(tempfile.mkdtemp())
    try:
        res = {}
        for tone in ("FC", "FL"):
            _multichannel(tmp / "src.wav", "5.1(side)", seconds=6, tone_on=tone)
            _ff("-i", str(tmp / "src.wav"), "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(tmp / "d0.wav"))
            filt, _ = ap.build_downmix_filter("5.1(side)", 6.0)
            _ff("-i", str(tmp / "src.wav"), "-af", filt, "-ar", "44100", "-c:a", "pcm_s16le", str(tmp / "d6.wav"))
            res[tone] = (_lufs(tmp / "d0.wav")[0], _lufs(tmp / "d6.wav")[0])
        fc_abs, fl_abs = res["FC"][1] - res["FC"][0], res["FL"][1] - res["FL"][0]
        assert abs(fc_abs - 3.8) < 0.25, fc_abs
        assert abs(fl_abs + 2.2) < 0.25, fl_abs
        assert abs((fc_abs - fl_abs) - 6.0) < 0.2, fc_abs - fl_abs
    finally:
        shutil.rmtree(tmp)


def test_plan_falls_back_for_layouts_without_a_matrix():
    cfg = _cfg(boost=6)
    for layout, ch, warn in (("stereo", 2, False), ("mono", 1, False), ("7.1(wide)", 8, True), ("unknown", 6, True), (None, 6, True)):
        p = ap.plan_downmix(cfg, layout, ch)
        assert p["method"] == "ffmpeg_default" and p["filter"] is None and p["center_boost_db_applied"] == 0.0
        assert p["notable"] and p["warn"] == warn, (layout, p)
    assert not ap.plan_downmix(_cfg(boost=0), "5.1", 6)["notable"]          # nothing asked for -> nothing to say
    assert ap.plan_downmix(_cfg(boost=6), "5.1", 6)["method"] == "pan_center_boost"


def test_downmix_to_stereo_integration():
    tmp = Path(tempfile.mkdtemp())
    try:
        out = {}
        for name, cfg in (("off", _cfg(enabled=False, boost=6)), ("zero", _cfg(boost=0)), ("on", _cfg(boost=6))):
            d = tmp / name
            _job(d, audio={"raw_file": "audio_raw.wav", "channels": 6, "channel_layout": "5.1(side)", "source_duration_sec": 6.0})
            _multichannel(d / "audio_raw.wav", "5.1(side)", seconds=6, tone_on="FC")
            extract.downmix_to_stereo(d, cfg, alog)
            st = utils.read_job(d)
            assert "1b_downmix" in st["steps_completed"] and (d / "audio_stereo.wav").exists()
            out[name] = (st["downmix"], _lufs(d / "audio_stereo.wav")[0])
        assert out["off"][0]["method"] == out["zero"][0]["method"] == "ffmpeg_default"
        assert abs(out["off"][1] - out["zero"][1]) < 0.01                       # disabled == 0 dB == today
        on = out["on"][0]
        assert on["method"] == "pan_center_boost" and on["center_boost_db_applied"] == 6.0 and on["source_layout"] == "5.1(side)"
        assert on["filter"].startswith("pan=stereo|FL=") and "matrix" in on
        assert abs((out["on"][1] - out["zero"][1]) - 3.8) < 0.25                # centre-only content: +3.8 dB absolute
        # resume path is untouched: second call verifies and returns
        extract.downmix_to_stereo(tmp / "on", _cfg(boost=6), alog)
    finally:
        shutil.rmtree(tmp)


# ── Step 6: mix & master ─────────────────────────────────────────────────────

def test_master_never_lowers_and_is_bit_identical_when_nothing_to_do():
    tmp = Path(tempfile.mkdtemp())
    try:
        d, s = _stems(tmp)
        _ff("-i", str(d), "-i", str(s), "-filter_complex", "amix=inputs=2:duration=first:normalize=0", "-c:a", "pcm_s16le", str(tmp / "plain.wav"))
        rec = ap.mix_and_master(d, s, tmp / "m.wav", ap.settings(_cfg(target=-60)), alog)      # already far above -60
        assert rec["gain_db"] == 0.0 and "limiter_delay_samples" not in rec
        assert _max_diff(tmp / "plain.wav", tmp / "m.wav") == 0
    finally:
        shutil.rmtree(tmp)


def test_master_hits_target_honors_ceiling_and_keeps_length_and_alignment():
    tmp = Path(tempfile.mkdtemp())
    try:
        d, s = _stems(tmp)
        before = _lufs(tmp / "score_sfx.wav")
        rec = ap.mix_and_master(d, s, tmp / "m.wav", ap.settings(_cfg(target=-24, ceiling=-3.0)), alog)
        i, tp, _ = _lufs(tmp / "m.wav")
        assert rec["gain_db"] > 0 and rec["mastering"]
        assert abs(i - (-24)) < 0.5 or rec["limiter_removes_up_to_db"] > 0, (i, rec)
        assert tp <= -3.0 + 0.7, tp                                              # sample-peak limiter: true peak within ~0.7 dB
        assert len(_pcm(tmp / "m.wav")) == len(_pcm(d))                          # exact length (delay trimmed + tail padded)
        assert rec["output_lufs"] is not None and rec["limiter_delay_samples"] > 100
    finally:
        shutil.rmtree(tmp)


def test_limiter_delay_is_compensated_sample_exactly():
    """An impulse through mix_and_master's limiter chain lands on the same sample it went in on."""
    tmp = Path(tempfile.mkdtemp())
    try:
        _ff("-f", "lavfi", "-i", "aevalsrc='if(eq(n,88200),0.5,0)|if(eq(n,88200),0.5,0)':s=44100:d=4", "-c:a", "pcm_s16le", str(tmp / "imp.wav"))
        delay = ap.measure_limiter_delay(5, 100, 44100, alog)
        _ff("-i", str(tmp / "imp.wav"), "-af",
            f"alimiter=limit=0.9:attack=5:release=100:level=0,atrim=start_sample={delay},asetpts=N/SR/TB,apad=pad_len={delay}",
            "-c:a", "pcm_s16le", str(tmp / "o.wav"))
        a, b = _pcm(tmp / "imp.wav"), _pcm(tmp / "o.wav")
        assert len(a) == len(b)
        assert max(range(0, len(a), 2), key=lambda k: abs(a[k])) == max(range(0, len(b), 2), key=lambda k: abs(b[k]))
    finally:
        shutil.rmtree(tmp)


def test_night_mode_reduces_range_and_keeps_average_when_no_target():
    tmp = Path(tempfile.mkdtemp())
    try:
        d, s = _stems(tmp)
        ap.mix_and_master(d, s, tmp / "x.wav", ap.settings(_cfg(night="medium")), alog)
        _ff("-i", str(d), "-i", str(s), "-filter_complex", "amix=inputs=2:duration=first:normalize=0", "-c:a", "pcm_s16le", str(tmp / "plain.wav"))
        (i0, tp0, lra0), (i1, tp1, lra1) = _lufs(tmp / "plain.wav"), _lufs(tmp / "x.wav")
        assert lra1 < lra0 - 1.0, (lra0, lra1)                                   # dynamic range down
        assert abs(i1 - i0) < 1.0, (i0, i1)                                      # makeup keeps the average
        assert tp1 < tp0, (tp0, tp1)                                             # loud peaks down
    finally:
        shutil.rmtree(tmp)


def test_recombine_integration_plain_and_mastered():
    tmp = Path(tempfile.mkdtemp())
    try:
        res = {}
        for name, cfg in (("plain", _cfg(enabled=False, target=-20)), ("master", _cfg(target=-24))):
            j = tmp / name
            d, s = _stems(_job(j), seconds=30)
            st = utils.read_job(j)
            st["total_duration_sec"] = 30.0
            st["mute"] = {"dialog_censored_sha256": utils.sha256_file(d)}
            st["merge_audio"] = {"score_sfx_sha256": utils.sha256_file(s)}
            utils.write_job(j, st)
            recombine.recombine(j, d, s, cfg, alog)
            rec = utils.read_job(j)["recombine"]
            assert "6_recombine" in utils.read_job(j)["steps_completed"] and rec["audio_censored_sha256"]
            res[name] = (rec["audio_processing"], j / "audio_censored.wav")
        assert res["plain"][0]["mastering"] is False and res["plain"][0]["night_mode"] == "off" and res["plain"][0]["target_lufs"] is None
        assert res["master"][0]["mastering"] is True and res["master"][0]["target_lufs"] == -24.0
        assert _lufs(res["master"][1])[0] > _lufs(res["plain"][1])[0] + 3
    finally:
        shutil.rmtree(tmp)


# ── settings log / drift ─────────────────────────────────────────────────────

def test_drift_detection():
    audio = {"channel_layout": "5.1(side)", "channels": 6}
    built = {"audio": audio, "downmix": {"center_boost_db_applied": 0.0},
             "recombine": {"audio_processing": ap.plain_record(ap.settings(_cfg(enabled=False)))}}
    done = ["1b_downmix", "6_recombine"]
    assert ap.drift_messages(built, _cfg(enabled=False), done) == []
    assert ap.drift_messages(built, _cfg(boost=0), done) == []                   # enabled but inert: no change
    assert ap.drift_messages(built, _cfg(boost=3, target=-24), []) == []         # nothing complete -> nothing to warn about
    msgs = ap.drift_messages(built, _cfg(boost=3, target=-24), done)
    assert len(msgs) == 2 and "--redo-audio" in msgs[0] and "--redo-step 6_recombine" in msgs[1]
    only_master = ap.drift_messages(built, _cfg(night="low"), done)
    assert len(only_master) == 1 and "night_mode=low" in only_master[0]
    legacy = {"audio": audio}                                                    # job from before this feature: no records at all
    assert ap.drift_messages(legacy, _cfg(enabled=False), done) == []
    assert len(ap.drift_messages(legacy, _cfg(boost=3), done)) == 1
    stereo = {"audio": {"channel_layout": "stereo", "channels": 2}, "downmix": {}}
    assert ap.drift_messages(stereo, _cfg(boost=6), ["1b_downmix"]) == []        # not applicable to stereo -> no drift
    rec_after = {"audio": audio, "downmix": {"center_boost_db_applied": 3.0},
                 "recombine": {"audio_processing": {"mastering": True, "night_mode": "off", "target_lufs": -24.0, "ceiling_dbfs": -2.0}}}
    assert ap.drift_messages(rec_after, _cfg(boost=3, target=-24), done) == []


def test_summary_and_completion_lines():
    assert "-24.0 LUFS" in ap.summary(_cfg(boost=3, target=-24)) and "+3.0 dB" in ap.summary(_cfg(boost=3, target=-24))
    st = {"downmix": {"center_boost_db_applied": 3.0, "source_layout": "5.1"},
          "recombine": {"audio_processing": {"mastering": True, "night_mode": "low", "gain_db": 9.1, "target_lufs": -24.0,
                                             "output_lufs": -24.1, "output_true_peak_dbtp": -2.3, "limiter_removes_up_to_db": 0.9}}}
    lines = "\n".join(ap.completion_lines(st))
    assert "+3.0 dB" in lines and "night_mode=low" in lines and "-24.1 LUFS" in lines
    assert "plain" in "\n".join(ap.completion_lines({}))


# ── --redo-audio ─────────────────────────────────────────────────────────────

def _finished_job(d):
    d.mkdir()
    raw = d / "audio_raw.ac3"
    raw.write_bytes(b"original audio bytes")
    for name in ("audio_stereo.wav", "audio_stereo_01.wav", "dialog.wav", "score_sfx.wav", "dialog_01.wav", "score_sfx_01.wav",
                 "dialog_censored.wav", "audio_censored.wav", "audio_encoded.mka", "dialog_transcribe_01.wav",
                 "transcript.json", "matches.json", "review.json", "censor_log.json", "transcript.srt", "transcript_01.json"):
        (d / name).write_text("x")
    (d / ".demucs_work").mkdir()
    (d / ".demucs_work" / "junk").write_text("x")
    steps = ["1a_extract_raw", "1b_downmix", "1c_segment", "2_separate", "2b_merge_audio", "3_transcribe", "3b_merge", "4b_flag",
             "4b_review", "5_mute", "6_recombine", "6b_encode", "6c_transcript_srt", "7_mux"]
    utils.write_job(d, {"job_id": "t", "steps_completed": steps,
                        "audio": {"raw_file": raw.name, "source_audio_sha256": utils.sha256_file(raw)}})
    return d


def test_prepare_audio_redo():
    tmp = Path(tempfile.mkdtemp())
    try:
        d = _finished_job(tmp / "job")
        assert ap.audio_merged(utils.read_job(d)["steps_completed"], utils.read_job(d))
        removed = ap.prepare_audio_redo(d, utils.read_job(d), alog)
        st = utils.read_job(d)
        for gone in ("audio_stereo.wav", "audio_stereo_01.wav", "dialog.wav", "score_sfx.wav", "dialog_01.wav", "score_sfx_01.wav",
                     "dialog_censored.wav", "audio_censored.wav", "audio_encoded.mka", ".demucs_work"):
            assert not (d / gone).exists(), gone
        for kept in ("audio_raw.ac3", "dialog_transcribe_01.wav", "transcript.json", "matches.json", "review.json",
                     "censor_log.json", "transcript.srt", "transcript_01.json"):
            assert (d / kept).exists(), kept
        assert set(st["steps_completed"]) == {"1a_extract_raw", "3_transcribe", "3b_merge", "4b_flag", "4b_review", "6c_transcript_srt"}
        assert ap.redo_pending(st) and "dialog.wav" in removed
        # the legacy "3b_merge implies audio merged" shortcut must not fire while pending ...
        assert not ap.audio_merged(st["steps_completed"], st)
        # ... and does again once the chain is finished and the marker cleared
        utils.mark_step_done(d, "2b_merge_audio")
        assert ap.audio_merged(utils.read_job(d)["steps_completed"], utils.read_job(d))
        ap.clear_redo_pending(d)
        assert not ap.redo_pending(utils.read_job(d))
        ap.clear_redo_pending(d)                                                  # idempotent
        # legacy job: 3b_merge only (no 2b marker, nothing pending) still counts as merged
        assert ap.audio_merged(["1a_extract_raw", "3b_merge"], {})
        assert not ap.audio_merged(["1a_extract_raw"], {})
    finally:
        shutil.rmtree(tmp)


def test_prepare_audio_redo_refuses_without_changing_anything():
    tmp = Path(tempfile.mkdtemp())
    try:
        d = _finished_job(tmp / "a")
        (d / "audio_raw.ac3").write_bytes(b"tampered")
        before = sorted(p.name for p in d.iterdir())
        for case in ("hash", "no_raw", "no_matches", "no_flag"):
            d2 = _finished_job(tmp / case)
            if case == "hash":
                (d2 / "audio_raw.ac3").write_bytes(b"tampered")
            elif case == "no_raw":
                (d2 / "audio_raw.ac3").unlink()
            elif case == "no_matches":
                (d2 / "matches.json").unlink()
            else:
                utils.unmark_step_done(d2, "4b_flag")
            snap = sorted(p.name for p in d2.iterdir())
            try:
                ap.prepare_audio_redo(d2, utils.read_job(d2), alog)
            except ap.RedoAudioError:
                pass
            else:
                raise AssertionError(case)
            assert sorted(p.name for p in d2.iterdir()) == snap, case              # nothing deleted
            assert "dialog.wav" in snap
        assert before == sorted(p.name for p in d.iterdir())
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    for fn in [
        test_yaml_bare_off_is_false_and_maps_back,
        test_validate_rejects_bad_values,
        test_disabled_means_everything_inert,
        test_matrix_rows_sum_to_one_and_boost_is_relative,
        test_zero_boost_matrix_reproduces_ffmpegs_own_downmix,
        test_boost_is_relative_and_matches_the_documented_numbers,
        test_plan_falls_back_for_layouts_without_a_matrix,
        test_downmix_to_stereo_integration,
        test_master_never_lowers_and_is_bit_identical_when_nothing_to_do,
        test_master_hits_target_honors_ceiling_and_keeps_length_and_alignment,
        test_limiter_delay_is_compensated_sample_exactly,
        test_night_mode_reduces_range_and_keeps_average_when_no_target,
        test_recombine_integration_plain_and_mastered,
        test_drift_detection,
        test_summary_and_completion_lines,
        test_prepare_audio_redo,
        test_prepare_audio_redo_refuses_without_changing_anything,
    ]:
        _run(fn)
    print("\nALL TESTS PASSED")

import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "replay_zones", pathlib.Path(__file__).resolve().parent.parent / "tools" / "replay_zones.py")
rz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rz)


def _line(age, score, start=100, thr=0.45, action="drop"):
    return (f"2026-09-06 10:45:02,021 tapo_monitor.monitor INFO audit camera=cam1 path=sampler "
            f"action={action} etype=motion start={start} event_age_s={age} score={score} "
            f"person_score={score} animal_score=0.0 threshold={thr} reason=x")


def test_parse_groups_and_orders_frames():
    groups = rz.parse_audit([_line(60, 0.1), _line(30, 0.9), _line(5, 0.1, start=7)])
    assert [f["age"] for f in groups[("cam1", "100")]] == [30.0, 60.0]
    assert ("cam1", "7") in groups


def test_early_exit_lines_are_not_frames():
    assert not rz.parse_audit([_line(30, 0.1, action="early_exit")])


def test_loiter_fires_once_per_group():
    frames = [{"age": 30.0 * i, "score": 0.9, "thr": 0.45} for i in range(1, 9)]
    assert rz.replay_loiter(frames, 4, 0, 30)[0] == 3
    assert rz.replay_loiter(frames[:3], 4, 0, 30) is None


def test_loiter_break_and_tolerance():
    scores = [0.9, 0.9, 0.0, 0.9, 0.9]
    frames = [{"age": 30.0 * i, "score": s, "thr": 0.45} for i, s in enumerate(scores)]
    assert rz.replay_loiter(frames, 4, 0, 30) is None
    assert rz.replay_loiter(frames, 4, 1, 30)[0] == 4


def test_feet_uses_bottom_centre_and_min_score():
    rec = {"w": 100, "h": 200, "person_scores": [0.9, 0.2],
           "person_boxes": [[10, 20, 30, 100], [0, 0, 10, 10]]}
    assert rz.feet_of(rec, 0.3) == [(0.9, 0.2, 0.5)]

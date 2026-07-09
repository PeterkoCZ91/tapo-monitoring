from tapo_monitor import audit


def test_parse_audit_line():
    line = (
        "2026 tapo_monitor.monitor INFO audit camera=front path=live action=drop "
        "etype=motion start=100 score=0.1234 threshold=0.4000 reason=below_threshold"
    )

    rec = audit.parse_audit_line(line)

    assert rec["camera"] == "front"
    assert rec["action"] == "drop"
    assert rec["score"] == 0.1234
    assert rec["threshold"] == 0.4


def test_summarize_counts_camera_detections_and_telegram():
    lines = [
        "audit camera=front path=getevents action=detect etype=person start=100",
        "audit camera=front path=live action=drop etype=motion start=101 "
        "score=0.10 threshold=0.40 reason=below_threshold",
        "audit camera=front path=sampler action=send etype=person start=100 "
        "score=0.70 threshold=0.40 telegram=true",
        "audit camera=front path=live action=scorer_unavailable etype=motion start=102",
    ]

    summary = audit.summarize(lines)["front"]
    text = audit.format_summary({"front": summary})

    assert summary.detections == 1
    assert summary.dropped_below_threshold == 1
    assert summary.telegram_ok == 1
    assert summary.scorer_unavailable == 1
    assert "detections=1" in text
    assert "telegram_ok=1" in text
    assert "dropped max=0.10" in text


def test_parse_quoted_camera_name():
    rec = audit.parse_audit_line(
        "audit camera=\"front yard\" path=live action=send etype=person start=1 telegram=true"
    )

    assert rec["camera"] == "front yard"
    assert audit.summarize(["audit camera=\"front yard\" path=getevents action=detect etype=person start=1"])[
        "front yard"
    ].detections == 1

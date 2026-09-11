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


def test_delivery_paths_distinguish_attempts_events_and_rescue():
    records = [
        "audit camera=front path=getevents action=detect etype=person start=100",
        "audit camera=front path=getevents action=detect etype=person start=100",
        "audit camera=front path=live action=defer etype=person start=100",
        "audit camera=front path=sd action=send etype=person start=100 telegram=true event_age_s=120",
        "audit camera=front path=sd action=send etype=person start=100 telegram=true event_age_s=140",
        "audit camera=front path=live action=send etype=person start=200 telegram=false event_age_s=5",
        "audit camera=front path=sd action=send etype=person start=300 event_age_s=7",
        "audit camera=front path=sd action=drop etype=person start=400 reason=panlimit_window",
        "audit camera=front path=sd action=drop etype=person start=400 reason=panlimit_window",
    ]
    summary = audit.summarize(records)["front"]
    report = audit.summary_data({"front": summary})["front"]
    assert report["detected_events"] == 1
    assert report["delivered_events"] == 1
    assert report["sd_rescued_events"] == 1
    assert report["panlimit_frames"] == 2
    assert report["panlimit_events"] == 1
    assert report["telegram_ok"] == 2
    assert report["telegram_failed"] == 1
    assert report["telegram_unknown"] == 1
    assert report["paths"]["sd"] == {"delivered": 2, "failed": 0, "unknown": 1,
                                        "latency_p50_s": 130.0, "latency_p95_s": 139.0}


def test_sd_rescue_excludes_events_already_delivered_live_regardless_of_order():
    records = [
        "audit camera=front path=sd action=send etype=motion start=100 telegram=true event_age_s=-1",
        "audit camera=front path=live action=defer etype=motion start=100",
        "audit camera=front path=sampler action=send etype=motion start=100 telegram=true event_age_s=nan",
    ]
    report = audit.summary_data(audit.summarize(records))["front"]
    assert report["sd_rescued_events"] == 0
    assert report["delivered_events"] == 1
    assert report["paths"]["sd"]["latency_p50_s"] is None
    assert report["paths"]["sampler"]["latency_p50_s"] is None


def test_audit_json_cli(tmp_path, capsys):
    import json
    log = tmp_path / "journal.log"
    log.write_text("audit camera=front path=live action=send etype=person start=1 telegram=true event_age_s=25")
    assert audit.main([str(log), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["front"]["paths"]["live"]["latency_p50_s"] == 25

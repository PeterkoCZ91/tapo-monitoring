from tapo_monitor import alarminfo as ai
from tapo_monitor import detection

NOW = 1_700_000_100.0
T = 1_700_000_000


def good(t=T, typ="motion"):
    return {"system": {"last_alarm_info": {"last_alarm_type": typ,
                                           "last_alarm_time": str(t)}}}


def test_parse_documented_shape():
    (ev,) = ai.parse_last_alarm(good(), now=NOW)
    assert ev["start_time"] == T and ev["end_time"] is None
    assert ev["event_type"] == "motion"
    assert detection.classify_getevent(ev["event_type"]) == "motion"


def test_person_type_classifies_person():
    (ev,) = ai.parse_last_alarm(good(typ="Person"), now=NOW)
    assert detection.classify_getevent(ev["event_type"]) == "person"


def test_other_shapes():
    info = {"last_alarm_type": "motion", "last_alarm_time": T}
    for resp in (info, {"last_alarm_info": info}, {"result": {"system": {"last_alarm_info": info}}}):
        assert len(ai.parse_last_alarm(resp, now=NOW)) == 1


def test_unknown_or_bad_shapes():
    bad = [None, [], "x", {}, {"system": {}}, {"system": {"last_alarm_info": {}}},
           good(t=0), good(t="abc"), good(typ=""), good(t=T * 1000),
           {"msg_alarm": {"chn1_msg_alarm_info": {"alarm_type": "0"}}}]
    for r in bad:
        assert ai.parse_last_alarm(r, now=NOW) == []


def test_stale_and_future():
    assert ai.parse_last_alarm(good(), now=T + 601) == []
    assert ai.parse_last_alarm(good(), now=T + 599)
    assert ai.parse_last_alarm(good(t=T + 10_000), now=NOW) == []


def test_dedupe_and_bound():
    seen = ai.SeenAlarms(maxlen=2)
    ev = ai.parse_last_alarm(good(), now=NOW)
    assert ai.dedupe(ev, seen) == ev
    assert ai.dedupe(ev, seen) == []
    for t in (T + 1, T + 2):
        ai.dedupe(ai.parse_last_alarm(good(t=t), now=NOW), seen)
    assert len(seen) == 2
    assert ai.dedupe(ev, seen) == ev  # evicted, so new again


class Fake:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def executeFunction(self, method, params):
        self.calls.append((method, params))
        if self.exc:
            raise self.exc
        return self.resp


def test_events_without_sd_newer_only():
    c = Fake(good())
    assert len(ai.events_without_sd(c, T - 1, now=NOW)) == 1
    assert c.calls == [("getLastAlarmInfo", ai.ALARM_PARAMS)]
    assert ai.events_without_sd(c, T, now=NOW) == []


def test_events_without_sd_errors_swallowed():
    assert ai.events_without_sd(Fake(exc=RuntimeError("-71114")), 0, now=NOW) == []
    assert ai.events_without_sd(Fake({"junk": 1}), 0, now=NOW) == []


def test_fallback_to_getlastalarminfo_method():
    class C:
        def getLastAlarmInfo(self):
            return good()
    assert len(ai.events_without_sd(C(), 0, now=NOW)) == 1

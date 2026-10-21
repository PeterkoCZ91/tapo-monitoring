"""Regression tests for the storage-layer hardening (health, hub, sent log, outbox, ...)."""

import json
import os
import stat
import threading
import types

import pytest

from tapo_monitor import health, hubclient, incident, ledger, outbox, runtime_state, sentlog

T0 = 1_790_000_000.0


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ── health ───────────────────────────────────────────────────────────────────
def _health_state(**overrides):
    values = {name: {} for name in health.PERSISTED_FIELDS}
    values.update(overrides)
    return types.SimpleNamespace(**values)


@pytest.mark.parametrize("field", ["online_since", "last_seen", "total_observed_online"])
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_health_rejects_non_finite_numbers(tmp_path, bad, field):
    path = tmp_path / "health.json"
    path.write_text(f'{{"version":1,"state":{{"{field}":{{"front":{bad}}}}}}}')
    state = _health_state(**{field: {"front": 1.0}})
    assert health.load_state(str(path), state) is False
    assert getattr(state, field) == {"front": 1.0}       # nothing half-applied


def test_health_save_tolerates_unserializable_state(tmp_path):
    state = _health_state(active_scorers={"a": "not-a-number"})
    assert health.save_state(str(tmp_path / "h.json"), state) is False
    assert [p.name for p in tmp_path.iterdir()] == []      # temp file cleaned up


# ── hub client ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("item", [None, 5, "x", [1]])
def test_unwrap_non_dict_response_is_empty(item):
    assert hubclient.unwrap({"result": {"responses": [item]}}) == (None, {})


def test_query_survives_non_dict_response():
    class Session:
        def send(self, method, params):
            return {"result": {"responses": ["garbage"]}}

    client = hubclient.HubClient(lambda: Session())
    assert client.query("getX", {}, 0) == (None, {})


# ── sent log ─────────────────────────────────────────────────────────────────
def test_sentlog_files_and_dirs_are_private(tmp_path):
    root = tmp_path / "sent"
    path = sentlog.archive_sent(str(root), b"jpeg", "cap", now=T0)
    assert _mode(root) == 0o700
    assert _mode(path) == 0o600
    assert _mode(root / sentlog.INDEX_NAME) == 0o600
    review = sentlog.archive_review_frame(str(tmp_path / "rev"), b"j",
                                          {"camera": "yard"}, now=T0, retention_days=1)
    assert _mode(review) == 0o600 and _mode(tmp_path / "rev") == 0o700


def test_prune_old_keeps_foreign_files(tmp_path):
    old = T0 - 30 * 86400
    ours = sentlog.archive_sent(str(tmp_path), b"j", "c", now=old, retention_days=1000)
    foreign = tmp_path / "family-photo.jpg"
    foreign.write_bytes(b"precious")
    for p in (ours, foreign):
        os.utime(p, (old, old))
    assert sentlog.prune_old(str(tmp_path), T0, 7) == 1
    assert not os.path.exists(ours) and foreign.exists()


# ── outbox ───────────────────────────────────────────────────────────────────
def _capture(box, tmp_path, n=0, **kw):
    img = tmp_path / "frame.jpg"
    img.write_bytes(b"\xff\xd8JPEG")
    return box.capture(camera="yard", image=str(img), caption="c", failed_at=T0 + n,
                       incident=f"yard-{int(T0) + n}", etype="person", **kw)


def test_outbox_files_are_private(tmp_path):
    box = outbox.Outbox(str(tmp_path / "box"))
    key = _capture(box, tmp_path)
    assert _mode(tmp_path / "box") == 0o700
    assert _mode(box._path(key, outbox._IMAGE)) == 0o600
    assert _mode(box._path(key, outbox._META)) == 0o600


def test_outbox_temp_names_are_unique(tmp_path, monkeypatch):
    box = outbox.Outbox(str(tmp_path))
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (seen.append(a), real(a, b))[1])
    box._write_atomic(str(tmp_path / "x.json"), b"1")
    box._write_atomic(str(tmp_path / "x.json"), b"2")
    assert len(set(seen)) == 2 and all(s != str(tmp_path / "x.json.tmp") for s in seen)


def test_outbox_concurrent_updates_do_not_lose_each_other(tmp_path):
    box = outbox.Outbox(str(tmp_path / "box"))
    key = _capture(box, tmp_path)
    entry = box.entries()[0]
    errors = []

    def worker(n):
        try:
            for i in range(30):
                box.update(entry, **{f"f{n}": i})
        except Exception as exc:     # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    meta = box._read_meta(key)
    assert not errors and all(meta[f"f{n}"] == 29 for n in range(4))


def test_summary_intent_is_persisted_before_the_send(tmp_path):
    box = outbox.Outbox(str(tmp_path / "box"), min_age=0)
    key = _capture(box, tmp_path)
    seen = {}

    def send_text(text):
        seen["announced"] = box._read_meta(key)["announced"]
        return False

    outbox.drain(box, now=T0 + 10_000, known_cameras={"yard"}, send_text=send_text,
                 send_photo=lambda e, c: True, archive_review=lambda e, m: None,
                 score_for=lambda cam: None, threshold_for=lambda cam, ts: 0.5,
                 probe=lambda: True)
    assert seen["announced"] is True                    # recorded before sending
    assert box._read_meta(key)["announced"] is False    # taken back when it failed


def _drain(box, now, send_text):
    return outbox.drain(box, now=now, known_cameras={"yard"}, send_text=send_text,
                        send_photo=lambda e, c: True, archive_review=lambda e, m: None,
                        score_for=lambda cam: None, threshold_for=lambda cam, ts: 0.5,
                        probe=lambda: True)


def test_raising_summary_send_reverts_the_intent(tmp_path):
    box = outbox.Outbox(str(tmp_path / "box"), min_age=0)
    key = _capture(box, tmp_path)

    assert box._read_meta(key)["announced"] is False     # before the send
    during = []

    def boom(text):
        during.append(box._read_meta(key)["announced"])
        raise RuntimeError("telegram down")

    with pytest.raises(RuntimeError):
        _drain(box, T0 + 10_000, boom)
    assert during == [True]                              # intent was recorded for the send
    assert box._read_meta(key)["announced"] is False     # and taken back after the raise


def test_unconfirmed_announce_is_resent_after_the_wait(tmp_path):
    box = outbox.Outbox(str(tmp_path / "box"), min_age=0)
    _capture(box, tmp_path)
    now = T0 + 10_000
    box.update(box.entries()[0], announced=True, announce_started_at=now - 10,
               announce_sent=False)           # a crash left this behind
    texts = []
    _drain(box, now, lambda t: texts.append(t) or True)
    assert texts == []                         # still within the wait
    _capture(box, tmp_path)             # the first drain delivered and removed it
    box.update(box.entries()[0], announced=True, announce_started_at=now - 600,
               announce_sent=False)
    _drain(box, now, lambda t: texts.append(t) or True)
    assert len(texts) == 1


def test_remove_drops_the_key_lock(tmp_path):
    box = outbox.Outbox(str(tmp_path / "box"))
    key = _capture(box, tmp_path)
    assert key in box._key_locks
    box.remove(key)
    assert key not in box._key_locks


# ── incident CLI ─────────────────────────────────────────────────────────────
def test_incident_cli_does_not_create_a_ledger(tmp_path, capsys):
    missing = tmp_path / "state" / "events.sqlite3"
    assert incident.main(["yard-1710003600", "--ledger", str(missing)]) == 0
    assert not missing.exists() and not missing.parent.exists()


def test_incident_cli_reads_an_existing_ledger_without_writing(tmp_path, capsys):
    events = ledger.EventLedger(tmp_path / "events.sqlite3")
    start = 1710003600.0
    events.record_camera_event(camera="yard", event_type="person", event_at=start,
                               observed_at=start + 4)
    directory = os.path.dirname(events.path)
    snapshot = {n: (open(os.path.join(directory, n), "rb").read(),
                    os.stat(os.path.join(directory, n)).st_mtime_ns)
                for n in os.listdir(directory)}
    assert incident.main(["yard-1710003600", "--ledger", events.path, "--json"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert len(found["observations"]) == 1
    after = {n: (open(os.path.join(directory, n), "rb").read(),
                 os.stat(os.path.join(directory, n)).st_mtime_ns)
             for n in os.listdir(directory)}
    assert after == snapshot            # same files, same bytes, same mtimes


# ── runtime state ────────────────────────────────────────────────────────────
class _Churning(dict):
    """A dict whose first copies fail like one mutated by another thread."""

    def __init__(self, *a, fails=2):
        super().__init__(*a)
        self.fails = fails

    def _maybe_fail(self):
        if self.fails:
            self.fails -= 1
            raise RuntimeError("dictionary changed size during iteration")

    def items(self):          # direct iteration, as the unfixed code did
        self._maybe_fail()
        return super().items()

    def keys(self):           # what dict(<subclass>) goes through
        self._maybe_fail()
        return super().keys()


def test_snapshot_tolerates_concurrent_mutation():
    state = types.SimpleNamespace(
        pending_hub=[], pending_sd=[], last_alert=_Churning({("yard", "p"): 5.0}),
        last_event_start={}, privacy_announced=_Churning({"yard": True}),
        app_push_announced={}, detection_announced={})
    data = runtime_state.snapshot(state)
    assert data["last_alert"] == [["yard", "p", 5.0]]
    assert data["privacy_announced"] == {"yard": True}

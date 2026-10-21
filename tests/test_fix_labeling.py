"""Hardening of the label web server and LabelSession.undo."""

import http.client
import json
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tapo_monitor import labeling

from .test_labeling import _serve, dataset  # noqa: F401  (dataset is a fixture)

JSON = {"Content-Type": "application/json"}


@pytest.fixture
def served(dataset):  # noqa: F811
    session = labeling.LabelSession(dataset, seed=1)
    srv, url = _serve(session)
    yield session, srv
    srv.shutdown()
    srv.server_close()


def _request(srv, method, path, body=None, headers=None, raw_headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    try:
        conn.putrequest(method, path, skip_host=bool(headers and "Host" in headers))
        for key, value in {**(headers or {}), **(raw_headers or {})}.items():
            conn.putheader(key, value)
        data = body if isinstance(body, bytes) else json.dumps(body or {}).encode()
        if "Content-Length" not in (raw_headers or {}):
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        resp = conn.getresponse()
        return resp.status, resp, resp.read()
    finally:
        conn.close()


def test_post_requires_json_content_type(served):
    _session, srv = served
    status, _, _ = _request(srv, "POST", "/api/undo", headers={"Content-Type": "text/plain"})
    assert status == 415
    status, _, _ = _request(srv, "POST", "/api/undo")
    assert status == 415
    status, _, _ = _request(srv, "POST", "/api/undo", headers=JSON)
    assert status == 200


def test_post_rejects_foreign_origin(served):
    _session, srv = served
    port = srv.server_address[1]
    status, _, _ = _request(srv, "POST", "/api/undo",
                            headers={**JSON, "Origin": "http://evil.example"})
    assert status == 403
    status, _, _ = _request(srv, "POST", "/api/undo",
                            headers={**JSON, "Origin": f"http://127.0.0.1:{port}"})
    assert status == 200


def test_rebinding_host_is_rejected_on_get_and_post(served):
    _session, srv = served
    port = srv.server_address[1]
    bad = {"Host": f"evil.example:{port}"}
    assert _request(srv, "GET", "/api/next", headers=bad)[0] == 403
    assert _request(srv, "POST", "/api/undo", headers={**JSON, **bad})[0] == 403
    assert _request(srv, "GET", "/api/next", headers={"Host": "evil.example"})[0] == 403
    assert _request(srv, "GET", "/api/next", headers={"Host": f"localhost:{port}"})[0] == 200


def test_security_headers_on_page(served):
    _session, srv = served
    status, resp, _ = _request(srv, "GET", "/")
    assert status == 200
    assert resp.getheader("X-Frame-Options") == "DENY"
    csp = resp.getheader("Content-Security-Policy")
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'unsafe-inline'" in csp  # the page's own inline script must run


def test_oversized_body_is_413(served):
    _session, srv = served
    status, _, _ = _request(srv, "POST", "/api/label", headers=JSON,
                            raw_headers={"Content-Length": str(labeling.MAX_BODY + 1)})
    assert status == 413


@pytest.mark.parametrize("value,expected", [("-1", 400), ("abc", 400), ("", 400),
                                            (str(10**12), 413)])
def test_bad_content_length_is_400(served, value, expected):
    session, srv = served
    # A perfectly valid label body rides along: if the handler ever read it despite the
    # bad header, the verdict would be recorded (and the old code blocked on the read).
    body = json.dumps({"path": session._queue[0].path, "label": "person"}).encode()
    status, _, _ = _request(srv, "POST", "/api/label", body=body, headers=JSON,
                            raw_headers={"Content-Length": value})
    assert status == expected
    assert not session._labeled
    assert not session._history


def test_handler_sets_a_socket_timeout():
    assert labeling.REQUEST_TIMEOUT > 0
    assert 0 < labeling.make_server.__globals__["REQUEST_TIMEOUT"] <= 60


def test_stalled_body_times_out(dataset, monkeypatch):  # noqa: F811
    monkeypatch.setattr(labeling, "REQUEST_TIMEOUT", 0.3)
    session = labeling.LabelSession(dataset, seed=1)
    srv, _url = _serve(session)
    try:
        port = srv.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(f"POST /api/label HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                         "Content-Type: application/json\r\nContent-Length: 50\r\n\r\n".encode())
            reply = sock.recv(4096)  # body never sent: the server must answer, not hang
        assert reply.startswith(b"HTTP/1.0 408") or reply.startswith(b"HTTP/1.1 408")
    finally:
        srv.shutdown()
        srv.server_close()


def test_undo_write_failure_keeps_the_history_entry(dataset, monkeypatch):  # noqa: F811
    session = labeling.LabelSession(dataset, seed=1)
    path = session.pending()[0].path
    session.label(path, "person", now=1.0)
    real = session._append

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(session, "_append", broken)
    with pytest.raises(OSError):
        session.undo(now=2.0)
    assert session.state()["can_undo"] is True
    monkeypatch.setattr(session, "_append", real)
    assert session.undo(now=3.0) is not None
    assert session.state()["can_undo"] is False


def test_undo_skip_of_a_frame_labeled_elsewhere_does_not_raise(dataset):  # noqa: F811
    session = labeling.LabelSession(dataset, seed=1)
    path = session.pending()[0].path
    session.skip(path)
    # Another tab labels the same frame: it leaves the queue.
    session.label(path, "person", now=1.0)
    # History is [skip, label]: undo the label first, then the skip; use a clean case too.
    session._history.pop()  # drop the label entry, leaving the stale skip
    session.undo()
    assert [f.path for f in session._queue].count(path) <= 1
    assert session.state()["can_undo"] is False


def test_port_is_not_checked_so_port_forwards_work(served):
    _session, srv = served
    assert _request(srv, "GET", "/api/next", headers={"Host": "localhost:8080"})[0] == 200
    assert _request(srv, "POST", "/api/undo",
                    headers={**JSON, "Origin": "http://localhost:8080"})[0] == 200
    assert _request(srv, "POST", "/api/undo",
                    headers={**JSON, "Origin": "http://evil.example:8080"})[0] == 403


def test_allowed_hosts_env_adds_names(served, monkeypatch):
    _session, srv = served
    host = {"Host": "box.tail.example:8791"}
    assert _request(srv, "GET", "/api/next", headers=host)[0] == 403
    monkeypatch.setenv("TAPO_LABEL_ALLOWED_HOSTS", "other.example, Box.Tail.Example")
    assert _request(srv, "GET", "/api/next", headers=host)[0] == 200

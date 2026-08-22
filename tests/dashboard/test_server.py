from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from typing import cast

import pytest

from ses.dashboard import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    create_dashboard_server,
    load_status,
)
from ses.dashboard.server import DashboardHTTPServer


@contextmanager
def _running_server(workspace: Path) -> Iterator[tuple[str, int]]:
    server = create_dashboard_server(workspace, port=0)
    address = cast(tuple[str, int], server.server_address)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _request(
    address: tuple[str, int],
    method: str,
    target: str,
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection(address[0], address[1], timeout=3)
    try:
        connection.request(method, target)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _write_status(workspace: Path, value: object) -> Path:
    status = workspace / ".ses/status.json"
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return status


def _workspace_snapshot(workspace: Path) -> dict[str, bytes]:
    return {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_server_is_loopback_only_and_accepts_a_configurable_port(
    tmp_path: Path,
) -> None:
    assert DEFAULT_HOST == "127.0.0.1"
    assert DEFAULT_PORT == 8765

    server = create_dashboard_server(tmp_path, port=0)
    try:
        host, port = cast(tuple[str, int], server.server_address)
        assert host == DEFAULT_HOST
        assert port > 0
        assert isinstance(server, DashboardHTTPServer)
    finally:
        server.server_close()


def test_html_and_json_have_explicit_types_and_security_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-this-value-must-not-appear"
    monkeypatch.setenv("SILICONFLOW_API_KEY", secret)
    _write_status(
        tmp_path,
        {
            "overall_status": "running",
            "current_station": 2,
            "experiment_cost": {"amount": "0.31", "currency": "CNY"},
            "stations": [{"id": 0, "status": "complete"}],
        },
    )

    with _running_server(tmp_path) as address:
        html_status, html_headers, html_body = _request(address, "GET", "/")
        json_status, json_headers, json_body = _request(
            address, "GET", "/.ses/status.json?poll=1"
        )

    assert html_status == 200
    assert html_headers["Content-Type"] == "text/html; charset=utf-8"
    assert html_headers["X-Content-Type-Options"] == "nosniff"
    assert html_headers["X-Frame-Options"] == "DENY"
    assert html_headers["Referrer-Policy"] == "no-referrer"
    assert html_headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "connect-src 'self'" in html_headers["Content-Security-Policy"]
    assert json_status == 200
    assert json_headers["Content-Type"] == "application/json; charset=utf-8"
    assert json_headers["Cache-Control"] == "no-store"
    assert json_headers["X-Content-Type-Options"] == "nosniff"
    assert json.loads(json_body)["current_station"] == 2
    assert secret.encode() not in html_body
    assert secret.encode() not in json_body


def test_missing_and_invalid_status_render_safe_waiting_and_error_states(
    tmp_path: Path,
) -> None:
    missing = load_status(tmp_path)
    assert json.loads(missing.payload)["overall_status"] == "waiting"

    status = _write_status(tmp_path, {"api_key": "sk-never-serve-this"})
    invalid = load_status(tmp_path)
    assert json.loads(invalid.payload)["overall_status"] == "error"
    assert b"sk-never-serve-this" not in invalid.payload

    status.write_text("not-json", encoding="utf-8")
    malformed = load_status(tmp_path)
    assert json.loads(malformed.payload)["overall_status"] == "error"


def test_head_matches_get_headers_without_sending_a_body(tmp_path: Path) -> None:
    with _running_server(tmp_path) as address:
        get_status, get_headers, get_body = _request(address, "GET", "/")
        head_status, head_headers, head_body = _request(address, "HEAD", "/")

    assert get_status == head_status == 200
    assert get_headers["Content-Length"] == head_headers["Content-Length"]
    assert int(get_headers["Content-Length"]) == len(get_body)
    assert head_body == b""


def test_only_status_declared_dot_ses_artifacts_are_served(tmp_path: Path) -> None:
    report = tmp_path / ".ses/reports/baseline.html"
    report.parent.mkdir(parents=True)
    report.write_text("<!doctype html><title>Baseline</title>", encoding="utf-8")
    private = tmp_path / ".ses/private.json"
    private.write_text('{"private":true}', encoding="utf-8")
    source = tmp_path / "notes.md"
    source.write_text("not a dashboard artifact", encoding="utf-8")
    _write_status(
        tmp_path,
        {
            "stations": [
                {
                    "id": 0,
                    "status": "complete",
                    "reports": [
                        {"label": "基线报告", "path": ".ses/reports/baseline.html"}
                    ],
                }
            ]
        },
    )

    with _running_server(tmp_path) as address:
        ok_status, ok_headers, ok_body = _request(
            address, "GET", "/artifact/.ses/reports/baseline.html"
        )
        private_status, _, _ = _request(address, "GET", "/artifact/.ses/private.json")
        source_status, _, _ = _request(address, "GET", "/artifact/notes.md")
        direct_status, _, _ = _request(address, "GET", "/.ses/private.json")

    assert ok_status == 200
    assert ok_body == report.read_bytes()
    assert ok_headers["Content-Type"] == "text/html; charset=utf-8"
    assert "script-src 'none'" in ok_headers["Content-Security-Policy"]
    assert private_status == 404
    assert source_status == 403
    assert direct_status == 404


def test_canonical_artifact_ref_is_bound_to_its_declared_digest(
    tmp_path: Path,
) -> None:
    report = tmp_path / ".ses/reports/baseline.html"
    report.parent.mkdir(parents=True)
    original = b"<!doctype html><title>Canonical baseline</title>"
    report.write_bytes(original)
    _write_status(
        tmp_path,
        {
            "artifact_refs": [
                {
                    "root": "workspace",
                    "path": ".ses/reports/baseline.html",
                    "sha256": hashlib.sha256(original).hexdigest(),
                }
            ]
        },
    )

    with _running_server(tmp_path) as address:
        ok_status, _, ok_body = _request(
            address, "GET", "/artifact/.ses/reports/baseline.html"
        )
        replacement = b"secret content that is not covered by status"
        report.write_bytes(replacement)
        changed_status, _, changed_body = _request(
            address, "GET", "/artifact/.ses/reports/baseline.html"
        )

    assert ok_status == 200
    assert ok_body == original
    assert changed_status == 409
    assert replacement not in changed_body


def test_malformed_canonical_artifact_ref_is_not_downgraded_to_path_only(
    tmp_path: Path,
) -> None:
    report = tmp_path / ".ses/reports/baseline.html"
    report.parent.mkdir(parents=True)
    report.write_text("must not be served", encoding="utf-8")
    _write_status(
        tmp_path,
        {
            "artifact_refs": [
                {
                    "root": "workspace",
                    "path": ".ses/reports/baseline.html",
                    "sha256": "not-a-digest",
                }
            ]
        },
    )

    with _running_server(tmp_path) as address:
        status, _, body = _request(
            address, "GET", "/artifact/.ses/reports/baseline.html"
        )

    assert status == 404
    assert b"must not be served" not in body


@pytest.mark.parametrize(
    "target",
    [
        "/artifact/../outside.html",
        "/artifact/%2e%2e/outside.html",
        "/artifact/%252e%252e/outside.html",
        "/artifact/.ses/reports/%2e%2e/private.html",
        "/artifact/https%3A%2F%2Fexample.invalid%2Freport.html",
    ],
)
def test_artifact_routes_reject_directory_traversal(
    tmp_path: Path,
    target: str,
) -> None:
    _write_status(tmp_path, {"reports": [target.removeprefix("/artifact/")]})

    with _running_server(tmp_path) as address:
        status, _, _ = _request(address, "GET", target)

    assert status == 403


def test_symlinked_status_and_artifacts_cannot_escape_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    reports = workspace / ".ses/reports"
    reports.mkdir(parents=True)
    outside_status = tmp_path / "outside-status.json"
    outside_status.write_text('{"overall_status":"complete"}', encoding="utf-8")
    status_link = workspace / ".ses/status.json"
    status_link.symlink_to(outside_status)

    assert json.loads(load_status(workspace).payload)["overall_status"] == "error"
    status_link.unlink()

    outside_report = tmp_path / "outside-report.html"
    outside_report.write_text("secret report", encoding="utf-8")
    (reports / "leak.html").symlink_to(outside_report)
    _write_status(
        workspace,
        {"reports": [{"label": "leak", "path": ".ses/reports/leak.html"}]},
    )

    with _running_server(workspace) as address:
        status, _, body = _request(address, "GET", "/artifact/.ses/reports/leak.html")

    assert status == 403
    assert b"secret report" not in body


def test_mutating_methods_are_rejected_and_workspace_is_unchanged(
    tmp_path: Path,
) -> None:
    _write_status(tmp_path, {"overall_status": "waiting", "stations": []})
    before = _workspace_snapshot(tmp_path)

    with _running_server(tmp_path) as address:
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            status, headers, body = _request(address, method, "/.ses/status.json")
            assert status == 405
            assert headers["Allow"] == "GET, HEAD"
            assert json.loads(body)["error"] == "dashboard is read-only"

    assert _workspace_snapshot(tmp_path) == before


def test_server_does_not_read_credentials_from_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-environment-only"
    monkeypatch.setenv("CHATANYWHERE_API_KEY", secret)
    assert os.environ["CHATANYWHERE_API_KEY"] == secret

    with _running_server(tmp_path) as address:
        _, _, page = _request(address, "GET", "/")
        _, _, status = _request(address, "GET", "/.ses/status.json")

    assert secret.encode() not in page
    assert secret.encode() not in status

"""A loopback-first, read-only HTTP server for the journey dashboard."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Final, cast
from urllib.parse import unquote, urlsplit

from ses.contracts.security import validate_public_data
from ses.dashboard.render import render_dashboard_html

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8765
STATUS_REFERENCE: Final = PurePosixPath(".ses/status.json")
MAX_STATUS_BYTES: Final = 2 * 1024 * 1024

_ARTIFACT_CONTAINERS = frozenset(
    {
        "artifact",
        "artifact_refs",
        "artifacts",
        "decision_refs",
        "output",
        "outputs",
        "report",
        "reports",
    }
)
_ARTIFACT_PATH_KEYS = frozenset(
    {"artifact_path", "href", "output_path", "path", "report_path"}
)
_PUBLIC_ARTIFACT_SUFFIXES = frozenset(
    {".csv", ".html", ".htm", ".json", ".md", ".pdf", ".txt"}
)

_PAGE_CSP = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'"
)
_ARTIFACT_CSP = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "connect-src 'none'; "
    "font-src 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "script-src 'none'; "
    "style-src 'unsafe-inline'"
)


class DashboardPathError(ValueError):
    """Raised when a requested local path is outside the public boundary."""


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """A public status response and the parsed data used for its link allowlist."""

    payload: bytes
    data: Mapping[str, object]


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _waiting_status() -> StatusSnapshot:
    data: dict[str, object] = {
        "overall_status": "waiting",
        "message": "等待第一站写入本地进度。",
        "stations": [],
    }
    return StatusSnapshot(payload=_json_bytes(data), data=data)


def _error_status() -> StatusSnapshot:
    data: dict[str, object] = {
        "overall_status": "error",
        "message": "status.json 无法安全读取, 请回终端查看最近一条命令。",
        "stations": [],
    }
    return StatusSnapshot(payload=_json_bytes(data), data=data)


def _safe_regular_file(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or not relative.parts:
        raise DashboardPathError("dashboard paths must be relative")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise DashboardPathError("dashboard path contains an unsafe component")

    candidate = root
    for component in relative.parts:
        candidate /= component
        if candidate.is_symlink():
            raise DashboardPathError("dashboard path contains a symlink")
    if not candidate.is_file():
        raise FileNotFoundError("dashboard file does not exist")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise DashboardPathError("dashboard path escapes its workspace") from exc
    return resolved


def load_status(workspace_root: Path) -> StatusSnapshot:
    """Load public status without following symlinks or exposing invalid data."""

    if workspace_root.is_symlink() or not workspace_root.is_dir():
        return _error_status()
    try:
        root = workspace_root.resolve(strict=True)
    except OSError:
        return _error_status()
    try:
        status_path = _safe_regular_file(root, STATUS_REFERENCE)
    except FileNotFoundError:
        return _waiting_status()
    except DashboardPathError:
        return _error_status()

    try:
        if status_path.stat().st_size > MAX_STATUS_BYTES:
            return _error_status()
        value = json.loads(status_path.read_bytes())
        if not isinstance(value, Mapping):
            return _error_status()
        validate_public_data(value)
        payload = _json_bytes(value)
    except (OSError, UnicodeError, ValueError):
        return _error_status()
    return StatusSnapshot(payload=payload, data=value)


def _decode_reference(value: str) -> str:
    decoded = value
    for _ in range(4):
        expanded = unquote(decoded, errors="strict")
        if expanded == decoded:
            return expanded
        decoded = expanded
    return decoded


def normalize_artifact_reference(value: str) -> PurePosixPath:
    """Normalize one status-declared public artifact path."""

    decoded = _decode_reference(value.strip())
    if decoded.startswith("/artifact/"):
        decoded = decoded.removeprefix("/artifact/")
    decoded = decoded.removeprefix("./").removeprefix("/")
    if not decoded or "\\" in decoded or "\x00" in decoded:
        raise DashboardPathError("artifact path is invalid")
    if ":" in PurePosixPath(decoded).parts[0]:
        raise DashboardPathError("artifact URLs are not supported")
    relative = PurePosixPath(decoded)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != ".ses"
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix.casefold() not in _PUBLIC_ARTIFACT_SUFFIXES
    ):
        raise DashboardPathError("artifact path is outside the public boundary")
    return relative


def _collect_artifact_references(
    value: object,
    output: dict[PurePosixPath, str | None],
    conflicts: set[PurePosixPath],
    *,
    in_artifact_container: bool = False,
) -> None:
    if isinstance(value, str):
        if in_artifact_container:
            try:
                _record_artifact_reference(
                    output,
                    conflicts,
                    normalize_artifact_reference(value),
                    expected_sha256=None,
                )
            except DashboardPathError:
                pass
        return
    if isinstance(value, list):
        for child in value:
            _collect_artifact_references(
                child,
                output,
                conflicts,
                in_artifact_container=in_artifact_container,
            )
        return
    if not isinstance(value, Mapping):
        return
    normalized = {str(key).casefold(): child for key, child in value.items()}
    direct_path_keys = {
        key for key in _ARTIFACT_PATH_KEYS if isinstance(normalized.get(key), str)
    }
    if in_artifact_container and direct_path_keys:
        canonical_like = "root" in normalized or "sha256" in normalized
        expected_sha256: str | None = None
        canonical_valid = not canonical_like
        if canonical_like:
            digest = normalized.get("sha256")
            canonical_valid = (
                normalized.get("root") == "workspace"
                and isinstance(digest, str)
                and len(digest) == 64
                and all(character in "0123456789abcdef" for character in digest)
            )
            if canonical_valid:
                expected_sha256 = digest
        for key in direct_path_keys:
            try:
                reference = normalize_artifact_reference(cast(str, normalized[key]))
            except DashboardPathError:
                continue
            if canonical_valid:
                _record_artifact_reference(
                    output,
                    conflicts,
                    reference,
                    expected_sha256=expected_sha256,
                )
            else:
                output.pop(reference, None)
                conflicts.add(reference)
    for raw_key, child in value.items():
        key = str(raw_key).casefold()
        if in_artifact_container and key in direct_path_keys:
            continue
        if (
            key in _ARTIFACT_PATH_KEYS
            and isinstance(child, str)
            and (in_artifact_container or key != "path")
        ):
            try:
                _record_artifact_reference(
                    output,
                    conflicts,
                    normalize_artifact_reference(child),
                    expected_sha256=None,
                )
            except DashboardPathError:
                pass
        elif key in _ARTIFACT_CONTAINERS:
            _collect_artifact_references(
                child,
                output,
                conflicts,
                in_artifact_container=True,
            )
        elif isinstance(child, (Mapping, list)):
            _collect_artifact_references(
                child,
                output,
                conflicts,
                in_artifact_container=in_artifact_container,
            )
        elif in_artifact_container and not direct_path_keys and isinstance(child, str):
            try:
                _record_artifact_reference(
                    output,
                    conflicts,
                    normalize_artifact_reference(child),
                    expected_sha256=None,
                )
            except DashboardPathError:
                pass


def _record_artifact_reference(
    output: dict[PurePosixPath, str | None],
    conflicts: set[PurePosixPath],
    reference: PurePosixPath,
    *,
    expected_sha256: str | None,
) -> None:
    """Keep the strongest declaration and fail closed on digest conflicts."""

    if reference in conflicts:
        return
    if reference not in output:
        output[reference] = expected_sha256
        return
    current = output[reference]
    if expected_sha256 is None:
        return
    if current is None:
        output[reference] = expected_sha256
        return
    if current != expected_sha256:
        output.pop(reference, None)
        conflicts.add(reference)


def _artifact_manifest(
    status: Mapping[str, object],
) -> dict[PurePosixPath, str | None]:
    references: dict[PurePosixPath, str | None] = {}
    conflicts: set[PurePosixPath] = set()
    _collect_artifact_references(status, references, conflicts)
    return references


def artifact_allowlist(status: Mapping[str, object]) -> frozenset[PurePosixPath]:
    """Return the local report paths explicitly named by public status data."""

    return frozenset(_artifact_manifest(status))


class DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying only an immutable workspace root and page."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        workspace_root: Path,
    ) -> None:
        self.workspace_root = workspace_root
        self.page = render_dashboard_html().encode("utf-8")
        super().__init__(server_address, DashboardRequestHandler)


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve the dashboard, status, and allowlisted artifacts with GET or HEAD."""

    server_version = "ses-dashboard"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        """Avoid logging request targets that may contain local path details."""

    def _dashboard_server(self) -> DashboardHTTPServer:
        if not isinstance(self.server, DashboardHTTPServer):
            raise RuntimeError("dashboard handler is attached to the wrong server")
        return self.server

    def _security_headers(self, csp: str) -> None:
        self.send_header("Content-Security-Policy", csp)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy", "camera=(), geolocation=(), microphone=()"
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str,
        csp: str,
        cache_control: str = "no-store",
        include_body: bool,
        allow: str | None = None,
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if allow is not None:
            self.send_header("Allow", allow)
        self._security_headers(csp)
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _error(
        self,
        status: HTTPStatus,
        message: str,
        *,
        include_body: bool,
        allow: str | None = None,
    ) -> None:
        self._send(
            status,
            _json_bytes({"error": message}),
            content_type="application/json; charset=utf-8",
            csp="default-src 'none'; frame-ancestors 'none'",
            include_body=include_body,
            allow=allow,
        )

    def _artifact(
        self,
        request_path: str,
        *,
        include_body: bool,
    ) -> None:
        server = self._dashboard_server()
        try:
            requested = normalize_artifact_reference(request_path)
        except (DashboardPathError, UnicodeError):
            self._error(
                HTTPStatus.FORBIDDEN,
                "artifact path is not allowed",
                include_body=include_body,
            )
            return
        snapshot = load_status(server.workspace_root)
        manifest = _artifact_manifest(snapshot.data)
        if requested not in manifest:
            self._error(
                HTTPStatus.NOT_FOUND,
                "artifact is not listed in status.json",
                include_body=include_body,
            )
            return
        try:
            artifact = _safe_regular_file(server.workspace_root, requested)
            body = artifact.read_bytes()
        except DashboardPathError:
            self._error(
                HTTPStatus.FORBIDDEN,
                "artifact path is not allowed",
                include_body=include_body,
            )
            return
        except OSError:
            self._error(
                HTTPStatus.NOT_FOUND,
                "artifact is unavailable",
                include_body=include_body,
            )
            return
        expected_sha256 = manifest[requested]
        if (
            expected_sha256 is not None
            and hashlib.sha256(body).hexdigest() != expected_sha256
        ):
            self._error(
                HTTPStatus.CONFLICT,
                "artifact content does not match status.json",
                include_body=include_body,
            )
            return
        guessed, _ = mimetypes.guess_type(artifact.name)
        content_type = guessed or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/json":
            content_type = f"{content_type}; charset=utf-8"
        self._send(
            HTTPStatus.OK,
            body,
            content_type=content_type,
            csp=_ARTIFACT_CSP,
            cache_control="no-cache",
            include_body=include_body,
        )

    def _handle_read(self, *, include_body: bool) -> None:
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.fragment:
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid request target",
                include_body=include_body,
            )
            return
        try:
            path = _decode_reference(target.path)
        except UnicodeError:
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid request path",
                include_body=include_body,
            )
            return

        server = self._dashboard_server()
        if path in {"", "/"}:
            self._send(
                HTTPStatus.OK,
                server.page,
                content_type="text/html; charset=utf-8",
                csp=_PAGE_CSP,
                include_body=include_body,
            )
            return
        if path == "/.ses/status.json":
            snapshot = load_status(server.workspace_root)
            self._send(
                HTTPStatus.OK,
                snapshot.payload,
                content_type="application/json; charset=utf-8",
                csp="default-src 'none'; frame-ancestors 'none'",
                include_body=include_body,
            )
            return
        if path.startswith("/artifact/"):
            self._artifact(path, include_body=include_body)
            return
        self._error(
            HTTPStatus.NOT_FOUND,
            "route not found",
            include_body=include_body,
        )

    def do_GET(self) -> None:
        """Serve a read-only resource."""

        self._handle_read(include_body=True)

    def do_HEAD(self) -> None:
        """Serve the same headers as GET without a response body."""

        self._handle_read(include_body=False)

    def _method_not_allowed(self) -> None:
        self._error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "dashboard is read-only",
            include_body=True,
            allow="GET, HEAD",
        )

    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed


def create_dashboard_server(
    workspace_root: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> DashboardHTTPServer:
    """Bind a read-only dashboard server; callers own its lifecycle."""

    if not host.strip():
        raise ValueError("dashboard host must not be empty")
    if not 0 <= port <= 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    if workspace_root.is_symlink() or not workspace_root.is_dir():
        raise ValueError("dashboard workspace must be a real directory")
    root = workspace_root.resolve(strict=True)
    return DashboardHTTPServer((host, port), root)


def serve_dashboard(
    workspace_root: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Serve until interrupted without changing anything in the workspace."""

    with create_dashboard_server(workspace_root, host=host, port=port) as server:
        server.serve_forever()

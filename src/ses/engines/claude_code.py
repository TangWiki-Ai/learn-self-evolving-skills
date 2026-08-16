"""Claude Code headless adapter with isolated subprocess lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator, Mapping

from ses.contracts import (
    CompletedPayload,
    EngineEvent,
    EngineExitStatus,
    EngineRequest,
    ErrorPayload,
)
from ses.engines.events import make_event
from ses.engines.stream_json import ClaudeStreamParser, StreamParseError
from ses.foundation.config import LockedModel
from ses.foundation.credentials import (
    ProviderCredentials,
    build_claude_environment,
    credential_values,
    redact,
)
from ses.foundation.workspace import CaseWorkspace

_FILESYSTEM_TOOLS = (
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "NotebookEdit",
    "Read",
    "Write",
)


class ClaudeCodeEngine:
    """Run one Claude process per request and expose only canonical events."""

    def __init__(
        self,
        *,
        model: LockedModel,
        credentials: ProviderCredentials,
        workspace: CaseWorkspace,
        executable: str = "claude",
        environ: Mapping[str, str] | None = None,
        system_prompt: str | None = None,
        output_json_schema: Mapping[str, object] | None = None,
    ) -> None:
        self._model = model
        self._credentials = credentials
        self._workspace = workspace
        self._executable = executable
        self._source_environment = dict(os.environ if environ is None else environ)
        self._secrets = tuple(
            dict.fromkeys(
                (*credential_values(self._source_environment), credentials.api_key)
            )
        )
        self._system_prompt = system_prompt
        if output_json_schema is not None and not output_json_schema:
            raise ValueError("output JSON schema cannot be empty")
        self._output_json_schema = (
            json.dumps(
                output_json_schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if output_json_schema is not None
            else None
        )
        self._running: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def model(self) -> LockedModel:
        """Return the immutable model binding used to build every command."""

        return self._model

    @property
    def workspace(self) -> CaseWorkspace:
        """Return the workspace in which every request executes."""

        return self._workspace

    def build_command(self, request: EngineRequest) -> list[str]:
        """Build an argv array; credentials never enter this value."""
        if self._output_json_schema is not None and request.allowed_tools:
            raise ValueError("structured output requests cannot enable case tools")
        forbidden = set(request.allowed_tools) & set(_FILESYSTEM_TOOLS)
        if forbidden:
            raise ValueError(
                "case engine cannot enable filesystem tools: "
                + ", ".join(sorted(forbidden))
            )
        command = [
            self._executable,
            "--bare",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "dontAsk",
            "--disallowedTools",
            ",".join(_FILESYSTEM_TOOLS),
        ]
        if request.resume_session_id is not None:
            command.extend(("--resume", request.resume_session_id))
        if request.allowed_tools:
            command.extend(("--allowedTools", ",".join(request.allowed_tools)))
        if self._output_json_schema is not None:
            command.extend(("--tools", "", "--json-schema", self._output_json_schema))
        if self._workspace.mcp_config is not None:
            command.extend(
                (
                    "--strict-mcp-config",
                    "--mcp-config",
                    str(self._workspace.mcp_config),
                )
            )
        command.extend(("--model", self._model.model_id))
        if self._system_prompt:
            command.extend(("--system-prompt", self._system_prompt))
        command.append(request.prompt)
        return command

    def build_environment(self) -> dict[str, str]:
        """Return the isolated provider environment for Claude itself."""
        return build_claude_environment(
            self._source_environment,
            self._credentials,
            base_url=self._model.base_url,
            model_id=self._model.model_id,
            config_dir=self._workspace.claude_config_dir,
        )

    async def cancel(self, request_id: str) -> bool:
        """Cancel a live process and its subprocess group."""
        async with self._lock:
            process = self._running.get(request_id)
            if process is None:
                return False
            self._cancelled.add(request_id)
        await self._stop_process(process)
        return True

    async def stream(self, request: EngineRequest) -> AsyncIterator[EngineEvent]:
        parser = ClaudeStreamParser(
            secrets=self._secrets,
            expects_structured_output=self._output_json_schema is not None,
        )
        sequence = 0
        pending_completed: CompletedPayload | None = None
        stream_failed = False
        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[bytes] | None = None

        try:
            command = self.build_command(request)
            environment = self.build_environment()
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._workspace.root,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=sys.platform != "win32",
            )
            async with self._lock:
                if request.request_id in self._running:
                    await self._stop_process(process)
                    raise RuntimeError(
                        f"request is already running: {request.request_id}"
                    )
                self._running[request.request_id] = process
            assert process.stdout is not None
            assert process.stderr is not None
            stderr_task = asyncio.create_task(process.stderr.read())

            async with asyncio.timeout(request.timeout_seconds):
                while line_bytes := await process.stdout.readline():
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        payloads = parser.parse_line(line)
                    except StreamParseError as exc:
                        await self._stop_process(process)
                        yield make_event(
                            request_id=request.request_id,
                            sequence=sequence,
                            payload=ErrorPayload(
                                error_code="malformed_stream",
                                message=redact(str(exc), self._secrets),
                            ),
                        )
                        sequence += 1
                        stream_failed = True
                        pending_completed = CompletedPayload(
                            exit_status=EngineExitStatus.ERROR
                        )
                        break
                    for payload in payloads:
                        if isinstance(payload, CompletedPayload):
                            pending_completed = payload
                            continue
                        yield make_event(
                            request_id=request.request_id,
                            sequence=sequence,
                            payload=payload,
                        )
                        sequence += 1
                return_code = await process.wait()

            # A clean Claude parent exit does not prove MCP children exited. Reap the
            # entire process group before exposing the terminal event.
            await self._stop_process(process)
            cancelled = request.request_id in self._cancelled
            if cancelled:
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=CompletedPayload(
                        exit_status=EngineExitStatus.CANCELLED,
                        session_id=parser.session_id,
                    ),
                )
            elif stream_failed:
                assert pending_completed is not None
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=pending_completed,
                )
            elif return_code != 0:
                detail = await self._stderr_detail(stderr_task)
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=ErrorPayload(
                        error_code="process_exit",
                        message=f"claude exited with code {return_code}: {detail}",
                    ),
                )
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence + 1,
                    payload=CompletedPayload(
                        exit_status=EngineExitStatus.ERROR,
                        session_id=parser.session_id,
                    ),
                )
            elif pending_completed is not None:
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=pending_completed,
                )
            else:
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence,
                    payload=ErrorPayload(
                        error_code="missing_result",
                        message="claude stream ended without a result event",
                    ),
                )
                yield make_event(
                    request_id=request.request_id,
                    sequence=sequence + 1,
                    payload=CompletedPayload(
                        exit_status=EngineExitStatus.ERROR,
                        session_id=parser.session_id,
                    ),
                )
        except TimeoutError:
            if process is not None:
                await self._stop_process(process)
            yield make_event(
                request_id=request.request_id,
                sequence=sequence,
                payload=ErrorPayload(
                    error_code="timeout",
                    message=f"engine timed out after {request.timeout_seconds:g}s",
                ),
            )
            yield make_event(
                request_id=request.request_id,
                sequence=sequence + 1,
                payload=CompletedPayload(
                    exit_status=EngineExitStatus.TIMEOUT,
                    session_id=parser.session_id,
                ),
            )
        except (OSError, ValueError) as exc:
            detail = redact(str(exc), self._secrets)
            error_code = (
                "unsafe_request" if isinstance(exc, ValueError) else "process_start"
            )
            yield make_event(
                request_id=request.request_id,
                sequence=sequence,
                payload=ErrorPayload(
                    error_code=error_code,
                    message=f"cannot start claude: {detail}",
                ),
            )
            yield make_event(
                request_id=request.request_id,
                sequence=sequence + 1,
                payload=CompletedPayload(exit_status=EngineExitStatus.ERROR),
            )
        except asyncio.CancelledError:
            if process is not None:
                await self._stop_process(process)
            raise
        finally:
            if process is not None:
                await self._stop_process(process)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            async with self._lock:
                self._running.pop(request.request_id, None)
                self._cancelled.discard(request.request_id)

    async def _stderr_detail(self, task: asyncio.Task[bytes]) -> str:
        try:
            raw = await task
        except (OSError, asyncio.CancelledError):
            return "no error detail"
        detail = raw.decode("utf-8", errors="replace").strip() or "no error detail"
        return redact(detail, self._secrets).replace("\n", " ")[:600]

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if sys.platform != "win32":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.2)
                except TimeoutError:
                    pass
            await asyncio.sleep(0.05)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.returncode is None:
                process.kill()
                await process.wait()
            return
        if process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=0.2)
            except TimeoutError:
                process.kill()
                await process.wait()

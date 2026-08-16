"""Provider-neutral engine interface and the two Issue #2 adapters."""

from ses.engines.base import Engine
from ses.engines.claude_code import ClaudeCodeEngine
from ses.engines.fake import FakeEngine, FakeFixture, load_fake_fixture
from ses.engines.stream_json import ClaudeStreamParser, StreamParseError

__all__ = [
    "ClaudeCodeEngine",
    "ClaudeStreamParser",
    "Engine",
    "FakeEngine",
    "FakeFixture",
    "StreamParseError",
    "load_fake_fixture",
]

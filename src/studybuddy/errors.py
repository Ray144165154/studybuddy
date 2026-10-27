"""Exception hierarchy for studybuddy.

Every error raised by the library derives from :class:`AgentError`, so callers can
catch a single type when they do not care about the specifics.
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """Base class for all studybuddy errors."""


class ToolError(AgentError):
    """A tool raised while being executed."""

    def __init__(self, tool_name: str, message: str, *, original: Exception | None = None) -> None:
        super().__init__(f"tool {tool_name!r} failed: {message}")
        self.tool_name = tool_name
        self.original = original


class ToolNotFound(AgentError):
    """The model asked for a tool that is not registered."""

    def __init__(self, tool_name: str, available: list[str] | None = None) -> None:
        self.tool_name = tool_name
        self.available = available or []
        hint = f" available: {', '.join(sorted(self.available))}" if self.available else ""
        super().__init__(f"unknown tool {tool_name!r}.{hint}")


class LLMError(AgentError):
    """The provider returned an error or an unparseable payload."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class RetryExhausted(AgentError):
    """Every retry attempt failed."""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        super().__init__(f"gave up after {attempts} attempt(s): {last_error}")
        self.attempts = attempts
        self.last_error = last_error


class MaxIterationsReached(AgentError):
    """The agent loop hit its iteration budget without producing a final answer."""

    def __init__(self, iterations: int) -> None:
        super().__init__(f"agent did not finish within {iterations} iteration(s)")
        self.iterations = iterations


class PDFError(AgentError):
    """A PDF could not be read, is encrypted, or contains no extractable text."""


class LibraryError(AgentError):
    """The local library of books and chunks is missing or malformed."""


class MemoryStoreError(AgentError):
    """The long-term memory file is missing or malformed."""


class StudyError(AgentError):
    """A study-level operation failed, e.g. asking about a book that is not indexed."""

"""Exception hierarchy for studybuddy.

Every error raised by the library derives from :class:`AgentError`, so callers can
catch a single type when they do not care about the specifics.
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """Base class for all studybuddy errors."""


class LLMError(AgentError):
    """The provider returned an error or an unparseable payload."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class RetryExhausted(LLMError):
    """Every retry attempt failed.

    Inherits :class:`LLMError` so callers that map "the provider is broken" to a
    specific status (the web UI returns 502) treat a give-up the same way as a
    single failed call. Retrying already proved it is not a user mistake.
    """

    def __init__(self, attempts: int, last_error: Exception) -> None:
        super().__init__(f"gave up after {attempts} attempt(s): {last_error}")
        self.attempts = attempts
        self.last_error = last_error


class PDFError(AgentError):
    """A PDF could not be read, is encrypted, or contains no extractable text."""


class OcrError(AgentError):
    """OCR could not run: no backend, a compiler failure, or unusable output."""


class LibraryError(AgentError):
    """The local library of books and chunks is missing or malformed."""


class MemoryStoreError(AgentError):
    """The long-term memory file is missing or malformed."""


class StudyError(AgentError):
    """A study-level operation failed, e.g. asking about a book that is not indexed."""

"""Test doubles: a scripted model, a fake HTTP response, and a tiny PDF builder.

The PDF builder writes a valid single-font PDF by hand so the test-suite can exercise
real parsing without shipping binary fixtures.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from studybuddy.types import LLMResponse, Message, Usage


class ScriptedLLM:
    """A model client that replays a fixed list of responses."""

    def __init__(self, *responses: LLMResponse | Callable[..., LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[Message]] = []

    def complete(self, messages: Sequence[Message], tools: Any = None) -> LLMResponse:
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError("ScriptedLLM ran out of scripted responses")
        item = self.responses.pop(0)
        return item(messages) if callable(item) else item

    async def acomplete(self, messages: Sequence[Message], tools: Any = None) -> LLMResponse:
        return self.complete(messages, tools)

    @property
    def last_user_content(self) -> str:
        for message in reversed(self.calls[-1]):
            if message.role == "user":
                return message.content or ""
        return ""

    @property
    def request_count(self) -> int:
        return len(self.calls)


def says(text: str, *, tokens: int = 10) -> LLMResponse:
    return LLMResponse(text=text, usage=Usage(total_tokens=tokens), model="test-model")


class FakeResponse:
    """Enough of ``http.client.HTTPResponse`` for the LLM client."""

    def __init__(self, body: bytes | str, headers: dict[str, str] | None = None) -> None:
        self._raw = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = headers or {}
        self._io = io.BytesIO(self._raw)

    def read(self, *args: Any) -> bytes:
        return self._io.read(*args)

    def __iter__(self):
        return iter(self._io)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------------------
# Minimal PDF writer
# --------------------------------------------------------------------------------------


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: Sequence[str]) -> bytes:
    body = ["BT", "/F1 11 Tf", "14 TL", "60 780 Td"]
    for line in lines:
        body.append(f"({_escape(line)}) Tj")
        body.append("T*")
    body.append("ET")
    return "\n".join(body).encode("latin-1", "replace")


def make_pdf(path: str | Path, pages: Sequence[Sequence[str]]) -> Path:
    """Write a valid PDF whose pages contain ``pages[i]`` lines and return the path."""
    file = Path(path)
    count = len(pages)
    kids = " ".join(f"{4 + 2 * index} 0 R" for index in range(count))

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, lines in enumerate(pages):
        stream = _content_stream(lines)
        page_id = 4 + 2 * index
        content_id = 5 + 2 * index
        assert page_id == len(objects) + 1, "object numbering must stay aligned"
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_position = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_position}\n%%EOF\n"
    ).encode()

    file.write_bytes(bytes(out))
    return file


def make_textbook(path: str | Path) -> Path:
    """A two-page textbook with recognisable section headings."""
    return make_pdf(
        path,
        [
            [
                "Chapter 3 Derivatives",
                "3.1 The Concept of Derivative",
                "The derivative measures the instantaneous rate of change.",
                "It is defined as the limit of a difference quotient.",
                "42",
            ],
            [
                "3.2 Differentiation Rules",
                "The product rule and the chain rule follow from the definition.",
                "Practice differentiating composite functions.",
            ],
        ],
    )

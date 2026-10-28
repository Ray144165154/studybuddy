"""studybuddy -- a study assistant that actually remembers.

Upload your textbooks as PDFs, ask questions across all of them, get answers with
page-level citations, and let the assistant keep track of what you struggle with.

    from studybuddy import StudyBuddy

    buddy = StudyBuddy(model="deepseek-chat", base_url="https://api.deepseek.com/v1")
    buddy.add_book("高等数学.pdf", course="高等数学")

    answer = buddy.ask("拉格朗日中值定理和柯西中值定理有什么区别？")
    print(answer.with_citations())
"""

from __future__ import annotations

from .chunk import Chunker, chunk_document, detect_section
from .embed import Embedder, EmbeddingIndex, cosine, reciprocal_rank_fusion
from .errors import (
    AgentError,
    LibraryError,
    LLMError,
    MemoryStoreError,
    PDFError,
    RetryExhausted,
    StudyError,
)
from .index import BM25, tokenize
from .llm import LLM
from .memory import Memory
from .pdf import PdfDocument, PdfPage, read_pdf
from .store import Library
from .study import Comparison, Quiz, QuizQuestion, ReviewItem, StudyBuddy
from .types import (
    Answer,
    Book,
    Chunk,
    Episode,
    Fact,
    LLMResponse,
    MemoryHit,
    Message,
    Profile,
    Source,
    ToolCall,
    TopicState,
    Usage,
)

__version__ = "0.8.5"

__all__ = [
    "StudyBuddy",
    "Embedder",
    "EmbeddingIndex",
    "Library",
    "Memory",
    "LLM",
    "BM25",
    "tokenize",
    "cosine",
    "reciprocal_rank_fusion",
    "read_pdf",
    "PdfDocument",
    "PdfPage",
    "Chunker",
    "chunk_document",
    "detect_section",
    "Answer",
    "Book",
    "Chunk",
    "Comparison",
    "Episode",
    "Fact",
    "LLMResponse",
    "MemoryHit",
    "Message",
    "Profile",
    "Quiz",
    "QuizQuestion",
    "ReviewItem",
    "Source",
    "ToolCall",
    "TopicState",
    "Usage",
    "AgentError",
    "LibraryError",
    "LLMError",
    "MemoryStoreError",
    "PDFError",
    "RetryExhausted",
    "StudyError",
    "__version__",
]

"""A complete study session with **no API key and no network**.

This starts a tiny fake OpenAI-compatible server on localhost, builds a two-book
library, then runs the real pipeline three times so you can see what studybuddy
actually does:

1. ask a question -> answer with page-level citations
2. ask a follow-up -> the assistant recalls what it learned about you
3. review        -> the spaced-repetition queue

    python examples/offline_demo.py
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from studybuddy import Book, Chunk, Library, Memory, StudyBuddy

# --------------------------------------------------------------------------------------
# A fake provider. Two canned behaviours, chosen by looking at the prompt.
# --------------------------------------------------------------------------------------

SOURCE_LINE = re.compile(r"\[(\d+)\] 《[^》]+》 第(\d+)页 · (.+)")


def compose_answer(prompt: str) -> str:
    """Build an answer whose citation numbers match the passages actually supplied."""
    numbers = {section.strip(): number for number, _page, section in SOURCE_LINE.findall(prompt)}
    derivative = numbers.get("3.1 导数的概念", "1")
    theorem = numbers.get("4.2 中值定理", "2")
    return (
        f"导数的定义是函数增量与自变量增量之比的极限 [{derivative}]，它刻画的是瞬时变化率。\n\n"
        f"拉格朗日中值定理是柯西中值定理的特例 [{theorem}]，所以考试里遇到“存在 ξ 使 f'(ξ)=...”时，"
        "先判断能不能直接用拉格朗日；如果两个函数同时出现，才考虑柯西。\n\n"
        "你上次在这块丢过分，建议先手推一遍罗尔定理的证明。"
    )


MEMORY_PAYLOAD = {
    "topics": ["导数的定义", "中值定理"],
    "facts": [
        {"text": "中值定理部分比较薄弱，需要先复习罗尔定理", "kind": "weakness", "topic": "中值定理"},
        {"text": "偏好先看证明再看例题", "kind": "preference", "topic": "导数"},
    ],
    "summary": "复习了导数定义，并辨析了拉格朗日与柯西中值定理",
}

QUIZ_PAYLOAD = {
    "topic": "中值定理",
    "questions": [
        {
            "question": "拉格朗日中值定理的三个条件分别是什么？缺一个会怎样？",
            "answer": "闭区间连续、开区间可导，且存在 ξ…",
            "hint": "想想罗尔定理比它多了什么条件",
            "difficulty": "medium",
        }
    ],
}


class FakeProvider(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        prompt = "\n".join(message.get("content") or "" for message in payload["messages"])

        if "抽取值得长期记住" in prompt:
            content = json.dumps(MEMORY_PAYLOAD, ensure_ascii=False)
        elif "只输出 JSON" in prompt and "questions" in prompt:
            topic = "中值定理"
            match = re.search(r"主题: (.+)", prompt)
            if match:
                topic = match.group(1).strip()
            payload = {
                "topic": topic,
                "questions": [
                    {
                        "question": f"关于「{topic}」：它的成立条件是什么？少一个条件会怎样？",
                        "answer": "闭区间连续、开区间可导，且存在 ξ 使…",
                        "hint": "想想罗尔定理比它多了什么",
                        "difficulty": "medium",
                    }
                ],
            }
            content = json.dumps(payload, ensure_ascii=False)
        else:
            content = compose_answer(prompt)

        body = json.dumps(
            {
                "model": "fake-model",
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 320, "completion_tokens": 96, "total_tokens": 416},
            },
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_library(root: Path) -> None:
    """Pretend two textbooks were uploaded (no PDF needed for this demo)."""
    library = Library(root)
    library.books = [
        Book(id="math-1", title="高等数学", course="高等数学", pages=2, chunks=2),
        Book(id="la-1", title="线性代数", course="线性代数", pages=1, chunks=1),
    ]
    library.chunks = [
        Chunk(
            id="math-1:p12:0",
            book_id="math-1",
            book_title="高等数学",
            page=12,
            section="3.1 导数的概念",
            course="高等数学",
            text="导数定义为函数增量与自变量增量之比的极限，记作 f'(x)，它刻画瞬时变化率。",
        ),
        Chunk(
            id="math-1:p40:0",
            book_id="math-1",
            book_title="高等数学",
            page=40,
            section="4.2 中值定理",
            course="高等数学",
            text="拉格朗日中值定理是柯西中值定理的特例，罗尔定理又是拉格朗日中值定理的特例。",
        ),
        Chunk(
            id="la-1:p5:0",
            book_id="la-1",
            book_title="线性代数",
            page=5,
            section="1.1 矩阵",
            course="线性代数",
            text="矩阵乘法不满足交换律，但满足结合律与分配律。",
        ),
    ]
    library.save()
    Memory(root).set_profile(major="计算机科学与技术", grade="大二", courses=["高等数学", "线性代数"])


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "studybuddy-data"
        build_library(root)

        buddy = StudyBuddy(
            root,
            model="fake-model",
            api_key="demo",
            base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
        )

        print("=" * 68)
        print("1) 提问：导数是什么？拉格朗日和中值定理什么关系？")
        print("=" * 68)
        answer = buddy.ask("导数的定义是什么？拉格朗日和中值定理什么关系？")
        print(answer.with_citations())

        print("\n" + "=" * 68)
        print("2) 只问一句，看看它记住了什么")
        print("=" * 68)
        print(Memory(root).digest())

        print("\n" + "=" * 68)
        print("3) 按遗忘曲线复习（把话题设为今天到期）")
        print("=" * 68)
        memory = Memory(root)
        for topic in memory.topics:
            memory.topics[topic].due = "2000-01-01T00:00:00"
        memory.save()
        buddy.memory = memory
        for item in buddy.review(limit=2):
            print(f"· {item.topic}（掌握度 {item.mastery:.0%}）: {item.question}")

    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()

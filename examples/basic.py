"""Use studybuddy as a library instead of the CLI.

export STUDYBUDDY_API_KEY=sk-...
export STUDYBUDDY_BASE_URL=https://api.deepseek.com/v1
python examples/basic.py 高等数学.pdf
"""

from __future__ import annotations

import sys
from pathlib import Path

from studybuddy import StudyBuddy

if len(sys.argv) < 2:
    raise SystemExit("usage: python examples/basic.py <textbook.pdf>")

buddy = StudyBuddy(model="deepseek-chat", base_url="https://api.deepseek.com/v1")
buddy.add_book(Path(sys.argv[1]), course="高等数学", on_progress=lambda message: print(f"  {message}"))
buddy.memory.set_profile(major="计算机科学与技术", courses=["高等数学"])

answer = buddy.ask("这本书第三章讲了什么？")
print(answer.with_citations())

print("\n记忆里现在有:")
print(buddy.memory.digest())

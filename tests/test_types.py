"""Wire-format details of the plain data structures."""

from __future__ import annotations

import unittest

from studybuddy import Message, ToolCall, Usage


class TestMessages(unittest.TestCase):
    def test_user_message(self) -> None:
        self.assertEqual(Message(role="user", content="hi").to_openai(), {"role": "user", "content": "hi"})

    def test_assistant_with_tool_calls(self) -> None:
        payload = Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})],
        ).to_openai()

        self.assertEqual(payload["role"], "assistant")
        self.assertIsNone(payload["content"])
        self.assertEqual(
            payload["tool_calls"],
            [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'},
                }
            ],
        )

    def test_tool_result_message(self) -> None:
        payload = Message(role="tool", content="42", tool_call_id="c1", name="add").to_openai()
        self.assertEqual(payload, {"role": "tool", "content": "42", "tool_call_id": "c1", "name": "add"})

    def test_repr(self) -> None:
        self.assertIn("add", str(Message(role="assistant", tool_calls=[ToolCall("c", "add", {})])))
        self.assertEqual(str(Message(role="user", content="hi")), "[user] hi")


class TestToolCall(unittest.TestCase):
    def test_from_openai(self) -> None:
        call = ToolCall.from_openai({"id": "x", "function": {"name": "f", "arguments": '{"k": "v"}'}})
        self.assertEqual((call.id, call.name, call.arguments), ("x", "f", {"k": "v"}))

    def test_missing_arguments(self) -> None:
        self.assertEqual(ToolCall.from_openai({"id": "x", "function": {"name": "f"}}).arguments, {})

    def test_dict_arguments(self) -> None:
        call = ToolCall.from_openai({"id": "x", "function": {"name": "f", "arguments": {"a": 1}}})
        self.assertEqual(call.arguments, {"a": 1})


class TestUsage(unittest.TestCase):
    def test_add(self) -> None:
        total = Usage(1, 2, 3) + Usage(10, 20, 30)
        self.assertEqual((total.prompt_tokens, total.completion_tokens, total.total_tokens), (11, 22, 33))

    def test_iadd(self) -> None:
        usage = Usage()
        usage += Usage(1, 1, 2)
        self.assertEqual(usage.total_tokens, 2)

    def test_original_not_mutated(self) -> None:
        first = Usage(1, 1, 2)
        _ = first + Usage(1, 1, 2)
        self.assertEqual(first.total_tokens, 2)


if __name__ == "__main__":
    unittest.main()

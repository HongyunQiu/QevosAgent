#!/usr/bin/env python3
"""Minimal sanity tests for parse_response robustness.

Run:
  python3 tests_parse_response.py
"""

from agent.core.llm import parse_response


def assert_ok(name: str, raw: str):
    a = parse_response(raw)
    assert a.type.name in ("TOOL_CALL", "DONE"), f"{name} unexpected type: {a.type} thought={a.thought!r}"


def main():
    # 1) Prefix text before JSON
    assert_ok(
        "prefix_text",
        "First tool call.\n{\n  \"thought\": \"x\",\n  \"action\": \"tool_call\",\n  \"tool\": \"scratchpad_set\",\n  \"args\": {\"content\": \"hi\"}\n}",
    )

    # 2) Multiple JSON objects back-to-back (should parse the first)
    assert_ok(
        "two_json",
        "{\"thought\":\"x\",\"action\":\"tool_call\",\"tool\":\"scratchpad_set\",\"args\":{\"content\":\"a\"}}\n{\"thought\":\"y\",\"action\":\"done\",\"final_answer\":\"b\"}",
    )

    # 3) Fenced json
    assert_ok(
        "fenced",
        "```json\n{\"thought\":\"x\",\"action\":\"done\",\"final_answer\":\"ok\"}\n```",
    )

    # 4) Missing outer closing brace — the death-loop case (run 20260630-151151):
    #    model drops the outer } after a nested args object. Must recover via the
    #    dependency-free balanced-completion repair, even without json_repair.
    assert_ok(
        "missing_outer_brace",
        '{\n"thought": "check proxy",\n"action": "tool_call",\n"tool": "shell",\n'
        '"args": {"command": "curl -s http://127.0.0.1:7897 && echo ok",\n"timeout": 10\n}',
    )

    # 5) Dangling unterminated string + missing closers — balanced completion closes both.
    assert_ok(
        "dangling_string",
        '{"thought":"x","action":"tool_call","tool":"shell","args":{"command":"echo hello',
    )

    print("OK")


if __name__ == "__main__":
    main()

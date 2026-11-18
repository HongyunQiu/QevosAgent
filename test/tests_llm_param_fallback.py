#!/usr/bin/env python3
"""Offline regressions for OpenAI-compatible parameter fallback."""

import json
import unittest
from types import SimpleNamespace

from httpx import Request

from agent.core.llm import OpenAIBackend


class Status400(Exception):
    status_code = 400


class SequenceCreate:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def bare_backend(outcomes):
    backend = object.__new__(OpenAIBackend)
    create = SequenceCreate(outcomes)
    backend.client = SimpleNamespace(
        chat=SimpleNamespace(completions=create),
    )
    backend.temperature = 0.8
    backend._logprobs_enabled = True
    backend._use_response_format = True
    backend._suppressed_params = set()
    backend._suppressed_create_params = set()
    backend._suppressed_extra_body_params = set()
    return backend, create


def success_response(content="ok"):
    message = SimpleNamespace(content=content, reasoning_content=None)
    choice = SimpleNamespace(finish_reason="stop", logprobs=None, message=message)
    return SimpleNamespace(choices=[choice])


class CaptureBackend(OpenAIBackend):
    def _create_with_retry(self, kwargs, is_stream=False):
        self.captured = kwargs
        return success_response()


def capture_backend(*, official=False):
    backend = object.__new__(CaptureBackend)
    backend.model = "model"
    backend.temperature = 0.8
    backend.thinking_budget = 100
    backend._is_official_openai = official
    backend._use_response_format = True
    backend._logprobs_enabled = True
    backend._suppressed_params = set()
    backend._suppressed_create_params = set()
    backend._suppressed_extra_body_params = set()
    backend.last_token_logprobs = None
    return backend


class ParameterStripTests(unittest.TestCase):
    def test_normal_provider_is_one_call_and_kwargs_unchanged(self):
        backend, create = bare_backend(["ok"])
        kwargs = {
            "model": "model",
            "temperature": 0.8,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
        }
        expected = {
            "model": "model",
            "temperature": 0.8,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
        }

        self.assertEqual(backend._try_create_with_param_strip(kwargs), "ok")
        self.assertEqual(create.calls, [expected])
        self.assertEqual(kwargs, expected)

    def test_non_parameter_error_is_raised_unchanged_without_retry(self):
        original = RuntimeError("ordinary provider failure")
        backend, create = bare_backend([original])

        with self.assertRaises(RuntimeError) as caught:
            backend._try_create_with_param_strip({"model": "model"})

        self.assertIs(caught.exception, original)
        self.assertEqual(len(create.calls), 1)
        self.assertEqual(backend._suppressed_params, set())
        self.assertEqual(backend._suppressed_create_params, set())
        self.assertEqual(backend._suppressed_extra_body_params, set())

    def test_sequential_structured_400s_are_all_stripped(self):
        backend, create = bare_backend([
            Status400("response_format is not supported"),
            Status400("Unknown parameter: logprobs"),
            Status400('Unknown name "chat_template_kwargs": Cannot find field'),
            "ok",
        ])
        kwargs = {
            "model": "model",
            "response_format": {"type": "json_object"},
            "logprobs": True,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
        }

        self.assertEqual(backend._try_create_with_param_strip(kwargs), "ok")
        self.assertEqual(len(create.calls), 4)
        self.assertFalse(backend._use_response_format)
        self.assertFalse(backend._logprobs_enabled)
        self.assertEqual(
            backend._suppressed_extra_body_params,
            {"chat_template_kwargs"},
        )
        self.assertNotIn("extra_body", kwargs)

    def test_typeerror_can_be_followed_by_http_param_rejection(self):
        backend, create = bare_backend([
            TypeError(
                "Completions.create() got an unexpected keyword argument "
                "'legacy_option'"
            ),
            Status400('Unknown name "thinking": Cannot find field'),
            "ok",
        ])
        kwargs = {
            "model": "model",
            "legacy_option": True,
            "extra_body": {"thinking": {"type": "enabled"}},
        }

        self.assertEqual(backend._try_create_with_param_strip(kwargs), "ok")
        self.assertEqual(len(create.calls), 3)
        self.assertEqual(backend._suppressed_create_params, {"legacy_option"})
        self.assertEqual(backend._suppressed_extra_body_params, {"thinking"})

    def test_repeated_error_without_progress_stops_immediately(self):
        backend, create = bare_backend([
            Status400("Unknown parameter: sdk_injected"),
            Status400("Unknown parameter: sdk_injected"),
        ])

        with self.assertRaises(Status400):
            backend._try_create_with_param_strip({"model": "model"})
        self.assertEqual(len(create.calls), 2)


class RequestConstructionTests(unittest.TestCase):
    def test_normal_compatible_request_shape_is_preserved(self):
        backend = capture_backend(official=False)

        result = backend._call_api(
            [],
            "system",
            256,
            True,
            thinking_budget=100,
            continue_final=True,
            want_logprobs=True,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(backend.captured, {
            "model": "model",
            "messages": [{"role": "system", "content": "system"}],
            "temperature": 0.8,
            "max_tokens": 256,
            "response_format": {"type": "json_object"},
            "logprobs": True,
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": True},
                "thinking": {"type": "enabled", "budget_tokens": 100},
                "continue_final_message": True,
                "add_generation_prompt": False,
            },
        })

    def test_extra_body_suppression_does_not_become_top_level_kwarg(self):
        backend = capture_backend(official=False)
        backend._suppressed_extra_body_params = {
            "chat_template_kwargs",
            "thinking",
            "add_generation_prompt",
        }

        backend._call_api(
            [],
            "system",
            256,
            False,
            thinking_budget=100,
            continue_final=True,
        )

        self.assertNotIn("chat_template_kwargs", backend.captured)
        self.assertNotIn("thinking", backend.captured)
        self.assertNotIn("add_generation_prompt", backend.captured)
        self.assertEqual(
            backend.captured["extra_body"],
            {"continue_final_message": True},
        )

    def test_official_request_still_omits_compatible_extensions(self):
        backend = capture_backend(official=True)
        backend._call_api([], "system", 256, True, want_logprobs=True)

        self.assertEqual(backend.captured["max_completion_tokens"], 256)
        self.assertNotIn("max_tokens", backend.captured)
        self.assertNotIn("extra_body", backend.captured)


class HttpRequestRewriteTests(unittest.TestCase):
    def test_rewrite_recomputes_length_and_preserves_extensions(self):
        captured = []
        transport = SimpleNamespace(send=lambda request, **kwargs: captured.append(request))
        backend = object.__new__(OpenAIBackend)
        backend._suppressed_params = {"bad"}
        backend.client = SimpleNamespace(_client=transport)
        backend._wrap_create_method()
        original = Request(
            "POST",
            "https://example.test/v1/chat/completions",
            headers={"content-type": "application/json"},
            content=json.dumps({"model": "m", "bad": True}).encode(),
            extensions={"trace": {"id": 7}},
        )

        backend.client._client.send(original)

        self.assertEqual(len(captured), 1)
        rewritten = captured[0]
        self.assertEqual(json.loads(rewritten.content), {"model": "m"})
        self.assertEqual(int(rewritten.headers["content-length"]), len(rewritten.content))
        self.assertEqual(rewritten.extensions, {"trace": {"id": 7}})


if __name__ == "__main__":
    unittest.main(verbosity=2)

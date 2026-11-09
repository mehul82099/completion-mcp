#!/usr/bin/env python3
"""Stdio-level tests for completion-mcp.

Every test drives the real server process over newline-delimited JSON-RPC on
stdin/stdout, the same framing a client would use.
"""

import json
import os
import subprocess
import sys
import unittest

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "completion_mcp.py")


def run_session(messages, env_extra=None):
    """Send messages (dicts or raw strings), return parsed response dicts."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    payload = "".join(
        (m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages
    )
    proc = subprocess.run(
        [sys.executable, SERVER],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines], proc


def req(i, method, params=None):
    m = {"jsonrpc": "2.0", "id": i, "method": method}
    if params is not None:
        m["params"] = params
    return m


def complete(i, ref, name, value, context_args=None):
    params = {"ref": ref, "argument": {"name": name, "value": value}}
    if context_args is not None:
        params["context"] = {"arguments": context_args}
    return req(i, "completion/complete", params)


def initialize(i=1, meta_style=False):
    if meta_style:
        return req(i, "initialize", {
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
        })
    return req(i, "initialize", {"protocolVersion": "2025-06-18"})


class InitializeTests(unittest.TestCase):
    def test_declares_completions_capability(self):
        responses, _ = run_session([initialize()])
        result = responses[0]["result"]
        self.assertIn("completions", result["capabilities"])
        self.assertEqual(result["capabilities"]["completions"], {})
        self.assertEqual(result["serverInfo"]["name"], "completion-mcp")

    def test_legacy_protocol_version_is_echoed(self):
        responses, _ = run_session([initialize()])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")

    def test_meta_style_protocol_version(self):
        responses, _ = run_session([initialize(meta_style=True)])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2026-07-28")

    def test_unknown_protocol_version_falls_back_to_latest(self):
        responses, _ = run_session(
            [req(1, "initialize", {"protocolVersion": "1999-01-01"})])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2026-07-28")

    def test_notification_gets_no_response(self):
        messages = [initialize(),
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    req(2, "ping")]
        responses, _ = run_session(messages)
        self.assertEqual([r["id"] for r in responses], [1, 2])


class CompletionTests(unittest.TestCase):
    def test_language_prefix_match(self):
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/prompt", "name": "translate"},
                     "source_lang", "hi"),
        ])
        completion = responses[1]["result"]["completion"]
        self.assertEqual(completion["values"], ["hi"])
        self.assertEqual(completion["total"], 1)
        self.assertFalse(completion["hasMore"])

    def test_language_name_match(self):
        # "engl" is a prefix of the label "English", not of the code "en".
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/prompt", "name": "translate"},
                     "source_lang", "engl"),
        ])
        self.assertEqual(responses[1]["result"]["completion"]["values"], ["en"])

    def test_result_type_envelope(self):
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/prompt", "name": "translate"},
                     "source_lang", "en"),
        ])
        self.assertEqual(responses[1]["result"]["resultType"], "complete")

    def test_context_excludes_resolved_source_language(self):
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/prompt", "name": "translate"},
                     "target_lang", "h", {"source_lang": "hi"}),
        ])
        values = responses[1]["result"]["completion"]["values"]
        # Without the exclusion, "hi" would be the only prefix match for "h".
        self.assertEqual(values, [])

    def test_register_completion(self):
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/prompt", "name": "translate"},
                     "register", "fo"),
        ])
        self.assertEqual(responses[1]["result"]["completion"]["values"], ["formal"])

    def test_response_is_capped_at_100_with_total_and_hasmore(self):
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/prompt", "name": "grep_docs"},
                     "keyword", "keyword-"),
        ])
        completion = responses[1]["result"]["completion"]
        self.assertEqual(len(completion["values"]), 100)
        self.assertEqual(completion["total"], 150)
        self.assertTrue(completion["hasMore"])

    def test_refined_prefix_narrows_pool(self):
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/prompt", "name": "grep_docs"},
                     "keyword", "keyword-14"),
        ])
        completion = responses[1]["result"]["completion"]
        self.assertEqual(completion["values"], ["keyword-140", "keyword-141",
                                                "keyword-142", "keyword-143",
                                                "keyword-144", "keyword-145",
                                                "keyword-146", "keyword-147",
                                                "keyword-148", "keyword-149"])
        self.assertFalse(completion["hasMore"])

    def test_file_type_completion(self):
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/prompt", "name": "grep_docs"},
                     "file_type", "y"),
        ])
        self.assertEqual(responses[1]["result"]["completion"]["values"], ["yaml"])

    def test_resource_template_language(self):
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/resource", "uri": "snippet://{language}/{name}"},
                     "language", "p"),
        ])
        self.assertEqual(responses[1]["result"]["completion"]["values"], ["python"])

    def test_resource_template_name_uses_context(self):
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/resource", "uri": "snippet://{language}/{name}"},
                     "name", "h", {"language": "go"}),
        ])
        values = responses[1]["result"]["completion"]["values"]
        self.assertEqual(values, ["hello-world", "http-server"])

    def test_template_name_without_context_aggregates(self):
        responses, _ = run_session([
            initialize(),
            complete(2, {"type": "ref/resource", "uri": "snippet://{language}/{name}"},
                     "name", "hello"),
        ])
        # hello-world exists in three languages but is deduplicated.
        self.assertEqual(responses[1]["result"]["completion"]["values"], ["hello-world"])


class CompletionErrorTests(unittest.TestCase):
    def _error(self, message, env_extra=None):
        responses, _ = run_session([initialize(), message], env_extra=env_extra)
        return responses[1]

    def test_unknown_prompt(self):
        r = self._error(complete(2, {"type": "ref/prompt", "name": "nope"}, "x", ""))
        self.assertEqual(r["error"]["code"], -32602)

    def test_unknown_argument_for_prompt(self):
        r = self._error(complete(2, {"type": "ref/prompt", "name": "translate"}, "color", "r"))
        self.assertEqual(r["error"]["code"], -32602)

    def test_unknown_template_uri(self):
        r = self._error(complete(2, {"type": "ref/resource", "uri": "nope://{x}"}, "x", ""))
        self.assertEqual(r["error"]["code"], -32602)

    def test_unknown_template_argument(self):
        r = self._error(complete(2, {"type": "ref/resource", "uri": "snippet://{language}/{name}"}, "page", ""))
        self.assertEqual(r["error"]["code"], -32602)

    def test_unsupported_ref_type(self):
        r = self._error(complete(2, {"type": "ref/tool", "name": "x"}, "x", ""))
        self.assertEqual(r["error"]["code"], -32602)

    def test_missing_argument(self):
        r = self._error(req(2, "completion/complete",
                            {"ref": {"type": "ref/prompt", "name": "translate"}}))
        self.assertEqual(r["error"]["code"], -32602)

    def test_rate_limit(self):
        messages = [initialize()] + [
            complete(i + 1, {"type": "ref/prompt", "name": "translate"},
                     "register", "f")
            for i in range(1, 4)
        ]
        responses, _ = run_session(messages, env_extra={"COMPLETION_MCP_RATE_LIMIT": "2"})
        self.assertIn("result", responses[1])
        self.assertIn("result", responses[2])
        self.assertEqual(responses[3]["error"]["code"], -32603)


class ProtocolTests(unittest.TestCase):
    def test_unknown_method(self):
        responses, _ = run_session([initialize(), req(2, "tools/list")])
        self.assertEqual(responses[1]["error"]["code"], -32601)

    def test_parse_error(self):
        responses, _ = run_session(["{not json"])
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertIsNone(responses[0]["id"])

    def test_prompts_list(self):
        responses, _ = run_session([initialize(), req(2, "prompts/list")])
        names = [p["name"] for p in responses[1]["result"]["prompts"]]
        self.assertEqual(names, ["translate", "grep_docs"])

    def test_prompts_get_translate(self):
        responses, _ = run_session([
            initialize(),
            req(2, "prompts/get", {"name": "translate",
                                   "arguments": {"source_lang": "en",
                                                 "target_lang": "hi"}}),
        ])
        messages = responses[1]["result"]["messages"]
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("from en to hi", messages[0]["content"]["text"])

    def test_prompts_get_unknown(self):
        responses, _ = run_session(
            [initialize(), req(2, "prompts/get", {"name": "nope"})])
        self.assertEqual(responses[1]["error"]["code"], -32602)

    def test_resource_templates_list(self):
        responses, _ = run_session([initialize(), req(2, "resources/templates/list")])
        templates = responses[1]["result"]["resourceTemplates"]
        self.assertEqual(templates[0]["uriTemplate"], "snippet://{language}/{name}")

    def test_ids_stay_aligned_across_a_batch(self):
        messages = [initialize()] + [
            complete(i + 2, {"type": "ref/prompt", "name": "translate"},
                     "source_lang", code)
            for i, code in enumerate(["h", "e", "j"])
        ]
        responses, _ = run_session(messages)
        self.assertEqual([r["id"] for r in responses], [1, 2, 3, 4])
        self.assertEqual(responses[1]["result"]["completion"]["values"][0], "hi")
        self.assertEqual(responses[2]["result"]["completion"]["values"][0], "en")
        self.assertEqual(responses[3]["result"]["completion"]["values"][0], "ja")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for codexfix. Run: python3 -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import codexfix  # noqa: E402


def body(items, model="deepseek-flash"):
    return {"model": model, "input": items}


class RewriteRules(unittest.TestCase):
    def test_missing_call_id_is_rewritten(self):
        doc = body(
            [
                {"type": "message", "role": "user", "content": []},
                {
                    "type": "function_call_output",
                    "id": "fco_1",
                    "name": "automation_update",
                    "namespace": "codex_app",
                    "output": "<heartbeat>x</heartbeat>",
                },
            ]
        )
        new, stats = codexfix.rewrite_payload(doc)
        self.assertEqual(stats["unidentified"], 1)
        self.assertEqual(stats["unpaired"], 0)
        rewritten = new["input"][1]
        self.assertEqual(rewritten["type"], "message")
        self.assertEqual(rewritten["role"], "user")
        texts = [part["text"] for part in rewritten["content"]]
        self.assertIn("[tool output: automation_update]", texts)
        self.assertIn("<heartbeat>x</heartbeat>", texts)

    def test_empty_call_id_is_rewritten(self):
        doc = body([{"type": "function_call_output", "call_id": "", "name": "t", "output": "x"}])
        _, stats = codexfix.rewrite_payload(doc)
        self.assertEqual(stats["unidentified"], 1)

    def test_unpaired_call_id_is_left_alone_by_default(self):
        doc = body([{"type": "function_call_output", "call_id": "call_ghost", "name": "t", "output": "x"}])
        new, stats = codexfix.rewrite_payload(doc)
        self.assertEqual(stats["unidentified"], 0)
        self.assertEqual(stats["unpaired"], 0)
        self.assertEqual(new["input"][0]["type"], "function_call_output")

    def test_unpaired_call_id_rewritten_when_opted_in(self):
        doc = body([{"type": "function_call_output", "call_id": "call_ghost", "name": "t", "output": "x"}])
        new, stats = codexfix.rewrite_payload(doc, repair_unpaired=True)
        self.assertEqual(stats["unpaired"], 1)
        self.assertEqual(new["input"][0]["type"], "message")

    def test_intact_pair_is_untouched(self):
        doc = body(
            [
                {"type": "function_call", "call_id": "call_1", "name": "t", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_1", "name": "t", "output": "ok"},
            ]
        )
        new, stats = codexfix.rewrite_payload(doc)
        self.assertEqual(stats["unidentified"] + stats["unpaired"], 0)
        self.assertIs(new, doc)

    def test_local_shell_call_counts_as_pairing_partner(self):
        doc = body(
            [
                {"type": "local_shell_call", "call_id": "call_shell"},
                {"type": "function_call_output", "call_id": "call_shell", "output": "stdout"},
            ]
        )
        _, stats = codexfix.rewrite_payload(doc, repair_unpaired=True)
        self.assertEqual(stats["unpaired"], 0)

    def test_custom_tool_call_output_is_covered(self):
        doc = body(
            [
                {
                    "type": "custom_tool_call_output",
                    "id": "ctco_1",
                    "name": "send_message_to_thread",
                    "output": "<codex_delegation>report</codex_delegation>",
                }
            ]
        )
        new, stats = codexfix.rewrite_payload(doc)
        self.assertEqual(stats["unidentified"], 1)
        self.assertEqual(new["input"][0]["type"], "message")

    def test_structured_output_keeps_images_and_drops_encrypted(self):
        doc = body(
            [
                {
                    "type": "function_call_output",
                    "id": "fco_1",
                    "name": "screenshot",
                    "output": [
                        {"type": "input_text", "text": "here"},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
                        {"type": "encrypted_content", "encrypted_content": "secret"},
                    ],
                }
            ]
        )
        new, _ = codexfix.rewrite_payload(doc)
        content = new["input"][0]["content"]
        kinds = [part["type"] for part in content]
        self.assertIn("input_image", kinds)
        self.assertNotIn("secret", json.dumps(content))
        self.assertIn("[encrypted content omitted]", json.dumps(content))

    def test_model_filter_skips_other_models(self):
        doc = body([{"type": "function_call_output", "id": "f", "name": "t", "output": "x"}], model="gpt-5.5")
        new, stats = codexfix.rewrite_payload(doc, models=["deepseek"])
        self.assertEqual(stats["unidentified"], 0)
        self.assertIs(new, doc)

    def test_role_is_configurable_but_defaults_to_user(self):
        doc = body([{"type": "function_call_output", "id": "f", "name": "t", "output": "x"}])
        default_new, _ = codexfix.rewrite_payload(doc)
        dev_new, _ = codexfix.rewrite_payload(doc, role="developer")
        self.assertEqual(default_new["input"][0]["role"], "user")
        self.assertEqual(dev_new["input"][0]["role"], "developer")

    def test_marker_can_be_disabled(self):
        doc = body([{"type": "function_call_output", "id": "f", "name": "t", "output": "x"}])
        new, _ = codexfix.rewrite_payload(doc, marker=False)
        self.assertEqual([p["text"] for p in new["input"][0]["content"]], ["x"])

    def test_body_bytes_are_returned_untouched_when_nothing_matches(self):
        raw = b'{"model":"deepseek-flash","input":[{"type":"message","role":"user","content":[]}]}'
        out, stats = codexfix.rewrite_body_bytes(
            raw, role="user", repair_unpaired=False, marker=True, models=None
        )
        self.assertIs(out, raw)
        self.assertFalse(stats["unidentified"] or stats["unpaired"])

    def test_invalid_json_is_passed_through(self):
        raw = b'{"not":"json"'
        out, stats = codexfix.rewrite_body_bytes(
            raw, role="user", repair_unpaired=False, marker=True, models=None
        )
        self.assertEqual(out, raw)
        self.assertFalse(stats["parsed"])


class Doctor(unittest.TestCase):
    def _write_session(self, root, name, lines):
        path = os.path.join(root, "sessions", "2026", "09", "11", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        return path

    def test_doctor_finds_orphans_and_real_failures_only(self):
        with tempfile.TemporaryDirectory() as home:
            self._write_session(
                home,
                "rollout-broken.jsonl",
                [
                    {"type": "session_meta", "payload": {"thread_source": "automation"}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "id": "fco_1",
                            "name": "automation_update",
                            "output": "<heartbeat>x</heartbeat>",
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "error": {
                                "message": "Failed to deserialize the JSON body into the target type: "
                                "input: missing field `call_id` at line 1 column 42"
                            },
                        },
                    },
                ],
            )
            # A session that only *quotes* the error inside a tool output must not count
            self._write_session(
                home,
                "rollout-quoted.jsonl",
                [
                    {"type": "session_meta", "payload": {"thread_source": "user"}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_ok",
                            "name": "exec_command",
                            "output": "missing field `call_id` at line 1 column 9",
                        },
                    },
                    {"type": "response_item", "payload": {"type": "function_call", "call_id": "call_ok"}},
                ],
            )
            report = codexfix.scan_sessions(home)
            self.assertEqual(report["sessions_scanned"], 2)
            self.assertEqual(len(report["orphans"]), 1)
            self.assertEqual(report["by_name"]["automation_update"], 1)
            self.assertEqual(len(report["error_sessions"]), 1)
            self.assertIn("rollout-broken.jsonl", report["error_sessions"][0][0])

    def test_doctor_reports_clean_home(self):
        with tempfile.TemporaryDirectory() as home:
            report = codexfix.scan_sessions(home)
            self.assertEqual(report["sessions_scanned"], 0)
            self.assertEqual(report["orphans"], [])
            self.assertEqual(report["error_sessions"], [])


class UpstreamParsing(unittest.TestCase):
    def test_parse_upstream(self):
        # RFC 5737 documentation addresses only
        self.assertEqual(codexfix.parse_upstream("http://192.0.2.10:8080"), ("192.0.2.10", 8080, False))
        self.assertEqual(codexfix.parse_upstream("http://192.0.2.10:8080/"), ("192.0.2.10", 8080, False))
        self.assertEqual(codexfix.parse_upstream("https://api.example.com/v1"), ("api.example.com", 443, True))


if __name__ == "__main__":
    unittest.main()

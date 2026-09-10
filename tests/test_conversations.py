"""Synthetic contract tests for bidirectional staged conversation conversion."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from converter import conversations


TIME = "2026-01-01T00:00:00Z"
IMAGE_DATA = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def write_jsonl(path: Path, records: list[dict]) -> bytes:
    raw = b"".join(
        (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for record in records
    )
    path.write_bytes(raw)
    return raw


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def claude_row(row_type: str, row_id: str, parent: str | None, blocks: list[dict]) -> dict:
    message = {"role": row_type, "content": blocks}
    if row_type == "assistant":
        message.update({
            "id": "msg_" + row_id,
            "type": "message",
            "model": "fixture-model",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })
    return {
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/synthetic/work",
        "sessionId": "source-session",
        "version": "2.1.263",
        "gitBranch": "main",
        "type": row_type,
        "uuid": row_id,
        "timestamp": TIME,
        "message": message,
    }


def last_prompt(leaf: str) -> dict:
    return {"type": "last-prompt", "sessionId": "source-session", "leafUuid": leaf, "lastPrompt": "fixture"}


def boundary(row_id: str, metadata: dict | None = None) -> dict:
    return {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/synthetic/work",
        "sessionId": "source-session",
        "version": "2.1.263",
        "gitBranch": "main",
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": row_id,
        "timestamp": TIME,
        "content": "Conversation compacted",
        "compactMetadata": {"trigger": "manual", "preTokens": 100, **(metadata or {})},
    }


def codex_line(record_type: str, payload: dict) -> dict:
    return {"timestamp": TIME, "type": record_type, "payload": payload}


def codex_header() -> list[dict]:
    return [
        codex_line("session_meta", {
            "id": "codex-session",
            "session_id": "codex-session",
            "timestamp": TIME,
            "cwd": "/synthetic/work",
            "model": "fixture-model",
        }),
        codex_line("turn_context", {
            "turn_id": "turn-1",
            "cwd": "/synthetic/work",
            "current_date": "2026-01-01",
            "timezone": "UTC",
            "model": "fixture-model",
            "approval_policy": "never",
            "sandbox_policy": {"type": "read-only"},
            "summary": "auto",
        }),
    ]


def codex_message(role: str, blocks: list[dict]) -> dict:
    return {"type": "message", "role": role, "content": blocks}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="conversation-converter-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.jsonl"

    def stage(self, records, source="claude", target="codex", name="out", **kwargs):
        raw = write_jsonl(self.source, records)
        output = self.root / name
        report = conversations.stage_conversation(
            self.source, output, source, target, **kwargs
        )
        return raw, output, report

    def linear_claude(self, *, duplicate=False):
        root = claude_row("user", "u-root", None, [
            {"type": "text", "text": "ROOT_TEXT"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": IMAGE_DATA}},
        ])
        call = claude_row("assistant", "a-call", "u-root", [
            {"type": "text", "text": "CALLING"},
            {"type": "tool_use", "id": "tool-call-1", "name": "FixtureTool", "input": {
                "query": "café", "nested": {"items": [1, True, None]},
            }},
        ])
        result = claude_row("user", "u-result", "a-call", [{
            "type": "tool_result",
            "tool_use_id": "tool-call-1",
            "is_error": False,
            "content": [
                {"type": "text", "text": "TOOL_ONLY_FACT"},
                {"type": "text", "text": "SECOND_RESULT_BLOCK"},
            ],
        }])
        final = claude_row("assistant", "a-final", "u-result", [{"type": "text", "text": "FINAL_TEXT"}])
        wrong_user = claude_row("user", "u-wrong", "u-root", [{"type": "text", "text": "WRONG_BRANCH"}])
        wrong_final = claude_row("assistant", "a-wrong", "u-wrong", [{"type": "text", "text": "WRONG_FINAL"}])
        rows = [root, call, result, final, wrong_user, wrong_final]
        if duplicate:
            rows.insert(1, root)
        rows.append(last_prompt("a-final"))
        return rows

    def linear_codex(self):
        arguments = json.dumps({"query": "café", "nested": [1, True, None]}, ensure_ascii=False, separators=(",", ":"))
        output = json.dumps({"fact": "TOOL_ONLY_FACT", "items": ["a", "b"]}, separators=(",", ":"))
        return [
            *codex_header(),
            codex_line("response_item", codex_message("user", [
                {"type": "input_text", "text": "ROOT_TEXT"},
                {"type": "input_image", "image_url": f"data:image/png;base64,{IMAGE_DATA}", "detail": "auto"},
            ])),
            codex_line("response_item", codex_message("assistant", [{"type": "output_text", "text": "CALLING"}])),
            codex_line("response_item", {
                "type": "function_call", "id": "fc-1", "name": "FixtureTool",
                "arguments": arguments, "call_id": "call-1",
            }),
            codex_line("response_item", {
                "type": "function_call_output", "id": "fco-1", "call_id": "call-1", "output": output,
            }),
            codex_line("response_item", codex_message("assistant", [{"type": "output_text", "text": "FINAL_TEXT"}])),
        ]

    def test_claude_to_codex_preserves_selected_payloads_and_archive(self):
        raw, output, report = self.stage(self.linear_claude())
        self.assertEqual((output / "archive/original.jsonl").read_bytes(), raw)
        rollout = read_jsonl(output / "generated/rollout.jsonl")
        items = [row["payload"] for row in rollout if row["type"] == "response_item"]
        self.assertEqual([item["type"] for item in items], [
            "message", "message", "function_call", "function_call_output", "message",
        ])
        self.assertEqual(items[0]["content"][0]["text"], "ROOT_TEXT")
        self.assertEqual(items[0]["content"][1]["image_url"], f"data:image/png;base64,{IMAGE_DATA}")
        self.assertEqual(json.loads(items[2]["arguments"]), {"query": "café", "nested": {"items": [1, True, None]}})
        tagged = json.loads(items[3]["output"])[conversations.CLAUDE_TOOL_RESULT_TAG]
        self.assertEqual(tagged["content"][0]["text"], "TOOL_ONLY_FACT")
        self.assertIs(tagged["is_error"], False)
        self.assertTrue(items[3]["id"].startswith("fco_"))
        self.assertNotIn("WRONG_BRANCH", json.dumps(rollout))
        self.assertEqual(report["selected_branch"], "a-final")
        self.assertEqual(report["active_source_indices"], [0, 1, 2, 3])
        self.assertTrue(report["strict_continuation_ready"])

    def test_explicit_branch_overrides_last_prompt(self):
        _, output, report = self.stage(self.linear_claude(), branch="a-wrong")
        text = (output / "generated/rollout.jsonl").read_text()
        self.assertIn("WRONG_BRANCH", text)
        self.assertNotIn("FINAL_TEXT", text)
        self.assertEqual(report["branch_source"], "explicit")

    def test_ambiguous_branch_rejects_but_retains_archive(self):
        records = [
            claude_row("user", "root", None, [{"type": "text", "text": "root"}]),
            claude_row("assistant", "left", "root", [{"type": "text", "text": "left"}]),
            claude_row("assistant", "right", "root", [{"type": "text", "text": "right"}]),
        ]
        raw = write_jsonl(self.source, records)
        output = self.root / "ambiguous"
        with self.assertRaises(conversations.AmbiguousBranchError):
            conversations.stage_conversation(self.source, output, "claude", "codex")
        self.assertEqual((output / "archive/original.jsonl").read_bytes(), raw)
        self.assertFalse((output / "generated").exists())
        self.assertEqual(json.loads((output / "preservation-report.json").read_text())["status"], "rejected")

    def test_unknown_parent_and_cycle_reject(self):
        cases = [
            [claude_row("user", "leaf", "absent", [{"type": "text", "text": "x"}]), last_prompt("leaf")],
            [
                claude_row("user", "one", "two", [{"type": "text", "text": "1"}]),
                claude_row("assistant", "two", "one", [{"type": "text", "text": "2"}]),
                last_prompt("two"),
            ],
        ]
        for index, records in enumerate(cases):
            with self.subTest(index=index):
                write_jsonl(self.source, records)
                with self.assertRaises(conversations.AmbiguousBranchError):
                    conversations.stage_conversation(self.source, self.root / f"bad-{index}", "claude", "codex")

    def test_claude_compaction_preserves_declared_order(self):
        root = claude_row("user", "pre-root", None, [{"type": "text", "text": "PRE_ROOT"}])
        retained = claude_row("assistant", "retained", "pre-root", [{"type": "text", "text": "RETAINED"}])
        edge = boundary("boundary", {"preservedMessages": {"anchorUuid": "summary", "uuids": ["pre-root", "retained"]}})
        summary = claude_row("user", "summary", "boundary", [{"type": "text", "text": "SUMMARY"}])
        summary["isCompactSummary"] = True
        post = claude_row("assistant", "post", "summary", [{"type": "text", "text": "POST"}])
        _, output, report = self.stage([root, retained, edge, summary, post, last_prompt("post")])
        rollout = read_jsonl(output / "generated/rollout.jsonl")
        compacted = next(row for row in rollout if row["type"] == "compacted")
        replacement = compacted["payload"]["replacement_history"]
        self.assertEqual([item["content"][0]["text"] for item in replacement], ["SUMMARY", "PRE_ROOT", "RETAINED"])
        after = rollout[rollout.index(compacted) + 1 :]
        self.assertEqual(after[0]["payload"]["content"][0]["text"], "POST")
        self.assertTrue(report["compacted"])

    def test_preserved_segment_follows_ancestry_without_sibling_contamination(self):
        root = claude_row("user", "root", None, [{"type": "text", "text": "ROOT"}])
        head = claude_row("assistant", "head", "root", [{"type": "text", "text": "HEAD"}])
        wrong = claude_row("user", "wrong", "head", [{"type": "text", "text": "WRONG_BRANCH"}])
        tail = claude_row("user", "tail", "head", [{"type": "text", "text": "TAIL"}])
        edge = boundary("boundary", {"preservedSegment": {
            "headUuid": "head", "tailUuid": "tail", "anchorUuid": "boundary",
        }})
        summary = claude_row("user", "summary", "boundary", [{"type": "text", "text": "SUMMARY"}])
        summary["isCompactSummary"] = True
        post = claude_row("assistant", "post", "summary", [{"type": "text", "text": "POST"}])
        _, output, _ = self.stage([root, head, wrong, tail, edge, summary, post, last_prompt("post")])
        rollout = read_jsonl(output / "generated/rollout.jsonl")
        replacement = next(row for row in rollout if row["type"] == "compacted")["payload"]["replacement_history"]
        self.assertEqual([item["content"][0]["text"] for item in replacement], ["HEAD", "TAIL", "SUMMARY"])
        self.assertNotIn("WRONG_BRANCH", json.dumps(rollout))

    def test_unknown_selected_claude_record_and_unknown_preserved_anchor_reject(self):
        future = claude_row("future-active", "future", "root", [{"type": "text", "text": "OPAQUE"}])
        child = claude_row("user", "child", "future", [{"type": "text", "text": "CHILD"}])
        cases = [
            [claude_row("user", "root", None, [{"type": "text", "text": "ROOT"}]), future, child, last_prompt("child")],
        ]
        edge = boundary("edge", {"preservedMessages": {"anchorUuid": "unknown", "uuids": []}})
        summary = claude_row("user", "sum", "edge", [{"type": "text", "text": "SUM"}])
        summary["isCompactSummary"] = True
        cases.append([edge, summary, last_prompt("sum")])
        for index, records in enumerate(cases):
            with self.subTest(index=index):
                write_jsonl(self.source, records)
                with self.assertRaises(conversations.UnsupportedActiveSemanticsError):
                    conversations.stage_conversation(self.source, self.root / f"claude-active-bad-{index}", "claude", "codex")

    def test_unsupported_thinking_and_incomplete_tool_history_reject(self):
        thinking = [
            claude_row("assistant", "think", None, [{"type": "thinking", "thinking": "secret", "signature": "sig"}]),
            last_prompt("think"),
        ]
        missing_output = [
            claude_row("assistant", "call", None, [{"type": "tool_use", "id": "missing", "name": "Tool", "input": {}}]),
            last_prompt("call"),
        ]
        for index, records in enumerate((thinking, missing_output)):
            with self.subTest(index=index):
                write_jsonl(self.source, records)
                with self.assertRaises(conversations.UnsupportedActiveSemanticsError):
                    conversations.stage_conversation(self.source, self.root / f"unsupported-{index}", "claude", "codex")

    def test_context_mismatch_is_explicit_and_never_shortens(self):
        raw = write_jsonl(self.source, self.linear_claude())
        output = self.root / "too-small"
        with self.assertRaises(conversations.ContextMismatchError):
            conversations.stage_conversation(
                self.source, output, "claude", "codex", max_active_bytes=1
            )
        self.assertEqual((output / "archive/original.jsonl").read_bytes(), raw)
        self.assertFalse((output / "generated").exists())

    def test_non_strict_archives_unsupported_without_continuation(self):
        records = [
            claude_row("assistant", "think", None, [{"type": "thinking", "thinking": "x", "signature": "s"}]),
            last_prompt("think"),
        ]
        raw, output, report = self.stage(records, strict=False)
        self.assertEqual(report["status"], "archived-only")
        self.assertEqual((output / "archive/original.jsonl").read_bytes(), raw)
        self.assertFalse((output / "generated").exists())

    def test_codex_to_claude_preserves_text_tool_and_image(self):
        raw, output, report = self.stage(self.linear_codex(), "codex", "claude")
        self.assertEqual((output / "archive/original.jsonl").read_bytes(), raw)
        rows = read_jsonl(output / "generated/claude-session.jsonl")
        messages = [row for row in rows if row.get("type") in {"user", "assistant"}]
        self.assertEqual([row["type"] for row in messages], ["user", "assistant", "assistant", "user", "assistant"])
        self.assertEqual(messages[0]["message"]["content"][0]["text"], "ROOT_TEXT")
        self.assertEqual(messages[0]["message"]["content"][1]["source"]["data"], IMAGE_DATA)
        self.assertEqual(messages[2]["message"]["content"][0]["input"]["query"], "café")
        self.assertEqual(messages[3]["message"]["content"][0]["content"], json.dumps({"fact": "TOOL_ONLY_FACT", "items": ["a", "b"]}, separators=(",", ":")))
        self.assertEqual(rows[-1]["type"], "last-prompt")
        self.assertEqual(report["selected_branch"], "codex-session")

    def test_codex_compaction_maps_to_preserved_messages(self):
        replacement = [
            codex_message("user", [{"type": "input_text", "text": "SUMMARY"}]),
            {"type": "function_call", "id": "fc", "name": "Tool", "arguments": "{\"x\":1}", "call_id": "call"},
            {"type": "function_call_output", "id": "fco", "call_id": "call", "output": "RESULT"},
        ]
        records = [
            *codex_header(),
            codex_line("response_item", codex_message("user", [{"type": "input_text", "text": "PRE"}])),
            codex_line("compacted", {"message": "archived compaction", "replacement_history": replacement}),
            codex_line("response_item", codex_message("assistant", [{"type": "output_text", "text": "POST"}])),
        ]
        _, output, report = self.stage(records, "codex", "claude")
        rows = read_jsonl(output / "generated/claude-session.jsonl")
        edge = next(row for row in rows if row.get("subtype") == "compact_boundary")
        summary = next(row for row in rows if row.get("isCompactSummary"))
        preserved_ids = edge["compactMetadata"]["preservedMessages"]["uuids"]
        preserved = [next(row for row in rows if row.get("uuid") == item) for item in preserved_ids]
        self.assertEqual([row["message"]["content"][0]["type"] for row in preserved], ["tool_use", "tool_result"])
        self.assertEqual(summary["message"]["content"][0]["text"], "SUMMARY")
        self.assertEqual(rows[-1]["leafUuid"], next(row for row in rows if row.get("message", {}).get("content") == [{"type": "text", "text": "POST"}])["uuid"])
        self.assertTrue(report["compacted"])

    def test_codex_rollback_removes_superseded_turn_without_resurrection(self):
        records = [
            *codex_header(),
            codex_line("response_item", codex_message("user", [{"type": "input_text", "text": "KEEP"}])),
            codex_line("response_item", codex_message("assistant", [{"type": "output_text", "text": "KEEP_REPLY"}])),
            codex_line("response_item", codex_message("user", [{"type": "input_text", "text": "ROLLED_BACK"}])),
            codex_line("response_item", codex_message("assistant", [{"type": "output_text", "text": "ROLLED_REPLY"}])),
            codex_line("event_msg", {"type": "thread_rolled_back", "num_turns": 1}),
            codex_line("response_item", codex_message("user", [{"type": "input_text", "text": "CURRENT"}])),
        ]
        _, output, _ = self.stage(records, "codex", "claude")
        generated = (output / "generated/claude-session.jsonl").read_text()
        self.assertIn("KEEP_REPLY", generated)
        self.assertIn("CURRENT", generated)
        self.assertNotIn("ROLLED_BACK", generated)
        self.assertNotIn("ROLLED_REPLY", generated)

    def test_native_tool_output_that_matches_converter_tag_remains_exact_text(self):
        output_text = json.dumps({
            conversations.CLAUDE_TOOL_RESULT_TAG: {"content": "COLLISION"},
            "other": "ORIGINAL_JSON_TEXT",
        }, separators=(",", ":"))
        records = [
            *codex_header(),
            codex_line("response_item", {
                "type": "function_call", "id": "fc", "call_id": "call", "name": "Tool", "arguments": "{}",
            }),
            codex_line("response_item", {
                "type": "function_call_output", "id": "fco", "call_id": "call", "output": output_text,
            }),
        ]
        _, output, _ = self.stage(records, "codex", "claude")
        rows = read_jsonl(output / "generated/claude-session.jsonl")
        result = next(
            block for row in rows for block in row.get("message", {}).get("content", [])
            if block.get("type") == "tool_result"
        )
        self.assertEqual(result["content"], output_text)

    def test_codex_reasoning_unknown_and_bad_tool_topology_reject(self):
        cases = [
            [*codex_header(), codex_line("response_item", {"type": "reasoning", "summary": []})],
            [*codex_header(), codex_line("response_item", {"type": "unknown_future", "opaque": 1})],
            [*codex_header(), codex_line("response_item", {"type": "function_call_output", "call_id": "orphan", "output": "x"})],
            [*codex_header(), codex_line("response_item", codex_message("user", [{
                "type": "input_image",
                "image_url": f"data:image/png;base64,{IMAGE_DATA}",
                "detail": "high",
            }]))],
        ]
        for index, records in enumerate(cases):
            with self.subTest(index=index):
                write_jsonl(self.source, records)
                with self.assertRaises(conversations.UnsupportedActiveSemanticsError):
                    conversations.stage_conversation(self.source, self.root / f"codex-bad-{index}", "codex", "claude")

    def test_both_provenance_round_trips_restore_exact_source(self):
        for index, (source, target, records, generated_name) in enumerate([
            ("claude", "codex", self.linear_claude(), "rollout.jsonl"),
            ("codex", "claude", self.linear_codex(), "claude-session.jsonl"),
        ]):
            with self.subTest(source=source):
                input_path = self.root / f"round-{index}.jsonl"
                original = write_jsonl(input_path, records)
                first = self.root / f"first-{index}"
                second = self.root / f"second-{index}"
                conversations.stage_conversation(input_path, first, source, target)
                report = conversations.stage_conversation(
                    first / "generated" / generated_name, second, target, source
                )
                restored_name = "rollout.jsonl" if source == "codex" else "claude-session.jsonl"
                self.assertEqual((second / "generated" / restored_name).read_bytes(), original)
                self.assertEqual(report["transformation"], "exact-original-restoration")

    def test_generated_history_drift_rejects_reverse(self):
        input_path = self.root / "drift-source.jsonl"
        write_jsonl(input_path, self.linear_claude())
        first = self.root / "drift-first"
        conversations.stage_conversation(input_path, first, "claude", "codex")
        generated = first / "generated/rollout.jsonl"
        generated.write_bytes(generated.read_bytes() + b"{}\n")
        with self.assertRaisesRegex(conversations.SourceDriftError, "changed after staging"):
            conversations.stage_conversation(generated, self.root / "drift-second", "codex", "claude")
        self.assertFalse((self.root / "drift-second").exists())

    def test_archive_corruption_and_mismatched_reverse_branch_reject(self):
        input_path = self.root / "provenance-source.jsonl"
        write_jsonl(input_path, self.linear_claude())
        corrupt = self.root / "corrupt-first"
        conversations.stage_conversation(input_path, corrupt, "claude", "codex")
        archive = corrupt / "archive/original.jsonl"
        archive.write_bytes(archive.read_bytes() + b"{}\n")
        with self.assertRaisesRegex(conversations.SourceDriftError, "archived original"):
            conversations.stage_conversation(
                corrupt / "generated/rollout.jsonl", self.root / "corrupt-second", "codex", "claude"
            )

        valid = self.root / "branch-first"
        conversations.stage_conversation(input_path, valid, "claude", "codex")
        with self.assertRaises(conversations.AmbiguousBranchError):
            conversations.stage_conversation(
                valid / "generated/rollout.jsonl", self.root / "branch-second", "codex", "claude",
                branch="NONEXISTENT",
            )

        manifest_corrupt = self.root / "manifest-first"
        conversations.stage_conversation(input_path, manifest_corrupt, "claude", "codex")
        (manifest_corrupt / "manifest.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(conversations.SourceDriftError, "manifest is corrupt"):
            conversations.stage_conversation(
                manifest_corrupt / "generated/rollout.jsonl",
                self.root / "manifest-second", "codex", "claude",
            )

    def test_duplicates_remain_in_archive_and_repeated_outputs_are_identical(self):
        records = self.linear_claude(duplicate=True)
        raw = write_jsonl(self.source, records)
        reports = []
        for name in ("repeat-a", "repeat-b"):
            reports.append(conversations.stage_conversation(self.source, self.root / name, "claude", "codex"))
        self.assertEqual((self.root / "repeat-a/archive/original.jsonl").read_bytes(), raw)
        self.assertEqual((self.root / "repeat-a/generated/rollout.jsonl").read_bytes(), (self.root / "repeat-b/generated/rollout.jsonl").read_bytes())
        self.assertEqual((self.root / "repeat-a/manifest.json").read_bytes(), (self.root / "repeat-b/manifest.json").read_bytes())
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[0]["source_record_count"], len(records))

    def test_destination_collision_and_cli(self):
        records = self.linear_claude()
        write_jsonl(self.source, records)
        output = self.root / "cli-output"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = conversations.main([
                "--from", "claude", "--to", "codex",
                "--input", str(self.source), "--output", str(output),
            ])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "staged")
        with self.assertRaises(FileExistsError):
            conversations.stage_conversation(self.source, output, "claude", "codex")


if __name__ == "__main__":
    unittest.main()

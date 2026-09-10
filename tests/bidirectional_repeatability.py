#!/usr/bin/env python3
"""Measure deterministic setup and conversation conversion in both directions.

The four cases run sequentially. Each case repeats at one fixed source/output
location, then repeats in fresh Python processes with distinct hash seeds.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from converter.claude_to_codex import Converter
from converter import codex_to_claude, conversations
from repeatability import fixture as forward_fixture


CASES = (
    "setup-forward",
    "setup-reverse",
    "conversation-claude-to-codex",
    "conversation-codex-to-claude",
)
TIME = "2026-01-01T00:00:00Z"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree(root: Path) -> tuple[str, list[list[object]]]:
    records: list[list[object]] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            records.append([relative, "symlink", os.readlink(path)])
        elif path.is_file():
            records.append([
                relative,
                "file",
                path.stat().st_mode & 0o777,
                sha256(path),
            ])
    encoded = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), records


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(
        (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        for row in records
    ))


def reverse_fixture(root: Path) -> None:
    files = {
        ".codex/config.toml": (
            "model = \"fixture-model\"\nmodel_reasoning_effort = \"low\"\n"
            "[mcp_servers.fixture]\ncommand = \"fixture-not-executed\"\n"
            "disabled_tools = [\"delete\"]\n"
            "[mcp_servers.fixture.tools.read]\napproval_mode = \"approve\"\n"
            "[mcp_servers.fixture.tools.write]\napproval_mode = \"prompt\"\n"
        ),
        "AGENTS.md": "Exact root instructions.\r\n",
        "src/AGENTS.md": "Exact nested instructions.\n",
        ".agents/skills/check/SKILL.md": "---\nname: check\ndescription: Check output\n---\nRead reference.md.\n",
        ".agents/skills/check/reference.md": "UTF-8: café — 日本語.\n",
        ".codex/agents/reviewer.toml": "description = \"Review changes\"\ndeveloper_instructions = \"Read only.\"\n",
        ".codex/hooks.json": json.dumps({"hooks": {"Stop": [{"command": "exit 99"}]}}),
    }
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    executable = root / ".codex/helper.sh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)


def claude_conversation_fixture(path: Path) -> None:
    records = [
        {
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/synthetic/work",
            "sessionId": "fixture-claude-session",
            "version": "2.1.263",
            "gitBranch": "main",
            "type": "user",
            "uuid": "u1",
            "timestamp": TIME,
            "message": {"role": "user", "content": [{"type": "text", "text": "Exact café 日本語"}]},
        },
        {
            "parentUuid": "u1",
            "isSidechain": False,
            "userType": "external",
            "cwd": "/synthetic/work",
            "sessionId": "fixture-claude-session",
            "version": "2.1.263",
            "gitBranch": "main",
            "type": "assistant",
            "uuid": "a1",
            "timestamp": TIME,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Exact response"}],
                "id": "msg_a1",
                "type": "message",
                "model": "fixture-model",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
        {"type": "last-prompt", "sessionId": "fixture-claude-session", "leafUuid": "a1", "lastPrompt": "Exact café 日本語"},
    ]
    write_jsonl(path, records)


def codex_conversation_fixture(path: Path) -> None:
    records = [
        {
            "timestamp": TIME,
            "type": "session_meta",
            "payload": {
                "id": "fixture-codex-session",
                "session_id": "fixture-codex-session",
                "timestamp": TIME,
                "cwd": "/synthetic/work",
                "model": "fixture-model",
            },
        },
        {
            "timestamp": TIME,
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "cwd": "/synthetic/work",
                "current_date": "2026-01-01",
                "timezone": "UTC",
                "model": "fixture-model",
                "approval_policy": "never",
                "sandbox_policy": {"type": "read-only"},
            },
        },
        {
            "timestamp": TIME,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Exact café 日本語"}],
            },
        },
        {
            "timestamp": TIME,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Exact response"}],
            },
        },
    ]
    write_jsonl(path, records)


def make_fixture(case: str, source: Path, global_settings: Path) -> None:
    if case == "setup-forward":
        source.mkdir(parents=True)
        forward_fixture(source)
        global_settings.write_text("{}")
    elif case == "setup-reverse":
        source.mkdir(parents=True)
        reverse_fixture(source)
    elif case == "conversation-claude-to-codex":
        claude_conversation_fixture(source)
    elif case == "conversation-codex-to-claude":
        codex_conversation_fixture(source)
    else:
        raise ValueError(f"Unknown case: {case}")


def run_case(case: str, source: Path, output: Path, global_settings: Path) -> tuple[object, str]:
    if output.exists() or output.is_symlink():
        if output.is_dir() and not output.is_symlink():
            shutil.rmtree(output)
        else:
            output.unlink()
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        if case == "setup-forward":
            options = SimpleNamespace(
                source=source,
                output=output,
                global_settings=global_settings,
                model_map=[],
                include_external_hooks=False,
                include_user_resources=False,
                strict=False,
                native_status=False,
            )
            signature: object = Converter(options).run()
        elif case == "setup-reverse":
            report = codex_to_claude.stage_codex_to_claude(source, output, strict=False)
            signature = [report["state"], report["exit_code"]]
        elif case == "conversation-claude-to-codex":
            report = conversations.stage_conversation(source, output, "claude", "codex")
            signature = [report["status"], report["transformation"]]
        elif case == "conversation-codex-to-claude":
            report = conversations.stage_conversation(source, output, "codex", "claude")
            signature = [report["status"], report["transformation"]]
        else:
            raise ValueError(f"Unknown case: {case}")
    return signature, tree(output)[0]


def assert_source_subset(source: Path, restored: Path, *, exact_modes: bool,
                         converted_controls_only: bool = False) -> int:
    count = 0
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if converted_controls_only:
            parts = relative.parts
            selected = (
                parts[:1] == (".claude",)
                or path.name in {"CLAUDE.md", "CLAUDE.local.md"}
                or relative == Path(".mcp.json")
            )
            if not selected:
                continue
        target = restored / relative
        if path.is_symlink():
            if not target.is_symlink() or os.readlink(target) != os.readlink(path):
                raise AssertionError(f"Round trip changed symlink: {relative}")
            count += 1
        elif path.is_file():
            if not target.is_file() or target.is_symlink():
                raise AssertionError(f"Round trip omitted file: {relative}")
            if path.read_bytes() != target.read_bytes():
                raise AssertionError(f"Round trip changed bytes: {relative}")
            source_mode = path.stat().st_mode & 0o777
            target_mode = target.stat().st_mode & 0o777
            if exact_modes and source_mode != target_mode:
                raise AssertionError(f"Round trip changed mode: {relative}")
            if source_mode & 0o111 != target_mode & 0o111:
                raise AssertionError(f"Round trip changed executable bits: {relative}")
            count += 1
    return count


def verify_roundtrip(case: str, source: Path, output: Path, root: Path, global_settings: Path) -> dict:
    reverse_output = root / "roundtrip"
    if case == "setup-forward":
        report = codex_to_claude.stage_codex_to_claude(output, reverse_output, strict=True)
        if report["exit_code"] != 0:
            raise AssertionError("Forward setup did not reverse without findings.")
        count = assert_source_subset(
            source, reverse_output, exact_modes=False, converted_controls_only=True
        )
    elif case == "setup-reverse":
        options = SimpleNamespace(
            source=output, output=reverse_output, global_settings=global_settings,
            model_map=[], include_external_hooks=False, include_user_resources=False,
            strict=False, native_status=False,
        )
        if Converter(options).run() != 0:
            raise AssertionError("Reverse setup did not restore through forward conversion.")
        count = assert_source_subset(source, reverse_output, exact_modes=True)
    elif case == "conversation-claude-to-codex":
        restored = conversations.stage_conversation(
            output / "generated/rollout.jsonl", reverse_output, "codex", "claude"
        )
        restored_path = reverse_output / "generated/claude-session.jsonl"
        if restored["transformation"] != "exact-original-restoration":
            raise AssertionError("Claude provenance did not select exact restoration.")
        if restored_path.read_bytes() != source.read_bytes():
            raise AssertionError("Claude conversation round trip changed source bytes.")
        count = restored["source_record_count"]
    else:
        restored = conversations.stage_conversation(
            output / "generated/claude-session.jsonl", reverse_output, "claude", "codex"
        )
        restored_path = reverse_output / "generated/rollout.jsonl"
        if restored["transformation"] != "exact-original-restoration":
            raise AssertionError("Codex provenance did not select exact restoration.")
        if restored_path.read_bytes() != source.read_bytes():
            raise AssertionError("Codex conversation round trip changed source bytes.")
        count = restored["source_record_count"]
    return {"passed": True, "verified_source_items": count}


def worker(args: argparse.Namespace) -> int:
    signature, digest = run_case(args.worker, args.source, args.output, args.global_settings)
    print(json.dumps({"signature": signature, "tree_sha256": digest}, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10000)
    parser.add_argument("--fresh-processes", type=int, default=128)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--worker", choices=CASES, help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--global-settings", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if not all((args.source, args.output, args.global_settings)):
            parser.error("worker paths are required")
        return worker(args)
    if not args.report:
        parser.error("--report is required")
    if args.runs < 1 or args.fresh_processes < 0:
        parser.error("counts must be non-negative and runs must be positive")

    started = time.monotonic()
    selected = args.case or list(CASES)
    report = {
        "schema_version": 1,
        "requested_runs_per_case": args.runs,
        "requested_fresh_processes_per_case": args.fresh_processes,
        "comparison": "All output relative paths, regular file bytes, modes, and symlink targets at one fixed source/output path; timestamps excluded.",
        "case_order": selected,
        "cases": {},
        "passed": False,
    }

    def persist() -> None:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["code_sha256"] = {
            str(path.relative_to(ROOT)): sha256(path)
            for path in sorted((ROOT / "converter").glob("*.py"))
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")

    try:
        for case in selected:
            case_started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix=f"cue-{case}-repeatability-") as directory:
                case_root = Path(directory).resolve()
                source = case_root / ("source.jsonl" if case.startswith("conversation-") else "source")
                output = case_root / "output"
                global_settings = case_root / "global-settings.json"
                global_settings.write_text("{}")
                make_fixture(case, source, global_settings)
                source_before = tree(source)[0] if source.is_dir() else sha256(source)
                baseline = None
                row = {
                    "identical_runs": 0,
                    "fresh_process_checks": 0,
                    "mismatches": [],
                    "roundtrip": None,
                    "passed": False,
                }
                report["cases"][case] = row
                persist()
                for index in range(args.runs):
                    actual = run_case(case, source, output, global_settings)
                    if baseline is None:
                        baseline = actual
                    if actual != baseline:
                        row["mismatches"].append({"run": index + 1, "actual": actual})
                        raise AssertionError(f"{case} changed output on run {index + 1}")
                    row["identical_runs"] += 1
                    if (index + 1) % 250 == 0:
                        print(json.dumps({"case": case, "identical_runs": index + 1}), flush=True)
                for seed in range(args.fresh_processes):
                    env = dict(os.environ)
                    env.update(PYTHONHASHSEED=str(seed), PYTHONDONTWRITEBYTECODE="1")
                    result = subprocess.run([
                        sys.executable, str(Path(__file__).resolve()),
                        "--worker", case,
                        "--source", str(source),
                        "--output", str(output),
                        "--global-settings", str(global_settings),
                    ], env=env, text=True, capture_output=True, timeout=60)
                    if result.returncode:
                        raise AssertionError(f"{case} fresh process {seed} failed: {result.stderr}")
                    child = json.loads(result.stdout)
                    actual = (child["signature"], child["tree_sha256"])
                    if actual != baseline:
                        row["mismatches"].append({"hash_seed": seed, "actual": actual})
                        raise AssertionError(f"{case} changed output with hash seed {seed}")
                    row["fresh_process_checks"] += 1
                source_after = tree(source)[0] if source.is_dir() else sha256(source)
                if source_after != source_before:
                    raise AssertionError(f"{case} mutated its source")
                if (source if source.is_dir() else source.parent).joinpath("HOOK_MUST_NOT_RUN").exists():
                    raise AssertionError(f"{case} executed a source hook")
                row["source_unchanged"] = True
                row["artifact_tree_sha256"] = baseline[1]
                row["output_file_count"] = len(tree(output)[1])
                row["roundtrip"] = verify_roundtrip(case, source, output, case_root, global_settings)
                row["elapsed_seconds"] = round(time.monotonic() - case_started, 3)
                row["passed"] = True
                persist()
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        persist()
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Deterministic, staged Claude/Codex conversation conversion.

The callable API is :func:`stage_conversation`. It never registers a session,
installs a file, executes a historical tool, or mutates its input.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Iterable
import uuid

try:
    from .version import VERSION
except ImportError:  # pragma: no cover - direct script execution
    from version import VERSION


SCHEMA_VERSION = 1
CLAUDE_DIALECT = "2.1.263"
CODEX_DIALECT = "0.153.4"
FIXED_TIME = "1970-01-01T00:00:00Z"
ID_NAMESPACE = uuid.UUID("5055ec7f-f0c7-5daa-a650-a79d3228fc65")
CLAUDE_TOOL_RESULT_TAG = "__claude_tool_result_v1__"
DATA_IMAGE_RE = re.compile(r"^data:([^;,]+);base64,(.*)$", re.DOTALL)


class ConversationConversionError(ValueError):
    """Base error for a conversion that cannot be staged safely."""


class UnsupportedActiveSemanticsError(ConversationConversionError):
    """Active history contains semantics the target cannot preserve."""

    def __init__(self, message: str, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.report = report


class AmbiguousBranchError(UnsupportedActiveSemanticsError):
    """The source does not identify one active branch."""


class SourceDriftError(ConversationConversionError):
    """A generated history or its archived source changed after staging."""


class ContextMismatchError(UnsupportedActiveSemanticsError):
    """The complete active history exceeds an explicitly supplied limit."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def jsonl_bytes(records: Iterable[dict[str, Any]]) -> bytes:
    return b"".join((canonical_json(record) + "\n").encode("utf-8") for record in records)


def stable_uuid(source_hash: str, *parts: object) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, ":".join([source_hash, *(str(part) for part in parts)])))


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise SourceDriftError("provenance path escapes its bundle") from error
    return candidate


def _read_jsonl(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    raw_file = path.read_bytes()
    records: list[dict[str, Any]] = []
    offset = 0
    for index, raw in enumerate(raw_file.splitlines(keepends=True)):
        body = raw.rstrip(b"\r\n")
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("record is not an object")
            error = None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            value = None
            error = str(exc)
        records.append({
            "index": index,
            "offset": offset,
            "length": len(raw),
            "sha256": sha256(raw),
            "raw": raw,
            "value": value,
            "parse_error": error,
            "disposition": "archive-only",
        })
        offset += len(raw)
    return raw_file, records


def _source_ref(record: dict[str, Any], *, block: int | None = None) -> dict[str, Any]:
    result = {
        "source_index": record["index"],
        "source_offset": record["offset"],
        "source_length": record["length"],
        "source_sha256": record["sha256"],
    }
    if block is not None:
        result["source_block_index"] = block
    return result


def _gap(code: str, message: str, record: dict[str, Any] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"code": code, "message": message, "active": True}
    if record is not None:
        value.update(_source_ref(record))
    return value


def _record_report(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for record in records:
        value = record["value"] or {}
        payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
        output.append({
            "index": record["index"],
            "offset": record["offset"],
            "length": record["length"],
            "sha256": record["sha256"],
            "type": value.get("type"),
            "source_id": value.get("uuid") or payload.get("id"),
            "parse_error": record["parse_error"],
            "disposition": record["disposition"],
        })
    return output


def _content_list(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content", [])
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list) or not all(isinstance(item, dict) for item in content):
        raise UnsupportedActiveSemanticsError("message content is not an ordered block list")
    return content


def _claude_image(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        raise UnsupportedActiveSemanticsError("active Claude image is not inline base64")
    media_type, data = source.get("media_type"), source.get("data")
    if not isinstance(media_type, str) or not isinstance(data, str):
        raise UnsupportedActiveSemanticsError("active Claude image has missing media type or data")
    try:
        base64.b64decode(data, validate=True)
    except Exception as error:
        raise UnsupportedActiveSemanticsError("active Claude image contains invalid base64") from error
    return {"type": "input_image", "image_url": f"data:{media_type};base64,{data}", "detail": "auto"}


def _claude_message_to_codex(
    record: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    row = record["value"]
    message = row.get("message")
    if not isinstance(message, dict):
        raise UnsupportedActiveSemanticsError("active Claude message is missing its message object")
    role = message.get("role")
    if role not in {"user", "assistant"}:
        raise UnsupportedActiveSemanticsError(f"active Claude role is unsupported: {role!r}")
    items: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    pending_blocks: list[int] = []

    def flush() -> None:
        if not pending:
            return
        items.append({"type": "message", "role": role, "content": list(pending)})
        provenance.append({
            "target_item": len(items) - 1,
            "source": [_source_ref(record, block=block) for block in pending_blocks],
            "mapping": "claude-message-blocks-to-codex-message",
        })
        pending.clear()
        pending_blocks.clear()

    for block_index, block in enumerate(_content_list(message)):
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise UnsupportedActiveSemanticsError("active Claude text block has no string text")
            pending.append({
                "type": "input_text" if role == "user" else "output_text",
                "text": text,
            })
            pending_blocks.append(block_index)
        elif block_type == "image":
            if role != "user":
                raise UnsupportedActiveSemanticsError("assistant image blocks are unsupported by Codex history")
            pending.append(_claude_image(block))
            pending_blocks.append(block_index)
        elif block_type == "tool_use":
            if role != "assistant":
                raise UnsupportedActiveSemanticsError("Claude tool_use block is not in an assistant message")
            flush()
            tool_id, name, tool_input = block.get("id"), block.get("name"), block.get("input")
            if not isinstance(tool_id, str) or not isinstance(name, str) or not isinstance(tool_input, dict):
                raise UnsupportedActiveSemanticsError("Claude tool_use lacks id, name, or object input")
            items.append({
                "type": "function_call",
                "id": tool_id,
                "name": name,
                "arguments": json.dumps(tool_input, ensure_ascii=False, separators=(",", ":")),
                "call_id": tool_id,
            })
            provenance.append({
                "target_item": len(items) - 1,
                "source": [_source_ref(record, block=block_index)],
                "mapping": "claude-tool-use-to-codex-function-call",
            })
        elif block_type == "tool_result":
            if role != "user":
                raise UnsupportedActiveSemanticsError("Claude tool_result block is not in a user message")
            flush()
            call_id = block.get("tool_use_id")
            if not isinstance(call_id, str):
                raise UnsupportedActiveSemanticsError("Claude tool_result lacks tool_use_id")
            envelope = {
                CLAUDE_TOOL_RESULT_TAG: {
                    "content": block.get("content"),
                    "is_error_present": "is_error" in block,
                    "is_error": block.get("is_error"),
                }
            }
            items.append({
                "type": "function_call_output",
                "id": "fco_" + stable_uuid(record["sha256"], "function-output", block_index),
                "call_id": call_id,
                "output": json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
            })
            provenance.append({
                "target_item": len(items) - 1,
                "source": [_source_ref(record, block=block_index)],
                "mapping": "claude-tool-result-to-tagged-codex-output",
            })
        elif block_type in {"thinking", "redacted_thinking"}:
            raise UnsupportedActiveSemanticsError("Claude reasoning signatures are not representable as Codex reasoning")
        else:
            raise UnsupportedActiveSemanticsError(f"unsupported active Claude content block: {block_type!r}")
    flush()
    return items, provenance


def _validate_tool_topology(items: list[dict[str, Any]]) -> None:
    calls: dict[str, int] = {}
    outputs: dict[str, int] = {}
    for index, item in enumerate(items):
        if item.get("type") == "function_call":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or call_id in calls:
                raise UnsupportedActiveSemanticsError("active tool calls have a missing or duplicate call_id")
            calls[call_id] = index
        elif item.get("type") == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or call_id in outputs:
                raise UnsupportedActiveSemanticsError("active tool outputs have a missing or duplicate call_id")
            outputs[call_id] = index
    orphaned = sorted(set(outputs) - set(calls))
    missing = sorted(set(calls) - set(outputs))
    out_of_order = sorted(call_id for call_id in calls.keys() & outputs.keys() if outputs[call_id] < calls[call_id])
    if orphaned or missing or out_of_order:
        raise UnsupportedActiveSemanticsError(
            f"active tool topology is incomplete: orphaned={orphaned}, missing={missing}, out_of_order={out_of_order}"
        )


def _claude_selection(
    records: list[dict[str, Any]], branch: str | None
) -> dict[str, Any]:
    parse_errors = [record for record in records if record["parse_error"]]
    if parse_errors:
        raise UnsupportedActiveSemanticsError(
            f"source has malformed JSONL at record {parse_errors[0]['index']}"
        )
    by_uuid: dict[str, dict[str, Any]] = {}
    duplicate_uuids: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        value = record["value"]
        source_id = value.get("uuid")
        if isinstance(source_id, str):
            if source_id in by_uuid:
                duplicate_uuids.setdefault(source_id, [by_uuid[source_id]]).append(record)
            else:
                by_uuid[source_id] = record
    for source_id, copies in duplicate_uuids.items():
        if len({copy["sha256"] for copy in copies}) != 1:
            raise AmbiguousBranchError(f"source UUID {source_id} has conflicting records")

    prompts = [
        record["value"].get("leafUuid")
        for record in records
        if record["value"].get("type") == "last-prompt" and isinstance(record["value"].get("leafUuid"), str)
    ]
    if branch is not None:
        leaf = branch
        branch_source = "explicit"
    elif prompts:
        if len(set(prompts)) != 1:
            raise AmbiguousBranchError("source contains conflicting last-prompt branch metadata")
        leaf = prompts[-1]
        branch_source = "last-prompt"
    else:
        parents = {
            record["value"].get("parentUuid")
            for record in records
            if isinstance(record["value"].get("parentUuid"), str)
        }
        leaves = [
            source_id for source_id, record in by_uuid.items()
            if source_id not in parents and record["value"].get("type") in {"user", "assistant"}
        ]
        if len(leaves) != 1:
            raise AmbiguousBranchError(f"source branch is ambiguous; candidate leaves={sorted(leaves)}")
        leaf = leaves[0]
        branch_source = "single-leaf"
    if leaf not in by_uuid:
        raise AmbiguousBranchError(f"selected branch leaf is absent: {leaf}")

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor: str | None = leaf
    while cursor is not None:
        if cursor in seen:
            raise AmbiguousBranchError(f"cycle in selected branch at {cursor}")
        record = by_uuid.get(cursor)
        if record is None:
            raise AmbiguousBranchError(f"selected branch has unknown parent {cursor}")
        seen.add(cursor)
        chain.append(record)
        parent = record["value"].get("parentUuid")
        if parent is not None and not isinstance(parent, str):
            raise AmbiguousBranchError(f"selected branch has invalid parent at {cursor}")
        cursor = parent
    chain.reverse()
    unknown_chain_records = [
        record for record in chain
        if record["value"].get("type") not in {"user", "assistant"}
        and not (
            record["value"].get("type") == "system"
            and record["value"].get("subtype") == "compact_boundary"
        )
    ]
    if unknown_chain_records:
        record = unknown_chain_records[0]
        raise UnsupportedActiveSemanticsError(
            f"unsupported record in selected Claude ancestry at record {record['index']}: "
            f"{record['value'].get('type')!r}"
        )
    chain_messages = [record for record in chain if record["value"].get("type") in {"user", "assistant"}]
    compact_summaries = [record for record in chain_messages if record["value"].get("isCompactSummary") is True]
    if not compact_summaries:
        for record in chain_messages:
            record["disposition"] = "active"
        return {
            "leaf": leaf,
            "branch_source": branch_source,
            "compacted": False,
            "precompact": [],
            "replacement": chain_messages,
            "postcompact": [],
            "active": chain_messages,
        }

    summary = compact_summaries[-1]
    summary_pos = chain.index(summary)
    boundary = None
    parent_id = summary["value"].get("parentUuid")
    if isinstance(parent_id, str):
        candidate = by_uuid.get(parent_id)
        if candidate and candidate["value"].get("type") == "system" and candidate["value"].get("subtype") == "compact_boundary":
            boundary = candidate
    if boundary is None:
        prior_boundaries = [
            record for record in records[: summary["index"]]
            if record["value"].get("type") == "system" and record["value"].get("subtype") == "compact_boundary"
        ]
        boundary = prior_boundaries[-1] if prior_boundaries else None
    if boundary is None:
        raise UnsupportedActiveSemanticsError("compact summary has no compact_boundary record")
    boundary["disposition"] = "compaction-metadata"
    metadata = boundary["value"].get("compactMetadata") or {}
    if not isinstance(metadata, dict):
        raise UnsupportedActiveSemanticsError("compactMetadata is not an object")

    preserved: list[dict[str, Any]] = []
    placement = "suffix"
    preserved_messages = metadata.get("preservedMessages")
    if isinstance(preserved_messages, dict):
        ids = preserved_messages.get("uuids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise UnsupportedActiveSemanticsError("preservedMessages.uuids is not an ordered string list")
        if len(ids) != len(set(ids)):
            raise UnsupportedActiveSemanticsError("preservedMessages.uuids contains duplicates")
        anchor = preserved_messages.get("anchorUuid")
        if anchor == boundary["value"].get("uuid"):
            placement = "prefix"
        elif anchor == summary["value"].get("uuid"):
            placement = "suffix"
        else:
            raise UnsupportedActiveSemanticsError("preservedMessages anchor is unsupported")
        for source_id in ids:
            record = by_uuid.get(source_id)
            if record is None or record["value"].get("type") not in {"user", "assistant"}:
                raise UnsupportedActiveSemanticsError(f"preserved message is absent: {source_id}")
            preserved.append(record)
    elif isinstance(metadata.get("preservedSegment"), dict):
        segment = metadata["preservedSegment"]
        head, tail, anchor = segment.get("headUuid"), segment.get("tailUuid"), segment.get("anchorUuid")
        if not all(isinstance(item, str) for item in (head, tail, anchor)):
            raise UnsupportedActiveSemanticsError("preservedSegment is incomplete")
        head_record, tail_record = by_uuid.get(head), by_uuid.get(tail)
        if head_record is None or tail_record is None:
            raise UnsupportedActiveSemanticsError("preservedSegment range is invalid")
        segment_reverse: list[dict[str, Any]] = []
        segment_seen: set[str] = set()
        segment_cursor: str | None = tail
        while segment_cursor is not None:
            if segment_cursor in segment_seen:
                raise UnsupportedActiveSemanticsError("preservedSegment contains a cycle")
            segment_seen.add(segment_cursor)
            record = by_uuid.get(segment_cursor)
            if record is None or record["value"].get("type") not in {"user", "assistant"}:
                raise UnsupportedActiveSemanticsError("preservedSegment ancestry is invalid")
            segment_reverse.append(record)
            if segment_cursor == head:
                break
            parent = record["value"].get("parentUuid")
            segment_cursor = parent if isinstance(parent, str) else None
        if not segment_reverse or segment_reverse[-1]["value"].get("uuid") != head:
            raise UnsupportedActiveSemanticsError("preservedSegment tail does not descend from head")
        preserved = list(reversed(segment_reverse))
        if anchor == boundary["value"].get("uuid"):
            placement = "prefix"
        elif anchor == summary["value"].get("uuid"):
            placement = "suffix"
        else:
            raise UnsupportedActiveSemanticsError("preservedSegment anchor is unsupported")

    after_summary = [
        record for record in chain[summary_pos + 1 :]
        if record["value"].get("type") in {"user", "assistant"}
    ]
    replacement = [*preserved, summary] if placement == "prefix" else [summary, *preserved]
    active = [*replacement, *after_summary]
    for record in active:
        record["disposition"] = "active"
    if boundary not in chain:
        raise UnsupportedActiveSemanticsError("compact_boundary is outside the selected Claude ancestry")
    selected_precompact = [
        record for record in chain[: chain.index(boundary)]
        if record["value"].get("type") in {"user", "assistant"}
    ]
    for record in selected_precompact:
        if record["disposition"] == "archive-only":
            record["disposition"] = "selected-precompaction"
    return {
        "leaf": leaf,
        "branch_source": branch_source,
        "compacted": True,
        "boundary": boundary,
        "precompact": selected_precompact,
        "replacement": replacement,
        "postcompact": after_summary,
        "active": active,
    }


def _map_claude_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for record in records:
        mapped, mapped_provenance = _claude_message_to_codex(record)
        start = len(items)
        items.extend(mapped)
        for entry in mapped_provenance:
            entry["target_item"] += start
            provenance.append(entry)
    return items, provenance


def _source_timestamp(records: list[dict[str, Any]]) -> str:
    for record in records:
        value = record["value"] or {}
        payload = value.get("payload")
        timestamp = value.get("timestamp") or (payload.get("timestamp") if isinstance(payload, dict) else None)
        if isinstance(timestamp, str) and timestamp:
            return timestamp
    return FIXED_TIME


def _source_cwd(records: list[dict[str, Any]]) -> str:
    for record in records:
        value = record["value"] or {}
        payload = value.get("payload")
        cwd = value.get("cwd") or (payload.get("cwd") if isinstance(payload, dict) else None)
        if isinstance(cwd, str) and cwd:
            return cwd
    return "."


def _codex_line(timestamp: str, record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": timestamp, "type": record_type, "payload": payload}


def _claude_to_codex(
    records: list[dict[str, Any]], source_hash: str, branch: str | None
) -> tuple[bytes, dict[str, Any]]:
    selection = _claude_selection(records, branch)
    timestamp, cwd = _source_timestamp(records), _source_cwd(records)
    session_id = stable_uuid(source_hash, "claude-to-codex-session")
    turn_id = stable_uuid(source_hash, "import-turn")
    rollout: list[dict[str, Any]] = [
        _codex_line(timestamp, "session_meta", {
            "id": session_id,
            "session_id": session_id,
            "timestamp": timestamp,
            "cwd": cwd,
            "originator": "claude-codex-converter",
            "cli_version": f"codex-cli {CODEX_DIALECT} converted",
            "source": "converted",
            "model_provider": "converted-history",
            "model": "converted-history",
        }),
        _codex_line(timestamp, "turn_context", {
            "turn_id": turn_id,
            "cwd": cwd,
            "current_date": timestamp[:10] if len(timestamp) >= 10 else "1970-01-01",
            "timezone": "UTC",
            "model": "converted-history",
            "approval_policy": "never",
            "sandbox_policy": {"type": "read-only"},
            "summary": "auto",
        }),
    ]
    provenance: list[dict[str, Any]] = []
    if selection["compacted"]:
        precompact, pre_provenance = _map_claude_records(selection["precompact"])
        for item in precompact:
            rollout.append(_codex_line(timestamp, "response_item", item))
        for entry in pre_provenance:
            entry["target_record"] = 2 + entry.pop("target_item")
            provenance.append(entry)
        replacement, replacement_provenance = _map_claude_records(selection["replacement"])
        _validate_tool_topology(replacement)
        boundary = selection["boundary"]
        rollout.append(_codex_line(timestamp, "compacted", {
            "message": str(boundary["value"].get("content", "Conversation compacted")),
            "replacement_history": replacement,
            "guardian_history": None,
            "mcp_resource_origins": None,
            "window_number": 1,
            "first_window_id": None,
            "previous_window_id": None,
            "window_id": None,
            "compaction_response_id": None,
            "latest_token_usage_record": None,
        }))
        compacted_index = len(rollout) - 1
        provenance.append({
            "target_record": compacted_index,
            "source": [_source_ref(boundary)],
            "mapping": "claude-compaction-to-codex-replacement-history",
            "replacement_items": replacement_provenance,
        })
        postcompact, post_provenance = _map_claude_records(selection["postcompact"])
        active_items = [*replacement, *postcompact]
        _validate_tool_topology(active_items)
        for item in postcompact:
            rollout.append(_codex_line(timestamp, "response_item", item))
        for entry in post_provenance:
            entry["target_record"] = compacted_index + 1 + entry.pop("target_item")
            provenance.append(entry)
    else:
        active_items, active_provenance = _map_claude_records(selection["active"])
        _validate_tool_topology(active_items)
        for item in active_items:
            rollout.append(_codex_line(timestamp, "response_item", item))
        for entry in active_provenance:
            entry["target_record"] = 2 + entry.pop("target_item")
            provenance.append(entry)
    return jsonl_bytes(rollout), {
        "selected_branch": selection["leaf"],
        "branch_source": selection["branch_source"],
        "compacted": selection["compacted"],
        "active_source_indices": [record["index"] for record in selection["active"]],
        "active_item_count": len(active_items),
        "provenance": provenance,
    }


def _codex_selection(records: list[dict[str, Any]], branch: str | None) -> dict[str, Any]:
    parse_errors = [record for record in records if record["parse_error"]]
    if parse_errors:
        raise UnsupportedActiveSemanticsError(
            f"source has malformed JSONL at record {parse_errors[0]['index']}"
        )
    session_ids: set[str] = set()
    for record in records:
        value = record["value"]
        if value.get("type") != "session_meta":
            continue
        payload = value.get("payload")
        session_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise UnsupportedActiveSemanticsError("Codex session_meta lacks a string id")
        session_ids.add(session_id)
    if len(session_ids) != 1:
        raise AmbiguousBranchError(f"Codex rollout must identify one session; ids={sorted(session_ids)}")
    session_id = next(iter(session_ids))
    if branch is not None and branch != session_id:
        raise AmbiguousBranchError("Codex rollout is one branch; explicit branch does not match its session id")

    compacted = [record for record in records if record["value"].get("type") == "compacted"]
    boundary = compacted[-1] if compacted else None
    boundary_index = boundary["index"] if boundary else -1
    precompact = [
        {"item": record["value"]["payload"], "record": record, "nested_index": None}
        for record in records[:boundary_index]
        if record["value"].get("type") == "response_item"
    ] if boundary else []
    replacement: list[dict[str, Any]] = []
    if boundary:
        boundary_payload = boundary["value"].get("payload")
        history = boundary_payload.get("replacement_history") if isinstance(boundary_payload, dict) else None
        if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
            raise UnsupportedActiveSemanticsError("Codex compaction replacement_history is invalid")
        boundary["disposition"] = "compaction-metadata"
        replacement = [
            {"item": item, "record": boundary, "nested_index": index}
            for index, item in enumerate(history)
        ]
    active: list[dict[str, Any]] = list(replacement)

    def drop_last_user_turns(num_turns: int) -> None:
        nonlocal active
        if num_turns == 0:
            return
        boundaries = [
            index for index, wrapped in enumerate(active)
            if wrapped["item"].get("type") == "message"
            and wrapped["item"].get("role") == "user"
        ]
        if num_turns >= len(boundaries):
            active = []
        else:
            active = active[: boundaries[-num_turns]]

    for record in records[boundary_index + 1 :]:
        record_type = record["value"].get("type")
        if record_type == "response_item":
            item = record["value"].get("payload")
            if not isinstance(item, dict):
                raise UnsupportedActiveSemanticsError("active Codex response_item payload is not an object")
            active.append({"item": item, "record": record, "nested_index": None})
        elif record_type == "event_msg":
            payload = record["value"].get("payload")
            if not isinstance(payload, dict):
                raise UnsupportedActiveSemanticsError("active Codex event_msg payload is not an object")
            if payload.get("type") == "thread_rolled_back":
                num_turns = payload.get("num_turns")
                if isinstance(num_turns, bool) or not isinstance(num_turns, int) or num_turns < 0:
                    raise UnsupportedActiveSemanticsError("thread_rolled_back num_turns is invalid")
                drop_last_user_turns(num_turns)
                record["disposition"] = "active-state-metadata"
        elif record_type not in {"session_meta", "turn_context", "compacted"}:
            raise UnsupportedActiveSemanticsError(f"unknown active Codex rollout record: {record_type!r}")

    compaction_survives = bool(boundary and active[: len(replacement)] == replacement)
    if not compaction_survives:
        boundary = None
        replacement = []
        precompact = []
    for wrapped in active:
        wrapped["record"]["disposition"] = "active"
    return {
        "selected_branch": session_id,
        "branch_source": "rollout-session",
        "compacted": boundary is not None,
        "boundary": boundary,
        "precompact": precompact,
        "replacement": replacement,
        "postcompact": active[len(replacement):] if boundary else [],
        "active": active,
    }


def _codex_message_to_claude(item: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    role = item.get("role")
    if role not in {"user", "assistant"}:
        raise UnsupportedActiveSemanticsError(f"active Codex message role is unsupported: {role!r}")
    content = item.get("content")
    if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
        raise UnsupportedActiveSemanticsError("active Codex message has invalid content")
    blocks: list[dict[str, Any]] = []
    for block in content:
        block_type = block.get("type")
        if block_type in {"input_text", "output_text"}:
            text = block.get("text")
            if not isinstance(text, str):
                raise UnsupportedActiveSemanticsError("active Codex text block has no string text")
            blocks.append({"type": "text", "text": text})
        elif block_type == "input_image":
            image_url = block.get("image_url")
            detail = block.get("detail", "auto")
            if detail != "auto":
                raise UnsupportedActiveSemanticsError(
                    f"active Codex image detail is unsupported by Claude: {detail!r}"
                )
            match = DATA_IMAGE_RE.match(image_url) if isinstance(image_url, str) else None
            if not match:
                raise UnsupportedActiveSemanticsError("active Codex image is not an inline base64 data URL")
            media_type, data = match.groups()
            try:
                base64.b64decode(data, validate=True)
            except Exception as error:
                raise UnsupportedActiveSemanticsError("active Codex image contains invalid base64") from error
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            })
        else:
            raise UnsupportedActiveSemanticsError(f"unsupported active Codex message block: {block_type!r}")
    return role, blocks


def _codex_item_to_claude(item: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    item_type = item.get("type")
    if item_type == "message":
        return _codex_message_to_claude(item)
    if item_type == "function_call":
        name, call_id, arguments = item.get("name"), item.get("call_id"), item.get("arguments")
        if not isinstance(name, str) or not isinstance(call_id, str) or not isinstance(arguments, str):
            raise UnsupportedActiveSemanticsError("Codex function_call lacks name, call_id, or arguments")
        try:
            tool_input = json.loads(arguments)
        except json.JSONDecodeError as error:
            raise UnsupportedActiveSemanticsError("Codex function_call arguments are not JSON") from error
        if not isinstance(tool_input, dict):
            raise UnsupportedActiveSemanticsError("Codex function_call arguments are not an object")
        return "assistant", [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]
    if item_type == "function_call_output":
        call_id, output = item.get("call_id"), item.get("output")
        if not isinstance(call_id, str) or not isinstance(output, str):
            raise UnsupportedActiveSemanticsError("Codex function_call_output lacks call_id or string output")
        return "user", [{"type": "tool_result", "tool_use_id": call_id, "content": output}]
    if item_type == "reasoning":
        raise UnsupportedActiveSemanticsError("Codex reasoning is not representable as Claude signed reasoning")
    raise UnsupportedActiveSemanticsError(f"unsupported active Codex response item: {item_type!r}")


def _claude_row(
    source_hash: str,
    source_key: str,
    session_id: str,
    cwd: str,
    timestamp: str,
    parent_uuid: str | None,
    role: str,
    blocks: list[dict[str, Any]],
    *,
    compact_summary: bool = False,
) -> dict[str, Any]:
    row_uuid = stable_uuid(source_hash, "claude-row", source_key)
    row: dict[str, Any] = {
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "userType": "external",
        "cwd": cwd,
        "sessionId": session_id,
        "version": CLAUDE_DIALECT,
        "gitBranch": "HEAD",
        "type": role,
        "uuid": row_uuid,
        "timestamp": timestamp,
        "message": {"role": role, "content": blocks},
    }
    if role == "assistant":
        row["message"].update({
            "id": "msg_" + row_uuid.replace("-", "")[:24],
            "type": "message",
            "model": "converted-history",
            "stop_reason": "tool_use" if any(block.get("type") == "tool_use" for block in blocks) else "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })
    if compact_summary:
        row["isCompactSummary"] = True
    return row


def _map_codex_items(
    wrapped_items: list[dict[str, Any]],
    source_hash: str,
    session_id: str,
    cwd: str,
    timestamp: str,
    parent_uuid: str | None,
    key_prefix: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    rows: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    parent = parent_uuid
    for item_index, wrapped in enumerate(wrapped_items):
        item, record = wrapped["item"], wrapped["record"]
        if not isinstance(item, dict):
            raise UnsupportedActiveSemanticsError("active Codex response item is not an object")
        role, blocks = _codex_item_to_claude(item)
        row = _claude_row(
            source_hash, f"{key_prefix}-{item_index}", session_id, cwd, timestamp, parent, role, blocks
        )
        rows.append(row)
        parent = row["uuid"]
        source = _source_ref(record)
        if wrapped.get("nested_index") is not None:
            source["source_nested_item"] = wrapped["nested_index"]
        provenance.append({
            "target_record": len(rows) - 1,
            "source": [source],
            "mapping": f"codex-{item.get('type')}-to-claude-message",
        })
    return rows, provenance, parent


def _codex_to_claude(
    records: list[dict[str, Any]], source_hash: str, branch: str | None
) -> tuple[bytes, dict[str, Any]]:
    selection = _codex_selection(records, branch)
    active_items = [wrapped["item"] for wrapped in selection["active"]]
    _validate_tool_topology(active_items)
    timestamp, cwd = _source_timestamp(records), _source_cwd(records)
    session_id = stable_uuid(source_hash, "codex-to-claude-session")
    output: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    parent: str | None = None

    if selection["compacted"]:
        pre_rows, pre_provenance, parent = _map_codex_items(
            selection["precompact"], source_hash, session_id, cwd, timestamp, parent, "precompact"
        )
        output.extend(pre_rows)
        provenance.extend(pre_provenance)
        replacement = selection["replacement"]
        if not replacement or replacement[0]["item"].get("type") != "message" or replacement[0]["item"].get("role") != "user":
            raise UnsupportedActiveSemanticsError("Codex compaction must begin with a user summary for Claude continuation")
        summary_role, summary_blocks = _codex_item_to_claude(replacement[0]["item"])
        preserved_rows, preserved_provenance, _ = _map_codex_items(
            replacement[1:], source_hash, session_id, cwd, timestamp, parent, "preserved"
        )
        output.extend(preserved_rows)
        provenance.extend({**entry, "target_record": entry["target_record"] + len(pre_rows)} for entry in preserved_provenance)
        boundary_uuid = stable_uuid(source_hash, "claude-boundary")
        boundary = {
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": cwd,
            "sessionId": session_id,
            "version": CLAUDE_DIALECT,
            "gitBranch": "HEAD",
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": boundary_uuid,
            "timestamp": timestamp,
            "content": selection["boundary"]["value"].get("payload", {}).get("message", "Conversation compacted"),
            "compactMetadata": {"trigger": "import", "preTokens": 0},
        }
        if preserved_rows:
            boundary["compactMetadata"]["preservedMessages"] = {
                "anchorUuid": stable_uuid(source_hash, "claude-summary"),
                "uuids": [row["uuid"] for row in preserved_rows],
            }
        output.append(boundary)
        boundary_target = len(output) - 1
        provenance.append({
            "target_record": boundary_target,
            "source": [_source_ref(selection["boundary"])],
            "mapping": "codex-compaction-to-claude-boundary",
        })
        summary = _claude_row(
            source_hash, "summary", session_id, cwd, timestamp, boundary_uuid,
            summary_role, summary_blocks, compact_summary=True,
        )
        # The fixed summary UUID is referenced by preservedMessages.
        summary["uuid"] = stable_uuid(source_hash, "claude-summary")
        if summary_role == "assistant":
            summary["message"]["id"] = "msg_" + summary["uuid"].replace("-", "")[:24]
        output.append(summary)
        provenance.append({
            "target_record": len(output) - 1,
            "source": [{**_source_ref(replacement[0]["record"]), "source_nested_item": 0}],
            "mapping": "codex-replacement-head-to-claude-summary",
        })
        parent = summary["uuid"]
        post_rows, post_provenance, parent = _map_codex_items(
            selection["postcompact"], source_hash, session_id, cwd, timestamp, parent, "postcompact"
        )
        base = len(output)
        output.extend(post_rows)
        provenance.extend({**entry, "target_record": entry["target_record"] + base} for entry in post_provenance)
    else:
        rows, provenance, parent = _map_codex_items(
            selection["active"], source_hash, session_id, cwd, timestamp, parent, "active"
        )
        output.extend(rows)

    if parent is None:
        raise UnsupportedActiveSemanticsError("Codex rollout has no active conversation records")
    latest_user_text = ""
    for row in reversed(output):
        if row.get("type") == "user":
            for block in reversed(row.get("message", {}).get("content", [])):
                if block.get("type") == "text":
                    latest_user_text = block.get("text", "")
                    break
        if latest_user_text:
            break
    output.append({
        "type": "last-prompt",
        "sessionId": session_id,
        "leafUuid": parent,
        "lastPrompt": latest_user_text,
    })
    return jsonl_bytes(output), {
        "selected_branch": selection["selected_branch"],
        "branch_source": selection["branch_source"],
        "compacted": selection["compacted"],
        "active_source_indices": sorted({wrapped["record"]["index"] for wrapped in selection["active"]}),
        "active_item_count": len(active_items),
        "provenance": provenance,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _publish_bundle(
    output_dir: Path,
    source_bytes: bytes,
    generated_bytes: bytes | None,
    generated_name: str | None,
    manifest: dict[str, Any],
    report: dict[str, Any],
) -> None:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.stage-", dir=output_dir.parent))
    try:
        (stage / "archive").mkdir()
        (stage / "archive" / "original.jsonl").write_bytes(source_bytes)
        if generated_bytes is not None and generated_name is not None:
            (stage / "generated").mkdir()
            (stage / "generated" / generated_name).write_bytes(generated_bytes)
        _write_json(stage / "manifest.json", manifest)
        _write_json(stage / "preservation-report.json", report)
        stage.rename(output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _prior_bundle(input_path: Path, source: str, target: str) -> tuple[Path, dict[str, Any]] | None:
    if input_path.parent.name != "generated":
        return None
    root = input_path.parent.parent
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceDriftError("bundle manifest is corrupt") from error
    if not isinstance(manifest, dict):
        raise SourceDriftError("bundle manifest is not an object")
    if manifest.get("source_format") != target or manifest.get("target_format") != source:
        return None
    generated = manifest.get("generated") or {}
    expected_path = _safe_child(root, generated.get("path", ""))
    if input_path.resolve() != expected_path:
        raise SourceDriftError("input does not match the generated path in provenance")
    if sha256(input_path.read_bytes()) != generated.get("sha256"):
        raise SourceDriftError("generated conversation changed after staging")
    archive = manifest.get("archive") or {}
    archive_path = _safe_child(root, archive.get("path", ""))
    if not archive_path.is_file() or sha256(archive_path.read_bytes()) != archive.get("sha256"):
        raise SourceDriftError("archived original is missing or corrupt")
    return archive_path, manifest


def _rejection_report(
    source: str,
    target: str,
    source_bytes: bytes,
    records: list[dict[str, Any]],
    error: ConversationConversionError,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "converter_version": VERSION,
        "status": "rejected",
        "source_format": source,
        "target_format": target,
        "strict_continuation_ready": False,
        "archive_exact": True,
        "source_sha256": sha256(source_bytes),
        "source_record_count": len(records),
        "gaps": [_gap(type(error).__name__, str(error))],
        "records": _record_report(records),
    }


def stage_conversation(
    input_path: str | Path,
    output_dir: str | Path,
    source: str,
    target: str,
    *,
    branch: str | None = None,
    strict: bool = True,
    max_active_bytes: int | None = None,
) -> dict[str, Any]:
    """Stage one conversation conversion and return its preservation report.

    The output directory must not exist. Strict rejections still stage the exact
    source archive and a rejection report, but no target continuation file.
    """
    source, target = source.lower(), target.lower()
    if source not in {"claude", "codex"} or target not in {"claude", "codex"}:
        raise ConversationConversionError("source and target must be claude or codex")
    if source == target:
        raise ConversationConversionError("source and target formats must differ")
    input_path, output_dir = Path(input_path).resolve(), Path(output_dir).resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if max_active_bytes is not None and max_active_bytes < 0:
        raise ConversationConversionError("max_active_bytes must be non-negative")

    prior = _prior_bundle(input_path, source, target)
    source_bytes, records = _read_jsonl(input_path)
    source_hash = sha256(source_bytes)
    generated_name = "rollout.jsonl" if target == "codex" else "claude-session.jsonl"
    transformation = "mapped"
    parent_manifest_hash = None
    try:
        if prior:
            archive_path, prior_manifest = prior
            if branch is not None:
                if source == "claude":
                    _claude_selection(records, branch)
                else:
                    _codex_selection(records, branch)
            generated_bytes = archive_path.read_bytes()
            details = {
                "selected_branch": prior_manifest.get("selected_branch"),
                "branch_source": "verified-provenance",
                "compacted": prior_manifest.get("compacted", False),
                "active_source_indices": [],
                "active_item_count": prior_manifest.get("active_item_count"),
                "provenance": [{
                    "target": "generated/" + generated_name,
                    "mapping": "exact-original-restoration",
                    "source_archive_sha256": sha256(generated_bytes),
                }],
            }
            transformation = "exact-original-restoration"
            parent_manifest_hash = sha256((input_path.parent.parent / "manifest.json").read_bytes())
        elif source == "claude":
            generated_bytes, details = _claude_to_codex(records, source_hash, branch)
        else:
            generated_bytes, details = _codex_to_claude(records, source_hash, branch)
        if max_active_bytes is not None and len(generated_bytes) > max_active_bytes:
            raise ContextMismatchError(
                f"complete staged continuation needs {len(generated_bytes)} bytes; target allows {max_active_bytes}"
            )
    except UnsupportedActiveSemanticsError as error:
        report = _rejection_report(source, target, source_bytes, records, error)
        if not strict:
            report["status"] = "archived-only"
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "converter_version": VERSION,
            "status": report["status"],
            "source_format": source,
            "target_format": target,
            "archive": {"path": "archive/original.jsonl", "sha256": source_hash, "bytes": len(source_bytes)},
            "generated": None,
            "gaps": report["gaps"],
        }
        _publish_bundle(output_dir, source_bytes, None, None, manifest, report)
        error.report = report
        if strict:
            raise
        return report

    report = {
        "schema_version": SCHEMA_VERSION,
        "converter_version": VERSION,
        "status": "staged",
        "source_format": source,
        "target_format": target,
        "strict_continuation_ready": True,
        "archive_exact": True,
        "source_sha256": source_hash,
        "generated_sha256": sha256(generated_bytes),
        "source_record_count": len(records),
        "generated_bytes": len(generated_bytes),
        "selected_branch": details["selected_branch"],
        "branch_source": details["branch_source"],
        "compacted": details["compacted"],
        "active_source_indices": details["active_source_indices"],
        "active_item_count": details["active_item_count"],
        "transformation": transformation,
        "gaps": [],
        "records": _record_report(records),
        "provenance": details["provenance"],
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "converter_version": VERSION,
        "status": "staged",
        "source_format": source,
        "target_format": target,
        "archive": {"path": "archive/original.jsonl", "sha256": source_hash, "bytes": len(source_bytes)},
        "generated": {
            "path": "generated/" + generated_name,
            "sha256": sha256(generated_bytes),
            "bytes": len(generated_bytes),
            "dialect": CODEX_DIALECT if target == "codex" else CLAUDE_DIALECT,
        },
        "selected_branch": details["selected_branch"],
        "compacted": details["compacted"],
        "active_item_count": details["active_item_count"],
        "transformation": transformation,
        "parent_manifest_sha256": parent_manifest_hash,
        "gaps": [],
    }
    _publish_bundle(output_dir, source_bytes, generated_bytes, generated_name, manifest, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", choices=("claude", "codex"), required=True)
    parser.add_argument("--to", dest="target", choices=("claude", "codex"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--branch", help="explicit Claude leaf UUID or matching Codex session id")
    parser.add_argument("--non-strict", action="store_true", help="archive unsupported active history without emitting a continuation")
    parser.add_argument("--max-active-bytes", type=int)
    args = parser.parse_args(argv)
    try:
        report = stage_conversation(
            args.input,
            args.output,
            args.source,
            args.target,
            branch=args.branch,
            strict=not args.non_strict,
            max_active_bytes=args.max_active_bytes,
        )
    except ConversationConversionError as error:
        print(json.dumps({"status": "rejected", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

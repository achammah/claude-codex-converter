# Bidirectional converter contract

Authority: owner requests both setup and conversation conversion in both directions on 2026-09-06.
Existing native installation work continues. Preserve existing forward behavior and original installations.

## Public operations

- Keep `convert` as the existing Claude-to-Codex setup operation.
- Add `reverse` for standalone Codex-to-Claude setup staging.
- Add `conversation --from claude|codex --to claude|codex --input FILE --output DIR`.
- Both new operations stage into a new output directory. They never overwrite source or install implicitly.
- Expose inspectable preservation reports. Unsupported active semantics fail strict continuation, with original bytes retained.
- Determinism fixes input bytes, source/target types, converter version and options. Do not use current time or random IDs in conversion output.

## Setup unit

Own `converter/codex_to_claude.py`, `tests/test_reverse_setup.py`, and `verification/reverse-setup.json`.
Provide `main()` for public CLI dispatch and a documented callable API.
Read the existing forward converter, instruction resolver and host adapter before implementing.
Handle independent native Codex input and generated Codex setups carrying original-source provenance.
Inventory all files and preserve unknown data; do not treat archival copying as runtime equivalence.
Map AGENTS instructions, nested overrides, skills, agents, hooks, MCP, model settings and permissions where semantics are proven.
Do not translate approvals into broader privileges, execute hooks, copy authentication secrets into reports, or invent a native equivalent.
Original-source restoration must verify hashes and distinguish source edits, generated edits and conflicts.
Test round trips, independent Codex inputs, permissions, malformed config, unknown fields, symlinks and destination collisions.

## Conversation unit

Own `converter/conversations.py`, `tests/test_conversations.py`, and `verification/conversations.json`.
Provide `main()` for public CLI dispatch and a documented callable API.
Consume CONVERSATION-PLAN-v2.md, the original preservation matrix and the source/native experiment evidence.
Preserve whole original files, ordered records, duplicates and opaque content in a deterministic bundle.
Emit native Claude JSONL and native Codex rollout JSONL for supported message/tool/image/compaction histories.
Choose branches from explicit source metadata or an explicit argument; ambiguous histories fail rather than concatenate siblings.
Keep full payloads and ordered block relationships. No silent tool truncation, invented results, replay or new compaction.
Use provenance for exact reverse restoration and detect changed generated histories; do not silently return an obsolete original snapshot.
Mark missing assets, unknown active types and unrepresentable reasoning as explicit gaps; reject strict continuation when applicable.
Registration and active file installation remain separate from staging. Generated files must be exercised against actual native loaders.
Test both directions, both round trips, corrections, retained compaction segments, malformed topology, corruption and repeated conversion.

## Integration and ownership

Main owns `converter/cli.py`, README, release packaging integration and final verification.
Installer worker retains native_runtime.py, installation and its existing tests.
Native capture worker retains native experiment files and will verify generated conversations after the core lands.
No worker edits another unit. Shared checkout lacks isolated tracked history; each unit has disjoint new files.
Do not revert other workers or read/print credentials. Every new finding is returned with evidence for the main findings log.

| Unit | Type | Inputs | Assertions | Output | Reviewer | Model |
|---|---|---|---|---|---|---|
| Reverse setup | Plumbing | Existing converter and this contract | Both-direction setup fixtures and drift rejection | codex_to_claude.py and dedicated tests | Cue | Inherited worker model |
| Conversation pair | Plumbing | Plans and measured source/target evidence | Ordered payload preservation and strict unsupported failure | conversations.py and dedicated tests | Cue | Inherited worker model |
| Native validation | Verification | Generated files after core lands | Actual source/target outbound request equality | Existing experiment directories | Cue | Existing capture workers |
| CLI and packaging | Plumbing | New module APIs | Public standalone invocation and deterministic distribution | cli.py, README and release artifacts | Cue | Main |

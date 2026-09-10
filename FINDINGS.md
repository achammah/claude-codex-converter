# Converter findings and regression rules

This is the shareable mapping log. Each conversion also produces source-specific
findings and file hashes. Personal estate inventories and live chat plans are not
part of this distribution.

| Finding | General converter rule | Evidence |
|---|---|---|
| Full instructions can exceed Codex's default cap | Measure UTF-8 bytes and retain full instructions; raise the configured cap | Full-estate conversion and native prompt rendering |
| Local settings and global dependencies alter effective behavior | Preserve layer provenance and explicit resource boundaries | Settings merger and user-resource inventory |
| Failed shell events differ across hosts | Dispatch source failure hooks from actual nonzero completion | Runtime event bridge |
| Patch tools carry different payloads | Visit every affected file; never assume one patch equals one Edit | Protocol adapter |
| Unsupported output fields can invalidate an entire native hook response | Normalize fields per event; preserve deny/veto semantics | Permission regression cases |
| A broader allow can erase a wildcard, compound, or newline deny | Evaluate restrictions before grants and after input rewrites | Independent regressions |
| An ask plus rewritten input must not become allow | Reject unsupported approval rewrites | Independent regressions |
| Source exit 2 and continue:false were initially dropped | Fold vetoes before any permission grant | Independent regressions |
| Shell prefixes are broader than exact commands | Do not emit broad prefix grants for exact source approvals | Exact permission matcher |
| Skill names can differ from directory names | Move/copy the complete resource tree, including relative references | Renamed-skill regression |
| Skill hook metadata is executable configuration | Retain it and provide explicit scoped activation | Scoped-hook regression |
| Source paths can collide with generated metadata or each other | Detect collisions; archive originals outside the translated namespace | Archive and translated-path regressions |
| Symlinked parent directories bypass leaf-only checks | Inspect ancestors and refuse unexpected traversal | Symlink regressions |
| Inline and rule-level instruction imports were initially omitted | Expand recursively and record unresolved references/cycles | Import regressions |
| Some existing plain descriptions are invalid YAML | Limit recovery to unambiguous single-line description scalars; record repair | Real-estate parsing |
| Hashes recorded before rewriting become stale | Hash final generated and relocated installation bytes | Manifest regressions |
| Installer output can escape through changed roots or modified plans | Recheck canonical roots and every relative path at apply time | Installer adversarial tests |
| Receipt paths can overwrite installed files | Keep recovery artifacts separate from destination content | Receipt collision regression |
| Native import acceptance is not completion | Match import IDs and validate the completed result shape | Fake app-server regressions |
| Status can accidentally erase the original import receipt | Preserve correlation and append reconciliation evidence | Native status regression |
| Native detection may resolve a parent Git root | Compare detected scope with explicitly inventoried source scope | Live read-only native detection |
| A patched `codex` alone breaks code-mode tools | Install the matching code-mode host and pinned ripgrep in the upstream package layout; receipt and restore the complete package | Real missing-companion failure, guarded repair, and installer regressions |
| Set iteration can randomize findings order | Sort unordered collections before serializing | Repeated conversions and varied PYTHONHASHSEED processes |
| A successful native turn can skip untrusted project hooks | Verify actual events and a forced deny, not just process exit | Failed then corrected live host fixtures |
| Host instruction loading and hook trust are separate | Check native hook registry and disclose pending trust | Native project registry inspection |
| Model decisions and external state are not deterministic conversion inputs | Restrict repeatability claims to the tested converter boundary | Verification report scope |
| Exact MCP grants have native per-tool controls | Map exact allow/ask/deny rules to approve/prompt/disabled tools, with deny precedence; preserve reverse mappings and unresolved identities | `tests/test_mcp_permissions.py`, `tests/test_reverse_mcp_permissions.py` |
| Saved configuration can differ from a recorded Codex turn | Inspect only an explicitly selected transcript; report approval and sandbox controls separately from saved settings and managed-policy claims | `tests/test_doctor.py` |
| File deny and file ask rules have different Bash scope | Derived literal file views enforce deny only; do not turn file asks into extra Bash prompts or file allows into shell grants | `tests/test_bash_permission_views.py`; isolated Claude 2.1.268 source comparison |
| Codex lifecycle hooks cap SessionEnd and Interrupt at three seconds | Clamp only the native wrapper; preserve each source timeout and report work that can be interrupted | Startup regression |
| A large skill catalog can shorten descriptions before prompt assembly | Set the documented catalog budget maximum and preserve complete skill files | Generated-config and prompt-input regression |
| Project persona disappeared when only provider-specific instructions were copied | Keep the source role in AGENTS.md and answer host/model identity questions accurately | Startup regression |
| Configured MCP credentials are not connectivity evidence | Diagnose offline by default; sanitize native inventory and suggest login only for an explicit logged-out state | Doctor regressions |
| A source permission ask cannot be inferred from an escalation argument | Keep the call blocked until a verified native approval route exists | Permission regressions and native probe |
| Status commands can depend on a provider-specific session store | Preserve the command, expose session-bound context, and rebind only one verified staged literal with syntax and archive checks | Status regressions |
| Codex's native footer accepts identifiers instead of arbitrary commands | Configure verified built-in fields; generate a PTY launcher that reserves two rows for the converted custom status | Official configuration contract, source audit, and live PTY probe |

Official host contracts were checked against Codex CLI 0.153.4 and the official
[hook](https://learn.chatgpt.com/docs/hooks),
[import](https://learn.chatgpt.com/docs/import), and
[configuration](https://developers.openai.com/codex/config-reference/) documentation.

## Additional reproduced gaps and fixes

- User CLAUDE.md/rules and nested project instructions were omitted. The collector
  now archives and merges explicitly selected user instructions and emits scoped
  nested AGENTS.md files, including cumulative document-size accounting.
- Instruction-only projects were incorrectly rejected. CLAUDE.md, CLAUDE.local.md
  and MCP-only roots are now accepted without an existing .claude directory.
- Source disableAllHooks and disabledMcpjsonServers were preserved as text but
  could reactivate their mechanisms. Explicit disablement now controls activation.
- Different plugin identifiers could collapse to one filesystem slug. Names now
  include source-identity hashes, and resource collisions fail visibly.
- Literal @file examples in inline code or mismatched nested code fences could
  be expanded. Delimiter-aware parsing keeps code examples literal.
- MCP elicitation provides a working AskUserQuestion contract. Native tests covered
  accept, decline, cancel, multiple selection, custom text, model tool dispatch,
  and a forced hook denial. Direct app-server calls bypass native tool hooks.
- A completed question call is not necessarily answered. Empty, partial, malformed,
  declined, and cancelled results now fail answer-evidence checks; the source
  PostToolUseFailure route receives unanswered calls.
- Native approvalPolicy=never declines elicitation before the client sees a form.
  This must not be represented as a user answer or approval.
- Codex 0.153.4 provides `tui.status_line` for ordered built-in footer items. The
  converter emits model, directory, Git branch, remaining-context, and native task-
  progress identifiers. The generated `cue-codex` launcher renders arbitrary converted
  status output in two terminal rows outside Codex's child viewport.
- A release record that contains the source ZIP checksum cannot also be embedded in
  that ZIP without becoming stale. The builder keeps the final release record beside
  the artifacts and excludes it from the source ZIP.
- Codex passes command results to hooks as text, including JSON-shaped results. The
  adapter now keeps that original text when it also parses the JSON, and it flattens
  native content blocks. Post-command hooks can therefore see board reads and output.

- Inherited commands previously survived only as files. They now register as skills
  with complete relative resource trees and an explicit source-command map. The
  optional Claude host view consumes that map without duplicate skill wrappers.
- Shared host staging restores original Claude skill metadata, links agent prompts
  and resources, and clears superseded local entrypoints with installer backups.
  Inherited global hooks and stale rules remain explicit cutover findings.

## Shell file-rule boundary

An isolated Claude 2.1.268 comparison uses generated files and a local canned
provider. In manual mode without an approval handler, direct Read, Edit, and Bash
ask controls require approval. Read/Edit ask rules do not propagate to the tested
cat, head, tail, input-redirection, and explicitly allowed output-redirection calls;
corresponding deny rules block them. This result is bounded to those tested forms
and that version, not every shell construct or interactive mode.

The converter checks recognized literal operands and redirects for source denies.
Dynamic expansion, arbitrary subprocesses, aliases, and recursive traversal remain
incomplete coverage. Native filesystem restrictions remain necessary; these tests
do not authorize removing them or claim that native approval dialogs are verified.

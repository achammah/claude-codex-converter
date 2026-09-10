# Behavioral contract

The converter distinguishes conversion, archival preservation, manual adaptation,
runtime verification, and native activation. Review the emitted findings for the
actual input. Exit 0 means staging succeeded; it does not certify semantic parity.

Known limits in version 0.3.4 (native updater verification in progress):

- Permission matching handles literal shell commands, source prefix rules, and
  restrictive wildcard/compound/newline cases. It is not a shell interpreter or
  an OS security boundary. Dynamic shell execution and per-tool/file semantics
  need host restrictions and dedicated tests. Read/Edit ask rules become filesystem
  restrictions, which differ from Claude's literal per-tool prompts. These source
  ask-to-deny mappings are explicit manual gaps. Managed filesystem denials can be
  non-escalatable; another command approval does not override them. Source permission
  modes, including `acceptEdits`, are not equivalent to Codex sandbox modes. Source
  ask rules stay blocked when no verified native approval route exists. Hosted tools
  and later `write_stdin` input are outside current native PreToolUse coverage.
- Source agent tool lists remain role instructions, not enforceable individual
  tool ACLs. Total lifetime subagent count has no verified native setting.
- Command-hook execution is adapted. Prompt/agent hooks and unrecognized options
  are retained with findings. Notification, PermissionDenied, and StopFailure have
  no assumed native lifecycle equivalent. Source handlers execute sequentially
  inside the bridge, which can change timing relative to Claude.
- `apply_patch` is decomposed into per-file source-tool views. Whole-file contents
  and arbitrary input rewrites cannot always be reconstructed from a patch.
  Each repeated handler retains its source deadline, but the enclosing native
  deadline cannot scale with an unknown file count; conversion reports this gap.
- Explicit command-hook deadlines remain unchanged. Missing deadlines use the
  source event defaults: 600 seconds ordinarily, 30 for UserPromptSubmit, and
  1.5 for SessionEnd. Ordinary native wrappers budget the sum of candidate
  deadlines, rounding each up to whole seconds, plus 30 seconds for adapter work.
  This overhead is an allowance, not a guarantee for arbitrary transcript sizes.
  Invalid deadlines and sums outside the native unsigned 64-bit range reject.
- Included AskUserQuestion uses native MCP forms. Its versioned layout hint groups
  the listed choices and custom answer in the patched renderer. Only an acknowledged
  native response receives `same-question-v1`; standard-host responses preserve their
  separate-field answers and receive `unconfirmed-standard-form`. Structured answer
  details preserve selection versus custom-text provenance. Decline/cancel/empty or
  partial answers never satisfy answer gates.
  Model-dispatched MCP calls fire hooks; direct app-server MCP calls bypass that
  pipeline. Native policy can decline a form without asking a user. Source ask
  decisions are never promoted to allow by an accompanying input rewrite.
- Transcript normalization supports known message/tool records and logs unknown
  formats. It is not a lossless conversion of all provider event formats. Native
  session import is a separate operation and has host-controlled fidelity/limits.
- Skill hooks use explicit activation and remain active for the session. Claude's
  exact invocation lifecycle, nested scopes, and background execution need further
  host integration. The model must run the activation command before the skill.
- Path-scoped instruction rules are inlined with applicability instructions;
  this is not the same as a native conditional loader. Imports outside the selected
  project or explicitly selected user root and unresolved imports remain findings.
  Nested scope is represented by nested AGENTS.md, with host precedence differences
  reported. Dependency/build directories and symlink trees are not searched blindly.
- Existing provider plugins are preserved and their resources discovered. LSP,
  custom transport/auth, marketplace installation, and external hook programs are
  not inferred from plugin names. External command hooks are omitted by default;
  `--include-external-hooks` retains their routes with manual findings.
- User resource inclusion is explicit. Organization-managed settings, arbitrary
  ancestor configurations, remote stores, generated secrets, and indirect runtime
  dependencies cannot be assumed to be inside the selected source boundary.
- Codex can discover a parent repository scope during native detection. Verify
  scope before applying; do not assume nested `.claude` directories were detected.
- Source frontmatter must be parseable. One narrowly defined repair quotes plain
  single-line descriptions containing YAML punctuation and records that repair.
- The optional Claude host view restores native metadata and shared resource
  links. It is tested with fixtures, not a live Claude cutover. Re-inherited user
  hooks/plugins and stale rules need review to avoid duplicate execution.
- `.cue/` is an editable intermediate representation. Re-running conversion reads
  the selected Claude source into a fresh destination; it is not a bidirectional
  live synchronization daemon and does not transfer running processes or approvals.
- Claude `statusLine` commands are preserved and can be run explicitly. Stock Codex's
  native footer is configured with its built-in model, directory, Git branch, remaining-
  context, and task-progress items. The pinned stock runtime cannot attach arbitrary command output. The
  generated `.codex/cue-codex` PTY launcher reserves two terminal rows and renders
  the converted custom status there while forwarding input, output, resize, and exit.
  Plain stock `codex` still shows only native fields. The `setup` command, or
  `convert --native-status`, generates provider configuration for the included
  pinned native patch. The user-authorized terminal installation and companion repair
  passed plain-command rendering and real tool execution. The 0.3.3 installer builds the patched CLI, obtains the same-version official npm
  code-mode host with a pinned SHA-512 archive, and packages hash-pinned ripgrep.
  The official companion has distinct provenance without source-commit attestation.
  A fresh macOS arm64 download and helper launch pass; other platform execution
  remain unverified. The optimized macOS arm64 CLI passes both markers and actual
  CLI-to-helper execution. Terminal question smoke passes; host filesystem permissions block activation,
  so owner activation remains required. The revised
  complete-package application remains verified with disposable filesystem fixtures.
  Automatic legacy state rebinding covers only a recognized helper shape; explicit
  literal mappings use `--legacy-state-root`. Dynamic state discovery remains
  a manual adapter task.
- `SessionEnd` and `Interrupt` native wrappers use the Codex three-second maximum.
  Longer or sequential source handlers remain preserved but can be interrupted;
  conversion reports this instead of claiming lifecycle parity.
- Skill catalog budget is set to the documented maximum and full skill files stay
  intact. Very large enabled catalogs can still need the operator to disable unused
  skills or plugins.
- Installation stages a complete native package before replacing its visible links.
  Receipts preserve both prior command targets and the package inventory. Rollback
  rejects later changes, including unrecorded files, directories, and symbolic links.
- Reverse setup maps supported native Codex controls and archives originals.
  Unproven native hook and permission equivalents remain explicit review gaps.
  Provenance-backed restoration rejects changes to generated controls or archives.
  Runtime-created files inside `.cue`, including Python bytecode caches, currently
  count as added controls and can prevent strict original restoration. Preserve a
  pristine staged bundle for an exact round trip; the clean smoke disables bytecode writes.
- Bidirectional conversation conversion stages native JSONL and exact source
  archives; it does not register sessions in either history picker. Supported
  text, tool and image fixtures passed actual next-request checks in both hosts.
  Unsupported active content rejects strict conversion. Model responses, hidden
  reasoning, running processes and context-window behavior are not equivalent
  merely because historical input is preserved.

Tests cover reproduced data-integrity, permission, native-protocol, and installation
bugs. They include synthetic fixtures, a fake import app-server, actual native MCP forms,
and model-driven hook trips. They do not certify every third-party workflow or
imply that a real native chat import has completed. Each of the four conversion
directions passed 10,000 repeated runs and 128 fresh-process checks at version 0.3.0. Those
historical runs were not repeated for the 0.3.2 timeout change. Version 0.3.3 repeats
all four fixtures with 10,000 runs and 128 fresh processes each, using a verified
non-cloud source snapshot. All provenance round trips pass. These reports
verify artifact determinism at fixed input/output paths, not model determinism.

The 0.3.3 repeatability stamp precedes the official-companion installer correction.
Conversion core hashes are unchanged; the full Python suite is rerun for the
installer change. The 40,000 conversions are not claimed as a second run.

Managed updates use an explicit compatible-release descriptor. No public hosted
converter feed exists. The bundled default reports its current release; a real
new release requires an explicit file or HTTPS descriptor. Newer stock Codex
versions are not assumed compatible. A declared validated release remains a
publisher claim; archive hashes verify bytes. Select a trusted descriptor source.
Native installs, managed updates, and rollback coordinate through an advisory lock.
Update receipts preserve prior package integrity and unique attempt history.

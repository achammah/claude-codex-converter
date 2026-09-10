# Claude–Codex Converter

A standalone converter for Claude Code and Codex setups and conversations, with
source archives, explicit compatibility reports, reversible setup installation,
and an optional bridge to Codex's native importer. No account, project checkout, personal
configuration, credentials, or access to the author's machine is required.

**Requirements:** Python 3.11+ on macOS/Linux. Codex is needed for use of the generated
configuration and for native import, but not for offline conversion. The native
protocol was verified against Codex CLI 0.153.4. Windows hook execution and symlink
installation are not supported by this release.

## Run the single-file distribution

Download/copy `claude-codex-converter.pyz` and run:

```sh
python3 claude-codex-converter.pyz --help
python3 claude-codex-converter.pyz convert /path/to/project --output /tmp/converted-project
```

The `.pyz` bundles its Python dependency. Staging works offline. It never runs
source hooks, installs conversation files, writes into the source project, or
creates native trust records. Use a new output directory for each run.

## Both conversion directions

The source tree includes these operations. Use a distribution built from this
version; older zipapps do not contain them.

```sh
# Claude setup -> Codex setup
python3 claude-codex-converter.pyz convert /path/to/claude-project --output /tmp/codex-project
# Codex setup -> Claude setup
python3 claude-codex-converter.pyz reverse /path/to/codex-project --output /tmp/claude-project --strict

# Conversation files, either direction
python3 claude-codex-converter.pyz conversation --from claude --to codex \
  --input /path/to/claude-session.jsonl --output /tmp/codex-conversation
python3 claude-codex-converter.pyz conversation --from codex --to claude \
  --input /path/to/codex-rollout.jsonl --output /tmp/claude-conversation
```

Conversation output contains `archive/original.jsonl`, a native JSONL file under
`generated/`, `manifest.json`, and `preservation-report.json`. This operation stages
files; it does not register the session in either application's history picker.
Keep the bundle together so a reverse conversion can verify and restore original
bytes. Changed generated history is rejected instead of restoring an obsolete snapshot.

Conversation conversion is strict by default. Unsupported active content produces
an explicit gap and retains original bytes; it does not create a lossy continuation.
`--non-strict` permits archival review, not silent content loss in an emitted session.
`--branch` resolves an explicit branch. `--max-active-bytes` is a caller-supplied
byte guard, not a model-token estimate or a context-window guarantee.

Reverse setup preserves native Codex originals under `.reverse-source/`. Provenance
from a previous forward conversion enables original Claude restoration after
generated-file and archive integrity checks. Native Codex hooks and permission
policies without proven Claude equivalents remain review gaps; `--strict` exits 2.
Use `--model-map CODEX=CLAUDE` for explicit model mappings. Models remain different.

To include inherited user resources and explicit model choices:

```sh
python3 claude-codex-converter.pyz convert /path/to/project \
  --output /tmp/converted-project \
  --global-settings "$HOME/.claude/settings.json" \
  --include-user-resources \
  --model-map sonnet=YOUR_CODEX_WORKER_MODEL \
  --model-map opus=YOUR_CODEX_REVIEW_MODEL
```

Unmapped models inherit the selected Codex model and produce a finding. The tool
does not claim that models from different providers behave identically.

## Review and install

Inspect `AGENTS.md`, `.codex/`, `.cue/compatibility-report.md`,
`.cue/conversion-findings.json`, and `.cue/file-manifest.json` in the output.
Every source file is hashed; original regular files are retained privately under
`.cue-source-archive/`. Symlinks are recorded rather than silently traversed.
Generated configuration and archives can contain **your own** secrets: do not
publish a converted setup without reviewing it.

```sh
python3 claude-codex-converter.pyz install plan /tmp/converted-project /path/to/project \
  --plan /tmp/conversion-install-plan.json
# Read the plan, including all replacement paths.
python3 claude-codex-converter.pyz install apply \
  --plan /tmp/conversion-install-plan.json --receipt /tmp/conversion-install-receipt.json
```

Installation checks source and destination drift, relocates generated paths,
records the original bytes of replacements, and verifies installed bytes. Keep
the private receipt. To restore the previous files:

```sh
python3 claude-codex-converter.pyz install rollback --receipt /tmp/conversion-install-receipt.json
```

Rollback refuses to overwrite later edits. Empty generated directories may remain.
After installation, start Codex in the project, review project trust and the exact
hook definitions in `/hooks`, and test your actual workflows. File generation is
not evidence that hooks have been activated.

Check the generated setup without running its hooks or MCP servers:

```sh
python3 claude-codex-converter.pyz doctor /path/to/project
python3 claude-codex-converter.pyz doctor /path/to/project --native-mcp
python3 claude-codex-converter.pyz doctor /path/to/project --claude-session /path/to/session.jsonl
```

The offline check also reads global hook definitions from `CODEX_HOME` or
`~/.codex`. Supply `--codex-home /path/to/home` to inspect another home directory.
It reports exact duplicate event, matcher, and command definitions across global
and project scopes without executing or printing those commands.

With `--claude-session`, the diagnostic compares saved Claude permissions with
permission modes recorded in the supplied conversation and the configured Codex
approval policy and profile. It requires an exact project-directory match and
does not report message content. For large logs, it examines the newest 512 MiB
and reports the inspected byte range; this is not a complete historical scan.
A runtime mode can differ from saved settings. These observations do not establish
Codex's effective runtime or managed policy, and the diagnostic changes no permissions.

The optional native MCP check reads sanitized authentication metadata. It never
starts OAuth, prints credentials, disables a server, or treats a configured token
as proof that the server is reachable. The report gives a `codex mcp login` command
only when Codex explicitly reports that login is required.

## Status line and board state

### Native status setup

The `setup` command stages conversion with a native status provider and provisions
an executable patched with the status, question-form and managed-updater changes.
Installation and native runtime verification are separate from offline conversion.

```sh
python3 claude-codex-converter.pyz setup /path/to/project \
  --work-dir /tmp/cue-native-setup --runtime-dir /directory/already/on/PATH
# Add --plan-only to prepare the source, build, and installation plans without applying them.
```

The implementation uses the actual executable name `codex`, with no shell alias,
function, or PTY wrapper. It pins the upstream source commit and every patch hash, retains
native package and project rollback receipts, and installs into a directory already on PATH.
The source installer applies the status-provider, question-form, and managed-updater
patches in that order. Before installation, it checks every required feature marker
in the built executable. A binary missing a required feature is rejected. All patch
files remain with the installation plan after zipapp extraction ends.
After applying project configuration, `setup` runs the offline diagnostic and saves
`project-diagnostics.json` beside its plans and receipts. The final result includes
its findings. Existing global duplicates are reported without deleting hooks or
changing permissions.

The package contains the source-built patched `codex`, the same-version official
npm `codex-code-mode-host`, and hash-pinned ripgrep. The installer verifies the
companion archive SHA-512, package identity, version, and named regular-file member.
The companion has official binary provenance; its source commit is not attested.
A fresh macOS arm64 companion download and `--help` launch pass. Other platform
contracts have offline tests; their native execution remains unverified. The normal
optimized macOS arm64 CLI passes both marker checks and actual CLI-to-helper
execution against a local synthetic provider, returning the expected result 42.
The 2026-09-09 helper source build encountered a missing rusty_v8 archive. The
installer therefore builds the CLI and acquires the pinned official companion.
Source compilation requires network access and a supported system linker; Rust is
provisioned into a scoped directory when absent. Normal Codex authentication,
project trust, and hook trust still apply. Installing an upstream Codex update can
replace the patched binary; the converter must revalidate compatibility before
reapplying a patch to another release.
Native package installation supports macOS and Linux. It rejects Windows before
planning because the Windows runtime helpers have not passed this release gate.

### Updates to the managed runtime (0.3.4)

The converter-managed package uses its own updater. It must not invoke Homebrew,
npm, or the stock Codex installer, which can replace the custom patches.
The updater keeps project configuration unchanged and replaces the complete
CLI, code-mode companion, and ripgrep package with a rollback receipt.

The updater supports a configured HTTPS release feed. Without one, it reads the
bundled compatible-release descriptor and reports the bundled release as current.
An upstream Codex release becomes eligible only after native compatibility checks.

The included [release pipeline](RELEASE-PIPELINE.md) discovers upstream releases,
builds complete packages on each supported host, verifies their behavior, and
publishes the compatible feed. Hosting and a successful first workflow run are
required before this becomes an active update service. New `setup` installations
default to `https://github.com/achammah/claude-codex-converter/releases/latest/download/compatible-releases.json`.
Use `setup --update-feed HTTPS_URL` to choose another compatible feed. Planning
records the URL without fetching it or changing host trust. Direct `runtime plan`
also accepts `--update-feed HTTPS_URL`; updates retain that feed setting.

An explicit compatible release descriptor can also be supplied for one invocation:

```sh
python3 claude-codex-converter.pyz update check \
  --installation /path/to/runtime/codex-package.json \
  --source /path/to/compatible-releases.json
python3 claude-codex-converter.pyz update update \
  --installation /path/to/runtime/codex-package.json \
  --source /path/to/compatible-releases.json
```

To connect an older managed installation without a default feed, use an updated
converter and explicitly adopt the published feed during its next package update:

```sh
python3 claude-codex-converter.pyz update update \
  --installation /path/to/runtime/codex-package.json \
  --source https://github.com/achammah/claude-codex-converter/releases/latest/download/compatible-releases.json \
  --adopt-feed
```

Adoption requires an explicit HTTPS source and a successful package update. A
failed check or a current release leaves the existing feed unchanged. Rollback
restores the previous package and feed. Without `--adopt-feed`, a source override
does not change the installation's default feed.

The descriptor identifies a validated release, platform, ordered release sequence,
required patch markers, and SHA-256 of its package ZIP. The updater validates the
archive, executable version, required markers, companions, and current installation
before replacing links. A patch-only release can update the same Codex version.
It rejects changed targets and retains the prior package for rollback. Each update
attempt has a unique receipt, so rollback does not prevent retrying the same release.
An OS advisory lock coordinates native installs, updates, and rollback, and releases
when the process exits. Rollback verifies the prior package before changing links.
The release
source is an explicit trust choice; archive hashes prove byte integrity, not that
an arbitrary publisher is trusted.

### Historical 0.1.3 status behavior

A Claude `statusLine` command is preserved in `.cue/status-line.json`. Hooks write
session-scoped Codex snapshots, and the standalone reader can merge observed
organization and board data from a session-bound provider file:

```sh
python3 claude-codex-converter.pyz status /path/to/project \
  --session "$CODEX_THREAD_ID" --context /path/to/session-context.json
```

Stock Codex's native footer accepts built-in field identifiers, not an arbitrary
status command. The converter configures model, directory, Git branch, remaining
context, and Codex task progress in `tui.status_line`. The generated launcher keeps
Claude's custom status visible in two reserved terminal rows while Codex runs:

```sh
./.codex/cue-codex
# If an installation host removed executable mode:
sh .codex/cue-codex
```

With the stock executable, plain `codex` shows only the native built-in footer.
The 0.3.1 `setup` command, or `convert --native-status`, stages
`tui.status_provider`; plain `convert` does not. The provider becomes active only
after the patched executable and project configuration are installed. Launching
`cue-codex` explicitly activates the preserved source status command. Direct status
reads remain side-effect free unless `--run-source` is passed.

Legacy state roots can be rebound in generated runtime copies while the source
archive stays byte-identical:

```sh
python3 claude-codex-converter.pyz convert /path/to/project \
  --output /tmp/converted-project \
  --legacy-state-root '~/.nexus-mcp/cue-session=.cue/state/codex/sessions'
```

Rebinding requires complete literal anchors in a staged script and a successful
syntax check. All matching code and help references move together. Missing,
dynamic, or unknown bindings remain manual findings.

## AskUserQuestion

Every conversion includes `.cue/scripts/ask_user_question.py` and registers
`mcp__cue_questions__AskUserQuestion`. It uses the host
[MCP elicitation UI](https://learn.chatgpt.com/docs/app-server), supports one to four
Claude-shaped questions, and returns actual accepted answers or decline/cancel.
Options preserve labels, descriptions and text previews; free text is available.
The updated bridge sends a versioned layout hint for the patched native renderer.
Single-choice questions accept a listed choice or a custom answer. Multiple-choice
questions can include custom text alongside selected choices. Structured
`answerDetails` preserve selected options and custom text separately, including
custom text that matches an option label.

A host must acknowledge that layout before the bridge labels it `same-question-v1`.
Older hosts retain their standard separate fields. Their answers are preserved and
labeled `unconfirmed-standard-form`; the bridge does not claim equivalent presentation.

The adapter routes unanswered calls to `PostToolUseFailure` so they cannot satisfy
answer-based gates. It does not turn answers into host execution permission.

The server uses only the Python standard library and can also run independently:

```sh
python3 claude-codex-converter.pyz questions
```

This command speaks MCP JSON-RPC on standard input/output; configure it as an MCP
server rather than typing answers into its terminal. A host without form elicitation
returns an explicit unsupported error. `approvalPolicy=never` causes Codex to decline
forms automatically. A 3,600-second tool timeout is configured; timeout is not consent.
Do not use form elicitation for credentials or secrets.

## Stage Claude against the shared setup

After installing the converted `.cue` tree, stage Claude bindings to that same
project. Use a fresh staging directory:

```sh
python3 claude-codex-converter.pyz host stage /path/to/project --host claude \
  --output /tmp/claude-view
python3 claude-codex-converter.pyz install plan /tmp/claude-view /path/to/project \
  --plan /tmp/claude-view-plan.json
python3 claude-codex-converter.pyz install apply --plan /tmp/claude-view-plan.json \
  --receipt /tmp/claude-view-receipt.json
```

The view restores Claude skill metadata, links agents/resources to the shared tree,
and stages native settings and instructions. It clears superseded local settings
and instruction entrypoints through the same recoverable installer. Read the
stage report before applying: existing global hooks/plugins and stale rules can
still affect Claude loading. The generic Claude view has fixture coverage; live
Claude cutover is not certified. Settings and skill headers are generated snapshots;
restage after changing their shared metadata. This is not a live synchronization
service and does not migrate running agent processes.

## Native import and chat history

Codex's own importer is used for resumable chat history. A Markdown transcript
copy would not create a resumable Codex conversation.

```sh
python3 claude-codex-converter.pyz native detect /path/to/project \
  --include-home --plan /tmp/native-import-plan.json
python3 claude-codex-converter.pyz native apply --plan /tmp/native-import-plan.json \
  --types SESSIONS --receipt /tmp/native-import-receipt.json
python3 claude-codex-converter.pyz native status --receipt /tmp/native-import-receipt.json
```

Detection saves a private selectable inventory. Apply rechecks detection, submits
the exact selected categories, and waits for the matching completion notification.
Partial failures return exit code 2. Timeouts retain the import ID where available;
reconcile status before retrying. Native import writes to real Codex destinations:
the native API has no alternate destination or rollback parameter. Avoid importing
the same configuration categories through both pathways.

Default history detection requests 50 conversations from the last 30 days;
`--max-sessions` and `--max-age-days` let you request another horizon, subject to
the installed Codex implementation. Zero is not advertised as an unlimited mode.

## Coverage and limits

| Source feature | Result |
|---|---|
| All regular `.claude` files, hidden markers, references, scripts, help, fixtures | Hashed and preserved; runtime resources copied, backup/cache files archived |
| Project/user instructions, nested directories, recursive `@file` imports | Full content and directory-scoped `AGENTS.md`; cumulative size cap raised; code examples remain literal; unresolved imports reported |
| Global/project/local settings | Provenance retained; permission and hook lists merged; unknown settings reported |
| Skills and commands | Codex skill bindings with full resource trees; scoped hook activation adapter |
| Agents | Standalone TOML definitions, full instructions, model map, effort, preloaded skill instructions |
| Command hooks | Tool/event/output normalization, patch file expansion, shell failure dispatch, permission veto preservation |
| Permission rules | Deny/ask precedence, path and symlink checks, patch destinations, MCP/agent selectors, and conservative filesystem restrictions; source ask-to-deny mappings remain explicit manual gaps |
| MCP | Supported stdio/HTTP declarations, explicit disablement retained; authentication remains a target-host action |
| AskUserQuestion | Included MCP stdio server; real forms, single/multiple selections, free text, decline/cancel, and hook observation |
| Status line and boards | `setup` installs the patched native provider for plain `codex`; `convert --native-status` stages its configuration. Stock Codex requires the historical launcher for custom source output. |
| User resources and enabled plugins | Installed package inventory, skill/agent discovery, source manifest preservation |
| Recent chat history | Optional native Codex importer with private completion receipts |
| Installation | Explicit plan, drift checks, byte verification, backups, rollback |

Source permission modes are not interchangeable with Codex sandbox modes. A source
Read “ask first” rule mapped to a filesystem denial changes behavior and is reported
as a manual gap. A managed denial can be non-escalatable, so approving a repeated
command does not necessarily grant access. The converter does not automatically
broaden permissions to remove those differences.

**This release does not promise universal behavioral equivalence.** Arbitrary
executable hooks, external services, exact per-tool agent ACLs, unsupported lifecycle
events, command interpolation, native UI behavior, plugin LSP servers, and active
in-memory agent state can require provider-specific adapters. A preserved file is
not labeled as a working mechanism. `--strict` exits 2 while any manual or runtime
verification finding remains. See [COMPATIBILITY.md](COMPATIBILITY.md).

## Verification

Run the Python suite from the standalone source checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m unittest discover -s tests -v
```

The release pipeline separately exercises the actual native executable, helper,
question form, update prompt, complete-package replacement and rollback. Its
published evidence binds results to source, patches, package bytes and target.
See [the native smoke instructions](tests/native_smoke/README.md).

Repeated conversion fixtures measure deterministic file output and provenance
restoration. They do not establish identical model behavior or correctness for
all possible setups. Historical development reports are excluded from public
exports because they can contain local installation paths and private evidence.

Native tests use temporary trust overrides for reviewed synthetic hooks; they do
not grant permanent trust to a converted project. Codex 0.154.0 has candidate
patches in this source tree; patch replay alone does not certify its runtime.

## Install from source

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/claude-codex-converter --help
.venv/bin/python -m unittest discover -s tests -v
```

Build a fresh single-file application after installing dependencies:

```sh
.venv/bin/python build.py --output dist
```

The source distribution includes the converter, runtime, templates, tests, license,
builder, and release automation. It excludes personal
configuration, user chats, credentials, project-specific hooks, and the external
release record. Findings are emitted by every conversion so new source patterns can
become documented mappings and regression cases.

Documentation: [Codex import](https://learn.chatgpt.com/docs/import),
[hooks](https://learn.chatgpt.com/docs/hooks),
[configuration](https://developers.openai.com/codex/config-reference/).

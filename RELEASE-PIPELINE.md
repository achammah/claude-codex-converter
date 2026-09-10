# Publishing compatible runtime releases

The pipeline prepares releases for the managed updater. It does not publish source
repositories or infer compatibility from an upstream version number. Publication
requires an explicitly configured HTTPS destination. Repository credentials remain
with the publishing runner.

Run the module from the source distribution:

```sh
python3 -m converter.release_pipeline --help
```

## Candidate and patch validation

`discover --metadata release.json --commit COMMIT --output candidate.json`
consumes upstream release metadata and an independently resolved full commit.
Its result remains a candidate. Draft and prerelease records are rejected.

`patch-check --source CHECKOUT --manifest native/manifest.json --output patches.json`
checks the source commit and patch hashes, then applies the ordered patches in a
temporary checkout. It leaves the original checkout unchanged. This proves patch
application, not compilation or runtime behavior. A newer upstream version whose
patches fail remains pending; the published compatible feed stays unchanged.

A candidate manifest can declare `sourcePreparation` with
`program: normalize_workspace_lock.py` and its SHA-256. The bytes must match the
bundled normalizer. It updates inherited local workspace versions before patch
application; registry packages remain unchanged. `apply_candidate_source` runs the
same preparation and patch sequence in a runner-owned disposable checkout.

## Build and runner evidence

The native builder owns compilation. The complete package contains `bin/codex`,
`bin/codex-code-mode-host`, `codex-path/rg`, managed updater resources, and
`codex-package.json`. Linux additionally requires `bin/codex-linux-sandbox`.

The runner evidence binds its result to these exact inputs:

- SHA-256 of canonical candidate and patch-proof JSON.
- SHA-256 of the package inventory, including file hashes and modes.
- Target triple, release identity, and positive release sequence.

Each required check carries `passed: true` and the SHA-256 of its captured log:
`native_tests`, `cli_helper_execution`, `question_ui`, `update_route`, and
`update_rollback`. A report is a trusted runner assertion. The publishing job must
collect it from its own successful jobs; accepting arbitrary uploaded reports
would not establish compatibility.

Use `release_pipeline.canonical`, `sha`, and `inventory` to compute the binding.
The package command accepts `--root`, `--candidate`, `--patch-proof`, `--evidence`,
`--output`, `--base-url`, and `--record`. The last path receives the release record.
The archive uses deterministic entry ordering, timestamps, and file modes.
Existing archives with different bytes are rejected.

## Publication and feed advancement

Publish immutable archives and evidence first. Download the hosted files and
verify their hashes. Pass the release-record list to `advance --releases` and
a publication verification map to `--published`, keyed by archive URL. Each map
value contains `sha256`, `evidenceSha256`, and `verified: true`.

`advance --feed PATH` holds an advisory lock and atomically replaces the descriptor
only after every new record passes. Conflicting or older sequences, unverified
publication, and unknown targets leave the existing feed unchanged. Repeating an
identical release is idempotent. A patch-only release increments the sequence even
when the upstream version does not change.

The existing target contracts cover macOS and Linux on ARM64 and x64. Include only
targets whose own runtime checks passed. A missing runner or failed check means
that target remains pending; another platform's evidence cannot qualify it.

Remote-feed activation and hosting are separate integration steps. This module
does not invent a default endpoint, change installed permissions, or update an
installed runtime.

For installations using a published feed, pass `feed_url` to `stage_manager`, or
`--update-feed HTTPS_URL` to `runtime plan`. Default update checks fetch that URL.
Fetch failures remain errors; they do not silently report the bundled release as
current. An explicit updater `--source` overrides one invocation. Updates preserve
the installation's configured feed, and rollback restores its previous metadata.

New `setup` installs default to the public project's feed:
`https://github.com/achammah/claude-codex-converter/releases/latest/download/compatible-releases.json`.
`setup --update-feed HTTPS_URL` overrides this default. Plan-only mode records the
URL without fetching it; offline setup conversion remains independent of the feed.

Older managed installations without a feed can use the current converter's
`update update --installation MANIFEST --source HTTPS_URL --adopt-feed` command.
The explicit URL becomes the default only inside a successful atomic package
replacement. If no newer compatible package exists, `feedAdopted` is false and
metadata is unchanged. Failure leaves the prior package intact, and rollback
restores its original feed policy. A plain `--source` remains a one-time override.

## GitHub Actions service

The standalone repository includes `.github/workflows/compatible-codex.yml`.
Once installed at a repository root, its scheduled discovery resolves the latest
stable upstream tag to an exact commit. A version-specific manifest under
`native/manifests/<version>/` takes precedence over the baseline manifest.

`scripts/ci_release.py` prepares immutable candidate inputs. Separate native jobs
run `scripts/release_runner.py` on macOS and Linux, each on ARM64 and x64.
Compilation uses one Cargo job per host. The jobs execute native library tests,
the code-mode helper, the question form, update routing, update prompts and rollback.
`scripts/ci_package.py` accepts only evidence bound to that candidate and package.

The publishing job requires every configured target to pass. It creates a
prerelease pinned to the converter source commit, uploads packages and evidence,
and downloads them again to verify their hashes. Only then does it promote the
release and its `compatible-releases.json` feed. A retry verifies existing assets
and uploads missing assets; it never overwrites different bytes.

Failed compatibility checks retain diagnostic logs and create one open issue.
An incompatible upstream change needs a reviewed patch or manifest correction;
the service does not invent code repairs or offer an unverified upstream binary.
The installed runtime remains usable while a candidate is being qualified.

The feed address is derived from the explicitly selected release repository:
`https://github.com/<owner>/<repository>/releases/latest/download/compatible-releases.json`.
Public downloads require a public release repository. Do not copy a private parent
repository, organization configuration, session logs, or local verification reports
into that repository. Review the standalone export before the first publication.

Implementation tests cover discovery, identity binding, failed-check rejection and
publication retries. Actual hosted workflow execution and feed activation remain
separate acceptance steps; source tests alone do not establish a running service.

# Codex 0.154.0 candidate patches

Source commit: `6b9826e3aa83b1a5947db50f4332cb9c65f1b340`, official tag `rust-v0.154.0`.

Run the manifest's pinned workspace-lock preparation first, then apply status, question, and managed-updater patches in order. This exact sequence passes on a clean checkout; all 36 resulting changed/new files match the adapted source.

The question patch is unchanged. Status refresh insertions preserve upstream rate-limit prefetch and model-picker refresh. The doctor check preserves upstream async execution through spawn_blocking.

The status patch also adds its codex-utils-pty dependency to the codex-tui Cargo.lock entry. Actual Cargo resolution produces exactly this one-line correction after workspace version preparation. Locked metadata resolution passes for both macOS targets and both Linux musl targets; a fresh patch replay equals Cargo's resolved lock byte-for-byte. Native compilation remains a separate release gate.

The four companion package records come from the official npm registry at matching version 0.154.0. Their archives and runtime compatibility still require release-runner verification. No candidate native build or runtime test is claimed by this manifest. The runner must supply release identity and sequence.

The release runner records patch proof and native test evidence for each build. The focused test harness is `tests/native_smoke/native_tests.py` at the repository root.

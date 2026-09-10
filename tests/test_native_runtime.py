"""Disposable-fixture tests for the pinned native Codex installer backend."""

import importlib.util
import io
import tarfile
import hashlib
import base64
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "converter" / "native_runtime.py"


def load_module():
    spec = importlib.util.spec_from_file_location("test_native_runtime_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cue-native-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.native = load_module()
        self.companion_fetch = mock.patch.object(self.native, "_fetch_official_companion", return_value=None)
        self.companion_fetch.start()
        self.addCleanup(self.companion_fetch.stop)
        self.source = self.root / "source"
        cli = self.source / "codex-rs" / "cli"
        host = self.source / "codex-rs" / "code-mode-host"
        cli.mkdir(parents=True)
        host.mkdir(parents=True)
        (self.source / "codex-rs" / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.95.0"\n', encoding="utf-8"
        )
        (self.source / "codex-rs" / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["cli", "code-mode-host"]\n[workspace.package]\nversion = "0.153.4"\n',
            encoding="utf-8",
        )
        (cli / "Cargo.toml").write_text(
            '[package]\nname = "fixture-codex-cli"\nversion = "0.0.0"\n'
            '[[bin]]\nname = "codex"\npath = "src/main.rs"\n',
            encoding="utf-8",
        )
        (host / "Cargo.toml").write_text(
            '[package]\nname = "fixture-code-mode-host"\nversion = "0.0.0"\n'
            '[[bin]]\nname = "codex-code-mode-host"\npath = "src/main.rs"\n',
            encoding="utf-8",
        )
        rg_manifest = self.source / "scripts" / "codex_package" / "rg"
        rg_manifest.parent.mkdir(parents=True)
        _, dotslash_platform, suffix = self.native._host_package_target()
        rg_manifest.write_text(
            '#!/usr/bin/env dotslash\n' + json.dumps({"platforms": {dotslash_platform: {
                "size": 16, "hash": "sha256", "digest": "0" * 64,
                "format": "tar.gz", "path": "fixture/rg" + suffix,
                "providers": [{"url": "https://github.com/BurntSushi/ripgrep/fixture.tar.gz"}],
            }}}) + '\n', encoding="utf-8",
        )
        self.patch = self.root / "native.patch"
        self.patch.write_bytes(b"fixture patch\n")
        self.question_patch = self.root / "question.patch"
        self.question_patch.write_bytes(b"fixture question patch\n")
        self.update_patch = self.root / "update.patch"
        self.update_patch.write_bytes(b"fixture managed updater patch\n")
        self.metadata = self.root / "native.json"
        self.metadata.write_text(json.dumps({
            "schema_version": 1,
            "release_id": "fixture-native-1",
            "release_sequence": 1,
            "upstream_commit": self.native.UPSTREAM_COMMIT,
            "patch_sha256": self.native.sha256(self.patch.read_bytes()),
            "patch_file": self.patch.name,
            "feature_marker": "fixture_native_status_provider",
            "additional_patches": [{"patch_file": self.question_patch.name,
                "patch_sha256": self.native.sha256(self.question_patch.read_bytes()),
                "feature_marker": self.native.QUESTION_FEATURE_MARKER},
                {"patch_file": self.update_patch.name,
                 "patch_sha256": self.native.sha256(self.update_patch.read_bytes()),
                 "feature_marker": self.native.UPDATE_FEATURE_MARKER}],
        }), encoding="utf-8")
        tools = self.root / "tools"
        tools.mkdir()
        self.git = tools / "git"
        self.cargo = tools / "cargo"
        self.rustc = tools / "rustc"
        for tool in (self.git, self.cargo, self.rustc):
            tool.write_bytes(b"fixture executable")
            tool.chmod(0o755)
        self.toolchain = {
            "channel": "1.95.0",
            "cargo": str(self.cargo),
            "rustc": str(self.rustc),
            "cargo_home": str(self.root / "cargo-home"),
            "rustup_home": str(self.root / "rustup-home"),
        }
        self.install_dir = self.root / "bin"
        self.build_root = self.root / "build"
        self.plan = self.root / "plan.json"
        self.receipt = self.root / "receipt.json"

    def make_plan(self, **options):
        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"):
            return self.native.plan_install(
                self.source, self.patch, self.metadata, self.install_dir,
                self.plan, self.build_root, self.toolchain, git=str(self.git), **options,
            )

    def fake_build_run(self, argv, *, cwd=None, env=None, log_path=None):
        if len(argv) > 1 and argv[1] == "clone":
            shutil.copytree(self.source, self.build_root)
        elif argv[0] == str(self.cargo):
            contract = self.native._build_contract(self.source)
            for artifact in contract["artifacts"]:
                output = self.build_root / artifact["output"]
                output.parent.mkdir(parents=True, exist_ok=True)
                data = (b"native fixture codex fixture_native_status_provider CUE_QUESTION_UI_V1 CUE_MANAGED_UPDATE_V1" if
                        artifact["name"] == "codex" else b"fixture runtime helper")
                output.write_bytes(data)
                output.chmod(0o755)
            self.assertEqual(env["CARGO_HOME"], self.toolchain["cargo_home"])
            self.assertEqual(env["RUSTUP_HOME"], self.toolchain["rustup_home"])
            self.assertEqual(env["CARGO_TARGET_DIR"], str(self.build_root / "codex-rs" / "target"))
            self.assertEqual(env["RUSTC"], str(self.rustc))
            self.assertEqual(env["PATH"].split(os.pathsep)[0], str(self.rustc.parent))
            self.assertIn("fixture-codex-cli", argv)
        elif argv[0] == str(self.build_root / "codex-rs" / "target" / "release" / "codex"):
            return subprocess.CompletedProcess(argv, 0, "codex-cli 0.153.4\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def apply(self):
        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "_run", side_effect=self.fake_build_run), \
                mock.patch.object(self.native, "_run_build", side_effect=self.fake_build_run), \
                mock.patch.object(self.native, "_fetch_ripgrep", side_effect=self.fake_fetch_rg):
            return self.native.apply_install(self.plan, self.receipt)

    def fake_fetch_rg(self, contract, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fixture ripgrep")
        destination.chmod(0o755)

    def companion_archive(self, version=None, symlink=False):
        contract = self.native._official_companion_contract("0.153.4")
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as archive:
            data = json.dumps({"name": "@openai/codex", "version": version or contract["version"]}).encode()
            info = tarfile.TarInfo("package/package.json"); info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            info = tarfile.TarInfo(contract["member"])
            if symlink:
                info.type = tarfile.SYMTYPE; info.linkname = "/tmp/outside"
                archive.addfile(info)
            else:
                data = b"official matching companion"; info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        data = out.getvalue()
        contract["integrity"] = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        return contract, data

    def test_official_companion_checks_integrity_and_extracts_only_named_file(self):
        self.companion_fetch.stop()
        contract, data = self.companion_archive()
        output = self.root / "download/host"
        with mock.patch.object(self.native.urllib.request, "urlopen", return_value=io.BytesIO(data)):
            self.native._fetch_official_companion(contract, output)
        self.assertEqual(output.read_bytes(), b"official matching companion")
        self.assertEqual(output.stat().st_mode & 0o777, 0o755)
        self.assertEqual([p.name for p in output.parent.iterdir()], ["host"])

    def test_official_companion_rejects_archive_corruption_before_extracting(self):
        self.companion_fetch.stop()
        contract, data = self.companion_archive()
        output = self.root / "download/host"
        with mock.patch.object(self.native.urllib.request, "urlopen", return_value=io.BytesIO(data + b"corrupt")):
            with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                self.native._fetch_official_companion(contract, output)
        self.assertFalse(output.exists())

    def test_official_companion_rejects_wrong_version_and_symlink_member(self):
        self.companion_fetch.stop()
        for options, error in (({"version": "other"}, "version mismatch"), ({"symlink": True}, "regular file")):
            with self.subTest(options=options):
                contract, data = self.companion_archive(**options)
                output = self.root / "download/host"
                with mock.patch.object(self.native.urllib.request, "urlopen", return_value=io.BytesIO(data)):
                    with self.assertRaisesRegex(ValueError, error):
                        self.native._fetch_official_companion(contract, output)
                self.assertFalse(output.exists())

    def test_official_companion_is_not_compiled_and_provenance_survives_receipt(self):
        plan = self.make_plan()
        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "_run", side_effect=self.fake_build_run), \
                mock.patch.object(self.native, "_run_build", side_effect=self.fake_build_run) as build, \
                mock.patch.object(self.native, "_fetch_ripgrep", side_effect=self.fake_fetch_rg):
            receipt = self.native.apply_install(self.plan, self.receipt)
        argv = build.call_args.args[0]
        self.assertNotIn("fixture-code-mode-host", argv)
        self.assertEqual(receipt["official_companion"], plan["official_companion"])

    def test_plan_discovers_package_and_plain_binary_from_source(self):
        result = self.make_plan()
        self.assertEqual(result["build"]["package"], "fixture-codex-cli")
        self.assertEqual(result["build"]["binary"], "codex")
        self.assertEqual(
            [row["name"] for row in result["build"]["artifacts"]][:2],
            ["codex", "codex-code-mode-host"],
        )
        self.assertEqual(Path(result["target"]).name, "codex")
        self.assertEqual([row["name"] for row in result["runtime_files"]],
                         ["codex", "codex-code-mode-host"])
        self.assertNotIn("command", result["build"])
        self.assertEqual(stat.S_IMODE(self.plan.stat().st_mode), 0o600)

    def test_platform_contract_supports_native_macos_and_linux_and_rejects_windows(self):
        cases = (
            ("darwin", "arm64", "aarch64-apple-darwin", "macos-aarch64"),
            ("linux", "x86_64", "x86_64-unknown-linux-gnu", "linux-x86_64"),
        )
        for system, machine, target, dotslash in cases:
            with self.subTest(system=system), mock.patch.object(self.native.sys, "platform", system), \
                    mock.patch.object(self.native.platform, "machine", return_value=machine):
                self.assertEqual(self.native._host_package_target(), (target, dotslash, ""))
        with mock.patch.object(self.native.sys, "platform", "win32"), \
                self.assertRaisesRegex(RuntimeError, "not implemented for Windows"):
            self.native._host_package_target()

    def test_absent_codex_installs_direct_binary_and_rolls_back_to_absent(self):
        self.make_plan()
        result = self.apply()
        target = self.install_dir / "codex"
        self.assertEqual(result["state"], "installed")
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve().read_bytes(), b"native fixture codex fixture_native_status_provider CUE_QUESTION_UI_V1 CUE_MANAGED_UPDATE_V1")
        helper = self.install_dir / "codex-code-mode-host"
        self.assertTrue(helper.is_symlink())
        self.assertEqual(helper.resolve().read_bytes(), b"fixture runtime helper")
        runtime_dir = Path(result["runtime_dir"])
        self.assertEqual((runtime_dir / "codex-path" / "rg").read_bytes(), b"fixture ripgrep")
        rolled_back = self.native.rollback_install(self.receipt)
        self.assertEqual(rolled_back["state"], "rolled-back")
        self.assertFalse(target.exists())
        self.assertFalse(helper.exists())
        self.assertFalse(runtime_dir.exists())

    def test_existing_codex_file_is_backed_up_and_restored(self):
        self.install_dir.mkdir()
        target = self.install_dir / "codex"
        target.write_bytes(b"old codex")
        target.chmod(0o700)
        self.make_plan()
        self.apply()
        self.native.rollback_install(self.receipt)
        self.assertEqual(target.read_bytes(), b"old codex")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_existing_codex_symlink_is_replaced_itself_and_restored(self):
        self.install_dir.mkdir()
        old = self.root / "old-codex"
        old.write_bytes(b"old")
        target = self.install_dir / "codex"
        target.symlink_to(old)
        self.make_plan()
        self.apply()
        self.assertTrue(target.is_symlink())
        self.assertNotEqual(os.readlink(target), str(old))
        self.assertEqual(old.read_bytes(), b"old")
        self.native.rollback_install(self.receipt)
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(old))

    def test_source_drift_is_rejected_before_build_or_target_write(self):
        self.make_plan()
        with mock.patch.object(self.native, "_git_head", return_value="0" * 40):
            with self.assertRaisesRegex(ValueError, "source changed"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertFalse((self.install_dir / "codex").exists())
        self.assertFalse(self.build_root.exists())

    def test_receipt_cannot_replace_installed_binary(self):
        self.make_plan()
        target = self.install_dir / "codex"
        with self.assertRaisesRegex(ValueError, "Receipt must not replace"):
            self.native.apply_install(self.plan, target)
        self.assertFalse(target.exists())
        self.assertFalse(self.build_root.exists())

    def test_patch_corruption_is_rejected_before_build_or_target_write(self):
        self.make_plan()
        self.patch.write_bytes(b"corrupt")
        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"):
            with self.assertRaisesRegex(ValueError, "patch hash"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertFalse((self.install_dir / "codex").exists())
        self.assertFalse(self.build_root.exists())

    def test_later_binary_edit_blocks_rollback(self):
        self.make_plan()
        self.apply()
        target = self.install_dir / "codex"
        target.unlink()
        target.write_bytes(b"later edit")
        with self.assertRaisesRegex(ValueError, "changed after installation"):
            self.native.rollback_install(self.receipt)
        self.assertEqual(target.read_bytes(), b"later edit")

    def test_target_change_during_cargo_build_is_not_overwritten(self):
        self.install_dir.mkdir()
        target = self.install_dir / "codex"
        target.write_bytes(b"old")
        self.make_plan()

        def run(argv, *, cwd=None, env=None, log_path=None):
            result = self.fake_build_run(argv, cwd=cwd, env=env, log_path=log_path)
            if argv[0] == str(self.cargo):
                target.write_bytes(b"concurrent replacement")
            return result

        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "_run", side_effect=run), \
                mock.patch.object(self.native, "_run_build", side_effect=run), \
                mock.patch.object(self.native, "_fetch_ripgrep", side_effect=self.fake_fetch_rg):
            with self.assertRaisesRegex(ValueError, "changed while"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertEqual(target.read_bytes(), b"concurrent replacement")
        self.assertFalse(self.receipt.exists())

    def test_install_directory_symlink_replacement_during_build_is_rejected(self):
        self.install_dir.mkdir()
        self.make_plan()

        def run(argv, *, cwd=None, env=None, log_path=None):
            result = self.fake_build_run(argv, cwd=cwd, env=env, log_path=log_path)
            if argv[0] == str(self.cargo):
                moved = self.root / "bin-before"
                replacement = self.root / "bin-replacement"
                self.install_dir.rename(moved)
                replacement.mkdir()
                self.install_dir.symlink_to(replacement, target_is_directory=True)
            return result

        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "_run", side_effect=run), \
                mock.patch.object(self.native, "_run_build", side_effect=run), \
                mock.patch.object(self.native, "_fetch_ripgrep", side_effect=self.fake_fetch_rg):
            with self.assertRaisesRegex(ValueError, "directory changed while"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertTrue(self.install_dir.is_symlink())
        self.assertFalse((self.install_dir / "codex").exists())
        self.assertFalse(self.receipt.exists())

    def test_built_binary_without_feature_marker_is_rejected(self):
        self.make_plan()

        def run(argv, *, cwd=None, env=None, log_path=None):
            result = self.fake_build_run(argv, cwd=cwd, env=env, log_path=log_path)
            if argv[0] == str(self.cargo):
                output = self.build_root / "codex-rs" / "target" / "release" / "codex"
                output.write_bytes(b"unpatched codex")
                output.chmod(0o755)
            return result

        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "_run", side_effect=run), \
                mock.patch.object(self.native, "_run_build", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "feature marker"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertFalse((self.install_dir / "codex").exists())
        self.assertFalse(self.receipt.exists())

    def test_status_only_binary_is_rejected_before_any_installation(self):
        self.make_plan()
        def run(argv, **kwargs):
            result = self.fake_build_run(argv, **kwargs)
            if argv[0] == str(self.cargo):
                output = self.build_root / "codex-rs/target/release/codex"
                output.write_bytes(b"fixture_native_status_provider")
            return result
        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "_run", side_effect=run), \
                mock.patch.object(self.native, "_run_build", side_effect=run), \
                mock.patch.object(self.native, "_fetch_ripgrep", side_effect=self.fake_fetch_rg):
            with self.assertRaisesRegex(RuntimeError, "CUE_QUESTION_UI_V1"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.install_dir.exists())

    def test_question_patch_corruption_rejected_before_build(self):
        self.make_plan()
        self.question_patch.write_bytes(b"changed question patch")
        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"):
            with self.assertRaisesRegex(ValueError, "Additional native patch hash"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertFalse(self.build_root.exists())

    def test_production_package_sequences_enable_next_release_selection(self):
        from converter import managed_update
        self.make_plan()
        first = self.apply()
        first_manifest = Path(first["runtime_dir"]) / "codex-package.json"
        other = NativeRuntimeTests(methodName="runTest")
        other.setUp()
        self.addCleanup(other.doCleanups)
        metadata = json.loads(other.metadata.read_text())
        metadata.update(release_id="fixture-native-2", release_sequence=2)
        other.metadata.write_text(json.dumps(metadata))
        other.make_plan()
        second = other.apply()
        second_root = Path(second["runtime_dir"])
        result = managed_update.selection(first_manifest, second_root / "codex-resources/compatible-releases.json")[0]
        self.assertEqual(result["state"], "update_available")
        self.assertNotEqual(result["releaseId"], result["installedReleaseId"])
        self.assertEqual(result["version"], "0.153.4")

    def test_production_plan_and_package_preserve_explicit_update_feed(self):
        url='https://updates.example.invalid/compatible.json'
        plan=self.make_plan(update_feed=url)
        self.assertEqual(plan['update_feed'],url)
        result=self.apply()
        metadata=json.loads((Path(result['runtime_dir'])/'codex-package.json').read_text())
        self.assertEqual(metadata['cueUpdate']['feedUrl'],url)

    def test_invalid_update_feed_rejected_before_plan_write(self):
        with self.assertRaisesRegex(ValueError,'HTTPS'):
            self.make_plan(update_feed='https://user:secret@example.invalid/feed')
        self.assertFalse(self.plan.exists())

    def test_native_install_commit_obeys_managed_update_lock(self):
        self.install_dir.mkdir()
        self.make_plan()
        with self.native._installation_lock(self.install_dir / "codex"):
            with self.assertRaisesRegex(ValueError, "installation lock"):
                self.apply()
        self.assertFalse((self.install_dir / "codex").exists())

    def test_direct_install_rollback_rejects_missing_prior_private_package(self):
        old = self.root / "old-private-package"
        (old / "bin").mkdir(parents=True)
        (old / "codex-package.json").write_text("{}")
        for name in ("codex", "codex-code-mode-host"):
            (old / "bin" / name).write_text("prior runtime")
        self.install_dir.mkdir()
        for name in ("codex", "codex-code-mode-host"):
            (self.install_dir / name).symlink_to(old / "bin" / name)
        self.make_plan()
        receipt = self.apply()
        self.assertIn("prior_package", receipt["files"][0])
        shutil.rmtree(old)
        with self.assertRaisesRegex(ValueError, "Previous runtime package"):
            self.native.rollback_install(self.receipt)
        self.assertTrue(Path(receipt["runtime_dir"]).is_dir())
        self.assertEqual((self.install_dir / "codex").resolve(), Path(receipt["runtime_dir"]) / "bin/codex")

    def test_release_sequence_is_required_and_validated(self):
        metadata = json.loads(self.metadata.read_text())
        metadata["release_sequence"] = True
        self.metadata.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "release sequence"):
            self.make_plan()

    def test_missing_managed_updater_marker_rejects_before_installation(self):
        self.make_plan()
        original = self.fake_build_run
        def missing_marker(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[0] == str(self.cargo):
                output = self.build_root / "codex-rs/target/release/codex"
                output.write_bytes(output.read_bytes().replace(b"CUE_MANAGED_UPDATE_V1", b"missing"))
            return result
        with mock.patch.object(self, "fake_build_run", side_effect=missing_marker):
            with self.assertRaisesRegex(RuntimeError, "CUE_MANAGED_UPDATE_V1"):
                self.apply()
        self.assertFalse((self.install_dir / "codex").exists())

    def test_metadata_without_question_marker_rejects_plan(self):
        metadata = json.loads(self.metadata.read_text())
        metadata.pop("additional_patches")
        self.metadata.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "question renderer"):
            self.make_plan()
        self.assertFalse(self.plan.exists())

    def test_edited_plan_cannot_remove_question_patch_requirement(self):
        self.make_plan()
        plan = json.loads(self.plan.read_text())
        plan["patches"] = plan["patches"][:1]
        self.plan.write_text(json.dumps(plan))
        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"):
            with self.assertRaisesRegex(ValueError, "patch set changed"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertFalse(self.build_root.exists())

    def test_question_patch_filename_cannot_escape_bundle(self):
        metadata = json.loads(self.metadata.read_text())
        metadata["additional_patches"][0]["patch_file"] = "../question.patch"
        self.metadata.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "Unsafe additional"):
            self.make_plan()
        self.assertFalse(self.plan.exists())

    def test_all_three_patches_applied_in_order_before_build(self):
        self.make_plan()
        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "_run", side_effect=self.fake_build_run) as run, \
                mock.patch.object(self.native, "_run_build", side_effect=self.fake_build_run), \
                mock.patch.object(self.native, "_fetch_ripgrep", side_effect=self.fake_fetch_rg):
            self.native.apply_install(self.plan, self.receipt)
        applied = [call.args[0][-1] for call in run.call_args_list
                   if "apply" in call.args[0] and "--check" not in call.args[0]]
        self.assertEqual(applied, [str(self.patch), str(self.question_patch), str(self.update_patch)])

    def test_missing_built_helper_is_rejected_before_main_target_replacement(self):
        self.install_dir.mkdir()
        target = self.install_dir / "codex"
        target.write_bytes(b"working prior codex")
        self.make_plan()

        def run(argv, *, cwd=None, env=None, log_path=None):
            result = self.fake_build_run(argv, cwd=cwd, env=env, log_path=log_path)
            if argv[0] == str(self.cargo):
                (self.build_root / "codex-rs" / "target" / "release" /
                 "codex-code-mode-host").unlink()
            return result

        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "_run", side_effect=run), \
                mock.patch.object(self.native, "_run_build", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "codex-code-mode-host"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertEqual(target.read_bytes(), b"working prior codex")
        self.assertFalse((self.install_dir / "codex-code-mode-host").exists())
        self.assertFalse(self.receipt.exists())

    def test_companion_target_drift_during_build_blocks_all_runtime_replacement(self):
        self.install_dir.mkdir()
        target = self.install_dir / "codex"
        helper = self.install_dir / "codex-code-mode-host"
        target.write_bytes(b"prior codex")
        helper.write_bytes(b"prior helper")
        self.make_plan()

        def run(argv, *, cwd=None, env=None, log_path=None):
            result = self.fake_build_run(argv, cwd=cwd, env=env, log_path=log_path)
            if argv[0] == str(self.cargo):
                helper.write_bytes(b"concurrent helper")
            return result

        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "_run", side_effect=run), \
                mock.patch.object(self.native, "_run_build", side_effect=run), \
                mock.patch.object(self.native, "_fetch_ripgrep", side_effect=self.fake_fetch_rg):
            with self.assertRaisesRegex(ValueError, "codex-code-mode-host"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertEqual(target.read_bytes(), b"prior codex")
        self.assertEqual(helper.read_bytes(), b"concurrent helper")
        self.assertFalse(self.receipt.exists())

    def test_rollback_restores_main_and_companion_with_package_removal(self):
        self.install_dir.mkdir()
        target = self.install_dir / "codex"
        helper = self.install_dir / "codex-code-mode-host"
        target.write_bytes(b"prior codex")
        helper_source = self.root / "prior-helper"
        helper_source.write_bytes(b"prior helper")
        helper.symlink_to(helper_source)
        self.make_plan()
        installed = self.apply()
        runtime_dir = Path(installed["runtime_dir"])
        self.assertTrue(target.is_symlink())
        self.assertTrue(helper.is_symlink())
        self.native.rollback_install(self.receipt)
        self.assertEqual(target.read_bytes(), b"prior codex")
        self.assertFalse(target.is_symlink())
        self.assertEqual(os.readlink(helper), str(helper_source))
        self.assertFalse(runtime_dir.exists())

    def test_rollback_refuses_unrecorded_package_entries_of_every_path_kind(self):
        self.make_plan()
        installed = self.apply()
        runtime_dir = Path(installed["runtime_dir"])
        mutations = (
            ("empty directory", lambda path: path.mkdir(), lambda path: path.rmdir()),
            ("directory symlink", lambda path: path.symlink_to(runtime_dir / "bin", target_is_directory=True),
             lambda path: path.unlink()),
            ("broken symlink", lambda path: path.symlink_to(runtime_dir / "missing"),
             lambda path: path.unlink()),
        )
        for index, (label, create, remove) in enumerate(mutations):
            with self.subTest(label=label):
                unexpected = runtime_dir / f"unexpected-{index}"
                create(unexpected)
                with self.assertRaisesRegex(ValueError, "package changed"):
                    self.native.rollback_install(self.receipt)
                self.assertTrue(unexpected.exists() or unexpected.is_symlink())
                self.assertTrue((self.install_dir / "codex").is_symlink())
                remove(unexpected)
        self.native.rollback_install(self.receipt)

    def test_built_binary_with_wrong_version_is_rejected(self):
        self.make_plan()

        def run(argv, *, cwd=None, env=None, log_path=None):
            result = self.fake_build_run(argv, cwd=cwd, env=env, log_path=log_path)
            if argv[0] == str(self.build_root / "codex-rs" / "target" / "release" / "codex"):
                return subprocess.CompletedProcess(argv, 0, "codex-cli 10.153.4\n", "")
            return result

        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "_run", side_effect=run), \
                mock.patch.object(self.native, "_run_build", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "version mismatch"):
                self.native.apply_install(self.plan, self.receipt)
        self.assertFalse((self.install_dir / "codex").exists())
        self.assertFalse(self.receipt.exists())

    def test_corrupt_backup_blocks_rollback_without_overwrite(self):
        self.install_dir.mkdir()
        target = self.install_dir / "codex"
        target.write_bytes(b"old codex")
        self.make_plan()
        self.apply()
        receipt = json.loads(self.receipt.read_text())
        receipt["files"][0]["original_base64"] = "bm90IHRoZSBvbGQgY29kZXg="
        self.receipt.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "backup is corrupt"):
            self.native.rollback_install(self.receipt)
        self.assertEqual(target.read_bytes(), b"native fixture codex fixture_native_status_provider CUE_QUESTION_UI_V1 CUE_MANAGED_UPDATE_V1")

    def test_missing_prerequisites_are_reported_without_mutation(self):
        with mock.patch.object(self.native, "_which", return_value=None):
            result = self.native.inspect_prerequisites()
        self.assertFalse(result["supported"])
        self.assertEqual(result["missing"], ["git", "rustup-or-posix-sh"])
        with mock.patch.object(self.native, "_which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "no POSIX sh"):
                self.native.provision_toolchain(self.source, self.root / "scoped")

    def test_absent_rustup_uses_official_scoped_bootstrap_without_path_changes(self):
        scoped = self.root / "scoped-bootstrap"
        cargo = scoped / "toolchain" / "cargo"
        rustc = scoped / "toolchain" / "rustc"
        cargo.parent.mkdir(parents=True)
        cargo.write_bytes(b"cargo")
        rustc.write_bytes(b"rustc")
        shell = self.root / "sh"
        shell.write_bytes(b"fixture shell")
        shell.chmod(0o755)
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return b"#!/bin/sh\n# rustup fixture\n"

        def which(name):
            return str(shell) if name == "sh" else None

        def run(argv, *, cwd=None, env=None, log_path=None):
            calls.append((argv, env))
            if argv[0] == str(shell):
                installed = scoped / "cargo" / "bin" / "rustup"
                installed.parent.mkdir(parents=True)
                installed.write_bytes(b"rustup")
            output = ""
            if "which" in argv:
                output = str(cargo if argv[-1] == "cargo" else rustc) + "\n"
            return subprocess.CompletedProcess(argv, 0, output, "")

        with mock.patch.object(self.native, "_which", side_effect=which), \
                mock.patch.object(self.native.urllib.request, "urlopen", return_value=Response()), \
                mock.patch.object(self.native, "_run", side_effect=run), \
                mock.patch.object(self.native, "_run_build", side_effect=run):
            result = self.native.provision_toolchain(self.source, scoped)
        bootstrap_call = calls[0][0]
        self.assertEqual(bootstrap_call[0], str(shell))
        self.assertIn("--no-modify-path", bootstrap_call)
        self.assertEqual(result["bootstrap"]["url"], self.native.RUSTUP_BOOTSTRAP_URL)
        self.assertEqual(result["bootstrap"]["sha256"], self.native.sha256(b"#!/bin/sh\n# rustup fixture\n"))

    def test_toolchain_is_provisioned_in_scoped_homes(self):
        rustup = self.root / "rustup"
        rustup.write_bytes(b"fixture")
        rustup.chmod(0o755)
        scoped = self.root / "scoped"
        cargo = scoped / "toolchain" / "cargo"
        rustc = scoped / "toolchain" / "rustc"
        cargo.parent.mkdir(parents=True)
        cargo.write_bytes(b"cargo")
        rustc.write_bytes(b"rustc")
        calls = []

        def run(argv, *, cwd=None, env=None, log_path=None):
            calls.append((argv, env))
            output = ""
            if "which" in argv:
                output = str(cargo if argv[-1] == "cargo" else rustc) + "\n"
            return subprocess.CompletedProcess(argv, 0, output, "")

        with mock.patch.object(self.native, "_run", side_effect=run), \
                mock.patch.object(self.native, "_run_build", side_effect=run):
            result = self.native.provision_toolchain(self.source, scoped, rustup=str(rustup))
        self.assertEqual(result["channel"], "1.95.0")
        self.assertEqual(calls[0][0][1:4], ["toolchain", "install", "1.95.0"])
        self.assertEqual(calls[0][1]["CARGO_HOME"], str(scoped / "cargo"))
        self.assertEqual(calls[0][1]["RUSTUP_HOME"], str(scoped / "rustup"))

    def test_long_build_output_streams_to_a_persistent_log(self):
        log = self.root / "native-build.log"
        self.native._run_build(
            [sys.executable, "-c", "import sys; print('out', flush=True); print('err', file=sys.stderr)"],
            cwd=self.root, env=os.environ, log_path=log,
        )
        self.assertEqual(log.read_text().splitlines(), ["out", "err"])

    def test_long_build_failure_points_to_full_log(self):
        log = self.root / "native-build-failure.log"
        with self.assertRaisesRegex(RuntimeError, str(log)):
            self.native._run_build(
                [sys.executable, "-c", "raise SystemExit(7)"],
                cwd=self.root, env=os.environ, log_path=log,
            )
        self.assertTrue(log.is_file())

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_cancelled_build_stops_and_reaps_its_process_group(self):
        child_pid = self.root / "child.pid"
        child_ready = self.root / "child.ready"
        child_program = (
            "import pathlib,signal,sys,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(60)"
        )
        program = """import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]])
ready = pathlib.Path(sys.argv[2])
while not ready.exists():
    time.sleep(.01)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", program, str(child_pid), str(child_ready), child_program],
            start_new_session=True,
        )
        for _ in range(100):
            if child_pid.is_file():
                break
            __import__("time").sleep(0.01)
        self.assertTrue(child_pid.is_file())
        descendant = int(child_pid.read_text())
        self.native._stop_process_group(process, timeout=1)
        self.assertIsNotNone(process.poll())
        for _ in range(100):
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            __import__("time").sleep(0.01)
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant, 0)

    def test_cancelled_build_records_interruption_and_stops_group(self):
        process = mock.Mock()
        process.wait.side_effect = KeyboardInterrupt
        log = self.root / "cancelled.log"
        with mock.patch.object(self.native.subprocess, "Popen", return_value=process), \
                mock.patch.object(self.native, "_stop_process_group") as stop:
            with self.assertRaises(KeyboardInterrupt):
                self.native._run_build(
                    ["cargo", "build"], cwd=self.root, env=os.environ, log_path=log,
                )
        stop.assert_called_once_with(process)
        self.assertIn("build interrupted", log.read_text())

    def test_path_detection_matches_shell_empty_and_literal_tilde_entries(self):
        with mock.patch.dict(os.environ, {"PATH": os.pathsep + "~"}, clear=False):
            self.assertTrue(self.native._path_contains(Path.cwd().resolve()))
            self.assertFalse(self.native._path_contains(Path.home().resolve()))

    def test_acquisition_checks_out_and_verifies_pinned_commit(self):
        destination = self.root / "acquired"
        calls = []

        def run(argv, *, cwd=None, env=None):
            calls.append(argv)
            if "clone" in argv:
                destination.mkdir()
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(self.native, "_run", side_effect=run), \
                mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"):
            result = self.native.acquire_source(destination, git=str(self.git))
        self.assertEqual(result, destination)
        self.assertEqual(calls[0][1:4], ["clone", "--filter=blob:none", "--no-checkout"])
        self.assertIn(self.native.UPSTREAM_COMMIT, calls[1])

    def test_default_plan_defers_scoped_toolchain_provisioning_to_apply(self):
        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"):
            result = self.native.plan_install(
                self.source, self.patch, self.metadata, self.install_dir,
                self.plan, self.build_root,
            )
        self.assertTrue(result["toolchain"]["provision_on_apply"])
        self.assertEqual(result["toolchain"]["channel"], "1.95.0")
        with mock.patch.object(self.native, "_git_head", return_value=self.native.UPSTREAM_COMMIT), \
                mock.patch.object(self.native, "_require_clean_source"), \
                mock.patch.object(self.native, "provision_toolchain", return_value=self.toolchain) as provision, \
                mock.patch.object(self.native, "_run", side_effect=self.fake_build_run), \
                mock.patch.object(self.native, "_run_build", side_effect=self.fake_build_run), \
                mock.patch.object(self.native, "_fetch_ripgrep", side_effect=self.fake_fetch_rg):
            installed = self.native.apply_install(self.plan, self.receipt)
        self.assertEqual(installed["state"], "installed")
        provision.assert_called_once()

    def test_resolve_patch_bundle_prefers_package_then_source_layout(self):
        package_native = MODULE_PATH.parent / "native"
        source_native = MODULE_PATH.parents[1] / "native"
        with mock.patch.object(Path, "is_dir", autospec=True) as is_dir:
            is_dir.side_effect = lambda path: path == source_native
            with mock.patch.object(Path, "glob", autospec=True, return_value=[]):
                with self.assertRaises(FileNotFoundError):
                    self.native.resolve_patch_bundle()

    def test_low_level_plan_persists_bundled_assets_beyond_package_lifetime(self):
        captured = {}

        def plan_install(*args, **kwargs):
            captured["patch"] = Path(args[1])
            captured["metadata"] = Path(args[2])
            return {"state": "planned"}

        with mock.patch.object(
                self.native, "resolve_patch_bundle",
                return_value=(self.patch, self.metadata)), \
                mock.patch.object(self.native, "plan_install", side_effect=plan_install):
            result = self.native.main([
                "plan", str(self.source), str(self.install_dir), str(self.build_root),
                "--plan", str(self.plan),
            ])
        self.assertEqual(result, 0)
        expected = self.root / "plan-native-assets"
        self.assertEqual(captured["patch"].parent, expected)
        self.assertEqual(captured["metadata"].parent, expected)
        self.assertEqual(captured["patch"].read_bytes(), self.patch.read_bytes())
        self.assertEqual(captured["metadata"].read_bytes(), self.metadata.read_bytes())
        self.assertEqual((expected / self.question_patch.name).read_bytes(), self.question_patch.read_bytes())
        self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()

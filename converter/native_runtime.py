#!/usr/bin/env python3
"""Pinned source builder and reversible installer for native Codex.

This module is an integration backend.  It does not change shell startup files,
install wrappers, or select an installation directory for the caller.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import tomllib
from typing import Mapping
import sys
import urllib.request
import urllib.parse
import zipfile


SCHEMA_VERSION = 1
UPSTREAM_REPOSITORY = "https://github.com/openai/codex.git"
UPSTREAM_COMMIT = "3d2ee51ca2d5db578f328aa75e20aa22c0197c9a"
EXECUTABLE_NAME = "codex"
CODE_MODE_HOST_NAME = "codex-code-mode-host"
PACKAGE_METADATA_NAME = "codex-package.json"
PACKAGE_LAYOUT_VERSION = 1
QUESTION_FEATURE_MARKER = "CUE_QUESTION_UI_V1"
UPDATE_FEATURE_MARKER = "CUE_MANAGED_UPDATE_V1"
RUSTUP_BOOTSTRAP_URL = "https://sh.rustup.rs"
MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024
OFFICIAL_COMPANIONS = {'darwin-arm64': {'version': '0.153.4-darwin-arm64', 'url': 'https://registry.npmjs.org/@openai/codex/-/codex-0.153.4-darwin-arm64.tgz', 'integrity': 'sha512-B1qhN3fa1ay0R0wGziXqgwSkB5icpYChNKHhtBHff/0UtSTC7z+l8aTtvMlGjH3E8HEvY3+njIJelM9CAAoVWg=='}, 'darwin-x64': {'version': '0.153.4-darwin-x64', 'url': 'https://registry.npmjs.org/@openai/codex/-/codex-0.153.4-darwin-x64.tgz', 'integrity': 'sha512-vnSbbPzfoDZmmyzsxswsDDXQ06IVFBzkQU7/hroB3ji93Ok2utcsq8Psfk2tjF5r9mEx8RWFJhzuTGHG26/NDA=='}, 'linux-arm64': {'version': '0.153.4-linux-arm64', 'url': 'https://registry.npmjs.org/@openai/codex/-/codex-0.153.4-linux-arm64.tgz', 'integrity': 'sha512-QKdjYLYV4hXIuUQDP3P6F4NXuWFoKo9WUoV4nAREIx55kiUyi8UsYdsVobkeXir5n/maEQgYMCKLHVma4rNPiw=='}, 'linux-x64': {'version': '0.153.4-linux-x64', 'url': 'https://registry.npmjs.org/@openai/codex/-/codex-0.153.4-linux-x64.tgz', 'integrity': 'sha512-x1EcwBlY3AObM1VTUHNM2AzAJQsyreGdagpF+qFiYi/Oa30VBktvvG0C6tLtCzqW6hjZNWkGZQWmeVk7MuJKWg=='}}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _private_json(path: Path, value: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _replace_json(path: Path, value: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cue-native-receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run(argv: list[str], *, cwd: Path | None = None,
         env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise RuntimeError(f"Command failed ({result.returncode}): {argv[0]}: {detail}")
    return result


def _run_build(argv: list[str], *, cwd: Path, env: Mapping[str, str],
               log_path: Path) -> None:
    """Run the long native build without retaining unbounded process output."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("x", encoding="utf-8") as log:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(env),
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
            )
            try:
                return_code = process.wait()
            except KeyboardInterrupt:
                log.write("\n[cue native installer] build interrupted; stopping process group\n")
                log.flush()
                _stop_process_group(process)
                raise
    except FileExistsError as exc:
        raise ValueError(f"Native build log already exists: {log_path}") from exc
    if return_code:
        raise RuntimeError(
            f"Native Cargo build failed ({return_code}); full diagnostics: {log_path}"
        )


def _stop_process_group(process: subprocess.Popen, timeout: float = 5.0) -> None:
    """Stop a build and its descendants, then reap the direct child."""
    if os.name != "posix" and process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
    elif process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def _which(name: str) -> str | None:
    value = shutil.which(name)
    return str(Path(value).resolve()) if value else None


def _path_contains(directory: Path) -> bool:
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        try:
            # Shell PATH semantics: an empty entry is cwd and '~' is literal.
            if Path(entry or ".").resolve() == directory:
                return True
        except OSError:
            continue
    return False


def _git_head(source_root: Path, git: str) -> str:
    return _run([git, "-C", str(source_root), "rev-parse", "HEAD"]).stdout.strip()


def _require_clean_source(source_root: Path, git: str) -> None:
    dirty = _run(
        [git, "-C", str(source_root), "status", "--porcelain", "--untracked-files=no"]
    ).stdout.strip()
    if dirty:
        raise ValueError("Pinned Codex source has tracked modifications; use a clean checkout.")


def _file_record(path: Path) -> dict:
    if path.is_symlink():
        return {"kind": "symlink", "link": os.readlink(path)}
    if path.is_file():
        data = path.read_bytes()
        return {
            "kind": "file",
            "sha256": sha256(data),
            "mode": stat.S_IMODE(path.stat().st_mode),
        }
    if path.exists():
        raise ValueError(f"Expected a file, symlink, or absent target: {path}")
    return {"kind": "absent"}


def _directory_record(path: Path) -> dict:
    if path.is_symlink():
        return {"kind": "symlink", "link": os.readlink(path)}
    if path.is_dir():
        info = path.stat()
        return {
            "kind": "directory",
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": stat.S_IMODE(info.st_mode),
        }
    if path.exists():
        raise ValueError(f"Expected a directory or absent installation parent: {path}")
    return {"kind": "absent"}


def _toolchain_channel(source_root: Path) -> str:
    path = source_root / "codex-rs" / "rust-toolchain.toml"
    if not path.is_file():
        raise ValueError(f"Pinned source is missing toolchain metadata: {path}")
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    channel = value.get("toolchain", {}).get("channel")
    if not isinstance(channel, str) or not channel.strip():
        raise ValueError("Pinned source has no valid Rust toolchain channel.")
    return channel


def _build_contract(source_root: Path) -> dict:
    _, _, executable_suffix = _host_package_target()
    required = [EXECUTABLE_NAME, CODE_MODE_HOST_NAME]
    if sys.platform.startswith("linux"):
        required.append("bwrap")
    elif sys.platform == "win32":
        required.extend(("codex-command-runner", "codex-windows-sandbox-setup"))
    matches = {name: [] for name in required}
    cargo_root = source_root / "codex-rs"
    for manifest in sorted(cargo_root.glob("*/Cargo.toml")):
        value = tomllib.loads(manifest.read_text(encoding="utf-8"))
        package = value.get("package", {}).get("name")
        bins = value.get("bin", [])
        if isinstance(package, str):
            for name in required:
                if any(isinstance(row, dict) and row.get("name") == name for row in bins):
                    matches[name].append((package, manifest.relative_to(source_root).as_posix()))
    for name in required:
        if len(matches[name]) != 1:
            raise ValueError(
                f"Pinned source must declare exactly one {name!r} binary; found {len(matches[name])}."
            )
    workspace_manifest = tomllib.loads((cargo_root / "Cargo.toml").read_text(encoding="utf-8"))
    version = workspace_manifest.get("workspace", {}).get("package", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("Pinned source has no workspace package version.")
    artifacts = []
    package_paths = {
        EXECUTABLE_NAME: f"bin/{EXECUTABLE_NAME}{executable_suffix}",
        CODE_MODE_HOST_NAME: f"bin/{CODE_MODE_HOST_NAME}{executable_suffix}",
        "bwrap": "codex-resources/bwrap",
        "codex-command-runner": "codex-resources/codex-command-runner.exe",
        "codex-windows-sandbox-setup": "codex-resources/codex-windows-sandbox-setup.exe",
    }
    for name in required:
        package, manifest = matches[name][0]
        artifacts.append({
            "name": name,
            "package": package,
            "binary": name,
            "manifest": manifest,
            "output": f"codex-rs/target/release/{name}{executable_suffix}",
            "package_relative": package_paths[name],
        })
    primary = artifacts[0]
    return {
        "cargo_cwd": "codex-rs",
        "package": primary["package"],
        "binary": primary["binary"],
        "manifest": primary["manifest"],
        "output": primary["output"],
        "artifacts": artifacts,
        "version": version,
    }


def _host_package_target() -> tuple[str, str, str]:
    machine = platform.machine().lower()
    if sys.platform == "win32":
        raise RuntimeError(
            "Native package installation is not implemented for Windows; use the official Codex package."
        )
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(machine)
    systems = {
        "darwin": ("apple-darwin", "macos"),
        "linux": ("unknown-linux-gnu", "linux"),
    }
    if arch is None or sys.platform not in systems:
        raise RuntimeError(f"No pinned Codex runtime package is defined for {sys.platform}/{machine}.")
    triple_suffix, dotslash_os = systems[sys.platform]
    return f"{arch}-{triple_suffix}", f"{dotslash_os}-{arch}", ""


def _package_entry_record(path: Path) -> dict:
    if path.is_symlink() or path.is_file():
        return _file_record(path)
    if path.is_dir():
        return {"kind": "directory", "mode": stat.S_IMODE(path.stat().st_mode)}
    raise ValueError(f"Expected a runtime package entry: {path}")


def _ripgrep_contract(source_root: Path) -> dict:
    manifest_path = source_root / "scripts" / "codex_package" / "rg"
    if not manifest_path.is_file():
        raise ValueError(f"Pinned source is missing its ripgrep artifact manifest: {manifest_path}")
    text = manifest_path.read_text(encoding="utf-8")
    if text.startswith("#!"):
        text = "\n".join(text.splitlines()[1:])
    manifest = json.loads(text)
    target, dotslash_platform, suffix = _host_package_target()
    row = manifest.get("platforms", {}).get(dotslash_platform)
    if not isinstance(row, dict):
        raise ValueError(f"Pinned ripgrep manifest has no entry for {dotslash_platform!r}.")
    providers = row.get("providers")
    if not isinstance(providers, list) or len(providers) != 1 or not isinstance(providers[0], dict):
        raise ValueError("Pinned ripgrep manifest must declare exactly one artifact provider.")
    url = providers[0].get("url")
    digest = row.get("digest")
    if (row.get("hash") != "sha256" or not isinstance(digest, str) or len(digest) != 64 or
            not isinstance(url, str) or not url.startswith("https://github.com/BurntSushi/ripgrep/")):
        raise ValueError("Pinned ripgrep manifest has an unsupported source or digest.")
    return {
        "manifest": manifest_path.relative_to(source_root).as_posix(),
        "manifest_sha256": sha256(manifest_path.read_bytes()),
        "platform": dotslash_platform,
        "target": target,
        "name": "rg" + suffix,
        "size": int(row["size"]),
        "sha256": digest,
        "format": row["format"],
        "member": row["path"],
        "url": url,
    }


def _official_companion_contract(version: str) -> dict:
    target, _, _ = _host_package_target()
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    key = sys.platform + "-" + arch
    row = OFFICIAL_COMPANIONS[key]
    if row["version"] != version + "-" + key:
        raise ValueError("No pinned official companion matches the source version.")
    official_target = target.replace("linux-gnu", "linux-musl")
    return {**row, "package": "@openai/codex", "platform": key,
            "member": f"package/vendor/{official_target}/bin/{CODE_MODE_HOST_NAME}"}


def _fetch_official_companion(contract: Mapping, destination: Path) -> None:
    """Extract one companion from the integrity-pinned official npm release."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / "official-companion.tgz"
    digest = hashlib.sha512()
    total = 0
    try:
        with urllib.request.urlopen(contract["url"], timeout=60) as response, archive.open("xb") as out:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 512 * 1024 * 1024:
                    raise RuntimeError("Official companion archive exceeds the download bound.")
                digest.update(chunk)
                out.write(chunk)
        actual = "sha512-" + base64.b64encode(digest.digest()).decode("ascii")
        if actual != contract["integrity"]:
            raise ValueError("Official companion archive integrity mismatch.")
        with tarfile.open(archive, "r:gz") as package:
            manifest = package.getmember("package/package.json")
            member = package.getmember(contract["member"])
            if not manifest.isfile() or manifest.size > 1024 * 1024:
                raise ValueError("Official companion package metadata is invalid.")
            metadata = json.load(package.extractfile(manifest))
            if metadata.get("name") != contract["package"] or metadata.get("version") != contract["version"]:
                raise ValueError("Official companion package name or version mismatch.")
            if not member.isfile() or not 0 < member.size <= 512 * 1024 * 1024:
                raise ValueError("Official companion member is not a bounded regular file.")
            with package.extractfile(member) as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
        destination.chmod(0o755)
    finally:
        archive.unlink(missing_ok=True)


def inspect_prerequisites(source_root: Path | None = None) -> dict:
    """Return JSON-ready prerequisite state without changing the host."""
    tools = {name: _which(name) for name in ("git", "rustup", "cargo", "rustc", "sh")}
    bootstrap_supported = bool(tools["rustup"] or tools["sh"])
    result = {
        "supported": bool(tools["git"] and bootstrap_supported),
        "tools": tools,
        "missing": (["git"] if not tools["git"] else []) +
                   (["rustup-or-posix-sh"] if not bootstrap_supported else []),
        "rustup_bootstrap_available": bool(not tools["rustup"] and tools["sh"]),
    }
    if source_root is not None:
        source_root = Path(source_root).resolve()
        result["source_root"] = str(source_root)
        if tools["git"] and source_root.is_dir():
            result["source_commit"] = _git_head(source_root, tools["git"])
            result["source_pinned"] = result["source_commit"] == UPSTREAM_COMMIT
            result["toolchain_channel"] = _toolchain_channel(source_root)
            result["build"] = _build_contract(source_root)
    return result


def acquire_source(destination: Path, *, metadata_path: Path | None = None,
                   repository: str = UPSTREAM_REPOSITORY,
                   git: str | None = None) -> Path:
    """Acquire the official source and verify the exact pinned commit."""
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Source destination already exists: {destination}")
    git = str(Path(git).resolve()) if git else _which("git")
    if not git:
        raise RuntimeError("Git is required to acquire Codex source. Install Git, then retry.")
    if metadata_path is not None:
        metadata = _json_read(Path(metadata_path).expanduser().resolve())
        if (metadata.get("schema_version") != SCHEMA_VERSION or
                metadata.get("upstream_commit") != UPSTREAM_COMMIT):
            raise ValueError("Native patch metadata does not target the pinned Codex commit.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run([git, "clone", "--filter=blob:none", "--no-checkout", repository, str(destination)])
        _run([git, "-C", str(destination), "checkout", "--detach", UPSTREAM_COMMIT])
        actual = _git_head(destination, git)
        if actual != UPSTREAM_COMMIT:
            raise ValueError(f"Codex source drift: expected {UPSTREAM_COMMIT}, found {actual}.")
        _require_clean_source(destination, git)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise
    return destination


def provision_toolchain(source_root: Path, toolchain_root: Path, *,
                        rustup: str | None = None) -> dict:
    """Provision the source-declared Rust toolchain into caller-owned directories."""
    source_root = Path(source_root).resolve()
    toolchain_root = Path(toolchain_root).expanduser().resolve()
    rustup = str(Path(rustup).resolve()) if rustup else _which("rustup")
    channel = _toolchain_channel(source_root)
    cargo_home = toolchain_root / "cargo"
    rustup_home = toolchain_root / "rustup"
    cargo_home.mkdir(parents=True, exist_ok=True)
    rustup_home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"CARGO_HOME": str(cargo_home), "RUSTUP_HOME": str(rustup_home)})
    bootstrap = None
    logs = []
    if not rustup:
        shell = _which("sh")
        if not shell:
            raise RuntimeError(
                "Rustup is absent and this host has no POSIX sh for the official scoped bootstrap. "
                "Install rustup without modifying PATH, then retry."
            )
        bootstrap_path = toolchain_root / "rustup-init.sh"
        try:
            with urllib.request.urlopen(RUSTUP_BOOTSTRAP_URL, timeout=60) as response:
                data = response.read(MAX_BOOTSTRAP_BYTES + 1)
        except OSError as exc:
            raise RuntimeError(
                f"Could not download the official rustup bootstrap from {RUSTUP_BOOTSTRAP_URL}: {exc}"
            ) from exc
        if not data or len(data) > MAX_BOOTSTRAP_BYTES or b"rustup" not in data[:65536].lower():
            raise RuntimeError("Official rustup bootstrap response was empty, oversized, or invalid.")
        bootstrap_path.write_bytes(data)
        bootstrap_path.chmod(0o700)
        bootstrap = {"url": RUSTUP_BOOTSTRAP_URL, "sha256": sha256(data), "bytes": len(data)}
        bootstrap_log = toolchain_root / "rustup-bootstrap.log"
        print(json.dumps({"state": "bootstrapping-rustup", "log": str(bootstrap_log)}), flush=True)
        _run_build([
            shell, str(bootstrap_path), "-y", "--no-modify-path", "--profile", "minimal",
            "--default-toolchain", channel,
        ], cwd=toolchain_root, env=env, log_path=bootstrap_log)
        logs.append(str(bootstrap_log))
        rustup_path = cargo_home / "bin" / "rustup"
        if not rustup_path.is_file():
            raise RuntimeError("Official rustup bootstrap completed without a scoped rustup executable.")
        rustup = str(rustup_path)
    toolchain_log = toolchain_root / "rustup-toolchain.log"
    print(json.dumps({"state": "provisioning-rust-toolchain", "log": str(toolchain_log)}), flush=True)
    _run_build(
        [rustup, "toolchain", "install", channel, "--profile", "minimal", "--no-self-update"],
        cwd=toolchain_root, env=env, log_path=toolchain_log,
    )
    logs.append(str(toolchain_log))
    cargo = _run([rustup, "which", "--toolchain", channel, "cargo"], env=env).stdout.strip()
    rustc = _run([rustup, "which", "--toolchain", channel, "rustc"], env=env).stdout.strip()
    for name, path in (("cargo", cargo), ("rustc", rustc)):
        if not Path(path).is_file():
            raise RuntimeError(f"Rustup reported an unusable {name} executable: {path}")
    result = {
        "channel": channel,
        "cargo": cargo,
        "rustc": rustc,
        "cargo_home": str(cargo_home),
        "rustup_home": str(rustup_home),
        "logs": logs,
    }
    if bootstrap:
        result["bootstrap"] = bootstrap
    return result


def _patch_metadata(metadata_path: Path, patch_path: Path) -> dict:
    metadata = _json_read(metadata_path)
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported native patch metadata schema.")
    if metadata.get("upstream_commit") != UPSTREAM_COMMIT:
        raise ValueError("Native patch metadata does not target the pinned Codex commit.")
    if type(metadata.get("release_sequence")) is not int or metadata["release_sequence"] < 1:
        raise ValueError("Native release sequence must be a positive integer.")
    if not isinstance(metadata.get("release_id"), str) or not metadata["release_id"].strip():
        raise ValueError("Native release identity is missing.")
    declared_patch = metadata.get("patch_file")
    if declared_patch is not None and declared_patch != patch_path.name:
        raise ValueError("Native patch filename does not match its metadata.")
    actual = sha256(patch_path.read_bytes())
    if metadata.get("patch_sha256") != actual:
        raise ValueError("Native patch hash does not match its metadata.")
    rows = metadata.get("additional_patches", [])
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError("Native patch metadata must include the question renderer and managed updater patches.")
    names = {patch_path.name, metadata_path.name}
    for row, marker in zip(rows, (QUESTION_FEATURE_MARKER, UPDATE_FEATURE_MARKER)):
        if not isinstance(row, dict) or row.get("feature_marker") != marker:
            raise ValueError("Required native feature marker is missing from patch metadata: " + marker)
        name = row.get("patch_file")
        if (not isinstance(name, str) or Path(name).name != name or
                name in (".", "..") or name in names):
            raise ValueError("Unsafe additional native patch filename.")
        names.add(name)
        if sha256((metadata_path.parent / name).read_bytes()) != row.get("patch_sha256"):
            raise ValueError("Additional native patch hash does not match its metadata.")
    return metadata


def _patch_records(metadata_path: Path, patch_path: Path, metadata: dict) -> list[dict]:
    return [{"path": str(patch_path), "sha256": metadata["patch_sha256"],
             "feature_marker": metadata["feature_marker"]}] + [
        {"path": str(metadata_path.parent / row["patch_file"]),
         "sha256": row["patch_sha256"], "feature_marker": row["feature_marker"]}
        for row in metadata["additional_patches"]
    ]


def resolve_patch_bundle() -> tuple[Path, Path]:
    """Find the bundled native patch in an installed package or source tree."""
    module_dir = Path(__file__).resolve().parent
    roots = (module_dir / "native", module_dir.parent / "native")
    for root in roots:
        if not root.is_dir():
            continue
        candidates = []
        for metadata_path in sorted(root.glob("*.json")):
            try:
                metadata = _json_read(metadata_path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if (metadata.get("schema_version") != SCHEMA_VERSION or
                    metadata.get("upstream_commit") != UPSTREAM_COMMIT or
                    not isinstance(metadata.get("patch_sha256"), str)):
                continue
            declared = metadata.get("patch_file")
            if declared is not None:
                if not isinstance(declared, str) or Path(declared).name != declared:
                    raise ValueError(f"Unsafe patch filename in {metadata_path}")
                patch_path = root / declared
            else:
                adjacent = metadata_path.with_suffix(".patch")
                patches = sorted(root.glob("*.patch"))
                patch_path = adjacent if adjacent.is_file() else (patches[0] if len(patches) == 1 else Path())
            if patch_path.is_file():
                _patch_metadata(metadata_path, patch_path)
                candidates.append((patch_path, metadata_path))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ValueError(f"Multiple native patch bundles found in {root}")
    raise FileNotFoundError("No verified native Codex patch bundle is packaged with the converter.")


def _persist_patch_bundle(patch_path: Path, metadata_path: Path,
                          destination: Path) -> tuple[Path, Path]:
    """Copy a verified package bundle to storage that outlives zipapp extraction."""
    patch_path = Path(patch_path).resolve()
    metadata_path = Path(metadata_path).resolve()
    metadata = _patch_metadata(metadata_path, patch_path)
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Native asset directory already exists: {destination}")
    destination.mkdir(parents=True, mode=0o700)
    saved_patch = destination / patch_path.name
    saved_metadata = destination / metadata_path.name
    try:
        assets = [(patch_path, saved_patch), (metadata_path, saved_metadata)]
        assets.extend((metadata_path.parent / row["patch_file"], destination / row["patch_file"])
                      for row in metadata["additional_patches"])
        for source, target in assets:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(source.read_bytes())
        _patch_metadata(saved_metadata, saved_patch)
        return saved_patch, saved_metadata
    except BaseException:
        shutil.rmtree(destination)
        raise


def _validate_update_feed(value):
    if value is None:
        return
    parsed = urllib.parse.urlparse(value) if isinstance(value, str) else None
    if (not parsed or parsed.scheme != 'https' or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError('Update feed requires HTTPS without credentials or fragments')


def plan_install(source_root: Path, patch_path: Path, metadata_path: Path,
                 install_dir: Path, plan_path: Path, build_root: Path,
                 toolchain: Mapping[str, str] | None = None, *, git: str | None = None,
                 update_feed: str | None = None) -> dict:
    """Create an immutable, reviewable native build and installation plan."""
    _validate_update_feed(update_feed)
    plan_path = Path(plan_path).expanduser()
    if plan_path.exists() or plan_path.is_symlink():
        raise ValueError(f"Plan already exists: {plan_path}")
    source_root = Path(source_root).expanduser().resolve()
    patch_path = Path(patch_path).expanduser().resolve()
    metadata_path = Path(metadata_path).expanduser().resolve()
    build_root = Path(build_root).expanduser().resolve()
    install_dir = Path(install_dir).expanduser().resolve()
    target = install_dir / EXECUTABLE_NAME
    helper_target = install_dir / CODE_MODE_HOST_NAME
    git = str(Path(git).resolve()) if git else _which("git")
    if not git:
        raise RuntimeError("Git is required to verify and build Codex source. Install Git, then retry.")
    if not source_root.is_dir() or not patch_path.is_file() or not metadata_path.is_file():
        raise ValueError("Source root, patch, and patch metadata must already exist.")
    if build_root.exists() or build_root.is_symlink():
        raise ValueError(f"Build destination already exists: {build_root}")
    if build_root == source_root or build_root.is_relative_to(source_root):
        raise ValueError("Build destination must be outside the pinned source checkout.")
    actual_commit = _git_head(source_root, git)
    if actual_commit != UPSTREAM_COMMIT:
        raise ValueError(f"Codex source drift: expected {UPSTREAM_COMMIT}, found {actual_commit}.")
    _require_clean_source(source_root, git)
    patch_metadata = _patch_metadata(metadata_path, patch_path)
    feature_marker = patch_metadata.get("feature_marker")
    if (not isinstance(feature_marker, str) or not feature_marker.isascii() or
            not 4 <= len(feature_marker) <= 128):
        raise ValueError("Native patch metadata must declare a bounded ASCII feature_marker.")
    patches = _patch_records(metadata_path, patch_path, patch_metadata)
    build = _build_contract(source_root)
    ripgrep = _ripgrep_contract(source_root)
    channel = _toolchain_channel(source_root)
    if toolchain is None:
        toolchain_record = {
            "channel": channel,
            "toolchain_root": str(build_root.parent / (build_root.name + "-toolchain")),
            "provision_on_apply": True,
        }
    else:
        required_toolchain = ("cargo", "rustc", "cargo_home", "rustup_home", "channel")
        if any(not isinstance(toolchain.get(key), str) or not toolchain[key]
               for key in required_toolchain):
            raise ValueError("Toolchain record is incomplete.")
        if toolchain["channel"] != channel:
            raise ValueError("Provisioned Rust toolchain does not match the pinned source.")
        if not Path(toolchain["cargo"]).is_file() or not Path(toolchain["rustc"]).is_file():
            raise ValueError("Provisioned Rust toolchain executables are missing.")
        toolchain_record = dict(toolchain)
        toolchain_record["provision_on_apply"] = False
    runtime_dir = install_dir / (
        f".cue-codex-runtime-{build['version']}-{sha256(''.join(row['sha256'] for row in patches).encode())[:12]}"
    )
    if runtime_dir.exists() or runtime_dir.is_symlink():
        raise ValueError(f"Native runtime destination already exists: {runtime_dir}")
    runtime_files = [
        {
            "name": EXECUTABLE_NAME,
            "target": str(target),
            "before": _file_record(target),
            "package_relative": f"bin/{EXECUTABLE_NAME}",
        },
        {
            "name": CODE_MODE_HOST_NAME,
            "target": str(helper_target),
            "before": _file_record(helper_target),
            "package_relative": f"bin/{CODE_MODE_HOST_NAME}",
        },
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "repository": UPSTREAM_REPOSITORY,
        "upstream_commit": UPSTREAM_COMMIT,
        "source_root": str(source_root),
        "source_commit": actual_commit,
        "patches": patches,
        "patch_path": str(patch_path),
        "patch_sha256": patch_metadata["patch_sha256"],
        "metadata_path": str(metadata_path),
        "metadata_sha256": sha256(metadata_path.read_bytes()),
        "git": git,
        "toolchain": toolchain_record,
        "build": build,
        "ripgrep": ripgrep,
        "official_companion": _official_companion_contract(build["version"]),
        "build_root": str(build_root),
        "build_log": str(build_root.parent / (build_root.name + ".log")),
        "runtime_dir": str(runtime_dir),
        "runtime_dir_before": _directory_record(runtime_dir),
        "runtime_files": runtime_files,
        "target": str(target),
        "before": runtime_files[0]["before"],
        "target_parent": _directory_record(install_dir),
        "feature_marker": feature_marker,
        "install_dir_on_path": _path_contains(install_dir),
        "path_activation_required": not _path_contains(install_dir),
    }
    if update_feed is not None:
        result['update_feed'] = update_feed
    _private_json(plan_path, result)
    return result


def _validate_plan(plan: dict) -> None:
    _validate_update_feed(plan.get('update_feed'))
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported native installation plan schema.")
    if plan.get("upstream_commit") != UPSTREAM_COMMIT or plan.get("source_commit") != UPSTREAM_COMMIT:
        raise ValueError("Installation plan does not pin the supported Codex commit.")
    target = Path(plan.get("target", ""))
    if target.name != EXECUTABLE_NAME or not target.is_absolute():
        raise ValueError("Installation plan target must be the plain codex executable.")
    expected = _build_contract(Path(plan["source_root"]))
    if plan.get("build") != expected:
        raise ValueError("Installation build contract changed after review.")
    if plan.get("ripgrep") != _ripgrep_contract(Path(plan["source_root"])):
        raise ValueError("Pinned ripgrep artifact contract changed after review.")
    if plan.get("official_companion") != _official_companion_contract(expected["version"]):
        raise ValueError("Official companion contract changed after review.")
    runtime_dir = Path(plan.get("runtime_dir", ""))
    if not runtime_dir.is_absolute() or runtime_dir.parent != target.parent:
        raise ValueError("Installation runtime directory must be inside the command directory.")
    files = plan.get("runtime_files")
    expected_names = (EXECUTABLE_NAME, CODE_MODE_HOST_NAME)
    if not isinstance(files, list) or tuple(row.get("name") for row in files) != expected_names:
        raise ValueError("Installation plan does not contain the complete native runtime.")
    for row in files:
        row_target = Path(row.get("target", ""))
        if row_target != target.parent / row["name"] or not row_target.is_absolute():
            raise ValueError("Installation runtime target changed after review.")
        expected_relative = f"bin/{row['name']}"
        if row.get("package_relative") != expected_relative:
            raise ValueError("Installation package layout changed after review.")
    build_root = Path(plan.get("build_root", ""))
    expected_log = build_root.parent / (build_root.name + ".log")
    if Path(plan.get("build_log", "")) != expected_log:
        raise ValueError("Installation build log path changed after review.")


def _atomic_binary_install(source: Path, target: Path, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cue-native-codex-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source.read_bytes())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fetch_ripgrep(contract: Mapping, destination: Path) -> None:
    """Fetch and extract the exact ripgrep artifact declared by pinned source."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / "ripgrep.download"
    try:
        digest = hashlib.sha256()
        total = 0
        with urllib.request.urlopen(contract["url"], timeout=60) as response, archive.open("xb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > contract["size"]:
                    raise RuntimeError("Pinned ripgrep archive exceeded its declared size.")
                digest.update(chunk)
                out.write(chunk)
        if total != contract["size"] or digest.hexdigest() != contract["sha256"]:
            raise RuntimeError("Downloaded ripgrep archive does not match pinned source metadata.")
        if contract["format"] == "tar.gz":
            with tarfile.open(archive, "r:gz") as bundle:
                member = bundle.getmember(contract["member"])
                source = bundle.extractfile(member)
                if source is None or not member.isfile():
                    raise RuntimeError("Pinned ripgrep archive member is not a file.")
                with source, destination.open("xb") as out:
                    shutil.copyfileobj(source, out)
        elif contract["format"] == "zip":
            with zipfile.ZipFile(archive) as bundle, bundle.open(contract["member"]) as source, destination.open("xb") as out:
                shutil.copyfileobj(source, out)
        else:
            raise RuntimeError("Pinned ripgrep archive has an unsupported format.")
        destination.chmod(0o755)
    except (KeyError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise RuntimeError("Pinned ripgrep archive is missing its declared executable.") from exc
    finally:
        archive.unlink(missing_ok=True)


def _atomic_symlink_install(link: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".cue-native-link-{target.name}-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(link)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_record(path: Path, before: Mapping) -> dict:
    row = {"target": str(path), "before": dict(before)}
    if before["kind"] == "file":
        data = path.read_bytes()
        if sha256(data) != before["sha256"]:
            raise ValueError(f"Runtime target changed while its backup was created: {path}")
        row["original_base64"] = base64.b64encode(data).decode("ascii")
    elif before["kind"] == "symlink":
        referent = path.resolve()
        row["before_referent"] = {"path": str(referent), "record": _file_record(referent)}
        package = referent.parent.parent
        if path.name == EXECUTABLE_NAME and referent.parent.name == "bin" and (package / PACKAGE_METADATA_NAME).is_file():
            row["prior_package"] = {"path": str(package), "inventory": [
                {"relative": item.relative_to(package).as_posix(), "record": _package_entry_record(item)}
                for item in sorted(package.rglob("*"))]}
    return row


def _validate_backup(row: Mapping) -> None:
    referent = row.get("before_referent")
    if referent and _file_record(Path(referent["path"])) != referent["record"]:
        raise ValueError("Previous runtime package referent changed; refusing rollback.")
    prior = row.get("prior_package")
    if prior:
        package = Path(prior["path"])
        if not package.is_dir() or package.is_symlink():
            raise ValueError("Previous runtime package is missing; refusing rollback.")
        expected = {item["relative"]: item["record"] for item in prior["inventory"]}
        actual = {item.relative_to(package).as_posix(): _package_entry_record(item)
                  for item in package.rglob("*")}
        if actual != expected:
            raise ValueError("Previous runtime package changed; refusing rollback.")
    before = row["before"]
    if before["kind"] == "file":
        try:
            data = base64.b64decode(row["original_base64"], validate=True)
        except (KeyError, ValueError) as exc:
            raise ValueError("Runtime receipt backup is corrupt; refusing rollback.") from exc
        if sha256(data) != before["sha256"]:
            raise ValueError("Runtime receipt backup is corrupt; refusing rollback.")


def _restore_target(row: Mapping) -> None:
    target = Path(row["target"])
    before = row["before"]
    if before["kind"] == "absent":
        target.unlink(missing_ok=True)
    elif before["kind"] == "file":
        data = base64.b64decode(row["original_base64"], validate=True)
        _atomic_binary_install_bytes(data, target, before["mode"])
    elif before["kind"] == "symlink":
        target.unlink(missing_ok=True)
        _atomic_symlink_install(before["link"], target)
    else:
        raise ValueError("Runtime receipt contains an unsupported prior target kind.")


@contextlib.contextmanager
def _installation_lock(target: Path):
    lock = target.parent / ".cue-codex-update.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Update lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another managed update holds the installation lock") from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def apply_install(plan_path: Path, receipt_path: Path) -> dict:
    """Build and install a complete, reversible native Codex package."""
    plan_path = Path(plan_path).expanduser().resolve()
    receipt_path = Path(receipt_path).expanduser()
    if receipt_path.exists() or receipt_path.is_symlink():
        raise ValueError(f"Receipt already exists: {receipt_path}")
    plan = _json_read(plan_path)
    _validate_plan(plan)
    source_root = Path(plan["source_root"])
    patch_path = Path(plan["patch_path"])
    metadata_path = Path(plan["metadata_path"])
    build_root = Path(plan["build_root"])
    build_log = Path(plan["build_log"])
    target = Path(plan["target"])
    runtime_dir = Path(plan["runtime_dir"])
    runtime_rows = plan["runtime_files"]
    if any(receipt_path.resolve() == Path(row["target"]).resolve() for row in runtime_rows):
        raise ValueError("Receipt must not replace a native runtime installation target.")
    if receipt_path.resolve() == build_log.resolve():
        raise ValueError("Receipt must not replace the native build log.")
    if receipt_path.resolve().is_relative_to(build_root):
        raise ValueError("Receipt must be outside the disposable build checkout.")
    git = plan["git"]
    if not Path(git).is_file():
        raise RuntimeError("The reviewed Git executable is unavailable; create a new plan.")
    if build_root.exists() or build_root.is_symlink():
        raise ValueError(f"Build destination changed after review: {build_root}")
    if build_log.exists() or build_log.is_symlink():
        raise ValueError(f"Build log destination changed after review: {build_log}")
    if _directory_record(runtime_dir) != plan["runtime_dir_before"]:
        raise ValueError("Native runtime directory changed after review.")
    if _git_head(source_root, git) != UPSTREAM_COMMIT:
        raise ValueError("Pinned Codex source changed after review.")
    _require_clean_source(source_root, git)
    if sha256(metadata_path.read_bytes()) != plan["metadata_sha256"]:
        raise ValueError("Native patch metadata changed after review.")
    patch_metadata = _patch_metadata(metadata_path, patch_path)
    if plan.get("patches") != _patch_records(metadata_path, patch_path, patch_metadata):
        raise ValueError("Native patch set changed after review.")
    if plan.get("feature_marker") != patch_metadata.get("feature_marker"):
        raise ValueError("Native feature marker changed after review.")
    if sha256(patch_path.read_bytes()) != plan["patch_sha256"]:
        raise ValueError("Native patch changed after review.")
    for row in runtime_rows:
        if _file_record(Path(row["target"])) != row["before"]:
            raise ValueError(f"Native runtime target changed after review: {row['name']}")
    if _directory_record(target.parent) != plan["target_parent"]:
        raise ValueError("Codex installation directory changed after review.")
    toolchain = plan["toolchain"]
    if toolchain.get("provision_on_apply"):
        toolchain = provision_toolchain(source_root, Path(toolchain["toolchain_root"]))
        if toolchain["channel"] != _toolchain_channel(source_root):
            raise RuntimeError("Provisioned Rust toolchain does not match the reviewed source.")
    cargo = Path(toolchain["cargo"])
    rustc = Path(toolchain["rustc"])
    if not cargo.is_file() or not rustc.is_file():
        raise RuntimeError("The reviewed scoped Rust toolchain is unavailable; provision it again.")
    _run([git, "clone", "--local", "--no-checkout", str(source_root), str(build_root)])
    _run([git, "-C", str(build_root), "checkout", "--detach", UPSTREAM_COMMIT])
    if _git_head(build_root, git) != UPSTREAM_COMMIT:
        raise RuntimeError("Isolated build checkout did not preserve the pinned commit.")
    for patch in plan["patches"]:
        _run([git, "-C", str(build_root), "apply", "--check", patch["path"]])
        _run([git, "-C", str(build_root), "apply", patch["path"]])
    _run([git, "-C", str(build_root), "diff", "--check"])
    build = plan["build"]
    env = dict(os.environ)
    env.update({
        "CARGO_HOME": toolchain["cargo_home"],
        "RUSTUP_HOME": toolchain["rustup_home"],
        "CARGO_TARGET_DIR": str(build_root / "codex-rs" / "target"),
        "RUSTC": str(rustc),
        "PATH": str(rustc.parent) + os.pathsep + str(cargo.parent) + os.pathsep +
                os.environ.get("PATH", ""),
    })
    for inherited in ("CARGO_BUILD_TARGET", "RUSTC_WRAPPER", "RUSTFLAGS",
                      "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(inherited, None)
    print(json.dumps({"state": "building-native-codex", "log": str(build_log)}), flush=True)
    build_command = [str(cargo), "build", "--locked", "--release"]
    for artifact in build["artifacts"]:
        if artifact["name"] == CODE_MODE_HOST_NAME:
            continue
        build_command.extend(("--package", artifact["package"], "--bin", artifact["binary"]))
    _run_build(build_command, cwd=build_root / build["cargo_cwd"], env=env, log_path=build_log)
    built_files = {}
    for artifact in build["artifacts"]:
        built = build_root / artifact["output"]
        if artifact["name"] == CODE_MODE_HOST_NAME:
            _fetch_official_companion(plan["official_companion"], built)
        if not built.is_file() or built.is_symlink():
            raise RuntimeError(f"Cargo did not produce declared runtime executable: {artifact['name']}")
        built_files[artifact["name"]] = built
    built = built_files[EXECUTABLE_NAME]
    version_output = _run([str(built), "--version"]).stdout.strip()
    if build["version"] not in version_output.split():
        raise RuntimeError(
            f"Built Codex version mismatch: expected {build['version']!r}, got {version_output!r}."
        )
    built_bytes = built.read_bytes()
    for patch in plan["patches"]:
        if patch["feature_marker"].encode("ascii") not in built_bytes:
            raise RuntimeError(f"Built Codex does not contain required feature marker: {patch['feature_marker']}")
    if UPDATE_FEATURE_MARKER.encode("ascii") not in built_bytes:
        raise RuntimeError("Built Codex is missing managed updater feature marker: " + UPDATE_FEATURE_MARKER)
    helper_help = _run([str(built_files[CODE_MODE_HOST_NAME]), "--help"])
    if helper_help.returncode != 0:
        raise RuntimeError("Built code-mode host failed its launch sanity check.")
    rg_output = build_root / "runtime-assets" / plan["ripgrep"]["name"]
    _fetch_ripgrep(plan["ripgrep"], rg_output)
    _run([str(rg_output), "--version"])
    for row in runtime_rows:
        if _file_record(Path(row["target"])) != row["before"]:
            raise ValueError(f"Native runtime target changed while build was running: {row['name']}")
    if _directory_record(target.parent) != plan["target_parent"]:
        raise ValueError("Codex installation directory changed while the native build was running.")
    if _directory_record(runtime_dir) != plan["runtime_dir_before"]:
        raise ValueError("Native runtime directory changed while the native build was running.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with _installation_lock(target):
        for row in runtime_rows:
            if _file_record(Path(row["target"])) != row["before"]:
                raise ValueError("Runtime target changed before locked installation commit.")
        if _directory_record(runtime_dir) != plan["runtime_dir_before"]:
            raise ValueError("Runtime directory changed before locked installation commit.")
        package_stage = Path(tempfile.mkdtemp(prefix=".cue-codex-runtime-stage-", dir=target.parent))
        package_records = []
        try:
            (package_stage / "bin").mkdir()
            (package_stage / "codex-resources").mkdir()
            (package_stage / "codex-path").mkdir()
            for artifact in build["artifacts"]:
                destination = package_stage / artifact["package_relative"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                _atomic_binary_install(built_files[artifact["name"]], destination, 0o755)
            _atomic_binary_install(rg_output, package_stage / "codex-path" / plan["ripgrep"]["name"], 0o755)
            metadata = {
                "layoutVersion": PACKAGE_LAYOUT_VERSION,
                "version": build["version"],
                "target": plan["ripgrep"]["target"],
                "variant": "codex",
                "entrypoint": f"bin/{EXECUTABLE_NAME}",
                "resourcesDir": "codex-resources",
                "pathDir": "codex-path",
            }
            try:
                from .managed_update import stage_manager
            except ImportError:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "cue_managed_update", Path(__file__).with_name("managed_update.py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                stage_manager = module.stage_manager
            release_id = patch_metadata["release_id"] + "-" + metadata["target"]
            stage_manager(package_stage, metadata, target=target,
                          runtime_source=Path(__file__), release_id=release_id,
                          sequence=patch_metadata["release_sequence"],
                          feed_url=plan.get('update_feed'))
            (package_stage / PACKAGE_METADATA_NAME).write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            for path in sorted(package_stage.rglob("*")):
                package_records.append({
                    "relative": path.relative_to(package_stage).as_posix(),
                    "record": _package_entry_record(path),
                })
            backups = [_backup_record(Path(row["target"]), row["before"]) for row in runtime_rows]
            installed_rows = []
            for row in runtime_rows:
                link = os.path.relpath(runtime_dir / row["package_relative"], Path(row["target"]).parent)
                installed_rows.append({**row, "installed": {"kind": "symlink", "link": link}})
        except BaseException:
            shutil.rmtree(package_stage, ignore_errors=True)
            raise
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "state": "applying",
            "plan": str(plan_path),
            "target": str(target),
            "before": plan["before"],
            "installed": installed_rows[0]["installed"],
            "files": [{**installed, **backup} for installed, backup in zip(installed_rows, backups)],
            "runtime_dir": str(runtime_dir),
            "runtime_package": package_records,
            "official_companion": plan["official_companion"],
            "target_parent": plan["target_parent"],
            "build_root": str(build_root),
            "build_log": str(build_log),
            "toolchain": toolchain,
            "install_dir_on_path": plan["install_dir_on_path"],
            "path_activation_required": plan["path_activation_required"],
        }
        if backups[0].get("original_base64"):
            receipt["original_base64"] = backups[0]["original_base64"]
        _private_json(receipt_path, receipt)
        changed = []
        try:
            os.replace(package_stage, runtime_dir)
            for row in reversed(receipt["files"]):
                _atomic_symlink_install(row["installed"]["link"], Path(row["target"]))
                changed.append(row)
                if _file_record(Path(row["target"])) != row["installed"]:
                    raise RuntimeError(f"Installed runtime link is invalid: {row['name']}")
            receipt["state"] = "installed"
            _replace_json(receipt_path, receipt)
        except Exception as exc:
            rollback_error = None
            try:
                for row in reversed(changed):
                    _restore_target(row)
                if runtime_dir.is_dir() and not runtime_dir.is_symlink():
                    shutil.rmtree(runtime_dir)
                if plan["target_parent"]["kind"] == "absent":
                    try:
                        target.parent.rmdir()
                    except OSError:
                        pass
            except Exception as restore_exc:
                rollback_error = str(restore_exc)
            receipt["state"] = "failed-rolled-back" if rollback_error is None else "failed"
            receipt["error"] = str(exc)
            if rollback_error is not None:
                receipt["rollback_error"] = rollback_error
            _replace_json(receipt_path, receipt)
            raise
        return receipt


def rollback_install(receipt_path: Path) -> dict:
    receipt = _json_read(Path(receipt_path).expanduser().resolve())
    files = receipt.get("files")
    target = Path(files[0]["target"] if isinstance(files, list) and files else receipt["target"])
    if not target.is_absolute() or target.name != EXECUTABLE_NAME:
        raise ValueError("Invalid native rollback target.")
    with _installation_lock(target):
        return _rollback_install_unlocked(receipt_path)


def _rollback_install_unlocked(receipt_path: Path) -> dict:
    """Restore the exact prior executable after verifying no later edits exist."""
    receipt_path = Path(receipt_path).expanduser().resolve()
    receipt = _json_read(receipt_path)
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported native installation receipt schema.")
    if receipt.get("state") == "rolled-back":
        raise ValueError("Native installation is already rolled back.")
    if receipt.get("state") == "failed-rolled-back":
        raise ValueError("Failed native installation was already rolled back.")
    if receipt.get("state") not in ("applying", "failed", "installed"):
        raise ValueError("Native installation receipt has an invalid state.")
    if "files" in receipt:
        rows = receipt["files"]
        runtime_dir = Path(receipt["runtime_dir"])
        if not isinstance(rows, list) or not rows:
            raise ValueError("Native installation receipt has no runtime files.")
        for row in rows:
            _validate_backup(row)
            current = _file_record(Path(row["target"]))
            if current not in (row["installed"], row["before"]):
                raise ValueError(f"Native runtime changed after installation: {row['name']}")
        if runtime_dir.exists():
            if runtime_dir.is_symlink() or not runtime_dir.is_dir():
                raise ValueError("Native runtime package changed after installation.")
            expected = {row["relative"]: row["record"] for row in receipt["runtime_package"]}
            actual_paths = sorted(p.relative_to(runtime_dir).as_posix() for p in runtime_dir.rglob("*"))
            if actual_paths != sorted(expected) or any(
                    _package_entry_record(runtime_dir / relative) != record
                    for relative, record in expected.items()):
                raise ValueError("Native runtime package changed after installation.")
        if "previous_package" in receipt:
            previous = Path(receipt["previous_package"])
            prior_rows = receipt.get("previous_package_inventory")
            if (not previous.is_absolute() or previous.is_symlink() or not previous.is_dir()
                    or not isinstance(prior_rows, list) or not prior_rows):
                raise ValueError("Previous runtime package is missing or has no verified inventory.")
            expected_prior = {row["relative"]: row["record"] for row in prior_rows}
            actual_prior = {path.relative_to(previous).as_posix(): _package_entry_record(path)
                            for path in previous.rglob("*")}
            if actual_prior != expected_prior:
                raise ValueError("Previous runtime package changed; refusing rollback.")
        for row in rows:
            if _file_record(Path(row["target"])) != row["before"]:
                _restore_target(row)
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir)
        if receipt.get("target_parent", {}).get("kind") == "absent":
            try:
                runtime_dir.parent.rmdir()
            except OSError:
                pass
        for row in rows:
            if _file_record(Path(row["target"])) != row["before"]:
                raise RuntimeError(f"Rollback did not restore native runtime target: {row['name']}")
        receipt["state"] = "rolled-back"
        _replace_json(receipt_path, receipt)
        return receipt
    target = Path(receipt["target"])
    if target.name != EXECUTABLE_NAME or not target.is_absolute():
        raise ValueError("Receipt target is not the plain codex executable.")
    current = _file_record(target)
    if current not in (receipt["installed"], receipt["before"]):
        raise ValueError("Codex changed after installation; refusing to overwrite it.")
    if current != receipt["before"]:
        before = receipt["before"]
        if before["kind"] == "absent":
            target.unlink()
        elif before["kind"] == "file":
            original = base64.b64decode(receipt["original_base64"], validate=True)
            if sha256(original) != before["sha256"]:
                raise ValueError("Receipt backup is corrupt; refusing rollback.")
            _atomic_binary_install_bytes(original, target, before["mode"])
        elif before["kind"] == "symlink":
            target.unlink()
            target.symlink_to(before["link"])
        else:
            raise ValueError("Receipt contains an unsupported prior target kind.")
    if _file_record(target) != receipt["before"]:
        raise RuntimeError("Rollback did not restore the prior Codex target.")
    receipt["state"] = "rolled-back"
    _replace_json(receipt_path, receipt)
    return receipt


def _atomic_binary_install_bytes(data: bytes, target: Path, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cue-native-rollback-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _emit(value: Mapping) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    """Low-level CLI. The top-level setup command orchestrates these APIs."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--source", type=Path)
    acquire = sub.add_parser("acquire")
    acquire.add_argument("destination", type=Path)
    acquire.add_argument("--repository", default=UPSTREAM_REPOSITORY)
    acquire.add_argument("--metadata", type=Path)
    provision = sub.add_parser("provision")
    provision.add_argument("source", type=Path)
    provision.add_argument("toolchain_root", type=Path)
    provision.add_argument("--record", type=Path)
    plan = sub.add_parser("plan")
    plan.add_argument("source", type=Path)
    plan.add_argument("install_dir", type=Path)
    plan.add_argument("build_root", type=Path)
    plan.add_argument("--plan", type=Path, required=True)
    plan.add_argument("--toolchain-record", type=Path)
    plan.add_argument("--patch", type=Path)
    plan.add_argument("--metadata", type=Path)
    plan.add_argument("--update-feed", help="Explicit HTTPS compatible-release feed for this installation")
    apply = sub.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--receipt", type=Path, required=True)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "inspect":
            result = inspect_prerequisites(args.source)
        elif args.action == "acquire":
            metadata_path = args.metadata
            if metadata_path is None:
                _, metadata_path = resolve_patch_bundle()
            source = acquire_source(
                args.destination, metadata_path=metadata_path, repository=args.repository
            )
            result = {
                "repository": args.repository,
                "source_root": str(source),
                "upstream_commit": UPSTREAM_COMMIT,
            }
        elif args.action == "provision":
            result = provision_toolchain(args.source, args.toolchain_root)
            if args.record:
                _private_json(args.record, result)
        elif args.action == "plan":
            if bool(args.patch) != bool(args.metadata):
                raise ValueError("Provide both --patch and --metadata, or neither for the bundled patch.")
            if args.patch:
                patch_path, metadata_path = args.patch, args.metadata
            else:
                patch_path, metadata_path = resolve_patch_bundle()
                asset_dir = args.plan.expanduser().resolve().parent / (
                    args.plan.stem + "-native-assets"
                )
                patch_path, metadata_path = _persist_patch_bundle(
                    patch_path, metadata_path, asset_dir
                )
            result = plan_install(
                args.source, patch_path, metadata_path, args.install_dir,
                args.plan, args.build_root,
                _json_read(args.toolchain_record) if args.toolchain_record else None,
                update_feed=args.update_feed,
            )
        elif args.action == "apply":
            result = apply_install(args.plan, args.receipt)
        else:
            result = rollback_install(args.receipt)
        _emit(result)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

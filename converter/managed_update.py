#!/usr/bin/env python3
"""Update a converter-managed Codex package from an explicit compatible release feed."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import secrets
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

MANAGER = "claude-codex-converter"
REQUIRED_MARKERS = ("native-status-provider-v1", "CUE_QUESTION_UI_V1", "CUE_MANAGED_UPDATE_V1")
MAX_DESCRIPTOR = 1024 * 1024
MAX_ARCHIVE = 1024 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_feed_url(value):
    parsed = urllib.parse.urlparse(value) if isinstance(value, str) else None
    if (not parsed or parsed.scheme != 'https' or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError('Update feed requires HTTPS without credentials or fragments')
    return value


def read_json(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def confined(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Invalid package-relative path")
    part = PurePosixPath(relative)
    if part.is_absolute() or any(x in (".", "..") for x in relative.split("/")):
        raise ValueError("Package path escapes its root")
    path = root.joinpath(*part.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Package symlink escapes its root")
    return path


def package_info(installation):
    installation = Path(installation).absolute()
    root = installation.parent
    meta = read_json(installation)
    cue = meta.get("cueUpdate", {})
    if meta.get("manager") != MANAGER or not isinstance(cue, dict) or cue.get("schemaVersion") != 1:
        raise ValueError("Installation is not converter-managed")
    for key, hash_key in (("program", "sha256"), ("runtimeFile", "runtimeSha256")):
        path = confined(root, cue.get(key))
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != cue.get(hash_key):
            raise ValueError("Managed updater asset changed: " + key)
    sequence = cue.get("sequence")
    if type(sequence) is not int or sequence < 1 or not isinstance(cue.get("releaseId"), str):
        raise ValueError("Invalid installed release identity")
    if 'feedUrl' in cue:
        validate_feed_url(cue['feedUrl'])
    return root, meta, cue


def load_descriptor(source, root, cue):
    if source is None and 'feedUrl' in cue:
        source = validate_feed_url(cue['feedUrl'])
    if source is None:
        path = confined(root, cue["descriptorFile"])
        data = path.read_bytes()
        if digest(data) != cue["descriptorSha256"]:
            raise ValueError("Bundled compatible-release descriptor changed")
        origin = str(path)
    elif urllib.parse.urlparse(str(source)).scheme:
        validate_feed_url(str(source))
        with urllib.request.urlopen(str(source), timeout=30) as response:
            validate_feed_url(response.geturl())
            data = response.read(MAX_DESCRIPTOR + 1)
        origin = str(source)
    else:
        origin = str(Path(source).expanduser().resolve())
        with open(origin, "rb") as stream:
            data = stream.read(MAX_DESCRIPTOR + 1)
    if len(data) > MAX_DESCRIPTOR:
        raise ValueError("Release descriptor exceeds size limit")
    descriptor = json.loads(data)
    if not isinstance(descriptor, dict) or descriptor.get("schemaVersion") != 1 or descriptor.get("manager") != MANAGER:
        raise ValueError("Unrecognized compatible-release descriptor")
    if not isinstance(descriptor.get("releases"), list):
        raise ValueError("Release list is missing")
    return descriptor, origin


def selection(installation, source=None):
    root, meta, cue = package_info(installation)
    descriptor, origin = load_descriptor(source, root, cue)
    compatible = []
    for row in descriptor["releases"]:
        if not isinstance(row, dict):
            raise ValueError("Invalid release record")
        if row.get("target") != meta["target"]:
            continue
        if type(row.get("sequence")) is not int or row["sequence"] < 1:
            raise ValueError("Invalid release sequence")
        compatibility = row.get("compatibility", {})
        if compatibility.get("validated") is not True or not set(REQUIRED_MARKERS).issubset(compatibility.get("markers", [])):
            continue
        if not isinstance(row.get("version"), str) or not isinstance(row.get("releaseId"), str):
            raise ValueError("Release identity is missing")
        compatible.append(row)
    ordered = sorted(compatible, key=lambda row: (row["sequence"], row["releaseId"]))
    if len({row["sequence"] for row in ordered}) != len(ordered):
        raise ValueError("Ambiguous compatible release sequence")
    latest = ordered[-1] if ordered else None
    state = "no_compatible_update" if latest is None else "current"
    if latest and latest["sequence"] > cue["sequence"]:
        state = "update_available"
    if latest and latest["sequence"] == cue["sequence"] and latest["releaseId"] != cue["releaseId"]:
        raise ValueError("Conflicting release identity at installed sequence")
    result = {"state": state, "version": latest["version"] if latest else meta["version"],
              "releaseId": latest["releaseId"] if latest else cue["releaseId"],
              "installedReleaseId": cue["releaseId"]}
    return result, latest, origin


def _runtime(root, cue):
    path = confined(root, cue["runtimeFile"])
    spec = importlib.util.spec_from_file_location("cue_managed_native_runtime", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _archive(row, origin, destination):
    spec = row.get("archive", {})
    location, expected = spec.get("location"), spec.get("sha256")
    if not isinstance(location, str) or not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("Release archive needs a location and SHA-256")
    if urllib.parse.urlparse(origin).scheme == "https":
        location = urllib.parse.urljoin(origin, location)
    elif not urllib.parse.urlparse(location).scheme:
        location = str(Path(origin).parent / location)
    if urllib.parse.urlparse(location).scheme:
        if urllib.parse.urlparse(location).scheme != "https":
            raise ValueError("Release archives require HTTPS")
        stream = urllib.request.urlopen(location, timeout=60)
        if urllib.parse.urlparse(stream.geturl()).scheme != "https":
            stream.close()
            raise ValueError("Release archive redirected outside HTTPS")
    else:
        stream = open(location, "rb")
    count, hashed = 0, hashlib.sha256()
    with stream, destination.open("xb") as out:
        while chunk := stream.read(1024 * 1024):
            count += len(chunk)
            if count > MAX_ARCHIVE:
                raise ValueError("Release archive exceeds size limit")
            hashed.update(chunk)
            out.write(chunk)
    if hashed.hexdigest() != expected:
        raise ValueError("Release archive integrity mismatch")


def _extract(archive, stage):
    seen, total = set(), 0
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = member.filename.rstrip("/")
            destination = confined(stage, name)
            if name in seen:
                raise ValueError("Duplicate archive member")
            seen.add(name)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not stat.S_ISREG(mode) and not stat.S_ISDIR(mode)):
                raise ValueError("Archive contains a non-regular member")
            total += member.file_size
            if total > MAX_ARCHIVE:
                raise ValueError("Extracted package exceeds size limit")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)
            destination.chmod(0o755 if mode & 0o111 else 0o644)


def update(installation, source=None, *, adopt_feed=False):
    adopted_url = validate_feed_url(str(source)) if adopt_feed else None
    result, release, origin = selection(installation, source)
    if result["state"] != "update_available":
        if adopt_feed:
            result['feedAdopted'] = False
        return result
    root, meta, cue = package_info(installation)
    runtime = _runtime(root, cue)
    target = Path(cue["installTarget"])
    if not target.is_absolute() or target.name != "codex":
        raise ValueError("Invalid managed installation target")
    with installation_lock(target):
        result = _apply_update(result, release, origin, root, meta, cue, runtime, target,
                               adopted_url=adopted_url)
    if adopt_feed:
        result['feedAdopted'] = True
    return result


@contextlib.contextmanager
def installation_lock(target):
    """Advisory lock releases on process exit; the inert lock file stays in place."""
    lock = Path(target).parent / ".cue-codex-update.lock"
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


def rollback(installation, receipt):
    root, meta, cue = package_info(installation)
    return _runtime(root, cue).rollback_install(Path(receipt))


def _apply_update(result, release, origin, root, meta, cue, runtime, target, *, adopted_url=None):
    names = ["codex", "codex-code-mode-host"]
    if meta["target"].endswith("linux-musl") or meta["target"].endswith("linux-gnu"):
        names.append("codex-linux-sandbox")
    rows = []
    for name in names:
        path = target.parent/name
        if not path.is_symlink() or path.resolve() != (root/"bin"/name).resolve():
            raise ValueError("Managed target drift: " + name)
        rows.append({"name": name, "target": str(path), "before": runtime._file_record(path)})
    inventory = {str(path.relative_to(root)): runtime._package_entry_record(path) for path in root.rglob("*")}
    parent_before = runtime._directory_record(target.parent)
    with tempfile.TemporaryDirectory(prefix="cue-compatible-update-") as work:
        archive = Path(work)/"release.zip"
        _archive(release, origin, archive)
        stage = Path(tempfile.mkdtemp(prefix=".cue-update-stage-", dir=target.parent))
        moved = False
        try:
            _extract(archive, stage)
            new_root, new_meta, new_cue = package_info(stage/"codex-package.json")
            if new_meta["version"] != release["version"] or new_meta["target"] != meta["target"] or new_cue["releaseId"] != release["releaseId"] or new_cue["sequence"] != release["sequence"]:
                raise ValueError("Candidate package differs from release descriptor")
            if new_cue["installTarget"] != cue["installTarget"]:
                # Target relocation belongs to this installation, not a distributor's path.
                new_cue["installTarget"] = cue["installTarget"]
            # An explicit one-off source cannot change the installation's feed policy.
            if adopted_url is not None:
                new_cue['feedUrl'] = adopted_url
            elif 'feedUrl' in cue:
                new_cue['feedUrl'] = cue['feedUrl']
            else:
                new_cue.pop('feedUrl', None)
            (stage/"codex-package.json").write_text(json.dumps(new_meta, indent=2)+"\n")
            for name in names:
                path = stage/"bin"/name
                if not path.is_file() or path.is_symlink():
                    raise ValueError("Incomplete candidate package: " + name)
            rg = stage/"codex-path/rg"
            if not rg.is_file() or rg.is_symlink():
                raise ValueError("Candidate ripgrep is missing")
            cli = stage/"bin/codex"
            data = cli.read_bytes()
            if any(marker.encode() not in data for marker in REQUIRED_MARKERS):
                raise ValueError("Candidate is missing a required native patch")
            if release["version"] not in runtime._run([str(cli), "--version"]).stdout.split():
                raise ValueError("Candidate executable version differs")
            runtime._run([str(stage/"bin/codex-code-mode-host"), "--help"])
            runtime._run([str(rg), "--version"])
            actual = {str(path.relative_to(root)): runtime._package_entry_record(path) for path in root.rglob("*")}
            if actual != inventory or runtime._directory_record(target.parent) != parent_before:
                raise ValueError("Installed package changed while preparing update")
            if any(runtime._file_record(Path(row["target"])) != row["before"] for row in rows):
                raise ValueError("Installation targets changed while preparing update")
            destination = target.parent/(".cue-codex-runtime-update-"+digest(release["releaseId"].encode())[:16])
            if destination.exists() or destination.is_symlink():
                raise ValueError("Update destination already exists")
            receipt_path = target.parent/(destination.name+"-receipt-"+secrets.token_hex(12)+".json")
            records = [{"relative":str(path.relative_to(stage)),"record":runtime._package_entry_record(path)} for path in sorted(stage.rglob("*"))]
            files = [{**row, **runtime._backup_record(Path(row["target"]),row["before"]), "installed":{"kind":"symlink","link":os.path.relpath(destination/"bin"/row["name"],target.parent)}} for row in rows]
            receipt = {"schema_version":runtime.SCHEMA_VERSION,"state":"applying","files":files,"runtime_dir":str(destination),"runtime_package":records,"target_parent":parent_before,"previous_package":str(root),"previous_package_inventory":[{"relative": key, "record": value} for key, value in sorted(inventory.items())],"release":release["releaseId"]}
            runtime._private_json(receipt_path,receipt)
            changed=[]
            try:
                os.replace(stage,destination)
                moved=True
                for row in reversed(files):
                    runtime._atomic_symlink_install(row["installed"]["link"],Path(row["target"]))
                    changed.append(row)
                    if runtime._file_record(Path(row["target"])) != row["installed"]:
                        raise RuntimeError("Installed target verification failed")
                receipt["state"]="installed"
                runtime._replace_json(receipt_path,receipt)
            except BaseException as original:
                recovery_errors = []
                for row in reversed(changed):
                    try:
                        runtime._restore_target(row)
                    except BaseException as recovery:
                        recovery_errors.append(str(recovery))
                if moved and not recovery_errors:
                    try:
                        shutil.rmtree(destination)
                    except BaseException as recovery:
                        recovery_errors.append(str(recovery))
                receipt["state"] = "failed" if recovery_errors else "failed-rolled-back"
                receipt["error"] = str(original)
                if recovery_errors:
                    receipt["rollback_errors"] = recovery_errors
                runtime._replace_json(receipt_path,receipt)
                if recovery_errors:
                    raise RuntimeError(str(original) + "; rollback incomplete: " + "; ".join(recovery_errors)) from original
                raise
            return {**result,"state":"installed","receipt":str(receipt_path),"installation":str(destination/"codex-package.json")}
        finally:
            if not moved and stage.exists():
                shutil.rmtree(stage)


def stage_manager(package_stage, metadata, *, target, runtime_source, release_id, sequence=1, feed_url=None):
    if feed_url is not None:
        validate_feed_url(feed_url)
    """Bundle a durable updater and an offline compatible-release descriptor."""
    resources = package_stage/"codex-resources"
    resources.mkdir(exist_ok=True)
    program = resources/"cue-update"
    source = Path(__file__).read_bytes()
    program.write_bytes(source)
    program.chmod(0o755)
    runtime_file = resources/"native_runtime.py"
    runtime_file.write_bytes(Path(runtime_source).read_bytes())
    descriptor = {"schemaVersion":1,"manager":MANAGER,"releases":[{"releaseId":release_id,"sequence":sequence,"version":metadata["version"],"target":metadata["target"],"compatibility":{"validated":True,"markers":list(REQUIRED_MARKERS)}}]}
    descriptor_file=resources/"compatible-releases.json"
    descriptor_file.write_text(json.dumps(descriptor,indent=2)+"\n")
    metadata["manager"]=MANAGER
    metadata["cueUpdate"]={"schemaVersion":1,"program":"codex-resources/cue-update","sha256":digest(source),"runtimeFile":"codex-resources/native_runtime.py","runtimeSha256":digest(runtime_file.read_bytes()),"descriptorFile":"codex-resources/compatible-releases.json","descriptorSha256":digest(descriptor_file.read_bytes()),"releaseId":release_id,"sequence":sequence,"installTarget":str(target)}
    if feed_url is not None:
        metadata['cueUpdate']['feedUrl'] = feed_url


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("check","update","rollback"))
    parser.add_argument("--installation",type=Path,required=True)
    parser.add_argument("--source",help="Explicit compatible-release descriptor file or HTTPS URL")
    parser.add_argument("--receipt",type=Path)
    parser.add_argument("--adopt-feed",action="store_true",help="Explicitly adopt the HTTPS --source as the default feed only after a successful package update")
    args=parser.parse_args(argv)
    try:
        if args.adopt_feed and args.command != 'update':
            raise ValueError('--adopt-feed is only valid for update with an explicit HTTPS --source')
        if args.command=="check":
            result=selection(args.installation,args.source)[0]
        elif args.command=="update":
            result=update(args.installation,args.source,adopt_feed=args.adopt_feed)
        else:
            root,meta,cue=package_info(args.installation)
            if not args.receipt: raise ValueError("Rollback requires --receipt")
            result=rollback(args.installation,args.receipt)
        print(json.dumps(result,sort_keys=True))
        return 0
    except (ValueError,OSError,RuntimeError,KeyError,zipfile.BadZipFile) as exc:
        print(json.dumps({"state":"error","error":str(exc)}),file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())

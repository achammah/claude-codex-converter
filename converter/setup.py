"""Convert a project and provision its compatible native Codex runtime."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from . import install
from . import doctor
from .claude_to_codex import Converter


def runtime_directory(explicit=None):
    """Choose a real executable destination already reachable as plain codex."""
    search = [Path(p or '.').resolve() for p in os.get_exec_path()]
    current = shutil.which('codex')
    current_directory = Path(current).absolute().parent.resolve() if current else None
    current_index = search.index(current_directory) if current_directory in search else len(search)
    if explicit:
        chosen = Path(explicit).expanduser().resolve()
        if chosen not in search:
            raise ValueError('Runtime directory is not on PATH; add it to PATH before setup: ' + str(chosen))
        if search.index(chosen) > current_index:
            raise ValueError('An earlier PATH entry would still launch the existing codex: ' + str(current_directory))
        if not chosen.is_dir() or not os.access(chosen, os.W_OK | os.X_OK):
            raise ValueError('Runtime directory is not writable and searchable: ' + str(chosen))
        return chosen
    if current_directory and os.access(current_directory, os.W_OK | os.X_OK):
        return current_directory
    for directory in search[:current_index + 1]:
        if directory.is_dir() and os.access(directory, os.W_OK | os.X_OK):
            return directory
    raise ValueError('No writable executable directory exists on PATH. Supply --runtime-dir pointing to an on-PATH directory you can install into.')


def native_assets():
    from . import native_runtime
    patch, metadata = native_runtime.resolve_patch_bundle()
    return metadata, patch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--runtime-dir', type=Path, help='Directory already on PATH for the actual codex executable')
    parser.add_argument('--work-dir', type=Path, required=True, help='New persistent directory for source, build, plans and rollback receipts')
    parser.add_argument('--source-tree', type=Path, help='Existing clean pinned Codex source checkout; otherwise fetched automatically')
    parser.add_argument('--plan-only', action='store_true', help='Prepare reviewable plans without applying project or binary changes')
    parser.add_argument('--global-settings', type=Path)
    parser.add_argument('--include-user-resources', action='store_true')
    parser.add_argument('--include-external-hooks', action='store_true')
    parser.add_argument('--model-map', action='append', default=[])
    parser.add_argument('--legacy-state-root', action='append', default=[])
    args = parser.parse_args(argv)
    try:
        from . import native_runtime
        metadata, patch = native_assets()
        target = args.project.expanduser().resolve()
        if target.name == '.claude':
            target = target.parent
        if not target.is_dir():
            raise ValueError('Project directory does not exist.')
        work = args.work_dir.expanduser().resolve()
        if work.exists() and any(work.iterdir()):
            raise ValueError('Work directory must be new or empty; retain previous receipts for rollback.')
        destination = runtime_directory(args.runtime_dir)
        work.mkdir(parents=True, exist_ok=True)
        work.chmod(0o700)
        # Zipapp extraction is temporary; build plans must retain their inputs
        # after this process exits, including in --plan-only mode.
        assets = work / 'native'
        patch, metadata = native_runtime._persist_patch_bundle(patch, metadata, assets)
        stage = work / 'project-stage'
        conversion = argparse.Namespace(source=target, output=stage,
            global_settings=args.global_settings, include_user_resources=args.include_user_resources,
            model_map=args.model_map, include_external_hooks=args.include_external_hooks,
            legacy_state_root=args.legacy_state_root, strict=False, native_status=True)
        result = Converter(conversion).run()
        if result:
            return result
        project_plan = work / 'project-plan.json'
        install.plan(stage, target, project_plan)
        # The backend owns acquisition, pinned source validation, scoped toolchain
        # provisioning and executable replacement. Never execute a shell alias.
        source = args.source_tree.expanduser().resolve() if args.source_tree else native_runtime.acquire_source(
            metadata_path=metadata, destination=work / 'codex-source')
        runtime_plan = work / 'runtime-plan.json'
        native_runtime.plan_install(source_root=source, patch_path=patch,
            metadata_path=metadata, install_dir=destination, plan_path=runtime_plan,
            build_root=work / 'codex-build')
        if args.plan_only:
            print(json.dumps({'state': 'planned', 'project_plan': str(project_plan),
                              'runtime_plan': str(runtime_plan)}))
            return 0
        runtime_receipt = work / 'runtime-receipt.json'
        project_receipt = work / 'project-receipt.json'
        try:
            native_runtime.apply_install(runtime_plan, runtime_receipt)
            install.apply(project_plan, project_receipt)
        except (ValueError, OSError, RuntimeError, KeyboardInterrupt) as original:
            failures = []
            # Receipts are written before mutation, including partial failures.
            # Restore the project first, then its runtime. Keep all diagnostics.
            for receipt, restore in ((project_receipt, install.rollback),
                                     (runtime_receipt, native_runtime.rollback_install)):
                if receipt.exists():
                    try:
                        restore(receipt)
                    except (ValueError, OSError, RuntimeError) as recovery:
                        failures.append(str(receipt) + ': ' + str(recovery))
            if failures:
                raise RuntimeError(str(original) + '; rollback incomplete: ' + '; '.join(failures)) from original
            raise
        diagnostics_path = work / 'project-diagnostics.json'
        diagnostics = doctor.diagnose(target)
        native_runtime._private_json(diagnostics_path, diagnostics)
        print(json.dumps({'state': 'installed', 'command': 'codex', 'project': str(target),
                          'runtime': str(destination / 'codex'), 'receipts': str(work),
                          'diagnostics': str(diagnostics_path), 'findings': diagnostics['findings'],
                          'launch': 'Open a fresh terminal, enter the project directory, and run codex.',
                          'trust': 'Normal Codex project and hook trust still apply.',
                          'verification': 'Installation is not a live UI proof; launch codex in the project.'}))
        return 0
    except KeyboardInterrupt:
        print('Setup interrupted; retained plans and recovery receipts.', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        print('Setup failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

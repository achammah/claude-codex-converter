"""Unified public command line, also used by the self-contained zip application."""
import sys

try:
    from .version import VERSION
except ImportError:
    from version import VERSION


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('--help', '-h'):
        print(f'Claude–Codex Converter {VERSION}\n\n'
              'Usage: claude-codex-converter COMMAND [arguments]\n\n'
              '  convert   Preserve, translate and audit a Claude setup\n'
              '  reverse   Preserve, translate and audit a Codex setup for Claude\n'
              '  conversation Convert conversation files in either direction\n'
              '  setup     Convert and install the compatible native Codex runtime\n'
              '  runtime   Plan/build/install/rollback a patched native Codex binary\n'
              '  update    Check/apply a compatible managed runtime update or rollback\n'
              '  install   Plan/apply/rollback generated setup files\n'
              '  native    Detect/import/reconcile using Codex native importer\n\n'
              '  doctor    Inspect target setup and optional native MCP auth metadata\n\n'
              '  status    Read session status or explicitly run the source status command\n\n'
              '  questions Run the standalone AskUserQuestion MCP stdio server\n\n'
              '  host      Stage another host view of the shared neutral setup\n\n'
              'Run COMMAND --help for details. Setup conversion does not import chats. Staging never executes source hooks.')
        return 0
    action = sys.argv.pop(1)
    if action == 'convert':
        from .claude_to_codex import main as run
    elif action == 'reverse':
        from .codex_to_claude import main as run
    elif action == 'conversation':
        from .conversations import main as run
    elif action == 'setup':
        from .setup import main as run
    elif action == 'update':
        from .managed_update import main as run
    elif action == 'runtime':
        from .native_runtime import main as run
    elif action == 'install':
        from .install import main as run
    elif action == 'native':
        from .native_import import main as run
    elif action == 'questions':
        from .ask_user_question import main as run
    elif action == 'host':
        from .host_adapter import main as run
    elif action == 'doctor':
        from .doctor import main as run
    elif action == 'status':
        from .status_line import main as run
    else:
        print('Unknown command: ' + action, file=sys.stderr)
        return 1
    return run()

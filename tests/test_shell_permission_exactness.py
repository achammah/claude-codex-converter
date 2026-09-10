"""Pin conservative authority until Claude's quoted-star rule grammar is verified.

The permission-pattern grammar must not be inferred from shell argv quoting.
Official source: https://code.claude.com/docs/en/permissions#bash
"""
import importlib.util
import os
from pathlib import Path
import unittest


class ShellPermissionExactnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(os.environ.get('CUE_PROTOCOL_TEST_PATH',
                    str(Path(__file__).resolve().parents[1] / 'converter/protocol.py')))
        spec = importlib.util.spec_from_file_location('exact_protocol', path)
        cls.protocol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.protocol)

    def test_exact_command_never_grants_trailing_arguments(self):
        rule = ['Bash(cue-probe status)']
        self.assertTrue(self.protocol.permitted_exact('cue-probe status', rule))
        for command in ['cue-probe status extra', 'cue-probe status --write',
                        'cue-probe status; another-command', 'cue-probe status\nanother-command']:
            with self.subTest(command=command):
                self.assertFalse(self.protocol.permitted_exact(command, rule))

    def test_quoted_and_escaped_stars_remain_unresolved_not_new_grants(self):
        for argument in ["'*'", '"*"', r'\*', "'prefix*suffix'"]:
            command = 'cue-probe '+argument
            with self.subTest(argument=argument):
                self.assertFalse(self.protocol.permitted_exact(command, ['Bash('+command+')']))
                self.assertFalse(self.protocol.permitted_exact(command+' extra', ['Bash('+command+')']))

    def test_general_wildcards_never_become_native_prefix_authority(self):
        for pattern, command in [('cue-probe *', 'cue-probe --write'),
                                 ('cue-* status', 'cue-other status'),
                                 ('* status', 'arbitrary status')]:
            with self.subTest(pattern=pattern):
                self.assertFalse(self.protocol.permitted_exact(command, ['Bash('+pattern+')']))

    def test_explicit_legacy_prefix_remains_segment_bounded(self):
        rule = ['Bash(cue-probe:*)']
        self.assertTrue(self.protocol.permitted_exact('cue-probe status', rule))
        for command in ['cue-probe-other status', 'cue-probe status && another',
                        'cue-probe status | another', 'cue-probe $(another)',
                        'cue-probe status > output']:
            with self.subTest(command=command):
                self.assertFalse(self.protocol.permitted_exact(command, rule))


if __name__ == '__main__':
    unittest.main()

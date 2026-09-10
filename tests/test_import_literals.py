"""Literal Markdown examples must never become active instruction imports."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / 'converter/claude_to_codex.py'
spec = importlib.util.spec_from_file_location('import_literal_converter', MODULE_PATH)
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


class ImportLiteralTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cue-import-literal-')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.source = self.project / 'CLAUDE.md'
        self.source.write_text('Synthetic source')
        (self.project / 'literal.md').write_text('MUST_NOT_EXPAND')
        (self.project / 'active.md').write_text('ACTIVE_EXPANDED')
        self.findings = []
        self.subject = converter.Converter.__new__(converter.Converter)
        self.subject.project = self.project
        self.subject.output = self.project / 'out'
        self.subject.args = SimpleNamespace(include_user_resources=False)
        self.subject.finding = lambda *args, **kwargs: self.findings.append((args, kwargs))

    def check_literal(self, literal):
        content = literal + '\nRead @active.md.\n'
        result = self.subject.imported_instructions(content, self.source)
        self.assertIn(literal, result)
        self.assertNotIn('MUST_NOT_EXPAND', result)
        self.assertIn('ACTIVE_EXPANDED', result)
        self.assertEqual([row[1]['imported_path'] for row in self.findings], ['active.md'])
        archives = list((self.subject.output / '.cue-source-archive/imported').rglob('*.md'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), b'ACTIVE_EXPANDED')

    def test_shorter_backtick_fence_inside_long_fence_is_literal(self):
        self.check_literal('````markdown\n```\n@literal.md\n```\n````\n')

    def test_mismatched_fence_marker_does_not_close_literal(self):
        self.check_literal('~~~markdown\n```\n@literal.md\n```\n~~~\n')

    def test_apparent_fence_close_with_suffix_is_literal(self):
        self.check_literal('```markdown\n```not-a-close\n@literal.md\n```\n')

    def test_shorter_backtick_inside_inline_code_is_literal(self):
        self.check_literal('Example: ``this ` @literal.md is literal``.')

    def test_inline_code_span_across_newline_is_literal(self):
        self.check_literal('Example: ``first line\n@literal.md\nlast line``.')


if __name__ == '__main__':
    unittest.main()

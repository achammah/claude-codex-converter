"""Operand classification only: these tests never execute their shell strings."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'converter'))
from bash_file_views import classify_bash_file_views


class BashFileViews(unittest.TestCase):
    def classify(self, command):
        return classify_bash_file_views(command, '/fixture')

    def pairs(self, result):
        return [(v['access'], v['path']) for v in result['views']]

    def test_all_cat_operands_and_quoted_literals(self):
        result = self.classify("cat -n -- 'one file' two 'literal*' 'a>b'")
        self.assertTrue(result['complete'])
        self.assertEqual(self.pairs(result), [('read', '/fixture/'+p) for p in ['one file', 'two', 'literal*', 'a>b']])

    def test_head_tail_counts_never_become_paths(self):
        for cmd in ['head -n 5 a b', 'head -c10 a b', 'tail -n +5 a b', 'tail --bytes=10 a b']:
            with self.subTest(cmd=cmd):
                result = self.classify(cmd)
                self.assertTrue(result['complete'])
                self.assertEqual(self.pairs(result), [('read', '/fixture/a'), ('read', '/fixture/b')])

    def test_rg_patterns_options_and_all_paths(self):
        result = self.classify("rg -n -e pattern -g '*.py' -f patterns --ignore-file=ignore a b")
        self.assertEqual(self.pairs(result), [('read', '/fixture/'+p) for p in ['patterns', 'ignore', 'a', 'b']])
        self.assertFalse(result['complete'])  # Recursive/implicit inputs are not certified.

    def test_rg_attached_option_and_dash_paths(self):
        result = self.classify('rg -epattern -fpatterns -- -file other')
        self.assertEqual(self.pairs(result), [('read', '/fixture/'+p) for p in ['patterns', '-file', 'other']])

    def test_redirections_and_descriptor_adjacency(self):
        result = self.classify('cat 2 > out 2>> err < input')
        self.assertTrue(result['complete'])
        self.assertEqual(self.pairs(result), [('edit', '/fixture/out'), ('edit', '/fixture/err'),
                                              ('read', '/fixture/input'), ('read', '/fixture/2')])

    def test_readwrite_redirect(self):
        self.assertEqual(self.pairs(self.classify(': <> file')), [('edit', '/fixture/file'), ('read', '/fixture/file')])

    def test_quoted_numeric_operand_is_not_descriptor(self):
        for cmd in ["cat '2'>out", 'cat \\2>out']:
            self.assertEqual(self.pairs(self.classify(cmd)), [('edit', '/fixture/out'), ('read', '/fixture/2')])

    def test_invalid_option_equals_are_unresolved(self):
        for cmd in ['cat -n=bad file', 'head -n5=bad file']:
            self.assertFalse(self.classify(cmd)['complete'])

    def test_descriptor_dup_and_null_are_not_files(self):
        result = self.classify('printf hi 2>&1 <&3 >&- >/dev/null')
        self.assertTrue(result['complete'])
        self.assertEqual(result['views'], [])

    def test_heredoc_and_nested_syntax_are_unresolved_not_fake_reads(self):
        for cmd in ['cat <<EOF\ncat secret\nEOF', 'cat <<<"cat secret"', 'cat $(printf file)',
                    '(cd other; cat file)', 'cat <(printf x)']:
            with self.subTest(cmd=cmd):
                result = self.classify(cmd)
                self.assertFalse(result['complete'])
                self.assertEqual(result['views'], [])

    def test_cd_and_pipeline_cwd(self):
        result = self.classify('cd ./sub && cat a b')
        self.assertTrue(result['complete'])
        self.assertEqual(self.pairs(result), [('read', '/fixture/sub/a'), ('read', '/fixture/sub/b')])
        for cmd in ['cd sub; cat a', 'cd sub | cat a', 'cd - && cat a']:
            self.assertFalse(self.classify(cmd)['complete'])

    def test_uncertain_cwd_does_not_invent_following_paths(self):
        for cmd in ['cd "$DIR" && cat a', 'cd sub && cat a', 'custom_function; cat a',
                    'echo x | cd ./sub && cat a']:
            with self.subTest(cmd=cmd):
                result = self.classify(cmd)
                self.assertFalse(result['complete'])
                self.assertEqual(result['views'], [])

    def test_missing_pipeline_commands_are_unresolved(self):
        for cmd in ['cat a &&', '| cat a', 'cat a |', 'cat a && ; cat b']:
            self.assertFalse(self.classify(cmd)['complete'])

    def test_dynamic_words_and_unknown_flags_are_not_certified(self):
        for cmd in ['cat "$FILE"', 'cat *.txt', 'cat ~/file', 'head --unknown value file',
                    'rg --pre script pattern file', 'timeout 5 cat file', 'python3 script.py']:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.classify(cmd)['complete'])

    def test_supported_wrapper_and_stdin(self):
        result = self.classify('command cat -- - a')
        self.assertTrue(result['complete'])
        self.assertEqual(self.pairs(result), [('read', '/fixture/a')])

    def test_unknown_command_still_reports_literal_redirection(self):
        result = self.classify('custom-tool > target')
        self.assertFalse(result['complete'])
        self.assertEqual(self.pairs(result), [('edit', '/fixture/target')])

    def test_malformed_syntax_and_missing_values(self):
        for cmd in ['cat "unterminated', 'cat trailing\\', 'cat >', 'head -n', 'rg -f']:
            self.assertFalse(self.classify(cmd)['complete'])

    def test_source_spans_preserve_quoted_text(self):
        cmd = "cat 'a b' c"
        result = self.classify(cmd)
        self.assertEqual([cmd[s:e] for s, e in [v['source_span'] for v in result['views']]], ["'a b'", 'c'])

    def test_comments_and_escaped_operators(self):
        result = self.classify('cat a\\>b # > fake\ncat c')
        self.assertTrue(result['complete'])
        self.assertEqual(self.pairs(result), [('read', '/fixture/a>b'), ('read', '/fixture/c')])

    def test_pure_input_is_unchanged_and_deterministic(self):
        command = 'cat a b > out'
        self.assertEqual(self.classify(command), self.classify(command))
        self.assertEqual(command, 'cat a b > out')


if __name__ == '__main__':
    unittest.main()

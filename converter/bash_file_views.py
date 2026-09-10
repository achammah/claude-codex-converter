"""Conservative, non-executing Bash file operands for permission checks.

This is not a shell interpreter or an authorization engine. Keep the original
Bash event. Incomplete results cannot establish that a command has no access.
Paths are lexical; the caller must also apply its canonical/symlink checks.
"""
import os
import re


_OPERATORS = re.compile(r'&&|\|\||;;|<<-|<<<|<<|>>|<>|>\||>&|<&|&>>|&>|\|&|[;|&()<>\n]')


def _tokens(command):
    result, errors = [], []
    i = 0
    while i < len(command):
        if command[i] in ' \t\r':
            i += 1
            continue
        if command[i] == '#':
            end = command.find('\n', i)
            i = len(command) if end < 0 else end
            continue
        operator = _OPERATORS.match(command, i)
        if operator:
            result.append({'text': operator[0], 'start': i, 'end': operator.end(), 'operator': True})
            i = operator.end()
            continue
        start, value, dynamic = i, '', False
        while i < len(command) and command[i] not in ' \t\r' and not _OPERATORS.match(command, i):
            char = command[i]
            if char in "'\"":
                quote = char
                i += 1
                while i < len(command) and command[i] != quote:
                    if quote == '"' and command[i] == '\\' and i + 1 < len(command):
                        if command[i + 1] in '$`"\\\n':
                            i += 1
                            if command[i] != '\n':
                                value += command[i]
                            i += 1
                            continue
                    if quote == '"' and command[i] in '$`':
                        dynamic = True
                    value += command[i]
                    i += 1
                if i == len(command):
                    errors.append({'reason': 'unclosed quote', 'source_span': [start, i]})
                    break
                i += 1
            elif char == '\\':
                i += 1
                if i == len(command):
                    errors.append({'reason': 'trailing escape', 'source_span': [start, i]})
                    break
                if command[i] != '\n':
                    value += command[i]
                i += 1
            else:
                dynamic |= char in '$`*?[~{}'
                value += char
                i += 1
        result.append({'text': value, 'start': start, 'end': i, 'dynamic': dynamic})
    return result, errors


def classify_bash_file_views(command, cwd):
    """Return file views plus explicit unresolved coverage; never run expansions.

    Each span indexes the original command string. `complete` covers only this
    recognized grammar, not aliases, shell functions, runtime races or OS access.
    """
    if not isinstance(command, str) or not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ValueError('command must be text and cwd must be absolute')
    tokens, unresolved = _tokens(command)
    views = []

    def unknown(reason, token):
        unresolved.append({'reason': reason, 'source_span': [token['start'], token['end']]})

    # Do not mistake heredoc body text or nested shell syntax for top-level acts.
    if any(t.get('operator') and t['text'] in ('<<', '<<-', '<<<', '(', ')', ';;', '&', '||', '|&') for t in tokens):
        return {'views': [], 'unresolved': unresolved + [{'reason': 'unsupported control flow or heredoc',
                'source_span': [0, len(command)]}], 'complete': False}
    if unresolved:
        return {'views': [], 'unresolved': unresolved, 'complete': False}
    current_cwd, cwd_known = os.path.normpath(cwd), True
    segments, segment = [], []
    for token in tokens:
        if token.get('operator') and token['text'] in (';', '&&', '|', '\n'):
            segments.append((segment, token['text']))
            segment = []
        else:
            segment.append(token)
    segments.append((segment, None))
    for index, (segment, connector) in enumerate(segments):
        if not segment:
            if connector in ('&&', '|') or (index and segments[index - 1][1] in ('&&', '|')):
                unresolved.append({'reason': 'missing command at control operator', 'source_span': [0, len(command)]})
            continue

        def file_view(token, access):
            if token.get('dynamic') or not cwd_known:
                unknown('dynamic path or unresolved working directory', token)
                return
            value = token['text']
            if not value:
                unknown('empty path operand', token)
                return
            views.append({'access': access, 'path': os.path.normpath(os.path.join(current_cwd, value)),
                          'cwd': current_cwd, 'command_index': index,
                          'source_span': [token['start'], token['end']]})

        words, j = [], 0
        while j < len(segment):
            token = segment[j]
            if not token.get('operator'):
                words.append(token)
                j += 1
                continue
            op = token['text']
            if (words and words[-1]['text'].isdigit() and words[-1]['end'] == token['start']
                    and command[words[-1]['start']:words[-1]['end']] == words[-1]['text']):
                words.pop()  # Attached descriptor, not an operand: 2>file.
            if j + 1 == len(segment) or segment[j + 1].get('operator'):
                unknown('redirect without a literal destination', token)
                j += 1
                continue
            target = segment[j + 1]
            if op in ('>&', '<&'):
                if target.get('dynamic') or not re.fullmatch(r'\d+-?|-', target['text']):
                    unknown('unsupported descriptor redirect', target)
            elif op in ('>', '>>', '>|', '&>', '&>>', '<', '<>'):
                if target['text'] != '/dev/null' or target.get('dynamic'):
                    file_view(target, 'read' if op == '<' else 'edit')
                    if op == '<>':
                        file_view(target, 'read')
            else:
                unknown('unsupported operator', token)
            j += 2
        if not words:
            continue
        if any(t.get('dynamic') for t in words):
            unknown('dynamic shell word; operand boundaries are not certified', words[0])
            cwd_known = False  # An expansion may name cd or a shell function.
            continue
        name = words[0]['text']
        args = words[1:]
        if name in ('command', 'builtin', 'noglob') and args and not args[0]['text'].startswith('-'):
            name, args = args[0]['text'], args[1:]
        if name == 'cd':
            if (len(args) == 1 and args[0]['text'].startswith(('/', './', '../')) and cwd_known
                    and not (index and segments[index - 1][1] == '|')):
                current_cwd = os.path.normpath(os.path.join(current_cwd, args[0]['text']))
                if connector != '&&':
                    cwd_known = False
                    unknown('cd success or pipeline scope is unresolved', words[0])
            else:
                cwd_known = False
                unknown('unsupported cd form, CDPATH resolution or pipeline scope', words[0])
            continue
        if name in ('printf', 'echo', ':', 'true', 'false', 'pwd'):
            continue
        if name not in ('cat', 'head', 'tail', 'rg'):
            unknown('unrecognized executable or wrapper', words[0])
            cwd_known = False  # Unknown shell functions can change the parent cwd.
            continue
        after_options, pattern_seen = False, False
        j = 0
        while j < len(args):
            token, value = args[j], args[j]['text']
            if value == '--' and not after_options:
                after_options = True
                j += 1
                continue
            if not after_options and value.startswith('-') and value != '-':
                flag, equal, attached = value.partition('=')
                if name == 'cat' and not equal and re.fullmatch(r'-[AbBenEstTuv]+', flag):
                    j += 1
                    continue
                if name in ('head', 'tail'):
                    if not equal and re.fullmatch(r'-[ncb][+-]?\d+|-[0-9]+|-[qvfFr]+', flag):
                        j += 1
                        continue
                    if flag in ('-n', '-c', '-b', '--lines', '--bytes'):
                        if equal:
                            good = bool(re.fullmatch(r'[+-]?\d+', attached))
                        else:
                            j += 1
                            good = j < len(args) and bool(re.fullmatch(r'[+-]?\d+', args[j]['text']))
                        if not good:
                            unknown('invalid count option', token)
                        j += 1
                        continue
                if name == 'rg':
                    values = {'-e', '--regexp', '-f', '--file', '--ignore-file', '-g', '--glob', '--iglob',
                              '-t', '--type', '-T', '--type-not', '-m', '--max-count', '-A', '-B', '-C',
                              '--context', '--max-depth', '-j', '--threads', '-r', '--replace'}
                    short = value[:2]
                    if flag in values or (short in values and len(value) > 2 and not value.startswith('--')):
                        flag = flag if flag in values else short
                        if equal or (flag == short and len(value) > 2):
                            operand = dict(token, text=attached if equal else value[2:])
                        else:
                            j += 1
                            if j == len(args):
                                unknown('missing option value', token)
                                break
                            operand = args[j]
                        if flag in ('-f', '--file', '--ignore-file') and operand['text'] != '-':
                            file_view(operand, 'read')
                        if flag in ('-e', '--regexp', '-f', '--file'):
                            pattern_seen = True
                        j += 1
                        continue
                    if re.fullmatch(r'-[nNiIvVwlLqshuUzaFxo]+', value) or value in ('--hidden', '--no-ignore', '--fixed-strings', '--files', '--files-with-matches', '--files-without-match'):
                        if value == '--files':
                            pattern_seen = True
                        j += 1
                        continue
                unknown('unknown option; remaining operand classification is incomplete', token)
                break
            if name == 'rg' and not pattern_seen:
                pattern_seen = True
            elif value != '-':
                file_view(token, 'read')
            j += 1
        if name == 'rg':
            unknown('search directory traversal and implicit configuration are not certified', words[0])
    return {'views': views, 'unresolved': unresolved, 'complete': not unresolved}

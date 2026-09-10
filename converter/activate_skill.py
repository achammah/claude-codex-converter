#!/usr/bin/env python3
"""Observable skill activation; the PostToolUse adapter records this invocation."""
import re
import sys

if len(sys.argv) != 2 or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', sys.argv[1]):
    raise SystemExit('Usage: activate_skill.py SKILL_NAME')
print('Activated skill scope: ' + sys.argv[1])

"""test_ext_self_contained.py -- an extension's parts must all live inside its own folder.

Run: python tests/test_ext_self_contained.py

THE PROPERTY THIS PINS, and why it is not merely tidiness. A release decides what it ships by
leaving whole extension folders out, and that only works if a folder holds the whole extension.
The quality scorer did not until 2026-09-13: its worker, its setup script and its multi-gigabyte
environment all sat at the app root, and only its manifest was in the folder, reaching back out
with `../../`. Leaving that folder out would have shipped an app with the scorer's worker and setup
script still lying in the root and no manifest to explain them.

So: every path a manifest declares -- worker, setup, venv, and anything in `requires` -- must
resolve inside the folder holding that manifest. `../` is the exact spelling that breaks it, which
is why this tests the RESOLVED path rather than looking for the characters.

It runs over every extension present, not a list kept here, so a new one is covered by existing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

failures = []


def check(name, cond, detail=''):
    print(('  ok    ' if cond else '  FAIL  ') + name + (('\n          ' + str(detail)) if not cond and detail else ''))
    if not cond:
        failures.append(name)


exts = server.list_extensions()
check('there is at least one extension to check', bool(exts), server.EXT_DIR)

for e in exts:
    folder = os.path.realpath(os.path.join(server.EXT_DIR, e['id']))
    declared = [('worker', e['worker']), ('setup', e['setup']), ('venv', e['venv'])]
    declared += [('requires[%d]' % i, p) for i, p in enumerate(e['requires'])]
    for what, path in declared:
        if not path:
            continue          # not declaring one is fine; declaring one that escapes is not
        inside = os.path.realpath(path).startswith(folder + os.sep)
        check('%s: %s stays inside its folder' % (e['id'], what),
              inside, '%s\n          is not under %s' % (path, folder))

# The scorer is the one that was wrong, so name it: a regression here would most likely be someone
# restoring the old layout, and a generic loop passing vacuously (no extensions present) must not
# be mistaken for that being fixed.
q = server.get_extension('quality')
check('the quality scorer is still present in master to be checked', q is not None)
if q:
    check('quality: its worker is worker.py in its own folder',
          os.path.basename(q['worker'] or '') == 'worker.py', q['worker'])
    check('quality: its setup is setup.bat in its own folder',
          os.path.basename(q['setup'] or '') == 'setup.bat', q['setup'])
    check('quality: its venv is venv/ in its own folder',
          os.path.basename(q['venv'] or '') == 'venv', q['venv'])

print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)

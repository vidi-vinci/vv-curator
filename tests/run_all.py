"""Run every regression test in this folder, one after another.

    python tests/run_all.py              all of them
    python tests/run_all.py hidden set   only tests whose name contains "hidden" or "set"
    python tests/run_all.py -v           stream each test's output as it runs

Each test is still a standalone script and still runs on its own -- this only
saves you doing that 23 times. Output is shown for FAILURES only, because 23
passing tests print several hundred lines nobody reads, and a failure buried in
that is the same as no runner at all.

SEQUENTIAL on purpose. Several of these bind a real port and drive the real
server (test_keepalive, test_hidden, test_set_cull, test_undo_delete...), so
running them at once would have them fight over the port and fail in ways that
have nothing to do with the code.

Exit code is 0 only if every test that ran passed. Node tests are skipped, not
failed, when node is not installed -- a missing toolchain is not a regression.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# A hung test must not wedge the whole run. Generous: the server-driven ones do
# real work, and a slow machine is not a failure.
TIMEOUT = 300


def discover(patterns):
    out = []
    for name in sorted(os.listdir(HERE)):
        if not name.startswith('test_'):
            continue
        if not (name.endswith('.py') or name.endswith('.js')):
            continue
        if patterns and not any(p.lower() in name.lower() for p in patterns):
            continue
        out.append(name)
    return out


def have_node():
    try:
        subprocess.run(['node', '--version'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def main(argv):
    verbose = '-v' in argv or '--verbose' in argv
    patterns = [a for a in argv if not a.startswith('-')]

    tests = discover(patterns)
    if not tests:
        print('No tests matched %s' % (patterns or '(everything)'))
        return 1

    node = have_node()
    passed, failed, skipped = [], [], []
    started = time.time()

    print('Running %d test%s from %s\n' % (len(tests), '' if len(tests) == 1 else 's', HERE))

    for name in tests:
        path = os.path.join(HERE, name)
        if name.endswith('.js'):
            if not node:
                skipped.append((name, 'node not installed'))
                print('SKIP  %-24s node not installed' % name)
                continue
            cmd = ['node', path]
        else:
            cmd = [sys.executable, path]

        t0 = time.time()
        try:
            # cwd=ROOT so a test that writes a scratch folder puts it where the
            # others do. Each test already adds ROOT to sys.path itself, so this
            # is about side effects, not imports.
            r = subprocess.run(cmd, cwd=ROOT, timeout=TIMEOUT,
                               stdout=None if verbose else subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            out = '' if verbose else (r.stdout or b'').decode('utf-8', 'replace')
            ok = r.returncode == 0
            note = '' if ok else 'exit %d' % r.returncode
        except subprocess.TimeoutExpired as e:
            out = (e.output or b'').decode('utf-8', 'replace') if e.output else ''
            ok, note = False, 'TIMED OUT after %ds' % TIMEOUT

        secs = time.time() - t0
        print('%-5s %-24s %5.1fs %s' % ('ok' if ok else 'FAIL', name, secs, note))
        (passed if ok else failed).append((name, out))

    print('\n' + '-' * 62)
    for name, out in failed:
        print('\n===== %s =====' % name)
        print(out.rstrip() or '(no output)')

    elapsed = time.time() - started
    print('\n%d passed, %d failed, %d skipped in %.0fs'
          % (len(passed), len(failed), len(skipped), elapsed))
    if skipped:
        print('skipped: ' + ', '.join('%s (%s)' % s for s in skipped))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

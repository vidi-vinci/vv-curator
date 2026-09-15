"""test_shipped_defaults.py — a change to a SHIPPED default must not reach an existing install.

Run: python test_shipped_defaults.py

THE BUG THIS EXISTS TO STOP COMING BACK. On 2026-08-21 a default was changed for the benefit of
people who don't have a config yet: "hide large images" was switched off, having shipped ON at
2000px. The same change blanked the AI endpoint, which shipped pointing at one particular KoboldCpp
install.

BOTH OF THOSE SETTINGS ARE NOW GONE -- the AI block on 2026-08-23, hide-large on 2026-09-08 -- so
the rule is pinned on `autoplay` instead, and the removed settings appear here in the other role: a
key that no longer exists must be DROPPED from an old config rather than served back to a client
with no use for it. Every install that ran an older build still has `hide_large` saved, so that
check is live rather than hypothetical.

It is safe only because of a rule this codebase already follows in comments but had never
asserted: **a default applies where a key is ABSENT, never over a value that was saved.** Get that
wrong and changing a default silently rewrites the settings of everyone who already uses the app —
which is invisible in review, invisible in the response, and only shows up as "why is half my
library missing".

The test is deliberately the OTHER HALF of test_settings.py's "absent means ON" check. That one pins
the direction a safeguard may not fail in (never default a guard off on upgrade); this one pins that
a *deliberate* default change stays confined to fresh installs.

Four configs, each in its own real server process, because the merge happens during config load at
startup — an in-process check would be testing a function rather than the thing that runs:

  1. an established install with everything saved -> nothing moves
  2. an established install with a partial block  -> saved value kept, missing keys filled
  3. no config file at all                        -> the new default applies
  4. a config still carrying an `analysis` block  -> it is dropped, general beside it untouched
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

failures = []


def check(name, cond, detail=''):
    print(('  ok    ' if cond else '  FAIL  ') + name + (('\n          ' + str(detail)) if not cond and detail else ''))
    if not cond:
        failures.append(name)


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def config_after_load(cfg_obj):
    """Start the real server on a temp config and return what it loaded."""
    tmp = tempfile.mkdtemp(prefix='vv-defaults-')
    cfg = os.path.join(tmp, 'config.json')
    data = os.path.join(tmp, 'data')
    os.makedirs(data)
    port = free_port()
    env = dict(os.environ, CV_CONFIG=cfg, CV_DATA=data, PORT=str(port))
    if cfg_obj is not None:
        cfg_obj['port'] = port
        json.dump(cfg_obj, open(cfg, 'w'))
    proc = subprocess.Popen([sys.executable, 'server.py'], cwd=HERE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/config', timeout=5) as r:
                    return json.loads(r.read().decode())
            except Exception:
                time.sleep(0.25)
        raise RuntimeError('server did not start')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


print('\nShipped defaults: new for strangers, invisible to an existing install\n')

# 1. An established install with everything saved. `autoplay` carries the rule now: it SHIPS OFF,
#    so a saved True is exactly the value a careless default would overwrite.
j = config_after_load({'roots': [], 'active': None,
                       'general': {'autoplay': True, 'keep_behavior': 'next',
                                   'confirm_recycle': True, 'models_dir': ''}})
check('an existing install keeps its saved value', j['general']['autoplay'] is True, j['general'])

# 2. An established install with only a PARTIAL general block — the rest of the keys have to be
#    filled from defaults without disturbing the one that was saved.
j = config_after_load({'roots': [], 'active': None, 'general': {'autoplay': True}})
check('a partial general block keeps its saved value', j['general']['autoplay'] is True, j['general'])
check('  and gains the missing keys from defaults',
      set(j['general']) >= {'keep_behavior', 'confirm_recycle', 'models_dir'}, j['general'])

# 3. A stranger. The shipped default applies.
j = config_after_load(None)
check('a fresh install gets the shipped default', j['general']['autoplay'] is False, j['general'])

# The AI settings block is gone (2026-08-23). A config that still carries one — every install that
# ran an older build does — must have it dropped rather than served back to a client that has no
# use for it. This is the load-bearing half of the removal: `analysis` left save_config()'s
# whitelist, so anything not popped at load would sit unread in config.json forever.
j = config_after_load({'roots': [], 'active': None,
                       'general': {'autoplay': True, 'hide_large': True},
                       'analysis': {'vlm_url': 'http://localhost:5001/v1/chat/completions'},
                       'vlm_url': 'http://legacy.example/'})
check('an old config\'s AI block is dropped, not served', 'analysis' not in j, sorted(j))
check('  and so is the legacy top-level vlm_url', 'vlm_url' not in j, sorted(j))
# Removed 2026-09-08, and this is the half that matters to an existing install: a setting nobody
# can reach any more must not sit in the general block being served to a client that ignores it.
check('  and a removed setting is dropped from general',
      'hide_large' not in j['general'], sorted(j['general']))
check('  while the rest of the general block is untouched',
      j['general']['autoplay'] is True, j['general'])

print('\nall passed' if not failures else '\n%d failed' % len(failures))
sys.exit(1 if failures else 0)

"""test_snapshots.py — snapshots: storage, validation, survival across a restart, and the
legacy `views` migration.

A snapshot is a named capture of the sidebar's filter state, stored in config.json. The whole list
is replaced on every write (POST /api/snapshots), so this endpoint is the ONLY validation point —
anything it lets through lands on disk and comes back on the next launch.

What matters here, in order of how badly it would bite:

  * the legacy `views` block still loads. The feature shipped as "Views" and real saved sets exist
    in the wild; if _normalize_config didn't migrate them they would vanish silently, because
    save_config() writes an explicit whitelist and would simply drop the old key.
  * the block actually reaches config.json. Same whitelist: a new key that isn't on it round-trips
    fine in memory and disappears on the next unrelated save. Every assertion below therefore reads
    the FILE, not just the response.
  * unknown keys never reach disk, so a future filter rename can't leave junk accumulating;
  * group/sets default TRUE when absent — the one default that isn't falsy. Getting it wrong
    silently un-merges every set on a snapshot saved before those keys existed;
  * names are the identity, so they're de-duped case-insensitively and truncated, not rejected.

Needs no images and no scan — snapshots touch nothing the index touches.

Run: python test_snapshots.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

_fails = []


def check(label, cond):
    print(('  ok   ' if cond else '  FAIL ') + label)
    if not cond:
        _fails.append(label)


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = tempfile.mkdtemp(prefix='vv_snapshots_')
    try:
        rc = run(here, tmp)
        rc = migration(here, tmp) or rc
        print('\n' + ('FAILED: ' + '; '.join(_fails) if _fails else 'All checks passed.'))
        return 1 if _fails else rc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _client(port):
    def get(path):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())

    def post(path, obj):
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                     data=json.dumps(obj).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
    return get, post


def _start(here, env, get, label):
    p = subprocess.Popen([sys.executable, 'server.py'], cwd=here, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(80):
        try:
            if get('/api/config'):
                return p
        except Exception:
            time.sleep(0.25)
    print(f'{label}: server never came up:\n' + p.stdout.read().decode(errors='replace')[-2000:])
    p.kill()
    return None


def _stop(p):
    if not p:
        return
    p.terminate()
    try:
        p.wait(timeout=15)
    except Exception:
        p.kill()


def run(here, tmp):
    lib = os.path.join(tmp, 'lib')
    os.makedirs(lib)
    cfg = os.path.join(tmp, 'config.json')
    data = os.path.join(tmp, 'data')
    os.makedirs(data)
    port = _free_port()
    with open(cfg, 'w') as f:
        json.dump({'roots': [{'key': 'A', 'name': 'Main', 'path': lib}], 'active': 'A', 'port': port}, f)
    get, post = _client(port)

    def on_disk(key='snapshots'):
        with open(cfg, 'r', encoding='utf-8') as fh:
            return json.load(fh).get(key)

    env = dict(os.environ, CV_CONFIG=cfg, CV_DATA=data)
    proc = _start(here, env, get, 'main')
    if not proc:
        return 1
    try:
        print('\nA fresh config has no snapshots')
        check('/api/config exposes snapshots as an empty list', get('/api/config').get('snapshots') == [])

        print('\nSaving snapshots: the echo, and what lands on disk')
        full = {'terms': ['cyberpunk'], 'xterms': ['blurry'], 'model': 'ill.safetensors',
                'folder': 'out', 'modelFolder': 'Illustrious', 'meta': '', 'type': 'image',
                'group': True, 'sets': False, 'tags': ['label:publish', 'portrait'],
                'favOnly': True, 'hasNote': True,
                'rmin': 0.7722, 'rmax': '', 'dfrom': '2026-01-01', 'dto': '',
                'sort': 'random', 'order': 'asc'}
        # `unreviewed` and `roots` are sent but are NOT in `full`, and the equality check below is
        # what proves both were dropped. Both left the whitelist, which is the mechanism that
        # clears a key out of snapshots already saved -- but for opposite reasons, and the test
        # carries both so the distinction survives:
        #   unreviewed (2026-08-19) -- a filter that was RETIRED. The feature is gone.
        #   roots      (2026-08-20) -- a filter that was RECLASSIFIED. The library show/hide
        #                              selection is alive and well; it stopped being part of the
        #                              filter set, because it is SCOPE. So a snapshot must not
        #                              carry one, and restoring a snapshot must not move yours.
        code, j = post('/api/snapshots', {'snapshots': [{'name': 'Publish queue',
                                                         'filters': dict(full, unreviewed=True,
                                                                         roots=['A'])},
                                                        {'name': 'Cull', 'filters': {'sort': 'size-desc'}}]})
        check('POST /api/snapshots returns 200', code == 200)
        check('two snapshots come back', [v['name'] for v in j.get('snapshots', [])] == ['Publish queue', 'Cull'])
        check('every filter value survives the round trip', j['snapshots'][0]['filters'] == full)
        check('a retired filter key is dropped, not passed through',
              'unreviewed' not in j['snapshots'][0]['filters'])
        check('the library selection is dropped, not passed through',
              'roots' not in j['snapshots'][0]['filters'])
        check('they reached config.json on disk', [v['name'] for v in (on_disk() or [])] ==
              ['Publish queue', 'Cull'])
        check('/api/config now serves them', len(get('/api/config').get('snapshots', [])) == 2)

        print('\nDefaults and coercion')
        code, j = post('/api/snapshots', {'snapshots': [{'name': 'Bare', 'filters': {}}]})
        f = j['snapshots'][0]['filters']
        check('group defaults to true when absent', f['group'] is True)
        check('sets defaults to true when absent', f['sets'] is True)
        check('other booleans default to false', f['favOnly'] is False and f['hasNote'] is False)
        check('missing strings become empty', f['model'] == '' and f['dfrom'] == '')
        check('order falls back to desc', f['order'] == 'desc')
        check('missing lists become empty', f['tags'] == [])
        check('rmin unset is empty, not 0', f['rmin'] == '')
        # 'unreviewed' was whitelisted until the filter was retired on 2026-08-19. Dropping it
        # from the whitelist is what makes an OLD snapshot shed a dead filter rather than
        # carry it forever, so its ABSENCE is the assertion now.
        check('a retired filter key is dropped, not carried', 'unreviewed' not in f)
        # Same mechanism, different reason -- see the note beside `full` above. A snapshot is
        # library-agnostic as of 2026-08-20, so the key's ABSENCE is the assertion.
        check('a reclassified filter key is dropped too', 'roots' not in f)

        code, j = post('/api/snapshots', {'snapshots': [{'name': 'Coerce', 'filters': {
            'rmin': 'abc', 'rmax': '0.5', 'order': 'asc', 'tags': 'notalist',
            'terms': ['ok', 5, None], 'junkKey': 'go away', 'group': 0}}]})
        f = j['snapshots'][0]['filters']
        check('an unparseable rmin becomes empty', f['rmin'] == '')
        check('a numeric-string rmax parses to a float', f['rmax'] == 0.5)
        check('order asc is kept', f['order'] == 'asc')
        check('a non-list tags value becomes []', f['tags'] == [])
        check('unusable list entries are dropped', f['terms'] == ['ok', '5'])
        check('an unknown key is stripped from the echo', 'junkKey' not in f)
        check('an unknown key never reaches disk', 'junkKey' not in on_disk()[0]['filters'])
        check('a falsy group is honoured (not defaulted back to true)', f['group'] is False)

        print('\nNames are the identity')
        code, j = post('/api/snapshots', {'snapshots': [
            {'name': 'Same', 'filters': {}},
            {'name': '  SAME  ', 'filters': {}},
            {'name': '   ', 'filters': {}},
            {'name': '', 'filters': {}},
            {'name': 'x' * 80, 'filters': {}}]})
        names = [v['name'] for v in j['snapshots']]
        check('a case-differing duplicate collapses to one', names.count('Same') == 1)
        check('blank and whitespace-only names are dropped', len(names) == 2)
        check('an over-long name is truncated, not rejected', names[1] == 'x' * 60)

        print('\nLimits and bad input')
        code, j = post('/api/snapshots', {'snapshots': [{'name': f'v{i}', 'filters': {}} for i in range(60)]})
        check('the list is capped at 50', len(j['snapshots']) == 50)
        code, j = post('/api/snapshots', {'snapshots': 'nope'})
        check('a non-list body is refused with 400', code == 400 and 'error' in j)
        check('the refused write left the previous list alone', len(on_disk()) == 50)
        code, j = post('/api/snapshots', {'snapshots': ['a string', 42, {'no': 'name'}]})
        check('unusable entries are skipped rather than failing the write', j['snapshots'] == [])

        print('\nSurvival across a restart')
        post('/api/snapshots', {'snapshots': [{'name': 'Keeper', 'filters': {'sort': 'size-desc', 'rmin': 0.25}}]})
        _stop(proc)
        proc = _start(here, env, get, 'restart')
        if not proc:
            return 1
        v = get('/api/config').get('snapshots', [])
        check('the snapshot is still there after a restart', [x['name'] for x in v] == ['Keeper'])
        check('its values survived normalization', v and v[0]['filters']['rmin'] == 0.25
              and v[0]['filters']['sort'] == 'size-desc')
        check('defaults were re-applied on load', v and v[0]['filters']['group'] is True)

        print('\nAn unrelated settings save does not drop the block')
        post('/api/settings', {'general': {'autoplay': True}})
        check('snapshots survive a settings write', [x['name'] for x in (on_disk() or [])] == ['Keeper'])
    finally:
        _stop(proc)
    return 0


def migration(here, tmp):
    """The feature shipped as "Views" and real saved sets exist under the legacy key. Losing them on
    upgrade is the one failure here that destroys user work, so it gets its own server."""
    print('\nLegacy `views` config migrates to `snapshots`')
    lib = os.path.join(tmp, 'lib2')
    os.makedirs(lib, exist_ok=True)
    cfg = os.path.join(tmp, 'config_legacy.json')
    data = os.path.join(tmp, 'data2')
    os.makedirs(data, exist_ok=True)
    port = _free_port()
    legacy = {'roots': [{'key': 'A', 'name': 'Main', 'path': lib}], 'active': 'A', 'port': port,
              'views': [{'name': 'Old publish queue',
                         'filters': {'tags': ['label:publish'], 'sort': 'size-desc', 'rmin': 0.5}}]}
    with open(cfg, 'w') as f:
        json.dump(legacy, f)
    get, post = _client(port)
    env = dict(os.environ, CV_CONFIG=cfg, CV_DATA=data)
    proc = _start(here, env, get, 'migration')
    if not proc:
        return 1
    try:
        s = get('/api/config').get('snapshots', [])
        check('the legacy view is served as a snapshot', [x['name'] for x in s] == ['Old publish queue'])
        check('its filters came across intact', s and s[0]['filters']['tags'] == ['label:publish']
              and s[0]['filters']['sort'] == 'size-desc' and s[0]['filters']['rmin'] == 0.5)
        check('/api/config no longer exposes a views key', 'views' not in get('/api/config'))
        # config.json is only rewritten on a save; force one the way any settings change would.
        post('/api/settings', {'general': {'autoplay': True}})
        with open(cfg, 'r', encoding='utf-8') as fh:
            on_disk = json.load(fh)
        check('config.json now holds the snapshots key',
              [x['name'] for x in on_disk.get('snapshots', [])] == ['Old publish queue'])
        check('the legacy views key is gone from disk', 'views' not in on_disk)
    finally:
        _stop(proc)
    return 0


def _free_port():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


if __name__ == '__main__':
    sys.exit(main())

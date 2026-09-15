"""Regression test for the General settings block (/api/settings + save_config + load_config).

Run: python test_settings.py

Written when `confirm_recycle` was added, and deliberately general: **nothing tested the `general`
block at all**. It was only ever used as a convenient lever to force a config write while testing
something else (test_snapshots.py and test_window_config.py both POST `general` for that reason),
so every failure mode below was live and uncovered for four settings.

Three things are asserted, and each is a different way a setting silently dies:

  * **On disk, not in the response.** save_config() builds an EXPLICIT whitelist dict, so a block
    it doesn't name is accepted by the endpoint, echoed back happily, written once, and dropped by
    the next save of anything else. The response cannot show that; the file can.
  * **Absent means ON.** A config.json written before a setting existed has no key for it. If the
    coercion defaults it to False, upgrading silently switches a safeguard off — the one direction
    a default may not fail in. Asserted with a real pre-existing config that predates the key.
  * **A partial POST is a MERGE, not a replace.** Every general key is coerced by hand (there is no
    generic coercer as there is for `miner`), so a caller sending one key must not
    zero the other four.

Runs in-process on an ephemeral port against a temp config. Touches no real library.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import http.client

TMP = tempfile.mkdtemp(prefix='vv-settings-')
CFG = os.path.join(TMP, 'config.json')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = CFG
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server                                              # noqa: E402
from http.server import ThreadingHTTPServer                # noqa: E402

httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
PORT = httpd.socket.getsockname()[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

failures = []


def check(name, cond, detail=''):
    if cond:
        print('  ok    %s' % name)
    else:
        print('  FAIL  %s%s' % (name, ('\n          ' + str(detail)) if detail else ''))
        failures.append(name)


def call(method, path, obj=None):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=20)
    if obj is None:
        c.request(method, path)
    else:
        c.request(method, path, json.dumps(obj), {'Content-Type': 'application/json'})
    r = c.getresponse()
    body = r.read()
    c.close()
    try:
        return r.status, json.loads(body or b'{}')
    except ValueError:
        return r.status, {}


def general_on_disk():
    if not os.path.exists(CFG):
        return None
    return json.load(io.open(CFG, encoding='utf-8')).get('general')


try:
    print('\nGeneral settings: confirm_recycle, and the block it lives in\n')

    # 1. A FRESH install confirms. A guard's default is the half worth pinning.
    st, j = call('GET', '/api/config')
    check('a fresh config confirms recycling by default',
          j.get('general', {}).get('confirm_recycle') is True, j.get('general'))
    check('  and the server advertises that default to the client',
          j.get('general_defaults', {}).get('confirm_recycle') is True, j.get('general_defaults'))

    # 2. Round-trip, and the ECHO — the client re-reads state.confirmRecycle from this response, so
    #    an unechoed value means the toggle appears to do nothing until you reload.
    st, j = call('POST', '/api/settings', {'general': {'confirm_recycle': False}})
    check('switching it off is accepted', st == 200, st)
    check('  and echoed back in the response',
          j.get('general', {}).get('confirm_recycle') is False, j.get('general'))
    check('  and reaches config.json on disk',
          (general_on_disk() or {}).get('confirm_recycle') is False, general_on_disk())

    # 3. THE WHITELIST. Saving an unrelated block must not drop it — the failure the response can
    #    never show, and the one that makes a setting work today and quietly stop tomorrow.
    call('POST', '/api/window', {'x': 100, 'y': 100, 'w': 1200, 'h': 800})
    check('  and survives an unrelated config save',
          (general_on_disk() or {}).get('confirm_recycle') is False, general_on_disk())

    # 4. A PARTIAL post merges. Each key is coerced by hand, so a caller sending one must not
    #    silently reset the rest to their defaults.
    call('POST', '/api/settings', {'general': {'confirm_recycle': False, 'autoplay': True,
                                               'keep_behavior': 'stay',
                                               'models_dir': 'D:/models'}})
    call('POST', '/api/settings', {'general': {'confirm_recycle': True}})
    g = general_on_disk() or {}
    check('turning it back on does not disturb its neighbours',
          g.get('autoplay') is True and g.get('keep_behavior') == 'stay'
          and g.get('models_dir') == 'D:/models', g)
    check('  and it is itself back on', g.get('confirm_recycle') is True, g)

    # 5. RESTART. load_config() is what a fresh process runs, so calling it re-reads the file
    #    through the same coercion the app boots with.
    call('POST', '/api/settings', {'general': {'confirm_recycle': False}})
    reloaded = server.load_config()
    check('the choice survives a restart',
          reloaded['general'].get('confirm_recycle') is False, reloaded['general'])

    # 6. AN UPGRADE. A config.json from before this setting existed has no key at all, and must
    #    come back with confirms ON. Defaulting a safeguard to off on upgrade is the one direction
    #    this may not fail in — and `bool(missing)` is False, so it is a live hazard, not a
    #    theoretical one.
    old = json.load(io.open(CFG, encoding='utf-8'))
    old['general'].pop('confirm_recycle', None)
    check('  (the fixture really has no key, so the check is not vacuous)',
          'confirm_recycle' not in old['general'])
    with io.open(CFG, 'w', encoding='utf-8') as f:
        json.dump(old, f, indent=2)
    upgraded = server.load_config()
    check('a config predating the setting comes back CONFIRMING',
          upgraded['general'].get('confirm_recycle') is True, upgraded['general'])
    check('  and its other settings are untouched by the upgrade',
          upgraded['general'].get('keep_behavior') == 'stay'
          and upgraded['general'].get('models_dir') == 'D:/models', upgraded['general'])

    # 7. Junk must not become a truthy setting by accident, in either direction. `None` is the
    #    interesting one and it FAILED first: an explicit null is a present key, so `bool(get(k,
    #    default))` never reaches its default and bool(None) is False — a null silently switched
    #    confirms off. Null and absent both mean "no answer", so both take the default (_bool_or).
    for junk, want in ((0, False), ('', False), (1, True), ('yes', True), (None, True)):
        call('POST', '/api/settings', {'general': {'confirm_recycle': junk}})
        got = (general_on_disk() or {}).get('confirm_recycle')
        check('  %r coerces to %r rather than being stored raw' % (junk, want),
              got is want, got)

    # ---- the card facts block ------------------------------------------------------------------
    # Added 2026-08-24 with Settings -> Cards. Same three failure modes as `general` above, plus one
    # of its own: this block is an ORDERED LIST, so its whole content is the order.
    print()
    print('Card facts: an ordered list, and the whitelist that keeps it')

    def cards_on_disk():
        if not os.path.exists(CFG):
            return None
        return json.load(io.open(CFG, encoding='utf-8')).get('cards')

    st, j = call('GET', '/api/config')
    keys = [r.get('key') for r in (j.get('cards') or [])]
    check('a fresh config offers every fact', sorted(keys) == sorted(
          ['dims', 'duration', 'age', 'filesize', 'model', 'folder']), keys)
    check('  and advertises the defaults to the client',
          isinstance(j.get('cards_defaults'), list) and j['cards_defaults'], j.get('cards_defaults'))

    # The author's own example: age before dimensions.
    reordered = [{'key': 'age', 'place': 'always'}, {'key': 'dims', 'place': 'always'},
                 {'key': 'duration', 'place': 'always'}, {'key': 'filesize', 'place': 'hover'},
                 {'key': 'model', 'place': 'off'}, {'key': 'folder', 'place': 'hover'}]
    st, j = call('POST', '/api/settings', {'cards': reordered})
    check('a new order is accepted', st == 200, st)
    check('  and echoed back', [r['key'] for r in j.get('cards', [])] ==
          [r['key'] for r in reordered], j.get('cards'))
    check('  and reaches config.json on disk',
          [r['key'] for r in (cards_on_disk() or [])] == [r['key'] for r in reordered],
          cards_on_disk())
    check('  keeping each placement', [r['place'] for r in (cards_on_disk() or [])] ==
          [r['place'] for r in reordered], cards_on_disk())

    # THE WHITELIST, again. save_config writes an explicit dict; a block missing from it is written
    # once and dropped by the next save of anything else. This is the assertion the response cannot
    # make, and the reason this test exists at all.
    call('POST', '/api/window', {'x': 10, 'y': 10, 'w': 800, 'h': 600})
    check('an unrelated save does not drop the order',
          [r['key'] for r in (cards_on_disk() or [])] == [r['key'] for r in reordered],
          cards_on_disk())

    # A FACT THIS BUILD DOES NOT KNOW is dropped; one MISSING is appended at its default. That pair
    # is what makes a newly-added fact appear for an old config rather than stay invisible.
    st, j = call('POST', '/api/settings',
                 {'cards': [{'key': 'age', 'place': 'always'},
                            {'key': 'nonesuch', 'place': 'always'},
                            {'key': 'age', 'place': 'off'}]})
    got = [r['key'] for r in j.get('cards', [])]
    check('an unknown key is dropped', 'nonesuch' not in got, got)
    check('a duplicate is dropped', got.count('age') == 1, got)
    check('and every missing fact is appended', sorted(got) == sorted(
          ['dims', 'duration', 'age', 'filesize', 'model', 'folder']), got)
    check('  with the one that was sent kept FIRST, i.e. the order survives', got[0] == 'age', got)

    # A DEFAULT THAT HAS BEEN WRITTEN DOWN STOPS BEING A DEFAULT, and this pair is the fix.
    # `cards` used to be materialised into config.json just for existing, so changing the shipped
    # order afterwards reached nobody who had ever saved anything -- the author hit it within a day.
    st, j = call('POST', '/api/settings', {'cards': [dict(r) for r in server.DEFAULT_CARD_FACTS]})
    check('saving the shipped order stores NOTHING', cards_on_disk() is None, cards_on_disk())
    check('  while the client is still told the full list',
          [r['key'] for r in j.get('cards', [])] ==
          [r['key'] for r in server.DEFAULT_CARD_FACTS], j.get('cards'))
    call('POST', '/api/window', {'x': 20, 'y': 20, 'w': 800, 'h': 600})
    check('  and an unrelated save still leaves it absent', cards_on_disk() is None, cards_on_disk())

    # ...and the one-shot: a config carrying the SUPERSEDED order is a default nobody chose, so it
    # follows the new one rather than freezing the old.
    old_cfg = json.load(io.open(CFG, encoding='utf-8'))
    old_cfg['cards'] = [{'key': 'dims', 'place': 'always'}, {'key': 'duration', 'place': 'always'},
                        {'key': 'age', 'place': 'always'}, {'key': 'filesize', 'place': 'hover'},
                        {'key': 'model', 'place': 'hover'}, {'key': 'folder', 'place': 'hover'}]
    with io.open(CFG, 'w', encoding='utf-8') as f:
        json.dump(old_cfg, f, indent=2)
    up = server.load_config()
    check('a config holding the superseded order picks up the new default',
          up.get('cards') is None, up.get('cards'))

    # A REAL choice is never mistaken for one, however close it looks.
    old_cfg['cards'] = [{'key': 'dims', 'place': 'always'}, {'key': 'duration', 'place': 'always'},
                        {'key': 'age', 'place': 'off'}, {'key': 'filesize', 'place': 'hover'},
                        {'key': 'model', 'place': 'hover'}, {'key': 'folder', 'place': 'hover'}]
    with io.open(CFG, 'w', encoding='utf-8') as f:
        json.dump(old_cfg, f, indent=2)
    up = server.load_config()
    check('  but the same order with a placement changed IS a choice, and is kept',
          up.get('cards') is not None
          and next(r['place'] for r in up['cards'] if r['key'] == 'age') == 'off', up.get('cards'))

    # A junk placement must land somewhere real rather than being stored raw and drawn as nothing.
    st, j = call('POST', '/api/settings', {'cards': [{'key': 'age', 'place': 'sideways'}]})
    place = next((r['place'] for r in j.get('cards', []) if r['key'] == 'age'), None)
    check('a placement this build does not know falls back', place in ('always', 'hover', 'off'), place)
finally:
    httpd.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)

print('\n' + ('all checks passed' if not failures
              else '%d FAILED:\n  - %s' % (len(failures), '\n  - '.join(failures))))
sys.exit(1 if failures else 0)

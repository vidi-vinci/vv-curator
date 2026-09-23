"""test_ext_manifest.py -- what an extension folder has to contain before the app calls it Ready.

Run: python tests/test_ext_manifest.py

The author set up the tagger, watched it build, saw **Ready** in Settings, ran it, and got a Python
traceback. Two separate faults, and this pins the app's half.

`installed` meant only "a venv folder exists". A setup that creates the environment and then fails
-- or is interrupted before the 5.6 GB model arrives -- leaves exactly that, so the app promised
something it could not do. An extension is entitled to say what *finished* looks like for it, so a
manifest can list `requires`: files that must exist before it is usable.

The rest is the loader's contract. A broken manifest must still produce a ROW, carrying its error,
because an extension that fails to load has to be visible -- the alternative is a folder the user
installed that never appears and never says why.
"""
import io
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix='vv_extmf_')
os.environ['CV_DATA'] = os.path.join(TMP, 'data')
os.environ['CV_CONFIG'] = os.path.join(TMP, 'config.json')
os.makedirs(os.environ['CV_DATA'], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

_fails = []


def check(label, cond, got=None):
    print(('  ok   ' if cond else '  FAIL ') + label + ('' if cond else '  -> %r' % (got,)))
    if not cond:
        _fails.append(label)


# Point the loader at a scratch extensions/ rather than the real one, so this test says the same
# thing whether or not the machine running it has any extension set up.
EXT = os.path.join(TMP, 'extensions')
os.makedirs(EXT)
server.EXT_DIR = EXT


def make(name, manifest, files=()):
    d = os.path.join(EXT, name)
    os.makedirs(d, exist_ok=True)
    if manifest is not None:
        with io.open(os.path.join(d, 'extension.json'), 'w', encoding='utf-8') as f:
            f.write(manifest if isinstance(manifest, str) else json.dumps(manifest))
    for rel in files:
        p = os.path.join(d, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, 'w').close()
    return d


print('\nWhat an extension has to contain before it is Ready\n')

# --- a plain worker with no venv: nothing to set up, ready as soon as it is unzipped ------------
make('plain', {'name': 'Plain', 'produces': 'tags', 'worker': 'worker.py'}, ['worker.py'])
e = server.get_extension('plain')
check('an extension with no venv is installed on arrival', e['installed'] is True, e)
check('...and has no setup step to offer', e['setup'] is None, e['setup'])

# --- a venv extension: the venv alone is NOT enough when it declares requirements ---------------
mf = {'name': 'Heavy', 'produces': 'tags', 'worker': 'worker.py', 'venv': 'venv',
      'setup': 'setup.bat', 'requires': ['venv/model-cache/weights.pth']}
make('heavy', mf, ['worker.py', 'setup.bat'])
e = server.get_extension('heavy')
check('no venv at all -> not installed', e['installed'] is False, e['installed'])

make('heavy', mf, ['venv/Scripts/python.exe'])
e = server.get_extension('heavy')
check('THE VENV ALONE IS NOT READY when a model is still missing',
      e['installed'] is False, e['installed'])

make('heavy', mf, ['venv/model-cache/weights.pth'])
e = server.get_extension('heavy')
check('venv AND the required file -> installed', e['installed'] is True, e['installed'])
check('...and it is OFF until switched on: every extension ships off', e['active'] is False)
server.CONFIG['extensions'] = {'heavy': True, 'texty': True}
e = server.get_extension('heavy')
check('...and active once switched on', e['active'] is True)

# --- a venv extension that requires nothing keeps the old, looser meaning -----------------------
make('loose', {'name': 'Loose', 'produces': 'score', 'worker': 'w.py', 'venv': 'venv'},
     ['w.py', 'venv/Scripts/python.exe'])
check('an extension that declares no requirements is installed with just its venv',
      server.get_extension('loose')['installed'] is True)

# --- the loader's own contract ------------------------------------------------------------------
make('broken', '{ this is not json')
e = server.get_extension('broken')
check('a broken manifest still produces a row', e is not None)
check('...carrying the reason', bool(e and e['error']), e and e['error'])
check('...and is never treated as usable', not (e and e['active']))

make('noworker', {'name': 'No worker', 'produces': 'tags'})
e = server.get_extension('noworker')
check('a manifest naming no worker is an error, not a silent skip',
      bool(e and e['error']), e and e['error'])

make('missing', {'name': 'Missing', 'produces': 'tags', 'worker': 'gone.py'})
e = server.get_extension('missing')
check('a worker that is not on disk is an error', bool(e and e['error']), e and e['error'])

make('weird', {'name': 'Weird', 'produces': 'sandwiches', 'worker': 'w.py'}, ['w.py'])
check('an unknown `produces` is dropped rather than trusted',
      server.get_extension('weird')['produces'] == '', server.get_extension('weird')['produces'])

os.makedirs(os.path.join(EXT, 'notanext'))       # a folder with no manifest at all
names = sorted(x['id'] for x in server.list_extensions())
check('a folder with no extension.json is not an extension',
      'notanext' not in names, names)

# --- settings a manifest may declare -------------------------------------------------------------
# A text extension needs configuring, which is what earned the manifest a `settings` block and an
# extension its own tab in Settings. A MALFORMED FIELD IS AN ERROR, NOT A DROPPED ONE: the quiet
# alternative is a setting the user can never reach and a worker reading a blank where it expected
# an address, which looks like the model being broken rather than the manifest being wrong.
make('texty', {'name': 'Texty', 'produces': 'text', 'worker': 'w.py',
               'settings': [{'key': 'endpoint', 'label': 'Address', 'default': 'http://x'},
                            {'key': 'on', 'type': 'checkbox'},
                            {'key': 'secret', 'type': 'password'}]}, ['w.py'])
e = server.get_extension('texty')
check('`text` is a kind an extension may produce', e['produces'] == 'text', e['produces'])
check('...and needs no venv or setup to be ready', e['active'] is True, e)
check('a field with no type is a text field', e['settings'][0]['type'] == 'text', e['settings'][0])
check('...and falls back to its key for a label',
      e['settings'][1]['label'] == 'on', e['settings'][1])

for bad, why in ((['nope'], 'a setting that is not an object'),
                 ([{'label': 'No key'}], 'a setting with no key'),
                 ([{'key': 'a'}, {'key': 'a'}], 'two settings sharing a key'),
                 ([{'key': 'a', 'type': 'colour'}], 'an unknown field type'),
                 ({'key': 'a'}, '`settings` that is not a list')):
    make('badset', {'name': 'Bad', 'produces': 'text', 'worker': 'w.py', 'settings': bad}, ['w.py'])
    e = server.get_extension('badset')
    check('%s is an error, not a silent drop' % why, bool(e['error']), e['error'])
    check('...and it is never treated as usable', e['active'] is False, e['active'])

check('an extension that says nothing offers no self-test',
      e['can_test'] is False, e['can_test'])
make('testy', {'name': 'Testy', 'produces': 'text', 'worker': 'w.py', 'test': True,
               'settings': [{'key': 'endpoint'}]}, ['w.py'])
check('one that declares `test` does', server.get_extension('testy')['can_test'] is True)
check('...and the client is told, so the button only appears where it would do something',
      {x['id']: x for x in server._extensions_payload()}['testy']['can_test'] is True)
check('a test is refused for an extension that never offered one',
      server.run_ext_test(server.get_extension('texty'), {})['ok'] is False)

# Defaults apply until the user saves something; then the saved value wins, per key.
server.CONFIG['ext_settings'] = {'texty': {'endpoint': 'http://mine', 'secret': 'k'}}
vals = server.ext_settings_for(server.get_extension('texty'))
check('a saved value beats the manifest default', vals['endpoint'] == 'http://mine', vals)
check('a key never saved falls back to its default', vals['on'] is False, vals)

# THE ONE RULE WORTH A TEST OF ITS OWN: an API key must not travel to the browser, because
# /api/config is a payload that ends up in debug traces and in anything that logs a response.
payload = {x['id']: x for x in server._extensions_payload()}['texty']
check('a password NEVER reaches the client', payload['values']['secret'] == '', payload['values'])
check('...but the client is told that one is set',
      payload['secrets_set'] == ['secret'], payload['secrets_set'])
check('a non-secret value is sent normally',
      payload['values']['endpoint'] == 'http://mine', payload['values'])
check('the key is nowhere in the whole payload', 'k' not in json.dumps(payload['values']))

# merge_ext_values is shared by Save and Test so the two cannot read the same panel differently —
# a Test that used the STORED address while Save used the TYPED one would be checking something
# other than the thing about to be saved, which is the one job the button has.
ex = server.get_extension('texty')
stored = {'endpoint': 'http://mine', 'secret': 'k'}
merged = server.merge_ext_values(ex, {'endpoint': 'http://typed', 'secret': ''}, stored)
check('a typed value is what gets used, saved or not',
      merged['endpoint'] == 'http://typed', merged)
check('...and a blank password means the saved one, not a cleared one',
      merged['secret'] == 'k', merged)
check('a key the manifest does not declare is dropped',
      'sneaky' not in server.merge_ext_values(ex, {'sneaky': 1}, stored))
server.CONFIG['ext_settings'] = {}

# --- the switch ---------------------------------------------------------------------------------
server.CONFIG['extensions'] = {'heavy': False}
e = server.get_extension('heavy')
check('switched off: still installed, but not active',
      e['installed'] is True and e['active'] is False, (e['installed'], e['active']))

# --- and the client never sees an absolute path -------------------------------------------------
server.CONFIG['extensions'] = {}
payload = server._extensions_payload()
blob = json.dumps(payload)
check('the payload tells the client THAT there is a setup, not where',
      TMP.replace('\\', '\\\\') not in blob and 'worker' not in payload[0],
      sorted(payload[0]))

print()
sys.exit(1 if _fails else 0)

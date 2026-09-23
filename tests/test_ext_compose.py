"""test_ext_compose.py -- the `compose` block: fields the user fills in PER RUN, not once.

Run: python tests/test_ext_compose.py

`settings` is answered once and saved; `compose` is answered every time and thrown away. They are
deliberately the same shape, validated by the same function and drawn by the same client code --
the app still draws every control and an extension still supplies no markup. That sameness is what
this file pins, along with the two things it makes possible to get wrong:

  * A SECRET IN `compose` would be echoed back to the page to pre-fill the next run, which is the
    exact leak the password rule in _extensions_payload exists to prevent. A key is standing
    config; it belongs in `settings`.
  * A KEY IN BOTH BLOCKS is an OVERRIDE -- a default you set once and change for one run -- and it
    was refused until the first real case turned up. What still has to match is the TYPE, because
    the worker receives them as one merged dict and its one key cannot change shape depending on
    which half supplied it.

Both are manifest mistakes an extension author makes once, so they have to produce a row carrying
an error rather than a folder that quietly does the wrong thing.
"""
import io
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix='vv_extcomp_')
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


EXT = os.path.join(TMP, 'extensions')
os.makedirs(EXT)
server.EXT_DIR = EXT


def make(name, manifest):
    """A folder with a manifest and a worker, so nothing fails for an unrelated reason."""
    d = os.path.join(EXT, name)
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, 'extension.json'), 'w', encoding='utf-8') as f:
        f.write(json.dumps(manifest))
    open(os.path.join(d, 'worker.py'), 'w').close()
    return server._read_extension(d)


BASE = {'name': 'Poster', 'produces': 'post', 'worker': 'worker.py'}


def with_blocks(**blocks):
    m = dict(BASE)
    m.update(blocks)
    return m


print('\nThe compose block\n')

# --- the fourth kind exists ---------------------------------------------------------------------
e = make('kind', with_blocks())
check("'post' is a kind a manifest may declare", e['produces'] == 'post', e['produces'])
check('an unknown kind is still emptied rather than trusted',
      make('bogus', with_blocks(produces='rutabaga'))['produces'] == '', 'not empty')

# --- a well-formed block -----------------------------------------------------------------------
e = make('good', with_blocks(compose=[
    {'key': 'title', 'label': 'Title', 'type': 'text'},
    {'key': 'detail', 'label': 'Description', 'type': 'textarea', 'help': 'Optional.'},
    {'key': 'mark', 'label': 'Mark these Posted', 'type': 'checkbox', 'default': True},
]))
check('a valid compose block loads without error', e['error'] is None, e['error'])
check('its fields arrive in manifest order',
      [f['key'] for f in e['compose']] == ['title', 'detail', 'mark'],
      [f['key'] for f in e['compose']])
check('a field with no label falls back to its key, as settings do',
      make('nolabel', with_blocks(compose=[{'key': 'title', 'type': 'text'}]))['compose'][0]['label'] == 'title')

# --- absent means empty, not broken -------------------------------------------------------------
e = make('none', with_blocks())
check('an extension with no compose block gets an empty list, not an error',
      e['compose'] == [] and e['error'] is None, (e['compose'], e['error']))

# --- the same validator, so the same refusals ---------------------------------------------------
e = make('badtype', with_blocks(compose=[{'key': 'title', 'type': 'colour-picker'}]))
check('an unknown field type is an error', bool(e['error']), e['error'])
check('and the message names the compose block, not settings',
      'compose' in (e['error'] or ''), e['error'])
check('two compose fields sharing a key is an error',
      bool(make('dupe', with_blocks(compose=[{'key': 'a', 'type': 'text'},
                                             {'key': 'a', 'type': 'text'}]))['error']))
check('a compose block that is not a list is an error',
      bool(make('notalist', with_blocks(compose={'key': 'title'}))['error']))

# --- a secret may not be typed per run ----------------------------------------------------------
e = make('secret', with_blocks(compose=[{'key': 'api_key', 'label': 'Key', 'type': 'password'}]))
check('a password in compose is refused', bool(e['error']), e['error'])
check("and the message says where a secret belongs",
      'settings' in (e['error'] or ''), e['error'])
check('the same field in SETTINGS is fine',
      make('secretok', with_blocks(settings=[{'key': 'api_key', 'type': 'password'}]))['error'] is None)

# --- a key in both blocks is an override ---------------------------------------------------------
e = make('override', with_blocks(settings=[{'key': 'site', 'type': 'text'}],
                                 compose=[{'key': 'site', 'type': 'text'}]))
check('a key in both blocks is allowed -- it is a default you can change per run',
      e['error'] is None, e['error'])
e = make('mismatch', with_blocks(settings=[{'key': 'site', 'type': 'text'}],
                                 compose=[{'key': 'site', 'type': 'checkbox'}]))
check('but not with a different type on each side', bool(e['error']), e['error'])
check('and the message names the key and both types',
      'site' in (e['error'] or '') and 'checkbox' in (e['error'] or ''), e['error'])

# --- select, the sixth field type ------------------------------------------------------------------
# A free-text box for a value with two valid answers is a typo waiting to become a broken link.
e = make('sel', with_blocks(compose=[{'key': 'site', 'type': 'select', 'default': 'a',
                                      'options': ['a', {'value': 'b', 'label': 'Bee'}]}]))
check('a select loads', e['error'] is None, e['error'])
check('a bare string option becomes its own label',
      e['compose'][0]['options'][0] == {'value': 'a', 'label': 'a'}, e['compose'][0]['options'])
check('and an object option keeps its label',
      e['compose'][0]['options'][1] == {'value': 'b', 'label': 'Bee'}, e['compose'][0]['options'])
check('a select with no options is an error -- it would draw a control with nothing to pick',
      bool(make('sel0', with_blocks(compose=[{'key': 'x', 'type': 'select'}]))['error']))
check('and an empty list is the same mistake',
      bool(make('sel1', with_blocks(compose=[{'key': 'x', 'type': 'select',
                                              'options': []}]))['error']))

# --- a select only ever sends a value the manifest offered -------------------------------------------
# The page is not the authority on what is in a closed set: a hand-made request must not be able to
# put an arbitrary string where a hostname goes.
field = [{'key': 'site', 'type': 'select', 'default': 'civitai.com',
          'options': [{'value': 'civitai.com', 'label': 'civitai.com'},
                      {'value': 'civitai.red', 'label': 'civitai.red'}]}]
ext = {'settings': field}
check('an offered value passes through',
      server.merge_ext_values(ext, {'site': 'civitai.red'}, {})['site'] == 'civitai.red')
check('anything else falls back to the default, not to itself',
      server.merge_ext_values(ext, {'site': 'evil.example'}, {})['site'] == 'civitai.com',
      server.merge_ext_values(ext, {'site': 'evil.example'}, {}))

# --- a broken block still produces a ROW --------------------------------------------------------
# The whole point of the loader's error handling: an extension that fails to load must be VISIBLE.
e = make('broken', with_blocks(compose='not a list at all'))
check('a broken compose block still yields an entry carrying its error',
      e is not None and bool(e['error']) and e['id'] == 'broken', e)

# --- the client is told about compose, and never sees a secret ----------------------------------
server.CONFIG['extensions'] = {}
server.CONFIG['ext_settings'] = {'secretok': {'api_key': 'sk-do-not-leak'}}
payload = server._extensions_payload()
by_id = {e['id']: e for e in payload}
check('the payload carries each extension\'s compose fields',
      [f['key'] for f in by_id['good']['compose']] == ['title', 'detail', 'mark'],
      by_id.get('good', {}).get('compose'))
check('a stored password is still not in the payload',
      'sk-do-not-leak' not in json.dumps(payload))
check('and the client is told it is set',
      by_id['secretok']['secrets_set'] == ['api_key'], by_id['secretok']['secrets_set'])

print()
sys.exit(1 if _fails else 0)

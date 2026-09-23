"""worker.py — post the selected files to Civitai as one draft.

Standard library only, and no venv: this is three HTTP calls, so there is nothing to install and
nothing that can drag an incompatible package into the app's Python.

Run by the app as:  python worker.py <items.json> <settings.json>
Prints one JSON object per line to stdout and nothing else.

THE SHAPE OF A POST, established against the live site in a standalone harness before any of this
was written:

    1. POST /api/v1/image-upload          -> {"id": <uuid>, "uploadURL": <presigned>}
    2. PUT  <presigned>                   the bytes. NO key: the signature IS the credential, and
                                          attaching ours would hand it to a third party.
    3. POST /api/trpc/post.createWithImages   {"json": {images: [...], publish: false, ...}}

`post.createWithImages` is tRPC, not part of the public REST surface, so it appears in no API
reference. The envelope is `{"json": input}` out and `data.result.data.json` back.

WHAT AN IMAGE ENTRY MAY CARRY was read off the server by sending deliberately invalid payloads and
reading the validator's complaint: `url`, `index`, `width`, `height`, `type` and `modelVersionId`.
`meta`, `resources`, `civitaiResources` and `prompt` are stripped as unknown. Two consequences run
through everything below:

  * A VIDEO CANNOT HAVE A GENERATION DATA PANEL. Civitai builds that by reading inside the file and
    an MP4 holds nothing. This is a ceiling, not a gap.
  * ONLY ONE RESOURCE LINKS PER POST -- `modelVersionId`, singular, no array. So a run's LoRAs
    cannot be credited through the API at all.

Which is why posting a video together with the still from the same run matters so much: the still
carries its own metadata, Civitai reads it out of the file, and the LoRA arrives that way. It is
the only route a video's LoRA has.

WHAT IS WORTH FIXING HERE, AND WHAT IS NOT. The author, 2026-09-21: everything lands as a DRAFT, so
order, the cover, and removing a file someone did not want are all a few seconds of editing on
the site. METADATA IS THE EXCEPTION -- Civitai reads it out of the file at upload and never
looks again, so a file that arrives without it cannot be repaired there at all. Weigh work on
this extension that way: fidelity first, convenience a long way after.
"""
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

# EVERY CALL GOES HERE, whatever `site` says. `site` picks the domain a finished draft OPENS on,
# nothing more -- civitai.red is a read-only replica and the create call belongs on .com. The two
# share a backend, so a draft made here opens and EDITS fine on .red; that is expected, and is not
# evidence the API would take a create call there.
API = 'https://civitai.com'
UA = 'VV-Curator-civitai/1.0'

# Read timeouts. A 200MB video needs a long one; the two JSON calls answer in under a second and a
# long wait on those only delays a clear failure.
UPLOAD_TIMEOUT = 1800
JSON_TIMEOUT = 120

# Extension -> (mime, the `type` the post entry carries). Anything else is refused before a byte
# goes out: failing at second zero is cheaper and clearer than a validator error from the far end.
TYPES = {
    '.png': ('image/png', 'image'), '.jpg': ('image/jpeg', 'image'),
    '.jpeg': ('image/jpeg', 'image'), '.webp': ('image/webp', 'image'),
    '.mp4': ('video/mp4', 'video'), '.webm': ('video/webm', 'video'),
    '.mov': ('video/quicktime', 'video'),
    '.mp3': ('audio/mpeg', 'audio'), '.flac': ('audio/flac', 'audio'),
    '.wav': ('audio/wav', 'audio'), '.m4a': ('audio/mp4', 'audio'),
}


def out(obj):
    sys.stdout.write(json.dumps(obj) + '\n')
    sys.stdout.flush()


def phase(text):
    """Say what is happening now. A control line moves no counter -- see run_ext_batch. Without
    this a single large video leaves the bar frozen with no sign of life for minutes."""
    out({'control': True, 'phase': text})


class Fail(Exception):
    pass


def request(method, url, key=None, body=None, content_type=None, timeout=JSON_TIMEOUT):
    """One HTTP call. `key` present -> Bearer auth. Returns (status, bytes).

    Errors carry the SERVER's own message: this runs against a live account, where the far end's
    own wording is the only useful evidence. A socket-level stall raises TimeoutError or a bare
    OSError, NEITHER of which is a URLError, so both are caught -- otherwise a stalled upload
    escapes as a traceback that says nothing anyone can act on.
    """
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header('User-Agent', UA)
    if key:
        req.add_header('Authorization', 'Bearer ' + key)
    if content_type:
        req.add_header('Content-Type', content_type)
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(),
                                    timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        full = (e.read() or b'').decode('utf-8', 'replace')
        raise Fail('%s %s failed: %s %s. %s'
                   % (method, url.split('?', 1)[0], e.code, e.reason, full[:400]))
    except urllib.error.URLError as e:
        raise Fail('%s %s could not connect: %s' % (method, url.split('?', 1)[0], e.reason))
    except (TimeoutError, OSError) as e:
        raise Fail('%s %s stalled: %s' % (method, url.split('?', 1)[0], e))


def trpc(key, procedure, payload):
    """A tRPC mutation. Input is wrapped `{"json": ...}`; the answer is unwrapped from
    data.result.data.json. Civitai's own client does exactly this."""
    _, raw = request('POST', '%s/api/trpc/%s' % (API, procedure), key,
                     json.dumps({'json': payload}).encode('utf-8'), 'application/json')
    try:
        data = json.loads(raw)
    except Exception:
        raise Fail('%s returned something that is not JSON.' % procedure)
    inner = (data.get('result') or {}).get('data')
    if isinstance(inner, dict) and 'json' in inner:
        return inner['json']
    return inner if inner is not None else data


# ---- reading what the files already say ---------------------------------------------------------

def png_size(data):
    """(width, height) from a PNG's IHDR, or None. Both are OPTIONAL to the API, so anything that
    cannot be read confidently is left out rather than guessed at."""
    if len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        return None
    return int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')


def png_parameters(data):
    """The A1111 `parameters` text out of a PNG's tEXt / zTXt / iTXt chunks, or ''.

    iTXt matters: PIL switches a chunk to iTXt the moment its text leaves Latin-1, so a prompt with
    one curly apostrophe lands in a different chunk type than the same prompt without it.

    Deliberately standalone rather than importing the app's reader. A worker is a subprocess with a
    JSON contract and no import path into the app, and that separation is the whole point of the
    extension system.
    """
    import zlib
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        return ''
    i = 8
    while i + 8 <= len(data):
        n = int.from_bytes(data[i:i + 4], 'big')
        kind = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + n]
        i += 12 + n
        try:
            if kind in (b'tEXt', b'zTXt', b'iTXt'):
                k, _, rest = body.partition(b'\x00')
                if k != b'parameters':
                    continue
                if kind == b'tEXt':
                    return rest.decode('latin-1')
                if kind == b'zTXt':
                    return zlib.decompress(rest[1:]).decode('latin-1')
                comp = rest[0:1] == b'\x01'
                rest = rest[2:].split(b'\x00', 2)[-1]
                return (zlib.decompress(rest) if comp else rest).decode('utf-8', 'replace')
            if kind == b'IEND':
                break
        except Exception:
            continue                      # one unreadable chunk is not a broken file
    return ''


def model_hash_in(data):
    """The checkpoint's AutoV2 fingerprint, if this file carries one.

    THE HASH IS ALREADY IN THE FILE -- the saver writes `Model hash: <10 hex>` into the parameters
    block at generation time. So crediting a video's checkpoint costs a chunk read, not a multi-
    gigabyte hash of the checkpoint itself. That distinction is why there is no "model version"
    field to type: for any file the saver wrote, the answer is already here.

    A file from before the saver wrote fingerprints (or a plain ComfyUI save) has no such line, and
    then nothing is credited rather than something being guessed.
    """
    m = re.search(r'Model hash: ([0-9a-fA-F]{8,})', png_parameters(data) or '')
    return m.group(1).lower() if m else None


def resolve_resource(ref):
    """An AutoV2 hash -> the Civitai model version id to attach.

    The lookup is PUBLIC and unauthenticated, which is why it can run before anything is uploaded:
    a resource that does not exist on Civitai should not cost a failed post to discover.
    """
    _, raw = request('GET', '%s/api/v1/model-versions/by-hash/%s' % (API, ref))
    d = json.loads(raw)
    vid = d.get('id')
    if not vid:
        return None, ''
    name = '%s %s' % ((d.get('model') or {}).get('name') or '', d.get('name') or '')
    return int(vid), name.strip()


# ---- the three calls ----------------------------------------------------------------------------

def upload_one(key, data, content_type):
    """Presign, then PUT. Returns the uuid the post will reference.

    THE PUT IS RETRIED ONCE, and only on a connection-level failure. A stall against object storage
    is the classic transient -- the harness hit one on an 11MB file and it succeeded on the retry.
    A fresh presign comes with it, because a signature has a window and the first attempt may have
    burned most of it. An HTTP error is never retried: a 403 or a 400 from storage is a real
    answer, and repeating it just asks the same question twice.
    """
    for attempt in (1, 2):
        presign = json.loads(request('POST', '%s/api/v1/image-upload' % API, key,
                                     b'{}', 'application/json')[1])
        uuid, put_url = presign.get('id'), presign.get('uploadURL')
        if not uuid or not put_url:
            raise Fail('image-upload returned no id/uploadURL.')
        try:
            # NO key here, deliberately: a presigned URL carries its own credential.
            request('PUT', put_url, None, data, content_type, timeout=UPLOAD_TIMEOUT)
            return uuid
        except Fail as e:
            transient = 'stalled' in str(e) or 'could not connect' in str(e)
            if attempt == 2 or not transient:
                raise
            phase('Upload did not complete, retrying once')


def run(items, settings):
    key = (settings.get('api_key') or '').strip()
    if not key:
        raise Fail('No API key. Add one under Settings → Extensions → Post to Civitai.')

    # Everything is validated and READ before a byte goes out. A bad path in position four is worth
    # hitting at second zero rather than after three uploads have landed.
    loaded = []
    for it in items:
        path = it.get('path') or ''
        ext = os.path.splitext(path)[1].lower()
        if ext not in TYPES:
            raise Fail('%s is not a file type Civitai takes.' % os.path.basename(path))
        if not os.path.isfile(path):
            raise Fail('Missing file: %s' % os.path.basename(path))
        with open(path, 'rb') as f:
            data = f.read()
        if not data:
            raise Fail('%s is empty.' % os.path.basename(path))
        mime, kind = TYPES[ext]
        loaded.append({'item': it, 'data': data, 'mime': mime, 'kind': kind,
                       'name': os.path.basename(path)})

    # The resource, resolved BEFORE uploading: the lookup is public and free, so a hash that names
    # nothing on Civitai should not cost an upload to find out. First one wins -- the create call
    # takes one `modelVersionId` and there is no array form.
    version_id, version_name = None, ''
    for f in loaded:
        h = model_hash_in(f['data'])
        if not h:
            continue
        phase('Looking up the model')
        try:
            version_id, version_name = resolve_resource(h)
        except Fail:
            version_id = None           # a lookup failure is not a reason to abandon the post
        if version_id:
            break

    out({'ready': True, 'device': 'civitai'})

    # EVERY UPLOAD FINISHES BEFORE THE POST IS CREATED, and a single failure abandons the run. The
    # alternative -- posting what succeeded -- creates a draft that looks complete and is not, and
    # a silently truncated post is the failure people write bug reports about. Abandoning leaves
    # uploaded blobs nobody references, which nobody ever sees.
    images = []
    total = len(loaded)
    for i, f in enumerate(loaded):
        phase('Uploading %s (%d of %d)' % (f['name'], i + 1, total))
        try:
            uuid = upload_one(key, f['data'], f['mime'])
        except Fail as e:
            out({'id': f['item'].get('id'), 'error': str(e)})
            raise Fail('%s did not upload, so nothing was posted. %s' % (f['name'], e))
        entry = {'url': uuid, 'index': i, 'type': f['kind']}
        size = png_size(f['data'])        # PNG only; anything else goes without, which is legal
        if size:
            entry['width'], entry['height'] = size
        # A resource id is pinned ONLY to a file that cannot speak for itself. An image carrying
        # its own parameters block already tells Civitai its checkpoint AND every LoRA with its
        # weight; one id on top of that can only ever say less. This is exactly the case of a video
        # posted with the still it came from: the video needs the id, the still does not.
        if version_id and not (f['kind'] == 'image' and png_parameters(f['data'])):
            entry['modelVersionId'] = version_id
        images.append(entry)
        out({'id': f['item'].get('id'), 'uploaded': True})

    phase('Creating the post')
    payload = {'images': images, 'publish': False}
    if version_id:
        payload['modelVersionId'] = version_id
    title = (settings.get('title') or '').strip()
    if title:
        payload['title'] = title
    # The title stands in when no description was typed. The PROMPT never does -- it is generation
    # input, not a blurb, and a video model's prompt runs long and busy enough to read as debris
    # under the picture. The still in the same post shows it properly in Generation data.
    detail = (settings.get('detail') or '').strip() or title
    if detail:
        payload['detail'] = detail
    tags = [t.strip() for t in (settings.get('tags') or '').split(',') if t.strip()]
    if tags:
        payload['tags'] = tags

    try:
        res = trpc(key, 'post.createWithImages', payload)
    except Fail as e:
        # NEVER RETRIED. The create call is not idempotent, so a retry after a lost answer is how
        # you end up with two drafts. The honest thing is to say the post may exist and where to
        # look -- this is the one case the app cannot resolve for you.
        raise Fail('The upload finished but the post could not be confirmed, so it may or may not '
                   'have been created. Check your drafts on Civitai before trying again. %s' % e)

    pid = (res or {}).get('id')
    if not pid:
        raise Fail('The post was not created (no id came back).')
    # THE EDITOR, not the public view. A draft is not public yet, so the public page is the wrong
    # destination twice over: it is where the remaining work happens (the resource copy-to-all, the
    # rearrange, the Publish button), and an R+ image is INVISIBLE on the public page unless you are
    # on the right domain -- which is exactly how the first link sent the author to a post he could
    # not see. His own draft renders in the editor whatever its rating.
    site = (settings.get('site') or '').strip().rstrip('/').split('://')[-1] or 'civitai.com'
    out({'control': True, 'post': {
        'id': pid, 'url': 'https://%s/posts/%s/edit' % (site, pid), 'draft': True,
        'images': len((res or {}).get('imageIds') or images),
        'resource': version_name,
        'ids': [f['item'].get('id') for f in loaded]}})


def run_test(settings):
    """Does the key work, and whose account is it? Nothing is posted and nothing is saved.

    THIS CANNOT CHECK PERMISSIONS, and it used to pretend otherwise. `/api/v1/me` returns
    `tokenScope` as an opaque NUMBER (11492205 on a working key, 2026-09-21), not a list of grants
    -- so the first version read it as text, found no "media" in it and reported "this key may not
    be allowed to post" about a key that posts perfectly well. A check that cries wolf is worse
    than one that admits what it cannot see.

    So it answers the question it actually can: does the key authenticate, and is it the account
    you meant? Whether it may POST is proved by posting, and the first push says so plainly if not.
    """
    key = (settings.get('api_key') or '').strip()
    if not key:
        return out({'ok': False, 'error': 'No key yet.'})
    try:
        _, raw = request('GET', '%s/api/v1/me' % API, key)
        me = json.loads(raw)
    except Fail as e:
        return out({'ok': False, 'error': str(e)})
    who = str(me.get('username') or '').strip()
    status = str(me.get('status') or '').strip().lower()
    # Status IS readable and does decide something: a suspended account cannot post whatever the
    # key allows. That is the one warning this can honestly raise.
    if status and status != 'active':
        return out({'ok': True, 'warn': True,
                    'detail': 'Signed in%s, but the account status is "%s".'
                              % (' as ' + who if who else '', status)})
    out({'ok': True, 'detail': ('Signed in as %s. Permissions are only proved by posting.' % who)
                               if who else 'Signed in. Permissions are only proved by posting.'})


def main():
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        items = json.load(f)
    settings = {}
    if len(sys.argv) > 2 and os.path.exists(sys.argv[2]):
        with open(sys.argv[2], 'r', encoding='utf-8') as f:
            settings = json.load(f)
    if '--test' in sys.argv[1:]:
        return run_test(settings)
    try:
        run(items, settings)
    except Fail as e:
        out({'control': True, 'fatal': str(e)})
        sys.exit(1)


if __name__ == '__main__':
    main()

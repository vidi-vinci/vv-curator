"""Reading a song: the ComfyUI graph out of ID3, and the song's attributes out of the caption.

The caption is prose, not fields, and the phrasing varies run to run — so these pin BOTH observed
phrasings ("78 BPM" and "bpm is 87") and, just as importantly, that an unreadable attribute comes
back as None instead of a guess.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import comfy_meta

# Keys print as ♯/♭, which a default Windows console (cp1252) cannot encode — the test would die
# in its own progress output rather than on a real failure.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'samples')

# Captions in the two phrasings seen in real output. Trimmed to the opening sentences, which is
# where every attribute below is stated.
CAP_INLINE = ('Global Metadata: Lo-fi hip-hop, chillhop. 78 BPM, D flat major, major scale with '
              'jazzy extensions. Laid-back and dreamy throughout.\n\n'
              'Vocal Details: Soft androgynous vocal, hushed half-sung delivery.')
CAP_LABELLED = ('Global Metadata\nBasic Attributes: bpm is 87. key is C, and scale is minor. '
                'Alternative Hip-Hop / Experimental Electronic.\n'
                'Vocal Details\nVocal Gender & Timbre: Singer A (Female). The vocalist possesses '
                'a clear, articulate mezzo-soprano timbre.')
CAP_RUNON = ('Global Metadata Basic Attributes: bpm is 74. key is F#, and scale is minor. '
             'Country / Outlaw Country. Global Emotional Progression: The track begins with a '
             'stark, isolated atmosphere. Vocal Gender & Timbre: Singer A (Male). The vocalist '
             'features a rugged baritone.')


def check(label, got, want):
    ok = got == want
    print(f'  {"ok  " if ok else "FAIL"} {label}: {got!r}' + ('' if ok else f'  (want {want!r})'))
    return ok


def test_captions():
    print('caption parsing')
    ok = True
    a = comfy_meta.parse_song_caption(CAP_INLINE)
    ok &= check('inline bpm', a['bpm'], 78)
    ok &= check('inline key', a['key'], 'D♭ major')
    ok &= check('inline genre', a['genre'], 'Lo-fi hip-hop, chillhop')
    ok &= check('inline vocal', a['vocal'], 'androgynous')

    b = comfy_meta.parse_song_caption(CAP_LABELLED)
    ok &= check('labelled bpm', b['bpm'], 87)
    ok &= check('labelled key', b['key'], 'C minor')
    ok &= check('labelled genre', b['genre'], 'Alternative Hip-Hop / Experimental Electronic')
    ok &= check('labelled vocal', b['vocal'], 'female')

    c = comfy_meta.parse_song_caption(CAP_RUNON)
    ok &= check('run-on bpm', c['bpm'], 74)
    ok &= check('run-on key', c['key'], 'F♯ minor')
    ok &= check('run-on genre', c['genre'], 'Country / Outlaw Country')
    ok &= check('run-on vocal', c['vocal'], 'male')
    return ok


def test_headings_are_not_genres():
    """Reported 2026-08-16: cards reading "Vocal is" and "Vocal" as their genre.

    Both are the caption template's own section headings, and both survived every earlier shape
    test by being short, digit-free noun phrases near the top. A genre is a noun phrase, so a
    candidate carrying a verb or preposition is a sentence; and a candidate made only of heading
    words is a heading. Both rules only ever REJECT — the cost of an unfamiliar template is a blank
    genre, never a wrong one.
    """
    print('headings are not genres')
    ok = True
    cases = [
        ('bpm is 128. Vocal is female, clear and bright. Singer A (Female).', 'vocal-is'),
        ('bpm is 128. Vocal. Singer A (Female). Warm delivery throughout.', 'bare-vocal'),
        ('bpm is 90. Arrangement. Layered synth pads and a driving kick.', 'bare-arrangement'),
        ('bpm is 90. Production is dense and compressed.', 'production-is'),
        # No tempo anywhere: nothing anchors the search, so nothing is picked up.
        ('A gentle piece. Warm delivery throughout. Pleasant.', 'unanchored'),
    ]
    for caption, label in cases:
        ok &= check(f'{label} yields no genre', comfy_meta.parse_song_caption(caption)['genre'], None)
    # ...and the rules must not cost a real genre that happens to contain "and" or one word.
    keep = [('bpm is 174. Drum and Bass. Rolling breaks at speed.', 'Drum and Bass'),
            ('Techno. bpm is 130. Relentless four-to-the-floor.', 'Techno'),
            ('bpm is 100. Synthwave / Retrowave. Neon and chrome.', 'Synthwave / Retrowave')]
    for caption, want in keep:
        ok &= check(f'kept {want!r}', comfy_meta.parse_song_caption(caption)['genre'], want)
    return ok


def test_nothing_invented():
    """A caption that states none of it must yield none of it — no partial guesses."""
    print('nothing invented')
    ok = True
    d = comfy_meta.parse_song_caption('A warm and pleasant piece of music, played gently.')
    for k in ('bpm', 'key', 'genre', 'vocal'):
        ok &= check(f'empty {k}', d[k], None)
    ok &= check('empty caption', comfy_meta.parse_song_caption('')['bpm'], None)
    ok &= check('None caption', comfy_meta.parse_song_caption(None)['genre'], None)
    return ok


def test_real_files():
    """The whole path, on real MP3s — skipped when the samples aren't present."""
    print('real sample files')
    mp3s = sorted(f for f in os.listdir(SAMPLES) if f.lower().endswith('.mp3')) \
        if os.path.isdir(SAMPLES) else []
    if not mp3s:
        print('  skip (no samples/*.mp3)')
        return True
    ok = True
    for fn in mp3s:
        res = comfy_meta.extract_audio(os.path.join(SAMPLES, fn))
        print(f'  {fn}')
        ok &= check('    model_name', res['model_name'], 'minimax_music3_dit_fp16')
        ok &= check('    has seed', res['seed'] is not None, True)
        ok &= check('    has steps', res['steps'], 30)
        ok &= check('    has caption', bool(res['positive']), True)
        ok &= check('    has lyrics', bool(res['lyrics']), True)
        ok &= check('    has bpm', res['bpm'] is not None, True)
        ok &= check('    has genre', bool(res['genre']), True)
        print(f'         genre={res["genre"]!r} bpm={res["bpm"]} key={res["key"]!r} '
              f'vocal={res["vocal"]!r}')
    return ok


def test_not_audio():
    """A non-MP3 path returns the empty shape rather than raising."""
    print('non-audio input')
    res = comfy_meta.extract_audio(os.path.join(SAMPLES, 'nope.png'))
    return check('png through extract_audio', res['positive'], None)


def test_embedded_cover():
    """Cover art round-trips through the tag without disturbing the graph already in it.

    Pins the padding trap that a first attempt at writing one fell into: an ID3 tag ends in zero
    padding, a reader stops at the first zero frame id, and a frame written INTO that padding is
    unreachable — the file looks valid and reports no cover. So the fixture writes the frame where
    a correct writer would, before the padding, and a second case writes one after it to prove the
    reader is not accidentally lenient.
    """
    print('embedded cover art')
    import io
    import shutil
    try:
        from PIL import Image
    except Exception:
        print('  skip (no Pillow)')
        return True
    src = next((os.path.join(SAMPLES, f) for f in sorted(os.listdir(SAMPLES))
                if f.lower().endswith('.mp3')), None) if os.path.isdir(SAMPLES) else None
    if not src:
        print('  skip (no samples/*.mp3)')
        return True

    def synchsafe(n):
        return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])

    buf = io.BytesIO()
    Image.new('RGB', (300, 300), (120, 60, 140)).save(buf, 'JPEG', quality=80)
    art = buf.getvalue()
    payload = b'\x03' + b'image/jpeg\x00' + b'\x03' + b'\x00' + art
    frame = b'APIC' + synchsafe(len(payload)) + b'\x00\x00' + payload

    def write_copy(dst, before_padding):
        shutil.copyfile(src, dst)
        with open(dst, 'rb') as f:
            head = f.read(10)
            tag = f.read(comfy_meta._synchsafe(head[6:10]))
            audio = f.read()
        pos = 0
        while pos + 10 <= len(tag) and tag[pos:pos + 4] != b'\x00\x00\x00\x00':
            pos += 10 + comfy_meta._synchsafe(tag[pos + 4:pos + 8])
        newtag = (tag[:pos] + frame + tag[pos:]) if before_padding else (tag + frame)
        with open(dst, 'wb') as f:
            f.write(head[:6] + synchsafe(len(newtag)) + newtag + audio)

    ok = True
    good = os.path.join(SAMPLES, '_test_cover_ok.mp3')
    bad = os.path.join(SAMPLES, '_test_cover_padding.mp3')
    try:
        write_copy(good, True)
        write_copy(bad, False)
        ok &= check('cover read back', len(comfy_meta.read_id3_cover(good) or b''), len(art))
        ok &= check('a frame after the padding is NOT found',
                    comfy_meta.read_id3_cover(bad), None)
        ok &= check('no cover on an untouched file', comfy_meta.read_id3_cover(src), None)
        # ...and the graph in the same tag must be undisturbed by the insertion.
        before, after = comfy_meta.extract_audio(src), comfy_meta.extract_audio(good)
        for k in ('model_name', 'seed', 'steps', 'bpm', 'genre', 'key', 'vocal', 'lyrics'):
            ok &= check(f'graph unchanged: {k}', after[k], before[k])
    finally:
        for f in (good, bad):
            if os.path.exists(f):
                os.remove(f)
    return ok


def test_readable_stem():
    """The last-resort headline, for an instrumental under a batch prefix: the filename, minus the
    machine tail. Reported 2026-08-16 as cards headed `..._19-36-14~vv3gflke_00001`."""
    print('last-resort headline')
    import server
    ok = True
    ok &= check('code + counter stripped',
                server._readable_stem('MM_Music_Aug15_19-36-14~vv3gflke_00001.mp3'),
                'MM_Music_Aug15_19-36-14')
    ok &= check('a plain name is left alone', server._readable_stem('My Song.mp3'), 'My Song')
    ok &= check('counter alone', server._readable_stem('track_00001.mp3'), 'track')
    ok &= check('empty stays empty', server._readable_stem(''), '')
    return ok


if __name__ == '__main__':
    results = [test_captions(), test_headings_are_not_genres(), test_nothing_invented(),
               test_embedded_cover(), test_readable_stem(), test_real_files(), test_not_audio()]
    print('\nPASS' if all(results) else '\nFAIL')
    sys.exit(0 if all(results) else 1)

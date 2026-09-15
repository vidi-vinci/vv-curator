"""
prompt_miner.py — mine a candidate tag vocabulary from the text a root already has.

Aggregates the positive prompts, folder names, and filenames across every image in a
root, counts how many distinct images each phrase / word appears in, and returns a
frequency-ranked list of candidate tags for the user to approve. No model, no network —
pure standard library, in the one-module-per-concern style of comfy_meta.py / thumbs.py.

Two token sources are combined so both prompt dialects contribute:
  - comma-phrases   — SDXL / danbooru-style "blonde hair, detailed skin, ..." lists
  - words + bigrams — Flux / Krea natural-language sentences

Folder and filename tokens are mined too (lower signal — de-noised of counters/hashes).
`mine()` returns (candidates, postings): the caller keeps `postings` (candidate -> image
ids) server-side so approving a candidate can bulk-tag exactly those images.

A candidate's `count` is the number of *distinct images* it occurs in (not raw
occurrences), so a phrase that also shows up as a bigram in the same image is not
double-counted.
"""
import os
import re

# Generic English function words + prompt filler that should never become tags on their
# own. The user's editable stoplist (Settings -> Miner) is merged over this at mine time.
STOPWORDS = {
    # articles / conjunctions / prepositions / pronouns
    'a', 'an', 'the', 'and', 'or', 'but', 'of', 'to', 'in', 'on', 'at', 'by', 'for',
    'with', 'from', 'as', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'it', 'its',
    'this', 'that', 'these', 'those', 'then', 'than', 'so', 'into', 'over', 'under',
    'out', 'up', 'down', 'off', 'about', 'very', 'more', 'most', 'some', 'any', 'all',
    'no', 'not', 'you', 'your', 'my', 'his', 'her', 'their', 'our', 'they', 'them',
    'he', 'she', 'we', 'i', 'me', 'him', 'who', 'which', 'what', 'has', 'have', 'had',
    # common prompt filler that is not a useful tag by itself
    'image', 'photo', 'picture', 'style', 'quality', 'detailed', 'detail', 'best',
    'wearing', 'looking', 'standing', 'sitting', 'shot', 'view', 'scene', 'ultra',
    'highly', 'realistic',
    # generic generator-filename noise (filenames are opt-in and noisy)
    'comfyui', 'img', 'output', 'render', 'final', 'copy', 'edit', 'upscale',
    'upscaled', 'tmp', 'temp', 'png', 'jpg', 'jpeg', 'webp',
}

# Default AI-prompt quality-boost boilerplate. Shown pre-filled in Settings -> Miner so
# the user can edit it; the edited list is what gets merged into the stopword set.
DEFAULT_STOPLIST = [
    'masterpiece', 'best quality', 'high quality', 'highest quality', 'ultra detailed',
    'ultra-detailed', 'extremely detailed', 'highly detailed', 'high resolution', 'hires',
    '8k', '4k', 'uhd', 'hdr', 'sharp focus', 'professional', 'award winning',
    'award-winning', 'trending on artstation', 'artstation', 'raw photo',
    'score_9', 'score_9_up', 'score_8', 'score_8_up', 'score_7_up', 'score_6_up',
    'worst quality', 'low quality', 'normal quality', 'lowres', 'bad quality',
]

_WEIGHT = re.compile(r'[()\[\]{}:]')            # prompt weight / emphasis punctuation
_WS = re.compile(r'\s+')
_STANDALONE_NUM = re.compile(r'(?:^|\s)\d+(?:\.\d+)?(?=\s|$)')  # leftover weight numbers
_HEXY = re.compile(r'^[0-9a-f]{6,}$')           # md5-ish hash tokens in filenames
_NUMISH = re.compile(r'^\d+$')
_WORDS = re.compile(r"[a-z0-9']+")              # word tokenizer (text already lowercased)
_SPLIT_PHRASE = re.compile(r'[,\n]')            # phrases split on comma or newline
_FNAME_SPLIT = re.compile(r'[^a-z0-9]+')        # filename token splitter


def normalize_phrase(s):
    """Lowercase a raw prompt phrase and strip weight syntax / emphasis / embeddings.

    '(detailed skin:1.2)' -> 'detailed skin'; 'embedding:badhands' -> 'badhands'.
    Preserves alphanumeric tags like '1girl' — only whitespace-delimited pure numbers
    (leftover weights) are removed.
    """
    if not s:
        return ''
    s = s.lower().replace('embedding:', ' ')
    s = _WEIGHT.sub(' ', s)                     # (x:1.2) / [x] / {x} -> x
    s = _STANDALONE_NUM.sub(' ', s)             # drop leftover standalone weight numbers
    s = _WS.sub(' ', s).strip(" \t-_.")
    return s


def phrases_from_prompt(text, max_words=5):
    """Comma/newline-delimited prompt phrases, normalized. Long clauses (> max_words)
    are dropped here and left for word/bigram mining instead."""
    out = []
    if not text:
        return out
    for raw in _SPLIT_PHRASE.split(text):
        p = normalize_phrase(raw)
        if not p or _NUMISH.match(p):
            continue
        if p.count(' ') + 1 > max_words:
            continue
        out.append(p)
    return out


def words_from_text(text, stopwords, min_len=3):
    """Unigrams + within-segment bigrams from free text, stopwords removed. Bigrams are
    built from adjacent surviving tokens within a comma/newline segment (so bigrams never
    span a comma), which lets "blonde hair" survive without joining across phrases."""
    grams = []
    if not text:
        return grams
    for seg in _SPLIT_PHRASE.split(text.lower()):
        toks = [w for w in _WORDS.findall(seg)
                if len(w) >= min_len and not _NUMISH.match(w) and w not in stopwords]
        grams.extend(toks)
        for i in range(len(toks) - 1):
            grams.append(toks[i] + ' ' + toks[i + 1])
    return grams


def terms_from_folder(folder):
    """Path-segment terms from a relative folder, '_'/'-' treated as spaces."""
    out = []
    if not folder:
        return out
    for seg in re.split(r'[\\/]+', folder):
        seg = normalize_phrase(seg.replace('_', ' ').replace('-', ' '))
        if seg and seg != '.' and not _NUMISH.match(seg):
            out.append(seg)
    return out


def terms_from_filename(filename, min_len=3):
    """Alpha(-numeric) filename tokens, minus the extension, counters, and hash-like runs."""
    out = []
    if not filename:
        return out
    name = os.path.splitext(filename)[0].lower()
    for tok in _FNAME_SPLIT.split(name):
        if len(tok) >= min_len and not _NUMISH.match(tok) and not _HEXY.match(tok):
            out.append(tok)
    return out


def build_stopset(stoplist):
    """The full stopword set: built-in STOPWORDS + the user's (or default) stoplist."""
    stop = set(STOPWORDS)
    for s in (stoplist if stoplist is not None else DEFAULT_STOPLIST):
        s = (s or '').strip().lower()
        if s:
            stop.add(s)
    return stop


def mine(rows, cfg, progress=None, should_stop=None):
    """Mine candidate tags from `rows` (iterable of (image_id, positive, folder, filename)).

    cfg keys: min_count, max_candidates, min_word_len, max_phrase_words,
              sources={prompt,folder,filename}, stoplist=[...].

    `progress(done, total)` is called every 200 rows; `should_stop()` truthy aborts and ranks
    whatever was accumulated so far, so a cancelled mine still returns usable (if partial) counts.
    Same shape as index_db.scan's callbacks.

    Returns (candidates, postings):
      candidates = [{'tag', 'count', 'sources'}] sorted by count desc, then tag.
      postings   = {tag: set(image_id)} for the surviving candidates only.
    """
    src = cfg.get('sources') or {}
    use_prompt = src.get('prompt', True)
    use_folder = src.get('folder', True)
    use_filename = src.get('filename', True)
    min_count = int(cfg.get('min_count', 5))
    max_candidates = int(cfg.get('max_candidates', 500))
    min_word_len = int(cfg.get('min_word_len', 3))
    max_phrase_words = int(cfg.get('max_phrase_words', 5))
    stop = build_stopset(cfg.get('stoplist'))

    postings = {}   # candidate -> set(image_id)
    sources = {}    # candidate -> set(source-name)

    def add(cand, iid, source):
        if not cand or cand in stop:
            return
        postings.setdefault(cand, set()).add(iid)
        sources.setdefault(cand, set()).add(source)

    total = len(rows) if hasattr(rows, '__len__') else 0
    for n, (iid, positive, folder, filename) in enumerate(rows, 1):
        if should_stop and n % 200 == 0 and should_stop():
            break                       # rank what we have; a partial count beats nothing
        if progress and n % 200 == 0:
            progress(n, total)
        if use_prompt and positive:
            for p in phrases_from_prompt(positive, max_phrase_words):
                add(p, iid, 'prompt')
            for w in words_from_text(positive, stop, min_word_len):
                add(w, iid, 'prompt')
        if use_folder and folder:
            for t in terms_from_folder(folder):
                add(t, iid, 'folder')
        if use_filename and filename:
            for t in terms_from_filename(filename, min_word_len):
                add(t, iid, 'filename')

    ranked = sorted(((c, len(ids)) for c, ids in postings.items() if len(ids) >= min_count),
                    key=lambda x: (-x[1], x[0]))[:max_candidates]
    keep = {c for c, _ in ranked}
    candidates = [{'tag': c, 'count': n, 'sources': sorted(sources[c])} for c, n in ranked]
    postings = {c: ids for c, ids in postings.items() if c in keep}
    return candidates, postings


if __name__ == '__main__':
    # Quick tokenizer sanity check (verification step 1): python prompt_miner.py
    sample_rows = [
        (1, '(masterpiece:1.2), best quality, 1girl, blonde hair, detailed skin, forest background',
         'portraits\\elf', 'ComfyUI_00042_.png'),
        (2, 'A cinematic photograph of a blonde woman standing in a sunlit forest, shallow depth of field',
         'portraits\\elf', 'render_final_a1b2c3d4e5.png'),
        (3, '1girl, blonde hair, forest, masterpiece', 'portraits', 'pic_00007.png'),
    ]
    cfg = {'min_count': 2, 'max_candidates': 50, 'sources': {'prompt': True, 'folder': True,
           'filename': True}}
    cands, post = mine(sample_rows, cfg)
    print('candidates (count >= %d):' % cfg['min_count'])
    for c in cands:
        print('  %-22s %2d  [%s]' % (c['tag'], c['count'], ','.join(c['sources'])))
    print('\nboilerplate dropped: masterpiece/best quality absent above ->',
          not any(c['tag'] in ('masterpiece', 'best quality') for c in cands))

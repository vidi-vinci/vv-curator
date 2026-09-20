"""
comfy_meta.py — extract ComfyUI generation metadata from PNG outputs.

Reads the embedded `prompt` (API-format graph) text chunk and pulls out:
  - positive prompt (traced from the main sampler's `positive` input, with a
    string-graph evaluator for Concatenate / Replace / passthrough nodes)
  - negative prompt (same, from `negative`)
  - model / checkpoint (traced from the sampler's `model` input back to the
    root loader; handles CheckpointLoaderSimple and Flux UNETLoader)

Pure standard library (struct/zlib/json) — no Pillow needed for metadata.
Designed to be defensive: unknown / exotic workflows fall back to heuristics
rather than raising.
"""
import base64
import struct
import zlib
import json
import heapq
import os
import re
import time

SAMPLER_CLASSES = {
    'KSampler', 'KSamplerAdvanced', 'SamplerCustom', 'SamplerCustomAdvanced',
    'KSampler (Efficient)', 'KSampler //Inspire', 'SamplerCustomAdvanced',
    'ClownsharKSampler_Beta', 'ClownsharKSampler', 'SharkSampler', 'ClownSampler',
}


def _is_sampler_node(n):
    """Does this node actually run a sampling pass?

    A NAME LIST ALONE CANNOT WORK. Custom sampler packs are everywhere, and a class this list has
    never heard of fails **silently**: the walker finds no sampler, so the prompt drops to a
    fallback and the generation settings come back empty, with nothing on screen to say why. That
    is exactly what RES4LYF's `ClownsharKSampler_Beta` did — a node carrying sampler_name,
    scheduler, steps, cfg and seed inline, in the ordinary shape, ignored because of its name.

    So the list stays (it is exact and cheap, and covers the classes whose shape is unusual), and
    anything else is judged on SHAPE: it calls itself a sampler, it consumes a latent, and it
    carries at least one sampling knob. Requiring the latent is what keeps helper nodes out —
    `KSamplerSelect` only names a sampler, and `SamplerCustom`'s sigma/noise sub-nodes only feed
    one; neither takes a latent, so neither is mistaken for the thing that ran.
    """
    ct = n.get('class_type', '') or ''
    if ct in SAMPLER_CLASSES:
        return True
    if 'sampler' not in ct.lower():
        return False
    inp = n.get('inputs', {}) or {}
    takes_latent = any(k in inp for k in ('latent_image', 'latent'))
    has_knob = any(k in inp for k in ('steps', 'seed', 'noise_seed', 'cfg'))
    return takes_latent and has_knob


# class_type -> the input key holding the model filename. EXACT AND CHEAP, and it stays for the
# same reason SAMPLER_CLASSES does: these are the classes worth naming. Anything else is judged on
# SHAPE by _loader_filename below.
LOADER_KEYS = {
    'CheckpointLoaderSimple': 'ckpt_name',
    'CheckpointLoader': 'ckpt_name',
    'ImageOnlyCheckpointLoader': 'ckpt_name',
    'UNETLoader': 'unet_name',
    'UnetLoaderGGUF': 'unet_name',
    'UnetLoaderGGUFAdvanced': 'unet_name',
}

# What a model file is called on disk. Used as the last gate on the shape test: a node can claim to
# be a loader and hold any string it likes, but the value has to look like a model.
MODEL_EXTS = ('.safetensors', '.ckpt', '.sft', '.gguf', '.pt', '.pth', '.bin')

# Input keys that hold the filename of THE MODEL, as against some other loadable thing. The
# exclusions matter more than the inclusions: `lora_name`, `vae_name`, `clip_name`,
# `control_net_name` and `style_model_name` all sit on nodes whose class names also say "loader".
_MODEL_KEYS = ('ckpt_name', 'unet_name', 'model_name', 'model_path', 'diffusion_model',
               'base_ckpt_name', 'checkpoint_name')
_NOT_MODEL_KEY_PREFIXES = ('lora', 'vae', 'clip', 'control', 'style', 'upscale', 'ipadapter',
                           'embedding', 'sampler', 'scheduler')


def _loader_filename(n):
    """The model filename this node loads, or None.

    A NAME LIST ALONE CANNOT WORK — the same lesson `_is_sampler_node` records, and the same
    silent failure: an unrecognised loader yields no model and no LoRAs, so the image lands under
    "(none)" and its Civitai export carries no resources. Nobody is told; it just looks like the
    picture has no model. Custom checkpoint loaders are as common as custom samplers (NF4, GGUF
    variants, Nunchaku, the "efficiency" packs, every workflow bundle with its own wrapper).

    So: the list first, then SHAPE. The class has to call itself a loader, and it has to carry a
    model-ish key whose value looks like a model file on disk. The extension check is what keeps a
    node that merely *names* a model — a string widget, a dropdown feeding something else — from
    being read as the thing that loaded it.
    """
    ct = n.get('class_type', '') or ''
    inp = n.get('inputs', {}) or {}
    if ct in LOADER_KEYS:
        v = inp.get(LOADER_KEYS[ct])
        return v if isinstance(v, str) else None
    low = ct.lower()
    if 'load' not in low:
        return None
    # CLASS-NAME EXCLUSIONS, because the key check alone is not enough: `UpscaleModelLoader` holds
    # its filename in `model_name`, which passes every other gate. It never appears in the diffusion
    # `model` chain, so _resolve_model can't reach it — but the any-loader-in-the-graph FALLBACK
    # iterates every node, and a workflow with an upscaler and an untraceable main chain would then
    # report "4x-UltraSharp" as the checkpoint. Wrong is worse than empty here: it also lands in the
    # Civitai export as the model.
    if any(w in low for w in ('upscale', 'vae', 'clip', 'controlnet', 'control_net', 'lora',
                              'ipadapter', 'style', 'embedding', 'preview', 'save')):
        return None
    for k in _MODEL_KEYS:
        v = inp.get(k)
        if not isinstance(v, str) or not v.strip():
            continue
        if k.split('_')[0] in _NOT_MODEL_KEY_PREFIXES:
            continue
        if v.lower().endswith(MODEL_EXTS):
            return v
    return None


def _is_lora_node(n):
    """Does this node apply a LoRA? Same reasoning as _loader_filename: the four class names this
    used to test for are not the four that exist. A node whose class says lora and which carries a
    `lora_name` string is one, whatever pack it came from. (rgthree's Power Lora Loader keeps its
    own branch at the call sites — it holds several LoRAs in one dict-shaped input.)"""
    ct = (n.get('class_type', '') or '').lower()
    return 'lora' in ct and isinstance((n.get('inputs', {}) or {}).get('lora_name'), str)
_LITERAL_KEYS = ('value', 'text', 'string', 'str', 'wildcard_text', 'populated_text')


def read_png_text_chunks(path):
    """Return {keyword: text} for all tEXt/zTXt/iTXt chunks in a PNG.

    Text-bearing chunks are read; every other chunk (notably the large IDAT
    pixel data) is *seeked* past, so we never load megabytes of pixels just to
    reach the metadata. Keeps scanning fast at tens of thousands of files.
    """
    out = {}
    with open(path, 'rb') as f:
        if f.read(8) != b'\x89PNG\r\n\x1a\n':
            return out
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            length, ctype = struct.unpack('>I4s', hdr)
            t = ctype.decode('latin-1')
            if t in ('tEXt', 'zTXt', 'iTXt'):
                data = f.read(length)
                f.read(4)  # CRC
                try:
                    if t == 'tEXt':
                        k, _, v = data.partition(b'\x00')
                        out[k.decode('latin-1')] = v.decode('latin-1')
                    elif t == 'zTXt':
                        k, _, rest = data.partition(b'\x00')
                        out[k.decode('latin-1')] = zlib.decompress(rest[1:]).decode('latin-1')
                    else:  # iTXt
                        k, _, rest = data.partition(b'\x00')
                        comp_flag = rest[0]
                        rest = rest[2:]
                        _, _, rest = rest.partition(b'\x00')  # language tag
                        _, _, rest = rest.partition(b'\x00')  # translated keyword
                        out[k.decode('latin-1')] = (zlib.decompress(rest) if comp_flag == 1 else rest).decode('utf-8')
                except Exception:
                    pass
            else:
                f.seek(length + 4, 1)  # skip chunk data + CRC without reading it
            if t == 'IEND':
                break
    return out


def _is_link(v):
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], (str, int))


def _resolve_string(g, ref, seen=None, depth=0):
    """Evaluate a string-producing node/link into its final text."""
    if depth > 64:
        return ''
    if isinstance(ref, str):
        return ref
    if not _is_link(ref):
        return ''
    nid = str(ref[0])
    if seen is None:
        seen = set()
    if nid in seen:
        return ''
    seen = seen | {nid}
    node = g.get(nid)
    if not node:
        return ''
    ct = node.get('class_type', '')
    inp = node.get('inputs', {})

    # Text input switches (e.g. Comfyroll 'CR Text Input Switch'): a selector picks ONE of several
    # textN / text_a.. inputs. Must honor the selector — otherwise the generic passthrough below
    # grabs the wrong branch (e.g. the disconnected grid instead of the chosen simple prompt).
    if 'Switch' in ct and any(k.lower().startswith('text') for k in inp):
        sel = None
        for sk in ('Input', 'input', 'select', 'select_input', 'index', 'switch'):
            if sk in inp:
                sel = _resolve_number(g, inp[sk])
                break
        if sel is None and 'boolean' in inp:            # 2-way boolean switch: true -> first input
            b = inp['boolean']
            sel = 1 if (b is True or b == 'true' or b == 1) else 2
        if sel is not None:
            n = int(sel)
            for key in (f'text{n}', f'text_{chr(96 + n)}' if 1 <= n <= 26 else None):
                if key and key in inp:
                    return _resolve_string(g, inp[key], seen, depth + 1)
        # selector unresolved -> fall through to the passthrough below (best effort)

    if 'Concatenate' in ct or 'Concat' in ct:
        delim = inp.get('delimiter', inp.get('separator', ''))
        if not isinstance(delim, str):
            delim = ''
        clean = inp.get('clean_whitespace') == 'true'
        parts = []
        for k in sorted(inp.keys()):  # text_a, text_b, ... / string_a, string_b
            if k in ('delimiter', 'separator', 'clean_whitespace'):
                continue
            v = inp[k]
            s = _resolve_string(g, v, seen, depth + 1) if _is_link(v) else (v if isinstance(v, str) else '')
            if s and s.strip():
                parts.append(s.strip() if clean else s)
        return delim.join(parts)

    if 'Replace' in ct:
        src = ''
        for k in ('text', 'string', 'value', 'text_a', 'string_a'):
            if k in inp:
                v = inp[k]
                src = _resolve_string(g, v, seen, depth + 1) if _is_link(v) else (v if isinstance(v, str) else '')
                break
        find = inp.get('find', inp.get('find1', ''))
        rep = inp.get('replace', inp.get('replace1', ''))
        if isinstance(find, str) and find:
            try:
                src = src.replace(find, rep if isinstance(rep, str) else '')
            except Exception:
                pass
        return src

    # literal-bearing node
    for k in _LITERAL_KEYS:
        if k in inp and isinstance(inp[k], str):
            return inp[k]

    # passthrough: follow the first string-yielding link input
    for k, v in inp.items():
        if _is_link(v):
            s = _resolve_string(g, v, seen, depth + 1)
            if s:
                return s
    return ''


def _encode_text(g, node):
    """Pull the text out of a CLIPTextEncode-like node (handles SDXL g/l)."""
    inp = node.get('inputs', {})
    if 'text' in inp:
        return _resolve_string(g, inp['text'])
    if 'text_g' in inp:
        tg = _resolve_string(g, inp['text_g'])
        tl = _resolve_string(g, inp.get('text_l', ''))
        return tg if tg == tl else (tg + (('\n' + tl) if tl and tl != tg else ''))
    if 'wildcard_text' in inp:
        return _resolve_string(g, inp['wildcard_text'])
    return ''


def _follow_to_text(g, ref, seen=None, depth=0):
    """From a conditioning link, walk to the CLIPTextEncode and return its text."""
    if depth > 64 or not _is_link(ref):
        return ''
    nid = str(ref[0])
    if seen is None:
        seen = set()
    if nid in seen:
        return ''
    seen = seen | {nid}
    node = g.get(nid)
    if not node:
        return ''
    ct = node.get('class_type', '')
    inp = node.get('inputs', {})
    if ct == 'ConditioningZeroOut':
        return ''  # zeroed conditioning = no effective prompt (common on Flux negatives)
    if 'CLIPTextEncode' in ct or 'text' in inp or 'text_g' in inp or 'wildcard_text' in inp:
        txt = _encode_text(g, node)
        if txt:
            return txt
    # passthrough conditioning nodes (FluxGuidance, ConditioningCombine, Reroute...)
    priority = ['conditioning', 'positive', 'cond', 'input', 'a', 'b']
    keys = sorted(inp.keys(), key=lambda k: (priority.index(k) if k in priority else 99, k))
    for k in keys:
        v = inp[k]
        if _is_link(v):
            r = _follow_to_text(g, v, seen, depth + 1)
            if r:
                return r
    return ''


# The chain is followed by more than one key name, and it has to be. On every one of the author's
# real files this returned None and the right answer arrived by luck through the any-loader fallback
# below -- because an rgthree Context node carries the model onward under `base_ctx`, and the walk
# stopped dead at the first one. A relay is a relay whatever it calls its input.
_MODEL_CHAIN_KEYS = ('model', 'guider', 'base_ctx', 'ctx', 'context', 'model_opt')


# A SAMPLER DOES NOT ALWAYS HOLD ITS OWN PROMPT. `KSampler` does, so the reader only ever looked
# there -- but `SamplerCustomAdvanced`, which is what the author's LTX and MiniMax video work runs
# on, takes a `guider` instead, and the positive, the negative and the model all sit on THAT.
# `CFGGuider` spells them positive/negative; `BasicGuider` carries one `conditioning` and no
# negative at all, which is correct for a model that has none.
#
# So the prompt is asked for where it actually is, following a bounded hop rather than assuming the
# one shape. Without this the saver walk lands on exactly the right sampler and still reads nothing
# off it, and the file falls back to the longest prompt in the graph -- which is the guess this
# whole change exists to remove.
_GUIDER_KEYS = ('guider', 'conditioning_source', 'cfg_guider')


def _sampler_conditioning(g, sid, depth=4):
    """(positive, negative) as they are wired on this sampler, following a guider when it has one.

    Either may be None. A reference is returned rather than text, so the caller resolves it exactly
    as it always has.
    """
    node = g.get(str(sid))
    seen = set()
    while node is not None and depth > 0:
        inp = node.get('inputs') or {}
        if 'positive' in inp or 'negative' in inp:
            return inp.get('positive'), inp.get('negative')
        if 'conditioning' in inp:                 # BasicGuider: one conditioning, no negative
            return inp.get('conditioning'), None
        nxt = None
        for k in _GUIDER_KEYS:
            v = inp.get(k)
            if _is_link(v) and str(v[0]) not in seen:
                nxt = str(v[0])
                break
        if nxt is None:
            return None, None
        seen.add(nxt)
        node = g.get(nxt)
        depth -= 1
    return None, None


def _resolve_model(g, start_id):
    """Follow the model input chain back to the root loader's filename."""
    cur = g.get(str(start_id))
    seen = set()
    for _ in range(64):
        if not cur:
            break
        inp = cur.get('inputs', {})
        val = _loader_filename(cur)
        if val:
            return val
        nxt = None
        for k in _MODEL_CHAIN_KEYS:
            m = inp.get(k)
            if _is_link(m) and str(m[0]) not in seen:
                nxt = str(m[0])
                break
        if nxt is None:
            break
        seen.add(nxt)
        cur = g.get(nxt)
    return None


# WHICH INPUT HOLDS THE FILENAME IS NOT SETTLED, and assuming `vae_name` would have shipped a
# feature that is blank on half of the author's video work. Checked against his actual install rather
# than guessed (`/object_info`, 2026-08-23): `VAELoader`, `VAELoaderKJ` and `AV_VAELoader` use
# `vae_name`; `WanVideoVAELoader` and `WanVideoTinyVAELoader` use **`model_name`**;
# `LTXVAudioVAELoader` uses `ckpt_name`; `OviMMAudioVAELoader` uses `vae`.
#
# So `vae_name` is trusted on ANY node, and the looser keys only on a node whose class says it is a
# VAE loader — `model_name` in particular is what half the checkpoint loaders in `_MODEL_KEYS` call
# their file, and accepting it everywhere would report the checkpoint as the VAE.
def _vae_named_by(node):
    inp = node.get('inputs') or {}
    v = inp.get('vae_name')
    if isinstance(v, str) and v.strip():
        return v
    low = (node.get('class_type') or '').lower()
    if 'vae' in low and 'loader' in low:
        for k in ('model_name', 'ckpt_name', 'vae'):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return None


def _vae_from_link(g, link):
    """Follow a `vae` input back to the loader that names a file."""
    seen = set()
    cur = g.get(str(link[0])) if _is_link(link) else None
    if cur is not None:
        seen.add(str(link[0]))
    for _ in range(64):
        if not cur:
            break
        named = _vae_named_by(cur)
        if named:
            return named
        cinp = cur.get('inputs') or {}
        nxt = None
        for k in ('vae', 'base_ctx', 'ctx', 'context'):   # same relay problem as the model chain
            v = cinp.get(k)
            if _is_link(v) and str(v[0]) not in seen:
                nxt = str(v[0])
                break
        if nxt is None:
            break
        seen.add(nxt)
        cur = g.get(nxt)
    return None


# CHOSEN BY WHAT IT DECODES, not by where it sits in the graph — the same rule `_pick_base_sampler`
# follows, and for the same reason: a node list is not ordered by the pipeline, so "the first
# VAELoader" is a coin toss wearing a rule.
#
# ONE VAE, on the author's instruction, and the newest video models make that a real choice rather than a
# formality: MiniMax H3 decodes video and audio through SEPARATE VAEs, so one graph holds two. The
# picture's decoder wins, because the picture is what you are looking at; an audio decoder names
# itself in its class and is skipped. Widening to both is a change to this function and the row that
# shows it — deliberately not a second column, until something needs to search on it.
#
# A checkpoint with a baked-in VAE yields NOTHING here, and that is correct rather than a gap: the
# chain ends at a loader naming a checkpoint, not a VAE. The row then hides, which reads as "this
# run didn't choose one" — printing the checkpoint's name would claim a fact the file never states.
def _is_vae_decode(ct, want_audio=False):
    """Is this class a VAE decoder? Compared with spaces and underscores removed and case ignored,
    because "CR VAE Decode" and "Vae Decode (mtb)" are both real, both in the author's install, and
    both invisible to the exact substring this used to test for."""
    low = (ct or '').lower().replace(' ', '').replace('_', '')
    if 'vaedecode' not in low:
        return False
    return want_audio or 'audio' not in low


def _resolve_vae(g, prefer=()):
    # THE DECODER THAT MADE THIS PICTURE, when the saver walk found one. Taken off the walk's own
    # path rather than searched for, so a graph holding two decoders cannot answer with the wrong
    # one -- which is the whole reason the audio exclusion below had to exist.
    for nid in (prefer or ()):
        node = g.get(str(nid))
        if not node:
            continue
        named = _vae_from_link(g, (node.get('inputs') or {}).get('vae'))
        if named:
            return named
    for node in g.values():
        if not _is_vae_decode(node.get('class_type', '')):
            continue
        named = _vae_from_link(g, (node.get('inputs') or {}).get('vae'))
        if named:
            return named
    # No decoder we can follow. Real cases: a decode node whose class spells it differently
    # ("CR VAE Decode", "Vae Decode (mtb)" — both real, both in the author's install), or a graph whose
    # decode is buried in a custom node. Any loader that names a VAE beats showing nothing — but
    # still never an AUDIO one, or a music workflow would report its audio VAE as the picture's.
    for node in g.values():
        if 'audio' in (node.get('class_type') or '').lower():
            continue
        named = _vae_named_by(node)
        if named:
            return named
    return None


# The same shortening the model and the LoRAs get: drop the folder and the extension.
def _vae_display_name(s):
    return _lora_display_name(s)


def _lora_display_name(s):
    base = s.replace('\\', '/').split('/')[-1]
    return os.path.splitext(base)[0]


_LORA_PATH_KEYS = ('lora', 'name', 'lora_name')
_LORA_STRENGTH_KEYS = ('str', 'strength', 'strength_model')
_LORA_OFF_KEYS = ('on', 'active', 'enabled')


def _lora_entries(v, depth=0):
    """The list of LoRA entry dicts inside one input value, whatever wrapper it arrived in.

    Three wrappers seen so far and the reason this is written as a shape rather than a list of
    them: a plain list, a dict holding the list under a private key (ComfyUI Lora Manager uses
    `__value__`), and a list encoded as one JSON string (the LTX stacker). A pack that bundles
    LoRAs is choosing one of these; it is not inventing a fourth kind of container.
    """
    if depth > 2:
        return []
    if isinstance(v, list):
        return [e for e in v if isinstance(e, dict)]
    if isinstance(v, dict):
        for k in ('__value__', 'value', 'loras'):
            if k in v:
                return _lora_entries(v[k], depth + 1)
        return []
    if isinstance(v, str):
        # Cheap gate before the parse: every graph carries long prompt strings, and JSON-decoding
        # each of them per node would be real work for nothing.
        if '"lora"' not in v and not ('"name"' in v and 'strength' in v):
            return []
        try:
            return _lora_entries(json.loads(v), depth + 1)
        except Exception:
            return []
    return []


def _lora_stack(inp):
    """LoRAs from a BUNDLE: one input holding several at once, as [{raw, strength}, ...].

    The third shape, after the single loader (`lora_name`) and rgthree's Power Lora Loader
    (`lora_1..N` as dicts). Found 2026-09-05 on `LTX_lora_loader` ("LoRA Loader Stack (LTX /
    MiniMax H3 Compatible)"), which carries no `lora_name` at all and keeps its whole list encoded
    in one `stack_data` string -- so every LoRA in a MiniMax video run was invisible, in the viewer
    AND in the sidecar the saver writes, because both ask this module the same question.

    MATCHED BY SHAPE, NOT BY NODE NAME. The class name and the input key are both the pack's
    choices; the convention -- a list of entries each naming a LoRA and a strength -- is the part
    another pack is likely to copy. Same reasoning as _is_lora_node: the four class names that used
    to be hardcoded were never the four that exist.

    Widened 2026-09-17 for ComfyUI Lora Manager's loader, which keeps a REAL list rather than one
    written out as text, names the file under `name`, and flags each entry `active` instead of
    `on`. Every one of those is a spelling of something already handled, so all three are now read
    as alternatives rather than as a new case.

    An entry that says nothing about being disabled is not disabled, so an absent flag means on.
    """
    out = []
    for v in inp.values():
        for e in _lora_entries(v):
            raw = next((e[k] for k in _LORA_PATH_KEYS
                        if isinstance(e.get(k), str) and e[k]), None)
            if not raw:
                continue
            if any(k in e and not e[k] for k in _LORA_OFF_KEYS):
                continue
            strength = next((e[k] for k in _LORA_STRENGTH_KEYS if e.get(k) is not None), None)
            out.append({'raw': raw, 'strength': strength})
    return out


def _extract_loras(g):
    """Collect the ACTIVE LoRAs used, as [{name, strength}], de-duplicated."""
    out = []
    for node in g.values():
        ct = node.get('class_type', '')
        inp = node.get('inputs', {})
        if _is_lora_node(node):
            name = inp.get('lora_name')
            if isinstance(name, str):
                out.append({'name': _lora_display_name(name),
                            'strength': inp.get('strength_model', inp.get('strength'))})
        elif 'Power Lora Loader' in ct:  # rgthree: lora_1..N = {on, lora, strength}
            for k, v in inp.items():
                if k.startswith('lora_') and isinstance(v, dict) and v.get('on') and v.get('lora'):
                    out.append({'name': _lora_display_name(v['lora']), 'strength': v.get('strength')})
        elif 'lora' in ct.lower():       # a stacker: the whole list in one JSON string
            for e in _lora_stack(inp):
                out.append({'name': _lora_display_name(e['raw']), 'strength': e['strength']})
    seen, res = set(), []
    for l in out:
        if l['name'] not in seen:
            seen.add(l['name'])
            res.append(l)
    return res


# Keys the A1111 settings line can carry (the trailing `Steps: 20, Sampler: Euler, ...` row).
# Used only to RECOGNISE that line — a prompt whose last line happens to contain a colon must not
# be mistaken for settings and swallowed.
_A1111_SETTING_KEYS = ('steps', 'sampler', 'cfg scale', 'seed', 'size', 'model',
                       'model hash', 'lora hashes', 'denoising strength', 'clip skip')
_LORA_TAG_RE = re.compile(r'<lora:([^:>]+)(?::([^>]*))?>', re.I)


def _take_civitai_resources(line):
    """Lift a `Civitai resources: [...]` array out of a settings line.

    Returns (line_without_it, parsed_list). WHY IT HAS TO COME OUT FIRST: the settings splitter
    below breaks on commas outside double quotes, and this value is JSON — `[{"type":"checkpoint",
    "modelVersionId":479474,...}]` has commas between quoted pairs, so the split shreds it into a
    dozen nonsense keys ("weight", {"type", "modelversionname"). That is what an image downloaded
    from Civitai looked like to this parser, which is why it reported a prompt, steps, sampler, CFG
    and seed, and no model at all.

    BRACKET-MATCHED, NOT A REGEX. A lazy `\\[.*?\\]` stops at the first `]`, and these arrays hold
    nested objects; a greedy one swallows `Civitai metadata: {...}` when it follows. Counting
    depth is the only thing that ends in the right place.
    """
    i = line.lower().find('civitai resources:')
    if i == -1:
        return line, []
    start = line.find('[', i)
    if start == -1:
        return line, []
    depth, quoted, end = 0, False, -1
    for j in range(start, len(line)):
        ch = line[j]
        if ch == '"' and line[j - 1] != '\\':
            quoted = not quoted
        elif not quoted:
            if ch in '[{':
                depth += 1
            elif ch in ']}':
                depth -= 1
                if depth == 0:
                    end = j
                    break
    if end == -1:
        return line, []
    blob = line[start:end + 1]
    rest = (line[:i] + line[end + 1:])
    # The comma that separated this field from the next goes with it, or the caller is left with
    # ", , Size: ..." and an empty pair in the middle.
    rest = re.sub(r',\s*,', ',', rest).strip().strip(',').strip()
    try:
        data = json.loads(blob)
    except Exception:
        return rest, []                     # malformed: drop it rather than guess at it
    return rest, data if isinstance(data, list) else []


def _from_civitai_resources(items):
    """What a Civitai resource list can tell us: the checkpoint, the LoRAs and the VAE.

    This is the ONLY record of the model in an image downloaded from Civitai -- those files carry no
    `Model:` field at all, so every one of them showed a prompt and settings while its checkpoint
    went unread and it never appeared under a model in the rail.

    THE NAME IS TAKEN VERBATIM, version and all left off. `modelName` is what the model is called on
    Civitai and what its page is titled, so it is the name that will mean something to the person
    looking at it -- even when the uploader put an emoji in it. The VERSION is deliberately dropped:
    folding "v4.0" into the name would file two versions of one checkpoint as two models, which is
    the opposite of what the Model filter is for.

    `embed` entries are ignored: the app has no concept of an embedding, and a list it cannot act on
    is noise. They stay in the file for whenever it does.
    """
    out = {'loras': []}
    for r in items or []:
        if not isinstance(r, dict):
            continue
        kind = (r.get('type') or '').strip().lower()
        name = (r.get('modelName') or '').strip()
        if not name:
            continue
        if kind == 'checkpoint' and not out.get('model_name'):
            out['model_name'] = name
        elif kind == 'lora':
            out['loras'].append({'name': name, 'raw': name, 'strength': _as_float(r.get('weight'))})
        elif kind == 'vae' and not out.get('vae_name'):
            out['vae_name'] = name
    if not out['loras']:
        out.pop('loras')
    return out


def _split_a1111_settings(line):
    """Split an A1111 settings line into {key: value}, respecting quoted values.

    `Lora hashes: "a: h1, b: h2"` contains both commas and colons inside its quotes, so a naive
    split on ',' then ':' shreds it — which is how the LoRA names would go missing."""
    out, buf, quoted = {}, [], False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        if ch == ',' and not quoted:
            out.update(_one_a1111_pair(''.join(buf)))
            buf = []
            continue
        buf.append(ch)
    out.update(_one_a1111_pair(''.join(buf)))
    return out


def _one_a1111_pair(chunk):
    k, sep, v = chunk.partition(':')
    if not sep:
        return {}
    return {k.strip().lower(): v.strip().strip('"')}


def parse_a1111(text):
    """Parse an A1111 `parameters` block back into the fields extract() reports.

    The inverse of _format_parameters, and the reason it exists: the VV saver writes this chunk
    from values the user WIRED, so it carries a model and a prompt for workflows the graph walker
    can't follow at all — a video model with no CLIPTextEncode, an unfamiliar loader, an API node.
    Those images used to show nothing, while the answer sat in the file unread.

    Layout, per _format_parameters:
        <positive> <lora tags>
        Negative prompt: <negative>        (omitted entirely when there is none)
        Steps: 20, Sampler: Euler, CFG scale: 7, Seed: 1, Size: 512x512, Model: foo, ...

    Returns {} for anything that isn't recognisably that shape, so a hand-written or foreign
    `parameters` chunk degrades to "no metadata" rather than to wrong metadata.
    """
    if not text or not text.strip():
        return {}
    lines = text.strip().split('\n')
    settings = {}
    # The settings row is the LAST line, and only if it actually looks like one. Checking rather
    # than assuming: an image whose prompt ends "lighting: dramatic" would otherwise lose that line.
    # A Civitai download's resource list comes out BEFORE the split, or its JSON commas shred the
    # whole settings row -- see _take_civitai_resources. Taken from the last line only, which is
    # where the settings live; a prompt that happens to mention the words is untouched.
    lines[-1], _civitai = _take_civitai_resources(lines[-1])
    tail = _split_a1111_settings(lines[-1])
    if any(k in _A1111_SETTING_KEYS for k in tail):
        settings = tail
        lines = lines[:-1]
    elif _civitai and not lines[-1].strip():
        # The row held the resources and nothing else we know; it is still a settings row, and
        # leaving it in would append a line of leftovers to the prompt.
        lines = lines[:-1]
    body = '\n'.join(lines)
    neg = None
    idx = body.find('\nNegative prompt:')
    if body.startswith('Negative prompt:'):
        idx = 0
    if idx != -1:
        head = body[:idx]
        neg = body[idx:].split(':', 1)[1].strip() or None
        body = head
    pos = body.strip()
    # LoRAs ride in the positive as <lora:name:weight> tags; lift them out so they populate the
    # LoRAs list rather than sitting as noise in the prompt text, exactly as the graph path does.
    loras = [{'name': m.group(1).strip(), 'raw': m.group(1).strip(),
              'strength': _as_float(m.group(2))} for m in _LORA_TAG_RE.finditer(pos)]
    pos = _LORA_TAG_RE.sub('', pos).strip()
    out = {}
    if pos:
        out['positive'] = pos
    if neg:
        out['negative'] = neg
    if settings.get('model'):
        out['model_name'] = settings['model']
    # UNDER `Model:`, NEVER OVER IT. A file carrying both is telling us the same thing twice, and
    # the plain field is the one every other tool writes and reads; the resource list is the
    # fallback for the files that have no such field, which is every Civitai download.
    for k, v in _from_civitai_resources(_civitai).items():
        if v and not out.get(k):
            out[k] = v
    # A1111 blocks often name a VAE, and this is the ONLY route to one for a downloaded JPEG/WebP
    # or for a video reading its `.txt` sidecar — neither has a graph to walk.
    if settings.get('vae'):
        out['vae_name'] = _vae_display_name(str(settings['vae']))
    if loras:
        out['loras'] = loras
    # The settings row already had to be parsed to find the model, so lifting the rest out is free.
    # This is the only route to generation settings for a file with no walkable graph — which is
    # every VIDEO carrying a .txt sidecar, and every image whose workflow the tracer can't follow.
    if settings.get('steps') is not None:
        out['steps'] = _as_int(settings['steps'])
    if settings.get('cfg scale') is not None:
        out['cfg'] = _as_float(settings['cfg scale'])
    if settings.get('seed') is not None:
        out['seed'] = _as_int(settings['seed'])
    if settings.get('sampler'):
        # A1111 writes sampler and scheduler as one field ("Euler a", "DPM++ 2M Karras"); the
        # trailing word is the scheduler when it is one we recognise. Split rather than storing a
        # blended string, or the same run reads differently depending which source answered.
        name, sched = _split_a1111_sampler(settings['sampler'])
        out['sampler_name'] = name
        if sched:
            out['scheduler'] = sched
    return out


# Schedulers A1111 appends to the sampler name. Kept small on purpose: an unknown trailing word is
# far more likely to be part of the sampler's own name than a scheduler we've never heard of.
_A1111_SCHEDULERS = ('karras', 'exponential', 'normal', 'simple', 'ddim_uniform', 'beta',
                     'sgm_uniform', 'linear_quadratic')


def _split_a1111_sampler(s):
    """"DPM++ 2M Karras" -> ("DPM++ 2M", "karras"). Returns (name, scheduler-or-None)."""
    s = (s or '').strip()
    if not s:
        return None, None
    parts = s.split()
    if len(parts) > 1 and parts[-1].lower() in _A1111_SCHEDULERS:
        return ' '.join(parts[:-1]), parts[-1].lower()
    return s, None


def _as_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _as_int(s):
    # Via float so "20.0" survives; a seed can exceed 2^53 so keep it whole rather than rounding.
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        pass
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------
# The run code — a generation's identity, carried in the FILENAME.
#
# Every image of one run is already stamped with a shared `vv_set_id`, which is how sets group
# exactly instead of by guessing at filenames. That id lives in a PNG text chunk, and a video has
# nowhere to put one — which is why a video could only ever reach a group through a filename
# coincidence, and why naming a video's sidecar meant knowing a counter nobody can predict.
#
# The code is the same fact moved somewhere every file type can carry it. Files sharing one are the
# same generation, and EVERYTHING AFTER IT IS NOISE — stage, ComfyUI's counter, VHS's `-audio`, the
# `.txt` extension. That is what removes the counter problem rather than solving it: a sidecar is
# named up to the code and stops, so it can be written before the video it describes exists.
#
# Two properties are load-bearing:
#
#   `~vv` marker, not a bare token.  A 6-character code alone (`~k3n9x7`) would be matched by an
#   ordinary filename that happens to contain a tilde, and the cost of a false positive is two
#   unrelated pictures silently merging into one card in a library nobody wants re-grouped. The
#   marker makes an accidental match essentially impossible, and it is greppable.
#
#   Derived from the CLOCK, not from dice.  Six base36 characters of random would collide about
#   once per 45,000 pairs of runs — roughly 3% across a folder of 300, i.e. once or twice a year,
#   silently. Seconds-since-2020 cannot collide at all unless two runs finish in the same second,
#   which a generation taking longer than a second rules out. It also sorts chronologically for
#   free. Six characters lasts until 2088.
CODE_EPOCH = 1577836800            # 2020-01-01 UTC — only ever moves forward, never re-base it
CODE_MARK = '~vv'
_CODE_RE = re.compile(r'~vv([0-9a-z]{4,10})(?![0-9a-z])', re.I)


def make_run_code(when=None):
    """The code for a run happening at `when` (epoch seconds, default now)."""
    n = max(0, int(time.time() if when is None else when) - CODE_EPOCH)
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return CODE_MARK + (out or '0').rjust(6, '0')


def run_code_key(name):
    """The part of `name` up to and including its run code, lowercased — or None if it has none.

    This is the whole matching rule: two files belong to the same generation when this is equal.
    Truncating (rather than returning the code alone) keeps the series and date in the key, so it
    reads as a name in a debug dump and a bare code can never be compared out of context.
    """
    base = os.path.basename(name or '')
    m = _CODE_RE.search(base)
    return base[:m.end()].lower() if m else None


# What the VV Naming node was told to call this run, recovered from the filename it produced:
# `<name>_<HH-MM-SS>~vvCODE_00001.mp3` -> `<name>`. Used as a song's headline, since nothing inside
# an audio file names it. Every part after the name is machine-written and fixed in shape, so this
# strips from the right and keeps whatever is left — a file that follows none of the conventions
# keeps its whole stem, which is still the best name available for it.
_NAME_TAIL_RE = re.compile(r'(?:_\d{5,})?(?:~vv[0-9a-z]{4,10})?(?:_\d{2}-\d{2}-\d{2})?$', re.I)


def name_from_filename(path):
    """The human-chosen part of a generated filename, or None if nothing is left of it."""
    stem = os.path.splitext(os.path.basename(path or ''))[0]
    prev = None
    while stem and stem != prev:          # the tails appear in a fixed order; peel until stable
        prev = stem
        stem = _NAME_TAIL_RE.sub('', stem).rstrip('_-. ')
    return stem or None


# A sidecar may name the run it belongs to. Matched on its OWN line as well as inside the settings
# row, because the first thing anyone does with this format is write one by hand.
_SIDECAR_KEYS = {'set_id': re.compile(r'^[ \t]*vv[ _]*set[ _]*id[ \t]*:[ \t]*(.+?)[ \t]*$', re.I | re.M),
                 'set_stage': re.compile(r'^[ \t]*vv[ _]*set[ _]*stage[ \t]*:[ \t]*(.+?)[ \t]*$', re.I | re.M)}
SIDECAR_MAX_BYTES = 64 * 1024      # a metadata note, not a document; refuse to slurp a stray log

# ComfyUI's `_00001_` and VHS's `_00001`, with VHS's hardcoded `-audio` suffix if present. Stripped
# from a stem before looking for its sidecar, so the text file does not have to predict a counter.
# FIVE digits minimum, not three: both writers zero-pad to five (`{counter:05}`), while three or
# four would also match a year, so `holiday_2024.mp4` would start hunting for `holiday.txt`.
_COUNTER_RE = re.compile(r'_\d{5,}_?(-audio)?$', re.I)


def sidecar_path(path):
    """The sidecar this file would use, or None. Candidates, most specific first:

      1. `clip.mp4.txt`  — unambiguous; can never collide with a real `clip.txt`.
      2. `clip.txt`      — what a generic 'save text' node produces from the same prefix.
      3. the stem with its trailing counter dropped — so a writer that never learns the counter
         (which is every writer, see the run code above) can still name the file.
      4. the stem truncated at its run code — ONE sidecar for a whole generation, found from any
         of its videos, whatever stage or role word each carries.

    All four are constructed names, never a directory listing: this runs once per video on every
    scan, and the library lives on a network share where a listing is a real round trip.
    """
    stem = os.path.splitext(path)[0]
    cands = [path + '.txt', stem + '.txt']
    trimmed = _COUNTER_RE.sub('', stem)
    if trimmed != stem:
        cands.append(trimmed + '.txt')
    m = _CODE_RE.search(os.path.basename(stem))
    if m:
        cands.append(os.path.join(os.path.dirname(stem),
                                  os.path.basename(stem)[:m.end()]) + '.txt')
    for cand in cands:
        try:
            if os.path.isfile(cand):
                return cand
        except OSError:
            pass
    return None


# ---- a video's OWN metadata -------------------------------------------------------------------
# A long-standing assumption in this codebase is that a video carries nothing and needs a `.txt`
# sidecar (see read_sidecar below). That is FALSE for anything ComfyUI saved: its video output
# carries the same two graphs a PNG does — `workflow` (the UI graph, what a drop into ComfyUI
# restores) and `prompt` (the API graph, what this module traces) — and they are simply somewhere
# this code never looked.
#
# Confirmed 2026-08-12 against a real MiniMax H3 file: 22KB of `workflow`, 6.6KB of `prompt`, and
# feeding that `prompt` straight to _from_graph yielded the model, steps, sampler, scheduler and
# seed with no new parsing at all.
#
# WHERE IT LIVES: an MP4/MOV keeps them in `moov > udta > meta`, as a `keys` box naming each entry
# and an `ilst` box holding the values in the same order. That is the QuickTime-style layout, which
# is what ComfyUI's SaveVideo writes.
#
# NOT COVERED: WebM/Matroska, which stores tags in an entirely different structure. Returns {} for
# those rather than pretending — so a caller must treat "no metadata" as "unknown", never as "none".
VIDEO_META_EXTS = {'.mp4', '.mov', '.m4v'}
# How much of the file to read looking for `moov`. TWO STEPS, because this now runs once per video
# during a scan and those files are on network shares: read a small head first and only reach for
# more if `moov` is not wholly inside it. On a real MiniMax file the metadata ended 29KB in, so the
# small read answers it — the large one exists for a file written without faststart, where `moov`
# can sit anywhere. Neither ever pulls the whole clip.
_VIDEO_META_HEAD = 256 * 1024
# How much of `moov` to take when it is found by walking. It holds the workflow JSON, so it is
# not tiny, but it is never the media -- 16MB is far past any real one and still bounded.
_MOOV_MAX = 16 * 1024 * 1024


def _mp4_walk_to_moov(f, total):
    """`moov`'s bytes, by SEEKING the top-level box chain. None when it isn't there.

    THE CASE THIS EXISTS FOR, found on the author's own library 2026-08-25: two LTX runs made minutes
    apart, one reporting its length and dimensions and the other reporting nothing at all. Nothing
    about the runs differed. The files were 3.4MB and 4.5MB, and the reader gave up after 4MB.

    An MP4 may keep `moov` before the media (what "faststart" means) or after it, and ComfyUI's
    video output puts it AFTER. So the metadata sits near the END of the file, at an offset that is
    simply however big the clip happens to be -- which makes a prefix read a coin toss decided by
    the length of the video, and silently: a file over the limit reports no model, no seed, no
    workflow, no dimensions and no duration, exactly as if it had never carried any.

    Seeking the chain costs a 16-byte read per top-level box -- typically four, `ftyp free mdat
    moov` -- and then one read of `moov` itself. The media is never touched at any size. That is
    both cheaper than the 4MB read it replaces and correct for a clip of any length.

    `moov` is capped at _MOOV_MAX. Anything larger is read up to the cap rather than abandoned: the
    boxes that carry the length and the dimensions sit at its start, so a truncated buffer still
    answers those, and every reader here already guards its own bounds.
    """
    off = 0
    while off + 8 <= total:
        f.seek(off)
        hdr = f.read(16)
        if len(hdr) < 8:
            return None
        size = struct.unpack('>I', hdr[0:4])[0]
        typ = hdr[4:8]
        if size == 1:                                   # 64-bit extended size
            if len(hdr) < 16:
                return None
            size = struct.unpack('>Q', hdr[8:16])[0]
        elif size == 0:                                 # "to end of file"
            size = total - off
        if size < 8 or off + size > total:
            return None                                 # a broken chain is not an answer
        if typ == b'moov':
            f.seek(off)
            return f.read(min(size, _MOOV_MAX)), size
        off += size
    return None

def _mp4_find_box(buf, want, start=0, end=None):
    """First child box of `want` type within [start, end). Returns (offset, size) or None."""
    end = len(buf) if end is None else end
    off = start
    while off + 8 <= end:
        size = struct.unpack('>I', buf[off:off + 4])[0]
        typ = buf[off + 4:off + 8]
        if size == 1:                                   # 64-bit extended size
            if off + 16 > end:
                return None
            size = struct.unpack('>Q', buf[off + 8:off + 16])[0]
        elif size == 0:                                 # "to end of file"
            size = end - off
        if size < 8 or off + size > end:
            return None
        if typ == want:
            return (off, size)
        off += size
    return None


def _read_video_boxes(path):
    """`(head, moov)` for an MP4/MOV, or `(None, None)`. Never raises.

    ONE bounded read, shared by every reader below. It is a helper rather than inline code because
    two different questions are now asked of the same bytes — what a ComfyUI video recorded, and how
    long the clip runs — and a video lives on a network share, where opening the file twice to
    answer both would be the whole cost of the feature.
    """
    try:
        if os.path.splitext(path)[1].lower() not in VIDEO_META_EXTS:
            return None, None
        # A small head read answers a faststart file outright, which costs one read and no seeks.
        with open(path, 'rb') as f:
            head = f.read(_VIDEO_META_HEAD)
            moov = _mp4_find_box(head, b'moov')
            if moov and moov[0] + moov[1] <= len(head):
                return head, moov
            # Not there, or not all of it. WALK to it rather than reading a bigger prefix: the
            # metadata may sit after the media, at an offset that is just however long the clip is,
            # and no fixed prefix can be the right size for that. See _mp4_walk_to_moov.
            got = _mp4_walk_to_moov(f, os.path.getsize(path))
        if not got:
            return None, None
        buf, size = got
        # The buffer IS the moov box now, so it starts at 0. `size` is what the box CLAIMS, which
        # can exceed the buffer when it was capped -- every reader compares the two before trusting
        # an offset, so a partial box answers what it can and nothing more.
        return buf, (0, size)
    except Exception:
        return None, None


def _mp4_duration(head, moov):
    """Seconds, from `mvhd` — a direct child of `moov`. None when it cannot be read.

    THE POINT OF READING IT HERE, rather than anywhere in the tag walk below: `mvhd` is present in
    every conforming MP4/MOV, where the ComfyUI tags are present in almost none. A clip downloaded
    or exported from anything else still has a duration, and reading it beside the tags would lose
    it to the first `return {}`.

        version(1) flags(3), then
        v0: creation(4) modification(4) timescale(4) duration(4)
        v1: creation(8) modification(8) timescale(4) duration(8)

    A v1 `duration` of 2**64-1 means "unknown" by spec; a zero timescale would divide by zero. Both
    read as no answer rather than as a wrong one.
    """
    try:
        mvhd = _mp4_find_box(head, b'mvhd', moov[0] + 8, moov[0] + moov[1])
        if not mvhd or mvhd[0] + mvhd[1] > len(head):
            return None
        off = mvhd[0] + 8
        version = head[off]
        if version == 1:
            timescale = struct.unpack('>I', head[off + 20:off + 24])[0]
            dur = struct.unpack('>Q', head[off + 24:off + 32])[0]
            if dur == 0xFFFFFFFFFFFFFFFF:
                return None
        else:
            timescale = struct.unpack('>I', head[off + 12:off + 16])[0]
            dur = struct.unpack('>I', head[off + 16:off + 20])[0]
            if dur == 0xFFFFFFFF:
                return None
        if not timescale or not dur:
            return None
        secs = dur / float(timescale)
        return secs if 0 < secs < 86400 * 7 else None   # a week is not a clip; that is a misparse
    except Exception:
        return None


def _mp4_dims(head, moov):
    """`(width, height)` in pixels, from the video track's `tkhd`. `(None, None)` when unreadable.

    READ HERE FOR THE SAME REASON AS THE DURATION ABOVE, and it matters more. A video's dimensions
    were never captured during a scan at all -- Pillow cannot size a video -- so they were
    backfilled from the poster frame the first time a thumbnail was made. That works for a video
    with its own card and never fires for one paired with a still, because a pair card is fronted
    by the STILL and the video's thumbnail is therefore never asked for. The result was that the
    videos the author generates most carried no dimensions anywhere, permanently.

    `tkhd` is mandatory in every conforming MP4/MOV and sits inside a `trak` we have already read,
    so this costs nothing beyond the bytes in hand -- no decoder, no subprocess, no second open.

        version(1) flags(3), then
        v0: creation(4) modification(4) track_ID(4) reserved(4) duration(4)   = 20
        v1: creation(8) modification(8) track_ID(4) reserved(4) duration(8)   = 32
        then reserved(8) layer(2) alternate_group(2) volume(2) reserved(2) matrix(36)
        then width(4) height(4), both 16.16 FIXED POINT -- hence the >> 16.

    A file has one `trak` per track and an AUDIO track's `tkhd` is 0x0, which is what picking the
    first NON-ZERO pair means: it selects the video track without having to identify it.

    One honest limit, and it is why the poster-frame backfill above is left in place rather than
    removed: `tkhd` carries DISPLAY dimensions. A clip flagged anamorphic reports its stretched
    size (verified: a 640x360 encode tagged 32:9 reads 1280x360), where a rotated one does not --
    rotation lives in the matrix, not the dimensions, so a 90-degree clip still reads 640x360.
    Neither shape comes out of ComfyUI, and when a poster frame is ever made the true frame size
    overwrites this. So this is the answer that is always available, and ffmpeg's is the one that
    corrects it.
    """
    try:
        end = moov[0] + moov[1]
        off = moov[0] + 8
        while off + 8 <= end and off + 8 <= len(head):
            size = struct.unpack('>I', head[off:off + 4])[0]
            typ = head[off + 4:off + 8]
            if size < 8:
                break
            if typ == b'trak':
                tkhd = _mp4_find_box(head, b'tkhd', off + 8, min(off + size, end))
                if tkhd and tkhd[0] + tkhd[1] <= len(head):
                    o = tkhd[0] + 8
                    base = 32 if head[o] == 1 else 20
                    p = o + 4 + base + 52
                    if p + 8 <= len(head):
                        w = struct.unpack('>I', head[p:p + 4])[0] >> 16
                        h = struct.unpack('>I', head[p + 4:p + 8])[0] >> 16
                        # 0x0 is an audio track; a nonsense size is a misparse, not an answer.
                        if 0 < w <= 65535 and 0 < h <= 65535:
                            return w, h
            off += size
    except Exception:
        pass
    return None, None


def _mp4_ilst_comment(head, ilst):
    """ComfyUI's graphs out of an ffmpeg-written MP4, where they ride in the comment atom.

    The atom is `©cmt` -- 0xA9 't' 'm' 'c' ... in file order `\\xa9cmt` -- holding a `data` child
    whose payload, past its 8 bytes of type and locale, is a JSON object:

        {"prompt": "{\\"1\\": {\\"inputs\\": …}}", "workflow": "{\\"nodes\\": …}"}

    Both values are JSON *strings* containing more JSON, which is how ComfyUI writes them and why
    they are handed back as strings here: the caller feeds them to the same tracer a PNG's chunks
    go to, and it parses them itself.

    ONLY STRING VALUES ARE KEPT. A comment that is not this shape -- a real comment someone typed,
    an unrelated tool's JSON -- yields nothing rather than a field full of the wrong thing.
    """
    out = {}
    off, stop = ilst[0] + 8, ilst[0] + ilst[1]
    while off + 8 <= stop:
        size = struct.unpack('>I', head[off:off + 4])[0]
        name = head[off + 4:off + 8]
        if size < 8 or off + size > stop:
            break
        if name == b'\xa9cmt':
            data = _mp4_find_box(head, b'data', off + 8, off + size)
            if data:
                # `data`: size(4) type(4) then 4 bytes of type-set + 4 of locale before the payload.
                payload = head[data[0] + 16: data[0] + data[1]]
                try:
                    blob = json.loads(payload.decode('utf-8'))
                except (UnicodeDecodeError, ValueError):
                    return {}
                if isinstance(blob, dict):
                    for k in ('prompt', 'workflow'):
                        v = blob.get(k)
                        if isinstance(v, str) and v.strip():
                            out[k] = v
            break
        off += size
    return out


def _mp4_tags(head, moov):
    """The `workflow` / `prompt` JSON strings a ComfyUI video carries, as {key: str}.

    Returns {} for a video that genuinely has no metadata — deliberately indistinguishable from a
    format we cannot read, because acting on the difference would mean guessing.
    """
    try:
        udta = _mp4_find_box(head, b'udta', moov[0] + 8, moov[0] + moov[1])
        if not udta:
            return {}
        meta = _mp4_find_box(head, b'meta', udta[0] + 8, udta[0] + udta[1])
        if not meta:
            return {}
        # `meta` is a FULL box: 4 bytes of version/flags before its children.
        inner, inner_end = meta[0] + 12, meta[0] + meta[1]
        keys = _mp4_find_box(head, b'keys', inner, inner_end)
        ilst = _mp4_find_box(head, b'ilst', inner, inner_end)
        if not ilst:
            return {}
        # TWO DIALECTS LIVE IN THE SAME BOX, and reading only one of them made every video VHS
        # writes look like a file with no metadata at all -- duration and dimensions and nothing
        # else. VHS is the most used video node in ComfyUI, so that was most people's video.
        #
        # QuickTime indexes its tags: a `keys` box lists the names and each `ilst` entry points at
        # one by number. That is the form below, and the form this only ever read.
        # ffmpeg, which is what VHS writes through, has no `keys` box at all. It puts the whole
        # payload in the standard COMMENT atom, and ComfyUI's is a JSON object wrapping the same
        # two graphs a PNG carries:  ©cmt -> {"prompt": "…", "workflow": "…"}
        if not keys:
            return _mp4_ilst_comment(head, ilst)
        # keys: version/flags(4) + count(4), then [size(4) namespace(4) name…] per entry.
        off = keys[0] + 12
        count = struct.unpack('>I', head[off:off + 4])[0]
        off += 4
        names = []
        for _ in range(count):
            if off + 8 > keys[0] + keys[1]:
                break
            size = struct.unpack('>I', head[off:off + 4])[0]
            if size < 8:
                break
            names.append(head[off + 8:off + size].decode('utf-8', 'replace'))
            off += size
        # ilst: [size(4) index(4)] then a `data` box whose payload starts 16 bytes in (the data box
        # header plus its type and locale fields). The index is 1-based into `names`.
        out = {}
        off, stop = ilst[0] + 8, ilst[0] + ilst[1]
        while off + 16 <= stop:
            size = struct.unpack('>I', head[off:off + 4])[0]
            idx = struct.unpack('>I', head[off + 4:off + 8])[0]
            dsize = struct.unpack('>I', head[off + 8:off + 12])[0]
            if size < 16 or dsize < 16 or off + size > stop:
                break
            if 0 < idx <= len(names):
                payload = head[off + 8 + 16: off + 8 + dsize]
                try:
                    out[names[idx - 1]] = payload.decode('utf-8')
                except UnicodeDecodeError:
                    pass                                # a binary tag (e.g. `encoder`) — not for us
            off += size
        return out
    except Exception:
        return {}


def read_video_meta(path):
    """The `workflow` / `prompt` JSON strings a ComfyUI video carries, as {key: str}. Never raises."""
    return read_video_file(path)[0]


def read_video_file(path):
    """`(tags, duration_seconds, (width, height))` from ONE read of the file.

    All three answers come off the same `moov`, and they are returned together because they are
    found together — a caller that wanted them separately would open the file twice.
    """
    head, moov = _read_video_boxes(path)
    if moov is None:
        return {}, None, (None, None)
    return _mp4_tags(head, moov), _mp4_duration(head, moov), _mp4_dims(head, moov)


def extract_video(path):
    """Everything we can learn about a VIDEO: its `.txt` sidecar first, then its own embedded graph
    for whatever the sidecar left empty.

    The video's counterpart to extract(). Same result shape, same tracer — a ComfyUI video's `prompt`
    tag holds the identical JSON a PNG's `prompt` chunk does, so _from_graph reads it unchanged.

    PRECEDENCE: THE SIDECAR WINS ON EVERYTHING IT HAS. The graph only fills blanks.

    I had this the other way round first — graph first, sidecar overriding only the prompt — and the
    safety test caught it immediately: on a video whose sidecar and graph disagree, the seed, steps,
    sampler, cfg and model all changed. That is precisely the thing this feature is not allowed to
    do. **A library showing a value today must show the same value afterwards**; reading a video's
    graph may only fill what was blank. The rule carries over from the generation-settings work and
    outranks any argument about which source is "better".

    It is also right on the merits. The sidecar is what the run DELIBERATELY recorded — including
    the prompt you wired into the VV Naming node, which the video cannot yield at all where the
    tracer can't follow a workflow's text nodes. That is not hypothetical: a real MiniMax H3 file
    gives up its model, steps, sampler, scheduler and seed, and not its prompt.

    Pinned by test_video_meta.py, which builds a sidecar disagreeing on every shared field.
    """
    res = read_sidecar(path)
    if res is None:
        res = {'loras': [], 'positive': None, 'negative': None,
               'model': None, 'model_name': None, 'method': None,
               'set_id': None, 'set_stage': None, 'vae_name': None,
               'steps': None, 'cfg': None, 'sampler_name': None, 'scheduler': None, 'seed': None}
        had_sidecar = False
    else:
        had_sidecar = True
    tags, duration, (vw, vh) = read_video_file(path)
    # BEFORE the early return, and that placement is the whole point: a clip with no ComfyUI graph
    # still has a length and a size, and this is the only line that would have quietly dropped
    # them. Both are properties of the CONTAINER, not of anything ComfyUI wrote.
    if duration is not None:
        res['duration'] = duration
    if vw and vh:
        res['width'], res['height'] = vw, vh
    if not tags.get('prompt'):
        return res
    from_video = {'loras': [], 'positive': None, 'negative': None,
                  'model': None, 'model_name': None, 'method': None,
                  'set_id': None, 'set_stage': None, 'vae_name': None,
                  'steps': None, 'cfg': None, 'sampler_name': None, 'scheduler': None, 'seed': None}
    # The file's own name and stage tell the walk which saver wrote THIS clip -- the case that
    # matters, because a video workflow routinely writes several and they are not the same length.
    _from_graph(tags['prompt'], from_video,
                _file_hints(path, stage=res.get('set_stage'), kind='video'))
    filled = False
    for k, v in from_video.items():
        if k == 'method':
            continue
        if k == 'loras':
            if v and not res['loras']:
                res['loras'] = v
                filled = True
        elif v not in (None, '') and res.get(k) in (None, ''):
            res[k] = v
            filled = True
    # `method` is diagnostic only — it answers "where did this come from" in diagnose_meta.
    if filled:
        src = from_video.get('method') or 'video-graph'
        res['method'] = f'sidecar+{src}' if had_sidecar else src
    return res


def read_sidecar(path):
    """Metadata for a file that carries none of its own — which means a VIDEO.

    A video is never opened during a scan (no PNG chunks to read, and Pillow can't size it), so
    until now a video had no route to a prompt, a model or a set id at all. For a txt2video run
    there isn't even a still to pair with, so there was no route at any price.

    A plain `.txt` beside it fixes that, and deliberately in the SAME A1111 format the saver already
    writes into PNGs: nothing new to learn, it pastes straight into Civitai, and `parse_a1111`
    already reads it. Plus `VV set id:`, which is what finally lets a video join a set — set ids
    live in PNG chunks, so a video could never carry one.

    Returns None when there is no sidecar, so the caller keeps its existing behaviour untouched.
    """
    sc = sidecar_path(path)
    if not sc:
        return None
    try:
        if os.path.getsize(sc) > SIDECAR_MAX_BYTES:
            return None
        with open(sc, 'rb') as f:
            text = f.read(SIDECAR_MAX_BYTES).decode('utf-8', 'replace')
    except OSError:
        return None
    res = {'loras': [], 'positive': None, 'negative': None,
           'model': None, 'model_name': None, 'method': None,
           'set_id': None, 'set_stage': None, 'vae_name': None,
           'steps': None, 'cfg': None, 'sampler_name': None, 'scheduler': None, 'seed': None}
    # Lift our own keys OUT before the A1111 parse. A1111's negative runs from its label to the end
    # of the body — multi-line negatives are legal — so a `VV set id:` line sitting after it gets
    # swallowed into the negative prompt. Found immediately on the first hand-written sidecar.
    for key, rx in _SIDECAR_KEYS.items():
        m = rx.search(text)
        if m:
            res[key] = m.group(1).strip() or None
    body = text
    for rx in _SIDECAR_KEYS.values():
        body = rx.sub('', body)
    got = parse_a1111(body)
    for k in ('positive', 'negative', 'model_name', 'vae_name', 'loras'):
        if got.get(k):
            res[k] = got[k]
    # The settings row too. A sidecar is the ONLY route to generation settings for a video, since
    # nothing else about a video can carry them -- so dropping them here would have made the whole
    # feature silently image-only.
    for k in ('steps', 'cfg', 'sampler_name', 'scheduler', 'seed'):
        if got.get(k) is not None:
            res[k] = got[k]
    if any(res[k] for k in ('positive', 'negative', 'model_name', 'set_id')):
        res['method'] = 'sidecar'
    return res


# Words a workflow writes when it has nothing to say. Seen for real: a saver that emitted a
# complete A1111 block reading "unknown" for the prompt, "unknown" for the negative, an empty
# sampler, an empty model, seed 0 and CFG 49 — every field a stand-in. The app showed
# "unknown" as the prompt, which is worse than showing nothing: a blank cell says "not recorded",
# where the word says "this image is of an unknown thing" and looks like data.
_PLACEHOLDER_WORDS = {'unknown', 'none', 'n/a', 'na', 'null', 'undefined', 'nil', '-'}


def _is_placeholder(v):
    return isinstance(v, str) and v.strip().lower() in _PLACEHOLDER_WORDS


def _fill_from_a1111(res, text):
    """Fill any field the graph walk left empty from the A1111 block. FILLS GAPS, NEVER OVERWRITES:
    where the graph produced an answer it stays authoritative, so no existing image's metadata can
    change. That also keeps this purely additive — the only images affected are ones that showed
    nothing before."""
    _SETTINGS = ('steps', 'cfg', 'sampler_name', 'scheduler', 'seed')
    # The skip-the-parse shortcut now has to account for the settings too. It used to ask only
    # "have we got a prompt and a model?", which was right when those were all this could supply —
    # but a graph-traced image can have both and still be missing every generation setting, and
    # returning here would have left them empty with the answer sitting in the same file.
    if res.get('positive') and res.get('model_name') and all(res.get(k) is not None for k in _SETTINGS):
        return                                   # nothing to gain; skip the parse entirely
    got = parse_a1111(text)
    if not got:
        return
    used = False
    for k in ('positive', 'negative', 'model_name', 'vae_name'):
        if got.get(k) and not res.get(k) and not _is_placeholder(got[k]):
            res[k] = got[k]
            used = True
    for k in _SETTINGS:
        if got.get(k) is not None and res.get(k) is None:
            res[k] = got[k]
            used = True
    if got.get('loras') and not res.get('loras'):
        res['loras'] = got['loras']
        used = True
    if used and not res.get('method'):
        res['method'] = 'a1111-chunk'            # so the diagnostic can say where it came from


# A JPEG or WebP has no PNG text chunks, so the A1111 block lives in EXIF `UserComment` (tag 37510)
# instead — the convention A1111, Forge and Civitai all write, and the reason a downloaded JPG shows
# its prompt everywhere except here. Found 2026-08-13 on a Civitai download whose whole metadata was
# 2,822 bytes of UserComment the viewer never looked at.
_EXIF_USER_COMMENT = 37510
_EXIF_IFD = 0x8769
_EXIF_META_EXTS = {'.jpg', '.jpeg', '.webp'}


# ComfyUI writes its graphs into a JPEG/WebP's EXIF as two ordinary TIFF strings — Make (271) holds
# `prompt:{…}` (the API graph, identical to a PNG's `prompt` chunk) and Model (272) holds
# `workflow:{…}` (the UI graph). Not UserComment, which is where the A1111 block lives; that is why
# read_exif_a1111 walks straight past them.
_EXIF_MAKE = 271
_EXIF_MODEL = 272


def read_exif_comfy(path):
    """The ComfyUI API graph from a JPEG/WebP's EXIF, as a JSON string, or None. Never raises.

    WHY THIS EXISTS. The reader only ever looked in PNG text chunks, so **every WebP and JPEG
    ComfyUI has ever saved read as "no metadata"** — including every animated WebP from a video
    workflow, which is what most LTX/AnimateDiff setups write. The file was carrying the whole
    graph: prompt, model, sampler, seed. Nobody had looked, because the assumption on record was
    that "ComfyUI metadata lives in PNG" (index_db.py said exactly that, in a comment).

    Proven on a real file rather than reasoned about: `samples/Comfy_070255__00001_.webp`, a 97-frame
    LTX render, whose EXIF holds the full graph down to `ltx-video-2b-v0.9.1.safetensors`.

    Only the API graph is returned. The UI graph beside it stores widget values POSITIONALLY, so
    reading it needs a node-type table — a separate job, and the reason this is not that job.
    """
    try:
        if os.path.splitext(path)[1].lower() not in _EXIF_META_EXTS:
            return None
        from PIL import Image                     # local: comfy_meta is otherwise stdlib-only
        with Image.open(path) as im:
            ex = im.getexif()
            if not ex:
                return None
            vals = [ex.get(_EXIF_MAKE), ex.get(_EXIF_MODEL)]
        for v in vals:
            if isinstance(v, bytes):
                v = v.decode('utf-8', 'replace')
            if not isinstance(v, str):
                continue
            # The prefix is part of the value, not a separate tag: `prompt:{"6": {...}}`. Match on
            # it rather than on the tag number — writers have swapped the two tags before, and a
            # value that starts with the word tells you what it is whichever slot it arrived in.
            for tag in ('prompt:', 'workflow:'):
                if v.startswith(tag):
                    body = v[len(tag):].strip()
                    # The API graph only. A UI graph has `nodes`/`links` at the top level and no
                    # numeric node ids, so it would walk as an empty graph and quietly return
                    # nothing — worse than declining, because it looks like it was read.
                    if tag == 'prompt:' and body.startswith('{'):
                        return body
    except Exception:
        return None
    return None


def read_exif_a1111(path):
    """The A1111 `parameters` text from a JPEG/WebP's EXIF UserComment, or None. Never raises.

    UserComment is prefixed by an 8-byte character code naming its encoding — `UNICODE\\0` (UTF-16,
    and writers disagree on the byte order), `ASCII\\0\\0\\0`, or nothing at all.

    **The byte order has to be guessed, and the obvious guess is wrong.** Counting NULs does not
    work: ASCII text stored little-endian and decoded big-endian comes back as CJK characters with
    no NULs at all, so both decodes look equally clean. Score by how much of the result is ordinary
    printable ASCII instead — the winner is the one that reads as English rather than as U+6100.
    """
    try:
        if os.path.splitext(path)[1].lower() not in _EXIF_META_EXTS:
            return None
        from PIL import Image                     # local: comfy_meta is otherwise stdlib-only
        with Image.open(path) as im:
            ifd = im.getexif().get_ifd(_EXIF_IFD)
        raw = ifd.get(_EXIF_USER_COMMENT)
        if not raw:
            return None
        if isinstance(raw, str):                  # some writers hand Pillow a decoded string
            return raw.strip() or None
        prefix, payload = raw[:8], raw[8:]
        if prefix.startswith(b'UNICODE'):
            def ascii_score(s):
                return sum(1 for ch in s if ' ' <= ch <= '~' or ch in '\r\n\t')
            be = payload.decode('utf-16-be', 'replace')
            le = payload.decode('utf-16-le', 'replace')
            text = be if ascii_score(be) >= ascii_score(le) else le
        elif prefix.startswith(b'ASCII') or prefix.startswith(b'\x00' * 8):
            text = payload.decode('utf-8', 'replace')
        else:
            text = raw.decode('utf-8', 'replace')  # no recognised prefix: take the lot
        text = text.replace('\x00', '').strip()
        return text or None
    except Exception:
        return None


# ---- Audio: the graph lives in ID3, and the song's attributes live in prose ---------------------
# A ComfyUI audio save writes the SAME two JSON blobs a PNG carries (`prompt` and `workflow`), into
# ID3 TXXX "user text" frames instead of PNG text chunks. So a song is a first-class file here, not
# a second-class one like a WebM: once the JSON is out of the tag, every walker below reads it
# exactly as it reads an image's.
#
# MP3 and FLAC are read. OPUS IS STILL NOT: it carries the same two blobs in the same Vorbis
# comment format, but wrapped in Ogg pages that have to be walked first, and nothing here has been
# tested against one. An untested format indexes and plays with no metadata rather than with
# guessed metadata, and the card says so by simply having nothing to show.
_AUDIO_META_EXTS = {'.mp3', '.flac'}
# An ID3 tag holding a workflow runs to tens of KB (48KB in testing). This ceiling is generous
# enough for a large graph and still refuses to read a corrupt header claiming hundreds of MB.
ID3_MAX_BYTES = 8 * 1024 * 1024


def _synchsafe(b):
    """ID3's 28-bit size: seven bits per byte, top bit always clear (so a size can never
    contain 0xFF and be mistaken for the start of an audio frame)."""
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _id3_frames(path):
    """(major_version, tag_bytes) for an MP3's ID3v2 tag, or (0, b'') if it has none."""
    with open(path, 'rb') as f:
        head = f.read(10)
        if len(head) < 10 or head[:3] != b'ID3':
            return 0, b''
        size = _synchsafe(head[6:10])
        if not (0 < size <= ID3_MAX_BYTES):
            return 0, b''
        return head[3], f.read(size)


def _walk_id3(major, tag):
    """Yield (frame_id, payload) for each real frame, stopping at the tag's padding.

    **The padding matters.** An ID3 tag is padded with zeros so a later edit can grow it without
    rewriting the file, and a reader must stop at the first zero frame id. A frame written INTO that
    padding is unreachable — which is exactly the bug a first attempt at writing cover art here hit:
    the file was valid, the frame was present, and every reader said there was no cover.
    """
    pos = 0
    while pos + 10 <= len(tag):
        fid = tag[pos:pos + 4]
        if fid == b'\x00\x00\x00\x00':
            return
        raw = tag[pos + 4:pos + 8]
        fsize = _synchsafe(raw) if major >= 4 else int.from_bytes(raw, 'big')
        if fsize <= 0 or pos + 10 + fsize > len(tag):
            return
        yield fid, tag[pos + 10:pos + 10 + fsize]
        pos += 10 + fsize


def read_id3_cover(path):
    """Embedded cover art as raw image bytes, or None. Never raises.

    APIC is the standard cover frame every music player reads: an encoding byte, a NUL-terminated
    MIME type, a picture-type byte, a NUL-terminated description, then the image. A file may carry
    several (front cover, back cover, artist); picture type 3 is the front cover, and it wins when
    present — otherwise the first one found stands in.
    """
    try:
        if os.path.splitext(path)[1].lower() not in _AUDIO_META_EXTS:
            return None
        first = None
        for fid, payload in _walk_id3(*_id3_frames(path)):
            if fid != b'APIC' or len(payload) < 4:
                continue
            _mime, _, rest = payload[1:].partition(b'\x00')
            if not rest:
                continue
            ptype, rest = rest[0], rest[1:]
            _desc, _, data = rest.partition(b'\x00')
            if not data:
                continue
            if ptype == 3:                      # front cover — the one a card should show
                return data
            if first is None:
                first = data
        return first
    except Exception:
        return None


def read_id3_txxx(path):
    """{description: value} for every TXXX frame in an MP3's ID3v2 tag. Never raises.

    TXXX is the "user-defined text" frame: a description and a value, NUL-separated, behind one
    encoding byte. ComfyUI uses two of them, described as `prompt` and `workflow`.

    Frame sizes are synchsafe in ID3v2.4 and plain big-endian in v2.3 — reading a v2.3 tag with the
    v2.4 rule silently under-reads every size and walks off into the middle of a frame, so the
    version byte decides.
    """
    out = {}
    try:
        for fid, payload in _walk_id3(*_id3_frames(path)):
            if fid != b'TXXX':
                continue
            enc = payload[0] if payload else 0
            codec = {0: 'latin-1', 1: 'utf-16', 2: 'utf-16-be', 3: 'utf-8'}.get(enc, 'utf-8')
            body = payload[1:].decode(codec, 'replace')
            desc, _, val = body.partition('\x00')
            # Trailing NULs: some writers terminate the value, and the JSON parser downstream
            # rejects the tail as "extra data" — which is what an unstripped NUL looks like.
            out[desc.strip('\x00').strip()] = val.rstrip('\x00')
    except Exception:
        return out
    return out


# ---- FLAC: the same two blobs, in the container FLAC actually uses ------------------------------
# A ComfyUI audio save writes `prompt` and `workflow` whatever the format; only the envelope
# changes. MP3 gets ID3 TXXX frames, read above. FLAC gets a VORBIS_COMMENT block, read here.
#
# WHY THIS WAS MISSING AND WHAT IT COST: the reader only knew ID3, so a FLAC came back with nothing
# at all -- no prompt, no model, no settings, no tempo or key -- and its Details pane was simply
# blank. The visible symptom the author reported was different and looked unrelated: the detail
# view's drag-to-ComfyUI handle showed a BROKEN IMAGE. That handle is a picture built out of the
# song's own workflow, so no workflow meant no picture, meant a 500, meant a broken box. One cause,
# two faults, and the one he could see was the further of the two from it.
#
# Verified against a real file before a line was written: samples/YUE_FemPhonemes_...flac carries
# `prompt` at 9,340 chars and `workflow` at 36,653, in a 46KB VORBIS_COMMENT block, and no PICTURE
# block at all -- which is why that track draws a waveform rather than cover art.
#
# THE TWO ENDIANNESSES ARE NOT A TYPO. A FLAC metadata block header is big-endian (it is FLAC's own
# format), and the Vorbis comment payload inside it is little-endian (it is Vorbis's). Reading
# either with the other's rule yields garbage lengths that walk off the end of the block.
_FLAC_MAGIC = b'fLaC'
_FLAC_VORBIS_COMMENT = 4
_FLAC_PICTURE = 6


def _flac_blocks(path):
    """Yield (block_type, payload) for each FLAC metadata block. Never raises.

    Reads block by block rather than slurping: the audio itself is megabytes and none of it is
    wanted, and the metadata all sits at the front of the file before the first audio frame.
    """
    try:
        with open(path, 'rb') as f:
            if f.read(4) != _FLAC_MAGIC:
                return
            while True:
                head = f.read(4)
                if len(head) < 4:
                    return
                last = head[0] >> 7
                btype = head[0] & 0x7F
                size = int.from_bytes(head[1:4], 'big')
                if size > ID3_MAX_BYTES:        # a corrupt header claiming hundreds of MB
                    return
                payload = f.read(size)
                if len(payload) < size:
                    return
                yield btype, payload
                if last:
                    return
    except Exception:
        return


def _parse_vorbis_comments(body):
    """{key: value} from a Vorbis comment payload. Keys are lowercased: the spec says the field
    name is case-insensitive and writers disagree in practice -- ffmpeg wrote these ones lowercase,
    but an uppercase WORKFLOW from another tool has to land in the same place."""
    out = {}
    try:
        p = 0
        vlen = int.from_bytes(body[p:p + 4], 'little'); p += 4
        p += vlen                                # the vendor string, which nothing here wants
        count = int.from_bytes(body[p:p + 4], 'little'); p += 4
        for _ in range(count):
            clen = int.from_bytes(body[p:p + 4], 'little'); p += 4
            if clen > len(body):                 # a length that cannot be real; stop rather than guess
                break
            raw = body[p:p + clen]; p += clen
            key, sep, val = raw.partition(b'=')
            if not sep:
                continue
            out[key.decode('utf-8', 'replace').strip().lower()] = val.decode('utf-8', 'replace')
    except Exception:
        return out
    return out


def read_vorbis_comments(path):
    """{key: value} for a FLAC's VORBIS_COMMENT block. Never raises, {} when there is none."""
    for btype, payload in _flac_blocks(path):
        if btype == _FLAC_VORBIS_COMMENT:
            return _parse_vorbis_comments(payload)
    return {}


def _parse_flac_picture(body):
    """Raw image bytes out of a FLAC PICTURE block, plus its picture type. All big-endian."""
    p = 0
    ptype = int.from_bytes(body[p:p + 4], 'big'); p += 4
    mlen = int.from_bytes(body[p:p + 4], 'big'); p += 4 + mlen
    dlen = int.from_bytes(body[p:p + 4], 'big'); p += 4 + dlen
    p += 16                                      # width, height, depth, colours -- four uint32s
    ilen = int.from_bytes(body[p:p + 4], 'big'); p += 4
    return ptype, body[p:p + ilen]


def read_flac_cover(path):
    """Embedded cover art from a FLAC, or None. Never raises.

    TWO PLACES TO LOOK, because writers disagree. The PICTURE metadata block is the native one;
    METADATA_BLOCK_PICTURE is the same structure base64'd into a comment, which is how a tool that
    only knows how to write comments smuggles art in. Picture type 3 is the front cover and wins
    when present, matching read_id3_cover's rule for APIC so both formats answer the same way.
    """
    try:
        first = None
        comments = None
        for btype, payload in _flac_blocks(path):
            if btype == _FLAC_PICTURE:
                ptype, data = _parse_flac_picture(payload)
                if data:
                    if ptype == 3:
                        return data
                    if first is None:
                        first = data
            elif btype == _FLAC_VORBIS_COMMENT:
                comments = payload
        if first is None and comments is not None:
            b64 = _parse_vorbis_comments(comments).get('metadata_block_picture')
            if b64:
                ptype, data = _parse_flac_picture(base64.b64decode(b64))
                if data:
                    return data
        return first
    except Exception:
        return None


# ---- one question, asked of whichever container this file happens to be -------------------------
# Everything above this line knows about a format. Everything below it, and every caller, should
# not: a song's workflow is a song's workflow. These two are the seam, and adding Opus means adding
# a branch here and nothing else.
def read_audio_tags(path):
    """{key: value} of a song's embedded text, whatever container it is in. Never raises."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.flac':
        return read_vorbis_comments(path)
    return read_id3_txxx(path)


def read_audio_cover(path):
    """A song's embedded cover art as raw bytes, or None, whatever container it is in."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.flac':
        return read_flac_cover(path)
    return read_id3_cover(path)


# The song's own attributes — tempo, key, genre, voice — are NOT fields. They are sentences inside
# the free-text caption the model was prompted with, and the phrasing varies run to run: "78 BPM"
# in one, "bpm is 87" in the next. So each is matched permissively and INDEPENDENTLY, and anything
# that doesn't match stays None rather than being guessed at — a card shows what was found and
# leaves out what wasn't, which is the only honest reading of prose.
_BPM_RES = (re.compile(r'\bbpm\s+is\s+(\d{2,3})\b', re.I),
            re.compile(r'\b(\d{2,3})\s*bpm\b', re.I))
_KEY_RES = (re.compile(r'\bkey\s+is\s+([A-G](?:\s*(?:sharp|flat)|[#b♯♭])?)\s*[,.]?\s*(?:and\s+)?'
                       r'scale\s+is\s+(\w+)', re.I),
            re.compile(r'\b([A-G](?:\s+(?:sharp|flat)|[#b♯♭])?)\s+(major|minor)\b'))
# Sentences that describe the tempo, key or SINGER rather than the genre. Used to REJECT candidates,
# never to find one — so an unfamiliar phrasing costs a blank genre, not a wrong one.
#
# The voice words are here because rejecting a heading only moves the problem to the next sentence:
# with "Vocal is female." refused, the walker took "Singer A (Female)" instead. A genre never names
# who is singing. NB `vocal` itself is deliberately absent — "Vocal Trance" is a real genre — which
# is why the bare heading "Vocal" is caught by _GENRE_HEADINGS instead.
_NOT_GENRE_RE = re.compile(
    r'\b(bpm|key|scale|major|minor|tempo|singer|vocalist|male|female|androgynous)\b', re.I)
# Leading section labels the caption template puts in front of the first real sentence.
_CAPTION_LABEL_RE = re.compile(r'^\s*(?:global\s+metadata|basic\s+attributes|genre|style)\s*[:\-]?\s*',
                               re.I)
_VOCAL_LABELLED_RE = re.compile(r'\bsinger\s+\w+\s*\(\s*(male|female)\s*\)', re.I)
_VOCAL_LOOSE_RE = re.compile(r'\b(androgynous|female|male)\b', re.I)
_GENRE_MAX = 60          # a genre names itself in a few words; longer means a description was caught
_GENRE_MAX_WORDS = 6     # "Alternative Hip-Hop / Experimental Electronic" is five; prose runs longer
# A genre is a label, not a statement, so it never opens with an article. Cheap, and it rejects the
# failure this filter actually has: a short descriptive sentence ("A warm and pleasant piece of
# music") that carries no tempo or key words and would otherwise look exactly like a genre.
_GENRE_ARTICLE_RE = re.compile(r'^(?:a|an|the|this|it)\b', re.I)
# A genre is a NOUN PHRASE. It never contains a verb or a preposition, so a candidate that does is a
# sentence — which is how "Vocal is female" was reaching cards as a genre. `and` is deliberately not
# here: "Drum and Bass" and "Rhythm and Blues" are genres.
_GENRE_FUNCTION_RE = re.compile(r'\b(?:is|are|was|were|be|has|have|with|of|in|on|for|to|by|that|which)\b',
                                re.I)
# Bare section headings from the caption template, which survive every shape test above by being
# short, wordless noun phrases. This is a list of STRUCTURAL words, not of genres — it only ever
# rejects, so an unfamiliar template costs a blank genre rather than a wrong one, and a real genre
# that happens to be one word ("Techno") is unaffected unless it collides with a heading.
_GENRE_HEADINGS = {'vocal', 'vocals', 'lyrics', 'arrangement', 'production', 'style', 'sonics',
                   'groove', 'harmony', 'melody', 'instrumentation', 'instruments', 'mood',
                   'emotion', 'tempo', 'metadata', 'attributes', 'imagery', 'scenario', 'summary'}


def parse_song_caption(caption):
    """{bpm, key, genre, vocal} read out of a music model's free-text caption. All optional.

    Each attribute is matched on its own so one unfamiliar phrasing can't take the others with it.
    """
    res = {'bpm': None, 'key': None, 'genre': None, 'vocal': None}
    if not caption:
        return res
    text = caption.replace('\n', ' ')

    for rx in _BPM_RES:
        m = rx.search(text)
        if m:
            res['bpm'] = int(m.group(1))
            break

    for rx in _KEY_RES:
        m = rx.search(text)
        if m:
            note = re.sub(r'\s+', ' ', m.group(1)).strip()
            note = re.sub(r'\s*sharp$', '♯', note, flags=re.I)
            note = re.sub(r'\s*flat$', '♭', note, flags=re.I)
            note = note.replace('#', '♯').replace('b', '♭') if len(note) > 1 else note
            res['key'] = f'{note[0].upper()}{note[1:]} {m.group(2).lower()}'
            break

    # The genre is a bare noun phrase — "Country / Outlaw Country." — and it is ANCHORED: in every
    # caption seen it sits within a sentence or two of the tempo/key statement, sometimes before it
    # and sometimes after, as part of the same opening metadata block.
    #
    # Shape alone is not enough, and trying it that way is what put "Vocal is", "Singer A (Female)"
    # and "Warm delivery throughout" on the author's cards: in a caption that states NO genre, some short
    # noun phrase always eventually passes, so the search finds a wrong answer instead of no answer.
    # Anchoring bounds the search to where a genre would actually be, which means a caption without
    # one has nowhere to find a false positive — the filters below then only have to reject the few
    # non-genre sentences that share that block.
    #
    # The cost is a caption that names a genre but no tempo, which yields nothing. That is the
    # trade this whole parser makes everywhere: a blank beats a confident wrong answer.
    sents = re.split(r'(?<=[.;])\s+', text[:1200])
    anchor = next((i for i, s in enumerate(sents)
                   if re.search(r'\bbpm\b|\bkey\s+is\b', s, re.I)), None)
    for raw in ([] if anchor is None else sents[max(0, anchor - 2):anchor + 3]):
        cand = _CAPTION_LABEL_RE.sub('', raw).strip(' .;')
        if not cand or len(cand) > _GENRE_MAX or len(cand.split()) > _GENRE_MAX_WORDS:
            continue
        if any(ch.isdigit() for ch in cand) or _NOT_GENRE_RE.search(cand):
            continue
        if _GENRE_ARTICLE_RE.match(cand) or _GENRE_FUNCTION_RE.search(cand):
            continue
        if all(w.strip('&/,.').lower() in _GENRE_HEADINGS for w in cand.split() if w.strip('&/,.')):
            continue
        res['genre'] = cand
        break

    m = _VOCAL_LABELLED_RE.search(text)
    if m:
        res['vocal'] = m.group(1).lower()
    else:
        # Unlabelled captions still say it in passing ("soft androgynous vocal"), so look only in
        # the neighbourhood of the word "vocal" — the whole caption mentions "male" in contexts
        # that have nothing to do with the singer.
        for vm in re.finditer(r'vocal\w*', text, re.I):
            near = text[max(0, vm.start() - 60):vm.end() + 60]
            hit = _VOCAL_LOOSE_RE.search(near)
            if hit:
                res['vocal'] = hit.group(1).lower()
                break
    return res


def _audio_text_nodes(g):
    """(caption, lyrics) from the graph's text-encode node — the style description and the words.

    A music encoder carries both on ONE node, under its own key names, and neither is the `text`
    input every image encoder uses — so the ordinary prompt walker finds nothing here and this
    reads them directly. Picks the longest of each across the graph, which settles a workflow
    holding more than one encoder.

    TWO NAMES FOR THE CAPTION, IN PRIORITY ORDER, not one pool. YuE2's generator nodes call it
    `style` where the encoder this was written against calls it `caption`, and `style` cannot
    simply join the pool: it is a common enough input name that an unrelated node carrying a long
    string would beat a real caption under the longest-wins rule. So `caption` is searched first
    and `style` only answers when nothing claimed the better name.

    FOUND BY THE FLAC WORK, 2026-09-18, and worth knowing it was never FLAC-specific: this graph
    also draws its own cover art, so it holds an IMAGE encoder beside the music one. With the
    caption unfound, the generic walker's longest-prompt fallback stood, and the song's "prompt"
    came out as the description of the woman on the sleeve. The format only decided whether anyone
    could see it.
    """
    caption = lyrics = ''
    for keys, which in ((('caption',), 'caption'), (('style',), 'caption'), (('lyrics',), 'lyrics')):
        if which == 'caption' and caption:
            continue                      # a better-named input already answered
        for n in g.values():
            inp = n.get('inputs', {})
            if not isinstance(inp, dict):
                continue
            for key in keys:
                v = inp.get(key)
                if _is_link(v):
                    v = _resolve_string(g, v)
                if not isinstance(v, str):
                    continue
                v = v.strip()
                if which == 'caption':
                    if len(v) > len(caption):
                        caption = v
                elif len(v) > len(lyrics):
                    lyrics = v
    return caption, lyrics


def extract_audio(path):
    """Generation metadata for a song. Same shape as extract(), plus the audio-only fields.

    `positive` is the caption — the style description the model was steered with — because that is
    the sentence that answers "what is this". The lyrics are kept separately: they are the words
    sung, not the instruction, and folding them into the prompt would make every search for a
    common word return songs.
    """
    res = {
        'loras': [], 'positive': None, 'negative': None,
        'model': None, 'model_name': None, 'method': None,
        'set_id': None, 'set_stage': None, 'vae_name': None,
        'steps': None, 'cfg': None, 'sampler_name': None, 'scheduler': None, 'seed': None,
        'lyrics': None, 'bpm': None, 'key': None, 'genre': None, 'vocal': None,
        'has_cover': 0,
    }
    if os.path.splitext(path)[1].lower() not in _AUDIO_META_EXTS:
        return res
    # One pass over the tag for both answers. Whether the file carries cover art is recorded here
    # rather than inferred later from whether a thumbnail exists: thumbnails are generated LAZILY on
    # first request, so an unvisited song has no thumb yet and would look coverless.
    frames = {}
    if os.path.splitext(path)[1].lower() == '.flac':
        # One pass here too, for the same reason: the comment block and any picture block both sit
        # at the front of the file, so asking two separate questions would walk it twice.
        for btype, payload in _flac_blocks(path):
            if btype == _FLAC_VORBIS_COMMENT:
                frames = _parse_vorbis_comments(payload)
                if 'metadata_block_picture' in frames:
                    res['has_cover'] = 1
            elif btype == _FLAC_PICTURE:
                res['has_cover'] = 1
    else:
        try:
            for fid, payload in _walk_id3(*_id3_frames(path)):
                if fid == b'APIC':
                    res['has_cover'] = 1
                elif fid == b'TXXX':
                    enc = payload[0] if payload else 0
                    codec = {0: 'latin-1', 1: 'utf-16', 2: 'utf-16-be', 3: 'utf-8'}.get(enc, 'utf-8')
                    desc, _, val = payload[1:].decode(codec, 'replace').partition('\x00')
                    frames[desc.strip('\x00').strip()] = val.rstrip('\x00')
        except Exception:
            pass
    # kind='audio' keeps the walk on the track's own branch: a song workflow that also draws cover
    # art holds an image sampler too, and that one is usually the one with the longer prompt.
    _from_graph(frames.get('prompt'), res, _file_hints(path, kind='audio'))
    try:
        g = json.loads(frames.get('prompt') or 'null')
        g = {k: v for k, v in g.items() if isinstance(v, dict)} if isinstance(g, dict) else {}
    except Exception:
        g = {}
    if g:
        caption, lyrics = _audio_text_nodes(g)
        if caption:
            res['positive'] = caption
            res['method'] = 'audio-caption'
        res['lyrics'] = lyrics or None
        res.update({k: v for k, v in parse_song_caption(caption).items()})
    return res


def extract(path):
    """Extract generation metadata from an image. Never raises.

    Three places, in order: the ComfyUI `prompt` graph, the A1111 `parameters` chunk, and — for a
    JPEG or WebP, which have no PNG chunks at all — the same A1111 block in EXIF UserComment.
    """
    res = {
        'loras': [],
        'positive': None, 'negative': None,
        'model': None, 'model_name': None,
        'method': None,
        'set_id': None, 'set_stage': None, 'vae_name': None,
        # Generation settings, read from the BASE sampler only — see _pick_base_sampler.
        'steps': None, 'cfg': None, 'sampler_name': None, 'scheduler': None, 'seed': None,
    }
    try:
        chunks = read_png_text_chunks(path)
    except Exception:
        # NOT a dead end any more. This used to return empty, which is why a JPEG or WebP reported
        # "no meta" however much metadata it carried — the read fails on the very first bytes,
        # before anything else got a chance. A non-PNG still has EXIF.
        chunks = {}
    # Set keys written by the comfy_vv_saver node: every image of one run shares vv_set_id, and
    # vv_set_stage names its stage. Read BEFORE the graph parsing below — they're independent of
    # the ComfyUI graph, and a saver with embed_comfy_wf off writes these but no `prompt` chunk.
    res['set_id'] = (chunks.get('vv_set_id') or '').strip() or None
    res['set_stage'] = (chunks.get('vv_set_stage') or '').strip() or None
    # The graph first, then the A1111 block for whatever it left empty. Structured as a helper so
    # the graph path can bail at any of its four dead ends WITHOUT skipping the fallback — which is
    # what the old `return res` early-outs did, and why a PNG with no readable graph reported
    # nothing even when the answer was sitting in its `parameters` chunk.
    hints = _file_hints(path, chunks)
    _from_graph(chunks.get('prompt'), res, hints)
    # THE SAME GRAPH, KEPT SOMEWHERE ELSE. A JPEG or WebP has no PNG chunks at all, so the line
    # above found nothing and the file read as "no metadata" however complete it was — every WebP
    # ComfyUI has ever written, including the animated ones every video workflow produces.
    # Tried only when the chunk path came up empty: a PNG never opens the file twice, and a format
    # that can carry both has one obvious winner. `_from_graph` fills only what is still None, so
    # this cannot overwrite anything already read.
    if not (res['positive'] or res['model']):
        _from_graph(read_exif_comfy(path), res, hints)
    # AFTER the graph, so a traced model still wins; the A1111 block only fills what was left empty.
    # The PNG chunk is tried first and EXIF only when it is absent — a PNG is never read twice, and
    # the file is only opened for a format that could carry EXIF at all.
    _fill_from_a1111(res, chunks.get('parameters') or read_exif_a1111(path))
    return res


def _from_graph(pj, res, hints=None):
    """Populate `res` from ComfyUI's `prompt` chunk. Returns nothing; a dead end just leaves the
    fields it couldn't fill alone.

    `hints` is what the FILE says about itself (see `_file_hints`), used to work out which node in
    the graph saved THIS file when several saved something. Optional, and everything still works
    without it -- a caller that has no path to offer just gets the older rules.
    """
    if not pj:
        return
    try:
        g = json.loads(pj)
    except Exception:
        return
    if not isinstance(g, dict):
        return
    # Nodes must be dicts; drop anything else so a malformed/hand-made graph can't
    # crash the walkers below (one bad file must never abort a whole scan).
    g = {k: v for k, v in g.items() if isinstance(v, dict)}
    res['loras'] = _extract_loras(g)

    # WHICH SAMPLER MADE THIS FILE. Asked once, answered by walking back from the node that saved
    # it, and used for the prompt, the model and the VAE alike -- so the row the library shows and
    # the file Export for Civitai writes can no longer describe the same picture differently.
    chain = resolve_output_chain(g, hints)

    vae = _resolve_vae(g, [chain['vae_decode']] if chain['vae_decode'] else ())
    if vae:
        res['vae_name'] = _vae_display_name(vae)

    def _read_sampler(sid):
        pos, neg = _sampler_conditioning(g, sid)
        ptxt = _follow_to_text(g, pos) if _is_link(pos) else (pos if isinstance(pos, str) else '')
        ntxt = _follow_to_text(g, neg) if _is_link(neg) else (neg if isinstance(neg, str) else '')
        return {'positive': ptxt, 'negative': ntxt, 'model': _resolve_model(g, sid)}

    best, how = None, None
    if chain['sampler'] and chain['sampler'] in g:
        best, how = _read_sampler(chain['sampler']), chain['method']
    if best is None or not (best['positive'] or best['model']):
        # The walk had no answer, or reached a sampler that states nothing. Fall back to the old
        # rule rather than report less than before: no file is ever worse off for this change.
        fallback = None
        for sid in [nid for nid, n in g.items() if _is_sampler_node(n)]:
            cand = _read_sampler(sid)
            if fallback is None or len(cand['positive'] or '') > len(fallback['positive'] or ''):
                fallback = cand
        if fallback is not None:
            best, how = fallback, 'sampler-trace'
    if best:
        res.update(positive=best['positive'] or None,
                   negative=best['negative'] or None,
                   model=best['model'])
        res['method'] = how

    # Generation settings STILL come from the BASE sampler, deliberately, even though the walk above
    # now knows which sampler made this particular file. Changing this moves the steps and CFG shown
    # on files the author has already looked at, and it is his call to make on its own -- parked
    # 2026-09-17 with the rest of the walk shipped. `Face__00001.mp4` is the known-wrong example:
    # it reports 8 steps / CFG 0.9 from another clip in the same workflow, where its own branch ran
    # 4 steps / CFG 1.0.
    base = _pick_base_sampler(g)
    if base is not None:
        for k, v in extract_gen_params(g, base).items():
            if v is not None:
                res[k] = v

    # No real negative (Flux/Krea etc., CFG=1): the negative input traces to the
    # same text node as positive, so drop the duplicate instead of showing it twice.
    if res['negative'] and res['negative'] == res['positive']:
        res['negative'] = None

    # Fallback: any loader in the graph
    if not res['model']:
        for n in g.values():
            v = _loader_filename(n)
            if v:
                res['model'] = v
                break

    # Fallback: longest CLIPTextEncode text anywhere
    if not res['positive']:
        texts = []
        for n in g.values():
            if 'CLIPTextEncode' in n.get('class_type', ''):
                s = _encode_text(g, n)
                if s:
                    texts.append(s)
        if texts:
            res['positive'] = max(texts, key=len)
            res['method'] = 'longest-clip-fallback'

    if res['model']:
        base = res['model'].replace('\\', '/').split('/')[-1]
        res['model_name'] = os.path.splitext(base)[0]


# ---- A1111 "parameters" export (Civitai-compatible) -------------------------------------------
# Civitai reliably parses the A1111 plain-text `parameters` PNG chunk; its ComfyUI-graph parser only
# handles simple graphs and often fails. So on export we translate the graph into that text format.

# ComfyUI sampler_name -> A1111 display name (Civitai is lenient; unknown names pass through raw).
_A1111_SAMPLERS = {
    'euler': 'Euler', 'euler_cfg_pp': 'Euler', 'euler_ancestral': 'Euler a',
    'euler_ancestral_cfg_pp': 'Euler a', 'heun': 'Heun', 'heunpp2': 'Heun',
    'dpm_2': 'DPM2', 'dpm_2_ancestral': 'DPM2 a', 'lms': 'LMS', 'dpm_fast': 'DPM fast',
    'dpm_adaptive': 'DPM adaptive', 'dpmpp_2s_ancestral': 'DPM++ 2S a', 'dpmpp_sde': 'DPM++ SDE',
    'dpmpp_sde_gpu': 'DPM++ SDE', 'dpmpp_2m': 'DPM++ 2M', 'dpmpp_2m_sde': 'DPM++ 2M SDE',
    'dpmpp_2m_sde_gpu': 'DPM++ 2M SDE', 'dpmpp_3m_sde': 'DPM++ 3M SDE', 'dpmpp_3m_sde_gpu': 'DPM++ 3M SDE',
    'ddim': 'DDIM', 'uni_pc': 'UniPC', 'uni_pc_bh2': 'UniPC', 'lcm': 'LCM',
}
_SCHED_SUFFIX = {'karras': ' Karras', 'exponential': ' Exponential'}
_NUM_KEYS = ('value', 'seed', 'noise_seed', 'int', 'float', 'number', 'Number', 'Value', 'X', 'a')
_COMBO_KEYS = ('sampler_name', 'scheduler', 'combo', 'value', 'string', 'text', 'name')


def _resolve_number(g, ref, seen=None, depth=0):
    """Follow a numeric node/link input to its literal value (or None). Handles inputs wired through
    primitive/slider nodes — e.g. rgthree `Seed` ({seed:…}) and `mxSlider` ({Xi,Xf,isfloatX})."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, (int, float)):
        return ref
    if isinstance(ref, str):
        try:
            return int(ref) if ref.lstrip('-').isdigit() else float(ref)
        except Exception:
            return None
    if not _is_link(ref) or depth > 32:
        return None
    nid = str(ref[0])
    seen = seen or set()
    if nid in seen:
        return None
    seen = seen | {nid}
    node = g.get(nid)
    if not node:
        return None
    inp = node.get('inputs', {})
    # mxSlider-style int/float pair with a selector flag
    if 'isfloatX' in inp and ('Xf' in inp or 'Xi' in inp):
        return inp.get('Xf') if inp.get('isfloatX') in (1, '1', True) else inp.get('Xi')
    for k in _NUM_KEYS:
        if k in inp and not isinstance(inp[k], bool) and isinstance(inp[k], (int, float, str)):
            r = _resolve_number(g, inp[k], seen, depth + 1)
            if r is not None:
                return r
    for v in inp.values():                      # passthrough: first numeric link
        if _is_link(v):
            r = _resolve_number(g, v, seen, depth + 1)
            if r is not None:
                return r
    return None


def _resolve_combo(g, ref, seen=None, depth=0):
    """Follow a combo/string input (sampler_name, scheduler) to its literal — e.g. `Sampler Selector`
    ({sampler_name:…}) and `Combo Clone` ({combo:…})."""
    if isinstance(ref, str):
        return ref
    if not _is_link(ref) or depth > 32:
        return None
    nid = str(ref[0])
    seen = seen or set()
    if nid in seen:
        return None
    seen = seen | {nid}
    node = g.get(nid)
    if not node:
        return None
    inp = node.get('inputs', {})
    for k in _COMBO_KEYS:
        if isinstance(inp.get(k), str):
            return inp[k]
    for v in inp.values():                      # passthrough: first string link
        if _is_link(v):
            r = _resolve_combo(g, v, seen, depth + 1)
            if r:
                return r
    return None


# ---------------------------------------------------------------------------------------------
# WALKING BACK FROM THE SAVER.
#
# Everything below exists to replace a guess. The old question was "which sampler in this graph has
# the longest positive prompt", which is not a fact about workflows at all -- it is a popularity
# contest on character count, and it picked the cover art's sampler for a song, and an upscale
# pass's for a picture whose prompt merely gained a few quality tags.
#
# The file carries the node that SAVED it. Walking backwards from there -- saver, decoder, sampler,
# model -- answers the question rather than guessing at it, and it is true of every workflow anyone
# will ever build. This REMOVES a rule instead of adding one, which is the opposite shape from the
# name lists elsewhere in this file, each of which has had to grow every time a new pack appeared.
#
# It answers the prompt, the model and the VAE. Generation settings deliberately still come from
# `_pick_base_sampler` -- see the note there.
# ---------------------------------------------------------------------------------------------

# How likely an input is to carry THE PICTURE backwards. Lower wins; a hop costs 1 on top.
#
# THIS IS A LIST, and it is worth being honest about that. The difference from the class-name lists
# is what it is a list OF: ComfyUI's socket names for its own payload types, which every pack
# inherits by using those types, rather than the names individual packs chose for themselves.
# Anything unrecognised stays reachable at _RANK_OTHER, so an omission costs a slower walk and not
# a wrong answer.
_RANK_PAYLOAD = 0        # carries the picture/sound itself
_RANK_RELAY = 2          # might carry it: switches, reroutes, rgthree's Context
_RANK_OTHER = 8          # unknown, but not obviously off-branch
_RANK_OFF_BRANCH = 40    # the model/conditioning side

_PAYLOAD_KEYS = frozenset((
    'samples', 'latent', 'latent_image', 'images', 'image', 'img',
    'audio', 'audio_in', 'video', 'pixels', 'frames', 'video_frames',
))
# Type-agnostic relays. `on_true`/`on_false` are a real switch node in the author's music workflow;
# `base_ctx` is rgthree's Context, which is the node that makes a naive walk go wrong -- it carries
# the model AND the picture, so the ranking is what sends the walk down the picture.
_RELAY_KEYS = frozenset((
    'on_true', 'on_false', 'input', 'value', 'source', 'default', 'any',
    'base_ctx', 'ctx', 'context', 'a', 'b',
))
_OFF_BRANCH_KEYS = frozenset((
    'model', 'clip', 'vae', 'audio_vae', 'clip_vision', 'style_model',
    'positive', 'negative', 'conditioning', 'guider', 'sampler', 'sigmas',
    'noise', 'control_net', 'mask', 'first_frame', 'start_image', 'end_image',
))
_RELAY_PREFIXES = ('any_', 'input_', 'input', 'any')


def _key_rank(k):
    """How eagerly the walk should follow an input named `k`."""
    low = (k or '').lower()
    if low in _PAYLOAD_KEYS:
        return _RANK_PAYLOAD
    if low in _OFF_BRANCH_KEYS:
        return _RANK_OFF_BRANCH
    if low in _RELAY_KEYS:
        return _RANK_RELAY
    # rgthree's Any Switch numbers its inputs any_01..any_NN; other packs use input1..N.
    for p in _RELAY_PREFIXES:
        if low.startswith(p) and low[len(p):].isdigit():
            return _RANK_RELAY
    return _RANK_OTHER


def _walk_back_to_sampler(g, start_id, max_expansions=512, max_cost=400):
    """The sampler that produced what `start_id` consumed, walking BACK along its inputs.

    Best-first rather than depth-first, picture-carrying branches before the model side. That
    ordering is the whole correctness argument: an rgthree Context node hands on the model and the
    latent together, so a plain depth-first walk can arrive at a different stage's sampler and be
    entirely confident about it.

    Returns (sampler_id, path); path is the ids from the first hop through to the sampler, which is
    what lets the caller find the decoder that made THIS picture. (None, []) when nothing is
    reachable inside the budget -- a cycle, a dead end, or a graph shape we don't model.
    """
    start = str(start_id)
    frontier = [(0, 0, start)]
    parents, visited, seq = {}, {start}, 0
    expansions = 0
    while frontier and expansions < max_expansions:
        cost, _, nid = heapq.heappop(frontier)
        if cost > max_cost:
            break
        expansions += 1
        node = g.get(nid)
        if not node:
            continue
        if nid != start and _is_sampler_node(node):
            path, cur = [], nid
            while cur in parents:
                path.append(cur)
                cur = parents[cur]
            return nid, list(reversed(path))
        inp = node.get('inputs') or {}
        # Sorted so the answer depends on the graph and not on JSON key order.
        for k, v in sorted(inp.items(), key=lambda kv: (_key_rank(kv[0]), str(kv[0]))):
            if not _is_link(v):
                continue
            src = str(v[0])
            if src in visited:
                continue
            visited.add(src)             # seeded with the start, so a cycle just exhausts the heap
            parents[src] = nid
            seq += 1
            heapq.heappush(frontier, (cost + _key_rank(k) + 1, seq, src))
    return None, []


def _link_sources(g):
    """Every node id that some other node reads an output from."""
    out = set()
    for n in g.values():
        for v in (n.get('inputs') or {}).values():
            if _is_link(v):
                out.add(str(v[0]))
    return out


# A node that WRITES A FILE is the end of a branch: it eats a picture and hands nothing on. That is
# its whole shape, and every writer in every pack has it -- which is why this is not another list of
# class names. The same reasoning `_is_sampler_node` and `_loader_filename` already record: the four
# names anyone would hardcode were never the four that exist.
def _saver_nodes(g):
    """[(nid, node), ...] for every node that ends a branch by consuming a payload."""
    consumed = _link_sources(g)
    out = []
    for nid, n in g.items():
        nid = str(nid)
        ct = (n.get('class_type') or '').lower()
        inp = n.get('inputs') or {}
        eats = any(_is_link(v) and _key_rank(k) <= _RANK_RELAY for k, v in inp.items())
        if not eats:
            continue
        # A sampler with nothing reading it is a dangling branch, not an output node.
        if _is_sampler_node(n):
            continue
        # Normally an output is a sink. The exception is a saver that passes its images through for
        # someone to preview, so a class that says what it does is admitted either way.
        says_so = ('save' in ct or 'write' in ct or 'export' in ct)
        if nid in consumed and not says_so:
            continue
        out.append((nid, n))
    return sorted(out, key=lambda t: (len(t[0]), t[0]))


# Keys that name a FILE. Resolving a filename down a wire is necessary -- the author's video saver
# is fed its prefix by a naming node -- but resolving every input would walk into the prompt, and a
# prompt is exactly the kind of long string that would then "match" a filename by accident.
_FILENAME_KEYS = ('filename_prefix', 'filename', 'file', 'path', 'output_path',
                  'base_name', 'basename', 'name', 'prefix')


def _node_literals(node):
    """The strings a node carries OUTRIGHT. Used where a wired-in value would be misleading: a
    preview node has no literal of its own, which is what tells it apart from a saver."""
    return [v for v in (node.get('inputs') or {}).values()
            if isinstance(v, str) and v.strip()]


def _node_filenames(g, node):
    """What this node says its file is called, literal or arriving down a wire."""
    out = []
    for k, v in (node.get('inputs') or {}).items():
        if k.lower() not in _FILENAME_KEYS:
            continue
        if isinstance(v, str):
            if v.strip():
                out.append(v)
        elif _is_link(v):
            try:
                sres = _resolve_string(g, v)
            except Exception:
                sres = ''
            if isinstance(sres, str) and sres.strip():
                out.append(sres)
    return out


# What KIND of file this is, for matching a file against what a saver eats. Deliberately wider than
# the sets that decide which files we can READ metadata out of: a FLAC's metadata is unreadable
# today, but a FLAC is still unmistakably audio, and that is all this answers.
_KIND_AUDIO_EXTS = {'.mp3', '.flac', '.wav', '.opus', '.m4a', '.ogg', '.aac'}
_KIND_VIDEO_EXTS = {'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi', '.gif'}


def _file_hints(path=None, chunks=None, stage=None, kind=None):
    """What the FILE says about which node wrote it: {'stem', 'stage', 'kind'}."""
    stem = None
    if path:
        stem = os.path.splitext(os.path.basename(path))[0]
    if kind is None and path:
        ext = os.path.splitext(path)[1].lower()
        kind = ('audio' if ext in _KIND_AUDIO_EXTS else
                'video' if ext in _KIND_VIDEO_EXTS else 'image')
    if stage is None and chunks:
        stage = (chunks.get('vv_set_stage') or '').strip() or None
    return {'stem': stem, 'stage': (stage or None), 'kind': kind}


def _saver_kind(node):
    """image / audio / video / None, from which payload inputs a saver actually eats."""
    keys = {k.lower() for k, v in (node.get('inputs') or {}).items() if _is_link(v)}
    has_img = bool(keys & {'images', 'image', 'img', 'frames', 'video_frames'})
    has_audio = bool(keys & {'audio', 'audio_in'})
    if 'video' in keys or (has_img and has_audio) or 'frames' in keys:
        return 'video'
    if has_audio:
        return 'audio'
    if has_img:
        return 'image'
    return None


def _pick_saver(g, hints=None, saver_id=None):
    """(nid, why) for the node that wrote THIS file. `why` says which evidence decided it.

    Each rule NARROWS the candidates and is skipped whenever it would leave none, so no single
    signal can disqualify the right answer on its own. In particular a filename prefix is positive
    evidence only: the author's own saver carries a stale literal `filename_prefix` of "ComfyUI"
    from before that widget was removed, and treating a mismatch as a veto would reject it.
    """
    cands = _saver_nodes(g)
    if not cands:
        return None, None
    if saver_id is not None and str(saver_id) in {nid for nid, _ in cands}:
        return str(saver_id), 'unique-id'
    hints = hints or {}
    if len(cands) == 1:
        return cands[0][0], 'only'

    # Strongest: the file's name begins with something the node says.
    stem = (hints.get('stem') or '').lower()
    if stem:
        best, best_len = None, 0
        for nid, n in cands:
            for fn in _node_filenames(g, n):
                tail = fn.replace('\\', '/').split('/')[-1].lower()
                if len(tail) >= 3 and stem.startswith(tail) and len(tail) > best_len:
                    best, best_len = nid, len(tail)
        if best:
            return best, 'prefix'

    # The stage this file was saved as, matched against ANY string the node carries. Not a named
    # key: the author's saver holds the literal "Custom..." under `stage` and the real answer under
    # `stage_custom`, so asking one field by name would read the wrong one.
    stage = (hints.get('stage') or '').strip().lower()
    if stage:
        hit = [nid for nid, n in cands
               if any(v.strip().lower() == stage for v in _node_literals(n))]
        if len(hit) == 1:
            return hit[0], 'stage'
        if hit:
            cands = [(nid, n) for nid, n in cands if nid in set(hit)]

    # What kind of file this is, against what the node eats.
    kind = hints.get('kind')
    if kind:
        hit = [(nid, n) for nid, n in cands if _saver_kind(n) == kind]
        if len(hit) == 1:
            return hit[0][0], 'kind'
        if hit:
            cands = hit

    # Last: it has to write a file, so it has to have been told what to call it. A preview has not.
    named = [(nid, n) for nid, n in cands if _node_filenames(g, n) or _node_literals(n)]
    if len(named) == 1:
        return named[0][0], 'writes'
    return None, None


def resolve_output_chain(g, hints=None, saver_id=None):
    """Which sampler made THIS file, found by walking back from the node that saved it.

    The one answer the module now uses for the prompt, the model and the VAE, so the indexed row and
    Export for Civitai can no longer disagree about the same picture. Everything it cannot answer
    falls through to the older rules untouched -- no file is ever worse off than before.

    Returns {'saver', 'sampler', 'path', 'vae_decode', 'method', 'why'}; any value may be None.
    """
    blank = {'saver': None, 'sampler': None, 'path': [], 'vae_decode': None,
             'method': None, 'why': None}
    cands = _saver_nodes(g)
    if not cands:
        return blank
    hints = hints or {}
    sid, why = _pick_saver(g, hints, saver_id)
    sampler, path = (None, [])
    if sid:
        sampler, path = _walk_back_to_sampler(g, sid)
    if sampler:
        out = dict(blank, saver=sid, sampler=sampler, path=path,
                   method='saver-walk', why=why)
    else:
        # Either nothing told the candidates apart, or the one we matched walks to no sampler at all
        # (a saver writing a loaded first frame, which is correct and must not veto). If every
        # candidate agrees, the answer is unambiguous however we got there.
        answers = {}
        for nid, _n in cands:
            s, p = _walk_back_to_sampler(g, nid)
            if s:
                answers[s] = p
        if len(answers) != 1:
            return blank
        s, p = next(iter(answers.items()))
        out = dict(blank, saver=sid, sampler=s, path=p,
                   method='saver-walk-agreed', why='agreed')
    # The decoder that made this picture, taken off the walk's own path rather than searched for by
    # class name -- which is how "CR VAE Decode" and "Vae Decode (mtb)" have been missed until now.
    want_audio = (hints.get('kind') == 'audio')
    for nid in out['path']:
        low = (g.get(nid, {}).get('class_type') or '').lower().replace(' ', '').replace('_', '')
        if 'vaedecode' in low and (want_audio or 'audio' not in low):
            out['vae_decode'] = nid
            break
    return out


def _pick_base_sampler(g):
    """The BASE sampler: the one that made the original image, before any detailer or upscaler.

    Defined structurally as *the sampler with no other sampler upstream of it*, NOT as the first one
    listed. Node order in a saved graph does not have to follow the pipeline, so first-in-file is a
    coin toss wearing a rule. A detailer is by definition fed by something earlier — that is what
    makes it a detailer — so the base is the one nothing else feeds. This holds for a txt2img run
    (its latent is an empty one) and an img2img run (its latent is a loaded image) without having to
    tell the two apart.

    Why the base and not the last: a multi-stage run saves several images and every one of them
    carries the WHOLE graph, so a per-image answer is not available from the file. The base
    generation is the one fact they genuinely share. Detailer settings are deliberately not
    reported.

    Returns None when the graph has no sampler at all.
    """
    samplers = [nid for nid, n in g.items() if _is_sampler_node(n)]
    if not samplers:
        return None
    if len(samplers) == 1:
        return samplers[0]
    sset = set(samplers)

    def has_sampler_upstream(nid):
        # Bounded backward walk over every input, not just the latent one: cheaper to be broad than
        # to maintain a list of which input names carry a latent across every sampler class.
        seen, stack = set(), [nid]
        while stack:
            cur = stack.pop()
            node = g.get(str(cur))
            if not node:
                continue
            for v in node.get('inputs', {}).values():
                if not _is_link(v):
                    continue
                src = str(v[0])
                if src in seen:
                    continue
                seen.add(src)
                if src in sset:
                    return True
                stack.append(src)
        return False

    bases = [s for s in samplers if not has_sampler_upstream(s)]
    if not bases:
        return _pick_sampler(g)     # every sampler fed by another: a cycle, or a shape we don't
                                    # model. Fall back rather than report nothing.
    # More than one independent generation in one graph is rare and has no right answer; take the
    # earliest by node id so at least it is stable across rescans.
    return sorted(bases, key=lambda n: (len(str(n)), str(n)))[0]


def _pick_sampler(g, hints=None, saver_id=None):
    """The sampler node driving the final image — the same answer extract() uses.

    THIS IS WHERE THE EXPORT AND THE LIBRARY USED TO DISAGREE. `list_resources` and `_extract_all`
    both come through here, so while this guessed by longest positive and `_from_graph` guessed
    separately, one picture could be indexed with one sampler's model and exported with another's.
    Both now ask the saver walk first and fall back to the same old rule when it has no answer.
    """
    chain = resolve_output_chain(g, hints, saver_id)
    if chain['sampler'] and chain['sampler'] in g:
        return chain['sampler']
    best_id, best_len = None, -1
    for nid, n in g.items():
        if _is_sampler_node(n):
            pos = n.get('inputs', {}).get('positive')
            ptxt = _follow_to_text(g, pos) if _is_link(pos) else (pos if isinstance(pos, str) else '')
            if len(ptxt or '') > best_len:
                best_len, best_id = len(ptxt or ''), nid
    return best_id


def extract_gen_params(g, sampler_id):
    """Pull {steps, cfg, sampler_name, scheduler, seed} from the sampler node (values may be wired
    from other nodes, so resolve through links). Missing fields come back as None."""
    p = {'steps': None, 'cfg': None, 'sampler_name': None, 'scheduler': None, 'seed': None}
    node = g.get(str(sampler_id))
    if not node:
        return p
    inp = node.get('inputs', {})
    p['steps'] = _resolve_number(g, inp['steps']) if 'steps' in inp else None
    p['cfg'] = _resolve_number(g, inp['cfg']) if 'cfg' in inp else None
    p['sampler_name'] = _resolve_combo(g, inp['sampler_name']) if 'sampler_name' in inp else None
    p['scheduler'] = _resolve_combo(g, inp['scheduler']) if 'scheduler' in inp else None
    p['seed'] = _resolve_number(g, inp['seed']) if 'seed' in inp else None
    if p['seed'] is None and 'noise_seed' in inp:
        p['seed'] = _resolve_number(g, inp['noise_seed'])
    # SamplerCustom(Advanced): steps/sampler/seed/cfg live in one-hop sub-nodes (noise/sampler/
    # sigmas/guider). Best-effort — scan the sampler's linked sources for any field still missing.
    if any(p[k] is None for k in ('steps', 'sampler_name', 'seed')):
        for v in inp.values():
            if not _is_link(v):
                continue
            sub = g.get(str(v[0]), {})
            si = sub.get('inputs', {})
            if p['seed'] is None:
                p['seed'] = _resolve_number(g, si['noise_seed']) if 'noise_seed' in si else p['seed']
            if p['sampler_name'] is None and isinstance(si.get('sampler_name'), (str, list)):
                p['sampler_name'] = _resolve_combo(g, si['sampler_name'])
            if p['steps'] is None and 'steps' in si:
                p['steps'] = _resolve_number(g, si['steps'])
            if p['scheduler'] is None and 'scheduler' in si:
                p['scheduler'] = _resolve_combo(g, si['scheduler'])
            if p['cfg'] is None and 'cfg' in si:
                p['cfg'] = _resolve_number(g, si['cfg'])
    return p


def _fmt_num(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _a1111_sampler(name, scheduler):
    disp = _A1111_SAMPLERS.get(name, name)
    return disp + _SCHED_SUFFIX.get((scheduler or '').lower(), '')


def _extract_loras_raw(g):
    """Like _extract_loras but keeps each LoRA's RAW name (subfolder+ext) for hash resolution."""
    out = []
    for node in g.values():
        ct = node.get('class_type', '')
        inp = node.get('inputs', {})
        if _is_lora_node(node):
            name = inp.get('lora_name')
            if isinstance(name, str):
                out.append({'name': _lora_display_name(name),
                            'strength': inp.get('strength_model', inp.get('strength')), 'raw': name})
        elif 'Power Lora Loader' in ct:
            for k, v in inp.items():
                if k.startswith('lora_') and isinstance(v, dict) and v.get('on') and v.get('lora'):
                    out.append({'name': _lora_display_name(v['lora']),
                                'strength': v.get('strength'), 'raw': v['lora']})
        elif 'lora' in ct.lower():       # a stacker: the whole list in one JSON string
            for e in _lora_stack(inp):
                out.append({'name': _lora_display_name(e['raw']),
                            'strength': e['strength'], 'raw': e['raw']})
    seen, res = set(), []
    for l in out:
        if l['name'] not in seen:
            seen.add(l['name'])
            res.append(l)
    return res


def _first_loader_ckpt(g):
    for n in g.values():
        v = _loader_filename(n)
        if v:
            return v
    return None


def _format_parameters(meta, params, width, height, hash_resolver=None, raw_ckpt=None, raw_loras=None):
    """Assemble the A1111 `parameters` text block. If `hash_resolver(category, raw_name)` is given,
    resources it can resolve get `Model hash` / `Lora hashes` (AutoV2) for Civitai auto-linking."""
    loras = raw_loras if raw_loras is not None else (meta.get('loras') or [])
    pos = (meta.get('positive') or '').strip()
    lora_tags = ' '.join(
        f"<lora:{l['name']}:{_fmt_num(l.get('strength') if l.get('strength') is not None else 1)}>"
        for l in loras if l.get('name'))
    line1 = (pos + (' ' + lora_tags if lora_tags else '')).strip()
    # A1111 OMITS the negative line when there's no negative (see create_infotext) — emitting a bare
    # placeholder is off-spec and breaks parsers that expect `Negative prompt: (.+)`, which is how
    # Flux/Krea images (ConditioningZeroOut -> empty negative) lost their prompt on Civitai.
    lines = [line1]
    neg = (meta.get('negative') or '').strip()
    if neg:
        lines.append('Negative prompt: ' + neg)
    parts = []
    if params.get('steps') is not None:
        parts.append(f"Steps: {int(params['steps'])}")
    if params.get('sampler_name'):
        parts.append(f"Sampler: {_a1111_sampler(params['sampler_name'], params.get('scheduler'))}")
    if params.get('cfg') is not None:
        parts.append(f"CFG scale: {_fmt_num(params['cfg'])}")
    if params.get('seed') is not None:
        parts.append(f"Seed: {int(params['seed'])}")
    if width and height:
        parts.append(f"Size: {int(width)}x{int(height)}")
    model_hash = hash_resolver('checkpoints', raw_ckpt) if (hash_resolver and raw_ckpt) else None
    if model_hash:
        parts.append(f"Model hash: {model_hash}")
    if meta.get('model_name'):
        parts.append(f"Model: {meta['model_name']}")
    if hash_resolver:                           # per-LoRA AutoV2 hashes Civitai matches on
        lh = []
        for l in loras:
            h = hash_resolver('loras', l.get('raw')) if l.get('raw') else None
            if h:
                lh.append(f"{l['name']}: {h}")
        if lh:
            parts.append('Lora hashes: "' + ', '.join(lh) + '"')
    if parts:
        lines.append(', '.join(parts))
    return '\n'.join(lines)


def list_resources(path):
    """The checkpoint + LoRA resources referenced by a ComfyUI PNG, as [(category, raw_name), ...]
    with category 'checkpoints'|'loras' and raw_name the value ComfyUI stored (subfolder+ext). Used
    to pre-resolve/hash before an export so the work can be shown with progress. [] if no graph."""
    try:
        chunks = read_png_text_chunks(path)
        g = json.loads(chunks.get('prompt') or 'null')
    except Exception:
        return []
    if not isinstance(g, dict):
        return []
    g = {k: v for k, v in g.items() if isinstance(v, dict)}
    out = []
    sid = _pick_sampler(g, _file_hints(path, chunks))
    ckpt = (_resolve_model(g, sid) if sid else None) or _first_loader_ckpt(g)
    if ckpt:
        out.append(('checkpoints', ckpt))
    for l in _extract_loras_raw(g):
        if l.get('raw'):
            out.append(('loras', l['raw']))
    return out


def _extract_all(path):
    """Pull (meta, params, raw_ckpt, raw_loras) from a ComfyUI PNG's graph, or None if it has no
    usable graph. Shared by the A1111 and the human-readable builders."""
    meta = extract(path)                        # positive / negative / model_name / loras
    chunks = read_png_text_chunks(path)
    pj = chunks.get('prompt')
    if not pj:
        return None
    try:
        g = json.loads(pj)
    except Exception:
        return None
    if not isinstance(g, dict):
        return None
    g = {k: v for k, v in g.items() if isinstance(v, dict)}
    if not (meta.get('positive') or meta.get('model_name')):
        return None                             # nothing worth exporting
    # TWO IDS ON PURPOSE, and only until the settings question is answered. The model comes from the
    # saver walk, so an exported file names the checkpoint that actually made it. The SETTINGS keep
    # whatever rule they had, because moving those is a separate decision the author has parked --
    # and an export that suddenly disagreed with the row beside it would be the worst of both.
    sid = _pick_sampler(g, _file_hints(path, chunks))
    sid_params = _pick_sampler(g)
    params = extract_gen_params(g, sid_params) if sid_params else {}
    raw_ckpt = (_resolve_model(g, sid) if sid else None) or _first_loader_ckpt(g)
    raw_loras = _extract_loras_raw(g)
    return meta, params, raw_ckpt, raw_loras


def build_civitai_parameters(path, width=None, height=None, hash_resolver=None):
    """Build the A1111 `parameters` text for a ComfyUI PNG, or None if it has no usable graph.
    `hash_resolver(category, raw_name) -> autov2|None` (optional) adds resource hashes."""
    r = _extract_all(path)
    if not r:
        return None
    meta, params, raw_ckpt, raw_loras = r
    return _format_parameters(meta, params, width, height, hash_resolver, raw_ckpt, raw_loras)


def build_readable_parameters(path, width=None, height=None, include_params=True):
    """Build a human-readable, labeled metadata block for copy-paste (one value per row), or None
    if the PNG has no usable graph. `include_params=False` drops the generation settings — used for
    video (a video's paired PNG carries the graph, but sampler/CFG/seed/size don't matter there)."""
    r = _extract_all(path)
    if not r:
        return None
    meta, params, _raw_ckpt, raw_loras = r
    return _format_readable(meta, params, width, height, raw_loras, include_params)


def _format_readable(meta, params, width, height, raw_loras=None, include_params=True):
    """Assemble the labeled copy-paste block: each field's label on its own row, value(s) below,
    LoRAs as names only (one per row), a blank line between sections. Empty fields are omitted."""
    loras = raw_loras if raw_loras is not None else (meta.get('loras') or [])
    clean = lambda s: (s or '').replace('<', '').replace('>', '')   # drop angle brackets; some
                                                                    # target fields choke on <...>
    sections = []                               # (label, value-block) pairs, in paste order
    pos = clean(meta.get('positive')).strip()
    sections.append(('Positive prompt:', pos or '(none)'))
    neg = clean(meta.get('negative')).strip()
    if neg:
        sections.append(('Negative prompt:', neg))
    model = clean(meta.get('model_name')).strip()
    if model:
        sections.append(('Model:', model))
    names = [clean(l['name']).strip() for l in loras if l.get('name')]
    names = [n for n in names if n]
    if names:
        sections.append(('LoRAs:', '\n'.join(names)))
    out = []
    for label, val in sections:
        if out:
            out.append('')                      # blank line between sections
        out.append(label)
        out.append(val)
    if include_params:
        settings = []
        if params.get('steps') is not None:
            settings.append(f"Steps: {int(params['steps'])}")
        if params.get('sampler_name'):
            settings.append(f"Sampler: {_a1111_sampler(params['sampler_name'], params.get('scheduler'))}")
        if params.get('cfg') is not None:
            settings.append(f"CFG scale: {_fmt_num(params['cfg'])}")
        if params.get('seed') is not None:
            settings.append(f"Seed: {int(params['seed'])}")
        if width and height:
            settings.append(f"Size: {int(width)}x{int(height)}")
        if settings:
            out.append('')
            out.extend(settings)
    return '\n'.join(out)


def _png_chunk(ctype, data):
    return struct.pack('>I', len(data)) + ctype + data + struct.pack('>I', zlib.crc32(ctype + data) & 0xffffffff)


def splice_parameters_png(src_path, text):
    """Return a new PNG (bytes) = the source image with ALL existing text chunks removed and one
    `parameters` chunk added. Dropping ComfyUI's own `prompt`/`workflow` chunks stops Civitai from
    falling back to its broken graph parser. Pixels (IDAT) are copied verbatim — no re-encode."""
    with open(src_path, 'rb') as f:
        raw = f.read()
    sig = b'\x89PNG\r\n\x1a\n'
    if raw[:8] != sig:
        raise ValueError('not a PNG')
    try:                                        # tEXt is latin-1; fall back to UTF-8 iTXt if needed
        param_chunk = _png_chunk(b'tEXt', b'parameters\x00' + text.encode('latin-1'))
    except UnicodeEncodeError:
        param_chunk = _png_chunk(
            b'iTXt', b'parameters\x00\x00\x00\x00\x00' + text.encode('utf-8'))
    out = bytearray(sig)
    i, n, inserted = 8, len(raw), False
    while i + 8 <= n:
        length, ctype = struct.unpack('>I4s', raw[i:i + 8])
        chunk = raw[i:i + 12 + length]          # 8 header + length data + 4 CRC
        i += 12 + length
        if ctype in (b'tEXt', b'zTXt', b'iTXt'):
            continue                            # drop every existing text chunk
        if ctype == b'IEND':
            out += param_chunk
            inserted = True
            out += chunk
            break
        out += chunk
    if not inserted:                            # defensive: no IEND seen
        out += param_chunk
    return bytes(out)


if __name__ == '__main__':
    import sys
    import glob
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    folder = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, 'samples')
    for p in sorted(glob.glob(os.path.join(folder, '*.png'))):
        r = extract(p)
        print('=' * 90)
        print(os.path.basename(p))
        print(f"  model    : {r['model_name']}   ({r['model']})")
        print(f"  method   : {r['method']}")
        pos = (r['positive'] or '').replace('\n', ' ')
        neg = (r['negative'] or '').replace('\n', ' ')
        print(f"  POSITIVE ({len(r['positive'] or '')} chars): {pos[:500]}")
        print(f"  negative ({len(r['negative'] or '')} chars): {neg[:150]}")

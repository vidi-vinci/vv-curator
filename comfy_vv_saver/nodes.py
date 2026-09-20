"""VV nodes for ComfyUI — two of them.

    VV Run Name    defines one run: names everything a press of Generate produces, and writes
                   the .txt of metadata for any file a non-VV node saves
    VV Save Image  saves the picture with its generation settings read off the graph

Why trace instead of asking: real workflows drive a sampler's steps/cfg/sampler_name/scheduler/seed
from separate control nodes (mxSlider, Sampler Selector, Combo Clone, rgthree Seed), which leaves the
sampler's own widget values stale. `vendor/comfy_meta.py` already resolves those links (it is a
verbatim copy of the viewer's module, so these nodes and the viewer's "Export for Civitai" emit the
same format). Prompts are the one thing NOT auto-traced by default: they are assembled by arbitrary
string nodes, so wiring the final string in is exact, with a trace as fallback.

Why only two. Everything else this package had — Save Context, Stage Prefix, a node that wrote the
video's .txt — existed to work around the fact that a video could not carry a set id, so it had to
be identified by its filename lining up with a still's. The run code put the identity IN the
filename, which made file type stop mattering: one `filename_prefix` string serves any writer, and
a second filename is a second VV Run Name chained off the first. Two is the floor rather than one only
because you need a saver per stage (see VVSaveImageCivitai).
"""
import hashlib
import json
import os
import random
import re
import time

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths

from .vendor import comfy_meta, model_hash

_HASH_CACHE = os.path.join(os.path.dirname(__file__), "model_hashes.db")

# Suggested output stages. The saver's `stage` is free text (so a one-off stage needs no code
# change); these are just the conventional values. Blank = append nothing (plain drop-in use).
# The stage names the VIEWER actually understands. index_db.SET_STAGE_ORDER holds exactly these,
# and it uses them to order a set's panes and to pick which member fronts the card.
#
# A name the viewer does NOT know — anything typed into `Custom...` — is no longer arbitrary: since
# 2026-09-07 it is treated as the stage BEFORE 'raw', so it sorts to the front of the set and never
# fronts the card. That matches how the field is actually used (a word like "First" for the shot a
# run starts from) and it means the escape hatch costs a set nothing. What it still cannot express
# is a custom stage that belongs in the MIDDLE or at the END of a pipeline; two different custom
# names in one set tie and fall back to filename order.
# The dropdown stays a dropdown for the same reason as before: the five values above are a key into
# a vocabulary the user cannot see, and typing one of them slightly wrong is not a typo the viewer
# can catch.
# Keep in step with index_db.SET_STAGE_ORDER.
STAGES = ["Raw", "Detail", "Refine", "Upscale", "Final"]
STAGE_NONE = "(none)"        # append nothing — a plain SaveImage drop-in, and the video-still case
STAGE_CUSTOM = "Custom…"     # use the stage_custom field, for a one-off the vocabulary lacks
STAGE_CHOICES = STAGES + [STAGE_NONE, STAGE_CUSTOM]


def _resolve_stage(stage, stage_custom):
    """The stage suffix to actually use. Blank means append nothing."""
    if stage == STAGE_NONE:
        return ""
    raw = stage_custom if stage == STAGE_CUSTOM else stage
    return _ILLEGAL.sub("", raw or "").strip()   # not _sanitize: blank must stay blank

_ILLEGAL = re.compile(r'[\\/:*?"<>|~]')


def _sanitize(name):
    """Strip characters Windows forbids in a path segment (notably ':' — a time like 21:12:26 is
    not a legal filename here, which is why the time below is hyphenated).

    '~' goes too, though Windows allows it: it is the run code's marker, and the code is located by
    searching the name for it. A series called "before~after" would put a second one in front of
    ours, and a matcher that has to decide WHICH tilde is the real one is a matcher that can be
    wrong. Reserving the character costs a series name nothing and makes the question unaskable."""
    out = _ILLEGAL.sub("", name or "").strip()
    return out or "untitled"


def _graph(prompt):
    """The executed prompt as {node_id: {class_type, inputs}} — the shape comfy_meta expects."""
    if not isinstance(prompt, dict):
        return {}
    return {k: v for k, v in prompt.items() if isinstance(v, dict)}


def _auto_set_id(prompt):
    """Fallback set id when no Generation ID node is wired. Every save node in one execution gets
    the identical `prompt`, so this hash is identical across MAIN/DET/REFINE of the same run.
    Trade-off vs the wired node: not date-readable, and an identical re-run reuses the id."""
    try:
        blob = json.dumps(prompt, sort_keys=True, separators=(",", ":")).encode("utf-8", "ignore")
    except Exception:
        return ""
    return "auto-" + hashlib.sha256(blob).hexdigest()[:12]


def _by_basename(category, raw):
    """The file for a resource ComfyUI's own lookup cannot find, matched on its name without an
    extension -- or None.

    STORED WITHOUT ITS EXTENSION. A LoRA wired through ComfyUI Lora Manager (and rgthree's Power
    Lora Loader reading from it) records `KreaKult_v2`, not `KreaKult_v2.safetensors`, and
    `get_full_path` wants the exact filename. So the LoRA appeared in the block by name and never
    carried a hash -- which on Civitai is the difference between a resource it NAMES and one it
    LINKS to its model page. The checkpoint was unaffected, because a checkpoint's stored name
    keeps its extension, which is why a post came up with the model linked and the LoRAs not.

    comfy_meta's own resolver has had this fallback since the reader learned to read Lora Manager
    (model_hash.resolve_file); this side never got it. Only tried when the exact lookup has already
    failed, so a real filename is never second-guessed.
    """
    stem = os.path.splitext(str(raw).replace("\\", "/").split("/")[-1])[0].lower()
    if not stem:
        return None
    try:
        names = folder_paths.get_filename_list(category)
    except Exception:
        return None
    for name in names or ():
        if os.path.splitext(str(name).replace("\\", "/").split("/")[-1])[0].lower() == stem:
            try:
                path = folder_paths.get_full_path(category, name)
            except Exception:
                path = None
            if path:
                return path
    return None


def _make_hash_resolver():
    """(category, raw_name) -> Civitai AutoV2 hash, using ComfyUI's own model resolution.
    Returns None for anything it can't resolve, so a resource still appears as text but never
    links to the wrong file. First hash of a big checkpoint is slow; cached by (path,size,mtime)."""
    def resolve(category, raw):
        if not raw:
            return None
        try:
            path = folder_paths.get_full_path(category, raw)
        except Exception:
            path = None
        if not path:
            path = _by_basename(category, raw)
        if not path:
            return None
        digest = model_hash.sha256_cached(path, _HASH_CACHE)
        return digest[:10] if digest else None
    return resolve


def _parameters_text(prompt, width, height, positive, negative, link_models, saver_id=None):
    """The A1111 metadata block for a run, read entirely off the executed graph.

    Module-level and shared: the image saver puts this in the PNG's `parameters` chunk and VV Run
    Name writes the same string into the `.txt` beside whatever a foreign node saved, so a run's
    picture and its video (or song) can never describe themselves differently. Width/height of 0
    omit the Size line, which is what a non-image wants -- VV Run Name has nothing to measure.

    There are no overrides any more. Every value here comes from the graph.

    `saver_id` IS THE ONE THING THIS SIDE KNOWS AND THE VIEWER NEVER CAN. The reader finds which
    sampler made a file by walking back from the node that saved it, and outside ComfyUI it has to
    work out WHICH node that was from the filename and the stage. In here the node is running: it
    knows its own id, so the walk starts from the right place with nothing inferred at all.
    """
    g = _graph(prompt)
    if not g:
        return ""
    sid = comfy_meta._pick_sampler(g, saver_id=saver_id)
    params = comfy_meta.extract_gen_params(g, sid) if sid else {}

    raw_ckpt = (comfy_meta._resolve_model(g, sid) if sid else None) or comfy_meta._first_loader_ckpt(g)
    raw_loras = comfy_meta._extract_loras_raw(g)

    pos = positive if positive else _text_from(g, sid, "positive")
    neg = negative if negative else _text_from(g, sid, "negative")

    # No real negative (Flux/Krea etc., CFG=1): the negative input traces back to the same text
    # node as the positive -- through a zero-out, or through an rgthree Context whose outputs the
    # walk cannot tell apart -- so the block would state the prompt twice and publish the second
    # copy as a negative. comfy_meta._from_graph has dropped this duplicate since the reader was
    # written; the saver never did, so a Krea image arrived on Civitai with a bogus negative.
    if neg and neg == pos:
        neg = ""

    model_name = ""
    if raw_ckpt:
        model_name = os.path.splitext(raw_ckpt.replace("\\", "/").split("/")[-1])[0]

    meta = {"positive": pos, "negative": neg, "model_name": model_name, "loras": raw_loras}
    resolver = _make_hash_resolver() if link_models else None
    return comfy_meta._format_parameters(meta, params, width, height,
                                         resolver, raw_ckpt, raw_loras)


# Our own nodes write their own metadata, so they are not "foreign" -- see _feeds_foreign_writer.
_OUR_NODES = ("VVSaveImageCivitai", "VVNameAndId")


def _feeds_foreign_writer(prompt, unique_id, slot=1):
    """Is this node's `filename` output wired to a node we did not write?

    The whole reason there is no switch for the .txt. A node that is not ours, taking our
    filename, is about to write a file under our name that will carry no `parameters` chunk of its
    own -- a video most often, but SaveAudio takes a filename_prefix too, and nothing here looks for
    a file type. Wiring it up IS the statement of intent, so nothing has to be enabled and nothing
    can be forgotten, and a workflow that only saves pictures never grows a stray .txt beside them.

    web/vv_meta_indicator.js mirrors this rule on the node face. If this logic changes, that changes
    with it, or the node will state something that is not true.

    Reads links out of the executed prompt, which is the same structure comfy_meta walks: an input
    is either a literal or `[source_node_id, output_slot]`.
    """
    g = _graph(prompt)
    if not g or unique_id is None:
        return False
    me = str(unique_id)
    for nid, node in g.items():
        if str(nid) == me or node.get("class_type") in _OUR_NODES:
            continue
        for ref in (node.get("inputs") or {}).values():
            if comfy_meta._is_link(ref) and str(ref[0]) == me and int(ref[1]) == slot:
                return True
    return False


def _sidecar_name_for(resolved_prefix):
    """The sidecar's filename (no extension) for a resolved prefix.

    Truncated at the run code when there is one, so a whole generation shares ONE text file however
    many videos it wrote -- base, interp and the muxed -audio copy all find it, because the viewer
    matches up to the code and ignores everything after. Without a code the full prefix is used and
    the viewer falls back to stripping the counter.
    """
    m = comfy_meta._CODE_RE.search(resolved_prefix)
    return resolved_prefix[:m.end()] if m else resolved_prefix


def _write_sidecar(filename_prefix, ctx, prompt):
    """Write the `.txt` that gives a video its PROMPT.

    THE ORIGINAL REASON FOR THIS FILE IS GONE, AND IT IS STILL NEEDED. It used to say a video
    carries no metadata of its own and the viewer never opens one; both stopped being true in
    August 2026, when videos started being read at scan. An MP4 from ComfyUI carries the same
    `prompt` graph a PNG does, byte for byte, and the viewer gets model, steps, sampler, seed and
    LoRAs straight out of it.

    What it does NOT carry is the equivalent of a PNG's `parameters` chunk -- and that chunk, not
    the graph, is where a prompt actually comes from for the video models. Verified 2026-09-05 on a
    MiniMax H3 run: the tracer returns nothing for `positive` on that graph, from the MP4 and from
    the PNG alike, because the workflow keeps its text in nodes the tracer cannot follow. The PNG
    reads fine only because this saver ALSO writes it a `parameters` chunk. MP4 has no such slot.

    So this file is that chunk, kept beside the video instead of inside it.

    IT CANNOT BE REPLACED BY BORROWING THE PAIRED STILL, which is the obvious-looking alternative.
    A txt2vid run has no start image, so it produces no PNG at all and there is nothing to borrow
    from. Requiring a workflow that generates a throwaway first frame purely to give the metadata
    somewhere to live is a thing one person can remember and not a thing to ask of anyone else.

    It is named up to the run code and stops, which is what lets it be written now, before the video
    exists and long before anyone knows the counter ComfyUI will give it.
    """
    try:
        body = _parameters_text(prompt, 0, 0, ctx.get("positive"), ctx.get("negative"),
                                link_models=False)   # never uploaded, so skip reading GBs
        sid = (ctx.get("set_id") or "").strip() or _auto_set_id(prompt or {})
        if sid:
            # LAST, deliberately. A1111's negative prompt runs from its label to the end of the
            # body, so this line sitting between them would be swallowed into the negative by any
            # ordinary parser. The viewer lifts our keys out first and would cope either way; a
            # human pasting the file into Civitai would not.
            body = f"{body}\nVV set id: {sid}"
        full_folder, name, _c, _s, _p = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_output_directory(), 0, 0)
        os.makedirs(full_folder, exist_ok=True)
        with open(os.path.join(full_folder, _sidecar_name_for(name) + ".txt"),
                  "w", encoding="utf-8") as f:
            f.write(body.strip() + "\n")
    except Exception as e:
        print(f"[VV] video info file failed: {e}")   # never take a run down over a text file


def _text_from(g, sampler_id, key):
    """Trace a sampler's positive/negative conditioning back to its CLIPTextEncode text."""
    node = g.get(str(sampler_id)) if sampler_id else None
    if not node:
        return ""
    ref = node.get("inputs", {}).get(key)
    if comfy_meta._is_link(ref):
        return comfy_meta._follow_to_text(g, ref) or ""
    return ref if isinstance(ref, str) else ""


class VVNameAndId:
    """Sets a run up — its name, its set id, its run code, and the context every saver reads.

    Emits, for series "womanInCemetery" at 21:12:26 on 2026-07-16:
        filename_prefix = womanInCemetery_7_16_26/womanInCemetery_21-12-26~vv3g05o5
        set_id          = 20260716-211226-a3f9

    Each saver appends its own stage, giving
        womanInCemetery_7_16_26/womanInCemetery_21-12-26~vv3g05o5_Final_00001_.png

    Both the date/time and the id are computed ONCE per run here and fanned out, so every image in
    a set shares them and differs only by stage. A per-save timestamp cannot do that: savers run
    seconds apart and would disagree.

    The `~vv……` on the end is the RUN CODE (comfy_meta.make_run_code), and it is the same identity
    as `set_id` written where every file type can carry it. A set id lives in a PNG text chunk, so
    a video has never been able to hold one; the code is in the name, so a video, a sidecar and an
    mp4 from a saver we have never heard of all carry it equally. The viewer matches on the name up
    to and including the code and ignores the rest, which is what lets a sidecar be named before the
    video exists — see the module note in comfy_meta.py for why it is marked and clock-derived.

    It goes on the FILE half of the prefix, never the folder: a folder is a day, not a run, and a
    code on it would put every run of a day in its own directory.

    It also bundles the prompts (which Save Context used to do) and emits the `filename_prefix` any
    other saving node takes (which Stage Prefix used to do). Both of those were nodes and wires that
    always went to the same place, so both are gone — deleted rather than kept as legacy stubs,
    because the missing-node placeholder an old workflow shows sits exactly where the node you were
    going to delete used to be. A dead class forever to avoid one placeholder is the worse trade.

    NOT VIDEO-ONLY, though the comments here long implied it. `filename_prefix` is what ComfyUI's
    save nodes all call their input — SaveAudio as much as VHS Video Combine — and
    `_feeds_foreign_writer` looks for "a node that is not ours", never for video. The author caught this
    on 2026-09-06 while renaming the sockets ("what about a music file?"), and he is right: the old
    "a video, in practice" described his habit as though it were a rule.

    SO THIS NODE HAS A SECOND JOB ITS NAME DOES NOT ADVERTISE — it is the metadata writer for
    anything a non-VV node saves, triggered purely by what `filename_prefix` is wired into. That is
    why the node face carries a live "Saving prompt + settings to .txt" line (see
    web/vv_meta_indicator.js):
    invisible-but-conditional was the single most confusing thing about it.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "name": ("STRING", {
                    "default": "untitled",
                    "tooltip": "What you are making. Becomes the folder and the start of every "
                               "filename, e.g. MM_FinJungle."}),
                # Renamed from `label` 2026-09-06. It only ever changed the filename_prefix OUTPUT,
                # never the images VV Save Image writes -- an asymmetry nothing on screen admitted
                # to, and the single biggest reason the old name read as meaningless.
                "name_suffix": ("STRING", {
                    "default": "",
                    "tooltip": "Optional word added to the filename_prefix output ONLY -- your "
                               "saved images are unaffected. Use it when one run writes two files "
                               "and you want to tell them apart, e.g. HiRes."}),
            },
            "optional": {
                # Spelled out, because KSampler's `positive`/`negative` are CONDITIONING and these
                # are raw text. Borrowing the sampler's exact words for a different kind of thing
                # made them look interchangeable.
                "positive_prompt": ("STRING", {"forceInput": True, "tooltip":
                                    "Your final positive prompt, as text. Wire the end of your "
                                    "prompt chain here and every saver records exactly that."}),
                "negative_prompt": ("STRING", {"forceInput": True, "tooltip":
                                    "Your final negative prompt, as text."}),
                # Chaining, which is what removes the need for a separate video node. Wire one of
                # these into another and the second reuses the FIRST's run code, id and prompts,
                # changing only the suffix -- so a second file is named apart while staying part of
                # the same generation. Two of the same node beats a special-case node nobody can
                # guess the purpose of.
                "inherit_run": ("VV_NAMING", {"tooltip":
                                "Optional. Wire another VV Run Name's run_data here to reuse its "
                                "run, so both belong to one generation and only the suffix "
                                "differs. Leave empty and this node starts a fresh run."}),
            },
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }

    # The type string stays VV_NAMING on purpose: ComfyUI validates links against it, so changing
    # it would invalidate the run->saver wire in every saved workflow. It is internal; the socket
    # LABEL is the part anyone reads.
    RETURN_TYPES = ("VV_NAMING", "STRING")
    RETURN_NAMES = ("run_data", "filename_prefix")
    FUNCTION = "make"
    CATEGORY = "VV"
    DESCRIPTION = ("Defines one run -- everything a single press of Generate produces -- so it "
                   "lands together in the VV Curator.\n\n"
                   "Wire `run_data` into each VV Save Image. Wire `filename_prefix` into any OTHER "
                   "saving node (video, audio, anything with a filename_prefix input); doing so "
                   "also writes a .txt beside that file carrying the prompt and settings, because "
                   "those formats have nowhere inside them to keep it.\n\n"
                   "One per workflow, unless you want a second filename for the same run.")

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")     # NaN != NaN -> never cached, so each run mints a fresh name + id

    def make(self, name, name_suffix="", positive_prompt=None, negative_prompt=None,
             inherit_run=None, prompt=None, unique_id=None):
        positive, negative = positive_prompt, negative_prompt
        parent = inherit_run or {}
        if parent.get("filename_prefix"):
            # Chained: inherit the run wholesale. Re-deriving the time here would mint a second
            # code seconds later, which is precisely the disagreement one-node-per-run exists to
            # prevent -- the two files would then be two generations.
            prefix = parent["filename_prefix"]
            set_id = parent.get("set_id") or ""
            positive = positive if positive is not None else parent.get("positive")
            negative = negative if negative is not None else parent.get("negative")
        else:
            s = _sanitize(name)
            # One instant drives all three, so the name, the id and the code can never describe
            # different moments of the same run.
            t = time.time()
            now = time.localtime(t)
            # Built by hand rather than strftime: %-m/%#m (no leading zero) is platform-specific.
            date = f"{now.tm_mon}_{now.tm_mday}_{now.tm_year % 100:02d}"      # 7_16_26
            clock = f"{now.tm_hour:02d}-{now.tm_min:02d}-{now.tm_sec:02d}"    # 21-12-26 (':' illegal)
            prefix = f"{s}_{date}/{s}_{clock}{comfy_meta.make_run_code(t)}"
            set_id = f"{time.strftime('%Y%m%d-%H%M%S', now)}-{random.randrange(16 ** 4):04x}"

        lab = _ILLEGAL.sub("", name_suffix or "").strip()   # not _sanitize: blank must stay blank
        filename = f"{prefix}_{lab}" if lab else prefix
        out = {"filename_prefix": prefix, "set_id": set_id,
               "positive": positive or "", "negative": negative or ""}

        # The sidecar, written only when something OTHER than our own saver is consuming this
        # filename -- i.e. when a node we do not control is about to write a file under our name and
        # will carry no metadata of its own. Wiring it up IS the intent, so there is nothing to
        # switch on and nothing to forget, and an image-only workflow never grows a stray .txt.
        if _feeds_foreign_writer(prompt, unique_id):
            _write_sidecar(filename, out, prompt)
        return (out, filename)


class VVSaveImageCivitai:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.prefix_append = ""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "stage": (STAGE_CHOICES, {"default": "Final", "tooltip":
                          "Which step of your pipeline made this picture. Added to the filename, "
                          "and the viewer uses it to order a set and pick which one fronts the "
                          "card. Anything off this list counts as the step before Raw."}),
                # DIRECTLY UNDER `stage`, and in `required` for that reason alone: ComfyUI draws
                # every required widget before every optional one, so this is the only way for the
                # two halves of one question to sit together. It was two rows below, with
                # the Civitai switch and filename_prefix between them (the author, 2026-09-06). Being
                # required costs it nothing -- it has a default and is read only when `stage` is
                # Custom…, exactly as before.
                "stage_custom": ("STRING", {"default": "", "tooltip":
                                 "Read only when stage is Custom…. A name the viewer doesn't know "
                                 "counts as the step before Raw: it sorts to the front of a set, "
                                 "and is never the one that fronts the card."}),
                # write_a1111 and embed_comfy_wf used to live here. Both are gone: this node exists
                # to write Civitai-ready metadata and a readable graph, and an option to do neither
                # is not a setting, it is a way to switch the node off and forget you did. (Which is
                # exactly what happened — months later the missing Civitai data read as a bug.)
                # This one STAYS: it can mean reading 20GB off disk, so it is a real trade, and it
                # is named for what it BUYS rather than what it does.
                # `use_` because this is a switch and a bare `civitai_links` reads as a noun -- a
                # value the node holds rather than a choice you make. The author's, 2026-09-06.
                "use_civitai_links": ("BOOLEAN", {"default": True, "tooltip":
                                      "Fingerprint the checkpoint and LoRAs so Civitai links to "
                                      "them automatically. Slow the first time it sees a model (it "
                                      "reads the whole file), instant after."}),
            },
            "optional": {
                "run_data": ("VV_NAMING", {"tooltip":
                             "From VV Run Name. Supplies the folder, the filename and the prompts, "
                             "so every saver in the run agrees. Without it this saver still works "
                             "and still traces everything -- it just names files ComfyUI_00001_."}),
                # `filename_prefix` USED TO SIT HERE and was removed 2026-09-06. Its only job was
                # letting this node stand in for core SaveImage with no VV Run Name in the graph --
                # a mode the README never shows and the author had never used. Meanwhile it displayed
                # "ComfyUI" on every saver while being ignored, because run_data wins.
                #
                # Removing it also DELETES A TRAP rather than labelling one. VV Run Name emits a
                # `filename_prefix` too, so the matching names invited a wire between them that
                # half-works: the name would be right, but the set id would fall back to a graph
                # hash and the prompts would be traced instead of taken from the ones you wired.
                # Quietly degraded is the worst failure mode, and now the wire cannot be made.
                # The BEHAVIOUR survives as save()'s own default, so an unwired saver still writes
                # ComfyUI_00001_.png exactly as core SaveImage does.
                #
                # stage_custom also used to sit here, two rows below the dropdown it belongs to.
                # model_name / sampler / scheduler / steps / cfg / seed_override used to sit here as
                # manual OVERRIDES of the traced values. They are gone, and their absence is the
                # point: this node exists because hand-set widgets drift from what actually made the
                # image, and offering six of them reproduced that exact defect in the node built to
                # prevent it. Six boxes that should always be empty also read as settings you are
                # meant to fill in. If a value comes out wrong the tracer is wrong -- fix the tracer.
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO",
                       "unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    # `civitai_data` pairs with the `run_data` input: same vocabulary going in and coming out. It
    # was "metadata text", the one socket in the package with a space in its name.
    RETURN_NAMES = ("images", "civitai_data")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "VV"
    DESCRIPTION = (
        "Saves the picture, then reads your actual workflow for the prompt, model, LoRAs, steps, "
        "CFG, sampler and seed and writes those into the file — so Civitai can read them and the "
        "VV Curator can group everything this run produced.\n\n"
        "Nothing to set: the settings come from the graph, so they can never drift from what really "
        "made the image. Use one per stage.")

    def save(self, images, stage, use_civitai_links, run_data=None,
             stage_custom="", prompt=None, extra_pnginfo=None, unique_id=None):
        # `filename_prefix` is a plain default here rather than a widget: the field was removed on
        # 2026-09-06 (see INPUT_TYPES), and this is what keeps an unwired saver behaving exactly
        # like core SaveImage instead of failing.
        filename_prefix = "ComfyUI"
        # Wired run_data supplies the shared values; without it every one of these stays empty and
        # the tracer fills them from the graph.
        ctx = run_data or {}
        positive = ctx.get("positive") or None
        negative = ctx.get("negative") or None
        set_id = (ctx.get("set_id") or "").strip()
        filename_prefix = (ctx.get("filename_prefix") or "").strip() or filename_prefix

        # The shared prefix (from VV Run Name) carries name/date/time/code; the stage is per-saver, so
        # a set's files differ only by this suffix.
        stage = _resolve_stage(stage, stage_custom)
        if stage:
            filename_prefix = f"{filename_prefix}_{stage}"
        filename_prefix += self.prefix_append



        width, height = images[0].shape[1], images[0].shape[0]

        # Same call core SaveImage uses -> identical %date:..% tokens, subfolders and counter.
        full_output_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, self.output_dir, width, height)

        text = ""
        try:
            text = _parameters_text(prompt, width, height, positive, negative,
                                    use_civitai_links, saver_id=unique_id)
        except Exception as e:                      # never lose the image over a metadata problem
            print(f"[VV] metadata build failed: {e}")
            text = ""

        sid_val = (set_id or "").strip() or _auto_set_id(prompt)

        results = []
        for batch_number, image in enumerate(images):
            arr = np.clip(255.0 * image.cpu().numpy(), 0, 255).astype(np.uint8)
            img = Image.fromarray(arr)

            info = PngInfo()
            # Comfy's own chunks, always. `prompt` is what the viewer reads for model and prompt;
            # `workflow` (in extra_pnginfo) is what makes the PNG droppable back into ComfyUI.
            # Dropping either is how a "saved" image becomes an image with no provenance.
            if prompt is not None:
                info.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo:
                for k, v in extra_pnginfo.items():
                    info.add_text(k, json.dumps(v))
            if text:
                info.add_text("parameters", text)   # the chunk Civitai parses
            if sid_val:                             # set grouping for the VV Curator
                info.add_text("vv_set_id", sid_val)
            if stage:
                info.add_text("vv_set_stage", stage)

            name = filename.replace("%batch_num%", str(batch_number))
            file = f"{name}_{counter:05}_.png"
            img.save(os.path.join(full_output_folder, file), pnginfo=info, compress_level=4)
            results.append({"filename": file, "subfolder": subfolder, "type": self.type})
            counter += 1

        return {"ui": {"images": results}, "result": (images, text)}


NODE_CLASS_MAPPINGS = {
    "VVSaveImageCivitai": VVSaveImageCivitai,
    "VVNameAndId": VVNameAndId,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    # Display names are cosmetic — ComfyUI keys a saved workflow on the CLASS name, so renaming
    # costs no existing graph anything. The two current nodes lead with the brand so they read as a
    # pair; the two legacy ones say so in the menu, which is the only place anyone would reach for
    # them by mistake.
    "VVNameAndId": "VV Run Name",
    "VVSaveImageCivitai": "VV Save Image",
}

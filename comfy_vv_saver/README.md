# VV Civitai Saver — ComfyUI nodes

**Two nodes.**

| Node | Purpose |
|---|---|
| **VV Run Name** | Defines one run — everything a single press of Generate produces. Wire `run_data` into each saver and `filename_prefix` into any other saving node. One per workflow. |
| **VV Save Image** | Saves the picture, with every generation setting read off the graph. One per stage. |

`VV Save Context` and `VV Stage Prefix` used to exist for these jobs and are **deleted**, not kept
as legacy stubs. A workflow predating this shows a missing-node placeholder where they were — which
is the node you were going to delete anyway — and carrying dead classes forever to avoid one
placeholder is the worse trade.

**File type stopped mattering, and that is what got this down to two.** The run code identifies a
generation from its *filename*, so a video needs no node of its own to be named, paired or grouped —
it takes the same `filename_prefix` string any other writer takes — video, audio or anything else
with a `filename_prefix` input.

## Wiring

```
  [ positive switch ]──►┐
  [ negative switch ]──►│  [ VV Run Name ]
                        │   name: MM_FinJungle
                        └────────┤ run_data ├──► VV Save Image   stage: Raw
                                 │          ├──► VV Save Image   stage: Detail
                                 │          └──► VV Save Image   stage: Final
                                 └ filename_prefix ──► video / audio / anything
```

Each saver needs **two wires**: `run_data` + `images`. Nothing else has to be set.

- **`positive_prompt` / `negative_prompt`** — wire the *final* prompt strings (your combined/manual
  switch output). Spelled out because a sampler's `positive`/`negative` are *conditioning*; these
  are raw text, and the two are not interchangeable.
  Prompts are assembled by arbitrary string nodes, so re-evaluating that chain is brittle; tapping
  the final string is exact. Unwired, the saver traces the sampler's conditioning back to the
  CLIPTextEncode instead.
- **`run_data` is optional on the saver.** Without it a saver still auto-traces everything and
  writes `ComfyUI_00001_.png`, exactly as core `SaveImage` does — it just cannot be steered. The
  saver's own `filename_prefix` field was removed on 2026-09-06: its only job was that drop-in
  case, it showed a value it ignored whenever `run_data` was wired, and sharing a name with VV Run
  Name's output invited a wire that half-worked (right name, hashed set id, traced prompts).
- **`filename_prefix`** is a plain string, named to match the input every ComfyUI save node has, so
  the wire reads the same word at both ends.
- **Wiring `filename_prefix` to a non-VV node also writes a `.txt`** beside whatever that node
  saves, carrying the prompt and settings — a video or a song has nowhere inside it to keep them.
  The node face says **Saving prompt + settings to .txt** when this is live, so it is no longer a
  surprise.

### Two files from one run

Add a **second VV Run Name**, wire the first's `run_data` into its `inherit_run` input, and give it
a `name_suffix`:

```
  [ VV Run Name ]──run_data──► [ VV Run Name: name_suffix "HiRes" ]──filename_prefix──► second node
```

The second inherits the run wholesale — same code, same set id, same prompts — and changes only the
filename. So the two files are named apart and still belong to one generation. Note that
`name_suffix` moves the `filename_prefix` output ONLY: images written by VV Save Image are
unaffected by it. Chaining the same
node beats a special-case node whose purpose nobody can guess, and it is why there is no video node.

It must **inherit** rather than re-derive: computing the time again would mint a second code seconds
later, and the two files would become two generations.

## Naming

`VV Run Name` takes a **name** and emits the folder and file naming. Each saver appends its own
**stage**, so for `womanInCemetery` at 21:12:26 on 2026-07-16:

```
womanInCemetery_7_16_26/                                   <- name + M_D_YY
  womanInCemetery_21-12-26~vv3g05o5_Raw_00001_.png         <- name + HH-MM-SS + run code + stage
  womanInCemetery_21-12-26~vv3g05o5_Detail_00001_.png
  womanInCemetery_21-12-26~vv3g05o5_Final_00001_.png
  womanInCemetery_21-12-26~vv3g05o5_00001_.mp4             <- a video, same run
  womanInCemetery_21-12-26~vv3g05o5.txt                    <- the video's metadata
```

All of them share one `vv_set_id` and one run code. The date/time, id and code are computed **once
per run** and fanned out — a per-save timestamp would disagree, since savers run seconds apart. The
`_00001_` counter is ComfyUI's own, and it is the reason the video and the still disagree on
numbering, which no longer matters to anything.

### The run code — `~vv3g05o5`

The same identity as `vv_set_id`, written where **every file type can carry it**. A set id lives in
a PNG text chunk, so a video has never been able to hold one; a filename is carried by videos, text
files, and output from savers this package has never heard of.

The viewer matches on **the name up to and including the code**, and everything after it is noise —
stage, counter, VHS's `-audio`, the `.txt` extension. That is what makes the metadata file writable:
it is named up to the code and stops, so it needs no counter, so it can be written before the video
it describes exists.

Two properties are load-bearing, and both are explained in `comfy_meta.py`'s module note:

- **`~vv` is a marker, not decoration.** A bare six-character token would be matched by any ordinary
  filename containing a tilde, and the price of a false positive is two unrelated pictures silently
  merging into one card in a library nobody asked to have re-grouped. `_sanitize` also strips `~`
  from the name you type, so ours is the only one and "which tilde is the real one" is un-askable.
- **It comes from the clock, not from dice.** Six base36 characters of random collide about once per
  45,000 pairs of runs — roughly 3% across a folder of 300, i.e. once or twice a year, silently.
  Seconds-since-2020 cannot collide unless two runs finish in the same second. It also sorts
  chronologically for free, and stays six characters until 2088.

The code goes on the **file** half of the prefix, never the folder: a folder is a day, not a run.

**Old files are untouched.** A name with no code is invisible to the matching, so a library written
before this existed groups by exactly the rules it always did — and a coded run and an uncoded one
can share a folder without interfering, because neither can land in the other's bucket.

Time is **hyphenated, not `21:12:26`** — `:` is illegal in Windows filenames (as are `\ / * ? " < > |`).
The name is sanitized of all of them; an empty name becomes `untitled`.

This deliberately does not use `%date:…%`: that token is **not** supported by ComfyUI core
(`folder_paths.compute_vars` handles only `%width% %height% %year% %month% %day% %hour% %minute%
%second%`), so it only works where some other extension patches it in. Owning the string end-to-end
avoids depending on that.

## The metadata `.txt`

**The original reason for this file is gone, and it is still needed.** It used to be that a video
carried nothing and the viewer never opened one; both stopped being true in August 2026. An MP4
from ComfyUI carries the same `prompt` graph a PNG does, and the viewer reads model, steps, sampler,
seed and LoRAs straight out of it.

What it does *not* carry is the equivalent of a PNG's `parameters` chunk — and that chunk, not the
graph, is where a prompt actually comes from for the video models. On a MiniMax H3 run the tracer
returns nothing for the prompt, from the MP4 and the PNG alike; the PNG reads fine only because our
saver also writes it that chunk. So this file **is** that chunk, kept beside the file instead of
inside it.

**Not video-only.** `SaveAudio` takes a `filename_prefix` too, and nothing here looks at file type.

**It writes itself, with nothing to switch on.** VV Run Name checks whether its `filename_prefix`
output is wired to a node *we did not write*. Something foreign taking our filename is about to
write a file under our name that will carry no `parameters` of its own — so the wiring **is** the
intent. Nothing to enable, nothing to forget, and a workflow that only saves pictures never grows a
stray `.txt`.

**And the node says so.** Its face reads **Saving prompt + settings to .txt**, or
**filename_prefix not wired - no .txt file saved**, live, as you
wire. That line exists because this behaviour being invisible-but-conditional was the single most
confusing thing about the node.

A checkbox was the obvious alternative and is the same foot-gun as the two toggles this package
deleted: forget it and the video silently has no metadata.

**One file per generation, not per video.** It stops at the run code, so a `base` video, an `interp`
one and VHS's muxed `-audio` copy all find the same file.

The body is the same A1111 block the saver writes into the picture, so the two can never describe
themselves differently, with `VV set id:` appended **last** — A1111's negative prompt runs to the
end of the body, so a key between the two would be swallowed by any ordinary parser. (The viewer
lifts our keys out first and would cope either way; a human pasting the file into Civitai would not.)

## Why it reads the graph

Existing metadata savers make you hand-set `steps` / `cfg` / `modelname` on the save node. Those
widgets silently drift from what actually generated the image. This node instead **reads the
executed graph**: it follows the sampler's wired inputs back to the real control nodes (mxSlider,
Sampler Selector, Combo Clone, rgthree Seed) and the model chain back through the Power Lora Loader
to the checkpoint. Nothing to keep in sync.

**There are no override fields, and their absence is the point.** `model_name`, `sampler`,
`scheduler`, `steps`, `cfg` and `seed_override` used to sit on the saver as manual overrides. Six
boxes that should always be empty read as settings you are meant to fill in — reproducing, in the
node built to prevent it, the exact defect it exists to prevent. If a value comes out wrong the
tracer is wrong; fix the tracer.

The graph-reading logic in `vendor/` is a verbatim copy of the VV Curator's `comfy_meta.py` /
`model_hash.py`, so this node and the viewer's "Export for Civitai" emit the **same** format.
Keep them in sync by re-copying; they are intentionally standalone (ComfyUI loads custom nodes
from its own folder and can't import the viewer). `tests/test_vendor_sync.py` compares them byte
for byte, because this sentence was untrue for months without anything noticing.

## `stage`

A **dropdown**: `Raw` · `Detail` · `Refine` · `Upscale` · `Final` · `(none)` · `Custom…`
(default `Final`). It's appended to the filename and written as `vv_set_stage`. `(none)` appends
nothing — the plain `SaveImage` drop-in case, and the one for a video's still.

It is a dropdown and not a text field because **the value is a key, not a label.** Those five names
are exactly `index_db.SET_STAGE_ORDER`, which the viewer uses to order a set's panes and to choose
which member fronts the card. `test_naming.py` asserts the list against `index_db` rather than
against itself, so the two cannot drift.

`Custom…` reads the `stage_custom` field, for a one-off the vocabulary doesn't cover. Since
2026-09-07 a name the viewer doesn't know is **the stage before `Raw`**: it sorts to the front of
the set and is never the member that fronts the card. That is one position, not a free one — a
custom stage cannot sit in the middle of a pipeline or at its end, and two different custom names in
one set tie and fall back to filename order. It was chosen over an editable stage list in the viewer
because the field is used for what comes BEFORE the vocabulary (a word like "First" for the shot a
run starts from), and one rule with nothing to configure covers that.

### Why a saver per stage, rather than one node with five image inputs

Each saver is its own ComfyUI **output node**, which buys two things a merged node would lose:

1. **Progressive saves** — each stage is written the moment it's ready. A merged node couldn't save
   *anything* until *every* wired stage finished, so an error late in the run would lose the
   earlier images.
2. **Clean bypass** — bypassing a stage's group takes its saver with it. Feeding a merged node from
   a bypassed group would pass an earlier stage's image straight through, silently saving a
   duplicate under the wrong stage name.

This is also why the saver could not be folded into VV Run Name, and why two is the floor rather
than one.

## Video, in practice

VHS Video Combine writes the encoding well and is the only video node that exposes what it wrote, so
we only give it the name. With `audio` wired it writes three files — a metadata PNG, a silent
`x_00001.mp4`, and the muxed `x_00001-audio.mp4`. All three collapse to **one card** in the viewer,
and it plays the `-audio` one, so the sound is there with no cleanup.

Civitai does not parse metadata from uploaded video (there are open feature requests), so the `.txt`
is for the viewer and for pasting by hand.

## Metadata written

| Key | Contents |
|---|---|
| `parameters` | A1111 text — prompt, `<lora:…>` tags, Steps/Sampler/CFG/Seed/Size, `Model hash`, `Lora hashes` |
| `vv_set_id` | shared across every save of one run — the viewer groups on this |
| `vv_set_stage` | Raw / Detail / Refine / Upscale / Final |
| `prompt`, `workflow` | Comfy's own chunks — always written |

**Writing `parameters` is not the same as being ready to upload.** Comfy's own chunks stay in the
file on purpose — that is what lets the picture be dragged back onto the canvas — and Civitai
prefers them to the A1111 text beside them, parsing them unreliably. So uploading one of these
PNGs **as-is** usually lands the settings and loses the prompt.

For a post where the prompt matters, run the picture through the viewer's **Export for Civitai**,
which writes a copy with those chunks removed, and upload that. This node makes the metadata
*correct*; the export makes it the *only* metadata in the file. One PNG cannot do both jobs.

## The bridge — not a node

This package also carries a small **bridge** so VV Curator can open a workflow on ComfyUI's canvas
with one click, instead of you dragging the file onto the window.

**It is not a node.** Nothing appears in the node menu and nothing is wired into a graph. That is
possible because a custom-node *package* is loaded when ComfyUI **starts** — so it can add HTTP
routes and a browser script — whereas a *node* only does anything when a workflow **runs**. A
canvas can only be loaded from ComfyUI's own page, which is why this has to live here rather than
in the viewer.

| Endpoint | Does |
|---|---|
| `GET /vv_bridge/ping` | Answers `{"ok": true}` — how the viewer tells "the bridge is here" from "ComfyUI is merely running" |
| `POST /vv_bridge/open` | Body is a workflow (or `{"workflow": …, "name": …}`). Opens it on the canvas |

**It never queues a render, never writes a file, and never saves to your workflow list.** It stops
exactly where a drag-and-drop stops. A send reaches every open ComfyUI tab, deliberately — with two
open there is no way to know which one you were looking at.

If it can't register (an old ComfyUI, or a leftover standalone `comfy_vv_bridge` holding the same
paths), it prints a line and the **nodes still load** — those are the part your generations depend on.

## Install

Copy or symlink this folder into `ComfyUI/custom_nodes/` and restart ComfyUI.

**Upgrading from the standalone `comfy_vv_bridge`:** delete that folder from `custom_nodes/` first.
Both register the same routes, and while a duplicate is now survivable rather than fatal, only one
of them can be the live copy.

```
mklink /D "C:\path\to\ComfyUI\custom_nodes\comfy_vv_saver" "C:\path\to\VV_Curator\comfy_vv_saver"
```

## Notes

- **Model fingerprints**: `use_civitai_links` resolves the checkpoint/LoRAs via ComfyUI's
  `folder_paths` and hashes them (Civitai AutoV2 = first 10 hex of SHA-256) so Civitai auto-links
  resources. The first hash of a multi-GB checkpoint is slow; results are cached in
  `model_hashes.db` by (path, size, mtime), so it happens once per file. Skipped entirely for the
  video's `.txt`, which is never uploaded.
- A metadata failure never loses the image; it logs and saves without the `parameters` chunk.
- `scheduler` is intentionally not emitted: A1111 only encodes it as a sampler-name suffix for
  `karras`/`exponential`, and Civitai doesn't parse a scheduler field.

## Tests

```
python comfy_vv_saver/test_trace.py     # tracer
python comfy_vv_saver/test_naming.py    # naming, run code, chaining, the video's .txt
```

`test_trace` checks the tracer against a graph mirroring the real ILU workflow (wired control nodes
+ Power Lora Loader), asserting it reads the control nodes rather than the sampler's stale widgets.
`test_naming` checks the folder/file/set-id spec (including that no Windows-illegal character can
reach a filename), the run code, chaining, when the `.txt` is written, and drives the node and the
viewer end to end through a stubbed `folder_paths`.

## Not done yet

- **A run holding several stills AND a video shows only the video.** A multi-image video model
  (MiniMax fed several generated frames) puts all of them on one card, and the viewer treats any
  group with a video as a still+video pair — so the stills are in the group, recycled with it and
  selected with it, but never shown. That is a viewer change rather than a node one, and it is on
  the viewer's backlog.
- **The note lives in tooltips.** ComfyUI has no always-visible note on a node body without a JS
  extension, and this package is pure Python — so the explanation of "nothing to set here" is on the
  node's description and each field's tooltip, both on hover.

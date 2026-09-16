# Decisions

**Why the app is the way it is** — the calls that would otherwise be re-argued, and the rules that
exist because breaking them shipped a bug.

Not a history. The account of how each thing was found is in `git log`, in full and searchable;
this is only what a future session needs *before* changing something. Where this and
[Help](help.md) disagree, **Help is right and this is stale**.

> **When to add an entry.** When a decision is REVERSED, or argued a second time. Not when one is
> made — the commit message is the record for that. This file replaced 14 notes totalling 4,200
> lines in 2026-08-29, most of which was retelling.

**What belongs in Help, and what does not.** Help carries only what the interface cannot tell you:
keys, defaults, limits, and the things that surprise people. If an entry reads like a description
of a button, the button needs fixing rather than documenting. This rule was written at the top of
`help.md` itself until 2026-09-14, where the reader met it first and had no use for it — it is a
rule for whoever edits that file, not for anyone reading it.

**Name a control by its words, never by a lookalike character.** `⧉` for the libraries button and
`↗` for Open in ComfyUI were characters chosen to resemble icons that are actually drawn SVGs, so
they matched nothing on screen and rendered as an empty box wherever the reader's font lacked the
glyph — which is how they were found, in the author's editor. `⋯` and `▶` are fine, because those are
literally the characters the interface uses.

---

## Objects: files, groups and cards

**Written down 2026-08-20 because losing it cost a bug.** Cited from `server.py` and
`tests/test_type_filter.py`; change it only with those.

| | |
|---|---|
| A **file** | one thing on disk. What the index has a row for |
| A **group** | the files from one generation — a still and its video, a set's three stages, a song and its cover. Found by run code, set id or filename stem |
| A **card** | what the grid draws. A **group** while that kind is merging; a **file** while it isn't |

**A card is not a thing in the database.** It is a group decided at query time, under the collapse
toggles — which is why the same library draws a different number of cards depending on a switch, and
why every rule below names whether it means files or cards.

1. **A card's identity comes from the whole group, never from the member being shown.** A pair
   holding a video *is* a video card, though its face is the still and the row that matched was a
   `.png`. The face is a representative, not the subject.
2. **A mark applies to the whole card** — star, label, tag, note, recycle. One exception, from
   the detail view.
3. **A filter for what a card CONTAINS matches if ANY member matches** — prompt, model, quality,
   date. You searched for a prompt and the run had it, so the run comes back whole.
4. **A filter for what a card IS must test the card, not its members.** File type is the only filter
   of this kind, and the only place rules 3 and 4 pull opposite ways. While a pair kind is
   collapsing, the options carve the library into non-overlapping sets by what the group *holds*:
   Videos / Songs / Images (neither). With merging off a card is a file again, and the still from a
   video run is an image — **that is what the Sets switch is for**.
5. **Every card appears under exactly one File type, and every card appears under one.** The
   partition is the invariant, and both halves matter: the bug made cards appear under two, and a
   fix aimed at the symptom could easily make some appear under none. `test_type_filter.py` asserts
   the partition rather than checking the options one at a time, because a per-option spot check is
   what missed it.

---

## The index, and what survives a rescan

- **Per-image fields survive by OMISSION, and the omission IS the mechanism.** The rescan `UPDATE`
  names its columns; `note` and anything hand-authored are simply absent from that list. Never
  widen it into a blanket update.
- **Curation in the `tags` table survives by a different route and needs no list at all** — the scan
  never touches that table. Labels ride it as `label:<slug>`, favourites as `source='fav'`.
- **The `tags.source` schema comment is WRONG** and has been since the first commit. It says
  `'wd14' | 'vlm' | 'manual'`; the live values are `user`, `fav`, and `ext:<id>` for tags an
  extension found (`hide` was one too, until the Hidden mark went on 2026-09-08). Do not plan on
  the strength of that comment — it cost a false "no migration needed" once already.
- **A file the scan cannot read must not look like a file that is gone.** The per-file guard sits
  deliberately *below* `seen.add(path)`, because `seen` is what the prune pass tests. One line
  higher and "I could not read this" becomes "this is gone", taking curation that cannot be rebuilt.
  A scan is silent on success and speaks when it skipped something — a refresh that quietly drops
  files is worse than one that stops.
- **A rescan walks only the folders whose timestamp moved.** The risk is never that it misses a
  file; it is that it removes one. A six-hour clock forces a full walk regardless, as the ceiling on
  how long a missed change can last.
- **"New" means modified time, everywhere**, and the share tolerance is 2 seconds — a test that
  changes a folder and looks immediately proves nothing.
- **Grouping runs in passes and the order matters** — explicit run codes first, then filename
  fallbacks, so an exact answer is never overwritten by a guess.
- **There is ONE scan verb, and the app tells you when the thorough one is needed** (2026-09-12,
  reversing a shipped design). The menu used to offer *Rescan* and *Rebuild metadata*, which were the
  same operation with one shortcut switched off: the difference described an internal optimisation,
  not anything in the user's library. The author, who commissioned both: *"I've lost the plot. What does
  rescan do vs. rebuild metadata? And will anyone else get the difference?"* If the person who asked
  for both cannot tell them apart, nobody can. *Rebuild all libraries…* went at the same time — it
  sat in a per-library menu and ignored the library it was opened from — and returned as
  *Rescan all libraries…* in the menu that is about all of them.
- **"Rescan" means MAKE THIS LIBRARY RIGHT** — the cheap walk when that is all it needs, the full
  re-read when the library is behind the current reader. **The one-verb change above deleted the
  second menu item but not the second behaviour** (completed 2026-09-14): the surviving verb stayed
  the cheap check, so a library behind the reader could not be caught up from its own menu at all —
  while the dialog on the first launch after an update was telling people to do exactly that. The
  author remembered the intent correctly and the implementation had never matched it. Proved rather
  than argued: an ordinary rescan of two stale rows reports `skipped: 2` and leaves them stale; a
  forced one reports `updated: 2`. **The user still chooses nothing** — the app decides how much
  work "right" is, and quotes the price before the expensive kind.
- **The ✓ on the strip stays cheap, and that is why the catch-up lives on the menu route only.**
  `refreshChanged()` (the ✓, and `R`) calls the plain per-library rescan for every library that
  changed on disk. Teaching *that* path to force would make a keystroke start a multi-minute job,
  which the rule two entries down forbids in as many words. Two callers, two meanings, deliberately.
- **A library behind the reader wears its own mark** in the Libraries list — a rotate, amber, beside
  the tick that means "new files on disk". Same colour because both mean "this library wants
  attention"; different silhouette because they are different questions and the answer to one is a
  cheap walk and to the other a full re-read. The launch dialog says THAT some libraries are behind;
  the marks say WHICH, which is what someone who dismissed the dialog is left needing.
- **Every row records the reader version that read it** (`READER_VERSION` in `index_db.py`), which
  is what replaced asking the user to diagnose. A row behind the current reader is re-read when that
  file is OPENED — one file, no button — and a library behind it is offered a rescan on the first
  launch after an update. **Bump `READER_VERSION` whenever the reader learns something**; not for a
  change to how anything is displayed, sorted or filtered, since nothing is re-read by those.
- **The app never starts a long job you did not ask for.** The author's rule, 2026-09-12: *"I don't want
  the app to 'decide' to do some time-consuming update in the background."* It may do the cheap
  walk and NOTICE, then say so. The cost of that rule, accepted openly: ignore the offer and those
  files keep showing blanks. A slow job you did not start is worse than a row that is still empty.
- **An estimate is only ever a measured one.** A forced rescan records how fast this machine reads
  that library — per library, because a share and an SSD are not comparable — and only a FORCED run
  counts, since an ordinary scan's rate is the speed of deciding not to work. With no measurement
  the dialog gives the file count and no time. It is the number someone decides on, so a guessed
  figure is worse than none.
- **Adding a General setting touches four hand-written places.** Miss one and it is written once and
  silently dropped by the next save of anything else. `save_config()` builds an explicit dict for
  the same reason — a block missing from it does not persist.

---

## Metadata

- **There are several sources, and the reason is that each one was believed to carry nothing.** The
  same early-out shipped **three times** — against JPEGs, then videos, then long videos ("only the
  short ones carry anything"). **The pattern is the finding**: an assumption about what a file
  format cannot hold gets written down as fact and then defended. Test the belief against a real
  file before building on it.
- **A name list cannot recognise a node.** Custom nodes are renamed freely, so the VAE and the
  sampler are decided by what a node *does* in the graph — its inputs and its position — not by
  matching a class name. A name-based rule fails silently on someone else's workflow.
  **Proven a third time on LoRAs (2026-09-05)**, and this one goes further than the node's name: a
  MiniMax H3 run reported *no* LoRAs because the loader carries no `lora_name` at all, keeping its
  whole list JSON-encoded in one `stack_data` string. So the shape of the DATA is matched too — a
  list of entries with an `on` flag and a `lora` path — not the input's key. Three shapes are known
  now (one per node, rgthree's dict-per-slot, and a stacker's encoded list); assume there is a
  fourth. **The tell is reporting nothing rather than something wrong**, which reads as a feature
  being off rather than a parser missing a case.
- **A video's `.txt` sidecar is its `parameters` chunk, and that is the whole of why it survives.**
  Argued a second time on 2026-09-05 and worth settling: the original reason — "a video carries no
  metadata of its own" — died in August 2026 when videos began being read at scan. An MP4 carries
  the same `prompt` graph a PNG does, byte for byte, and yields model, steps, sampler, seed and
  LoRAs from it. What it has no slot for is the A1111 `parameters` block, and for the video models
  **that** is where a prompt comes from: the tracer returns nothing for `positive` on a MiniMax H3
  graph, read from either file. The PNG only reads because the saver writes it that chunk too.
  **It cannot be replaced by borrowing the paired still** — a txt2vid run has no start image and so
  produces no PNG to borrow from, and requiring a throwaway-first-frame workflow to give metadata
  somewhere to live is not a thing to ask of anyone but its author.
- **"Keep the vendored copy in sync by re-copying" is not a mechanism.** `comfy_vv_saver/vendor/`
  duplicates the viewer's `comfy_meta.py` so the node and *Export for Civitai* emit the same format
  — the packages cannot import each other, so the duplication itself stands. What failed is trusting
  a convention to maintain it: the copy sat on four hardcoded LoRA class names long after the viewer
  generalised the test, so the saver silently found *fewer* LoRAs than the viewer did, in the
  sidecar and in the Civitai block alike. **A guarantee nothing checks is a guarantee that quietly
  stops being true.** The two copies' output is now compared directly by `tests/test_lora_stack.py`;
  drift that changes nothing still passes, drift that changes an answer fails.
- **The base sampler is the one with no other sampler upstream.** Two plausible alternatives both
  pass a single-sampler test and both are wrong: "first in the file" (node order need not follow the
  pipeline) and "longest positive prompt" (stages usually share one).
- **A seed can be bigger than the database can hold.** SQLite's `INTEGER` stops at 2^63−1; ComfyUI
  randomises across the full unsigned range. It is kept exactly in a TEXT column rather than
  clamped — a nearly-right seed looks usable and cannot reproduce the generation. Putting the digits
  in the INTEGER column does not work either: affinity turns them into a REAL, wrong by one,
  silently. The same reasoning already sends the seed to the browser as a string.
- **The run code is a generation's identity, carried in the filename** — which is what lets a run be
  grouped exactly rather than guessed at from stems.
- **Model family is derived at scan and learns the user's own folders**, rather than being a fixed
  list of names. A version glued to the end folds into its stem **only where that stem is already a
  family in that library** — `krea2` joins `Krea` because `Krea` is a folder there, and `SD15` stays
  whole where no `SD` folder exists. The rule can under-merge; it cannot invent a merge, which is the
  same principle the vocabulary itself runs on applied one level further in.
- **`SD` is held out of that fold by name** (2026-09-10, and argued twice in one sitting — the first
  version let `SD15` join an `SD` folder, and that was inverted an hour later). `SD` is a prefix
  shared by architectures that are not versions of each other: SD1.5, SDXL and SD3.5 cannot read each
  other's LoRAs. So nothing folds into a bare `SD`. `PonyXL` **does** fold into `Pony`, because there
  the XL one is what the name means in practice — which is why this is a named exception and not a
  rule about suffixes. Named rather than inferred, too: a "stems under three characters" test would
  block `SD` today by coincidence and let the next short family straight through.

---

## Curation, recycling and undo

- **The two safeguards divide on one line:** a confirm goes *before* something irreversible you
  might not have meant; an undo goes *after* something reversible you did mean. Never both.
- **Undo is "cancel the job", not "put it back".** The recycle is queued for 5 seconds and simply
  not performed — restoring from the Recycle Bin is unreliable across network shares, and this needs
  no such promise. The queue is in memory, which is the safe direction: a crash means the files
  stay.
- **The window was 15 seconds and is now 5.** Fifteen locked the grid for a quarter of a minute
  after every press, which is unusable during fast culling.
- **Presence is not order.** Restoring a batch by re-inserting each item where it was found gives
  the right *set* in the wrong *sequence*. Pinned by `test_undo_order.js`.
- **Recycling on a network share goes to a `_ToRecycle` folder**, because `send2trash` hard-deletes
  there — silently. Emptying that folder is a file-manager job, deliberately not the app's.
- **Hidden is "not this one, for now"** — a curation mark, not a filter, and absent from the
  snapshot/reset machinery on purpose.
- **"Apply to folder" is deliberately unreachable from the full-screen view, and `K` can never
  trigger it.** Raised a second time on 2026-09-15 — *"one thing we do NOT have in full-screen view
  is 'apply to all in folder' as we do in detail view. worth adding?"* — so it is written down here.
  It is enforced twice: marking a keeper unticks the box, and `K` marks a keeper before it recycles,
  so a stale tick cannot survive into that keystroke. **The reason is the count.** Ticking the box
  shows a preview of what the folder cull would take — how many sets, how many files — and that
  preview lives in the set bar, which the full-screen view hides. Offering the capability there
  would mean agreeing to a number you cannot see, which is worse than the existing flow rather than
  quicker. Three supporting reasons, none sufficient alone: a scope switch is safe because it sits
  *beside* the thing you press, and a key has no adjacent checkbox; `K` was chosen over Enter to be
  explicit, and folder scope turns one keystroke into a multi-set recycle inferred from a single
  example; and the mode is for judging one picture by eye — the Quality score was removed from it
  the same day for that reason — where a folder-wide cull is a judgement about the shape of a
  folder, which is what the panes view is for.

---

## The detail view: what is a layer, and where the size controls live

Both of these reverse earlier calls, on 2026-09-15, and both turn on the same thing: `z` can now
switch the magnifier on, where before only the icon could.

- **The magnifier is a SETTING, not a layer, so Escape no longer takes it.** It used to absorb the
  first Escape, on the reasoning that dropping the lens beats closing the view out from under
  someone who only wanted the magnifier gone. That was right while pinning meant a deliberate click
  on the icon, which made a raised lens rare. Tapping `z` makes it the ordinary state of the review
  flow — set a magnification, then walk the set with `←`/`→` — and a setting must not eat the key
  that leaves the view. It is now two presses from focus view to the grid whether the lens is up or
  not. The author's test: *"Easy 2-click back to home view."* Nothing is stranded, because `z`
  cycles round to off, the icon toggles, and closing the view clears it.
- **Maximize and shrink are two buttons, split by mode, not one toggle in the corner.** Maximize
  sits in the actions row beside the magnifier, because it acts on the card you are looking at and
  that is where that card's controls are. Shrink stays in the window corner beside the ✕. The
  earlier note called the corner button "the ENTRY point as well as the exit, so the mode is
  findable without already knowing the `f` key" — the entry is still findable, just next to the
  other control that changes how you are *looking* rather than where the file goes. **This is not a
  duplicated control**: maximizing hides the actions row entirely, so exactly one of the pair is
  ever on screen. It also has to be the corner, not the caption, because video and songs draw no
  caption.

---

## Scope

- **DESKTOP ONLY, and mobile is not a gap to close.** The author, 2026-09-08: *"I'm fine to say this app
  assumes Desktop usage - mobile IMO is just not a good fit. It requires screen real estate."* The
  app exists to cull a large library, which means seeing many images at once — a narrow screen
  cannot do the one thing it is for. So the sidebar deliberately never collapses, and a layout bug
  that only appears below ~600px wide is **not a bug**. Do not propose a responsive rail, a
  hamburger, or a mobile breakpoint for the grid. The one `@media (max-width: 760px)` block that
  remains restyles the DETAIL view, and earns its place on a narrow desktop window, not a phone.

---

## The sidebar and its controls

- **A control is drawn only while it is DOING something.** Reset is the single sanctioned exception,
  because a control that appears when you need it cannot be found before you need it.
- **The Libraries list is a set of SCOPE SWITCHES, not a facet, and three of its rules follow from
  that** (2026-09-14). Each row shows the library's SIZE, not how much of it matched — it used to
  show the facet count, so searching shrank every library. Rows stay in the order the libraries were
  added, not ranked, because a list that re-orders itself while you type is one you cannot learn the
  shape of. And unticking every box means NONE of them: one stored value used to mean both "nothing
  chosen, so show everything" and "the user unticked the last box", and the first reading won
  everywhere, so the last untick put them all back. `count` is still returned and still filtered —
  it is simply not what the rows display or what orders them.
- **A CONTROL MUST NOT CLAIM A SCOPE IT DOES NOT HAVE**, and where it sits is part of the claim.
  This was the same fault four times over on 2026-09-08: *Clear ALL tags* removed only yours,
  *Clear Quality scores* said "for the active root" when no such thing existed, a rebuild-everything
  item sat in one library's menu, and Reset sat below two of the three things it resets. The author found
  the last one and named the mechanism: *"the issue is hierarchy on the page. putting it below
  snapshots makes it seem like it doesn't apply to snapshots."* So Reset now sits below the one
  thing it does not touch (which libraries are in scope) and above everything it does. The test
  before shipping any clear/reset/rebuild control: read its label and its position as a promise, and
  check the code keeps both.
- **The master control cannot live inside an optional section.** Reset was nearly put on the
  Snapshots row; Snapshots gained a Settings switch that hides it the same week, which would have
  taken Reset with it. Anything that can be switched off is not a place to keep something that
  always has to be reachable.
- **How far a rule reaches says how much it separates.** Full-bleed (`.rail-sep`) divides the rail
  into sections; inset to the content column (`.pane-sep`) groups controls within one pane. Two
  widths, two meanings, and a new line has to pick one.
- **An offline library leaves scope entirely — REVERSING how this worked until 2026-09-14.** Its
  cards used to stay in the grid showing cached thumbnails, with a banner in the detail view
  explaining that the picture was the thumbnail and the file was out of reach. That lost to a real
  test: a thumbnail is only made on first view, so anything never opened while the drive was
  connected had nothing cached and nothing to build one from, and most of the grid came back broken.
  The banner worked — the author read it — and it still wasn't clear the pictures were the cached ones.
  His call: *"I'm inclined to just hide cards from inactive Libraries. The experience is SO broken
  otherwise. I suggest we act as if the library is unchecked."* A library half-showing is worse than
  one plainly absent. Excluded while unreachable rather than actually unticked, so it returns on its
  own; a dialog when one goes (at launch as well as mid-session), a toast when one comes back. The
  offline machinery below it is kept, because a detail view can still be reached from a snapshot.
- **On an offline library, everything is disabled except Remove.** I proposed dimming only the
  items that touch its files; The author's counter is the one that shipped: *"it doesn't make sense to me
  to take any action on an offline folder, except removing it. e.g. Model folder is LIKELY offline
  too."* Simpler to explain, and it cannot be wrong about a second path it never checked.
- **A picker's `None` is not a way to start clean.** One lived in the snapshot picker for about an
  hour on 2026-09-08. It detached the name and kept the filters — coherent, and nobody would guess
  it. The author: *"i prefer removing it and using the Reset."* A one-word picker item that needs
  explaining is the smell; Reset is the control that means start clean.
- **Snapshots is Native Instruments' sense of the word** — the saved state of every control on a
  panel, which is exactly the payload. Rejected: Lightroom's *Snapshot* (one photo's edit state,
  wrong scale), *Smart Collection* / *Saved search* (imply a virtual folder the images live in), and
  *View* (reads as a mode you are permanently in). It reads "Snapshots" until you restore one,
  because a permanent banner would insist you are always curating.
- **Position carries hierarchy only while the things being ordered already look different.** A
  parent control reads as a peer the moment it looks like its children. One full-bleed rule
  (`.rail-sep`) says it; the elevated tray that said it first cost a stacking context for two
  ordinary controls.
- **When several readouts share one field, define the order they give way in** — otherwise the one
  that matters is the one that vanishes.
- **A search term is committed by Enter or the ⏎ beside it, and by nothing else.** Comma and
  commit-on-blur are the rest of the email-recipient convention this field otherwise follows, and
  both were declined on 2026-09-08 after being argued as best practice: comma *"feels weird here"*,
  and blur because the author would *"rather not have a sort of non-action do a commit on the UI."* The
  field searches live as you type, so nothing is lost by leaving text uncommitted — which is what
  makes blur-commit a surprise rather than a rescue. Do not re-propose these as convention.
- **Include and Exclude err WIDE.** When it is a question of whether some text should be searchable,
  the answer is yes: the sidebar is full of ways to narrow — model, LoRA, folder, type, date,
  labels, quality — and none of them can rescue a word the index never held. The author, 2026-09-08, on
  adding song lyrics: *"err on side of wider search for Include and Exclude, since the user can use
  the many other Filter options to narrow search."* The one standing exception is the negative
  prompt, which is the same boilerplate on nearly every image and so matches everything or nothing —
  the test is whether the text DISTINGUISHES images, not whether it is text.

---

## Feedback: who owns the spinner

Everything that dims, spins or reports progress goes through `Busy`, `Job` and `refreshView`. The
three rules are in [DESIGN.md](../DESIGN.md#feedback-state); the reasoning:

- **The freeze was an ordering bug, not a missing report.** Work that starts before the indicator is
  raised leaves the UI frozen with nothing on screen to explain it.
- **`ETA 0:00` is a prediction of nothing dressed as a prediction**, and on an update the rate
  measures the wrong thing entirely — so neither is shown.
- **If a feature is blocked, say so.** A greyed control whose tooltip explains why beats a control
  that looks live and fails.

---

## Performance

Speed is a standing test, not a quality: the grid, the refresh and the scroll judge a feature as
much as what it does. Benchmarks regenerate from `bench_*.py`; these are the conclusions.

- **The page size is derived, not chosen.** 30, not 120: a page is inserted and laid out
  synchronously, so a big page visibly stalls the scroll. `topUpIfShort()` chains pages until the
  viewport is full, so a small page cannot leave the first screen half-empty.
- **The connection budget is six.** Anything that fans out per card competes with the thumbnails.
- **Collapsing sets costs the whole view, and that is inherent** — it groups and sorts every
  matching row before keeping a page. With collapsing off a page cost 3.5ms against 830ms on.
- **Anything that only matters for the rows you got back does not belong in the query that scans
  everything.** Applied three times (song columns, counts, the page query). After it, ~395ms of a
  large page is reading, grouping and sorting the library — a floor no leaner query reaches.
- **Keep-alive's real risk is a mis-framed response**: a handler that answers without reading its
  body leaves that body in the socket, and the next request on the connection reads it as a request
  line. `do_POST` reads the body once, before dispatch, for every branch.

---

## Dragging into ComfyUI

- **Only a picture can cross a window boundary.** The browser hands out a file, and ComfyUI reads
  the workflow out of it — so everything here is about producing the right *picture*.
- **A video is dragged as a PNG of its first frame**, carrying the workflow. Two dead ends first;
  the thumbnail was one of them, because it reached ComfyUI as a WebP.
- **The drop is named after the real file**, not the URL it came from.
- **A song has no picture, so one is drawn for it.**
- **One click instead of a drag needs a piece inside ComfyUI** — the bridge package, which adds no
  nodes. A *package* loads at ComfyUI start and can add routes; a *node* only runs inside a
  workflow. State that distinction before proposing anything of this shape.

---

## Extensions

- **The contract is a subprocess and a JSON line, never a Python API.** An extension never imports
  the app and the app never imports it, so it cannot break the viewer with an incompatible torch.
  The folder-plus-manifest *is* the API; a registry, a permissions model and versioned guarantees
  are promises to strangers and are deliberately not built.
- **MCP is a different thing** and gets conflated with this. It would let an agent drive the viewer;
  it would not let anyone add a tagger. Parked.
- **An extension contributes ACTIONS to one menu and DETAILS to the details list**, and its details
  are treated like the app's own. One icon however many are installed — a bar that grows per
  installed thing is the wrapping-action-bar problem seen coming.
- **A tag-producing extension gets its actions by KIND**, so a tagger needs no code in the app.
- **A worker may not set a curation label.** `label:<slug>` lives in the same table, so without the
  guard a tagger could mark a thousand images "To publish" by emitting a string.
- **An extension declares what "set up" means for it** (`requires`). A green status on an install
  that cannot run is worse than a red one: it sends the user to the wrong question.
- **Setup is also the repair path**, so its button is never hidden — an install reporting Ready
  without working is exactly the one that needs it. It opens its own console because a download that
  dies when you close the viewer is worse than one you can watch.
- **Verify by running the worker's own code path.** A tagger's setup once checked that its package
  imported, and passed on an install whose model class could not load. Import is not evidence; call
  what the worker calls. Verify *before* the multi-gigabyte download, and keep any compatibility
  shim in our own code rather than patching `site-packages`.
- **One scoring path, not one per surface.** The detail view had its own synchronous endpoint, which
  spawned a fresh worker and reloaded the model on every request — tens of seconds, with an ellipsis
  for feedback and the result carried in a closure that went stale the moment you navigated away. It
  sends its ids to the same background job the selection bar uses now, so every run in the app has
  one progress bar, one Stop and one way to fail, and the finish **reads the result back** instead of
  carrying it. A second path existed only to be faster and was slower.
- **Scoring a SET scores every member; the card keeps showing its face's score** (the author, 2026-09-10).
  Scoring one silently picked whichever member fronts the card, and one number against nothing is not
  a comparison — the set view exists to choose between the panes, so each pane carries its own score.
  The card's badge stays the face's because everything else on that card is the face's: thumbnail,
  filename, model. A card can therefore read 7.1 over a set holding an 8.4, and that is the honest
  answer rather than a number captioning a different picture.
- **The runner waited for its second caller.** `run_ext_batch` owns the subprocess, the JSON-line
  loop, cancel and the counters — everything belonging to the contract rather than the work. Two
  callbacks differ. Generalising against one user would have been a guess.
- **An extension is its folder, entirely — REVERSED 2026-09-13.** The quality scorer's worker, setup
  script and venv sat at the app root and its manifest reached back out with `../../`. That was a
  recorded decision, and the reason held: moving them would have made everyone who already had the
  scorer rebuild a multi-gigabyte environment for nothing. What changed is that **a release decides
  what ships by leaving folders out**, and a folder whose contents are elsewhere cannot be left out
  — the scorer could not be excluded at all while half of it was the app's own root. Nobody rebuilt
  anything: the update step moves an existing venv into place, because a venv survives being moved
  as long as nothing runs its `Scripts\` wrappers by path.
- **The first beta ships with NO extensions**, and that is not a retreat from the idea. The quality
  scorer works and is held back anyway — its setup pulls multi-gigabyte torch wheels and pyiqa's
  metrics are noncommercial (PolyForm) where the app is not; The author, 2026-09-08: *"I want to ship
  without it."* "Ask an LLM" is held back as unfinished. Settings still shows the Extensions page
  reading *Nothing installed yet* (the author's call, 2026-09-13) — the mechanism is real and the page
  says so honestly, where hiding it would make the app look like it has no such idea.
- **Every control an extension owns is hidden, not disabled, when it is absent**, declared in the
  markup as `data-ext="<id>"` rather than listed in code. This is what makes shipping without one
  safe: seven quality controls, a sort order and a filter range disappear together, and
  `applyExtensions()` also drops a hidden sort or filter that was still acting. A control that
  cannot work is not dimmed — there is nothing to explain.

---

## Startup and the app window

- **The window is opened at `http://localhost:<port>`, after the server answers.** A boot splash
  that put the window up first was built and reverted: opening at a `file://` URL gave the window a
  different identity, and Chromium then would not restore its position. `app/splash.html` is still on
  disk for `APP-6`, which would serve it from the server instead. Nothing runs it today.
- **The app remembers its own geometry** rather than trusting Chromium's, because anything that
  changes the launch URL between opening and closing loses it.
- **`--window-position` takes the window FRAME's top-left; `screenX/Y` report the VIEWPORT's.** Store
  one and restore it as the other and the window creeps down the screen by a title bar every
  session. It looks perfect for the first launch or two, which is why it needs a round-trip test.
- **`update.bat` updates itself one run late** — cmd reads a running `.bat` from disk, so it hands
  the copy to a detached process. A release that adds a folder to the copy list needs two runs.
- **The version check ends at "1.4 is out".** It never downloads and it never installs. An app that
  overwrites itself while running is a class of problem worth not having, and an update here is a
  folder copy. This reads as an obvious gap — *why not add a Download button* — which is why it is
  written down rather than rediscovered.
- **That is what settled the hosting question.** A GitHub release exposes a JSON endpoint giving the
  latest version, its notes and the zip's address, for one plain request with no account and no key.
  A zip attached to a forum post has no address to ask. The app being on GitHub follows from wanting
  this feature, not the other way round.
- **A blank `UPDATE_REPO` makes no request at all**, rather than one that fails. That is what let
  the whole feature ship before the repo existed, and the difference matters: "off" must mean no
  traffic, which is also why unticking the setting is checked on the server and not only in the UI.
- **Switching it off is forever, not "skip this version".** Considered and rejected: a per-version
  skip means the app keeps asking GitHub daily on behalf of someone who said no, which is the worst
  of both. Settings switches it back on.

---

## Discovery, songs, and things not built

- **Random is a seeded shuffle**, stable within a shuffle so paging cannot repeat or skip. A fresh
  deal only when you ask, so narrowing a shuffled grid keeps what you can already see where it is.
- **Least seen was retired; its columns were kept.** Viewing history cannot be rebuilt, and the sort
  may come back.
- **A vision model was not trusted to score quality.** It cannot verify its own judgement, and a
  confident wrong number is worse than none — the same reasoning as the seed. The data is dormant
  under `legacy_llm_*`.
- **Audio arrived already carrying its workflow** — a ComfyUI MP3 holds it the way a PNG does — which
  is why songs were cheap to add. The genre is read from prose and anchored to the tempo.
- **An unbounded search cannot say "no"**, which is why a song's headline chain stops rather than
  widening.

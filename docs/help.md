# Help

My goal is to make VV Curator intuitive, but there will always be gaps, and that's what this Help
doc is for.

## Getting started

**A library is a folder, and the libraries button at the top of the main left rail is where they
live.** It shows how many are in scope, and opens a list of every library with its file count and a
`⋯` menu — rename it, point it at your models folder. Removing a library has no effect on your
folder or files.

**The first library indexing takes a while, and only happens once.** A big folder is minutes rather
than seconds. After that the app starts in about a second, and only looks at what has changed.

**Things to try.**

- **Set the sort to Random** and press the shuffle button beside it — the quickest way to turn up
  things you'd forgotten you made.
- **Type a word you half-remember** into the Include search box. It looks through the prompt, the
  model, the filename and the folder at once, so you don't have to know which one it was.
- **Click the middle of a card** (or double-click it) to open the detail viewer, then use the
  left/right arrows to keep browsing from there.

**To get the most out of VV Curator, use the companion ComfyUI nodes in your workflows.**
`comfy_vv_saver/` came with the app: copy that folder into your `ComfyUI/custom_nodes/` and restart
ComfyUI. **VV Run Name** gives a run a root name and a unique ID, and every file carrying that ID
arrives here as one card — so a run that saved a raw and a detailed version shows up as one joined
card, and a song arrives with its cover art. **VV Save Image** writes Civitai-ready metadata as it
saves, and lets you open the workflow back in ComfyUI in one click. This applies from here on:
files you already have are not revisited.

**Without the nodes, a card is usually a single file.** The exception is a still and a video in the
same folder whose names match apart from the extension — `shot01.png` and `shot01.mp4`.

Wiring them up, what the run code in a filename means, and the `stage` setting are all in
`comfy_vv_saver/README.md`, in that folder.

## What it reads

Every file below is indexed and browsable. Reading the workflow out of one is a bonus on the formats
that carry it, not a requirement — a file with no metadata still gets a card.

| | |
|---|---|
| **Stills** | PNG, JPG, WebP, GIF |
| **Video** | MP4, MOV, WebM |
| **Audio** | MP3, WAV, FLAC, M4A, Opus |

Anything else in the folder is left alone. A video's own settings are read from inside the file the
same way an image's are; the exceptions are in [Known gaps](#known-gaps).

**A library goes all the way down.** Adding a folder indexes every subfolder below it, however deep,
so pointing it at a drive root means the whole drive. Two things are stepped over: a folder named
`_ToRecycle` (that's where deletes go on network drives), and anything reached through a shortcut or
junction rather than being really there — so a library can't loop back into itself.

**A library is named after its folder** unless you type a name when adding it. Renaming it later is
in the `⋯` menu beside it, and changes nothing on disk.

## Keeping a library up to date

**The ✓ at the top of the Libraries panel checks for new files** — and catches up whatever it finds.
`R` does the same. It looks at every library, and only actually works on the ones where something
changed on disk, so it is cheap and meant to be used often. It never re-opens a file that has not
changed.

**Each library's `⋯` menu has Rescan**, which is that same check for one library.

**The Libraries menu has Rescan all libraries…**, which is the thorough one: every file opened and
read again, in every library. You need it when the app has learned to read something it could not
read before — a new setting, a node it could not follow — because those files have not changed on
disk, so an ordinary check correctly skips them. It can take a long time, and the confirm tells you
roughly how long before you start.

**Nothing you have done to your images is ever lost to a scan.** Labels, tags, favourites and notes
survive every kind, including the thorough one.

### After you update the app

If a new version can read more than the one that indexed your libraries, it says so when you start
it, tells you what a rescan would cost, and offers to do it. **Do it later** is a real answer: it
asks again next time you start the app, and the same job is always available from
Libraries → Rescan all libraries….

Libraries that are offline at that moment are skipped and named, and you are asked about them again
when they are back. Stopping a rescan part way is safe — the next offer is about what is left, not
the whole library again.

**Files also fix themselves as you open them.** Open an image the app indexed before it knew how to
read some part of it, and it re-reads that one file there and then. You will not notice it happen.

**That is not a substitute for the rescan**, and this is the one thing worth understanding here:
opening a file fixes what you are *looking at*. Searching, filtering and sorting read the whole
library at once, so an image whose prompt was never indexed still will not be *found* by that
prompt, and still will not appear under its model, until the library is rescanned.

## Keyboard

**In the grid**

| Key | Does |
|---|---|
| `a`–`e` | Apply a curation label to the selection |
| `R` | Refresh changed libraries |
| `X` | Reset all — every filter, the sort, and the loaded snapshot |
| `Delete` / `Backspace` | Recycle the selection |
| `Esc` | Clear the selection |
| `Ctrl+A` | Select everything matching the current filters |

**With an image open**

| Key | Does |
|---|---|
| `←` `→` | Previous / next — or, in focus view on a set, that set's own members |
| `Shift`+`←` `→` | Previous / next card, whatever else the arrows are doing |
| `F` | Focus view: the picture takes the whole window. `F` again or `Esc` to come back, which also switches the magnifier off |
| `K` | In focus view on a set: keep the one on screen, recycle the rest |
| `z` | Magnify. Tap again to step the zoom, once more to switch it off. The step shows above the lens |
| `Shift`+`z` | Magnify only while held |
| `Space` | Play or pause a video |
| `1`–`9` | Mark the keeper in an image set. In focus view, show that member instead |
| `a`–`e` | Apply a curation label |
| `Delete` / `Backspace` | Recycle. In a set: recycle the others, or the whole set if no keeper is marked |
| `Esc` | Close. From focus view it goes back to the normal view first |

**Anywhere**

| Key | Does |
|---|---|
| `Ctrl+Z` | Cancel a recycle still inside its 5-second window |
| `Ctrl` `+` / `-` / `0` | Bigger, smaller, back to normal |

`Ctrl+Z` and `Ctrl+A` are the only `Ctrl` combinations the app takes; everything else falls through
to the browser, which is what keeps zoom and `Ctrl`+wheel working. Typing in a text field keeps the
keyboard; a checkbox or a slider does not swallow shortcuts.

## Settings, and what each one starts as

**General**

| Setting | Default |
|---|---|
| Autoplay videos | Off |
| Show Snapshots | On |
| After marking a keeper | Next card |
| Models folder (for Civitai export) | empty |
| Tell me when a new version is available | On, after the first-run pop-up asks |
| Record a debug trace | Off, and per browser rather than saved |

**Recycling**

| Setting | Default |
|---|---|
| Confirm before recycling | On |
| Recycle-folder reminder | On |
| File limit | 500 (range 50–100,000) |
| Size limit | 5 GB (range 0.1–500) |

The two limits fire on **whichever comes first**. A number outside its range corrects itself when
you leave the field.

**Tell me when a new version is available** is the app's only outbound request, which is why the
pop-up on your first library asks before making it. At startup it asks this app's GitHub page
whether a newer release exists; if one does, an amber ↓ appears beside the logo, and clicking it
shows the version, what changed, and a link. It sends nothing about you or your library, and it
never downloads or installs anything — updating is still you replacing the folder. Unticking it
stops the request itself, not just the icon.

**Cards** sets which details appear on a card and in what order — Dimensions · Duration · Age · File
size · Model · Folder, each Always / On hover / Off. **Appearance** is live colour pickers, saved
per install. **Miner** tunes what **Scan for tags** counts as a candidate — it is in a
library's `⋯` menu, and reads your positive prompts, folder names and file names to suggest
tags from what it finds.

**Extensions** lists what is in the `extensions/` folder. A row reads **Ready**, **Off**, **Not set
up**, **Setting up…** or **Not loading** (with the reason). The on/off switch saves immediately.
**Set up…** opens the extension's own console window and downloads what it needs; the button stays
as **Re-run setup…** afterwards, because re-running is also how you repair an install.

## Things that surprise people

**Cancelling a first index throws the library away.** While a newly added library is indexing, the
button reads *Cancel…* and confirming removes it — there is no half-indexed library to come back to,
and you add the folder again when you want it. Every other job's button says *Stop* and keeps
whatever it finished, including a rescan of a library you already have.

**An offline library drops out of view until it's back.** If a drive is unplugged or a share is
down, that library's cards leave the grid and its files leave the counts — the app says so once, and
your tick is left alone. Reconnect and they return on their own. Its `⋯` menu keeps only Remove in
the meantime.

**A card is not always a file.** Files from one generation — a still and its video, a set's three
stages, a song and its cover — collapse into one card, and a mark applies to the whole card. So
recycling a card takes the run. Turn the Sets switches off and every file becomes its own card
again, which is also how you get at the still from a video run.

**A stage name the viewer doesn't know counts as the first step.** A set is ordered by its stages —
Raw, Detail, Refine, Upscale, Final — and the card shows the most finished one. Type anything else
into the saver's `Custom…` field and it sorts to the front, ahead of Raw, so a word like "First"
lands where you meant it — and never on the card.

**Focus view shows a set one image at a time, not side by side.** `F` clears the filmstrip, the
details panel and the notes strip so the picture takes the whole window — and in a set, `←` `→` then
flip between that set's own members rather than moving through your results. Two images in the same
place, swapped, is a better test of which is sharper than two side by side; it also means the
magnifier stays on the same spot when you flip. `Shift`+`←` `→` is how you leave for the next card,
and the line under the picture says so while you are in there.

**After `K`, the arrows go quiet — if your Keep behaviour is "Stay on card".** You asked to stay on
what you kept, so a stray arrow does not carry you off it. `Shift`+`←` `→` moves on and releases it,
as does leaving focus view. With "Next card" you have already been moved on, and nothing is held.

**Recycling a card takes the whole run, including a file queued for recycle already.** A video's
`.txt` goes too, but only once every file of that run has gone: one text file can describe a whole
generation, so it stays while any of them remain.

**File type asks what a card *is*, not what it contains.** While runs are collapsing, a run holding
a video is a Video and appears under nothing else, even though a still is what you can see.

**Recycling hands files to Windows; it doesn't delete them itself.** On a local drive it does exactly
what deleting in File Explorer does — the file goes to the Windows Recycle Bin and stays there until
you empty it. Two things follow from that, and neither is the viewer's doing: emptying the bin is
permanent, and Windows silently discards the oldest things in the bin once a drive's bin quota is
full. So "recycled" means "as safe as anything else in your Recycle Bin", not "kept forever".

**Recycling on a network drive doesn't use the Recycle Bin,** because Windows has none there. Those
files go to a `_ToRecycle` folder beside the library, and nothing ever empties it — that is a
file-manager job. You can open it from the library's `⋯` menu, and a reminder appears once it passes
either limit above. The upside is that nothing expires the way the bin does; the cost is that it
grows until you clear it.

**Buttons say "Recycle" rather than naming where files land,** because that depends on the library:
the bin on a local drive, a `_ToRecycle` folder on a network one. A selection can span both at once.

**Recycling thousands of files at a time is done in batches** — the duplicate cull works through the
largest sets a few hundred at a time rather than all at once. A single huge delete can exceed the
drive's bin quota, at which point Windows starts purging the oldest bin contents and "recoverable"
stops being true.

**A refresh only looks in folders whose timestamp moved,** plus any it has never seen. If files
arrive without changing a folder's timestamp, use the library's own `↻`. A file it cannot read is
skipped and reported, never treated as deleted.

**Rescanning never touches your curation.** Labels, tags, favourites and notes survive every
rescan, including *Rescan all libraries…*.

**Export for Civitai makes a copy.** The original is untouched; the copy carries metadata in the
format Civitai parses, with your checkpoint and LoRAs linked if a models folder is set.
Uploading the raw file instead is why settings sometimes import without a prompt.

**Open in ComfyUI needs a piece that lives inside ComfyUI.** That button, under an image, opens its
workflow straight onto ComfyUI's canvas — the one-click version of dragging the file there. It
appears only for a file that carries a workflow, and it is dimmed until ComfyUI answers on this
machine (port 8188). Two things dim it: ComfyUI is not running, or the **VV Bridge** is missing.

The bridge is **not a separate download** — it comes with the **VV nodes**, the companion ComfyUI
package, and it adds nothing to your node menu. It exists only so the viewer can ask ComfyUI to open
a workflow, which nothing outside ComfyUI's own page is able to do; that is also why dragging is the
only other way. It never queues a render and never writes a file.

If you started ComfyUI after opening the image, just press the dimmed button — it checks again
rather than making you reopen anything.

**The app needs one port to itself.** It serves itself on port 8770. If another program is already
using that port, VV Curator will not start and says so in the console window. To move it, make a
file called `port.txt` beside `start.bat` containing just the number — `8771`, say — and start the
app again. If that file is missing or isn't a number, you get 8770 back.

**Include searches a song's lyrics, so songs answer ordinary words.** Search covers the prompt, the
model, the filename, the folder, and — for a song — its lyrics and its style description. A word
like *time* or *friends* will therefore turn up songs alongside images. Negative prompts are the one
thing deliberately left out: they are mostly the same boilerplate on every image, so indexing them
would match everything.

**Closing the window closes the app.** Nothing is left running behind it, though it takes about
fifteen seconds to go — a refresh looks the same from the outside for a moment, so it waits to see
whether you come back. A scan keeps it alive until it has finished.

## Where your things live

| Path | Holds |
|---|---|
| `config.json` | Your libraries, settings, snapshots, theme, window position |
| `port.txt` | Only if you made one — the port to serve on, and nothing else |
| `data/library.db` | The index — every file, its metadata, and all your curation |
| `data/thumbs/` | Thumbnail cache. Safe to delete; regenerates |
| `data/viewer.log` | Errors only. Previous run kept as `viewer.prev.log` |
| `extensions/<name>/` | One extension, including anything it downloaded |
| `_ToRecycle/` | Beside a network library: what was recycled from it |

The app serves itself on **port 8770**. Nothing leaves your machine.

## Known gaps

- **Clicking a tag suggestion identical to what you already typed does nothing** — the browser fires
  no event at all when the value doesn't change. Press `Enter` instead, which always works.
- **A custom stage can only be the first one.** It cannot sit in the middle of a pipeline or at the
  end, and two different custom names in one set fall back to filename order.
- **A run holding several stills and a video shows only the video on its card.** Open it and the
  rest of the run is there to step through.
- **Facet counts tally files rather than cards** when a sets-only filter is on, so a count can be
  larger than the number of cards you get.
- **A video's prompt often can't be read** — many video workflows keep their text in nodes the
  tracer can't follow. Model, seed and settings come through; a `.txt` sidecar covers the prompt.
- **WebM carries no readable metadata here** (MP4 and MOV only), and of the audio formats only MP3
  is read.

## Glossary

Ordinary words, used here for particular things.

| Term | |
|---|---|
| **Library** | A folder you pointed the app at, **and everything in every folder beneath it** — not just the files sitting directly in it |
| **Card** | One tile in the grid. Usually one file, sometimes a whole run |
| **Run** | What one generation produced — the stills, the video, a song and its cover |
| **Set** | A run's stages collapsed into one card: Raw, Detail, Refine, Upscale, Final. It only groups files the companion ComfyUI nodes stamped, or ones with `MAIN` / `DET` / `REFINE` in the filename — so you may have the switches and never see a set |
| **Pair** | A still and a video sharing a filename, shown as one card with a ▶ |
| **Keeper** | The one member of a set or pair you mark to keep. Recycling then takes the others |
| **Label** | One of five fixed marks for what you intend to do with a file — To publish, Published, To refine, To explore, For video — on keys `a`–`e`. A file carries one at a time; picking another replaces it |
| **Tag** | A word you type and attach to files yourself, for anything the labels don't cover. **None exist until you make the first one** — open a file and use *Add tags*, or tag a whole selection at once. They then become filters in the rail |
| **Snapshot** | A filter state — search, models, folders, sort — saved under a name so you can come back to it |
| **Extension** | An optional add-on living in `extensions/`. None come with the app |

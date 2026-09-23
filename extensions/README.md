# Writing an extension

An extension is a **folder holding an `extension.json`**, not a Python module. It never imports the
app and the app never imports it: the app spawns your worker with *your* interpreter and talks to it
in JSON lines over stdout. That is the whole contract, and it is the point — an extension cannot
break the viewer by pulling an incompatible torch.

Drop the folder in `extensions/`. **The folder name is the id.** Remove it by deleting the folder;
there is no uninstall step and nothing else to unregister.

## The manifest

```json
{
  "name": "Recognise things",
  "version": "1.0",
  "description": "One sentence. It is shown in Settings → Extensions.",
  "produces": "tags",
  "worker": "worker.py",
  "setup": "setup.bat",
  "venv": "venv",
  "requires": ["venv/model-cache/weights.pth"],
  "settings": [
    { "key": "endpoint", "label": "Address", "type": "text", "default": "http://localhost:8080" }
  ]
}
```

| Field | |
|---|---|
| `name` | Display name. Falls back to the folder id |
| `version` | Free string, shown beside the name |
| `description` | One sentence, shown in the Settings row |
| `produces` | **`"score"`** (a number per image), **`"tags"`** (rows in the tags table), or **`"text"`** (an answer shown in the detail panel and not stored). Anything else is ignored, and the extension contributes nothing |
| `worker` | **Required.** The script the app spawns |
| `setup` | Optional. A script the **Set up…** button runs in its own console window |
| `venv` | Optional. Omit it and your worker runs under the app's own Python — right for a pure-stdlib worker, which then needs no setup at all |
| `requires` | Files that must exist before the extension counts as installed |
| `settings` | Optional. Fields the user can fill in. Declaring any gives you a **tab of your own** in Settings |
| `test` | Optional. `true` if your worker can check its own setup on demand — see below |

Paths resolve **relative to the folder holding the manifest**. `installed`, `ready` and `error` are
derived by the app and must not be declared.

**Declare `requires` if your setup downloads anything.** Without it, "installed" means only that a
venv folder was created — so an install that built its environment and then failed the download
still reports **Ready**, and the user gets a green status and a traceback from the same install.

A manifest that fails to parse still appears in Settings, carrying its error. An extension that
silently never shows up would be worse. **A malformed `settings` block is the same kind of error**,
not a dropped field: a setting nobody can reach means a worker reading a blank where it expected an
address, which looks like your extension being broken rather than your manifest being wrong.

## Settings, and your own tab

Declare `settings` and you get a tab under **Extensions** in the app's Settings window, built from
what you declared. Each field takes `key` (required), `label`, `type`, `placeholder`, `help` and
`default`; `type` is `text`, `password`, `number`, `checkbox` or `textarea`.

**You declare fields; the app draws them.** That is deliberate and it is not negotiable — a panel
built from the app's own components cannot drift from the rest of Settings, and you cannot get the
house style wrong because you never touch it. There is no way to supply your own markup.

**A `password` field's value never reaches the browser.** The app tells the client only that one is
set. Submitting the field empty means *leave it alone*, so saving the rest of the panel cannot wipe
a key the user cannot see. It reaches your worker in full.

Values are stored per extension id in the app's `config.json`. **The folder name is the id**, so
renaming your folder gives you a new extension with nothing configured.

## Help, and what you send

**Put a `HELP.md` in your folder and it appears in the app's Help window**, at the end, under your
extension's name. The heading is the app's: start your own at `###`. It shows while your extension
is installed, switched off included. Write it as a manual: Help shows no live state.

**Say what you send, and when.** Every extension arrives switched off, and the app promises that
with none switched on nothing leaves the machine. If yours sends anything anywhere, the first thing
in `HELP.md`, and a sentence in your `description`, should say what goes, where, and what triggers
it.

## Testing yourself

Declare `"test": true` and your panel gets a **Test connection** button. The app spawns you exactly
as it does for a real run, plus a `--test` flag and an empty items list:

```
<your python> <your worker> <items.json> <settings.json> --test
```

Print one line and exit:

```
{"ok": true,  "detail": "Answered in 0.4s. Pictures work."}
{"ok": true,  "warn": true, "detail": "Answered, but it ignored the test image."}
{"ok": false, "error": "Nothing answered at http://… — is it running?"}
```

**The app never learns what "working" means for you.** It repeats your line and colours it: green
for `ok`, red for a failure, amber for `ok` with `warn`.

Two things worth doing, from building the first one:

- **Test what actually breaks, not what is easy to test.** "Can I reach it" is rarely the fault. An
  extension that asks a language model about a picture should send a small blue square and ask what
  colour it is: a text-only model answers everything else perfectly and silently ignores the
  picture, so every cheaper check passes.
- **Use `warn`.** A check usually has three honest outcomes — worked, worked but not the way it is
  set up, didn't work — and squeezing that into two makes the tick contradict the sentence beside
  it.

**The values you are sent are the ones on screen, not the ones saved.** Testing an address is how
someone finds out whether it is the right one; nothing is written to disk either way.

## The wire protocol

The app runs `<your python> <your worker> <items.json> <settings.json>`. `items.json` is a JSON
array of `{"id", "path"}`; a `text` extension also gets `picture` (a thumbnail on disk — already
generated, and the only readable frame a video has) and `facts` (what the library knows about the
file: prompt, model, LoRAs, sampler settings). `settings.json` is present only if you declared any.

**Extra arguments and extra item keys are both additive.** Read `argv[1]` and ignore the rest and
you behave exactly as you did before either existed — which is why the settings went into a second
argument rather than turning `items.json` into an object.

Print **one JSON object per line to stdout** — nothing else:

```
{"ready": true, "device": "cuda"}                     first line, before any work
{"id": 123, "tags": [{"tag": "beach", "score": 0.9}]}  produces: tags
{"id": 123, "reward": 0.72}                            produces: score, higher = better
{"id": 123, "text": "A cat on a roof."}                produces: text
{"id": 124, "error": "…"}                              this one file failed
```

The `ready` line is what turns the progress bar on; send it once your model is loaded, not before.
An `error` line fails that file and the batch continues. Exit non-zero, or die before `ready`, and
the app reports the run as having exited early.

Progress, **Stop**, the counters and the one-job-at-a-time rule all come from the app. You do not
implement them — just read the array, print lines, and exit.

There is no worked example in this folder to copy from: the two extensions written so far are not
part of a release, one because it is unfinished and one because its dependencies are heavy and
noncommercial. The contract above is the whole of it, and it is small on purpose.

## What the app does with your output

**Tags** go into the `tags` table with `source = 'ext:<your folder name>'`, which is what keeps them
distinguishable from tags the user typed. They appear under their own **Found tags** heading in the
detail panel, and **Settings → Clear machine tags** removes every extension's tags at once without
touching anything the user wrote.

Three rules the app enforces, so you need not:

- **A re-run replaces your rows for that image**, scoped to your own source — a second pass with a
  better threshold leaves no rejects behind, and cannot touch another extension's tags or the
  user's.
- **A tag beginning `label:` is dropped.** Curation labels live in the same table, and a worker
  emitting one string could otherwise mark a thousand images "To publish".
- Tag names are lowercased, stripped of commas and truncated to 64 characters; a `score` that isn't
  a number becomes null rather than an error.

**Scores** go into the `quality` table, one `reward` per image.

**Text goes nowhere.** The answer is shown under the open file and is gone the moment the panel
moves to another one — there is no table, nothing to clear and nothing to search. That is the
deliberate shape, not a gap waiting to be filled: a score and a tag are things the library can
filter on, so they earn a column, and prose that is kept forever has to be cleared, versioned and
searched before it is worth keeping at all. A text run is therefore one file at a time, and gets no
entry in the selection menu.

You get UI for free, by kind: any `produces: "tags"` extension gets a **Tag with `<name>`** entry in
the selection menu with no code in the app, a `produces: "text"` one gets an entry in the open
file's Extensions menu, and controls that belong to a kind of extension appear and disappear with
it.

## Setup

The **Set up…** button opens your setup script in its own console window, deliberately outside the
app — a multi-gigabyte download that dies when the viewer closes is worse than one you can watch.
The button never hides afterwards; it becomes **Re-run setup…**, because an install reporting Ready
without working is exactly the one that needs it.

**Verify by running the worker's own code path, before the big download.** Importing your package is
not evidence it works — call what the worker calls. And if a dependency needs a compatibility shim,
keep it in your own code rather than patching `site-packages`.

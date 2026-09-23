# About VV Curator

**A curation tool for people who generate with ComfyUI and end up drowning in their own output.**

Sifting through thousands of images is hard. If yours are spread across folders you named in a
hurry, and going back through them has never quite worked, that's the problem this solves.

It reads the workflow ComfyUI embeds in a PNG, and the copy it puts in a WebP or JPEG's EXIF, so the
whole library becomes searchable by prompt, model and LoRA. Then it helps you finish the job:
collapse a generation into one card, mark the keeper, recycle the rest, and export the good ones
with Civitai metadata.

**The base app, with no extensions switched on, is local only. Nothing leaves your machine.** There
is no account and no telemetry. Its one outbound request is at startup, asking GitHub whether a
newer version has been released. It sends nothing about you or your library, never downloads or
installs anything, asks before the first one, and can be switched off in Settings.

**Extensions are optional, arrive switched off, and each says what it sends.** The one that ships,
*Post to Civitai*, sends the files you choose and your Civitai API key to Civitai, and only when you
press Post.

## Is this for you?

**Probably, if** you generate heavily in ComfyUI on Windows, post to Civitai, and your bottleneck is
finding and finishing rather than making.

**Probably not, if** you want a general photo manager, you're on Mac or Linux, or your library is
small enough that File Explorer is fine.

## The questions you're about to ask

| | |
|---|---|
| **Do I need the custom nodes?** | No. They make grouping exact and Civitai metadata correct at generation time, but the viewer works without them — and *Export for Civitai* is still the file you upload either way |
| **Will it touch my files?** | It reads them where they are, and never reorganises your folders. Recycling is the one thing that moves a file — to the Windows Recycle Bin, or to a `_ToRecycle` folder beside the library on a network drive, with a 5-second undo |
| **Can it post to Civitai?** | Yes, with the *Post to Civitai* extension. Switch it on, add your Civitai API key, and a selection goes up as one draft. Nothing is public until you publish it on Civitai |
| **How big a library?** | Built and used against 110k+ files across five libraries — two local, three on network shares |
| **Windows only?** | Yes, end to end. There is no Mac or Linux version and none is planned |
| **Is there an installer?** | No. You download the folder and run it from there |
| **Does it work on a phone or tablet?** | No, and that is deliberate. It is a desktop tool: culling a library means seeing a lot of images at once, and a narrow screen cannot give you that |
| **A1111 / Forge images?** | **Untested — genuinely unknown.** It reads the A1111 `parameters` chunk and JPEG/WebP EXIF, so it should often work, but it has only ever been pointed at ComfyUI output. If you try it, say how it went |
| **Do I need a GPU?** | No. One optional extra uses one: the local image-quality scorer |
| **Videos?** | Yes — thumbnails, playback, and a video's own settings read out of the file. Video *prompts* are unreliable; see the README's known gaps |
| **What does it need?** | Python and a Chromium-based browser. See [Requirements](README.md#requirements) |

## What to expect from this project

A personal tool, built for one person's workflow on one machine, and shared in case it's useful to
someone else. Being straight about that up front:

- **MIT licensed** — use it, change it, redistribute it, build on it. Keep the copyright notice, and
  understand there is no warranty. The full text is in [LICENSE](LICENSE).
- **A permissive licence is not a support promise.** Not accepting pull requests, and there is no
  support commitment. You may fork it freely; that is the intended way to take it somewhere I'm not
  going to take it.
- **Telling me something is broken is welcome, though.** During the beta especially:
  [open an issue](https://github.com/vidi-vinci/vv-curator/issues). It is the only way I find out —
  the app reports nothing on its own. No promise about what gets fixed or when.
- The optional quality scorer installs [pyiqa](https://github.com/chaofengc/IQA-PyTorch), which is
  PolyForm Noncommercial licensed. That applies to your use of the scorer, not to the viewer.
- The icons are [Lucide](https://lucide.dev) (ISC), vendored in `app/vendor/lucide/` with their
  licence alongside them.

## Next

- [README](README.md) — what it does, how to run it, and the known gaps
- [Help](docs/help.md) — keys, defaults and the handful of things that surprise people

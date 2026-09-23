Posts files from your library to Civitai as one draft.

### What it sends

The files you choose and your Civitai API key, to Civitai, and only when you press **Post**. Nothing
else, and nothing at any other time. The key stays on this machine, in `config.json`.

### Posting

It needs a Civitai API key, set up once:

1. On Civitai, go to **Settings → Security & Apps → API Keys** and create a new key. Give it
   **Media & Posts** read and write, and **Profile** read. Leave **AI Services** write off: it can
   spend Buzz.
2. Save the key somewhere safe, such as a password manager.
3. In VV Curator, open **Settings → Extensions**, switch on **Post to Civitai**, and paste the key
   into its page.

Then select files and choose **Post to Civitai…** from the selection bar's `⋯` menu. They go up as
one **draft**: nothing is public until you publish it on Civitai.

**With Sets on, a card sends every file in it**, so a still and its video both go. That is usually
what you want: Civitai reads the LoRAs from the still, and a video posted without one arrives with
only its checkpoint credited. **Turn Sets off to pick single files**, and exactly what you selected
goes.

**Civitai takes at most 20 files in one post.** Over that, the app says so before anything is sent.

**Mark these Posted** adds the Posted flag to what you sent, and leaves any other labels alone.

**The extension's Settings page keeps a Post log**: everything sent from this library, including
posts that failed, which you can try again from their row. A row records how the post was created
and never checks Civitai again, so publishing the draft there does not change it. **Forget** removes
the record here only.

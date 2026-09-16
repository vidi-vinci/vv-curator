# VV Curator — design system

No framework: **tokens** (CSS custom properties in `app/style.css` `:root`) plus a small set of
**component classes**. Read this before writing UI. It is a lookup, not an essay — if something
close already exists, use it; if you add a component, add its row here in the same change.

---

## Tokens

> **Adding a colour token — it must land in THREE places** or the Appearance editor drifts out of
> sync with the stylesheet:
> 1. `:root` in `app/style.css`, **and** a light value in `THEME_LIGHT` (`app.js`);
> 2. `THEME_GROUPS` in `app.js` (adds its row to the Appearance editor);
> 3. `THEME_TOKENS` in `server.py` (so a saved edit passes hex validation).
>
> Prefer deriving a related shade with `color-mix()` over a near-duplicate token — the
> filtered-count chip is `color-mix(in srgb, var(--sidebar-bg), #fff 8%)`, so it tracks the rail.

### Surfaces & text
| Token | Value | Use |
|---|---|---|
| `--bg` / `--bg2` / `--bg3` | `#14161a` / `#1b1e24` / `#23272f` | app bg / panels / inputs & raised |
| `--sidebar-bg` | `#0e0f12` | the left rail (darker than the panels) |
| `--fg` | `#e6e8ec` | primary text |
| `--muted` | `#9aa3af` | secondary text, labels |
| `--border` | `#2c313a` dark · `#a7afbf` light | dividers and container outlines. Quiet in dark (1.15:1 on `--bg3`); the LIGHT value is darker because `--border-control` derives from it, and the derived edge had to clear 3:1 — see below |
| `--border-control` | `color-mix(in srgb, var(--border), var(--fg) 35%)` | the edge of a **control** — button, input, select, textarea, `.icon-btn`, `.facet-trigger`, a quiet field once it is drawn |

**A control's edge is not a divider.** One token was doing both, and a hairline drawn to disappear
between two things is the wrong weight for the boundary of something you click: "Select all" read as
borderless. Derived rather than duplicated, so the Appearance editor's **Border** still governs both,
and mixed toward `--fg` rather than `#fff` so the one number lightens the edge on the dark ground and
darkens it on the light one. Dividers, container outlines and the card's own frame keep `--border`
and stay quiet.

**Both themes clear WCAG 2.1 AA 1.4.11 (Non-text Contrast) as of 2026-09-16** — 3.41:1 dark, 3.08:1
light at worst. The light edge sat at 2.12–2.89 until then, and the fix was to darken `--border` in
the light preset rather than change the shared mix: **`--border` is a per-theme value, so the light
edge moves alone.** That was recorded here as a change that would drag dark's edges with it, which
was wrong and held the decision up for a day. The real cost is the one paid: dividers and card
frames are a little heavier in light, since they read `--border` directly. `audit_contrast.py`
reports every pair clearing its bar.

### Accents & state
| Token | Value | Use |
|---|---|---|
| `--accent` | `#4f8cff` | primary action, focus, selection — and **text**: links, the keep caption, `.ext-busy`. Never the ground under `--on-accent`; see the row below |
| `--accent-fill` | *computed* — `color-mix(--accent, #000 20%)` | **the ground under `--on-accent`**, everywhere: `.chip`, `button.primary`, `.ftab-badge`, a selected card's check, every `.active` segment. White on `--accent` is 3.22:1, and no single blue can fix that — carrying white at 4.5 needs luminance ≤ 0.183, being readable as link text on `--bg` needs ≥ 0.211, and those do not overlap. Mixed rather than authored so a custom accent brings its own fill |
| `--active` | `#3fb950` | active-filter highlight, and a FILL on the first-run Libraries nudge |
| `--active-ink` | *computed* | text/glyph on an `--active` ground. Set by `applyTheme` (`inkOn`), same construction as the label inks. **Nothing uses it today** — the one caller took `--on-accent` instead (the author's call, 2026-09-07); kept because the next thing filled with `--active` will want it |
| `--modified` | `#e0a23a` | drift marker — filters changed since a snapshot was restored |
| `--danger` | `#ff6b6b` | destructive actions, errors |
| `--success` | `#43d17a` | done / confirmed |
| `--on-accent` | `#fff` | text and icons **on `--accent-fill`** — the name predates the split and stayed, because it is also the ink on `--active` fills and the 18% lightener every filled hover mixes in |
| `--star` | `#ffca3a` | favourite star — **absolute, in both themes.** It lives in the corner of a picture, so the light preset must not darken it; `--star-edge` is what carries it on a themed surface. See *Marks on a picture* below |
| `--reward-fg` | *computed* — `inkOn(--reward-bg)` | ink on the Quality badge. Was `var(--bg)`, which put a themed ink on a gold that is the same in both themes: 9.14:1 in dark, **1.85:1** in light. Computed for the same reason the label inks are — `--reward-bg` is user-editable |
| `--star-edge` | *computed* — `color-mix(--star, #000 40%)` | the star's rim. Gold on a light ground is 2.28:1 against the 3.0 a state mark needs; 1.4.11 asks that of a component's **boundary**, so the rim clears it and the gold stays gold. Applied as `drop-shadow(0 0 1px …)` on both stars — via `--star-rim` on the card (composed into the existing thumbnail shadow) and on `#dFav.on::before` (a mask, so there is no path to stroke) |

### Marks on a picture

**A mark laid over a picture must carry its own ground — and once it does, that ground may follow
the theme.** It cannot rely on what is behind it: a photograph follows nobody, and the letterbox
beside it follows the preset. So legibility comes from a scrim, never a hairline. What stays
absolute is anything with no ground of its own — the star, which has only a rim, and the Quality
badge's gold, whose ink is computed from itself rather than from the page. Pinned by
`tests/test_media_marks.py`.

| Token | Use |
|---|---|
| `--card-scrim` / `--card-scrim-soft` / `--card-scrim-strong` | the ground under the card's own furniture: badges, facts bar, caption, zoom, selection check |
| `--card-ink` | what sits on those grounds |
| `--card-edge` / `--card-edge-soft` | the selection check's outline, the idle star |

All six are `light-dark()` pairs whose **dark halves are exactly what shipped**, so the dark theme
does not move. They resolve against `color-scheme`, which `applyTheme` already sets from the
effective `--bg` — so a custom theme picks the right side for free, with no list to keep in step.
This is why they are not `THEME_LIGHT` entries: nobody should be editing them, and that map takes
plain hex rather than `rgba`.

**Hover lifts, selection does not.** A hovered card takes a 2px accent ring (an inset outline) plus
`--card-lift`; a selected card keeps its 3px ring and no second, thinner one. What separates the two
states is the **lift**, not one pixel of width. `--card-lift` themes only its *colour* — `light-dark()`
returns a `<color>`, not a whole shadow — so one geometry reads as a drop shadow under a light card
and a pale glow around a dark one, since a drop shadow is nearly invisible on a dark ground. Neutral
rather than accent-tinted: a blue halo is close to what *selected* means, and a grid of them would
compete with the one card actually selected.

> **Never a thicker border for a state.** A border that changes width reflows the card and nudges its
> neighbours as the pointer sweeps the grid. Use an inset outline, which paints inside the box that
> already exists — the selected state and the filmstrip's current item both already do.
>
> And **scope the hover ring off `.selected`**: the hover rule sits later in the file, so at equal
> specificity a selected card under the pointer would drop to the thinner ring. Same source-order
> trap the grid card's gold star hit in August.

**The marks scale with the card.** `--card-badge-h` is 16px at Small and Medium, 20px on
`.grid.cards-lg` (256) and 24px on `.grid.cards-xl` (512). It was flat at every size, so a badge
went from 12.5% of a Small card to 3.1% of an Extra-large one — the author, on Large: *"they
seem...small"*. Everything at a card's foot derives from it: both bottom clusters, the selection
check, the star and its svg, the label band, and the insets that keep them clear of each other. The
glyph inside a badge is `calc(--card-badge-h - --space-1)`, which is 12px at the base — exactly the
`--glyph-xs` it replaced, so nothing moves until the card does.

> **A derived token must be declared where the override can reach it.** `--card-label-h` was written
> on `:root` as `var(--card-badge-h)`, and a custom property is substituted where it is *declared*,
> not where it is read — so it resolved against the base 16px once and inherited that number straight
> past `.grid.cards-lg`. The band stayed 16 while the badges grew. It is declared on `.card` and
> `.strip-item` instead. Caught by measuring a rendered band; the token alone looked right.

**The card's chrome is not the overlay's.** `--scrim-strong` still backs the modal backdrop and the
detail view's own caption, and stays dark in both presets — those dim the whole app, or sit on the
detail view's `--img-bg` letterbox, which is absolute. Reaching for `--scrim` on a card is what this
separation exists to stop: the furniture was authored dark-on-anything, which recedes on a dark UI
and turns into high-contrast blocks on a light one.

**A letterbox belongs to the thing holding the picture, not to the page behind it.** A card letterboxes against `--bg2` (what `.card` is filled with), a filmstrip item against `--bg3` (what it is filled with), the detail view against `--bg`. Pinned to `--bg` everywhere, the bars matched the page exactly, so wherever a picture did not fill its square the card's own extent dissolved into the background and only a 1.3:1 hairline said where it ended — which is what read as a flat, uniform grid. The rule is the same in both themes; it is the *value* that differs per surface. `--img-bg` is now an alias of `--bg` rather than an absolute near-black: it made the detail pane a dark slab inside a light UI, and even in the dark theme it was a *second*, darker black, so the picture sat in a visible rectangle of its own. The detail view's own chrome — nav, close, the maximised caption — takes the same treatment as the card's, because it sits on the same picture.

The grid's letterbox is `--bg`: `object-fit: contain` means anything that is not square
shows bars, and the badges sit in the corners — which for most pictures *is* bar, not picture. It
was `--bg` until 2026-09-15, so the light preset turned those bars near-white and the 1.5px white
edge ring every badge then carried simply vanished. The grid and the detail view now letterbox
against the same value. **That ring is gone** — with the ground reliably dark the scrim carries the
badge on its own, and a white outline around every mark was reading as a button. If a badge ever
does disappear on a pale thumbnail, the answer is a stronger scrim, not an outline.

Two further rules the same day's faults produced:

- **Translucent shapes must not overlap.** ONE scrim is the shared badge recipe and is fine; the
  fault was stacking. The set mark was three 78%-black squares on a diagonal, and where two met the
  alpha compounded to 95%. On a near-black ground both read as black, which is why it looked right
  for a year.
- **A mark must survive having its colour removed.** The star and the selection check already did,
  by being outline-versus-filled. The set mark carried its meaning in density alone and did not.
- **Fill carries meaning, never emphasis.** Every icon token is stroked except `--icon-star-on`,
  which is filled because filled is what *favourited* means once you take the colour away — it is
  the solid half of a deliberate pair. `--icon-play` was filled too, by media-player convention
  rather than by anything the app needed, and it sat in one badge cluster beside the stroked set
  mark: one solid glyph against one drawn one. Both it and the music note's noteheads are stroked
  now, so the cluster is one weight. Reach for the scrim or the size when something needs to be
  louder. `tests/test_media_marks.py` fails on any new filled glyph.
- **Don't draw what the icon set already has.** The set mark was rebuilt twice in one day — first as
  two opaque cards, which fixed the compounding and was still wrong: a pale fill is the loudest
  thing on a dark card, and a CSS rectangle with a box-shadow reads SOFT beside 2px strokes drawn to
  a spec. It is Lucide `layers` in the standard scrim pill now, so it is a sibling of ▶ rather than
  its own kind of object. **A bespoke mark needs a reason the vendored set cannot serve**, and
  "several pictures" was not one. `tests/test_media_marks.py` checks the glyph has a vendored source
  file, so a retyped path fails.

### Badges, tags, labels
`--reward-bg #e0b23a` + `--reward-fg` (*computed*, see above) (quality badge) · `--tag-fav #e0b83a` ·
`--chip-exclude #8a4a4a` · `--badge-fg #ffd479` ("no meta") · `--cap-fg #d7dbe2` (card caption) ·
`--chip-x-hover` / `--chip-x-hover-excl`.

Curation labels: `--label-publish #45b06a` · `--label-published #4a86c5` · `--label-refine #e0873a`
· `--label-explore #9a6ff0` · `--label-video #3fb8b0`. The label *set* (slug/name/key/colour) is one
`LABELS` constant in `server.py`, served via `/api/config`.

### Scales

**Spacing — 4px grid.** `--space-1` 4px · `-2` 8 · `-3` 12 · `-4` 16 · `-5` 20 · `-6` 24 · `-8` 32 ·
`-10` 40 · `-12` 48 · `-16` 64, in rem against a 16px root. **Never write a raw px for padding,
margin, gap or inset** — the stylesheet has none. 1px hairlines are the only exception. The grid
governs the OUTER box; internal padding is whatever makes the outer land on it.

**Control heights — declared, padding derived.** `--control-h` 24px (icon buttons, segmented bars,
strip items) · `--field-h` 36px (triggers, selects, inputs) · `--detail-h` 32px (the detail view's
own scale: `.da-btn`, the filmstrip's size select) · `--control-pad` centres a `--glyph-sm` inside
`--control-h`. Declaring the height and deriving the padding is the point: declaring padding twice
is how `.size-seg` sat at 26px against `.icon-seg`'s 25px — and how the filmstrip select sat at
34px against `.da-btn`'s 32px, with three comments claiming both were 34.

**The detail view is a second scale, not an exception to the first.** `--control-h` is sized for a
420px rail holding a dozen filters; in a near-fullscreen overlay it reads as a mis-sized fragment.
That is why `--detail-h` exists — and why a control on that surface takes it rather than picking a
spacing step that happens to look right.

**Type — six steps, nothing outside them.** `--text-xs` 11px (captions, counts, badges) ·
`--text-sm` 12 (cap labels, chips, small buttons) · `--text-base` 13 (default) · `--text-md` 14
(rail values, inputs) · `--text-lg` 16 (brand, headings) · `--text-xl` 18 (modal titles).
`--text-sm` and `--text-md` are one pixel apart deliberately — collapsing them makes a column of
matched sizes read as mismatched.

**Glyphs are a separate scale** — `--glyph-sm` 16px · `--glyph-md` 24 · `--glyph-lg` 32 ·
`--glyph-xl` 48 — because a 48px "type size" is a zoom control, not type.

**Radius.** `--radius-xs` 4px (badges, swatches) · `--radius-sm` 6px (chips, marks ~24px up) ·
`--radius-field` 8px (inputs, buttons, panels) · `--radius-modal` · `--radius-pill` 999px (count
badges only). Radius is **not** on the 4px grid.

**Other:** `--shadow-modal` · `--shadow-pop` · `--scrim-busy` / `--scrim-strong` · `--sidebar-w`
420px (resizable, persisted) · `--card-badge-h` 16px (every card corner badge derives from it) ·
`--card-min` 192px · `--rail-pad` 14px · `--sidebar-label-w` · `--ui-scale` ·
`--nav-clear` (what the detail view's prev/next arrows own at the edge of the media pane, derived
from the arrow's own box — anything that must stay out from under one measures itself with this).

**`--ui-scale` has no UI control and is not getting one** (decided 2026-08-15): browser zoom already
scales text, controls, spacing *and* thumbnails, and the browser's font-size setting scales the UI
without the images. Two complementary mechanisms that both already work.

---

## Rules that bind

Short because they are lookups. Each one exists because breaking it shipped a bug.

- **Never hardcode a hex.** Colours are tokens; see the three-place checklist above.
- **Never invent a noun.** Check the Glossary below, the way a colour gets checked against `:root`.
- **Use the existing component, or make a real new one — never a degraded variant of a shared
  class.** Restyling `.chip` with a dashed border and muted text produced grey-on-blue that read as
  broken. **The difference goes OUTSIDE the component**: a labelled row, a heading, a position.
- **A mark that can appear on two surfaces is a COMPONENT, never a part of one of them.** Scoping it
  to a parent (`.card .set-badge`) is not a tidiness choice — it is a bug with a delay on it, because
  the second surface then cannot show it at all. This cost four marks in one day (2026-09-05): the
  set stack, the curation label band and the favourite star were all card-only, so the filmstrip —
  **the view you review IN** — was the one view that could not show what you had already decided
  about an image. Write the selector unscoped and let each surface differ *outside* the component:
  its position, and `border-radius: inherit` where the two boxes round differently.
- **A mark on two surfaces needs its update path on both.** The component is half the job; the other
  half is that a grid re-render and a strip re-render are different events. Hook the one function
  that already knows the value changed — every favourite path funnels through `setCardFav`, every
  label through `refreshCard` — rather than each caller.
- **There is no global `.hidden` utility.** Every component scopes its own (`.popmenu.hidden`,
  `.kv.hidden`, …). A new element that toggles `hidden` **must add its own rule**, or the class
  silently does nothing. The one exception is `[data-ext].hidden`, opt-in by attribute for the
  controls an extension owns; an `<option>` uses the HTML attribute instead, because a display rule
  does not remove one from a select's keyboard navigation. Pinned by `tests/test_css_hidden.py`.
- **When verifying, assert computed display and `elementFromPoint`** — never
  `classList.contains('hidden')`. A menu can draw perfectly and be unclickable.
- **A control with no fixed backdrop needs two cues.** The detail view's prev/next sat on a 50%-black
  scrim, which reads as a lozenge over a photograph and vanishes over `--img-bg` or a song's own dark
  panel — measured 1.08:1 there, a white glyph with no button under it. It keeps the fill *and* takes
  a `--border-control` ring (3.96:1 on that ground): whichever cue the backdrop defeats, the other
  still draws the control.
- **A scroller with a hidden scrollbar owes an edge cue.** The filmstrip hides its bar (a horizontal
  one inside a fixed-height row eats the pixels the thumbnails are sized from), and so said nothing
  about continuing past either end. Each end fades into the row's own ground, drawn only on a side
  that can still scroll. Hard because narrow: a long gentle fade reads as a vignette and hides more of
  the picture while saying less.
- **A JS-placed popmenu is `.popmenu.placed`**, added by `placeMenu()`; `closeMenus()` closes by that
  class. **Never an id in the rule** — an id (1,0,0) beats `.popmenu.placed` (0,2,0), so an anchoring
  declaration left on `#thatMenu` survives placement and fights it. `#snapActions` kept a `right: 0`
  that way and opened 1041px wide, pinned from the rail to the far side of the grid.
- **The rail ranks the whole rail, not each menu.** `#sidebar` is `position: sticky`, which makes a
  stacking context whatever its `z-index` — so every `z-index` inside it, `.placed`'s 80 included, is
  sealed in and ranks against the page as `#sidebar` does. At `auto` that ties with `.card` and loses
  on tree order, and everything the rail overhangs with draws under the grid. Hence `#sidebar { z-index: 16 }`
  (over `.card` and the grid's `.selbar`, under `.pane-busy` and the overlays) with `.resizer` one
  above it. Raising a single menu cannot work, and would not have covered `#libPanel`, which is not
  a placed menu and overhangs the rail too.
- **A panel sized to the rail must be POSITIONED against the rail.** `#libPanel` is the rail's width
  but hung off `.lib-dd`; the day the Libraries label pushed that trigger a column to the right, the
  panel went with it and `#sidebar`'s `overflow-x: hidden` cut off its `⋯` column. It anchors to
  `.lib-row` now, inset by `--rail-pad` on both edges and with no width of its own.
- **Idle vs disabled: `aria-disabled="true"`, not the `disabled` attribute**, for a control merely
  *waiting for input* — a disabled button does not reliably show its `title`, and on a bar drawn to
  teach what it does, the tooltip is the payload. One capture-phase guard on the container, never a
  check in each handler. Reserve real `disabled` for genuinely unavailable.
- **A control that cannot apply is HIDDEN, not dimmed.** Nothing to explain means nothing to draw.
- **A control is drawn when it is DOING something** (`.quiet-field`). Reset is the one sanctioned
  exception. A filter you cannot see must not still be filtering.
- **THE QUARTER-SIDE CAP: a mark 16px or under takes at most a quarter of its shortest side.**
  Bars and pills are exempt. Pinned by `tests/test_radius_cap.py`.
- **An icon must be ONE silhouette at 15px.** Two overlaid shapes become a smudge.
- **A divider must match a real boundary**, and a derived shade must mix toward `--fg`, not `#fff`.
- **Hierarchy between two controls needs more than order** — position carries hierarchy only while
  the things being ordered already look different.
- **Any computed layout value is a CACHE.** Ask what invalidates it. Prefer removing the need to
  measure (flex/grid that self-corrects) over measuring well.

---

## Components

Use these. Do not invent another for the same job.

| Class | Is | The trap |
|---|---|---|
| `.primary` `.secondary` `.tertiary` `.ghost` `.danger` `.link` on `button` | emphasis; `.btn-sm` / `.btn-lg` size; `.done` / `disabled` state | `.ghost` is a legacy alias of `.tertiary`. **Emphasis carries a WEIGHT, not just a colour**: `.primary` is 600 and `.secondary` explicitly resets to 400, so a new primary that only takes the accent background comes out looking almost right |
| `.icon-btn` | **every** icon-only button | its 24px geometry is derived, not hardcoded — don't set a height |
| `.chip` | removable pill: `<span class="chip"><span>label</span><button></button></span>` | solid `--accent` fill, so `--muted` text on it is unreadable |
| `.ctl-label` / `.field-head` / `.set-row > label` | cap label — one shared 12px/.04em uppercase muted spec | `#sidebar > .ctl-label` is a direct-child rule; don't wrap it |
| `.group-title` | section header (FILTERS/TAGS) | distinct from a cap label |
| `.qenter` | the ⏎ in a search box: "press Enter to keep this" | tracks the END OF THE TEXT, not the field edge, so `left` is set from JS against a `position:relative` `.searchbox` — nothing in CSS can see where text inside an `<input>` ends. Clamped short of `.qclear`, which stays pinned right. It is a real button and commits the term: a glyph beside a live control gets clicked whatever it looks like |
| `.dialog-choices` | the shared dialog's CHOICES mode — a confirm that asks *which*, not just whether | built by `_dlgOpen` from a `choices` array, never written into the markup: the options differ per caller and some exist only conditionally. **Checkboxes, not radios**, because the things chosen between are disjoint and "both" is a real answer. OK is disabled while nothing is ticked — a destructive action on an empty set would report success having done nothing |
| `.dialog-title` | the shared dialog's OPTIONAL heading | `--text-xl` with no margin of its own, matching `.setup-box h2` / `.rename-box h2` — a modal's title is one size wherever the modal is, and the box already spaces its children. **A heading is for a dialog that ANNOUNCES something before it asks**; an ordinary confirm gets none, or you have a label reading "Confirm" above the thing being confirmed. `_dlgOpen` clears it when a caller passes none — one dialog serves every confirm in the app, so a title left standing reappears over the next unrelated question |
| `.settings-tab-sub` | an extension's own tab in the Settings rail | indented on `--set-indent` and ellipsised to one line: the name comes from the extension's author, and a long one made the rail's height a property of what happened to be installed |
| `.ext-answer` / `.ext-answer-text` | an extension's prose answer in the detail panel | follows `.ext-tags`' anatomy on purpose — a labelled row, not restyled prose. The answer is shown, not stored, so nothing here persists |
| `.answers-list` / `.answer-row` | many answers at once, in a modal | reuses `.miner-box` the way `#traceModal` does. The rule that generalises: **a scrolling list in a modal is already solved**, so a new one adds only what its rows need — here, top alignment, because the thumbnail is one line tall and the prose beside it is several |
| `.set-test.warn` | the third state of a self-test: amber, on `--modified` | **a check with three honest outcomes must not be squeezed into two**, or the mark contradicts the sentence beside it — here a green tick beside "pictures may NOT work" |
| `.hint` | a quiet line of explanation beside something — `--muted` at `--text-sm` | colour and size only; layout belongs to the caller. `.set-hint` and `.setup-hint` predate it and are the same two declarations plus their own flex/margin — fold them in when either is next touched, not as a pass of its own |
| `.panel` | inset bordered container on `--bg3` | `.popmenu` shares the recipe; the first-run `.empty-hint` composes with it rather than drawing its own box |
| `.keycap` | a single key, drawn as a key: a bordered box carrying its letter, or the `--chevron` mask turned a quarter for `←` / `→` (`.k-left` / `.k-right`) | **the box is the point.** The focus view spelled its keys as the characters `← → ⇧` at 11px and they read as nearly nothing — the author: *"the little arrows are almost invisible - can we use real icons?"* A bordered box says "press this" whatever the glyph inside weighs. Sized from `--glyph-sm`, coloured from the `--card-*` pair, so it works on any picture in either theme |
| `.inline-ico` | a control's own glyph quoted inside a sentence — "click the ⧉ icon" | the SVG is LIFTED from the live element at render time, never copied into the string; `vertical-align` keeps it out of the line box so the paragraph's leading doesn't jump |
| `.lib-stat.nudge` | first-run state on the Libraries button: `--active` fill, `--on-accent` mark, fill-pulse | a filled state must override `button.lib-stat:hover`, which otherwise repaints it grey — this one rings, auto-refresh lightens |
| `.popmenu` | menu surface; `.popmenu button` styles items | descendant selector — a non-item child inherits it |
| `.icon-seg` / `.size-seg` | segmented control, exactly one `.active` | both derive from `--control-h` — `#themeSeg` scopes its WIDTH only, never its height, because one instance quietly taller is how the 26px-vs-25px drift above began. **Reach for this whenever a row of buttons is really one question**: the Appearance tab had `Dark` / `Light` / `Reset to defaults`, where Reset and Dark were the same click (dark *is* the shipped default), nothing said which theme you were on, and a user pressed Reset on a default theme, saw nothing happen and reported it broken. Three actions were two, and the state they were all describing had no name until `Custom` got one. The lit segment is derived from the colours themselves, not from the last button pressed, so a theme restored from `config.json` lights the right one with nothing having had to remember |
| `.quiet-field` | a control drawn only while doing something | see the rule above |
| `.vsep` / `.rail-sep` / `.pane-sep` | vertical / full-bleed horizontal / inset horizontal rule | `.rail-sep` cancels `--rail-pad` with a negative margin — they must agree. `.pane-sep` does not: full bleed means “section of the rail”, inset means “group within one pane” |
| `.rail-cluster` | two or more controls pinned to the far edge of a row that wraps | The `margin-left:auto` goes on the WRAPPER, never on one of the children — a bare child's auto-margin holds only until the row is narrow enough to wrap, and then strands its neighbour on another line. Currently the Libraries strip's timer + check |
| `.grid` / `.card` | square cards, hover reveals controls | `--card-min` sets the image box |
| `.card .check` / `.star` | the card's top-row marks | **one shared rule** — sizing them separately gives different centres. `.star` has a filmstrip twin (`.strip-item .star`), display-only there |
| `.set-badge` | the three-card stack meaning "this is a set" | **unscoped on purpose** — card and filmstrip both draw it; see the rule above |
| `.label-strip` | the curation label band, carrying the label's NAME | also unscoped. Its ink is COMPUTED per label by `applyTheme`, because the five colours are user-editable; corners take `border-radius: inherit` so it follows whichever box it sits in |
| `.card-bl` / `.card-br` / `.strip-bl` | corner mark rows, bottom left and right | they measure from `--card-bottom-inset` / `--strip-bottom-inset`, which lift when a label band is present. Never a raw number |
| `.card-facts` / `.cf-row.cf-always` / `.cf-hover` | the small print on a card ("card details") | order comes from `CARD_FACTS`; the panel can't list one the card can't draw |
| `.reward` | quality badge | card-independent by design — it draws the same anywhere |
| `.chips-row` | chip container | `:empty` collapses it |
| `.kv` | a Details row | hidden as a whole when its value is absent |
| `.mark-toggle` | the detail title row's curation marks | one component, two instances |
| `.overlay` + `.overlay-bg` | every modal shares **one shell** | a new modal adds only width, padding and inner layout |
| `.ext-tags` / `.ext-row` | an extension's found tags; a Settings → Extensions row | the chips inside are ordinary `.chip` |
| `.sidebar-busy` / `.pane-busy` / `.task-scrim` | the two feedback tiers | see below |

Icons are vendored [Lucide](https://lucide.dev) (ISC) in `app/vendor/lucide/`.

### Feedback state

Everything that dims, spins or reports progress goes through **`Busy`**, **`Job`** and
**`refreshView`** in `app.js`. Never touch `#status`, `#gridBusy`, `#taskScrim`, `#sidebarBusy` or
`#moreBusy` directly.

- `Job.start(spec)` for anything with a `/status` endpoint; `Job.during(fn)` when several jobs are
  one user-visible run. Never hand-roll a poll loop, a Stop button or a status message.
- The **working** tier (`.sidebar-busy` + `.pane-busy`) is one recipe shared by two elements — the
  same alpha reads differently over the bright grid and the near-black rail, so it must be a recipe
  rather than one tint. The **job** tier (`.task-scrim`) is heavier and means *blocked, use Stop*.

---

## Copy

The app has one user, who knows what it is for. Copy labels and disambiguates; it does not teach.

| Element | Limit |
|---|---|
| Button / menu item | 1–3 words, starting with a verb |
| Label | 1–3 words, sentence case (`Video pairs`) |
| Tooltip | one line, ~10 words |
| Help under a setting | **one sentence** |
| Toast | what happened + the count |
| Confirm | one line: what happens, what's irreversible |

- **The "why" never goes in the UI.** Rationale goes to the commit message or the reference.
- **Don't restate the label** — a tooltip on *Rebuild metadata* saying "rebuilds metadata" is noise.
  Say what it costs or keeps.
- **Numbers beat adjectives.** "Recycled 2,061 copies · 3.5 GB".
- **Lead with the verb.** Never *This…*, *Use this to…*, *Allows you to…*.
- **Draft the short version first.** Writing long and trimming reliably ships long.
- **A key hint is `Action (Key)`, and the key is written as you would press it** — `Close (Esc)`,
  `Add this as a search term (Enter)`, `Check for new files on disk (R)`, `Maximize image (F)`,
  `Previous (←)`. Capital letters, real key names, arrow glyphs. **One key per hint**: where two
  work, name the one the control is about — `Exit maximized view (F)` on a button whose twin says
  `Maximize image (F)`, not `(F or Esc)`. A control that does more than one thing keeps the shape
  and adds after a dash: `Magnify (Z) — hold to peek · click to keep on · scroll to zoom`.
  *(Three were out of step before this was written down: two lowercase letters and one hint naming
  two keys. A shape that lives only in the existing examples drifts every time someone adds one.)*
- **A key LEGEND is the other way round: the key first, drawn as a `.keycap`, then what it does.**
  `[K] Keep this one`. `Action (Key)` is for a control's own label or tooltip, where the action is
  the thing being named and the key is an aside; a legend is a list of keys, so the key is the
  column you scan. The focus-view header is the only legend in the app — if a second appears, it
  takes this shape rather than inventing a third.

---

## Glossary — one spelling, one meaning

Check new wording against this the way a colour gets checked against `:root`. The failure it
prevents is a tab saying *Groups* while a tooltip says *collection*, because the two sentences were
written on different days.

| Term | Means | Never call it |
|---|---|---|
| **Library** | One folder you added, with its own root id | root, source |
| **Snapshot** | A saved capture of the whole sidebar state | view, preset, saved search |
| **Set** | Files from one generation collapsed into a card | group, stack, bundle |
| **Label** | The exclusive one-key curation mark (`a`–`e`) | flag, status, rating |
| **Tag** | A free-text keyword, many per image | keyword, category |
| **Detail** | One reported fact about a file — Dimensions, Age, Model, Quality | fact, field, property |
| **Group** | *(BR-12, unbuilt)* a hand-picked, ordered list | collection, folder, bin |
| **Folder** | The real folder on disk | directory, path |
| **Quality** | The pyiqa score | rating, grade |
| **Hidden** | The "not this one, for now" mark | archived, excluded, deleted |
| **Recycle** | Move to the OS bin or `_ToRecycle`. Acts on FILES, recoverable | delete, remove, trash |
| **Remove** | Take a LIBRARY out of the list. Touches no file | delete |
| **Song** | An audio file in the library | track, music, clip |
| **Dimensions** | Pixel width × height | size, resolution |
| **File size** | Bytes on disk | size, weight |
| **Duration** | How long a video or song runs | length, runtime |
| **Age** | How long ago a file was made, in words | date (that is the absolute one) |
| **Generation settings** | Seed/steps/CFG/sampler/scheduler read from the file | parameters, gen params |

**Two entries are warnings.** *Group* is what `group_id` and `recompute_groups` call a Set
internally — the UI meaning wins. *Detail* is likewise `CARD_FACTS` in the code. A mechanical rename
of working selectors buys nothing a reader of this table doesn't already have.

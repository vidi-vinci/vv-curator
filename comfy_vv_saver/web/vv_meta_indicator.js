// VV Run Name — the "Saving meta to .txt" line on the node face.
//
// WHY THIS EXISTS. VV Run Name has a second job its name cannot advertise: when its
// `filename_prefix` output feeds a node we did not write, it also writes a `.txt` beside that
// node's file carrying the prompt and settings, because a video or a song has nowhere inside it to
// keep them. That behaviour is triggered purely by what you wire, and nothing said so. The author,
// 2026-09-06, on why the node never made sense to him — it does two things and the title names one.
//
// It has to be browser code. The Python only runs at generate time, and the question this answers
// is "what am I wired to RIGHT NOW". Nothing else in the package can see the canvas.
//
// READ-ONLY, AND PHRASED AS A STATEMENT. It was nearly "Save meta to txt", which reads as a button
// and would invite clicks that do nothing. A status line has to sound like status.
//
// IT NAMES NO NODE TYPE. An earlier mockup said "→ VHS Video Combine"; the author's objection was that
// this is one of many, and he is right — nodes.py looks for "not one of ours", never for video.
//
// MIRRORS _feeds_foreign_writer IN nodes.py. If that rule changes and this does not, the node will
// state something untrue, which is worse than the silence this replaces.

import { app } from "/scripts/app.js";

const NODE = "VVNameAndId";
const OUR_NODES = ["VVSaveImageCivitai", "VVNameAndId"];
const SLOT = 1;                    // the filename_prefix output
// The author's wording, 2026-09-06. The OFF state names the socket rather than saying "not saving",
// because the useful thing to know is how to turn it on -- and it says "not wired" rather than
// "empty": filename_prefix is an OUTPUT and always has a value, so empty is the wrong idea.
const ON = "Saving prompt + settings to .txt";
const OFF = "filename_prefix not wired - no .txt file saved";

// Is output slot 1 linked to any node that is not one of ours?
function feedsForeignWriter(node) {
  const out = node?.outputs?.[SLOT];
  if (!out?.links?.length) return false;
  const graph = node.graph;
  if (!graph) return false;
  for (const linkId of out.links) {
    const link = graph.links?.[linkId];
    if (!link) continue;
    const target = graph.getNodeById?.(link.target_id);
    // An unresolvable target is treated as foreign: the honest default is to warn that a file is
    // about to be written, never to quietly promise nothing will be.
    if (!target || !OUR_NODES.includes(target.comfyClass ?? target.type)) return true;
  }
  return false;
}

app.registerExtension({
  name: "vv.meta_indicator",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== NODE) return;

    const onDrawForeground = nodeType.prototype.onDrawForeground;
    nodeType.prototype.onDrawForeground = function (ctx) {
      onDrawForeground?.apply(this, arguments);
      if (this.flags?.collapsed) return;
      // Drawn rather than added as a widget, deliberately: ComfyUI stores widget values BY
      // POSITION, so adding one here would desync every VV Run Name already on a canvas and force
      // a delete-and-re-add. A painted line owns no value and cannot shift anything.
      const on = feedsForeignWriter(this);
      const LG = window.LiteGraph || {};
      ctx.save();
      // EVERYTHING HERE COMES FROM LITEGRAPH, not from constants of our own. It was a hardcoded
      // 11px and a hardcoded green, and the author painted a node green: "the contrast is even worse
      // with the green text". Reading the theme's own values means the line stays legible whatever
      // he sets, and matches the widgets above it instead of approximating them.
      ctx.font = "normal " + (LG.NODE_SUBTEXT_SIZE || 12) + "px " + (LG.NODE_FONT || "Arial");
      ctx.textAlign = "left";
      // ONE COLOUR FOR BOTH STATES, deliberately -- the author's call. The words carry the state, so it
      // never depends on colour, which also means it survives a recoloured node and reads the same
      // to anyone who cannot separate the two hues.
      ctx.fillStyle = LG.NODE_TEXT_COLOR || "#aaa";
      this.vvFitWidth(ctx);          // after the font is set: it measures with these metrics
      // 15 is LiteGraph's own widget margin, so this starts exactly where the widget pills above
      // it do. It was 12, which left it hanging a few pixels to their left.
      ctx.fillText(on ? ON : OFF, LG.NODE_WIDGET_MARGIN || 15, this.vvLineY());
      ctx.restore();
    };

    // Reserve the strip the line is painted into, so it cannot land on top of a widget.
    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      onNodeCreated?.apply(this, arguments);
      // Reserve exactly ONE WIDGET ROW, the same amount vvLineY steps down by. It was a flat 16,
      // which is less than a row -- so the line was drawn into space that had not been reserved for
      // it, and the only reason it fitted was that it had been glued to the bottom edge.
      const LG = window.LiteGraph || {};
      const row = (LG.NODE_WIDGET_HEIGHT || 20) + 4;
      const [w, h] = this.size;
      this.size = [Math.max(w, 240), h + row];
      this._vvFitted = false;
    };

    // WHERE THE LINE SITS. Anchored to the LAST WIDGET, not to the node's bottom edge -- it was
    // `size[1] - 6`, which glued it to the frame and left a gap whenever the node was taller than
    // its contents (the author, 2026-09-06: "the text is too low in the window... ideally locate it below
    // name_suffix with same spacing as items above").
    //
    // The spacing is LiteGraph's own row pitch, NODE_WIDGET_HEIGHT + 4, so the gap between
    // name_suffix and this line is the same gap as between name and name_suffix. The 0.7 is where
    // LiteGraph puts a widget's own baseline inside its row, so the text sits on the same line it
    // would if it were a widget.
    //
    // Walks BACKWARDS to the last widget that actually drew: a widget converted to an input is
    // hidden and never sets last_y, so taking widgets[length-1] blindly would anchor to a row that
    // is not on screen. Falls back to the old bottom-edge maths if nothing has drawn yet.
    nodeType.prototype.vvLineY = function () {
      const LG = window.LiteGraph || {};
      const H = LG.NODE_WIDGET_HEIGHT || 20;
      for (let i = (this.widgets?.length || 0) - 1; i >= 0; i--) {
        const y = this.widgets[i].last_y;
        if (typeof y === "number") return y + H + 4 + H * 0.7;
      }
      return this.size[1] - 6;
    };

    // WIDEN TO FIT, ONCE, AND MEASURE RATHER THAN GUESS. The off-state string is long, and a width
    // picked by counting characters is wrong the moment the theme's font changes. Measured in the
    // draw handler because that is the only place a ctx exists; guarded by a flag so it cannot
    // churn, and it only ever grows -- a node the user has widened is never pulled back in.
    nodeType.prototype.vvFitWidth = function (ctx) {
      if (this._vvFitted) return;
      this._vvFitted = true;
      const margin = (window.LiteGraph?.NODE_WIDGET_MARGIN) || 15;
      const need = Math.max(ctx.measureText(ON).width, ctx.measureText(OFF).width) + margin * 2;
      if (need > this.size[0]) this.setSize([Math.ceil(need), this.size[1]]);
    };
  },
});

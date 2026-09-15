// VV Bridge — the half that has to live inside the ComfyUI page.
//
// ComfyUI's canvas is only reachable from the browser: no external program can put a graph on it,
// which is why dragging a file in is normally the only way. This script runs inside the page, so
// when the server-side endpoint says "open this", it can call the very same loader a drop calls.
//
// It adds no node, no menu item and no button. It does nothing at all until something posts to
// /vv_bridge/open.

import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

app.registerExtension({
  name: "vv.bridge",
  async setup() {
    api.addEventListener("vv_bridge.open", async (event) => {
      const data = event?.detail || {};
      const graph = data.workflow;
      if (!graph || !graph.nodes) {
        console.warn("[VV Bridge] ignored a message with no workflow in it");
        return;
      }
      try {
        // The same call ComfyUI makes when you drop a PNG on the window. Deliberately nothing
        // more: not queued, not saved to the workflow list, not written to disk.
        await app.loadGraphData(graph, true, true, data.name || "VV Curator");
      } catch (err) {
        // Older and newer frontends have taken different numbers of arguments here, so a failure
        // is far more likely to be a signature change than a bad graph. Retry with the one
        // argument every version has accepted, and only then give up — loudly, because a silent
        // failure here is indistinguishable from the viewer never having sent anything.
        try {
          await app.loadGraphData(graph);
        } catch (err2) {
          console.error("[VV Bridge] could not open the workflow", err, err2);
          app.ui?.dialog?.show?.("VV Bridge could not open that workflow — see the console.");
        }
      }
    });
    console.log("[VV Bridge] ready — VV Curator can open workflows here");
  },
});

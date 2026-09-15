"""The bridge: lets VV Curator open a workflow on ComfyUI's canvas.

**This is not a node**, and that distinction is the reason it can work at all. A custom-node
*package* is loaded when ComfyUI **starts**, so it can register HTTP routes and ship browser
scripts; a *node* only does anything when a workflow **runs**. Nothing here appears in the node
menu and nothing is wired into a graph.

Why it has to live inside ComfyUI: a canvas can only be loaded from ComfyUI's own page. Its API can
queue a run, upload a file or save a workflow to the sidebar list — none of them *open* one for
editing. So the viewer asks, and the browser half (web/vv_bridge.js) does the opening.

It never queues a render, never writes a file and never saves to the workflow list. It stops exactly
where a drag-and-drop stops.

Shipped separately as `comfy_vv_bridge` for its trial (2026-08-17) and folded in here once it was
proven. **The route paths are unchanged from that package on purpose** — the viewer needs no edit,
and a half-updated install still answers.
"""
import json

# The event the browser script listens for. Namespaced so it can never collide with a ComfyUI core
# message or another package's.
OPEN_EVENT = "vv_bridge.open"
VERSION = 2

try:
    from aiohttp import web
    from server import PromptServer
except Exception:                 # imported outside ComfyUI (the package's own tests) — stay importable
    PromptServer = None


def register():
    """Add the two routes to ComfyUI's server. Safe to call when ComfyUI isn't there.

    Wrapped in its own try/except because a DUPLICATE ROUTE RAISES, and the likeliest cause is the
    standalone `comfy_vv_bridge` still sitting in custom_nodes beside this. Taking ComfyUI's whole
    startup down over that would be a rotten trade for a convenience feature — so it warns and
    leaves whichever copy registered first doing the job.
    """
    if PromptServer is None:
        return False

    @PromptServer.instance.routes.get("/vv_bridge/ping")
    async def vv_bridge_ping(request):
        """So the viewer can tell "ComfyUI is running" apart from "the bridge is installed". Without
        it, sending would be the only way to find out, and a silent failure would look exactly like
        a successful send."""
        return web.json_response({"ok": True, "version": VERSION})

    @PromptServer.instance.routes.post("/vv_bridge/open")
    async def vv_bridge_open(request):
        """Open a workflow on the canvas. Body: the workflow JSON, or {"workflow": …, "name": …}."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body was not JSON"}, status=400)

        graph = body.get("workflow") if isinstance(body, dict) and "workflow" in body else body
        if isinstance(graph, str):        # a workflow read straight out of a file's metadata
            try:
                graph = json.loads(graph)
            except Exception:
                return web.json_response({"error": "workflow was not JSON"}, status=400)
        # Checked here rather than in the browser so a bad send fails with a reason, in the app that
        # sent it, instead of silently doing nothing.
        if not isinstance(graph, dict) or "nodes" not in graph:
            return web.json_response({"error": "not a workflow (no nodes)"}, status=400)

        name = (body.get("name") if isinstance(body, dict) else None) or "VV Curator"
        # Reaches every connected ComfyUI tab, deliberately: with two open there is no way to know
        # which one you were looking at, and opening in the other would look like nothing happened.
        PromptServer.instance.send_sync(OPEN_EVENT, {"workflow": graph, "name": str(name)})
        return web.json_response({"ok": True})

    return True

"""VV Civitai saver — ComfyUI custom node package.

Two nodes (VV Run Name, VV Save Image), plus a **bridge** that is not a node: it lets VV Curator open a
workflow on the canvas with one click. See bridge.py for why that has to live inside ComfyUI.

Install: copy (or symlink) this folder into ComfyUI/custom_nodes/ and restart ComfyUI.
"""
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

# The browser half of the bridge. ComfyUI serves this folder and auto-loads the .js inside it.
WEB_DIRECTORY = "./web"

# Never let the convenience feature break the nodes: if the routes can't be registered (an older
# ComfyUI, or the retired standalone comfy_vv_bridge still installed and holding the same paths),
# the saver must still load. That is the part someone's generations depend on.
try:
    from . import bridge
    bridge.register()
except Exception as _e:                                    # pragma: no cover — ComfyUI-only path
    print(f"[VV] bridge not registered ({_e}); the nodes are unaffected")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

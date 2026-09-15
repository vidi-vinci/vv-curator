"""Regression test for the vendored tracer, against an API-format graph mirroring the real ILU
workflow: the sampler's scalars are WIRED from control nodes (mxSlider / Sampler Selector / Combo
Clone / rgthree Seed), and the model runs Checkpoint -> Power Lora Loader -> KSampler.

The KSampler's own widget values are deliberately absent/wrong here, exactly as they are stale in
the real graph — the point is to prove we read the control nodes, not the sampler's widgets.

Run:  python comfy_vv_saver/test_trace.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vendor import comfy_meta

G = {
    # --- control nodes (the real source of truth) ---
    "277": {"class_type": "mxSlider", "inputs": {"Xi": 28, "Xf": 28.0, "isfloatX": 0}},      # Steps
    "265": {"class_type": "mxSlider", "inputs": {"Xi": 5, "Xf": 5.0, "isfloatX": 1}},        # CFG
    "173": {"class_type": "Sampler Selector", "inputs": {"sampler_name": "euler"}},
    "269": {"class_type": "Combo Clone", "inputs": {"combo": "kl_optimal"}},
    "273": {"class_type": "Seed (rgthree)", "inputs": {"seed": 190649247989453}},

    # --- model path: checkpoint -> power lora loader -> sampler ---
    "4": {"class_type": "CheckpointLoaderSimple",
          "inputs": {"ckpt_name": "Illustrious\\cyberillustriousSemi_playV30.safetensors"}},
    "500": {"class_type": "Power Lora Loader (rgthree)", "inputs": {
        "model": ["4", 0], "clip": ["4", 1],
        "lora_1": {"on": True,  "lora": "ILU\\Util\\S1 Dramatic Lighting Illustrious_V2.safetensors", "strength": 0.25},
        "lora_2": {"on": True,  "lora": "ILU\\Util\\zy_illustrious_Realism_Enhancer_v1.safetensors", "strength": 0.20},
        "lora_3": {"on": True,  "lora": "ILU\\Util\\Smooth_Booster_v5.safetensors", "strength": 0.20},
        "lora_4": {"on": False, "lora": "ILU\\Util\\ShouldBeIgnored_v1.safetensors", "strength": 0.99},
    }},

    # --- prompts: text wired in through the combined/manual switch chain ---
    "355": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "MANUAL pos text"}},
    "310": {"class_type": "Text Concatenate", "inputs": {"text_a": "combined pos", "text_b": " more"}},
    "312": {"class_type": "CR Text Switch", "inputs": {"Input": 1, "text1": ["310", 0], "text2": ["355", 0]}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["500", 1], "text": ["312", 0]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["500", 1], "text": "lowres, worst quality, bad hands"}},

    # --- the sampler: every scalar wired ---
    "3": {"class_type": "KSampler", "inputs": {
        "model": ["500", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["292", 0],
        "seed": ["273", 0], "steps": ["277", 0], "cfg": ["265", 0],
        "sampler_name": ["173", 0], "scheduler": ["269", 0],
    }},
    "292": {"class_type": "EmptyLatentImage", "inputs": {"width": 896, "height": 1152, "batch_size": 1}},
}


def main():
    sid = comfy_meta._pick_sampler(G)
    params = comfy_meta.extract_gen_params(G, sid)
    raw_ckpt = comfy_meta._resolve_model(G, sid) or comfy_meta._first_loader_ckpt(G)
    loras = comfy_meta._extract_loras_raw(G)

    print("sampler node :", sid)
    print("params       :", params)
    print("checkpoint   :", raw_ckpt)
    print("loras        :", loras)

    checks = []
    for k, want in (("steps", 28), ("cfg", 5.0), ("sampler_name", "euler"),
                    ("scheduler", "kl_optimal"), ("seed", 190649247989453)):
        checks.append((k, params.get(k) == want, params.get(k), want))
    checks.append(("checkpoint via Power Lora Loader",
                   bool(raw_ckpt) and "cyberillustrious" in raw_ckpt, raw_ckpt, "…cyberillustrious…"))
    checks.append(("active loras (on:False excluded)", len(loras) == 3, len(loras), 3))

    print("\n-- checks --")
    failed = 0
    for name, ok, got, want in checks:
        if not ok:
            failed += 1
        print(f"{'OK  ' if ok else 'FAIL'} {name}: got {got!r} want {want!r}")

    meta = {"positive": "WIRED pos text", "negative": "lowres, worst quality, bad hands",
            "model_name": "cyberillustriousSemi_playV30", "loras": loras}
    text = comfy_meta._format_parameters(meta, params, 896, 1152, None, raw_ckpt, loras)
    print("\n-- A1111 parameters --")
    print(text)

    print(f"\n{failed} failed" if failed else "\nall checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

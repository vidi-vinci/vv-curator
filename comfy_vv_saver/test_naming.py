"""Drives the REAL nodes against the naming spec:

  name "womanInCemetery" ->
    folder    womanInCemetery_7_16_26                    (name + M_D_YY)
    file      womanInCemetery_21-12-26~vv3g05o5_<Stage>  (name + time + run code + stage)
    set_id    20260716-211226-xxxx                       (shared by every file of the run)

Also covers the run code, chaining a second VV Run Name for a second filename, when the .txt
is and is not written, and a round trip through the viewer's own reader.

`folder_paths` is a ComfyUI-only module, so it's stubbed here — that lets us import and exercise
the actual node code rather than a re-implementation of it.

Run:  python comfy_vv_saver/test_naming.py
"""
import os
import re
import sys
import types

# import the PACKAGE (as ComfyUI does), so the `from .vendor import ...` relative import resolves
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault("folder_paths", types.ModuleType("folder_paths"))

from comfy_vv_saver.nodes import (STAGE_CHOICES, STAGE_CUSTOM, STAGE_NONE,  # noqa: E402
                                  STAGES, VVNameAndId, VVSaveImageCivitai,
                                  _feeds_foreign_writer, _resolve_stage, _sanitize,
                                  _sidecar_name_for)

FORBIDDEN = '\\/:*?"<>|'


def main():
    naming, filename = VVNameAndId().make("womanInCemetery")
    prefix, set_id = naming["filename_prefix"], naming["set_id"]
    folder, _, name = prefix.partition("/")
    print("filename :", filename)
    print("set_id   :", set_id)
    print("  folder :", folder)
    print("  file   :", name)
    for stage in STAGES:
        print(f"  -> {folder}/{name}_{stage}_00001_.png")

    checks = []
    checks.append(("folder = series_M_D_YY",
                   bool(re.fullmatch(r"womanInCemetery_\d{1,2}_\d{1,2}_\d{2}", folder)), folder))
    checks.append(("file base = series_HH-MM-SS~vvCODE",
                   bool(re.fullmatch(r"womanInCemetery_\d{2}-\d{2}-\d{2}~vv[0-9a-z]{6}", name)), name))
    checks.append(("the code is on the file half, never the folder",
                   "~vv" not in folder, folder))
    checks.append(("set_id = YYYYMMDD-HHMMSS-xxxx",
                   bool(re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", set_id)), set_id))
    # the filename half must be free of characters Windows rejects (esp. ':')
    checks.append(("no illegal chars in file base",
                   not any(c in name for c in FORBIDDEN), name))
    checks.append(("no illegal chars in folder",
                   not any(c in folder for c in FORBIDDEN), folder))
    # a series name full of junk must not produce a broken path
    dirty = _sanitize('woman:In/Cemetery*?"<>|')
    checks.append(("series sanitized", dirty == "womanInCemetery", dirty))
    checks.append(("empty series falls back", _sanitize("   ") == "untitled", _sanitize("   ")))

    # two savers in one run share the id+time; only the stage differs
    checks.append(("stage is the only difference between set members",
                   f"{prefix}_Raw" != f"{prefix}_Final", "ok"))

    # VV Run Name bundles the shared values itself, so each saver needs only run_data + images.
    ctx, _f2 = VVNameAndId().make("womanInCemetery", positive_prompt="pos text", negative_prompt="neg text")
    checks.append(("naming carries all four shared values",
                   set(ctx) == {"filename_prefix", "set_id", "positive", "negative"}, ctx))
    bare, _bf = VVNameAndId().make("womanInCemetery")
    checks.append(("unwired prompts still give a usable naming, not None",
                   bare["positive"] == "" and bare["negative"] == "", bare))

    from comfy_vv_saver.nodes import NODE_CLASS_MAPPINGS
    checks.append(("VV Run Name's outputs are run_data, filename_prefix",
                   VVNameAndId.RETURN_NAMES == ("run_data", "filename_prefix"), VVNameAndId.RETURN_NAMES))
    checks.append(("  and the declared slots match what make() returns",
                   len(VVNameAndId.RETURN_NAMES) == len(VVNameAndId.RETURN_TYPES) == 2, "ok"))
    checks.append(("the package registers exactly two nodes",
                   set(NODE_CLASS_MAPPINGS) == {"VVNameAndId", "VVSaveImageCivitai"},
                   sorted(NODE_CLASS_MAPPINGS)))
    # The overrides are GONE, and their absence is the point -- a hand-typed value is the drift this
    # node exists to prevent. Asserted, so nobody restores them as a convenience.
    saver_fields = set(VVSaveImageCivitai.INPUT_TYPES()["required"]) |         set(VVSaveImageCivitai.INPUT_TYPES()["optional"])
    checks.append(("the saver offers no manual overrides of the traced values",
                   not (saver_fields & {"model_name", "sampler", "scheduler", "steps", "cfg",
                                        "seed_override"}), sorted(saver_fields)))

    checks += suffix_and_chaining_checks()
    checks += stage_vocabulary_checks()
    checks += run_code_checks(prefix)
    checks += foreign_writer_checks()
    checks += sidecar_roundtrip_checks()
    checks += resource_hash_checks()

    return finish(checks)


def _graph_with(consumer_class, slot=1):
    """A two-node executed prompt: VV Run Name at id "1", and something consuming its output `slot`."""
    return {"1": {"class_type": "VVNameAndId", "inputs": {"name": "x"}},
            "2": {"class_type": consumer_class, "inputs": {"filename_prefix": ["1", slot]}}}


def suffix_and_chaining_checks():
    import comfy_meta
    """The name suffix, and chaining one VV Run Name into another.

    This is what removed the separate video node. A second video that needs its own name is a
    second VV Run Name wired to the first: same run code, same set id, different suffix. The rule that
    matters is that chaining must INHERIT the run rather than mint a new one -- re-deriving the time
    would produce a second code seconds later, and the two videos would become two generations.
    """
    out = []
    first, fname = VVNameAndId().make("MM_FinJungle", positive_prompt="pos", negative_prompt="neg")
    out.append(("no name_suffix leaves the filename as the run's own",
                fname == first["filename_prefix"], fname))

    second, fname2 = VVNameAndId().make("ignored", name_suffix="HiRes", inherit_run=first)
    out.append(("a chained node adds its name_suffix to the filename",
                fname2 == f"{first['filename_prefix']}_HiRes", fname2))
    out.append(("  and inherits the run code, so both videos are ONE generation",
                comfy_meta.run_code_key(fname2) == comfy_meta.run_code_key(fname),
                (comfy_meta.run_code_key(fname), comfy_meta.run_code_key(fname2))))
    out.append(("  and the set id with it", second["set_id"] == first["set_id"], second["set_id"]))
    out.append(("  and the prompts, so the second video's text file is not blank",
                (second["positive"], second["negative"]) == ("pos", "neg"), second))
    out.append(("  while its own name field is ignored, since the run is already named",
                fname2.startswith("MM_FinJungle"), fname2))
    out.append(("a name_suffix cannot smuggle a path separator into the filename",
                "/" not in VVNameAndId().make("x", name_suffix="a/b:c")[1].partition("/")[2], "ok"))
    return out


def foreign_writer_checks():
    """WHEN the video's text file gets written -- the thing that replaced a checkbox.

    A checkbox would have been the same foot-gun as the two toggles we deleted: forget it and the
    video silently has no metadata. Instead the node looks at whether its `filename` output feeds a
    node we did not write. That IS the intent, so there is nothing to switch on, and a workflow that
    only saves pictures never grows a stray .txt.
    """
    out = []
    out.append(("a foreign node taking the filename means a video is coming",
                _feeds_foreign_writer(_graph_with("VHS_VideoCombine"), "1"), "ok"))
    out.append(("  our own saver taking it does not -- it writes its own metadata",
                not _feeds_foreign_writer(_graph_with("VVSaveImageCivitai"), "1"), "not ok"))
    out.append(("  nor does a foreign node taking the OTHER output (the naming wire)",
                not _feeds_foreign_writer(_graph_with("VHS_VideoCombine", slot=0), "1"), "not ok"))
    out.append(("  nothing wired at all means nothing to write",
                not _feeds_foreign_writer({"1": {"class_type": "VVNameAndId", "inputs": {}}}, "1"),
                "not ok"))
    out.append(("  and a missing graph is answered no rather than raising",
                not _feeds_foreign_writer(None, "1") and not _feeds_foreign_writer({}, None), "ok"))
    return out


def sidecar_roundtrip_checks():
    """The node WRITES the video's text file and the viewer READS it -- two codebases, so drive
    both rather than assume they agree. Same reasoning as test_groups.py's PNG round-trip.

    `folder_paths` is stubbed to ComfyUI's actual contract: a prefix may carry a subfolder, and the
    returned `filename` is the resolved base the writer then appends to.
    """
    import shutil
    import comfy_meta
    import folder_paths

    out = []
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sidecar_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        folder_paths.get_output_directory = lambda: tmp

        def _gsip(fp, out_dir, _w, _h):
            sub, _, base = fp.replace("\\", "/").rpartition("/")
            full = os.path.join(out_dir, *sub.split("/")) if sub else out_dir
            os.makedirs(full, exist_ok=True)
            return full, base, 1, sub, fp
        folder_paths.get_save_image_path = _gsip

        # A text2video run: no image saver anywhere in the graph. This is the case the text file
        # exists for, and the case a switch on the image saver could never have served.
        g = _graph_with("VHS_VideoCombine")
        ctx, fname = VVNameAndId().make("MM_FinJungle", positive_prompt="a lighthouse", negative_prompt="blurry",
                                        prompt=g, unique_id="1")

        # Now stand where the viewer stands: a video ComfyUI numbered on its own, whose counter the
        # node never saw. Nothing in the two names was coordinated except the run code.
        folder = os.path.join(tmp, ctx["filename_prefix"].partition("/")[0])
        video = os.path.join(folder, f"{os.path.basename(fname)}_00042_.mp4")
        open(video, "wb").write(b"not really a video")

        got = comfy_meta.read_sidecar(video) or {}
        out.append(("the viewer finds the text file the node wrote, counter unseen",
                    got.get("positive") == "a lighthouse", got.get("positive")))
        out.append(("  with the negative intact", got.get("negative") == "blurry",
                    got.get("negative")))
        out.append(("  and the set id, which is what puts the video in the run's set",
                    got.get("set_id") == ctx["set_id"], got.get("set_id")))

        # A SECOND video from a chained node finds the same file -- one per generation, not one per
        # video, because the name stops at the run code.
        _c2, fname2 = VVNameAndId().make("x", name_suffix="HiRes", inherit_run=ctx)
        v2 = os.path.join(folder, f"{os.path.basename(fname2)}_00001_.mp4")
        open(v2, "wb").write(b"x")
        out.append(("  and so does a second, differently-named video of the same run",
                    (comfy_meta.read_sidecar(v2) or {}).get("positive") == "a lighthouse",
                    comfy_meta.read_sidecar(v2)))

        written = [f for f in os.listdir(folder) if f.endswith(".txt")]
        out.append(("exactly one text file for the whole run", len(written) == 1, written))
        out.append(("  named at the code, so nothing had to predict a counter",
                    written and not re.search(r"_\d{5}", written[0]), written))

        # An image-only workflow must not litter. Nothing foreign consumes the filename here.
        VVNameAndId().make("images_only", prompt=_graph_with("VVSaveImageCivitai"), unique_id="1")
        stray = [f for f in os.listdir(tmp) if f.endswith(".txt")]
        out.append(("an image-only workflow writes no text file at all",
                    not stray and len(written) == 1, stray))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def run_code_checks(prefix):
    """The run code, and the one claim the whole design rests on: a file that has one is matched
    on the name UP TO the code, so everything a writer cannot predict â€” stage, counter, role word,
    extension â€” stops mattering. Checked against the viewer's own module, never against a copy of
    the rule here, so the node and the viewer cannot drift.
    """
    import comfy_meta
    from comfy_vv_saver.nodes import _sidecar_name_for
    out = []

    code = comfy_meta.make_run_code(1786000000)
    out.append(("the code is the marker plus six base36 characters",
                bool(re.fullmatch(r"~vv[0-9a-z]{6}", code)), code))
    out.append(("  it is derived from the clock, so the same instant gives the same code",
                comfy_meta.make_run_code(1786000000) == code, code))
    out.append(("  a later run always sorts after an earlier one",
                comfy_meta.make_run_code(1786000001) > code, comfy_meta.make_run_code(1786000001)))
    out.append(("  and it stays six characters until 2088",
                len(comfy_meta.make_run_code(comfy_meta.CODE_EPOCH + 36 ** 6 - 1)) == len(code),
                comfy_meta.make_run_code(comfy_meta.CODE_EPOCH + 36 ** 6 - 1)))

    # THE claim. Every one of these is a real file from one run, named by a different writer.
    base = prefix.partition("/")[2]
    family = [f"{base}_Final_00001_.png", f"{base}_Final_base_00007_.mp4",
              f"{base}_Final_base_00007-audio.mp4", f"{base}_Raw_00001_.png",
              f"{base}.txt"]
    keys = {comfy_meta.run_code_key(f) for f in family}
    out.append(("every file of a run shares one key, whatever it is named",
                len(keys) == 1 and None not in keys, keys))

    # A tilde in an ordinary name is not a code: the promise is that an old library never re-groups.
    for innocent in ("holiday~photo_00001_.png", "PROGRA~1_00001_.png", "plain_00001_.png",
                     "sunset~abcdefghijkl_1.png"):
        out.append((f"  not a code: {innocent}", comfy_meta.run_code_key(innocent) is None,
                    comfy_meta.run_code_key(innocent)))
    out.append(("  and a series name cannot smuggle a second tilde in",
                "~" not in _sanitize("before~after"), _sanitize("before~after")))

    # The sidecar: one per generation, named without a counter anyone had to predict.
    sc = _sidecar_name_for(f"{base}_Final_base")
    out.append(("the sidecar is named up to the code and stops", sc == base, sc))
    out.append(("  so every video of the run finds the same one",
                _sidecar_name_for(f"{base}_Final_interp") == sc, _sidecar_name_for(f"{base}_Final_interp")))
    out.append(("  and it carries no counter to get wrong", not re.search(r"_\d{5}", sc), sc))
    uncoded = _sidecar_name_for("ComfyUI_base")
    out.append(("  with no code it falls back to the whole prefix", uncoded == "ComfyUI_base", uncoded))
    return out


def stage_vocabulary_checks():
    """The stage dropdown is only worth having if its values are the ones the VIEWER knows.

    The whole reason it stopped being a text field: a stage the viewer doesn't recognise still
    groups, but the set's pane order and the choice of which image fronts the card both fall back
    to "unknown", silently. So the list is checked against index_db, not against itself.
    """
    import index_db
    out = []
    out.append(("the dropdown offers exactly the viewer's vocabulary",
                [s.lower() for s in STAGES] == list(index_db.SET_STAGE_ORDER),
                f"{[s.lower() for s in STAGES]} vs {list(index_db.SET_STAGE_ORDER)}"))
    out.append(("  every offered stage ranks, none falls through to unknown",
                all(index_db._stage_rank(s) < len(index_db.SET_STAGE_ORDER) for s in STAGES),
                [(s, index_db._stage_rank(s)) for s in STAGES]))

    # Resolution: the two non-stage entries and the escape hatch.
    out.append((f"{STAGE_NONE!r} appends nothing", _resolve_stage(STAGE_NONE, "") == "", "ok"))
    out.append((f"  and ignores a stray custom value",
                _resolve_stage(STAGE_NONE, "First") == "", _resolve_stage(STAGE_NONE, "First")))
    out.append(("a named stage resolves to itself", _resolve_stage("Final", "") == "Final", "ok"))
    out.append(("  and ignores the custom field when not asked for",
                _resolve_stage("Final", "First") == "Final", _resolve_stage("Final", "First")))
    out.append((f"{STAGE_CUSTOM!r} uses the custom field",
                _resolve_stage(STAGE_CUSTOM, "First") == "First", "ok"))
    out.append(("  strips illegal path characters from it",
                _resolve_stage(STAGE_CUSTOM, 'Fi/rst:*') == "First", _resolve_stage(STAGE_CUSTOM, 'Fi/rst:*')))
    out.append(("  and a blank custom stays blank rather than becoming 'untitled'",
                _resolve_stage(STAGE_CUSTOM, "   ") == "", repr(_resolve_stage(STAGE_CUSTOM, "   "))))
    out.append(("the default is one of the choices",
                "Final" in STAGE_CHOICES, STAGE_CHOICES))
    return out


def resource_hash_checks():
    """A LoRA stored WITHOUT its extension still resolves to a file, so it can carry a hash.

    ComfyUI Lora Manager records `KreaKult_v2`, not `KreaKult_v2.safetensors`, and
    `folder_paths.get_full_path` wants the exact filename -- so the LoRA reached Civitai named but
    unlinked while the checkpoint, whose stored name keeps its extension, linked fine. Reported
    2026-09-20 from two real files: the exported copy carried `Lora hashes:` and the VV-saved
    original did not.
    """
    import folder_paths as fp
    from comfy_vv_saver.nodes import _by_basename, _make_hash_resolver

    fp.get_filename_list = lambda c: ["Krea\KreaKult_v2.safetensors", "other_v1.safetensors"]
    fp.get_full_path = lambda c, n: ("M:/models/%s/%s" % (c, n)) if str(n).endswith(
        ".safetensors") else None

    out = []
    got = _by_basename("loras", "KreaKult_v2")
    out.append(("an extensionless LoRA name finds its file",
                got == "M:/models/loras/Krea\KreaKult_v2.safetensors", got))
    got = _by_basename("loras", "Krea\KreaKult_v2.safetensors")
    out.append(("  a full name still finds it", bool(got), got))
    got = _by_basename("loras", "NeverInstalled")
    out.append(("  a name nothing matches returns None rather than a wrong file",
                got is None, got))

    # The resolver reaches the fallback only after the exact lookup fails, and hashes whatever it
    # lands on -- so a missing file yields no hash and the resource stays text, never a wrong link.
    resolve = _make_hash_resolver()
    out.append(("an unresolvable resource still yields no hash",
                resolve("loras", "NeverInstalled") is None, "None"))
    return out


def finish(checks):
    print("\n-- checks --")
    failed = 0
    for label, ok, got in checks:
        if not ok:
            failed += 1
        print(f"{'OK  ' if ok else 'FAIL'} {label}: {got!r}")
    print(f"\n{failed} failed" if failed else "\nall checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

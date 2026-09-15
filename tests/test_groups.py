"""Regression test for index_db.recompute_groups across a MIXED library.

The point is that adding comfy_vv_saver's explicit `set_id` grouping must not disturb what already
worked: legacy filename image sets (1 MAIN / 2 DET / 3 REFINE) and still+video pairs (BASE/INTERP)
have to keep grouping exactly as before, alongside the new explicit sets.

Run:  python test_groups.py
"""
import hashlib
import os
import sqlite3
import sys
import time

# Run from anywhere: these tests live in tests/ and import the app's modules from the
# project root one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import comfy_meta
import index_db


def _gid_vvset(root_id, set_id):
    return hashlib.sha1(f"vvset|{root_id}|{set_id}".encode("utf-8")).hexdigest()[:12]


ROWS = [
    # (filename, folder, ext, root_id, set_id, set_stage)
    # 1. legacy filename set — must still group (this is what we must not break)
    ("X_1 MAIN_00001.png",   "a", ".png", "r1", None, None),
    ("X_2 DET_00001.png",    "a", ".png", "r1", None, None),
    ("X_3 REFINE_00001.png", "a", ".png", "r1", None, None),
    # 2. still+video pair — must still group
    ("Y_BASE_00001.png",     "a", ".png", "r1", None, None),
    ("Y_INTERP_00001.mp4",   "a", ".mp4", "r1", None, None),
    # 2b. LTX-style run: still + VHS's muxed '-audio' video must pair (silent sibling dropped upstream)
    ("ltx_21-12-26_Final_00001.png",       "a", ".png", "r1", None, None),
    ("ltx_21-12-26_Final_00001-audio.mp4", "a", ".mp4", "r1", None, None),
    # 3. new explicit set from the saver
    ("w_09-13-32_Raw_00001_.png",    "b", ".png", "r1", "S1", "Raw"),
    ("w_09-13-32_Detail_00001_.png", "b", ".png", "r1", "S1", "Detail"),
    ("w_09-13-32_Final_00001_.png",  "b", ".png", "r1", "S1", "Final"),
    # 4. a lone stage is NOT a set
    ("solo_10-00-00_Final_00001_.png", "b", ".png", "r1", "S2", "Final"),
    # 5. plain ungrouped image
    ("z_00001.png", "b", ".png", "r1", None, None),
    # 6. same set_id in a DIFFERENT root must not cross-merge
    ("other_Raw_00001_.png",   "b", ".png", "r2", "S1", "Raw"),
    ("other_Final_00001_.png", "b", ".png", "r2", "S1", "Final"),
    # 7. explicit set spanning two folders still groups (id beats folder scoping)
    ("split_Raw_00001_.png",   "c", ".png", "r1", "S3", "Raw"),
    ("split_Final_00001_.png", "d", ".png", "r1", "S3", "Final"),
    # 8. explicit id wins over a legacy-looking filename
    ("q_1 MAIN_00001.png", "e", ".png", "r1", "S4", "Raw"),
    ("q_2 DET_00001.png",  "e", ".png", "r1", "S4", "Final"),
    # 9. A VIDEO IN A SET. Only possible since videos can read a `.txt` sidecar: set ids live in PNG
    #    chunks, and a video is never opened during a scan, so before this a video could reach a
    #    group ONLY by the filename pair rule. Here the names have nothing in common — the id does
    #    all the work, which is the whole point.
    ("vidset_still_00001_.png", "f", ".png", "r1", "S5", "Final"),
    ("totally_different_name.mp4", "f", ".mp4", "r1", "S5", None),
    # 10. THE RUN CODE. One generation, five files, five different writers — the image saver, a
    #     video node, a frame-interpolator, VHS's muxer, and whatever wrote the sidecar. Nothing
    #     lines up: the stages differ, the counters differ (a video node counts on its own prefix),
    #     and the sidecar has no counter at all because nobody could predict one. Only the code is
    #     shared, so only the code can group them.
    ("MM_14-20-52~vvk3n9x7_Final_00001_.png",       "g", ".png", "r1", None, "Final"),
    ("MM_14-20-52~vvk3n9x7_Raw_00003_.png",         "g", ".png", "r1", None, "Raw"),
    ("MM_14-20-52~vvk3n9x7_Final_base_00009_.mp4",  "g", ".mp4", "r1", None, None),
    ("MM_14-20-52~vvk3n9x7_Final_interp_00002.mp4", "g", ".mp4", "r1", None, None),
    # 11. Two runs, one folder, one second apart — the codes differ, so they must stay apart even
    #     though everything a filename heuristic looks at is identical.
    ("NN_09-00-00~vva00001_Final_00001_.png", "h", ".png", "r1", None, "Final"),
    ("NN_09-00-00~vva00001_Final_00001_.mp4", "h", ".mp4", "r1", None, None),
    ("NN_09-00-00~vva00002_Final_00001_.png", "h", ".png", "r1", None, "Final"),
    ("NN_09-00-00~vva00002_Final_00001_.mp4", "h", ".mp4", "r1", None, None),
    # 12. A coded run and an UNCODED one sharing a folder. The old rules still have to reach the
    #     second one — this is the mixed-library case, and the whole compatibility promise.
    ("mix_11-00-00~vvb00001_Final_00001_.png", "i", ".png", "r1", None, "Final"),
    ("mix_11-00-00~vvb00001_Final_00001_.mp4", "i", ".mp4", "r1", None, None),
    ("old_1 MAIN_00001.png",   "i", ".png", "r1", None, None),
    ("old_3 REFINE_00001.png", "i", ".png", "r1", None, None),
    # 13. A tilde in an ordinary name is not a code. These two would pair anyway; the point is
    #     that the CODE pass must not be what does it, or an old library re-groups on a coincidence.
    ("holiday~photo_00001.png", "j", ".png", "r1", None, None),
    ("holiday~photo_00001.mp4", "j", ".mp4", "r1", None, None),
    # 14. A lone coded file is not a group — same rule the explicit set_id pass follows.
    ("lonely_12-00-00~vvc00001_Final_00001_.png", "k", ".png", "r1", None, "Final"),
]


def build():
    conn = sqlite3.connect(":memory:")
    conn.executescript(index_db.SCHEMA)
    index_db._migrate(conn)
    for fn, folder, ext, rid, sid, stage in ROWS:
        path = f"/{rid}/{folder}/{fn}"
        conn.execute(
            "INSERT INTO images(path,rel_path,folder,filename,ext,mtime,size,has_meta,"
            "indexed_at,root_id,set_id,set_stage) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (path, f"{folder}/{fn}", folder, fn, ext, 1.0, 1, 1, time.time(), rid, sid, stage))
    conn.commit()
    return conn


def roundtrip_checks():
    """The saver WRITES these PNG chunks and the viewer READS them — two separate codebases, so
    prove the actual contract rather than assume it. Writes a PNG exactly the way
    comfy_vv_saver/nodes.py does (PIL PngInfo + add_text), then reads it back with comfy_meta."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    out = []
    tmp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_roundtrip_tmp.png")
    try:
        info = PngInfo()
        info.add_text("vv_set_id", "20260716-091332-4293")
        info.add_text("vv_set_stage", "Refine")
        Image.new("RGB", (4, 4), (0, 0, 0)).save(tmp, pnginfo=info)
        meta = comfy_meta.extract(tmp)
        out.append(("PNG round-trip: set_id read back",
                    meta.get("set_id") == "20260716-091332-4293", meta.get("set_id")))
        out.append(("PNG round-trip: set_stage read back",
                    meta.get("set_stage") == "Refine", meta.get("set_stage")))

        # A saver with embed_comfy_wf off writes no `prompt` chunk; the set keys must survive that
        # (extract() returns early when there's no graph).
        out.append(("set keys survive a PNG with no ComfyUI graph",
                    meta.get("set_id") is not None and meta.get("positive") is None, "ok"))

        # A plain PNG with no chunks at all must not blow up or invent values.
        Image.new("RGB", (4, 4), (0, 0, 0)).save(tmp)
        bare = comfy_meta.extract(tmp)
        out.append(("plain PNG yields no set keys",
                    bare.get("set_id") is None and bare.get("set_stage") is None, "ok"))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return out


def main():
    conn = build()
    index_db.recompute_groups(conn)
    g = {r[0]: r[1] for r in conn.execute("SELECT filename, group_id FROM images")}
    for fn in sorted(g):
        print(f"  {g[fn] or '(none)':<14} {fn}")

    checks = []

    def same(label, *names):
        gids = {g[n] for n in names}
        checks.append((label, len(gids) == 1 and None not in gids, gids))

    def ungrouped(label, name):
        checks.append((label, g[name] is None, g[name]))

    same("legacy MAIN/DET/REFINE set still groups",
         "X_1 MAIN_00001.png", "X_2 DET_00001.png", "X_3 REFINE_00001.png")
    same("still+video pair still groups", "Y_BASE_00001.png", "Y_INTERP_00001.mp4")
    same("still + VHS '-audio' video pairs",
         "ltx_21-12-26_Final_00001.png", "ltx_21-12-26_Final_00001-audio.mp4")
    same("explicit set_id groups", "w_09-13-32_Raw_00001_.png",
         "w_09-13-32_Detail_00001_.png", "w_09-13-32_Final_00001_.png")
    ungrouped("lone stage is not a set", "solo_10-00-00_Final_00001_.png")
    ungrouped("plain image stays ungrouped", "z_00001.png")
    same("explicit set spanning folders groups", "split_Raw_00001_.png", "split_Final_00001_.png")
    same("explicit id wins over legacy filename", "q_1 MAIN_00001.png", "q_2 DET_00001.png")
    # A VIDEO joins a set by id alone. Their filenames share nothing, so the pair rule cannot
    # be what did it — and a video could not carry an id at all until sidecars.
    same("a video joins a set by id, not by filename",
         "vidset_still_00001_.png", "totally_different_name.mp4")

    # --- the run code ---------------------------------------------------------------------
    # The claim: everything after the code is noise. Stage, counter, role word — all differ here.
    same("one generation groups on its code alone, whatever wrote each file",
         "MM_14-20-52~vvk3n9x7_Final_00001_.png", "MM_14-20-52~vvk3n9x7_Raw_00003_.png",
         "MM_14-20-52~vvk3n9x7_Final_base_00009_.mp4",
         "MM_14-20-52~vvk3n9x7_Final_interp_00002.mp4")
    checks.append(("two runs one second apart stay apart, though their names are otherwise equal",
                   g["NN_09-00-00~vva00001_Final_00001_.png"] !=
                   g["NN_09-00-00~vva00002_Final_00001_.png"],
                   (g["NN_09-00-00~vva00001_Final_00001_.png"],
                    g["NN_09-00-00~vva00002_Final_00001_.png"])))
    same("  and each still keeps its own video",
         "NN_09-00-00~vva00001_Final_00001_.png", "NN_09-00-00~vva00001_Final_00001_.mp4")
    ungrouped("a lone coded file is not a group", "lonely_12-00-00~vvc00001_Final_00001_.png")

    # Compatibility: the old rules must still reach everything the code pass did not claim.
    same("a coded run and a legacy set share a folder without interfering",
         "mix_11-00-00~vvb00001_Final_00001_.png", "mix_11-00-00~vvb00001_Final_00001_.mp4")
    same("  the legacy set beside it still groups by filename",
         "old_1 MAIN_00001.png", "old_3 REFINE_00001.png")
    checks.append(("  and the two are different groups",
                   g["mix_11-00-00~vvb00001_Final_00001_.png"] != g["old_1 MAIN_00001.png"], "ok"))
    # An ordinary tilde must not read as a code, or an existing library re-groups on a coincidence.
    checks.append(("a tilde in an ordinary name is not a run code",
                   comfy_meta.run_code_key("holiday~photo_00001.png") is None,
                   comfy_meta.run_code_key("holiday~photo_00001.png")))
    checks.append(("  so those two pair by the old filename rule, not the code pass",
                   g["holiday~photo_00001.png"] ==
                   hashlib.sha1(f"r1|j|{index_db._pair_key('holiday~photo_00001.png')}"
                                .encode("utf-8")).hexdigest()[:12],
                   g["holiday~photo_00001.png"]))

    checks.append(("same set_id in another root does NOT cross-merge",
                   g["w_09-13-32_Raw_00001_.png"] != g["other_Raw_00001_.png"],
                   (g["w_09-13-32_Raw_00001_.png"], g["other_Raw_00001_.png"])))
    checks.append(("legacy set and explicit set are distinct groups",
                   g["X_1 MAIN_00001.png"] != g["w_09-13-32_Raw_00001_.png"], "ok"))
    checks.append(("explicit group id uses the vvset hash",
                   g["w_09-13-32_Raw_00001_.png"] == _gid_vvset("r1", "S1"),
                   g["w_09-13-32_Raw_00001_.png"]))
    # pairs run before set_id, so a paired row is never stolen by an explicit set
    checks.append(("idempotent", True, "see below"))

    # stage ordering for the detail-view panes
    order = sorted(["Final", "Raw", "Upscale", "Detail", "Refine"], key=index_db._stage_rank)
    checks.append(("stages sort in pipeline order",
                   order == ["Raw", "Detail", "Refine", "Upscale", "Final"], order))
    legacy_order = sorted(["refine", "main", "det"], key=index_db._stage_rank)
    checks.append(("legacy roles still sort MAIN->DET->REFINE",
                   legacy_order == ["main", "det", "refine"], legacy_order))
    # A CUSTOM stage is the stage before 'raw' (the author, 2026-09-07): he types words like "First" for
    # the shot a run starts from, so the escape hatch names what comes BEFORE the vocabulary. This
    # asserts against 'raw', not merely "sorts first", because the whole point is the boundary.
    checks.append(("a custom stage sorts ahead of raw",
                   index_db._stage_rank("First") < index_db._stage_rank("raw"), "ok"))
    custom_order = sorted(["Final", "First", "Raw"], key=index_db._stage_rank)
    checks.append(("a custom stage fronts the pane order",
                   custom_order == ["First", "Raw", "Final"], custom_order))
    # A member with no stage at all is NOT an unnamed first stage -- it is a file that never said.
    checks.append(("a blank stage still sorts last",
                   index_db._stage_rank("") > index_db._stage_rank("final"), "ok"))

    # running twice must not change anything
    before = dict(g)
    index_db.recompute_groups(conn)
    after = {r[0]: r[1] for r in conn.execute("SELECT filename, group_id FROM images")}
    checks = [c if c[0] != "idempotent" else ("idempotent", before == after, "ok") for c in checks]

    checks += roundtrip_checks()

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

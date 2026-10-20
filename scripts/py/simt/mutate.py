"""Mutation witnesses for the SIMT PE's compute-unit blocks.

    python scripts/py/simt/mutate.py              # every mutation
    python scripts/py/simt/mutate.py icache_tag31 # one

Each entry breaks one defining line of one block and builds that block's unit
bench over the broken copy; the bench must FAIL. A bench that passes over a
broken block is not a bench (compute-unit.md s10).

A block that only exists wired into the core is witnessed through the machine:
its entry names a shader and the knobs, and runs kht_sys over the broken copy.
"""

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]

ICACHE = "src/kohakumpe/simt/kht_icache.v"
KCACHE = "src/kohakumpe/simt/kht_kcache.v"
SCORE = "src/kohakumpe/simt/kht_score.v"
LSU = "src/kohakumpe/simt/kht_lsu.v"
CORE = "src/kohakumpe/simt/kht_core.v"
UNIT = "src/kohakumpe/simt/kht_unit.v"


def shader(name, launch=1, *defines, dram="zero"):
    """A machine witness: run simt_<name>.s through kht_sys at these knobs."""
    return {"shader": name, "launch": launch, "defines": list(defines), "dram": dram}


#: (name, bench, source, the defining text, its broken form)
MUTATIONS = [
    (
        "icache_tag31",
        "kht_icache",
        ICACHE,
        "(tag_q[TAGW-1:0] == ra_q[31:IDX+5])",
        "(tag_q[TAGW-2:0] == ra_q[30:IDX+5])",
    ),
    (
        "icache_word_slot",
        "kht_icache",
        ICACHE,
        ".wr_addr({fill_addr[IDX+4:5], wcnt}),",
        ".wr_addr({fill_addr[IDX+4:5], wcnt + 3'd1}),",
    ),
    (
        "icache_valid_early",
        "kht_icache",
        ICACHE,
        "                    if (wcnt == 3'd7) begin\n                        tag_we",
        "                    if (wcnt == 3'd3) begin\n                        tag_we",
    ),
    (
        "icache_sweep_answers",
        "kht_icache",
        ICACHE,
        "assign rd_hit = (st != S_SWEEP) && tag_q[TW-1]",
        "assign rd_hit = tag_q[TW-1]",
    ),
    (
        "icache_inval_ignored",
        "kht_icache",
        ICACHE,
        "                S_IDLE: if (inval) begin",
        "                S_IDLE: if (1'b0) begin",
    ),
    (
        "kcache_tag31",
        "kht_kcache",
        KCACHE,
        "(tag_q[TAGW-1:0] == rd_addr[31:IDX+5])",
        "(tag_q[TAGW-2:0] == rd_addr[30:IDX+5])",
    ),
    (
        "kcache_word_select",
        "kht_kcache",
        KCACHE,
        "line_q[{rd_addr[4:2], 5'd0} +: 32]",
        "line_q[{rd_addr[4:3], 1'b0, 5'd0} +: 32]",
    ),
    (
        "kcache_sweep_answers",
        "kht_kcache",
        KCACHE,
        "assign rd_hit  = (st != S_SWEEP) && tag_q[TW-1]",
        "assign rd_hit  = tag_q[TW-1]",
    ),
    (
        "kcache_valid_on_request",
        "kht_kcache",
        KCACHE,
        "                    fill_valid <= 1'b1;\n                    st         <= S_REQ;",
        (
            "                    fill_valid <= 1'b1;\n"
            "                    tag_we <= 1'b1; tag_wa <= miss_addr[IDX+4:5];"
            " tag_wd <= {1'b1, miss_addr[31:IDX+5]};\n"
            "                    st         <= S_REQ;"
        ),
    ),
    (
        "score_rs2_unchecked",
        "kht_score",
        SCORE,
        "|| (chk_use[1] && (chk_rs2 != 5'd0) && row[chk_rs2])",
        "|| 1'b0",
    ),
    (
        "score_dest_unchecked",
        "kht_score",
        SCORE,
        "|| (chk_use[2] && (chk_rd  != 5'd0) && row[chk_rd]);",
        ";",
    ),
    (
        "score_clear_lost",
        "kht_score",
        SCORE,
        "pend[clr_wave][clr_rd] <= 1'b0;",
        "pend[clr_wave][clr_rd] <= pend[clr_wave][clr_rd];",
    ),
    (
        "score_wrong_wave",
        "kht_score",
        SCORE,
        "pend[set_wave][set_rd] <= 1'b1;",
        "pend[0][set_rd] <= 1'b1;",
    ),
    (
        "score_woke_any_wave",
        "kht_score",
        SCORE,
        "assign woke[g]  = clr_v && (clr_wave == g[WID-1:0]);",
        "assign woke[g]  = clr_v;",
    ),
    (
        "score_wpend_low_half",
        "kht_score",
        SCORE,
        "assign wpend[g] = |pend[g];",
        "assign wpend[g] = |pend[g][15:0];",
    ),
    (
        "lsu_read_past_write",
        "kht_lsu",
        LSU,
        "wire rd_block = (wr_v && (wr_line == fb_line[fb_req[FBW-1:0]]))",
        "wire rd_block = 1'b0",
    ),
    (
        "lsu_merge_lane_order",
        "kht_lsu",
        LSU,
        "for (mi = 0; mi < LANES; mi = mi + 1) begin",
        "for (mi = LANES - 1; mi >= 0; mi = mi - 1) begin",
    ),
    (
        "lsu_no_coalesce",
        "kht_lsu",
        LSU,
        "? (h_addr[32*si + 5 +: 26] == lead_line)",
        "? (si == lead)",
    ),
    (
        "lsu_half_unsigned",
        "kht_lsu",
        LSU,
        "(sx ? {{16{hw[15]}}, hw} : {16'd0, hw})",
        "{16'd0, hw}",
    ),
    (
        "lsu_half_low_bytes",
        "kht_lsu",
        LSU,
        "(a[1] ? 4'b1100 : 4'b0011)",
        "4'b0011",
    ),
    (
        "lsu_store_hit_clean",
        "kht_lsu",
        LSU,
        ": {1'b1, (WB != 0), cur_line[25:IDX]};",
        ": {1'b1, 1'b0, cur_line[25:IDX]};",
    ),
    (
        "lsu_victim_dropped",
        "kht_lsu",
        LSU,
        "wire vic_dirty = tag_s[TW-1] && tag_s[TW-2];",
        "wire vic_dirty = 1'b0;",
    ),
    (
        "lsu_inval_keeps",
        "kht_lsu",
        LSU,
        "(fl_inval ? {TW{1'b0}} : {2'b10, tag_s[TAGW-1:0]})",
        "{2'b10, tag_s[TAGW-1:0]}",
    ),
    (
        "lsu_scan_duplicates",
        "kht_lsu",
        LSU,
        "wire         sc_need = sc_on && !sc_inl1 && !sc_fb[FBW];",
        "wire         sc_need = sc_on && !sc_inl1;",
    ),
    (
        "lsu_rsp_one_buffer",
        "kht_lsu",
        LSU,
        "fb_data[rsp_i[FBW-1:0]] <= rsp_data;",
        "fb_data[0] <= rsp_data;",
    ),
    (
        "lsu_serve_installing",
        "kht_lsu",
        LSU,
        "&& !cfb_busy) begin",
        ") begin",
    ),
    (
        "lsu_flush_before_fills",
        "kht_lsu",
        LSU,
        "&& (hst == H_IDLE)\n               && !fb_any;",
        "&& (hst == H_IDLE);",
    ),
    (
        "core_replay_wrong_pc",
        shader("f32", 1, "KHT_SCORE=1"),
        CORE,
        "assign redir_pc = rp_hit    ? f2_pc",
        "assign redir_pc = 1'b0      ? f2_pc",
    ),
    (
        "core_float_not_scored",
        shader("f32", 1, "KHT_SCORE=1"),
        CORE,
        ".set_v(go && (is_flt || is_imul || ld_set))",
        ".set_v(go && (is_imul || ld_set))",
    ),
    (
        "unit_wbq_clear_on_entry",
        shader("f32", 1, "KHT_SCORE=1", "KHT_WBBUF=1"),
        UNIT,
        "assign lr_clr_v    = q_pop && q_head[WID];",
        "assign lr_clr_v    = fwb_v && fwb_last;",
    ),
    (
        "core_load_not_scored",
        shader("gather", 1, "KHT_SCORE=1", "KHT_LSUD=4"),
        CORE,
        "wire ld_set = lq_sent && ictl[C_LDANY] && (rd != 5'd0);",
        "wire ld_set = 1'b0;",
    ),
    (
        "unit_load_steals_port",
        shader("ldmix", 16, "KHT_SCORE=1", "KHT_LSUD=4", dram="ramp"),
        UNIT,
        "assign ld_take     = ld_v && !lr_v && !pw_v;",
        "assign ld_take     = ld_v && !lr_v;",
    ),
    (
        "core_queue_fault_lost",
        shader("fault", 1, "KHT_LSUD=4"),
        CORE,
        "bad_q <= bad_q || lq_bad || (",
        "bad_q <= bad_q || (",
    ),
    (
        "core_misalign_accepted",
        shader("misalign", 1, "KHT_LSUD=4"),
        CORE,
        "(!mask[DG] || (a[31] && al))",
        "(!mask[DG] || a[31])",
    ),
    (
        "core_mixed_accepted",
        shader("mixed", 1, "KHT_LSUD=4"),
        CORE,
        "&& all_dram_q && lq_ready;",
        "&& lq_ready;",
    ),
    (
        "core_warm_during_pass",
        shader("f32", 16, "KHT_FLANES=2", "KHT_SCORE=1", "KHT_LSUD=4"),
        CORE,
        "else if (f2_valid && !vt_stall && !fpass_hold && !base_hold",
        "else if (f2_valid && !vt_stall",
    ),
    (
        "core_warm_under_hold",
        shader(
            "f32",
            16,
            "KHT_FLANES=4",
            "KHT_LSUD=1",
            "KHT_COAL=0",
            "KHT_MSHR=1",
            "KHT_SCORE=1",
        ),
        CORE,
        "else if (go || rp_hit) begin",
        "else if (go || (rp_hit && !hold_rp)) begin",
    ),
    (
        "core_queue_sleep_forever",
        shader("f32", 16, "KHT_SCORE=1", "KHT_LSUD=1"),
        CORE,
        "& ~{WAVES{lq_pop}};",
        ";",
    ),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    args = ap.parse_args()

    todo = [m for m in MUTATIONS if not args.names or m[0] in args.names]
    bad = []
    for name, bench, src, old, new in todo:
        text = (ROOT / src).read_text(encoding="utf-8")
        if text.count(old) != 1:
            print(
                f"{name:24s} STALE   the defining text occurs {text.count(old)} times in {src}"
            )
            bad.append(name)
            continue

        out = ROOT / "build" / "simt" / "mut" / name / pathlib.Path(src).name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text.replace(old, new), encoding="utf-8")

        how = (
            ["--incdir", str(out.parent)]
            if src.endswith(".vh")
            # The copy lives elsewhere, so its `include`s need the original's directory.
            else [
                "--swap",
                f"{src}={out.as_posix()}",
                "--incdir",
                str(ROOT / pathlib.Path(src).parent),
            ]
        )
        root = ["--build-root", str(ROOT / "build" / "simt" / "mut" / name)]
        if isinstance(bench, dict):
            cmd = [
                str(ROOT / "tests/pe/tools/rv_simt_run.py"),
                str(ROOT / "tests/pe/prog" / f"simt_{bench['shader']}.s"),
                "--arg",
                "0x80000000",
                "--dram",
                bench["dram"],
                "--launch",
                str(bench["launch"]),
            ]
            for d in bench["defines"]:
                cmd += ["-d", d]
            label = "kht_sys:" + bench["shader"]
        else:
            cmd = [str(ROOT / "scripts/py/xsim.py"), bench]
            label = bench
        r = subprocess.run(
            [sys.executable, *cmd, *how, *root],
            capture_output=True,
            text=True,
            check=False,
        )
        verdict = [
            ln.strip()
            for ln in r.stdout.splitlines()
            if ln.strip().startswith(("PASS", "FAIL"))
        ]
        caught = r.returncode != 0 and any(v.startswith("FAIL") for v in verdict)
        print(
            f"{name:26s} {label:18s} {'caught' if caught else 'MISSED'}  "
            f"{verdict[-1] if verdict else '(no verdict)'}",
            flush=True,
        )
        if not caught:
            bad.append(name)

    if bad:
        sys.exit(f"missed: {bad}")
    print(f"all {len(todo)} mutations caught")


if __name__ == "__main__":
    main()

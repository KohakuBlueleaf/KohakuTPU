"""Mutation witnesses for the SRU unit benches: each mutation breaks one path
of one module, and its bench must FAIL on it.

    python scripts/py/sru/mutate.py            # every mutation
    python scripts/py/sru/mutate.py plane_tie  # by name

A mutated copy lands in build/sru/mut/<name>/ and the bench is built over it
with `xsim.py --swap`; the source tree is never touched.
"""

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
RED = "src/kohakutpu/sru/stream/sru_red.v"
MX7 = "src/kohakutpu/sru/stream/sru_mx7.v"
CORE = "src/kohakutpu/sru/sru_core.v"
PLANE = "src/kohakutpu/sru/lane/sru_plane.v"
POOL = "src/kohakutpu/sru/pool/sru_pool.v"

# name, bench, source, text, replacement
MUTATIONS = [
    (
        "plane_edge",
        "sru_plane",
        PLANE,
        "wire spec3 = ~pz3 & ~zc3 & esub3 & mc0_3 & (s3 == -11'sd1);",
        "wire spec3 = 1'b0;",
    ),
    ("plane_borrow", "sru_plane", PLANE, "{32'd0, neg10 & ~stk10}", "{32'd0, neg10}"),
    (
        "plane_zero_sign",
        "sru_plane",
        PLANE,
        "zs2   <= sp1 & sc1;",
        "zs2   <= sp1 | sc1;",
    ),
    (
        "plane_underflow",
        "sru_plane",
        PLANE,
        "end else if (ef15 <= 13'sd0) begin",
        "end else if (ef15 < 13'sd0) begin",
    ),
    (
        "plane_kfloor",
        "sru_plane",
        PLANE,
        "-$signed({1'b0, kip1} + {9'd0, kfr1})",
        "-$signed({1'b0, kip1})",
    ),
    (
        "plane_ksign",
        "sru_plane",
        PLANE,
        "-$signed({kf2[9], kf2})",
        "-$signed({1'b0, kf2})",
    ),
    (
        "plane_range",
        "sru_plane",
        PLANE,
        "wire        in4 = (w4[34:28] == {7{w4[28]}});",
        "wire        in4 = (w4[34:27] == {8{w4[27]}});",
    ),
    (
        "plane_cs",
        "sru_plane",
        PLANE,
        "{1'b0, {2'd0, t7} << cs7}",
        "{1'b0, {2'd0, t7}}",
    ),
    (
        "plane_op_flags",
        "sru_plane",
        PLANE,
        "fl[8] <= op7 ? gf7 : ff7;",
        "fl[8] <= ff7;",
    ),
    (
        "plane_wait",
        "sru_plane",
        PLANE,
        "wire [15:0]   ga7  = dl[7][DW-1    -: 16];",
        "wire [15:0]   ga7  = dl[6][DW-1    -: 16];",
    ),
    (
        "plane_c0_shift",
        "sru_plane",
        PLANE,
        "sh8   <= op7 ? 6'd18 : sh7;",
        "sh8   <= op7 ? 6'd17 : sh7;",
    ),
    ("plane_fn_exp", "sru_plane", PLANE, "12'sd98 +", "12'sd97 +"),
    ("plane_zero_seg", "sru_plane", PLANE, "(es7 == -6'sd32)", "1'b0"),
    ("plane_w_carry", "sru_plane", PLANE, " + {34'd0, sx3};", ";"),
    (
        "plane_tie",
        "sru_plane",
        PLANE,
        "wire        up14 = g14 & (st14 | sig14[0]);",
        "wire        up14 = g14 & st14;",
    ),
    (
        "plane_align_sticky",
        "sru_plane",
        PLANE,
        "wire        stk8 = (sh8 > 6'd31) & (|(gc8 & lm8)) & ~zcd8;",
        "wire        stk8 = 1'b0;",
    ),
    (
        "plane_rom_index",
        "sru_plane_rom",
        PLANE,
        "{3'd0, w4[23:17]}",
        "{3'd0, w4[22:16]}",
    ),
    (
        "plane_mant_half",
        "sru_plane",
        PLANE,
        "half1 ? {mrr1, a1[14:6]} : a1[14:5];",
        "half1 ? {1'b0, a1[14:6]} : a1[14:5];",
    ),
    ("plane_mant_neg", "sru_plane", PLANE, "mn2  <= neg1 ? -mqx1 : mqx1;", "mn2  <= mqx1;"),
    (
        "plane_mant_q",
        "sru_plane",
        PLANE,
        "wire signed [8:0]  mq1  = half1 ? (me1 >>> 1) : me1;",
        "wire signed [8:0]  mq1  = half1 ? (me1 >> 1) : me1;",
    ),
    ("plane_mant_zero", "sru_plane", PLANE, "mz1 ? neg1 : (mi1 & ~neg1),", "mz1 ? 1'b0 : (mi1 & ~neg1),"),
    ("plane_mant_flip", "sru_plane", PLANE, "~half1 & a1[23], a1[23]};", "1'b0, a1[23]};"),
    (
        "plane_mant_negx",
        "sru_plane",
        PLANE,
        "mf2  <= {mn1 | (half1 & a1[23] & ~mz1),",
        "mf2  <= {mn1,",
    ),
    ("plane_mrom_sel", "sru_plane", PLANE, "assign q7 = mr7 ? mq7 : eq7;", "assign q7 = eq7;"),
    (
        "plane_mrom_t",
        "sru_plane_nofn",
        PLANE,
        "mt2_ <= mr1   ? {a1[7:0], 6'd0}",
        "mt2_ <= mr1   ? {a1[6:0], 7'd0}",
    ),
    (
        "plane_mrom_index",
        "sru_plane_rom",
        PLANE,
        "ma2  <= mr1   ? {1'b0, half1, mrr1, a1[14:8]}",
        "ma2  <= mr1   ? {1'b0, half1, 1'b0, a1[14:8]}",
    ),
    ("red_max_first", "sru_red", RED, "if (v[2] && f[2]) begin", "if (1'b0) begin"),
    (
        "red_max_key",
        "sru_red",
        RED,
        "key = w[EW-1] ? {1'b0, ~w[EW-2:0]} : {1'b1, w[EW-2:0]};",
        "key = w[EW-1] ? {1'b0, w[EW-2:0]} : {1'b1, w[EW-2:0]};",
    ),
    (
        "red_sum_logical_shift",
        "sru_red_sum",
        RED,
        "$signed({48{A[47]}})",
        "{48{A[47]}}",
    ),
    ("red_sum_neg_carry", "sru_red_sum", RED, " + {47'd0, c4};", ";"),
    (
        "core_r2_parity",
        "sru_core_strong",
        CORE,
        "st2_rdy[r2_par] <= 1'b1;",
        "st2_rdy[1'b0] <= 1'b1;",
    ),
    (
        "core_mask_cmp",
        "sru_core_strong",
        CORE,
        "assign md[gk] = (dw3 > (",
        "assign md[gk] = (dw3 >= (",
    ),
    (
        "core_mask_lane",
        "sru_core_strong",
        CORE,
        "md[(q % 4) - (q / 4) + 3]",
        "md[(q / 4) - (q % 4) + 3]",
    ),
    (
        "core_load_timing",
        "sru_core_strong",
        CORE,
        "if (par) W1[ld - 3'd3] <= rs_q; else W0[ld - 3'd3] <= rs_q;",
        "if (par) W1[ld - 3'd2] <= rs_q; else W0[ld - 3'd2] <= rs_q;",
    ),
    (
        "pool_cc_addr",
        "sru_core_strong",
        POOL,
        "assign cc_raddr = col0 + mt2[12:4];",
        "assign cc_raddr = col0;",
    ),
    (
        "core_fin_dst",
        "sru_core_strong",
        CORE,
        "2'd1: if (par) W1[fw[1:0]][47:24] <= fo; else W0[fw[1:0]][47:24] <= fo;",
        "2'd1: if (par) W1[fw[1:0]][23:0] <= fo; else W0[fw[1:0]][23:0] <= fo;",
    ),
    (
        "pool_word_slot",
        "sru_core_strong",
        POOL,
        "tw1 <= cur_w - `SRU_O_KW(ow0);",
        "tw1 <= cur_w - `SRU_O_KW(ow0) + 5'd1;",
    ),
    (
        "pool_x_depth",
        "sru_core_strong",
        POOL,
        "wire [4:0] xa3  = xcnt - 5'd1 - tw3;",
        "wire [4:0] xa3  = xcnt - tw3;",
    ),
    ("pool_h_tap", "sru_core_strong", POOL, "h1r <= h1[d1_3];", "h1r <= h1[d0_3];"),
    (
        "pool_partner",
        "sru_core_strong",
        POOL,
        "2'd2:    av = xr4[gl ^ 1];",
        "2'd2:    av = xr4[gl];",
    ),
    (
        "pool_row_chunk",
        "sru_core_mid",
        POOL,
        "rbr <= rb3[(gl + F*e3) / 4];",
        "rbr <= rb3[gl / 4];",
    ),
    (
        "pool_out_chunk",
        "sru_core_mid",
        POOL,
        "out_data[(q + F*oe20)*24 +: 24] <= lo[q];",
        "out_data[q*24 +: 24] <= lo[q];",
    ),
    (
        "fin_mul_zero",
        "sru_core_strong",
        CORE,
        "(fa_op == F_MUL) ? {fa_a[23] ^ fa_b[23], 23'd0}",
        "(fa_op == F_MUL) ? 24'd0",
    ),
    ("fin_sub", "sru_core_strong", CORE, "{~fa_c[23], fa_c[22:0]} : fa_c;", "fa_c : fa_c;"),
    ("fin_min", "sru_core_strong", CORE, "(ka >= kb) : (ka <= kb)", "(ka >= kb) : (ka >= kb)"),
    (
        "fin_zero",
        "sru_core_strong",
        CORE,
        "sel_o <= (sel_w[22:15] == 8'd0) ? 24'd0 : sel_w;",
        "sel_o <= sel_w;",
    ),
    ("fin_rcp_route", "sru_core_strong", CORE, "assign fin_dfn   = f_rcp;", "assign fin_dfn   = 1'b0;"),
    (
        "fin_rsq_half",
        "sru_core_strong",
        CORE,
        "assign fin_dhalf = (fa_op == F_RSQ);",
        "assign fin_dhalf = 1'b0;",
    ),
    (
        "pool_mant_bit",
        "sru_core_strong",
        POOL,
        "{`SRU_O_MANT(ow4), `SRU_O_FIX(ow4)}",
        "{1'b0, `SRU_O_FIX(ow4)}",
    ),
    ("pool_neg_bit", "sru_core_strong", POOL, ".neg(dv | `SRU_O_NEG(ow4))", ".neg(dv)"),
    (
        "e8f16_round",
        "sru_e8f16",
        "src/kohakutpu/sru/stream/sru_e8f16_acc.v",
        "wire        up = m[4] & ((|m[3:0]) | m[5]);",
        "wire        up = m[4] & (|m[3:0]);",
    ),
    (
        "e8f16_flush",
        "sru_e8f16",
        "src/kohakutpu/sru/stream/sru_e8f16_acc.v",
        "(ef <= 9'd112)",
        "(ef < 9'd112)",
    ),
    (
        "e8f16_sat",
        "sru_e8f16",
        "src/kohakutpu/sru/stream/sru_e8f16_acc.v",
        "(ef >= 9'd143)",
        "(ef > 9'd143)",
    ),
    (
        "unpack_acc_zero",
        "sru_unpack_acc",
        "src/kohakutpu/sru/stream/sru_f16e8.vh",
        "f16e8_acc = (h[14:10] == 5'd0) ? {h[15], 23'd0}",
        "f16e8_acc = (h[14:10] == 5'd0) ? {1'b0, 23'd0}",
    ),
    (
        "core_occ",
        "sru_cu",
        CORE,
        "occ <= occ + (push ? 1'b1 : 1'b0) - (pop ? 1'b1 : 1'b0);",
        "occ <= occ + (push ? 1'b1 : 1'b0);",
    ),
    (
        "core_window",
        "sru_core_strong",
        CORE,
        "wire        slot    = !use_pl || pl_want;",
        "wire        slot    = 1'b1;",
    ),
    (
        "cu_credit_spend",
        "sru_cu",
        "src/kohakutpu/sru/sru_cu.v",
        "- ((oe_hdr && j_peer) ? {12'd0, oe_blen} : 16'd0);",
        ";",
    ),
    (
        "cu_walk_end",
        "sru_cu",
        "src/kohakutpu/sru/sru_cu.v",
        "wire        wa_end = (wa_left <= a_rm) && (wa_left <= 12'd32);",
        "wire        wa_end = (wa_left < a_rm) && (wa_left <= 12'd32);",
    ),
    (
        "cu_rs_load",
        "sru_cu",
        "src/kohakutpu/sru/sru_cu.v",
        "rs_wdata <= {rpay[95:72], rpay[63:40], rpay[31:8]};",
        "rs_wdata <= {rpay[63:40], rpay[95:72], rpay[31:8]};",
    ),
    (
        "cu_b_class",
        "sru_cu",
        "src/kohakutpu/sru/sru_cu.v",
        "2'd1: begin bf_we <= 1'b1; bf_wd <= rpay; end",
        "2'd1: begin af_we <= 1'b1; af_wd <= rpay; end",
    ),
    (
        "cu_out_stride",
        "sru_cu",
        "src/kohakutpu/sru/sru_cu.v",
        "oe_ptr  <= oe_row + {19'd0, j_ostr, 5'd0};",
        "oe_ptr  <= oe_row + {23'd0, oe_nw, 5'd0};",
    ),
    (
        "cu_run_min",
        "sru_cu",
        "src/kohakutpu/sru/sru_cu.v",
        "wire        wa_go  = wa_on && (wa_left != 12'd0) && (a_rm >= a_need);",
        "wire        wa_go  = wa_on && (wa_left != 12'd0) && (a_rm != 12'd0);",
    ),
    (
        "cu_need_reg",
        "sru_cu",
        "src/kohakutpu/sru/sru_cu.v",
        "a_need  <= need(wa_left - wa_run);",
        "a_need  <= 12'd1;",
    ),
    (
        "cu_stream_credit",
        "sru_cu",
        "src/kohakutpu/sru/sru_cu.v",
        "post_amt <= (cr_left < 24'd8) ? {8'd0, cr_left[7:0]} : 16'd8;",
        "post_amt <= 16'd8;",
    ),
    (
        "cu_stream_stop",
        "sru_cu",
        "src/kohakutpu/sru/sru_cu.v",
        "post_valid <= (cr_left != 24'd0);",
        "post_valid <= 1'b1;",
    ),
    (
        "unpack_subnormal",
        "sru_unpack",
        "src/kohakutpu/sru/stream/sru_f16e8.vh",
        "{4'd0, lp} + 8'd103",
        "{4'd0, lp} + 8'd104",
    ),
    (
        "mx7_round",
        "sru_mx7",
        MX7,
        "sm = {1'b0, x8[7:1]} + {7'd0, x8[0]};",
        "sm = {1'b0, x8[7:1]};",
    ),
    (
        "mx7_subnormal",
        "sru_mx7",
        MX7,
        "qs[s] <= (hh[14:10] == 5'd0) ? {1'b0, hh[9:0]} : {1'b1, hh[9:0]};",
        "qs[s] <= {1'b1, hh[9:0]};",
    ),
    (
        "mx7_peak_first",
        "sru_mx7",
        MX7,
        "if (st1 || (wp1[r] > pk[b1][r])) begin",
        "if (wp1[r] > pk[b1][r]) begin",
    ),
    ("mx7_scale_clamp", "sru_mx7", MX7, "if (sexp < -8'sd20) begin", "if (1'b0) begin"),
    (
        "mx7_b_layout",
        "sru_mx7_b",
        MX7,
        "acc[255 - ((s%4)*4 + 16 + s/4)*7 -: 7]",
        "acc[255 - ((s%4)*4 + 16 + (3 - s/4))*7 -: 7]",
    ),
    (
        "red_sum_grow",
        "sru_red_sum",
        RED,
        "gr2 <= f[0] ? 8'd0 : (emn - EM);",
        "gr2 <= 8'd0;",
    ),
    (
        "red_sum_inf_pair",
        "sru_red_sum",
        RED,
        ": (sp4 == 2'd0 || sp4 == SP) ? SP : 2'd1;",
        ": (sp4 == 2'd0 || sp4 == SP) ? SP : sp4;",
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
            # a stale entry is a failure of the witness set, not a stop
            print(f"{name:20s} STALE   '{old}' occurs {text.count(old)} times in {src}", flush=True)
            bad.append(name)
            continue

        out = ROOT / "build" / "sru" / "mut" / name / pathlib.Path(src).name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text.replace(old, new), encoding="utf-8")

        # A header is found through the include path, a module by the file list.
        how = (
            ["--incdir", str(out.parent)]
            if src.endswith(".vh")
            else ["--swap", f"{src}={out.as_posix()}"]
        )
        r = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/py/xsim.py"),
                bench,
                *how,
                "--build-root",
                str(ROOT / "build" / "sru" / "mut" / name),
            ],
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
            f"{name:20s} {bench:12s} {'caught' if caught else 'MISSED'}  "
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

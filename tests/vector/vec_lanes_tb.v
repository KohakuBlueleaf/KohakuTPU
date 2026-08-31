// The lane array: all four VMODE topologies, predication, and the reductions.
//
// Every operand is a small integer, and every integer below 2^16 is EXACT in
// E8M15's 16-bit significand, so the expected values are equalities rather than
// tolerances -- a rounding difference here would be a wiring bug, not arithmetic.
//
// What each mode has to prove is different:
//   FLAT  all 16 slices advance independently and land on their own slot
//   D2/D4 the chain carries C between stages, and only W slots retire per beat
//   TREE  8+4+2+1 reduces, the 16 rotating accumulators break the recurrence,
//         and the tail pass combines them without re-entering the accumulator

`default_nettype none
`timescale 1ns/1ps

// -d TB_RFPAD=36 / -d TB_RFPACK=2 run the same checks over the register file's
// padded and packed shapes (vec_regfile.v).
`ifndef TB_RFPAD
`define TB_RFPAD 24
`endif
`ifndef TB_RFPACK
`define TB_RFPACK 1
`endif

module vec_lanes_tb;
    localparam [1:0] M_FLAT = 2'd0, M_D2 = 2'd1, M_D4 = 2'd2, M_TREE = 2'd3;
    localparam [2:0] R_EXPSUM = 3'd5;
    localparam [4:0] OP_MOV = 5'd0, OP_ADD = 5'd3, OP_SUB = 5'd4, OP_MUL = 5'd5;
    localparam [4:0] OP_FMA = 5'd6, OP_MAX = 5'd8, OP_MIN = 5'd9;
    localparam [4:0] OP_CMPLT = 5'd11;
    localparam [1:0] SRC_V = 2'd0, SRC_S = 2'd1, SRC_C = 2'd2, SRC_K = 2'd3;

    reg clk = 0, rst = 1;
    always begin
        #2 clk = ~clk;
    end

    reg  [1:0]   mode;
    reg          ls_we, ls_ractive;
    reg  [6:0]   ls_waddr, ls_raddr;
    reg  [383:0] ls_wdata;
    wire [383:0] ls_rdata;

    reg          iss_valid, iss_is_cmp, iss_tail;
    reg  [15:0]  iss_tmask;
    reg  [1:0]   iss_phase, iss_pm, iss_pr;
    reg  [6:0]   iss_ra, iss_rb, iss_rc, iss_wa;
    reg  [2:0]   iss_chunk;
    reg  [19:0]  st_op;
    reg  [7:0]   st_sa, st_sb, st_sc;
    reg  [95:0]  st_ka, st_kb, st_kc;
    reg          red_init;
    reg  [2:0]   red_kind;
    reg  [1:0]   p_rd_sel;
    wire [127:0] p_rd_bits;
    wire [23:0]  red_result;
    wire         red_valid, pipe_empty;

    vec_lanes #(.MODEL(1), .RF_PAD(`TB_RFPAD), .RF_PACK(`TB_RFPACK)) dut (
        .clk(clk), .rst(rst), .mode(mode),
        .ls_we(ls_we), .ls_waddr(ls_waddr), .ls_wdata(ls_wdata),
        .ls_raddr(ls_raddr), .ls_ractive(ls_ractive), .ls_rdata(ls_rdata),
        .iss_valid(iss_valid), .iss_phase(iss_phase),
        .iss_ra(iss_ra), .iss_rb(iss_rb), .iss_rc(iss_rc), .iss_wa(iss_wa),
        .iss_pm(iss_pm), .iss_pr(iss_pr), .iss_chunk(iss_chunk),
        .iss_is_cmp(iss_is_cmp), .iss_tail(iss_tail), .iss_tmask(iss_tmask),
        .st_op(st_op), .st_sa(st_sa), .st_sb(st_sb), .st_sc(st_sc),
        .st_ka(st_ka), .st_kb(st_kb), .st_kc(st_kc),
        .red_init(red_init), .red_kind(red_kind),
        .p_rd_sel(p_rd_sel), .p_rd_bits(p_rd_bits),
        .red_result(red_result), .red_valid(red_valid), .pipe_empty(pipe_empty)
    );

    integer errors = 0, checks = 0;

    // E8M15 for a small signed integer. Exact for |v| < 2^16.
    function [23:0] e8i(input integer v);
        integer av, t, ex;
        reg        sgn;
        reg [31:0] sh;
        begin
            if (v == 0) begin
                e8i = 24'd0;
            end
            else begin
                sgn = (v < 0); av = sgn ? -v : v;
                t = av; ex = 0;
                while (t > 1) begin t = t >> 1; ex = ex + 1; end
                // sh must be sliced to 15 bits HERE: an unsized expression in
                // the concatenation makes it 32 bits wide and the sign and
                // exponent fall off the top of the 24-bit result.
                // Past 2^16 the significand no longer fits and the shift
                // REVERSES; `15 - ex` would go negative, which Verilog does
                // not define, and the reference would silently return zero.
                if (ex <= 15) begin
                    sh = av << (15 - ex);
                end
                else begin
                    sh = av >> (ex - 15);
                end
                e8i = {sgn, (8'd127 + ex[7:0]), sh[14:0]};
            end
        end
    endfunction

    task chk(input [23:0] got, input [23:0] want, input [255:0] what,
             input integer where);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                if (errors < 20) begin
                    $display("  FAIL %0s [%0d]: got %06h want %06h",
                             what, where, got, want);
                end
            end
        end
    endtask

    task wr_chunk(input [3:0] vr, input [2:0] ch, input [383:0] d);
        begin
            @(negedge clk);
            ls_we = 1'b1; ls_waddr = {vr, ch}; ls_wdata = d;
            @(negedge clk);
            ls_we = 1'b0;
        end
    endtask

    task rd_chunk(input [3:0] vr, input [2:0] ch);
        begin
            @(negedge clk);
            ls_ractive = 1'b1; ls_raddr = {vr, ch};
            @(negedge clk);
            @(negedge clk);
        end
    endtask

    task set_stage(input integer t, input [4:0] op,
                   input [1:0] sa, input [1:0] sb, input [1:0] sc,
                   input [23:0] ka, input [23:0] kb, input [23:0] kc);
        begin
            st_op[t*5 +: 5] = op;
            st_sa[t*2 +: 2] = sa; st_sb[t*2 +: 2] = sb; st_sc[t*2 +: 2] = sc;
            st_ka[t*24 +: 24] = ka; st_kb[t*24 +: 24] = kb;
            st_kc[t*24 +: 24] = kc;
        end
    endtask

    task beat(input [2:0] ch, input [1:0 ] ph,
              input [3:0] ra, input [3:0] rb, input [3:0] rc, input [3:0] wa,
              input [1:0] pm, input [1:0] pr, input is_cmp, input tail);
        begin
            @(negedge clk);
            iss_valid = 1'b1; iss_chunk = ch; iss_phase = ph;
            iss_ra = {ra, ch}; iss_rb = {rb, ch}; iss_rc = {rc, ch};
            iss_wa = {wa, ch};
            iss_pm = pm; iss_pr = pr; iss_is_cmp = is_cmp; iss_tail = tail;
            @(negedge clk);
            iss_valid = 1'b0; iss_tail = 1'b0;
        end
    endtask

    // Bounded, so a pipeline that never reports empty is a fast diagnosis
    // rather than a run that has to be killed from outside.
    integer spin;
    task drain;
        begin
            @(negedge clk);
            spin = 0;
            while (!pipe_empty && spin < 4000) begin
                spin = spin + 1;
                @(negedge clk);
            end
            if (spin >= 4000) begin
                errors = errors + 1;
                $display("  FAIL pipe_empty never asserted (mode=%0d)", mode);
            end
            repeat (4) @(negedge clk);
        end
    endtask

    integer i, c, ph, el;
    reg [383:0] buf0, buf1;
    reg [23:0]  want;

    // element value helpers, so expectation and stimulus cannot drift apart
    function integer va_of(input integer idx); begin va_of = idx + 1; end endfunction
    function integer vb_of(input integer idx); begin vb_of = 2*(idx + 1); end endfunction
    function integer vs_of(input integer idx); begin vs_of = (idx % 8) + 1; end endfunction

    initial begin
        mode = M_FLAT; ls_we = 0; ls_ractive = 0; ls_waddr = 0; ls_raddr = 0;
        ls_wdata = 0; iss_valid = 0; iss_phase = 0; iss_pm = 0; iss_pr = 0;
        iss_ra = 0; iss_rb = 0; iss_rc = 0; iss_wa = 0; iss_chunk = 0;
        iss_is_cmp = 0; iss_tail = 0; iss_tmask = 16'hFFFF;
        st_op = 0; st_sa = 0; st_sb = 0; st_sc = 0;
        st_ka = 0; st_kb = 0; st_kc = 0; red_init = 0; red_kind = 0; p_rd_sel = 0;

        repeat (8) @(negedge clk);
        rst = 0;
        repeat (4) @(negedge clk);

        // ---- load v0 = i+1, v1 = 2(i+1), v3 = (i mod 8)+1 ----
        for (c = 0; c < 8; c = c + 1) begin
            buf0 = 0; buf1 = 0;
            for (i = 0; i < 16; i = i + 1) begin
                el = {c[2:0], i[3:0]};
                buf0[i*24 +: 24] = e8i(va_of(el));
                buf1[i*24 +: 24] = e8i(vb_of(el));
            end
            wr_chunk(4'd0, c[2:0], buf0);
            wr_chunk(4'd1, c[2:0], buf1);
            buf0 = 0;
            for (i = 0; i < 16; i = i + 1) begin
                el = {c[2:0], i[3:0]};
                buf0[i*24 +: 24] = e8i(vs_of(el));
            end
            wr_chunk(4'd3, c[2:0], buf0);
        end

        // read-back is the only thing every later section depends on
        $display("--- 0. register file load / read back ---");
        for (c = 0; c < 8; c = c + 1) begin
            rd_chunk(4'd0, c[2:0]);
            for (i = 0; i < 16; i = i + 1) begin
                el = {c[2:0], i[3:0]};
                chk(ls_rdata[i*24 +: 24], e8i(va_of(el)), "v0 readback", el);
            end
        end
        ls_ractive = 0;

        // ================================================= 1. FLAT
        $display("--- 1. FLAT: add, mul, fma ---");
        mode = M_FLAT;
        set_stage(0, OP_ADD, SRC_V, SRC_K, SRC_V, 0, 0, 0);
        for (c = 0; c < 8; c = c + 1) begin
            beat(c[2:0], 2'd0, 4'd0, 4'd1, 4'd1, 4'd2, 2'd0, 2'd0, 1'b0, 1'b0);
        end
        drain;
        for (c = 0; c < 8; c = c + 1) begin
            rd_chunk(4'd2, c[2:0]);
            for (i = 0; i < 16; i = i + 1) begin
                el = {c[2:0], i[3:0]};
                chk(ls_rdata[i*24 +: 24], e8i(va_of(el) + vb_of(el)),
                    "FLAT add", el);
            end
        end
        ls_ractive = 0;

        set_stage(0, OP_MUL, SRC_V, SRC_V, SRC_K, 0, 0, 0);
        for (c = 0; c < 8; c = c + 1) begin
            beat(c[2:0], 2'd0, 4'd0, 4'd1, 4'd1, 4'd2, 2'd0, 2'd0, 1'b0, 1'b0);
        end
        drain;
        for (c = 0; c < 8; c = c + 1) begin
            rd_chunk(4'd2, c[2:0]);
            for (i = 0; i < 16; i = i + 1) begin
                el = {c[2:0], i[3:0]};
                chk(ls_rdata[i*24 +: 24], e8i(va_of(el) * vb_of(el)),
                    "FLAT mul", el);
            end
        end
        ls_ractive = 0;

        // a*b + c with c from a K constant, which is the shape every chained
        // stage past 0 uses
        set_stage(0, OP_FMA, SRC_V, SRC_V, SRC_K, 0, 0, e8i(5));
        for (c = 0; c < 8; c = c + 1) begin
            beat(c[2:0], 2'd0, 4'd0, 4'd1, 4'd1, 4'd2, 2'd0, 2'd0, 1'b0, 1'b0);
        end
        drain;
        for (c = 0; c < 8; c = c + 1) begin
            rd_chunk(4'd2, c[2:0]);
            for (i = 0; i < 16; i = i + 1) begin
                el = {c[2:0], i[3:0]};
                chk(ls_rdata[i*24 +: 24], e8i(va_of(el) * vb_of(el) + 5),
                    "FLAT fma", el);
            end
        end
        ls_ractive = 0;

        // ================================================= 2. predication
        $display("--- 2. FLAT: compare then predicated write ---");
        set_stage(0, OP_CMPLT, SRC_V, SRC_K, SRC_K, 0, e8i(64), 0);
        for (c = 0; c < 8; c = c + 1) begin
            beat(c[2:0], 2'd0, 4'd0, 4'd0, 4'd0, 4'd0, 2'd0, 2'd1, 1'b1, 1'b0);
        end
        drain;
        p_rd_sel = 2'd1;
        #1;
        for (i = 0; i < 128; i = i + 1) begin
            el = i[6:0];
            chk({23'd0, p_rd_bits[i]}, {23'd0, (va_of(el) < 64) ? 1'b1 : 1'b0},
                "predicate bit", el);
        end

        // v4 = 7 everywhere, then overwrite with v0 only where P1 is set
        for (c = 0; c < 8; c = c + 1) begin
            buf0 = 0;
            for (i = 0; i < 16; i = i + 1) begin
                buf0[i*24 +: 24] = e8i(7);
            end
            wr_chunk(4'd4, c[2:0], buf0);
        end
        set_stage(0, OP_MOV, SRC_V, SRC_K, SRC_K, 0, 0, 0);
        for (c = 0; c < 8; c = c + 1) begin
            beat(c[2:0], 2'd0, 4'd0, 4'd0, 4'd0, 4'd4, 2'd1, 2'd1, 1'b0, 1'b0);
        end
        drain;
        for (c = 0; c < 8; c = c + 1) begin
            rd_chunk(4'd4, c[2:0]);
            for (i = 0; i < 16; i = i + 1) begin
                el = {c[2:0], i[3:0]};
                want = (va_of(el) < 64) ? e8i(va_of(el)) : e8i(7);
                chk(ls_rdata[i*24 +: 24], want, "predicated write", el);
            end
        end
        ls_ractive = 0;

        // ================================================= 3. D2
        $display("--- 3. D2: two-stage chain ---");
        mode = M_D2;
        set_stage(0, OP_MUL, SRC_V, SRC_K, SRC_K, 0, e8i(2), 0);
        set_stage(1, OP_ADD, SRC_C, SRC_K, SRC_K, 0, 0, e8i(1));
        for (c = 0; c < 8; c = c + 1) begin
            for (ph = 0; ph < 2; ph = ph + 1) begin
                beat(c[2:0], ph[1:0], 4'd0, 4'd0, 4'd0, 4'd5, 2'd0, 2'd0,
                     1'b0, 1'b0);
            end
        end
        drain;
        for (c = 0; c < 8; c = c + 1) begin
            rd_chunk(4'd5, c[2:0]);
            for (i = 0; i < 16; i = i + 1) begin
                el = {c[2:0], i[3:0]};
                chk(ls_rdata[i*24 +: 24], e8i(2*va_of(el) + 1), "D2 chain", el);
            end
        end
        ls_ractive = 0;

        // ================================================= 4. D4
        $display("--- 4. D4: four-stage chain ---");
        mode = M_D4;
        set_stage(0, OP_MUL, SRC_V, SRC_K, SRC_K, 0, e8i(2), 0);
        set_stage(1, OP_ADD, SRC_C, SRC_K, SRC_K, 0, 0, e8i(1));
        set_stage(2, OP_MUL, SRC_C, SRC_K, SRC_K, 0, e8i(3), 0);
        set_stage(3, OP_SUB, SRC_C, SRC_K, SRC_K, 0, 0, e8i(4));
        for (c = 0; c < 8; c = c + 1) begin
            for (ph = 0; ph < 4; ph = ph + 1) begin
                beat(c[2:0], ph[1:0], 4'd0, 4'd0, 4'd0, 4'd6, 2'd0, 2'd0,
                     1'b0, 1'b0);
            end
        end
        drain;
        for (c = 0; c < 8; c = c + 1) begin
            rd_chunk(4'd6, c[2:0]);
            for (i = 0; i < 16; i = i + 1) begin
                el = {c[2:0], i[3:0]};
                chk(ls_rdata[i*24 +: 24], e8i(3*(2*va_of(el) + 1) - 4),
                    "D4 chain", el);
            end
        end
        ls_ractive = 0;

        // ================================================= 5. TREE
        $display("--- 5. TREE: sum, max, min ---");
        run_tree(3'd0, 4'd0, 4'd0, 1'b0);          // SUM of v0
        chk(red_result, e8i(128*129/2), "TREE sum", 0);
        run_tree(3'd1, 4'd0, 4'd0, 1'b0);          // MAX of v0
        chk(red_result, e8i(128), "TREE max", 0);
        run_tree(3'd2, 4'd0, 4'd0, 1'b0);          // MIN of v0
        chk(red_result, e8i(1), "TREE min", 0);

        $display("--- 6. TREE: sumsq and dot, half rate ---");
        run_tree(3'd3, 4'd3, 4'd3, 1'b1);          // SUMSQ of v3
        chk(red_result, e8i(16*204), "TREE sumsq", 0);
        run_tree(3'd4, 4'd3, 4'd1, 1'b1);          // DOT of v3 . v1
        chk(red_result, e8i(dot_ref()), "TREE dot", 0);

        $display("--- 7. TREE: EXPSUM writes back AND reduces ---");
        // exp2 is EXACT at integer arguments, so 0 and 1 give 1.0 and 2.0 and
        // the whole reference is exact: per chunk 8 of each, 24; over 8, 192.
        for (c = 0; c < 8; c = c + 1) begin
            for (el = 0; el < 16; el = el + 1) begin
                buf0[el*24 +: 24] = e8i(el & 1);
            end
            wr_chunk(4'd6, c[2:0], buf0);
        end
        run_expsum(4'd6, 4'd7);
        chk(red_result, e8i(192), "EXPSUM sum", 0);
        for (c = 0; c < 8; c = c + 1) begin
            rd_chunk(4'd7, c[2:0]);
            for (el = 0; el < 16; el = el + 1) begin
                chk(ls_rdata[el*24 +: 24], e8i((el & 1) ? 2 : 1),
                    "EXPSUM elementwise", c*16 + el);
            end
        end

        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

    function integer dot_ref;
        integer k, acc;
        reg [6:0] e7;
        begin
            acc = 0;
            for (k = 0; k < 128; k = k + 1) begin
                e7  = k[6:0];
                acc = acc + vs_of(e7) * vb_of(e7);
            end
            dot_ref = acc;
        end
    endfunction

    integer tc, tp;

    // Same shape as run_tree's half-rate path, but naming a vector destination:
    // EXPSUM keeps the leaf result as well as reducing it.
    task run_expsum(input [3:0] ra, input [3:0] wd);
        begin
            mode = M_TREE;
            red_kind = R_EXPSUM;
            @(negedge clk); red_init = 1'b1;
            @(negedge clk); red_init = 1'b0;
            for (tc = 0; tc < 8; tc = tc + 1) begin
                for (tp = 0; tp < 2; tp = tp + 1) begin
                    beat(tc[2:0], tp[1:0], ra, ra, ra, wd, 2'd0, 2'd0,
                         1'b0, 1'b0);
                end
            end
            drain;
            beat(3'd0, 2'd0, ra, ra, ra, wd, 2'd0, 2'd0, 1'b0, 1'b1);
            @(negedge clk);
            spin = 0;
            while (!red_valid && spin < 4000) begin
                spin = spin + 1;
                @(negedge clk);
            end
            if (spin >= 4000) begin
                errors = errors + 1;
                $display("  FAIL EXPSUM red_valid never asserted");
            end
        end
    endtask

    task run_tree(input [2:0] kind, input [3:0] ra, input [3:0] rb,
                  input half);
        begin
            mode = M_TREE;
            red_kind = kind;
            @(negedge clk); red_init = 1'b1;
            @(negedge clk); red_init = 1'b0;
            for (tc = 0; tc < 8; tc = tc + 1) begin
                if (half) begin
                    for (tp = 0; tp < 2; tp = tp + 1) begin
                        beat(tc[2:0], tp[1:0], ra, rb, ra, 4'd0, 2'd0, 2'd0,
                             1'b0, 1'b0);
                    end
                end
                else begin
                    beat(tc[2:0], 2'd0, ra, rb, ra, 4'd0, 2'd0, 2'd0,
                         1'b0, 1'b0);
                end
            end
            drain;
            beat(3'd0, 2'd0, ra, rb, ra, 4'd0, 2'd0, 2'd0, 1'b0, 1'b1);
            @(negedge clk);
            spin = 0;
            while (!red_valid && spin < 4000) begin
                spin = spin + 1;
                @(negedge clk);
            end
            if (spin >= 4000) begin
                errors = errors + 1;
                $display("  FAIL red_valid never asserted (kind=%0d)", kind);
            end
        end
    endtask

    initial begin
        #400000;
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire

// v2_lanes: free-running RTL against the gated model, every output at every
// edge, at RF_COPIES 4 and 2. Bursts issue, unpack, read ahead and pack at
// random -- sequences v2_core would refuse are fine, both copies see the
// same -- then hold, flipping single input bits inside the held stretches.

`timescale 1ns/1ps
`default_nettype none

module gate_v2_lanes_tb;
    localparam integer CYCLES = 200000;
    localparam integer RW = 5, RA = RW + 3;

    reg clk = 1'b0;
    always #1 clk = ~clk;

    reg          rst = 1'b1;
    reg          iss_valid = 0, iss_last = 0, iss_xc = 0, iss_wr = 0, iss_cmp = 0;
    reg          iss_expd = 0, uw_we = 0, pk_v = 0, pre_v = 0, iss_ch = 0, stk_clr = 0;
    reg [4:0]    iss_op = 0;
    reg [RA-1:0] iss_ra = 0, iss_rb = 0, iss_rc = 0, iss_wa = 0, uw_wa = 0, pk_ra = 0, pre_rc = 0;
    reg [1:0]    iss_sa = 0, iss_sb = 0, iss_sc = 0, iss_pr = 0, iss_pm = 0;
    reg [23:0]   iss_ka = 0, iss_kb = 0, iss_kc = 0;
    reg [2:0]    iss_xm = 0;
    reg [3:0]    iss_xk = 0;
    reg [15:0]   iss_tmask = 0;
    reg [383:0]  uw_wd = 0;

    // The outputs of one instance, concatenated: mw_bank_next, pk_rd, wb_*,
    // pipe_empty, sticky.
    localparam integer OW = 2 + 384 + 1 + RW + 1 + 1 + 2 + 1 + 16;

`define V2L_PORTS(o) \
        .clk(clk), .rst(rst), .iss_valid(iss_valid), .iss_last(iss_last), .iss_op(iss_op), \
        .iss_ra(iss_ra), .iss_rb(iss_rb), .iss_rc(iss_rc), \
        .iss_sa(iss_sa), .iss_sb(iss_sb), .iss_sc(iss_sc), \
        .iss_ka(iss_ka), .iss_kb(iss_kb), .iss_kc(iss_kc), \
        .iss_xm(iss_xm), .iss_xk(iss_xk), .iss_xc(iss_xc), \
        .iss_wa(iss_wa), .iss_wr(iss_wr), .iss_cmp(iss_cmp), \
        .iss_pr(iss_pr), .iss_pm(iss_pm), .iss_tmask(iss_tmask), .iss_expd(iss_expd), \
        .uw_we(uw_we), .uw_wa(uw_wa), .uw_wd(uw_wd), .mw_bank_next(o[OW-1 -: 2]), \
        .pk_ra(pk_ra), .pk_v(pk_v), .pk_rd(o[OW-3 -: 384]), \
        .pre_v(pre_v), .pre_rc(pre_rc), .iss_ch(iss_ch), \
        .wb_last(o[RW+21]), .wb_vreg(o[RW+20 -: RW]), .wb_wr(o[20]), .wb_cmp(o[19]), \
        .wb_pr(o[18:17]), .pipe_empty(o[16]), .stk_clr(stk_clr), .sticky(o[15:0])

    wire [OW-1:0] o4_r, o4_g, o2_r, o2_g;
    v2_lanes__rtl #(.MODEL(1), .RF_COPIES(4), .RW(RW)) u_r4 (`V2L_PORTS(o4_r));
    v2_lanes      #(.MODEL(1), .RF_COPIES(4), .RW(RW)) u_g4 (`V2L_PORTS(o4_g));
    v2_lanes__rtl #(.MODEL(1), .RF_COPIES(2), .RW(RW)) u_r2 (`V2L_PORTS(o2_r));
    v2_lanes      #(.MODEL(1), .RF_COPIES(2), .RW(RW)) u_g2 (`V2L_PORTS(o2_g));

    integer k;
    integer cyc = 0, left = 0, mism = 0, withheld = 0;
    reg     busy = 1'b0;

    always @(posedge clk) begin
        cyc <= cyc + 1;
        if (!u_g4.u_gate.run_l) withheld <= withheld + 1;
        if (cyc == 20) rst <= 1'b0;
        if (left == 0) begin
            busy <= !busy;
            left <= busy ? 1 + ($unsigned($random) % 1500) : 1 + ($unsigned($random) % 80);
        end else begin
            left <= left - 1;
        end
        if (busy) begin
            iss_valid <= ($unsigned($random) % 4) != 0;
            uw_we     <= ($unsigned($random) % 6) == 0;
            pre_v     <= ($unsigned($random) % 12) == 0;
            {iss_last, iss_xc, iss_wr, iss_cmp, iss_expd, pk_v, iss_ch, stk_clr} <= $random;
            iss_op <= ($unsigned($random) % 3) ? 5'd6 : $random;
            {iss_ra, iss_rb, iss_rc, iss_wa} <= $random;
            {uw_wa, pk_ra, pre_rc} <= $random;
            {iss_sa, iss_sb, iss_sc, iss_pr, iss_pm, iss_xm, iss_xk} <= $random;
            iss_ka <= $random; iss_kb <= $random; iss_kc <= $random;
            iss_tmask <= ($unsigned($random) % 3) ? 16'hFFFF : $random;
            for (k = 0; k < 12; k = k + 1) uw_wd[k*32 +: 32] <= $random;
        end else begin
            iss_valid <= 1'b0;
            uw_we     <= 1'b0;
            pre_v     <= 1'b0;
            // A single held input moves: the gate must reopen for it.
            if (($unsigned($random) % 1000) == 0) begin
                case ($unsigned($random) % 4)
                    0: iss_ra <= $random;
                    1: pk_ra <= $random;
                    2: iss_kc[$unsigned($random) % 24] <= ~iss_kc[$unsigned($random) % 24];
                    default: stk_clr <= !stk_clr;
                endcase
            end
        end
        if (cyc > 30000 && cyc < 30010) rst <= 1'b1;
        if (cyc == 30010) rst <= 1'b0;
    end

    always @(negedge clk) begin
        if (cyc > 40 && (o4_r !== o4_g || o2_r !== o2_g)) begin
            if (mism < 5)
                $display("MISMATCH cyc=%0d c4 %b c2 %b run4 %b run2 %b", cyc,
                         o4_r !== o4_g, o2_r !== o2_g, u_g4.u_gate.run_l, u_g2.u_gate.run_l);
            mism <= mism + 1;
        end
        if (cyc == CYCLES) begin
            $display("%s gate v2_lanes edges=%0d withheld=%0d mismatches=%0d",
                     (mism == 0 && withheld > CYCLES / 10) ? "PASS" : "FAIL",
                     cyc, withheld, mism);
            $finish;
        end
    end
endmodule

`default_nettype wire

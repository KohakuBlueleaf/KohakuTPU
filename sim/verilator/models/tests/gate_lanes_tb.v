// vec_lanes: free-running RTL against the gated model, every output at every
// edge. Bursts issue, load and reduce at random -- sequences the sequencer
// would refuse are fine, both copies see the same -- then hold.

`timescale 1ns/1ps
`default_nettype none

module gate_lanes_tb;
    localparam integer CYCLES = 200000;
    localparam integer RW = 7, CW = 3;

    reg clk = 1'b0;
    always #1 clk = ~clk;

    reg          rst = 1'b1;
    reg [1:0]    mode = 0, ls_pm = 0, ls_pr = 0, iss_phase = 0, iss_pm = 0, iss_pr = 0;
    reg [1:0]    p_rd_sel = 0;
    reg          ls_we = 0, ls_ractive = 0, ls_rsel = 0, iss_valid = 0, iss_is_cmp = 0;
    reg          iss_tail = 0, red_init = 0;
    reg [RW-1:0] ls_waddr = 0, ls_raddr = 0, iss_ra = 0, iss_rb = 0, iss_rc = 0, iss_wa = 0;
    reg [CW-1:0] iss_chunk = 0;
    reg [383:0]  ls_wdata = 0;
    reg [15:0]   iss_tmask = 0;
    reg [19:0]   st_op = 0;
    reg [7:0]    st_sa = 0, st_sb = 0, st_sc = 0;
    reg [95:0]   st_ka = 0, st_kb = 0, st_kc = 0;
    reg [2:0]    red_kind = 0;

    wire [383:0] rd_r, rd_g;
    wire [127:0] pb_r, pb_g;
    wire [23:0]  rr_r, rr_g;
    wire [3:0]   wv_r, wv_g;
    wire         rv_r, rv_g, pe_r, pe_g, lq_r, lq_g, wa_r, wa_g, wf_r, wf_g;

`define LANES_PORTS(rd, pb, rr, rv, pe, lq, wa, wf, wv) \
        .clk(clk), .rst(rst), .mode(mode), .ls_we(ls_we), .ls_pm(ls_pm), .ls_pr(ls_pr), \
        .ls_waddr(ls_waddr), .ls_wdata(ls_wdata), .ls_raddr(ls_raddr), \
        .ls_ractive(ls_ractive), .ls_rsel(ls_rsel), .ls_rdata(rd), \
        .iss_valid(iss_valid), .iss_phase(iss_phase), .iss_ra(iss_ra), .iss_rb(iss_rb), \
        .iss_rc(iss_rc), .iss_wa(iss_wa), .iss_pm(iss_pm), .iss_pr(iss_pr), \
        .iss_chunk(iss_chunk), .iss_is_cmp(iss_is_cmp), .iss_tail(iss_tail), \
        .iss_tmask(iss_tmask), .st_op(st_op), .st_sa(st_sa), .st_sb(st_sb), \
        .st_sc(st_sc), .st_ka(st_ka), .st_kb(st_kb), .st_kc(st_kc), \
        .red_init(red_init), .red_kind(red_kind), .p_rd_sel(p_rd_sel), .p_rd_bits(pb), \
        .red_result(rr), .red_valid(rv), .pipe_empty(pe), .ls_quiet(lq), \
        .wb_ahead(wa), .wb_fire(wf), .wb_vreg(wv)

    vec_lanes__rtl #(.MODEL(1), .VLMAX(128)) u_ref (
        `LANES_PORTS(rd_r, pb_r, rr_r, rv_r, pe_r, lq_r, wa_r, wf_r, wv_r));
    vec_lanes #(.MODEL(1), .VLMAX(128)) u_g (
        `LANES_PORTS(rd_g, pb_g, rr_g, rv_g, pe_g, lq_g, wa_g, wf_g, wv_g));

    integer k;
    integer cyc = 0, left = 0, mism = 0, withheld = 0;
    reg     busy = 1'b0;

    always @(posedge clk) begin
        cyc <= cyc + 1;
        if (!u_g.u_gate.run_l) withheld <= withheld + 1;
        if (cyc == 20) rst <= 1'b0;
        if (left == 0) begin
            busy <= !busy;
            left <= busy ? 1 + ($unsigned($random) % 1500) : 1 + ($unsigned($random) % 80);
            if (!busy) mode <= $random;
        end else begin
            left <= left - 1;
        end
        if (busy) begin
            iss_valid  <= ($unsigned($random) % 4) != 0;
            ls_we      <= ($unsigned($random) % 6) == 0;
            red_init   <= ($unsigned($random) % 50) == 0;
            {ls_pm, ls_pr, iss_phase, iss_pm, iss_pr, p_rd_sel} <= $random;
            {ls_ractive, ls_rsel, iss_is_cmp, iss_tail} <= $random;
            {ls_waddr, ls_raddr, iss_ra, iss_rb} <= $random;
            {iss_rc, iss_wa, iss_chunk} <= $random;
            iss_tmask  <= ($unsigned($random) % 3) ? 16'hFFFF : $random;
            st_op      <= $random;
            {st_sa, st_sb, st_sc} <= $random;
            red_kind   <= $random;
            for (k = 0; k < 12; k = k + 1) ls_wdata[k*32 +: 32] <= $random;
            for (k = 0; k < 3; k = k + 1) begin
                st_ka[k*32 +: 32] <= $random;
                st_kb[k*32 +: 32] <= $random;
                st_kc[k*32 +: 32] <= $random;
            end
        end else begin
            iss_valid <= 1'b0;
            ls_we     <= 1'b0;
            red_init  <= 1'b0;
            if (($unsigned($random) % 300) == 0) iss_ra <= $random;
        end
        if (cyc > 30000 && cyc < 30010) rst <= 1'b1;
        if (cyc == 30010) rst <= 1'b0;
    end

    always @(negedge clk) begin
        if (cyc > 40 && (rd_r !== rd_g || pb_r !== pb_g || rr_r !== rr_g || rv_r !== rv_g
                         || pe_r !== pe_g || lq_r !== lq_g || wa_r !== wa_g
                         || wf_r !== wf_g || wv_r !== wv_g)) begin
            if (mism < 3)
                $display("  alu14 ref: op %h a %h b %h c %h iv %b out %h q %0d run %b | gated: op %h a %h b %h c %h iv %b out %h q %0d run %b | mode %0d lanes_q %0d",
                         u_ref.g_alu[14].u_alu.op, u_ref.g_alu[14].u_alu.a, u_ref.g_alu[14].u_alu.b,
                         u_ref.g_alu[14].u_alu.c, u_ref.g_alu[14].u_alu.in_valid, u_ref.g_alu[14].u_alu.out,
                         u_ref.g_alu[14].u_alu.u_gate.quiet, u_ref.g_alu[14].u_alu.u_gate.run_l,
                         u_g.u_rtl.g_alu[14].u_alu.op, u_g.u_rtl.g_alu[14].u_alu.a, u_g.u_rtl.g_alu[14].u_alu.b,
                         u_g.u_rtl.g_alu[14].u_alu.c, u_g.u_rtl.g_alu[14].u_alu.in_valid, u_g.u_rtl.g_alu[14].u_alu.out,
                         u_g.u_rtl.g_alu[14].u_alu.u_gate.quiet, u_g.u_rtl.g_alu[14].u_alu.u_gate.run_l,
                         mode, u_g.u_gate.quiet);
            if (mism < 5)
                $display("MISMATCH cyc=%0d rd %b pb %b rr %b rv %b pe %b lq %b wa %b wf %b wv %b run %b",
                         cyc, rd_r !== rd_g, pb_r !== pb_g, rr_r !== rr_g, rv_r !== rv_g,
                         pe_r !== pe_g, lq_r !== lq_g, wa_r !== wa_g, wf_r !== wf_g,
                         wv_r !== wv_g, u_g.u_gate.run_l);
            mism <= mism + 1;
        end
        if (cyc == CYCLES) begin
            $display("%s gate vec_lanes edges=%0d withheld=%0d mismatches=%0d",
                     (mism == 0 && withheld > CYCLES / 10) ? "PASS" : "FAIL",
                     cyc, withheld, mism);
            $finish;
        end
    end
endmodule

`default_nettype wire

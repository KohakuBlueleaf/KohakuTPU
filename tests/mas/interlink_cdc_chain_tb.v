// interlink_cdc_chain_tb -- the SLR chain 0-1-3-2 with FOUR DIFFERENT CLOCKS.

// TWO RESETS, RELEASED IN ORDER. Misframed TLAST, an underflowed beat count and
// a forward past the chain's end were ALL one discarded beat at reset release.

// interlink_4mesh_tb proves the chain at ONE clock. The case only this bench
// has is TRANSIT: mesh0 -> mesh2 is three hops through two meshes whose rates
// neither end shares.

// DEPTH IS SWEPT 1, 2, 4: no link at all, one crossing, then the chain where
// credit returns travel back through two further rates.

// A BENCH THAT PASSES BY TIMEOUT IS NOT PASSING. The progress monitor fails if
// delivery stops for STALL_LIMIT cycles while packets are still owed.

`timescale 1ns / 1ps
`default_nettype none

module interlink_cdc_chain_tb;
    localparam integer LW   = 288;
    localparam integer UW   = 96;
    localparam integer RXB  = 64;
    localparam integer MAXB = 32;
    localparam integer PKTB = 8;         // beats per packet, <= MAXB
    localparam integer NPKT = 24;        // per source, per active destination
    localparam integer STALL_LIMIT = 20000;
    // Slowest-domain cycles between the last `ready` and the switch release.
    // ZERO measures the block design's real sequence: the AND of six `ready`.
    localparam integer RST_MARGIN = 0;

    localparam integer U_KIND = 0, U_DMESH = 4, U_SMESH = 6,
                       U_TXN = 8,  U_LEN = 16;
    localparam [3:0] K_NOC_FLIT = 4'h2;

    // INITIALISED AT DECLARATION, not in an initial block: a `real` is 0.0 at
    // time 0, and `always #0` is a zero-delay loop the simulator never leaves.

    // 300/150/123/250 MHz. mesh1 and mesh3 TRANSIT for mesh0 <-> mesh2 and
    // share a rate with neither end. 4.068 is non-harmonic with all three.
    real hp0 = 1.666, hp1 = 3.333, hp2 = 4.068, hp3 = 2.000;
    reg  [3:0] clkm = 4'b0000;
    always #hp0 clkm[0] = ~clkm[0];
    always #hp1 clkm[1] = ~clkm[1];
    always #hp2 clkm[2] = ~clkm[2];
    always #hp3 clkm[3] = ~clkm[3];

    // TWO RESETS: xpm DISCARDS writes while wr_rst_busy and a switch emits
    // credit the cycle it is released. Crossings out first, switches on `ready`.
    reg resetn = 0, cdc_rstn = 0;
    wire [5:0] cdc_fault;
    wire [5:0] cdc_ready;

    integer depth = 4;                   // meshes taking part this phase
    integer errors = 0, checks = 0, phase = 0;

    task chk(input cond, input [511:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 16)
                    $display("  FAIL depth %0d: %0s [%0d]", depth, what, where);
            end
        end
    endtask

    // Chain position of a mesh, and the mesh at a position. The ONLY place the
    // SLR order appears, as in interlink_4mesh_tb.
    function integer cpos(input integer m);
        cpos = (m == 0) ? 0 : (m == 1) ? 1 : (m == 3) ? 2 : 3;
    endfunction
    function integer cat(input integer p);
        cat = (p == 0) ? 0 : (p == 1) ? 1 : (p == 2) ? 3 : 2;
    endfunction

    // ---- switch ports ------------------------------------------------------
    reg  [UW-1:0] ltx_hdr [0:3];
    reg  [LW-1:0] ltx_dat [0:3];
    reg  [3:0]    ltx_hv, ltx_dv, ltx_dl;
    wire [3:0]    ltx_hr, ltx_dr;

    wire [UW-1:0] lrx_hdr [0:3];
    wire [LW-1:0] lrx_dat [0:3];
    wire [3:0]    lrx_hv, lrx_dv, lrx_dl;
    reg  [3:0]    lrx_hr, lrx_dr;

    wire [LW-1:0] dn_td [0:3], up_td [0:3];
    wire [UW-1:0] dn_tu [0:3], up_tu [0:3];
    wire [3:0]    dn_tl, dn_tv, up_tl, up_tv;

    // After the crossing, in the DESTINATION mesh's domain.
    wire [LW-1:0] s0_td [0:3], s1_td [0:3];
    wire [UW-1:0] s0_tu [0:3], s1_tu [0:3];
    wire [3:0]    s0_tl, s0_tv, s1_tl, s1_tv;

    wire [3:0] flt [0:3];
    wire [63:0] c_fwd [0:3];

    genvar g, h;
    generate
    for (g = 0; g < 4; g = g + 1) begin : m
        localparam integer P    = cpos(g);
        localparam integer HAS0 = (P != 0) ? 1 : 0;
        localparam integer HAS1 = (P != 3) ? 1 : 0;

        mag_switch #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB),
                     .MAX_BEATS(MAXB)) u (
            .clk(clkm[g]), .resetn(resetn), .my_mesh(g[1:0]),
            .ltx_hdr(ltx_hdr[g]), .ltx_hvalid(ltx_hv[g]), .ltx_hready(ltx_hr[g]),
            .ltx_dat(ltx_dat[g]), .ltx_dlast(ltx_dl[g]), .ltx_dvalid(ltx_dv[g]),
            .ltx_dready(ltx_dr[g]),
            .lrx_hdr(lrx_hdr[g]), .lrx_hvalid(lrx_hv[g]), .lrx_hready(lrx_hr[g]),
            .lrx_dat(lrx_dat[g]), .lrx_dlast(lrx_dl[g]), .lrx_dvalid(lrx_dv[g]),
            .lrx_dready(lrx_dr[g]),
            .m0_tdata(dn_td[g]), .m0_tuser(dn_tu[g]), .m0_tlast(dn_tl[g]),
            .m0_tvalid(dn_tv[g]), .m0_tready(1'b1),
            .s0_tdata(HAS0 ? s0_td[g] : {LW{1'b0}}),
            .s0_tuser(HAS0 ? s0_tu[g] : {UW{1'b0}}),
            .s0_tlast(HAS0 ? s0_tl[g] : 1'b0),
            .s0_tvalid(HAS0 ? s0_tv[g] : 1'b0), .s0_tready(),
            .m1_tdata(up_td[g]), .m1_tuser(up_tu[g]), .m1_tlast(up_tl[g]),
            .m1_tvalid(up_tv[g]), .m1_tready(1'b1),
            .s1_tdata(HAS1 ? s1_td[g] : {LW{1'b0}}),
            .s1_tuser(HAS1 ? s1_tu[g] : {UW{1'b0}}),
            .s1_tlast(HAS1 ? s1_tl[g] : 1'b0),
            .s1_tvalid(HAS1 ? s1_tv[g] : 1'b0), .s1_tready(),
            .ctr_tx0(), .ctr_rx0(), .ctr_stall0(),
            .ctr_tx1(), .ctr_rx1(), .ctr_stall1(),
            .ctr_fwd(c_fwd[g]), .ctr_lblock(),
            .cred0_state(), .cred1_state(), .fault(flt[g])
        );
    end

    // ---- one crossing per hop, per direction -------------------------------
    // Hop h joins positions h and h+1. UP leaves by m1 onto the upper mesh's
    // s0; DOWN leaves by m0 onto s1.
    for (h = 0; h < 3; h = h + 1) begin : hop
        localparam integer LO = cat(h);
        localparam integer HI = cat(h + 1);

        mag_link_cdc #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB)) up_cdc (
            .s_axis_aclk(clkm[LO]), .s_axis_aresetn(cdc_rstn),
            .m_axis_aclk(clkm[HI]), .m_axis_aresetn(cdc_rstn),
            .S_AXIS_tdata(up_td[LO]), .S_AXIS_tuser(up_tu[LO]),
            .S_AXIS_tlast(up_tl[LO]), .S_AXIS_tvalid(up_tv[LO]),
            .S_AXIS_tready(),
            .M_AXIS_tdata(s0_td[HI]), .M_AXIS_tuser(s0_tu[HI]),
            .M_AXIS_tlast(s0_tl[HI]), .M_AXIS_tvalid(s0_tv[HI]),
            .M_AXIS_tready(1'b1),
            .fault(cdc_fault[2*h]), .ready(cdc_ready[2*h])
        );

        mag_link_cdc #(.LINK_W(LW), .TUSER_W(UW), .RX_BEATS(RXB)) dn_cdc (
            .s_axis_aclk(clkm[HI]), .s_axis_aresetn(cdc_rstn),
            .m_axis_aclk(clkm[LO]), .m_axis_aresetn(cdc_rstn),
            .S_AXIS_tdata(dn_td[HI]), .S_AXIS_tuser(dn_tu[HI]),
            .S_AXIS_tlast(dn_tl[HI]), .S_AXIS_tvalid(dn_tv[HI]),
            .S_AXIS_tready(),
            .M_AXIS_tdata(s1_td[LO]), .M_AXIS_tuser(s1_tu[LO]),
            .M_AXIS_tlast(s1_tl[LO]), .M_AXIS_tvalid(s1_tv[LO]),
            .M_AXIS_tready(1'b1),
            .fault(cdc_fault[2*h+1]), .ready(cdc_ready[2*h+1])
        );
    end
    endgenerate

    // ---- payload, so a sink can check ordering without the header ----------
    function [LW-1:0] payload(input integer src, input integer dst,
                              input integer seq, input integer beat);
        payload = {{(LW-64){1'b0}}, src[1:0], dst[1:0], 12'd0,
                   seq[15:0], beat[15:0], 16'hA5A5};
    endfunction

    function [UW-1:0] header(input integer src, input integer dst);
        begin
            header = {UW{1'b0}};
            header[U_KIND  +: 4]  = K_NOC_FLIT;
            header[U_DMESH +: 2]  = dst[1:0];
            header[U_SMESH +: 2]  = src[1:0];
            header[U_LEN   +: 16] = PKTB - 1;
        end
    endfunction

    // ---- accounting --------------------------------------------------------
    integer sent_pkt [0:3];
    integer got_beat [0:3];
    integer exp_seq  [0:3][0:3];         // [dst][src]
    integer beat_in  [0:3];
    integer delivered = 0, owed = 0;

    // A mesh takes part when its chain position is below `depth`. Positions,
    // not ids: depth 2 must be two ADJACENT meshes or there is no link.
    function integer active(input integer mesh);
        active = (cpos(mesh) < depth) ? 1 : 0;
    endfunction

    // ---- per-mesh source, in that mesh's own domain ------------------------
    integer si [0:3];
    integer sd [0:3];
    integer sb [0:3];
    reg [1:0] sst [0:3];
    reg [3:0] hr_q, dr_q;

    localparam [1:0] T_HDR = 2'd0, T_DAT = 2'd1, T_DONE = 2'd2;

    genvar gs;
    generate
    for (gs = 0; gs < 4; gs = gs + 1) begin : src
        always @(posedge clkm[gs]) begin
            hr_q[gs] <= ltx_hr[gs];
            dr_q[gs] <= ltx_dr[gs];
        end

        // BLOCKING, so the data presented and the beat index never disagree. A
        // non-blocking version misframed TLAST and the link reported it.
        always @(negedge clkm[gs]) begin
            if (!resetn) begin
                ltx_hv[gs] = 1'b0; ltx_dv[gs] = 1'b0; ltx_dl[gs] = 1'b0;
                sst[gs] = T_HDR;
            end else if (active(gs) && (sd[gs] != gs) && (sst[gs] != T_DONE)) begin
                case (sst[gs])
                T_HDR:
                    if (ltx_hv[gs] && hr_q[gs]) begin
                        ltx_hv[gs] = 1'b0;
                        sb[gs]     = 0;
                        sst[gs]    = T_DAT;
                    end else begin
                        ltx_hdr[gs] = header(gs, sd[gs]);
                        ltx_hv[gs]  = 1'b1;
                    end
                T_DAT:
                    if (ltx_dv[gs] && dr_q[gs]) begin
                        if (sb[gs] == PKTB - 1) begin
                            ltx_dv[gs] = 1'b0;
                            ltx_dl[gs] = 1'b0;
                            sent_pkt[gs] = sent_pkt[gs] + 1;
                            sd[gs] = next_dst(gs, sd[gs]);
                            if (sd[gs] == first_dst(gs)) si[gs] = si[gs] + 1;
                            sst[gs] = (si[gs] >= NPKT) ? T_DONE : T_HDR;
                        end else begin
                            sb[gs] = sb[gs] + 1;
                            ltx_dat[gs] = payload(gs, sd[gs], si[gs], sb[gs]);
                            ltx_dl[gs]  = (sb[gs] == PKTB - 1);
                        end
                    end else begin
                        ltx_dat[gs] = payload(gs, sd[gs], si[gs], sb[gs]);
                        ltx_dl[gs]  = (sb[gs] == PKTB - 1);
                        ltx_dv[gs]  = 1'b1;
                    end
                default: ;
                endcase
            end
        end
    end
    endgenerate

    // BOUNDED. At depth 1 there is no other active mesh, and an unbounded
    // search for one hangs the simulator at time 0 with no output at all.
    function integer next_dst(input integer me, input integer from);
        integer d, n;
        begin
            next_dst = me;              // `me` means: nothing to send to
            d = from;
            for (n = 0; n < 4; n = n + 1) begin
                d = (d + 1) % 4;
                if ((d != me) && (active(d) != 0) && (next_dst == me))
                    next_dst = d;
            end
        end
    endfunction

    function integer first_dst(input integer me);
        first_dst = next_dst(me, me);
    endfunction

    // ---- per-mesh sink -----------------------------------------------------
    integer rseed = 4242;
    genvar gr;
    generate
    for (gr = 0; gr < 4; gr = gr + 1) begin : snk
        always @(posedge clkm[gr]) begin
            lrx_hr[gr] <= 1'b1;
            lrx_dr[gr] <= (($random(rseed) & 7) != 0);
        end

        always @(negedge clkm[gr]) if (resetn) begin
            if (lrx_dv[gr] && lrx_dr[gr]) begin
                got_beat[gr] = got_beat[gr] + 1;
                delivered    = delivered + 1;
                check_beat(gr, lrx_dat[gr]);
            end
        end
    end
    endgenerate

    task check_beat(input integer me, input [LW-1:0] d);
        integer s, dst, seq, beat;
        begin
            s    = d[63:62];
            dst  = d[61:60];
            seq  = d[47:32];
            beat = d[31:16];
            if (dst !== me) chk(1'b0, "a packet landed on the wrong mesh", me);
            else if (d[15:0] !== 16'hA5A5)
                chk(1'b0, "payload sentinel is corrupt", me);
            else if (beat !== beat_in[me])
                chk(1'b0, "beats arrived out of order within a packet", beat);
            else begin
                if (beat == PKTB - 1) begin
                    beat_in[me] = 0;
                    if (seq !== exp_seq[me][s])
                        chk(1'b0, "a packet from this source was lost or reordered",
                            seq);
                    exp_seq[me][s] = seq + 1;
                end else beat_in[me] = beat_in[me] + 1;
            end
        end
    endtask

    // ---- progress ----------------------------------------------------------
    // Against mesh0's clock, which is the fastest, so the limit is conservative.
    integer last_seen, stall_ct;
    always @(posedge clkm[0]) begin
        if (!resetn) begin last_seen <= 0; stall_ct <= 0; end
        else if (delivered != last_seen) begin
            last_seen <= delivered; stall_ct <= 0;
        end else if (delivered < owed) stall_ct <= stall_ct + 1;
    end

    // ---- the phases --------------------------------------------------------
    integer i, j, spin, t0, t1;

    task run_depth(input integer d, input [511:0] label);
        integer nact, npair;
        begin
            depth    = d;
            resetn   = 1'b0;
            cdc_rstn = 1'b0;
            for (i = 0; i < 4; i = i + 1) begin
                sent_pkt[i] = 0; got_beat[i] = 0; beat_in[i] = 0;
                si[i] = 0; sb[i] = 0;
                for (j = 0; j < 4; j = j + 1) exp_seq[i][j] = 0;
            end
            delivered = 0;
            nact = 0;
            for (i = 0; i < 4; i = i + 1) if (active(i)) nact = nact + 1;
            for (i = 0; i < 4; i = i + 1) if (active(i)) sd[i] = first_dst(i);
            npair = (nact > 1) ? nact * (nact - 1) : 0;
            owed  = npair * NPKT * PKTB;

            repeat (60) @(negedge clkm[0]);
            cdc_rstn = 1'b1;
            spin = 0;
            while ((cdc_ready !== 6'h3F) && (spin < 20000)) begin
                spin = spin + 1; @(negedge clkm[0]);
            end
            chk(spin < 20000, "every crossing came out of reset", spin);
            repeat (RST_MARGIN) @(negedge clkm[2]);
            resetn = 1'b1;

            $display("--- %0s: %0d mesh(es), %0d beats owed ---",
                     label, nact, owed);
            t0 = $time;

            spin = 0;
            while ((delivered < owed) && (stall_ct < STALL_LIMIT)
                   && (spin < 4000000)) begin
                spin = spin + 1; @(negedge clkm[0]);
            end
            t1 = $time;

            chk(stall_ct < STALL_LIMIT,
                "delivery never stalled while packets were owed", delivered);
            chk(delivered == owed, "every beat arrived", delivered);
            chk(cdc_fault === 6'd0, "no crossing discarded a beat", cdc_fault);
            for (i = 0; i < 4; i = i + 1)
                if (active(i)) chk(flt[i] === 4'd0, "the switch reported no fault", i);

            // `timescale is 1ns, so t1-t0 IS ns. Dividing it by 1000 and calling
            // the result ns reported both figures 1000x out.
            if (owed > 0)
                $display("    %0d beats in %0d ns -- %0.1f beats/us aggregate",
                         delivered, t1 - t0, delivered * 1000.0 / (t1 - t0));
            $display("    transit forwarded: mesh1 %0d, mesh3 %0d",
                     c_fwd[1][31:0], c_fwd[3][31:0]);
            phase = phase + 1;
        end
    endtask

    initial begin
        run_depth(1, "depth 1: no link, the control");
        run_depth(2, "depth 2: one crossing, 300 vs 150 MHz");
        run_depth(4, "depth 4: three hops, four rates");

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #40000000;
        $display("  FAIL -- watchdog in phase %0d, %0d of %0d beats",
                 phase, delivered, owed);
        $finish;
    end

endmodule

`default_nettype wire

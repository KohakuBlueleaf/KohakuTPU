// NSRC -> 1 -> NDST. Injection is an NSRC:1 mux; ejection is a BROADCAST with
// a per-destination valid gate, so there is no NSRC*NDST term in this module.

// Arbitration is packet-atomic -- a grant is held until `i_last` -- which gives
// AXI4's no-interleaving rule for free and commits a whole burst to one dst.

`default_nettype none

module sb_hub #(
    parameter integer NSRC = 3,
    parameter integer NDST = 9,
    parameter integer PW   = 64,                        // payload, excluding dst
    parameter integer DW   = (NDST <= 1) ? 1 : $clog2(NDST),
    parameter integer SW   = (NSRC <= 1) ? 1 : $clog2(NSRC),
    parameter integer STATS = 0,                        // 0 costs nothing
    // 1: register each source input, so the arbiter starts from a flop instead
    // of a combinational path back into a source FIFO (Fmax; costs FF+skid LUT).
    parameter integer ISKID = 0,
    // 1: the NSRC:1 payload select is one kept LUT6 a bit (kohaku_mux) shared
    // by the skid's hold and output registers: a 4-source PW-270 hub is 466
    // LUT. A 3-source hub packs its 3:1 into the skid's 2:1 at 1.3 a bit.
    parameter integer PAYMUX = (NSRC >= 4) ? 1 : 0
)(
    input  wire                clk,
    input  wire                rst,

    input  wire [NSRC-1:0]     i_valid,
    output wire [NSRC-1:0]     i_ready,
    input  wire [NSRC-1:0]     i_last,
    input  wire [NSRC*DW-1:0]  i_dst,
    input  wire [NSRC*PW-1:0]  i_pay,

    output wire [NDST-1:0]     o_valid,
    input  wire [NDST-1:0]     o_ready,
    output wire [PW-1:0]       o_pay,

    output wire [31:0]         stat_flits,
    output wire [31:0]         stat_wait
);
    wire [DW-1:0] o_dst;
    wire          skid_val, skid_rdy;

    // Out of range would hold the select at x and wedge the path; drop instead.
    wire o_rdy_sel = (o_dst < NDST) ? o_ready[o_dst] : 1'b1;

    // Arbiter's view of the inputs: raw, or flopped through a per-source skid.
    wire [NSRC-1:0]     a_valid, a_ready, a_last;
    wire [NSRC*DW-1:0]  a_dst;
    wire [NSRC*PW-1:0]  a_pay;
    genvar gi;
    generate
    if (ISKID) begin : g_iskid
        for (gi = 0; gi < NSRC; gi = gi + 1) begin : g_sk
            sb_skid #(.W(1 + DW + PW)) u_isk (
                .clk(clk), .rst(rst),
                .i_valid(i_valid[gi]), .i_ready(i_ready[gi]),
                .i_data({i_last[gi], i_dst[gi*DW +: DW], i_pay[gi*PW +: PW]}),
                .o_valid(a_valid[gi]), .o_ready(a_ready[gi]),
                .o_data({a_last[gi], a_dst[gi*DW +: DW], a_pay[gi*PW +: PW]}));
        end
    end else begin : g_noskid
        assign a_valid = i_valid;
        assign i_ready = a_ready;
        assign a_last  = i_last;
        assign a_dst   = i_dst;
        assign a_pay   = i_pay;
    end
    endgenerate

    // ---------------------------------------------------------- arbitration
    reg  [SW-1:0] rr, lock_sel;
    reg           locked;

    // Round-robin as rotate-mask / isolate-lowest / encode: no serial scan and
    // no `% NSRC` on a variable (the old downward scan was a modulo per step).
    wire [NSRC-1:0] rr_mask;              // ones at and above the pointer
    genvar gm;
    generate for (gm = 0; gm < NSRC; gm = gm + 1) begin : g_rrm
        assign rr_mask[gm] = (gm >= rr);
    end endgenerate
    wire [NSRC-1:0] hi   = a_valid & rr_mask;
    wire [NSRC-1:0] pick = (|hi) ? (hi & (~hi + 1'b1)) : (a_valid & (~a_valid + 1'b1));
    wire            scan_any = |a_valid;
    reg  [SW-1:0]   scan_sel;
    integer         k;
    always @(*) begin
        scan_sel = {SW{1'b0}};
        for (k = 0; k < NSRC; k = k + 1) begin
            if (pick[k]) begin
                scan_sel = scan_sel | k[SW-1:0];
            end
        end
    end

    wire [SW-1:0] sel   = locked ? lock_sel : scan_sel;
    wire          any   = locked ? a_valid[lock_sel] : scan_any;
    wire          grant = any && skid_rdy;

    always @(posedge clk) begin
        if (rst) begin
            rr       <= {SW{1'b0}};
            locked   <= 1'b0;
            lock_sel <= {SW{1'b0}};
        end else if (grant) begin
            locked   <= !a_last[sel];
            lock_sel <= sel;
            if (a_last[sel]) begin
                rr <= (sel == NSRC-1) ? {SW{1'b0}} : (sel + 1'b1);
            end
        end
    end

    genvar g;
    generate
    for (g = 0; g < NSRC; g = g + 1) begin : g_irdy
        assign a_ready[g] = grant && (sel == g);
    end
    endgenerate

    // ------------------------------------------------------ registered path
    // An NSRC:1 mux on the binary `sel`, written as an explicit per-source
    // select. NOT `a_pay[sel*PW +: PW]`: PW is not a power of two, so the
    // variable offset is a multiply and synthesis built a BARREL SHIFTER --
    // measured 5,712 LUT for one hub's skid input against ~370 for this form.
    reg  [DW-1:0] dst_sel;
    wire [PW-1:0] pay_sel;
    reg  [PW-1:0] pay_inf;
    integer       j;
    always @(*) begin
        dst_sel = a_dst[0 +: DW];
        pay_inf = a_pay[0 +: PW];
        for (j = 1; j < NSRC; j = j + 1) begin
            if (sel == j[SW-1:0]) begin
                dst_sel = a_dst[j*DW +: DW];
                pay_inf = a_pay[j*PW +: PW];
            end
        end
    end
    generate if (PAYMUX != 0 && NSRC > 1) begin : g_paymux
        kohaku_mux #(.W(PW), .N(NSRC), .KEEP(1)) u_pm (.d(a_pay), .sel(sel), .o(pay_sel));
    end else begin : g_payinf
        assign pay_sel = pay_inf;
    end endgenerate

    // The NSRC:1 select and the skid's 2:1 (hold vs input) are each ~1
    // LUT/bit on the payload; that is the hub's floor. A `keep` on the
    // selected payload to "split the cone" ADDED a stage instead: measured
    // +4,054 LUT on the 4-station bus (loop 4), reverted.
    sb_skid #(.W(DW + PW)) u_skid (
        .clk(clk), .rst(rst),
        .i_valid(any), .i_ready(skid_rdy),
        .i_data({dst_sel, pay_sel}),
        .o_valid(skid_val), .o_ready(o_rdy_sel), .o_data({o_dst, o_pay})
    );

    generate
    for (g = 0; g < NDST; g = g + 1) begin : g_ovld
        assign o_valid[g] = skid_val && (o_dst == g);
    end
    endgenerate

    // A crossbar cannot say WHY it is slow. `wait` is the only place the fabric
    // knows it made a source queue behind someone else's packet.
    generate
    if (STATS) begin : g_stats
        reg [31:0] n_flit, n_wait;
        always @(posedge clk) begin
            if (rst) begin
                n_flit <= 32'd0;
                n_wait <= 32'd0;
            end else begin
                if (grant) begin
                    n_flit <= n_flit + 32'd1;
                end
                if (|(i_valid & ~i_ready)) begin
                    n_wait <= n_wait + 32'd1;
                end
            end
        end
        assign stat_flits = n_flit;
        assign stat_wait  = n_wait;
    end else begin : g_nostats
        assign stat_flits = 32'd0;
        assign stat_wait  = 32'd0;
    end
    endgenerate

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (!rst && skid_val && (o_dst >= NDST)) begin
            $display("%0t ERROR sb_hub: dst %0d of %0d -- NMU decode is wrong",
                     $time, o_dst, NDST);
        end
    end
`endif
endmodule

`default_nettype wire

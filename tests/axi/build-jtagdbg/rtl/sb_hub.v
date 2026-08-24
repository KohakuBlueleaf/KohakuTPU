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
    parameter integer STATS = 0                         // 0 costs nothing
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

    // ---------------------------------------------------------- arbitration
    reg  [SW-1:0] rr, lock_sel;
    reg           locked;

    // Scan downward from the pointer so a higher-priority match overwrites the
    // lower one -- the idiom axi_n1.v and mag.v already use.
    reg  [SW-1:0] scan_sel;
    reg           scan_any;
    integer       k;
    always @(*) begin
        scan_sel = {SW{1'b0}};
        scan_any = 1'b0;
        for (k = NSRC-1; k >= 0; k = k - 1) begin
            if (i_valid[(k + rr) % NSRC]) begin
                scan_sel = (k + rr) % NSRC;
                scan_any = 1'b1;
            end
        end
    end

    wire [SW-1:0] sel   = locked ? lock_sel : scan_sel;
    wire          any   = locked ? i_valid[lock_sel] : scan_any;
    wire          grant = any && skid_rdy;

    always @(posedge clk) begin
        if (rst) begin
            rr       <= {SW{1'b0}};
            locked   <= 1'b0;
            lock_sel <= {SW{1'b0}};
        end else if (grant) begin
            locked   <= !i_last[sel];
            lock_sel <= sel;
            if (i_last[sel]) begin
                rr <= (sel + 1'b1) % NSRC;
            end
        end
    end

    genvar g;
    generate
    for (g = 0; g < NSRC; g = g + 1) begin : g_irdy
        assign i_ready[g] = grant && (sel == g);
    end
    endgenerate

    // ------------------------------------------------------ registered path
    // NEVER `i_pay[sel*PW +: PW]`: a variable BIT offset builds a BARREL
    // SHIFTER, measured 14,632 LUT for two hubs against ~1,700 for the mux.

    // LUT-ONLY WIN. Transistor reference: on an ASIC both forms are the same
    // one-hot pass-gate mux, identical gate count. This divergence is FPGA-only.
    reg [DW-1:0] dst_sel;
    reg [PW-1:0] pay_sel;
    integer      j;
    always @(*) begin
        dst_sel = {DW{1'b0}};
        pay_sel = {PW{1'b0}};
        for (j = 0; j < NSRC; j = j + 1) begin
            if (sel == j) begin
                dst_sel = i_dst[j*DW +: DW];
                pay_sel = i_pay[j*PW +: PW];
            end
        end
    end

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

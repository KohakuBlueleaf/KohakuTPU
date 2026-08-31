// mag_link -- one full-duplex end of a MAG-to-MAG interlink, on the Kohaku
// Transmit Surface. The wire format and the device constraint, neither of
// which the code below states: a class is a virtual channel (0 stops at the
// peer, 1 the peer forwards); a packet is its 96-bit header zero-padded into
// one flit, then its beats, `last` on the final one; both ends must agree on
// LINK_W and RX_BEATS. A Laguna site IS a flip-flop, so the tool can only use
// one where the path is flop -> SLL -> flop -- hence no ready on the wire.

`default_nettype none

module mag_link #(
    parameter integer LINK_W    = 288,
    parameter integer TUSER_W   = 96,
    // Receive buffer per class, in flits, and therefore the credit the peer is
    // issued. A packet is MAX_BEATS+1 flits, so RX_BEATS must exceed that.
    parameter integer RX_BEATS  = 64,
    parameter integer CRD_BATCH = 4,
    parameter integer MAX_BEATS = 32,
    parameter integer CN_W      = 4,
    parameter integer CW        = $clog2(RX_BEATS) + 1
)(
    input  wire                 clk,
    input  wire                 resetn,

    // ---- local send: class 0 stops at the peer, class 1 is forwarded -------
    input  wire [TUSER_W-1:0]   tx0_hdr,
    input  wire                 tx0_hvalid,
    output wire                 tx0_hready,
    input  wire [LINK_W-1:0]    tx0_dat,
    input  wire                 tx0_dlast,
    input  wire                 tx0_dvalid,
    output wire                 tx0_dready,

    input  wire [TUSER_W-1:0]   tx1_hdr,
    input  wire                 tx1_hvalid,
    output wire                 tx1_hready,
    input  wire [LINK_W-1:0]    tx1_dat,
    input  wire                 tx1_dlast,
    input  wire                 tx1_dvalid,
    output wire                 tx1_dready,

    // ---- local receive, same two classes ----------------------------------
    output wire [TUSER_W-1:0]   rx0_hdr,
    output wire                 rx0_hvalid,
    input  wire                 rx0_hready,
    output wire [LINK_W-1:0]    rx0_dat,
    output wire                 rx0_dlast,
    output wire                 rx0_dvalid,
    input  wire                 rx0_dready,

    output wire [TUSER_W-1:0]   rx1_hdr,
    output wire                 rx1_hvalid,
    input  wire                 rx1_hready,
    output wire [LINK_W-1:0]    rx1_dat,
    output wire                 rx1_dlast,
    output wire                 rx1_dvalid,
    input  wire                 rx1_dready,

    // ---- the surface out: this is the SLR crossing -------------------------
    output wire                 o_valid,
    output wire                 o_vc,
    output wire                 o_last,
    output wire [LINK_W-1:0]    o_flit,
    input  wire                 o_crd_valid,
    input  wire                 o_crd_vc,
    input  wire [CN_W-1:0]      o_crd_n,

    // ---- the surface in ----------------------------------------------------
    input  wire                 i_valid,
    input  wire                 i_vc,
    input  wire                 i_last,
    input  wire [LINK_W-1:0]    i_flit,
    output wire                 i_crd_valid,
    output wire                 i_crd_vc,
    output wire [CN_W-1:0]      i_crd_n,

    // ---- counters, docs/interlink/boundary.md s3 --------------------------
    output wire [63:0]          ctr_tx,        // {beats, packets} sent
    output wire [63:0]          ctr_rx,        // {beats, packets} received
    output wire [63:0]          ctr_stall,     // {idle cycles, credit-stalled}
    output wire [31:0]          cred_state,
    output wire                 fault_len
);
    localparam integer VC = 2;

    wire rst = !resetn;

    // Send: the header flit, then the beats. Credit is per flit, so nothing
    // here prices a packet before offering it.
    wire [TUSER_W-1:0] tx_hdr   [0:VC-1];
    wire [LINK_W-1:0]  tx_dat   [0:VC-1];
    wire [VC-1:0]      tx_hvalid, tx_dvalid, tx_dlast;
    wire [VC-1:0]      tx_hready, tx_dready;

    assign tx_hdr[0]  = tx0_hdr;
    assign tx_hdr[1]  = tx1_hdr;
    assign tx_dat[0]  = tx0_dat;
    assign tx_dat[1]  = tx1_dat;
    assign tx_hvalid  = {tx1_hvalid, tx0_hvalid};
    assign tx_dvalid  = {tx1_dvalid, tx0_dvalid};
    assign tx_dlast   = {tx1_dlast,  tx0_dlast};
    assign tx0_hready = tx_hready[0];
    assign tx1_hready = tx_hready[1];
    assign tx0_dready = tx_dready[0];
    assign tx1_dready = tx_dready[1];

    reg  [VC-1:0]    s_dat;                  // 0: offering the header
    wire [VC-1:0]    req_valid, req_last, req_take;
    wire [VC*LINK_W-1:0] req_flit;
    wire [VC*CW-1:0] credits;

    genvar g;
    generate
    for (g = 0; g < VC; g = g + 1) begin : g_tx
        assign req_valid[g] = s_dat[g] ? tx_dvalid[g] : tx_hvalid[g];
        assign req_last[g]  = s_dat[g] && tx_dlast[g];
        // Only the header's own bits are selected: above TUSER_W the header
        // flit carries whatever the data lines hold, which the receiver never
        // reads, so 192 of the 288 bits are a wire instead of a mux.
        assign req_flit[g*LINK_W +: TUSER_W] =
            s_dat[g] ? tx_dat[g][TUSER_W-1:0] : tx_hdr[g];
        assign req_flit[g*LINK_W + TUSER_W +: LINK_W-TUSER_W] =
            tx_dat[g][LINK_W-1:TUSER_W];
        assign tx_hready[g] = !s_dat[g] && req_take[g];
        assign tx_dready[g] =  s_dat[g] && req_take[g];

        always @(posedge clk) begin
            if (rst) begin
                s_dat[g] <= 1'b0;
            end
            else if (req_take[g]) begin
                s_dat[g] <= !req_last[g];
            end
        end
    end
    endgenerate

    kts_tx #(.W(LINK_W), .VC(VC), .CMAX(RX_BEATS), .CN_W(CN_W)) u_tx (
        .clk(clk), .rst(rst),
        .req_valid(req_valid), .req_last(req_last), .req_flit(req_flit),
        .req_take(req_take),
        .tx_valid(o_valid), .tx_vc(o_vc), .tx_last(o_last), .tx_flit(o_flit),
        .crd_valid(o_crd_valid), .crd_vc(o_crd_vc), .crd_n(o_crd_n),
        .credits(credits)
    );

    // Receive: the head of a class is its header while between packets.
    wire [VC-1:0]        out_valid, out_last;
    wire [VC*LINK_W-1:0] out_flit;
    wire [VC-1:0]        out_pop;

    kts_rx #(.W(LINK_W), .VC(VC), .D(RX_BEATS), .CN_W(CN_W),
             .CRD_BATCH(CRD_BATCH), .MEM("block")) u_rx (
        .clk(clk), .rst(rst),
        .rx_valid(i_valid), .rx_vc(i_vc), .rx_last(i_last), .rx_flit(i_flit),
        .out_valid(out_valid), .out_last(out_last), .out_flit(out_flit),
        .out_pop(out_pop),
        .crd_valid(i_crd_valid), .crd_vc(i_crd_vc), .crd_n(i_crd_n)
    );

    reg  [VC-1:0] r_dat;
    wire [VC-1:0] rx_hvalid, rx_hready, rx_dvalid, rx_dready;

    assign rx_hready = {rx1_hready, rx0_hready};
    assign rx_dready = {rx1_dready, rx0_dready};

    generate
    for (g = 0; g < VC; g = g + 1) begin : g_rx
        assign rx_hvalid[g] = out_valid[g] && !r_dat[g];
        assign rx_dvalid[g] = out_valid[g] &&  r_dat[g];
        assign out_pop[g]   = r_dat[g] ? rx_dready[g] : rx_hready[g];

        always @(posedge clk) begin
            if (rst) begin
                r_dat[g] <= 1'b0;
            end
            else if (out_valid[g] && out_pop[g]) begin
                r_dat[g] <= !(r_dat[g] && out_last[g]);
            end
        end
    end
    endgenerate

    assign rx0_hdr    = out_flit[0*LINK_W +: TUSER_W];
    assign rx1_hdr    = out_flit[1*LINK_W +: TUSER_W];
    assign rx0_dat    = out_flit[0*LINK_W +: LINK_W];
    assign rx1_dat    = out_flit[1*LINK_W +: LINK_W];
    assign rx0_dlast  = out_last[0];
    assign rx1_dlast  = out_last[1];
    assign rx0_hvalid = rx_hvalid[0];
    assign rx1_hvalid = rx_hvalid[1];
    assign rx0_dvalid = rx_dvalid[0];
    assign rx1_dvalid = rx_dvalid[1];

    // A packet longer than the receiver is deep cannot be drained by a
    // consumer that waits for its last beat.
    localparam integer U_LEN = 16;

    wire [15:0] h0_len = tx0_hdr[U_LEN +: 16];
    wire [15:0] h1_len = tx1_hdr[U_LEN +: 16];
    wire        h0_bad = tx0_hvalid && (h0_len >= MAX_BEATS[15:0]);
    wire        h1_bad = tx1_hvalid && (h1_len >= MAX_BEATS[15:0]);

    reg fault_len_r, fl_said;
    always @(posedge clk) begin
        if (rst) begin
            fault_len_r <= 1'b0;
        end
        else if (h0_bad || h1_bad) begin
            fault_len_r <= 1'b1;
        end
    end
    assign fault_len = fault_len_r;

    // Starved and credit-stalled look identical from a packet count and need
    // opposite fixes, so both cycles are counted.
    wire [VC-1:0] has_cred;
    generate
    for (g = 0; g < VC; g = g + 1) begin : g_hc
        assign has_cred[g] = (credits[g*CW +: CW] != {CW{1'b0}});
    end
    endgenerate

    wire want_send = |req_valid;
    wire can_send  = |(req_valid & has_cred);
    wire tx_beat   = |(req_take & s_dat);
    wire tx_pkt    = |(req_take & req_last);
    wire rx_beat   = |(out_valid & out_pop & r_dat);
    wire rx_pkt    = |(out_valid & out_pop & r_dat & out_last);

    reg [31:0] n_tx_pkt, n_tx_beat, n_rx_pkt, n_rx_beat, n_stall, n_idle;

    always @(posedge clk) begin
        if (rst) begin
            n_tx_pkt <= 32'd0; n_tx_beat <= 32'd0;
            n_rx_pkt <= 32'd0; n_rx_beat <= 32'd0;
            n_stall  <= 32'd0; n_idle    <= 32'd0;
        end
        else begin
            if (tx_beat) begin
                n_tx_beat <= n_tx_beat + 32'd1;
            end
            if (tx_pkt) begin
                n_tx_pkt <= n_tx_pkt + 32'd1;
            end
            if (rx_beat) begin
                n_rx_beat <= n_rx_beat + 32'd1;
            end
            if (rx_pkt) begin
                n_rx_pkt <= n_rx_pkt + 32'd1;
            end
            if (want_send && !can_send) begin
                n_stall <= n_stall + 32'd1;
            end
            if (!want_send) begin
                n_idle <= n_idle + 32'd1;
            end
        end
    end

    assign ctr_tx     = {n_tx_beat, n_tx_pkt};
    assign ctr_rx     = {n_rx_beat, n_rx_pkt};
    assign ctr_stall  = {n_idle, n_stall};
    assign cred_state = {{(16-CW){1'b0}}, credits[1*CW +: CW],
                         {(16-CW){1'b0}}, credits[0*CW +: CW]};

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (resetn && fault_len_r && !fl_said) begin
            fl_said <= 1'b1;
            $display("%0t ERROR mag_link: a packet of %0d beats was offered; MAX_BEATS is %0d and the receiver holds %0d flits.",
                     $time, h0_bad ? h0_len : h1_len, MAX_BEATS, RX_BEATS);
        end
        if (!resetn) begin
            fl_said <= 1'b0;
        end
    end
`endif

endmodule

`default_nettype wire

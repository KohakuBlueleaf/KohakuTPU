// L2 staging in a NoC local link: NoCRouter <-> l2_adapter <-> endpoint.
// Same six-signal interface on both faces, so PASS=1 is a straight wire.

// 256-bit line: a MEM_RD_RESP payload IS 256 bits and a local port sends one
// flit per cycle, so a wider line adds a mux and buys nothing. MAG differs.

`default_nettype none

module l2_adapter #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer DEPTH      = 4096,   // lines; 4 URAM per 4096
    parameter [33:0]  L2_BASE    = 34'h2_0000_0000,
    parameter integer L2_BITS    = 22,     // byte-address bits below the base
    parameter integer PASS       = 0
)(
    input  wire                   clk,
    input  wire                   rst,

    // ---- router side ----
    input  wire [FLIT_WIDTH-1:0]  rt_data,     // router -> here
    input  wire                   rt_valid,
    output wire                   rt_busy,
    output wire [FLIT_WIDTH-1:0]  ru_data,     // here -> router
    output wire                   ru_valid,
    input  wire                   ru_busy,

    // ---- endpoint side ----
    output wire [FLIT_WIDTH-1:0]  ep_data,     // here -> endpoint
    output wire                   ep_valid,
    input  wire                   ep_busy,
    input  wire [FLIT_WIDTH-1:0]  eu_data,     // endpoint -> here
    input  wire                   eu_valid,
    output wire                   eu_busy
);
    localparam [3:0] T_MEM_RD_REQ  = 4'h0,
                     T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2,
                     T_MEM_WR_DATA = 4'h4;

    localparam integer AW = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    generate if (PASS != 0) begin : g_bypass

        assign ep_data = rt_data;  assign ep_valid = rt_valid;
        assign rt_busy = ep_busy;
        assign ru_data = eu_data;  assign ru_valid = eu_valid;
        assign eu_busy = ru_busy;

    end else begin : g_l2

    // ---- header taps, matching noc_pkt.vh ----------------------------------
    wire [3:0]  u_ty  = eu_data[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire [7:0]  u_tag = eu_data[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];
    wire [33:0] u_adr = eu_data[255:222];
    wire [7:0]  u_cnt = eu_data[199:192];

    // One compare on the top bits. The whole address decode is this line.
    wire u_isL2 = (u_adr[33:L2_BITS] == L2_BASE[33:L2_BITS]);
    wire u_rd   = eu_valid && (u_ty == T_MEM_RD_REQ) && u_isL2;
    wire u_wr   = eu_valid && (u_ty == T_MEM_WR_REQ) && u_isL2;

    // ---- store: 256b line, one flit payload per read, no mux ---------------
    reg  [AW-1:0]  wr_addr, rd_addr;
    reg            wr_en;
    reg  [255:0]   wr_data;
    wire [255:0]   rd_data;

    kohaku_sdpram #(.WIDTH(256), .DEPTH(DEPTH), .MEM_PRIM("ultra"),
                    .READ_LAT(2)) u_ram (
        .clk(clk),
        .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data),
        .rd_en(1'b1),  .rd_addr(rd_addr), .rd_data(rd_data)
    );

    // ---- read engine -------------------------------------------------------
    // No reorder buffer: the CU tags responses {entry, word} and reassembles in
    // any order (mx_cluster_cu.v, fill path).
    reg          run;
    reg  [7:0]   left, tag_r;
    reg  [AW-1:0] cur;
    reg  [POS_WIDTH-1:0] dx_r, dy_r, sx_r, sy_r;
    reg  [1:0]   word_r;

    // READ_LAT=2: the valid walks beside the line rather than stalling for it.
    reg  [2:0]   vld_sr;
    reg  [1:0]   wsr [0:2];
    reg  [7:0]   tsr [0:2];

    wire emit_ok = !ru_busy;

    integer i;
    always @(posedge clk) begin
        if (rst) begin
            run <= 1'b0; left <= 8'd0; cur <= {AW{1'b0}};
            vld_sr <= 3'd0; word_r <= 2'd0;
            wr_en <= 1'b0;
        end else begin
            wr_en  <= 1'b0;
            vld_sr <= {vld_sr[1:0], 1'b0};
            for (i = 2; i > 0; i = i - 1) begin
                wsr[i] <= wsr[i-1]; tsr[i] <= tsr[i-1];
            end

            if (u_wr) begin
                wr_en   <= 1'b1;
                wr_addr <= u_adr[L2_BITS-1 -: AW];
                wr_data <= eu_data[255:0];
            end

            if (!run && u_rd) begin
                run    <= 1'b1;
                left   <= (u_cnt == 8'd0) ? 8'd1 : u_cnt;
                cur    <= u_adr[L2_BITS-1 -: AW];
                tag_r  <= u_tag;
                word_r <= 2'd0;
                // Reply to the requester: swap source and destination.
                dx_r <= eu_data[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
                dy_r <= eu_data[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];
                sx_r <= eu_data[FLIT_WIDTH-1              -: POS_WIDTH];
                sy_r <= eu_data[FLIT_WIDTH-POS_WIDTH-1    -: POS_WIDTH];
            end else if (run && emit_ok) begin
                rd_addr   <= cur;
                vld_sr[0] <= 1'b1;
                wsr[0]    <= word_r;
                tsr[0]    <= tag_r;
                cur       <= cur + {{(AW-1){1'b0}}, 1'b1};
                word_r    <= word_r + 2'd1;
                left      <= left - 8'd1;
                if (left == 8'd1) run <= 1'b0;
            end
        end
    end

    wire hit_out = vld_sr[2];

    wire [FLIT_WIDTH-1:0] resp =
        { dx_r, dy_r, sx_r, sy_r, T_MEM_RD_RESP, tsr[2],
          1'b0, 2'b00, wsr[2], rd_data };

    // ---- downstream: router flit, or a local response ----------------------
    // This 288-bit 2:1 is the adapter's only wide mux and its main LUT cost.
    assign ep_data  = hit_out ? resp : rt_data;
    assign ep_valid = hit_out | rt_valid;
    assign rt_busy  = ep_busy | hit_out;      // local response wins the cycle

    // ---- upstream: swallow what we served, forward the rest ----------------
    assign ru_data  = eu_data;
    assign ru_valid = eu_valid && !(u_rd || u_wr);
    assign eu_busy  = ru_busy && !(u_rd || u_wr);

    end endgenerate
endmodule

`default_nettype wire

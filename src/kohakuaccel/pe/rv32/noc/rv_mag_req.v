// rv_mag_req -- rv_l1 straight onto MAG's internal converged path.
//
// The MAG-resident control processor's memory channel. It replaces rv_noc_req
// entirely: no flits, no transaction tags, no agent write slots, because the
// processor is INSIDE MAG and reaches mag_stage_port/mag_dram_port through one
// more requester (docs/arch/sysnode/control-processor.md s3.1).
//
// Widths already agree -- rv_l1's line is 32 bytes and MAG's internal word is
// DATA_W=256 -- so this is a handshake adapter, not a protocol engine.
//
// L2 STAGING NEEDS NOTHING HERE: mag_stage_port claims by address, so a segment
// carrying a[39] with aperture 0 is served from URAM and one without goes to
// DRAM. The store is reached by naming it, not by a second port.

`default_nettype none

module rv_mag_req #(
    parameter integer ADDR_W = 40,
    parameter integer DATA_W = 256
)(
    input  wire                 clk,
    input  wire                 resetn,

    // ---- line fill, from the internal L1 ----
    input  wire                 fill_valid,
    output wire                 fill_ready,
    input  wire [30:0]          fill_addr,
    output reg                  resp_valid,
    output reg  [DATA_W-1:0]    resp_data,

    // ---- dirty writeback, from the internal L1 ----
    input  wire                 wb_valid,
    output wire                 wb_ready,
    input  wire [30:0]          wb_addr,
    input  wire [DATA_W-1:0]    wb_data,

    // ---- the segment file: the top bits a 32-bit core cannot name ----
    // Four windows selected by addr[30:29]; each supplies phys[39:31], which is
    // {special, rsvd, mesh[1:0], aperture[3:0]} plus the bit above the window.
    input  wire                 seg_we,
    input  wire [1:0]           seg_idx,
    input  wire [8:0]           seg_val,

    // ---- MAG's internal requester channel ----
    output wire [ADDR_W-1:0]    cp_awaddr,
    output wire [7:0]           cp_awlen,
    output wire                 cp_awvalid,
    input  wire                 cp_awready,
    output wire [DATA_W-1:0]    cp_wdata,
    output wire [DATA_W/8-1:0]  cp_wstrb,
    output wire                 cp_wlast,
    output wire                 cp_wvalid,
    input  wire                 cp_wready,
    input  wire                 cp_bvalid,
    output wire                 cp_bready,
    output wire [ADDR_W-1:0]    cp_araddr,
    output wire [7:0]           cp_arlen,
    output wire                 cp_arvalid,
    input  wire                 cp_arready,
    input  wire [DATA_W-1:0]    cp_rdata,
    input  wire                 cp_rlast,
    input  wire                 cp_rvalid,
    output wire                 cp_rready,

    output wire [15:0]          wr_out,
    output wire                 idle
);
    reg [8:0] seg [0:3];
    integer   s;
    always @(posedge clk) begin
        if (!resetn) begin
            for (s = 0; s < 4; s = s + 1) begin
                seg[s] <= 9'd0;
            end
        end
        else if (seg_we) begin
            seg[seg_idx] <= seg_val;
        end
    end

    function [ADDR_W-1:0] phys;
        input [30:0]  a;
        input [8:0]   sg;
        begin phys = {sg, a}; end
    endfunction

    wire [8:0] fill_seg = seg[fill_addr[30:29]];
    wire [8:0] wb_seg   = seg[wb_addr[30:29]];

    // ---- reads: one outstanding, which is rv_l1's whole miss model ---------
    reg              rd_pend;
    reg              ar_v;
    reg [ADDR_W-1:0] ar_a;

    assign cp_araddr  = ar_a;
    assign cp_arlen   = 8'd0;
    assign cp_arvalid = ar_v;
    assign cp_rready  = 1'b1;

    assign fill_ready = !rd_pend && !ar_v;

    // ---- writes: one burst in flight, B counted for flush-all -------------
    reg              aw_v, w_v;
    reg [ADDR_W-1:0] aw_a;
    reg [DATA_W-1:0] w_d;
    reg [15:0]       n_out;

    assign cp_awaddr  = aw_a;
    assign cp_awlen   = 8'd0;
    assign cp_awvalid = aw_v;
    assign cp_wdata   = w_d;
    assign cp_wstrb   = {(DATA_W/8){1'b1}};
    assign cp_wlast   = 1'b1;
    assign cp_wvalid  = w_v;
    assign cp_bready  = 1'b1;

    assign wb_ready = !aw_v && !w_v;
    assign wr_out   = n_out;
    assign idle     = !rd_pend && !ar_v && !aw_v && !w_v && (n_out == 16'd0);

    always @(posedge clk) begin
        if (!resetn) begin
            rd_pend <= 1'b0; ar_v <= 1'b0;
            aw_v <= 1'b0; w_v <= 1'b0; n_out <= 16'd0;
            resp_valid <= 1'b0;
        end else begin
            resp_valid <= 1'b0;

            if (ar_v && cp_arready) begin
                ar_v <= 1'b0;
            end
            if (aw_v && cp_awready) begin
                aw_v <= 1'b0;
            end
            if (w_v  && cp_wready) begin
                w_v  <= 1'b0;
            end

            if (fill_valid && fill_ready) begin
                ar_a    <= phys(fill_addr, fill_seg);
                ar_v    <= 1'b1;
                rd_pend <= 1'b1;
            end

            if (rd_pend && cp_rvalid) begin
                resp_data  <= cp_rdata;
                resp_valid <= 1'b1;
                rd_pend    <= 1'b0;
            end

            // Counted at ACCEPT, not at B: rv_l1 releases the writeback on this
            // cycle, so a count that lags leaves a window where flush-all sees
            // zero outstanding and stops one line short.
            if (wb_valid && wb_ready) begin
                aw_a  <= phys(wb_addr, wb_seg);
                aw_v  <= 1'b1;
                w_d   <= wb_data;
                w_v   <= 1'b1;
                n_out <= n_out + 16'd1;
            end else if (cp_bvalid && (n_out != 16'd0)) begin
                n_out <= n_out - 16'd1;
            end
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        if (cp_rvalid && !rd_pend) begin
            $display("%0t ERROR rv_mag_req: read data with no fill outstanding",
                     $time);
        end
        if (cp_bvalid && (n_out == 16'd0)) begin
            $display("%0t ERROR rv_mag_req: B with no write outstanding", $time);
        end
    end
`endif
endmodule

`default_nettype wire

// kht_req -- the SIMT PE's NoC requestor: line fills with a tag each, and dirty
// line writes, as MEM_RD_REQ / MEM_WR_REQ + MEM_WR_DATA.
//
// rv_noc_req's write side and descriptor shapes, with one change: a fill
// carries its own `txn`, so as many as the LSU has fill buffers are in flight
// at once and the response names its buffer (memory-protocol.md s3.2.1). A fill
// is a one-entry streaming read, one flit back, inside the LSU's MSHRS budget.
//
// WR_OUT UN-ACKNOWLEDGED WRITES. The protocol allows one OPEN write per source
// (s4.1: descriptor sent, data incomplete); a one-flit write closes the cycle
// after its descriptor, so several can wait for their acks. Each carries txn
// 0x20 + its slot and its ack frees that slot. Nothing orders a read against a
// write (s7.2): a fill waits while any un-acked write carries its line, and a
// write waits while another un-acked write carries the same line.

`default_nettype none

module kht_req #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer POS_X      = 1,
    parameter integer POS_Y      = 1,
    parameter integer MEM_X      = 0,
    parameter integer MEM_Y      = 1,
    parameter [39:0]  DRAM_BASE  = 40'h00_0000_0000,
    // Un-acknowledged writes, 1..16. A write-back flush ends one ack latency
    // per WR_OUT lines: 1 is ~50 cycles a dirty line on kht_sys, 8 is ~7.
    parameter integer WR_OUT     = 1
)(
    input  wire                   clk,
    input  wire                   resetn,

    input  wire                   rd_v,
    output wire                   rd_ready,
    input  wire [25:0]            rd_line,
    input  wire [7:0]             rd_tag,
    output reg                    rsp_v,
    output reg  [7:0]             rsp_tag,
    output reg  [255:0]           rsp_data,

    input  wire                   wr_v,
    output wire                   wr_ready,
    input  wire [25:0]            wr_line,
    input  wire [255:0]           wr_data,

    output reg  [FLIT_WIDTH-1:0]  send_flit,
    output reg                    send_valid,
    input  wire                   send_ready,

    input  wire                   rx_rd_resp,
    input  wire [7:0]             rx_txn,
    input  wire [255:0]           rx_data,
    input  wire                   rx_wr_ack,

    output wire [15:0]            wr_out,
    output wire                   idle
);
    localparam [3:0] T_MEM_RD_REQ = 4'h0, T_MEM_WR_REQ = 4'h1, T_MEM_WR_DATA = 4'h4;
    localparam integer PAY = FLIT_WIDTH - 4*POS_WIDTH - 16;
    localparam [7:0] F_STREAM = 8'h40;
    localparam [7:0] WTX      = 8'h20;
    localparam integer WN     = (WR_OUT < 1) ? 1 : WR_OUT;
    localparam integer WSW    = (WN > 1) ? $clog2(WN) : 1;

    localparam S_IDLE = 1'b0, S_WDATA = 1'b1;
    reg          st;
    reg [15:0]   n_out;
    reg [WN-1:0] w_v;
    reg [25:0]   w_ln [0:WN-1];
    reg [255:0]  hold_data;
    reg [FLIT_WIDTH-1:0] hold_desc;

    assign wr_out = n_out;
    assign idle   = (st == S_IDLE) && !send_valid;

    // A slot's line is written in the cycle its descriptor is taken, so a line
    // still in S_WDATA is already in the table.
    reg           rd_hit, wr_hit, w_free_f;
    reg [WSW-1:0] w_free;
    integer i;
    always @(*) begin
        rd_hit   = 1'b0;
        wr_hit   = 1'b0;
        w_free_f = 1'b0;
        w_free   = {WSW{1'b0}};
        for (i = WN - 1; i >= 0; i = i - 1) begin
            if (w_v[i] && (w_ln[i] == rd_line)) rd_hit = 1'b1;
            if (w_v[i] && (w_ln[i] == wr_line)) wr_hit = 1'b1;
            if (!w_v[i]) begin
                w_free_f = 1'b1;
                w_free   = i[WSW-1:0];
            end
        end
    end

    wire tx_free  = !send_valid || send_ready;
    wire take_rd  = (st == S_IDLE) && tx_free && rd_v && !rd_hit;
    wire take_wr  = (st == S_IDLE) && tx_free && wr_v && !take_rd && w_free_f && !wr_hit;
    assign rd_ready = take_rd;
    assign wr_ready = take_wr;

    wire [39:0] rd_phys = DRAM_BASE | {9'd0, rd_line, 5'd0};
    wire [39:0] wr_phys = DRAM_BASE | {9'd0, wr_line, 5'd0};
    wire [7:0]  wr_txn  = WTX + {{(8-WSW){1'b0}}, w_free};

    function [FLIT_WIDTH-1:0] hdr;
        input [3:0]     ty;
        input [7:0]     txn;
        input           lst;
        input [PAY-1:0] pay;
        hdr = {MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
               POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
               ty, txn, lst, 3'd0, pay};
    endfunction

    // The MEM_RD_REQ / MEM_WR_REQ descriptor, rv_noc_req's field for field.
    function [PAY-1:0] descr;
        input [39:0] a;
        input [7:0]  flags;
        input [7:0]  count;
        input [7:0]  ewords;
        descr = {a, 8'd0, flags, count, 24'd0, 2'd0, ewords, 158'd0};
    endfunction

    wire [7:0] ack_i  = rx_txn - WTX;
    wire       ack_ok = rx_wr_ack && (ack_i < WN);

    always @(posedge clk) begin
        if (!resetn) begin
            st         <= S_IDLE;
            send_valid <= 1'b0;
            n_out      <= 16'd0;
            rsp_v      <= 1'b0;
            w_v        <= {WN{1'b0}};
        end else begin
            if (send_valid && send_ready) send_valid <= 1'b0;

            rsp_v    <= rx_rd_resp;
            rsp_tag  <= rx_txn;
            rsp_data <= rx_data;

            if (ack_ok) w_v[ack_i[WSW-1:0]] <= 1'b0;

            case (st)
                S_IDLE: if (take_rd) begin
                    send_flit  <= hdr(T_MEM_RD_REQ, rd_tag, 1'b0,
                                      descr(rd_phys, F_STREAM, 8'd1, 8'd1));
                    send_valid <= 1'b1;
                end else if (take_wr) begin
                    send_flit    <= hdr(T_MEM_WR_REQ, wr_txn, 1'b0,
                                        descr(wr_phys, 8'd0, 8'd0, 8'd0));
                    send_valid   <= 1'b1;
                    w_v[w_free]  <= 1'b1;
                    w_ln[w_free] <= wr_line;
                    hold_data    <= wr_data;
                    hold_desc    <= hdr(T_MEM_WR_DATA, wr_txn, 1'b1, {PAY{1'b0}});
                    st           <= S_WDATA;
                end

                // The data flit follows its descriptor with nothing between.
                S_WDATA: if (tx_free) begin
                    send_flit  <= {hold_desc[FLIT_WIDTH-1:PAY], hold_data};
                    send_valid <= 1'b1;
                    st         <= S_IDLE;
                end
            endcase

            // Counted when the descriptor is taken, so a flush that polls this
            // never sees zero while a write is still on its way out.
            n_out <= n_out + {15'd0, take_wr}
                   - {15'd0, (rx_wr_ack && (n_out != 16'd0))};
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (resetn) begin
        if (rx_wr_ack && (n_out == 16'd0)) begin
            $display("%0t ERROR kht_req(%0d,%0d): MEM_WR_ACK with no write outstanding",
                     $time, POS_X, POS_Y);
        end
        if (rx_wr_ack && !(ack_ok && w_v[ack_i[WSW-1:0]])) begin
            $display("%0t ERROR kht_req(%0d,%0d): MEM_WR_ACK txn %0h names no open write",
                     $time, POS_X, POS_Y, rx_txn);
        end
    end
`endif

endmodule

`default_nettype wire

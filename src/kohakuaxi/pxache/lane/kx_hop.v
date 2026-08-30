// kx_hop -- one valid/ready channel across ONE partition boundary on one
// clock, with nothing combinational on either direction of the crossing.
// The sender adds a register whose only load is the boundary wire (the
// crossing's TX register); the receiver lands it in its buffer's RAM, whose
// write port is itself a register (RX_REG=0, the default: a 3-cycle hop), or
// in a register in front of that RAM (RX_REG=1: 4 cycles, for a placement
// that wants a flop at both ends of the wire); the receiver's pop comes back
// the same way as a registered pulse that the sender counts as credit -- no
// ready ever travels back. A skid on the far side would put a 2:1 in front
// of the landing; a ready seen two cycles late would overflow it, so the
// credit is the flow control. The Kohaku Partitioned Xache builds its lanes
// from these; each half sits in its own partition and takes that partition's
// reset. Same clock, no gray coding: credit round trip 3, so DEPTH >= 4
// streams a beat per cycle.
//
// BUF selects the landing buffer: "xpm" (xpm_fifo_sync, FWFT) or "lean" (a
// ring the credits keep from overflowing: no full flag, no FWFT machinery).

`default_nettype none

module kx_hop_tx #(
    parameter integer WIDTH = 64,
    parameter integer DEPTH = 16
)(
    input  wire             clk,
    input  wire             rstn,
    input  wire             s_valid,
    output wire             s_ready,
    input  wire [WIDTH-1:0] s_data,
    // across the boundary
    output reg              tx_v,
    output reg  [WIDTH-1:0] tx_d,
    input  wire             pp,          // a pop, from the receiving partition
    input  wire             fok_rx       // its buffer is out of reset
);
    localparam integer CW = $clog2(DEPTH + 1);
    reg [CW-1:0] credit;
    reg          cr, fok;                // the two landing registers
    wire         send = s_valid && (credit != 0) && fok;
    assign s_ready = (credit != 0) && fok;
    always @(posedge clk) begin
        cr   <= pp && rstn;
        fok  <= fok_rx && rstn;
        tx_v <= send && rstn;
        if (send) begin tx_d <= s_data; end
        if (!rstn) begin credit <= DEPTH[CW-1:0]; end
        else       begin credit <= credit - (send ? 1'b1 : 1'b0) + (cr ? 1'b1 : 1'b0); end
    end
endmodule

module kx_hop_rx #(
    parameter integer WIDTH = 64,
    parameter integer DEPTH = 16,
    parameter         MEM   = "distributed",
    parameter         BUF   = "xpm",
    parameter integer FASTW = 0,
    parameter integer RX_REG = 0
)(
    input  wire             clk,
    input  wire             rstn,
    // across the boundary
    input  wire             tx_v,
    input  wire [WIDTH-1:0] tx_d,
    output reg              pp,
    output reg              fok_rx,

    output wire             m_valid,
    input  wire             m_ready,
    output wire [WIDTH-1:0] m_data
);
    // the landing: the RAM's write port registers WE/ADDR/DIN at the edge, so
    // the wire lands there; RX_REG=1 puts a flop in front of it (+1 cycle)
    wire             rx_v;
    wire [WIDTH-1:0] rx_d;
    generate if (RX_REG != 0) begin : g_rxreg
        reg              rx_v_q;
        reg  [WIDTH-1:0] rx_d_q;
        always @(posedge clk) begin
            rx_v_q <= tx_v && rstn;
            rx_d_q <= tx_d;
        end
        assign rx_v = rx_v_q;
        assign rx_d = rx_d_q;
    end else begin : g_rxwire
        assign rx_v = tx_v && rstn;
        assign rx_d = tx_d;
    end endgenerate
    wire wr_busy, rd_busy;
    generate if (BUF == "lean") begin : g_lean
        kx_hop_ring #(.WIDTH(WIDTH), .DEPTH(DEPTH), .MEM(MEM), .FASTW(FASTW)) u_f (
            .clk(clk), .rstn(rstn),
            .wr_en(rx_v), .wr_data(rx_d), .wr_busy(wr_busy),
            .rd_en(m_valid && m_ready), .rd_data(m_data), .rd_busy(rd_busy));
    end else begin : g_xpm
        sync_fifo #(.DATA_WIDTH(WIDTH), .FIFO_DEPTH(DEPTH), .MEMORY_TYPE(MEM)) u_f (
            .clk(clk), .rst(~rstn),
            .wr_en(rx_v), .wr_data(rx_d), .wr_busy(wr_busy), .wr_almost(),
            .rd_en(m_valid && m_ready), .rd_data(m_data), .rd_busy(rd_busy));
    end endgenerate
    assign m_valid = !rd_busy;
    always @(posedge clk) begin
        pp     <= m_valid && m_ready && rstn;
        fok_rx <= !wr_busy && rstn;
    end
endmodule

// The lean landing buffer: a DEPTH-entry ring the sender's credits keep from
// overflowing, so it has no full flag. One read stage (kohaku_sdpram,
// READ_LAT 1): a word is issued when the output is free or being taken, so
// the stream never bubbles; the primitive holds rd_data until the next read.
// The top FASTW bits (a lane's destination and kind) come out of distributed
// RAM: what decodes the head must not wait a block RAM's 0.83 ns clock-to-out.
module kx_hop_ring #(
    parameter integer WIDTH = 64,
    parameter integer DEPTH = 16,
    parameter         MEM   = "distributed",
    parameter integer FASTW = 0
)(
    input  wire             clk,
    input  wire             rstn,
    input  wire             wr_en,
    input  wire [WIDTH-1:0] wr_data,
    output wire             wr_busy,
    input  wire             rd_en,
    output wire [WIDTH-1:0] rd_data,
    output wire             rd_busy
);
    localparam integer AW = (DEPTH <= 1) ? 1 : $clog2(DEPTH);
    reg [AW-1:0] wp, rp;
    reg [AW:0]   cnt;                    // words written and not yet issued
    reg          o_v;                    // rd_data holds a word
    wire o_take = o_v && rd_en;
    wire issue  = (cnt != 0) && (!o_v || o_take);
    generate if (FASTW > 0 && FASTW < WIDTH) begin : g_split
        kohaku_sdpram #(.WIDTH(WIDTH - FASTW), .DEPTH(DEPTH), .MEM_PRIM(MEM), .READ_LAT(1)) u_m (
            .clk(clk),
            .wr_en(wr_en), .wr_addr(wp), .wr_data(wr_data[WIDTH-FASTW-1:0]),
            .rd_en(issue), .rd_addr(rp), .rd_data(rd_data[WIDTH-FASTW-1:0]));
        kohaku_sdpram #(.WIDTH(FASTW), .DEPTH(DEPTH), .MEM_PRIM("distributed"), .READ_LAT(1)) u_f (
            .clk(clk),
            .wr_en(wr_en), .wr_addr(wp), .wr_data(wr_data[WIDTH-1 -: FASTW]),
            .rd_en(issue), .rd_addr(rp), .rd_data(rd_data[WIDTH-1 -: FASTW]));
    end else begin : g_one
        kohaku_sdpram #(.WIDTH(WIDTH), .DEPTH(DEPTH), .MEM_PRIM(MEM), .READ_LAT(1)) u_m (
            .clk(clk),
            .wr_en(wr_en), .wr_addr(wp), .wr_data(wr_data),
            .rd_en(issue), .rd_addr(rp), .rd_data(rd_data));
    end endgenerate
    always @(posedge clk) begin
        if (!rstn) begin
            wp <= 0; rp <= 0; cnt <= 0; o_v <= 1'b0;
        end else begin
            if (wr_en) begin wp <= wp + 1'b1; end
            if (issue) begin rp <= rp + 1'b1; end
            cnt <= cnt + (wr_en ? 1'b1 : 1'b0) - (issue ? 1'b1 : 1'b0);
            o_v <= issue || (o_v && !o_take);
        end
    end
    assign rd_busy = !o_v;
    assign wr_busy = !rstn;
endmodule

module kx_hop #(
    parameter integer WIDTH = 64,
    parameter integer DEPTH = 16,
    parameter         MEM   = "distributed",
    parameter         BUF   = "xpm",
    parameter integer FASTW = 0,
    parameter integer RX_REG = 0
)(
    input  wire             clk,
    input  wire             s_rstn,             // the sending partition's reset
    input  wire             m_rstn,             // the receiving partition's reset

    input  wire             s_valid,
    output wire             s_ready,
    input  wire [WIDTH-1:0] s_data,

    output wire             m_valid,
    input  wire             m_ready,
    output wire [WIDTH-1:0] m_data
);
    wire             tx_v, pp, fok_rx;
    wire [WIDTH-1:0] tx_d;
    kx_hop_tx #(.WIDTH(WIDTH), .DEPTH(DEPTH)) u_tx (
        .clk(clk), .rstn(s_rstn), .s_valid(s_valid), .s_ready(s_ready), .s_data(s_data),
        .tx_v(tx_v), .tx_d(tx_d), .pp(pp), .fok_rx(fok_rx));
    kx_hop_rx #(.WIDTH(WIDTH), .DEPTH(DEPTH), .MEM(MEM), .BUF(BUF), .FASTW(FASTW),
                .RX_REG(RX_REG)) u_rx (
        .clk(clk), .rstn(m_rstn), .tx_v(tx_v), .tx_d(tx_d), .pp(pp), .fok_rx(fok_rx),
        .m_valid(m_valid), .m_ready(m_ready), .m_data(m_data));
endmodule

`default_nettype wire

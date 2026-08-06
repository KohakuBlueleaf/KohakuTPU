// AXI4-Full master. Reference implementation -- this is the shape MAS needs.
//
// Takes one command (address, byte length, read/write) and turns it into as many
// legal AXI bursts as required. The three rules that make burst generation
// non-trivial, and that a hand-rolled master usually gets wrong:
//
//   1. A burst MUST NOT cross a 4 KB boundary (AXI4 A3.4.1). Interconnects are
//      permitted to do arbitrarily bad things if you do -- SmartConnect will
//      split or stall, other fabrics corrupt.
//   2. AWLEN/ARLEN max is 255 (256 beats).
//   3. WLAST must be asserted on the last beat of EVERY burst, not just the last
//      burst of the command.
//
// Discipline matches src/kohakuaxi/axi4_ram.v: VALID is never a function of READY,
// and a burst's length comes from a counter rather than from the data stream.
//
// One burst outstanding at a time. That is deliberate for a reference: it makes
// the FSM readable and provably correct. It is also the main thing to change for
// MAS, where DRAM latency means single-outstanding leaves most of the bandwidth
// on the floor -- see docs/noc/spec.md s10.5.

`default_nettype none

module axi4_master #(
    parameter DATA_WIDTH = 64,
    parameter ADDR_WIDTH = 64,
    parameter ID_WIDTH   = 4,
    parameter AXI_ID     = 0
) (
    input  wire                    clk,
    input  wire                    resetn,

    // ---- command ----
    input  wire [ADDR_WIDTH-1:0]   cmd_addr,     // must be beat-aligned
    input  wire [31:0]             cmd_beats,    // total beats, >= 1
    input  wire                    cmd_write,
    input  wire                    cmd_valid,
    output wire                    cmd_ready,
    output reg                     cmd_done,     // one-cycle pulse
    output reg  [1:0]              cmd_resp,     // worst response seen

    // ---- write data in ----
    input  wire [DATA_WIDTH-1:0]   wr_data,
    input  wire [DATA_WIDTH/8-1:0] wr_strb,
    input  wire                    wr_valid,
    output wire                    wr_ready,

    // ---- read data out ----
    output wire [DATA_WIDTH-1:0]   rd_data,
    output wire                    rd_valid,
    input  wire                    rd_ready,

    // ---- AXI4 master ----
    output wire [ID_WIDTH-1:0]     m_axi_awid,
    output wire [ADDR_WIDTH-1:0]   m_axi_awaddr,
    output wire [7:0]              m_axi_awlen,
    output wire [2:0]              m_axi_awsize,
    output wire [1:0]              m_axi_awburst,
    output wire                    m_axi_awvalid,
    input  wire                    m_axi_awready,
    output wire [DATA_WIDTH-1:0]   m_axi_wdata,
    output wire [DATA_WIDTH/8-1:0] m_axi_wstrb,
    output wire                    m_axi_wlast,
    output wire                    m_axi_wvalid,
    input  wire                    m_axi_wready,
    input  wire [ID_WIDTH-1:0]     m_axi_bid,
    input  wire [1:0]              m_axi_bresp,
    input  wire                    m_axi_bvalid,
    output wire                    m_axi_bready,
    output wire [ID_WIDTH-1:0]     m_axi_arid,
    output wire [ADDR_WIDTH-1:0]   m_axi_araddr,
    output wire [7:0]              m_axi_arlen,
    output wire [2:0]              m_axi_arsize,
    output wire [1:0]              m_axi_arburst,
    output wire                    m_axi_arvalid,
    input  wire                    m_axi_arready,
    input  wire [ID_WIDTH-1:0]     m_axi_rid,
    input  wire [DATA_WIDTH-1:0]   m_axi_rdata,
    input  wire [1:0]              m_axi_rresp,
    input  wire                    m_axi_rlast,
    input  wire                    m_axi_rvalid,
    output wire                    m_axi_rready
);

    localparam BYTES  = DATA_WIDTH/8;
    localparam LSB    = $clog2(BYTES);
    localparam [2:0]  SIZE = LSB[2:0];
    localparam [1:0]  INCR = 2'b01;

    localparam [2:0] S_IDLE  = 3'd0,
                     S_WADDR = 3'd1, S_WDATA = 3'd2, S_WRESP = 3'd3,
                     S_RADDR = 3'd4, S_RDATA = 3'd5;
    reg [2:0] state;

    reg [ADDR_WIDTH-1:0] addr;
    reg [31:0]           beats_left;   // beats remaining in the whole command
    reg [8:0]            burst_beats;  // beats remaining in the current burst
    reg [7:0]            burst_len;    // AxLEN for the current burst
    reg                  is_write;
    reg                  awvalid_r, arvalid_r;

    // Beats available before the next 4 KB boundary. Because the address is
    // beat-aligned this is exact, and it is what keeps every burst legal.
    function [31:0] beats_to_4k;
        input [ADDR_WIDTH-1:0] a;
        begin
            beats_to_4k = (32'd4096 - {20'd0, a[11:0]}) >> LSB;
        end
    endfunction

    function [8:0] plan;                     // beats for the next burst
        input [ADDR_WIDTH-1:0] a;
        input [31:0]           left;
        reg   [31:0]           lim;
        begin
            lim  = beats_to_4k(a);
            if (lim > 32'd256) lim = 32'd256;
            if (left < lim)    lim = left;
            plan = lim[8:0];
        end
    endfunction

    assign cmd_ready = (state == S_IDLE);

    assign m_axi_awid    = AXI_ID[ID_WIDTH-1:0];
    assign m_axi_awaddr  = addr;
    assign m_axi_awlen   = burst_len;
    assign m_axi_awsize  = SIZE;
    assign m_axi_awburst = INCR;
    assign m_axi_awvalid = awvalid_r;

    assign m_axi_arid    = AXI_ID[ID_WIDTH-1:0];
    assign m_axi_araddr  = addr;
    assign m_axi_arlen   = burst_len;
    assign m_axi_arsize  = SIZE;
    assign m_axi_arburst = INCR;
    assign m_axi_arvalid = arvalid_r;

    // W passes the caller's stream straight through; WLAST comes from our own
    // counter so a short or long caller stream cannot desynchronise the burst.
    assign m_axi_wdata  = wr_data;
    assign m_axi_wstrb  = wr_strb;
    assign m_axi_wvalid = (state == S_WDATA) && wr_valid;
    assign m_axi_wlast  = (state == S_WDATA) && (burst_beats == 9'd1);
    assign wr_ready     = (state == S_WDATA) && m_axi_wready;

    assign m_axi_bready = (state == S_WRESP);

    assign rd_data      = m_axi_rdata;
    assign rd_valid     = (state == S_RDATA) && m_axi_rvalid;
    assign m_axi_rready = (state == S_RDATA) && rd_ready;

    wire [8:0] next_plan = plan(addr, beats_left);

    always @(posedge clk) begin
        cmd_done <= 1'b0;
        if (!resetn) begin
            state       <= S_IDLE;
            addr        <= {ADDR_WIDTH{1'b0}};
            beats_left  <= 32'd0;
            burst_beats <= 9'd0;
            burst_len   <= 8'd0;
            is_write    <= 1'b0;
            awvalid_r   <= 1'b0;
            arvalid_r   <= 1'b0;
            cmd_resp    <= 2'b00;
        end else begin
            case (state)
                S_IDLE: if (cmd_valid && cmd_beats != 32'd0) begin
                    addr       <= cmd_addr;
                    beats_left <= cmd_beats;
                    is_write   <= cmd_write;
                    cmd_resp   <= 2'b00;
                    burst_beats <= plan(cmd_addr, cmd_beats);
                    burst_len   <= plan(cmd_addr, cmd_beats) - 9'd1;
                    if (cmd_write) begin awvalid_r <= 1'b1; state <= S_WADDR; end
                    else           begin arvalid_r <= 1'b1; state <= S_RADDR; end
                end

                S_WADDR: if (m_axi_awready) begin
                    awvalid_r <= 1'b0;
                    state     <= S_WDATA;
                end

                S_WDATA: if (m_axi_wvalid && m_axi_wready) begin
                    addr        <= addr + BYTES;
                    beats_left  <= beats_left - 32'd1;
                    burst_beats <= burst_beats - 9'd1;
                    if (burst_beats == 9'd1) state <= S_WRESP;
                end

                S_WRESP: if (m_axi_bvalid) begin
                    if (m_axi_bresp != 2'b00) cmd_resp <= m_axi_bresp;
                    if (beats_left == 32'd0) begin
                        cmd_done <= 1'b1;
                        state    <= S_IDLE;
                    end else begin
                        burst_beats <= next_plan;
                        burst_len   <= next_plan - 9'd1;
                        awvalid_r   <= 1'b1;
                        state       <= S_WADDR;
                    end
                end

                S_RADDR: if (m_axi_arready) begin
                    arvalid_r <= 1'b0;
                    state     <= S_RDATA;
                end

                S_RDATA: if (m_axi_rvalid && m_axi_rready) begin
                    if (m_axi_rresp != 2'b00) cmd_resp <= m_axi_rresp;
                    addr        <= addr + BYTES;
                    beats_left  <= beats_left - 32'd1;
                    burst_beats <= burst_beats - 9'd1;
                    if (burst_beats == 9'd1) begin
                        if (beats_left == 32'd1) begin
                            cmd_done <= 1'b1;
                            state    <= S_IDLE;
                        end else begin
                            // next_plan is computed from the pre-increment addr,
                            // so advance it by one beat here
                            burst_beats <= plan(addr + BYTES, beats_left - 32'd1);
                            burst_len   <= plan(addr + BYTES, beats_left - 32'd1) - 9'd1;
                            arvalid_r   <= 1'b1;
                            state       <= S_RADDR;
                        end
                    end
                end

                default: state <= S_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire

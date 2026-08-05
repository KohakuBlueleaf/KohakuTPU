// AXI4-Full slave RAM. Reference implementation for AXI bring-up.
//
// Supports INCR/FIXED/WRAP bursts to 256 beats, WSTRB byte-enables and ID
// reflection. Does not support exclusive access, AxCACHE/AxPROT semantics, or
// narrow-transfer read lane replication.
//
// Rationale for the handshake structure: docs/axi4-bringup.md

`default_nettype none

module axi4_ram #(
    parameter DATA_WIDTH = 64,
    parameter ADDR_WIDTH = 64,
    parameter ID_WIDTH   = 4,
    parameter DEPTH      = 4096          // in DATA_WIDTH-wide words
) (
    input  wire                    clk,
    input  wire                    resetn,      // ACTIVE LOW, like every AXI reset

    // AW
    input  wire [ID_WIDTH-1:0]     s_axi_awid,
    input  wire [ADDR_WIDTH-1:0]   s_axi_awaddr,
    input  wire [7:0]              s_axi_awlen,
    input  wire [2:0]              s_axi_awsize,
    input  wire [1:0]              s_axi_awburst,
    input  wire                    s_axi_awvalid,
    output wire                    s_axi_awready,
    // W
    input  wire [DATA_WIDTH-1:0]   s_axi_wdata,
    input  wire [DATA_WIDTH/8-1:0] s_axi_wstrb,
    input  wire                    s_axi_wlast,
    input  wire                    s_axi_wvalid,
    output wire                    s_axi_wready,
    // B
    output wire [ID_WIDTH-1:0]     s_axi_bid,
    output wire [1:0]              s_axi_bresp,
    output wire                    s_axi_bvalid,
    input  wire                    s_axi_bready,
    // AR
    input  wire [ID_WIDTH-1:0]     s_axi_arid,
    input  wire [ADDR_WIDTH-1:0]   s_axi_araddr,
    input  wire [7:0]              s_axi_arlen,
    input  wire [2:0]              s_axi_arsize,
    input  wire [1:0]              s_axi_arburst,
    input  wire                    s_axi_arvalid,
    output wire                    s_axi_arready,
    // R
    output wire [ID_WIDTH-1:0]     s_axi_rid,
    output wire [DATA_WIDTH-1:0]   s_axi_rdata,
    output wire [1:0]              s_axi_rresp,
    output wire                    s_axi_rlast,
    output wire                    s_axi_rvalid,
    input  wire                    s_axi_rready
);

    localparam STRB_WIDTH = DATA_WIDTH/8;
    localparam ADDR_LSB   = $clog2(STRB_WIDTH);   // byte offset within a word
    localparam IDX_WIDTH  = $clog2(DEPTH);

    localparam [1:0] RESP_OKAY = 2'b00;

    (* ram_style = "block" *)
    reg [DATA_WIDTH-1:0] mem [0:DEPTH-1];

    function [ADDR_WIDTH-1:0] next_addr;
        input [ADDR_WIDTH-1:0] addr;
        input [2:0]            size;
        input [1:0]            burst;
        input [7:0]            len;
        reg   [ADDR_WIDTH-1:0] incr;
        reg   [ADDR_WIDTH-1:0] mask;
        begin
            incr = {{(ADDR_WIDTH-1){1'b0}}, 1'b1} << size;
            case (burst)
                2'b00: next_addr = addr;                                  // FIXED
                2'b10: begin                                              // WRAP
                    mask      = (({{(ADDR_WIDTH-8){1'b0}}, len} + 1) << size) - 1;
                    next_addr = (addr & ~mask) | ((addr + incr) & mask);
                end
                default: next_addr = addr + incr;                         // INCR
            endcase
        end
    endfunction

    // ---------------------------------------------------------------- write
    localparam [1:0] W_IDLE = 2'd0, W_DATA = 2'd1, W_RESP = 2'd2;
    reg [1:0]            wstate;
    reg [ADDR_WIDTH-1:0] waddr;
    reg [7:0]            wlen;
    reg [2:0]            wsize;
    reg [1:0]            wburst;
    reg [ID_WIDTH-1:0]   wid;

    // Derived from state only. AXI4 A3.2.1 forbids VALID depending on READY, and
    // a slave whose READY depends on VALID deadlocks against such a master.
    assign s_axi_awready = (wstate == W_IDLE);
    assign s_axi_wready  = (wstate == W_DATA);
    assign s_axi_bvalid  = (wstate == W_RESP);
    assign s_axi_bid     = wid;
    assign s_axi_bresp   = RESP_OKAY;

    wire [IDX_WIDTH-1:0] widx = waddr[ADDR_LSB +: IDX_WIDTH];

    integer b;
    always @(posedge clk) begin
        if (!resetn) begin
            wstate <= W_IDLE;
            waddr  <= {ADDR_WIDTH{1'b0}};
            wlen   <= 8'd0;
            wsize  <= 3'd0;
            wburst <= 2'd0;
            wid    <= {ID_WIDTH{1'b0}};
        end else begin
            case (wstate)
                W_IDLE: if (s_axi_awvalid) begin
                    wid    <= s_axi_awid;
                    waddr  <= s_axi_awaddr;
                    wlen   <= s_axi_awlen;
                    wsize  <= s_axi_awsize;
                    wburst <= s_axi_awburst;
                    wstate <= W_DATA;
                end
                // Burst end comes from wlen, not WLAST, so a master that
                // miscounts WLAST cannot desynchronise the B response.
                W_DATA: if (s_axi_wvalid) begin
                    for (b = 0; b < STRB_WIDTH; b = b + 1)
                        if (s_axi_wstrb[b]) mem[widx][b*8 +: 8] <= s_axi_wdata[b*8 +: 8];
                    waddr <= next_addr(waddr, wsize, wburst, wlen);
                    if (wlen == 8'd0) wstate <= W_RESP;
                    else              wlen   <= wlen - 8'd1;
                end
                W_RESP: if (s_axi_bready) wstate <= W_IDLE;
                default: wstate <= W_IDLE;
            endcase
        end
    end

    // ----------------------------------------------------------------- read
    localparam R_IDLE = 1'b0, R_DATA = 1'b1;
    reg                  rstate;
    reg [ADDR_WIDTH-1:0] raddr;
    reg [8:0]            rbeats;          // 9 bits: ARLEN+1 can be 256
    reg [7:0]            rlen;            // captured ARLEN, needed by WRAP
    reg [2:0]            rsize;
    reg [1:0]            rburst;
    reg [ID_WIDTH-1:0]   rid;
    reg [DATA_WIDTH-1:0] rdata_r;
    reg                  rvalid_r;
    reg                  rlast_r;

    assign s_axi_arready = (rstate == R_IDLE);
    assign s_axi_rvalid  = rvalid_r;
    assign s_axi_rdata   = rdata_r;
    assign s_axi_rlast   = rlast_r;
    assign s_axi_rid     = rid;
    assign s_axi_rresp   = RESP_OKAY;

    wire [IDX_WIDTH-1:0] ridx = raddr[ADDR_LSB +: IDX_WIDTH];
    wire r_can_advance = !rvalid_r || s_axi_rready;

    always @(posedge clk) begin
        if (!resetn) begin
            rstate   <= R_IDLE;
            rvalid_r <= 1'b0;
            rlast_r  <= 1'b0;
            raddr    <= {ADDR_WIDTH{1'b0}};
            rbeats   <= 9'd0;
            rlen     <= 8'd0;
            rsize    <= 3'd0;
            rburst   <= 2'd0;
            rid      <= {ID_WIDTH{1'b0}};
        end else begin
            case (rstate)
                R_IDLE: begin
                    if (rvalid_r && s_axi_rready) rvalid_r <= 1'b0;
                    if (s_axi_arvalid) begin
                        rid    <= s_axi_arid;
                        raddr  <= s_axi_araddr;
                        rbeats <= {1'b0, s_axi_arlen} + 9'd1;
                        rlen   <= s_axi_arlen;
                        rsize  <= s_axi_arsize;
                        rburst <= s_axi_arburst;
                        rstate <= R_DATA;
                    end
                end
                R_DATA: if (r_can_advance) begin
                    if (rbeats != 9'd0) begin
                        rdata_r  <= mem[ridx];
                        rvalid_r <= 1'b1;
                        rlast_r  <= (rbeats == 9'd1);
                        rbeats   <= rbeats - 9'd1;
                        raddr    <= next_addr(raddr, rsize, rburst, rlen);
                    end else begin
                        rvalid_r <= 1'b0;
                        rlast_r  <= 1'b0;
                        rstate   <= R_IDLE;
                    end
                end
            endcase
        end
    end

endmodule

`default_nettype wire

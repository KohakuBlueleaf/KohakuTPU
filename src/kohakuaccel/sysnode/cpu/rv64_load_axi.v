// rv64_load_axi -- the AXI4 face of rv64_load_win: the 4 KB load slot at
// +0x8000 of a node's control port, as the host's 64-bit manager reaches it.
// One transaction at a time per direction; a beat is one lb_ access (writes
// with no strobe are dropped, as the orchestrator drops them).

`default_nettype none

module rv64_load_axi #(
    parameter integer IDW = 4
)(
    input  wire            clk,
    input  wire            resetn,

    input  wire [IDW-1:0]  s_awid,
    input  wire [31:0]     s_awaddr,
    input  wire [7:0]      s_awlen,
    input  wire            s_awvalid,
    output wire            s_awready,
    input  wire [63:0]     s_wdata,
    input  wire [7:0]      s_wstrb,
    input  wire            s_wlast,
    input  wire            s_wvalid,
    output wire            s_wready,
    output wire [IDW-1:0]  s_bid,
    output wire [1:0]      s_bresp,
    output wire            s_bvalid,
    input  wire            s_bready,
    input  wire [IDW-1:0]  s_arid,
    input  wire [31:0]     s_araddr,
    input  wire [7:0]      s_arlen,
    input  wire            s_arvalid,
    output wire            s_arready,
    output wire [IDW-1:0]  s_rid,
    output wire [63:0]     s_rdata,
    output wire [1:0]      s_rresp,
    output wire            s_rlast,
    output wire            s_rvalid,
    input  wire            s_rready,

    output reg             lb_en,
    output reg             lb_wr,
    output reg  [11:0]     lb_addr,
    output reg  [63:0]     lb_wdata,
    output reg  [7:0]      lb_wstrb,
    input  wire [63:0]     lb_rdata
);
    // ---- write: AW, then one lb write per W beat, then B ----
    localparam [1:0] W_IDLE = 2'd0, W_DATA = 2'd1, W_RESP = 2'd2;
    reg [1:0]     ws;
    reg [IDW-1:0] wid;
    reg [11:0]    waddr;
    assign s_awready = (ws == W_IDLE);
    assign s_wready  = (ws == W_DATA);
    assign s_bvalid  = (ws == W_RESP);
    assign s_bid     = wid;
    assign s_bresp   = 2'b00;

    // ---- read: AR, then per beat an address cycle, a capture cycle, a data cycle.
    // The processor's hs_rdata is REGISTERED from hs_addr, so the word for the
    // address presented in R_ADDR exists only in the cycle after it: captured a
    // cycle early, every host-control read returned 0 and a halted program read
    // as still running.
    localparam [1:0] R_IDLE = 2'd0, R_ADDR = 2'd1, R_CAP = 2'd3, R_DATA = 2'd2;
    reg [1:0]     rs;
    reg [IDW-1:0] rid;
    reg [11:0]    raddr;
    reg [8:0]     rleft;
    reg [63:0]    rdata_q;
    assign s_arready = (rs == R_IDLE);
    assign s_rvalid  = (rs == R_DATA);
    assign s_rid     = rid;
    assign s_rdata   = rdata_q;
    assign s_rresp   = 2'b00;
    assign s_rlast   = (rleft == 9'd1);

    always @(*) begin
        lb_en    = 1'b0;
        lb_wr    = 1'b0;
        lb_addr  = (ws == W_DATA) ? waddr : raddr;
        lb_wdata = s_wdata;
        lb_wstrb = s_wstrb;
        if (ws == W_DATA && s_wvalid && |s_wstrb) begin
            lb_en = 1'b1; lb_wr = 1'b1;
        end
        else if (rs == R_ADDR) begin
            lb_en = 1'b1;
        end
    end

    always @(posedge clk) begin
        if (!resetn) begin
            ws <= W_IDLE; rs <= R_IDLE;
        end else begin
            case (ws)
                W_IDLE: if (s_awvalid) begin wid <= s_awid; waddr <= s_awaddr[11:0]; ws <= W_DATA; end
                W_DATA: if (s_wvalid) begin
                    waddr <= waddr + 12'd8;
                    if (s_wlast) begin
                        ws <= W_RESP;
                    end
                end
                W_RESP: if (s_bready) begin
                    ws <= W_IDLE;
                end
                default: ws <= W_IDLE;
            endcase
            case (rs)
                R_IDLE: if (s_arvalid) begin
                    rid <= s_arid; raddr <= s_araddr[11:0]; rleft <= {1'b0, s_arlen} + 9'd1; rs <= R_ADDR;
                end
                R_ADDR: rs <= R_CAP;
                R_CAP:  begin rdata_q <= lb_rdata; rs <= R_DATA; end
                R_DATA: if (s_rready) begin
                    rleft <= rleft - 9'd1;
                    raddr <= raddr + 12'd8;
                    rs    <= (rleft == 9'd1) ? R_IDLE : R_ADDR;
                end
                default: rs <= R_IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire

// A 2-master, 2-slave AXI4 crossbar, standing in for SmartConnect.
//
// Deliberately minimal: one outstanding transaction per direction, whole
// transactions granted at a time, no interleaving, no width conversion. Real
// SmartConnect does far more, and for simulating the control plane none of it
// matters -- the masters here are a host model and a command engine, both of
// which issue one thing at a time.
//
// Address decode is a single bit: SEL_BIT of the address picks slave 1 over
// slave 0. That is all the routing the control path needs, and keeping it that
// crude makes the map obvious from the testbench.
//
// The point of having it at all is that the main orchestrator and the host both
// reach both slaves, which is what the architecture requires: the host must be
// able to poke a MAG directly for bring-up, and the orchestrator must be able
// to drive it during a run. See docs/arch-design.md s6.4.

`default_nettype none

module axi_xbar2 #(
    parameter integer ADDR_W  = 32,
    parameter integer DATA_W  = 64,
    parameter integer ID_W    = 4,
    parameter integer SEL_BIT = 28
)(
    input  wire               clk,
    input  wire               resetn,

    // ---- master 0 ----
    input  wire [ID_W-1:0]    m0_awid,
    input  wire [ADDR_W-1:0]  m0_awaddr,
    input  wire [7:0]         m0_awlen,
    input  wire               m0_awvalid,
    output wire               m0_awready,
    input  wire [DATA_W-1:0]  m0_wdata,
    input  wire [DATA_W/8-1:0] m0_wstrb,
    input  wire               m0_wlast,
    input  wire               m0_wvalid,
    output wire               m0_wready,
    output wire [ID_W-1:0]    m0_bid,
    output wire [1:0]         m0_bresp,
    output wire               m0_bvalid,
    input  wire               m0_bready,
    input  wire [ID_W-1:0]    m0_arid,
    input  wire [ADDR_W-1:0]  m0_araddr,
    input  wire [7:0]         m0_arlen,
    input  wire               m0_arvalid,
    output wire               m0_arready,
    output wire [ID_W-1:0]    m0_rid,
    output wire [DATA_W-1:0]  m0_rdata,
    output wire [1:0]         m0_rresp,
    output wire               m0_rlast,
    output wire               m0_rvalid,
    input  wire               m0_rready,

    // ---- master 1 ----
    input  wire [ID_W-1:0]    m1_awid,
    input  wire [ADDR_W-1:0]  m1_awaddr,
    input  wire [7:0]         m1_awlen,
    input  wire               m1_awvalid,
    output wire               m1_awready,
    input  wire [DATA_W-1:0]  m1_wdata,
    input  wire [DATA_W/8-1:0] m1_wstrb,
    input  wire               m1_wlast,
    input  wire               m1_wvalid,
    output wire               m1_wready,
    output wire [ID_W-1:0]    m1_bid,
    output wire [1:0]         m1_bresp,
    output wire               m1_bvalid,
    input  wire               m1_bready,
    input  wire [ID_W-1:0]    m1_arid,
    input  wire [ADDR_W-1:0]  m1_araddr,
    input  wire [7:0]         m1_arlen,
    input  wire               m1_arvalid,
    output wire               m1_arready,
    output wire [ID_W-1:0]    m1_rid,
    output wire [DATA_W-1:0]  m1_rdata,
    output wire [1:0]         m1_rresp,
    output wire               m1_rlast,
    output wire               m1_rvalid,
    input  wire               m1_rready,

    // ---- slave 0 ----
    output wire [ID_W-1:0]    s0_awid,
    output wire [ADDR_W-1:0]  s0_awaddr,
    output wire [7:0]         s0_awlen,
    output wire               s0_awvalid,
    input  wire               s0_awready,
    output wire [DATA_W-1:0]  s0_wdata,
    output wire [DATA_W/8-1:0] s0_wstrb,
    output wire               s0_wlast,
    output wire               s0_wvalid,
    input  wire               s0_wready,
    input  wire [ID_W-1:0]    s0_bid,
    input  wire [1:0]         s0_bresp,
    input  wire               s0_bvalid,
    output wire               s0_bready,
    output wire [ID_W-1:0]    s0_arid,
    output wire [ADDR_W-1:0]  s0_araddr,
    output wire [7:0]         s0_arlen,
    output wire               s0_arvalid,
    input  wire               s0_arready,
    input  wire [ID_W-1:0]    s0_rid,
    input  wire [DATA_W-1:0]  s0_rdata,
    input  wire [1:0]         s0_rresp,
    input  wire               s0_rlast,
    input  wire               s0_rvalid,
    output wire               s0_rready,

    // ---- slave 1 ----
    output wire [ID_W-1:0]    s1_awid,
    output wire [ADDR_W-1:0]  s1_awaddr,
    output wire [7:0]         s1_awlen,
    output wire               s1_awvalid,
    input  wire               s1_awready,
    output wire [DATA_W-1:0]  s1_wdata,
    output wire [DATA_W/8-1:0] s1_wstrb,
    output wire               s1_wlast,
    output wire               s1_wvalid,
    input  wire               s1_wready,
    input  wire [ID_W-1:0]    s1_bid,
    input  wire [1:0]         s1_bresp,
    input  wire               s1_bvalid,
    output wire               s1_bready,
    output wire [ID_W-1:0]    s1_arid,
    output wire [ADDR_W-1:0]  s1_araddr,
    output wire [7:0]         s1_arlen,
    output wire               s1_arvalid,
    input  wire               s1_arready,
    input  wire [ID_W-1:0]    s1_rid,
    input  wire [DATA_W-1:0]  s1_rdata,
    input  wire [1:0]         s1_rresp,
    input  wire               s1_rlast,
    input  wire               s1_rvalid,
    output wire               s1_rready
);
    // ---------------- write channel ----------------
    reg       wbusy;
    reg       wm;          // which master holds the grant
    reg       ws;          // which slave it is routed to

    wire w_req0 = m0_awvalid && !wbusy;
    wire w_req1 = m1_awvalid && !wbusy && !w_req0;   // master 0 wins ties

    always @(posedge clk) begin
        if (!resetn) begin
            wbusy <= 1'b0; wm <= 1'b0; ws <= 1'b0;
        end else begin
            if (!wbusy) begin
                if (w_req0) begin wbusy <= 1'b1; wm <= 1'b0; ws <= m0_awaddr[SEL_BIT]; end
                else if (w_req1) begin wbusy <= 1'b1; wm <= 1'b1; ws <= m1_awaddr[SEL_BIT]; end
            end else begin
                // the grant is released when the response is taken
                if ((ws ? s1_bvalid : s0_bvalid) &&
                    (wm ? m1_bready : m0_bready)) wbusy <= 1'b0;
            end
        end
    end

    wire [ID_W-1:0]   aw_id    = wm ? m1_awid    : m0_awid;
    wire [ADDR_W-1:0] aw_addr  = wm ? m1_awaddr  : m0_awaddr;
    wire [7:0]        aw_len   = wm ? m1_awlen   : m0_awlen;
    wire              aw_valid = wbusy && (wm ? m1_awvalid : m0_awvalid);
    wire [DATA_W-1:0] w_data   = wm ? m1_wdata   : m0_wdata;
    wire [DATA_W/8-1:0] w_strb = wm ? m1_wstrb   : m0_wstrb;
    wire              w_last   = wm ? m1_wlast   : m0_wlast;
    wire              w_valid  = wbusy && (wm ? m1_wvalid : m0_wvalid);

    assign s0_awid    = aw_id;    assign s1_awid    = aw_id;
    assign s0_awaddr  = aw_addr;  assign s1_awaddr  = aw_addr;
    assign s0_awlen   = aw_len;   assign s1_awlen   = aw_len;
    assign s0_awvalid = aw_valid && !ws;
    assign s1_awvalid = aw_valid &&  ws;
    assign s0_wdata   = w_data;   assign s1_wdata   = w_data;
    assign s0_wstrb   = w_strb;   assign s1_wstrb   = w_strb;
    assign s0_wlast   = w_last;   assign s1_wlast   = w_last;
    assign s0_wvalid  = w_valid && !ws;
    assign s1_wvalid  = w_valid &&  ws;
    assign s0_bready  = wbusy && !ws && (wm ? m1_bready : m0_bready);
    assign s1_bready  = wbusy &&  ws && (wm ? m1_bready : m0_bready);

    assign m0_awready = wbusy && !wm && (ws ? s1_awready : s0_awready);
    assign m1_awready = wbusy &&  wm && (ws ? s1_awready : s0_awready);
    assign m0_wready  = wbusy && !wm && (ws ? s1_wready  : s0_wready);
    assign m1_wready  = wbusy &&  wm && (ws ? s1_wready  : s0_wready);
    assign m0_bid     = ws ? s1_bid   : s0_bid;
    assign m1_bid     = ws ? s1_bid   : s0_bid;
    assign m0_bresp   = ws ? s1_bresp : s0_bresp;
    assign m1_bresp   = ws ? s1_bresp : s0_bresp;
    assign m0_bvalid  = wbusy && !wm && (ws ? s1_bvalid : s0_bvalid);
    assign m1_bvalid  = wbusy &&  wm && (ws ? s1_bvalid : s0_bvalid);

    // ---------------- read channel ----------------
    reg rbusy, rm, rs;

    wire r_req0 = m0_arvalid && !rbusy;
    wire r_req1 = m1_arvalid && !rbusy && !r_req0;

    always @(posedge clk) begin
        if (!resetn) begin
            rbusy <= 1'b0; rm <= 1'b0; rs <= 1'b0;
        end else begin
            if (!rbusy) begin
                if (r_req0) begin rbusy <= 1'b1; rm <= 1'b0; rs <= m0_araddr[SEL_BIT]; end
                else if (r_req1) begin rbusy <= 1'b1; rm <= 1'b1; rs <= m1_araddr[SEL_BIT]; end
            end else begin
                if ((rs ? s1_rvalid : s0_rvalid) && (rs ? s1_rlast : s0_rlast) &&
                    (rm ? m1_rready : m0_rready)) rbusy <= 1'b0;
            end
        end
    end

    wire [ID_W-1:0]   ar_id    = rm ? m1_arid   : m0_arid;
    wire [ADDR_W-1:0] ar_addr  = rm ? m1_araddr : m0_araddr;
    wire [7:0]        ar_len   = rm ? m1_arlen  : m0_arlen;
    wire              ar_valid = rbusy && (rm ? m1_arvalid : m0_arvalid);

    assign s0_arid    = ar_id;   assign s1_arid    = ar_id;
    assign s0_araddr  = ar_addr; assign s1_araddr  = ar_addr;
    assign s0_arlen   = ar_len;  assign s1_arlen   = ar_len;
    assign s0_arvalid = ar_valid && !rs;
    assign s1_arvalid = ar_valid &&  rs;
    assign s0_rready  = rbusy && !rs && (rm ? m1_rready : m0_rready);
    assign s1_rready  = rbusy &&  rs && (rm ? m1_rready : m0_rready);

    assign m0_arready = rbusy && !rm && (rs ? s1_arready : s0_arready);
    assign m1_arready = rbusy &&  rm && (rs ? s1_arready : s0_arready);
    assign m0_rid     = rs ? s1_rid   : s0_rid;
    assign m1_rid     = rs ? s1_rid   : s0_rid;
    assign m0_rdata   = rs ? s1_rdata : s0_rdata;
    assign m1_rdata   = rs ? s1_rdata : s0_rdata;
    assign m0_rresp   = rs ? s1_rresp : s0_rresp;
    assign m1_rresp   = rs ? s1_rresp : s0_rresp;
    assign m0_rlast   = rs ? s1_rlast : s0_rlast;
    assign m1_rlast   = rs ? s1_rlast : s0_rlast;
    assign m0_rvalid  = rbusy && !rm && (rs ? s1_rvalid : s0_rvalid);
    assign m1_rvalid  = rbusy &&  rm && (rs ? s1_rvalid : s0_rvalid);

endmodule

`default_nettype wire

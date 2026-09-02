// kts_pipe_bd -- one hop of a Kohaku Transmit Surface across a die boundary.
// u_tx is pinned with the sending die and u_rx with the landing die, so every
// crossing wire is one register driving one register (a Laguna pair,
// USER_SLL_REG), and each half takes its own die's copy of the reset
// (xcvu13p_rst_tree): no reset net crosses the boundary either. Device-specific
// by name; the framework's kts_pipe knows nothing about dies.
//
// ASYNC 1: the two dies run on their own clocks and a kts_cdc sits between the
// two halves. The surface has no ready, so its forward buffer must never fill:
// the sender's credits bound flits in flight to CRED per VC and kts_cdc sizes
// itself VC*CRED deep. CRED is mag_link's RX_BEATS.

`default_nettype none

module kts_pipe_bd #(
    parameter integer W     = 288,
    parameter integer VCW   = 1,
    parameter integer CN_W  = 4,
    parameter integer ASYNC = 0,
    parameter integer CRED  = 64,
    parameter         MEM   = "block"
)(
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_RESET rstn_tx" *)
    input  wire            clk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 clk_rx CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_RESET rstn_rx" *)
    input  wire            clk_rx,             // = clk at ASYNC 0
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rstn_tx RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire            rstn_tx,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 rstn_rx RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire            rstn_rx,

    input  wire            i_valid,
    input  wire [VCW-1:0]  i_vc,
    input  wire            i_last,
    input  wire [W-1:0]    i_flit,
    output wire            o_valid,
    output wire [VCW-1:0]  o_vc,
    output wire            o_last,
    output wire [W-1:0]    o_flit,

    input  wire            i_crd_valid,
    input  wire [VCW-1:0]  i_crd_vc,
    input  wire [CN_W-1:0] i_crd_n,
    output wire            o_crd_valid,
    output wire [VCW-1:0]  o_crd_vc,
    output wire [CN_W-1:0] o_crd_n
);
    // The boundary: flits leave u_tx for u_rx, credits leave u_rx for u_tx.
    wire            x_valid, x_crd_valid;
    wire [VCW-1:0]  x_vc, x_crd_vc;
    wire            x_last;
    wire [W-1:0]    x_flit;
    wire [CN_W-1:0] x_crd_n;

    // What u_rx sends on and what u_tx receives: the crossing itself at ASYNC 0,
    // the far side of the two buffers at ASYNC 1.
    wire            y_valid, y_crd_valid;
    wire [VCW-1:0]  y_vc, y_crd_vc;
    wire            y_last;
    wire [W-1:0]    y_flit;
    wire [CN_W-1:0] y_crd_n;

    generate if (ASYNC != 0) begin : g_async
        kts_cdc #(.W(W), .VC(1 << VCW), .D(CRED), .CN_W(CN_W), .MEM(MEM)) u_x (
            .a_clk(clk),    .a_rst(!rstn_tx),
            .b_clk(clk_rx), .b_rst(!rstn_rx),
            .i_valid(x_valid), .i_vc(x_vc), .i_last(x_last), .i_flit(x_flit),
            .o_valid(y_valid), .o_vc(y_vc), .o_last(y_last), .o_flit(y_flit),
            .i_crd_valid(x_crd_valid), .i_crd_vc(x_crd_vc), .i_crd_n(x_crd_n),
            .o_crd_valid(y_crd_valid), .o_crd_vc(y_crd_vc), .o_crd_n(y_crd_n));
    end else begin : g_sync
        assign y_valid     = x_valid;
        assign y_vc        = x_vc;
        assign y_last      = x_last;
        assign y_flit      = x_flit;
        assign y_crd_valid = x_crd_valid;
        assign y_crd_vc    = x_crd_vc;
        assign y_crd_n     = x_crd_n;
    end endgenerate

    kts_pipe #(.W(W), .VCW(VCW), .CN_W(CN_W), .N(1)) u_tx (
        .clk(clk), .rst(!rstn_tx),
        .i_valid(i_valid), .i_vc(i_vc), .i_last(i_last), .i_flit(i_flit),
        .o_valid(x_valid), .o_vc(x_vc), .o_last(x_last), .o_flit(x_flit),
        .i_crd_valid(y_crd_valid), .i_crd_vc(y_crd_vc), .i_crd_n(y_crd_n),
        .o_crd_valid(o_crd_valid), .o_crd_vc(o_crd_vc), .o_crd_n(o_crd_n)
    );

    kts_pipe #(.W(W), .VCW(VCW), .CN_W(CN_W), .N(1)) u_rx (
        .clk(clk_rx), .rst(!rstn_rx),
        .i_valid(y_valid), .i_vc(y_vc), .i_last(y_last), .i_flit(y_flit),
        .o_valid(o_valid), .o_vc(o_vc), .o_last(o_last), .o_flit(o_flit),
        .i_crd_valid(i_crd_valid), .i_crd_vc(i_crd_vc), .i_crd_n(i_crd_n),
        .o_crd_valid(x_crd_valid), .o_crd_vc(x_crd_vc), .o_crd_n(x_crd_n)
    );

endmodule

`default_nettype wire

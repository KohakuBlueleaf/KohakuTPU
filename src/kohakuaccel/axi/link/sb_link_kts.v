// One direction of a station-to-station crossing on a Kohaku Transmit Surface.
//
// Credit is a MESSAGE here, where sb_link returns a pulse down a pipe of
// matched depth and sb_link_cdc a gray pointer into the sender's own
// synchroniser. A message survives any carrier that preserves per-VC order, so
// `CDC` swaps a register pipe for a dual-clock FIFO with no protocol change.
//
// VCN = 2 puts both classes on one wire, each with its own credit and buffer,
// sharing one flit per cycle of max(WA, WB); VCN = 1 builds no second buffer.

`default_nettype none

module sb_link_kts #(
    parameter integer WA   = 640,
    parameter integer WB   = 522,
    parameter integer VCN  = 2,                 // 1: class A only
    parameter integer PIPE = 4,
    parameter integer CRED = 16,
    parameter integer CN_W = 4,
    parameter integer CDC  = 0,                 // 1: the carrier crosses clocks
    parameter         MEMORY_TYPE = "distributed"
)(
    input  wire          i_clk,
    input  wire          i_rst,
    input  wire          o_clk,
    input  wire          o_rst,

    input  wire          a_i_valid,
    output wire          a_i_ready,
    input  wire [WA-1:0] a_i_data,
    input  wire          b_i_valid,
    output wire          b_i_ready,
    input  wire [WB-1:0] b_i_data,

    output wire          a_o_valid,
    input  wire          a_o_ready,
    output wire [WA-1:0] a_o_data,
    output wire          b_o_valid,
    input  wire          b_o_ready,
    output wire [WB-1:0] b_o_data
);
    localparam integer W    = (VCN <= 1) ? WA : ((WA > WB) ? WA : WB);
    localparam integer RXD  = (CRED < 16) ? 16 : CRED;
    localparam integer VCW  = (VCN <= 1) ? 1 : $clog2(VCN);

    // A link spans the die, so its reset must not be the same net the stations
    // use. dont_touch, or Vivado merges this copy back into that one net.
    (* dont_touch = "yes" *) reg irst_q, orst_q;
    always @(posedge i_clk) begin
        irst_q <= i_rst;
    end
    always @(posedge o_clk) begin
        orst_q <= o_rst;
    end

    // ---- the sending end ---------------------------------------------------
    wire [VCN-1:0]   req_valid;
    wire [VCN-1:0]   req_take;
    wire [VCN*W-1:0] req_flit;

    generate if (VCN <= 1) begin : g_tx1
        assign req_valid = a_i_valid;
        assign req_flit  = a_i_data;
        assign a_i_ready = req_take[0];
        assign b_i_ready = 1'b1;
    end else begin : g_tx2
        assign req_valid = {b_i_valid, a_i_valid};
        assign req_flit[0*W +: W] = {{(W-WA){1'b0}}, a_i_data};
        assign req_flit[1*W +: W] = {{(W-WB){1'b0}}, b_i_data};
        assign a_i_ready = req_take[0];
        assign b_i_ready = req_take[1];
    end endgenerate

    wire            t_v, t_l, tc_v;
    wire [VCW-1:0]  t_vc, tc_vc;
    wire [W-1:0]    t_f;
    wire [CN_W-1:0] tc_n;

    kts_tx #(.W(W), .VC(VCN), .CMAX(RXD), .CN_W(CN_W)) u_tx (
        .clk(i_clk), .rst(irst_q),
        .req_valid(req_valid), .req_last({VCN{1'b1}}), .req_flit(req_flit),
        .req_take(req_take),
        .tx_valid(t_v), .tx_vc(t_vc), .tx_last(t_l), .tx_flit(t_f),
        .crd_valid(tc_v), .crd_vc(tc_vc), .crd_n(tc_n),
        .credits()
    );

    // ---- the carrier: register stages, or a dual-clock FIFO ----------------
    wire            p_v, p_l, pc_v;
    wire [VCW-1:0]  p_vc, pc_vc;
    wire [W-1:0]    p_f;
    wire [CN_W-1:0] pc_n;

    generate if (CDC != 0) begin : g_cdc
        kts_cdc #(.W(W), .VC(VCN), .D(RXD), .CN_W(CN_W), .MEM(MEMORY_TYPE)) u_c (
            .a_clk(i_clk), .a_rst(irst_q), .b_clk(o_clk), .b_rst(orst_q),
            .i_valid(t_v), .i_vc(t_vc), .i_last(t_l), .i_flit(t_f),
            .o_valid(p_v), .o_vc(p_vc), .o_last(p_l), .o_flit(p_f),
            .i_crd_valid(pc_v), .i_crd_vc(pc_vc), .i_crd_n(pc_n),
            .o_crd_valid(tc_v), .o_crd_vc(tc_vc), .o_crd_n(tc_n)
        );
    end else begin : g_pipe
        kts_pipe #(.W(W), .VCW(VCW), .CN_W(CN_W), .N(PIPE)) u_c (
            .clk(i_clk), .rst(irst_q),
            .i_valid(t_v), .i_vc(t_vc), .i_last(t_l), .i_flit(t_f),
            .o_valid(p_v), .o_vc(p_vc), .o_last(p_l), .o_flit(p_f),
            .i_crd_valid(pc_v), .i_crd_vc(pc_vc), .i_crd_n(pc_n),
            .o_crd_valid(tc_v), .o_crd_vc(tc_vc), .o_crd_n(tc_n)
        );
    end endgenerate

    // ---- the receiving end -------------------------------------------------
    wire [VCN-1:0]   out_valid;
    wire [VCN*W-1:0] out_flit;
    wire [VCN-1:0]   out_pop;

    generate if (VCN <= 1) begin : g_rx1
        assign out_pop   = a_o_ready;
        assign a_o_valid = out_valid[0];
        assign a_o_data  = out_flit[0 +: WA];
        assign b_o_valid = 1'b0;
        assign b_o_data  = {WB{1'b0}};
    end else begin : g_rx2
        assign out_pop   = {b_o_ready, a_o_ready};
        assign a_o_valid = out_valid[0];
        assign b_o_valid = out_valid[1];
        assign a_o_data  = out_flit[0*W +: WA];
        assign b_o_data  = out_flit[1*W +: WB];
    end endgenerate

    kts_rx #(.W(W), .VC(VCN), .D(RXD), .CN_W(CN_W), .MEM(MEMORY_TYPE)) u_rx (
        .clk(o_clk), .rst(orst_q),
        .rx_valid(p_v), .rx_vc(p_vc), .rx_last(p_l), .rx_flit(p_f),
        .out_valid(out_valid), .out_last(), .out_flit(out_flit),
        .out_pop(out_pop),
        .crd_valid(pc_v), .crd_vc(pc_vc), .crd_n(pc_n)
    );

endmodule

`default_nettype wire

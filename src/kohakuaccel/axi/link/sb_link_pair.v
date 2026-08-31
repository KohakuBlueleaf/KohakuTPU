// A whole station-to-station crossing: REQ and RSP, both directions.
// KTS = 0: four credit links, REQ_W + RSP_W wires each way, both classes
// moving in the same cycle. KTS = 1: two surfaces, REQ on VC 0 and RSP on
// VC 1, 3 + max(REQ_W, RSP_W) wires each way -- the classes then share one
// flit per cycle and the response buffer widens to the request's flit.

`default_nettype none

module sb_link_pair #(
    parameter integer REQ_W = 640,
    parameter integer RSP_W = 522,
    parameter integer PIPE  = 4,
    parameter integer CRED  = 16,
    parameter integer KTS   = 0,
    parameter integer CN_W  = 4
)(
    input  wire             bus_clk,
    input  wire             bus_rst,

    input  wire             a_req_valid,
    output wire             a_req_ready,
    input  wire [REQ_W-1:0] a_req_data,
    output wire             b_req_valid,
    input  wire             b_req_ready,
    output wire [REQ_W-1:0] b_req_data,

    input  wire             b_rsp_valid,
    output wire             b_rsp_ready,
    input  wire [RSP_W-1:0] b_rsp_data,
    output wire             a_rsp_valid,
    input  wire             a_rsp_ready,
    output wire [RSP_W-1:0] a_rsp_data,

    input  wire             b_req2_valid,
    output wire             b_req2_ready,
    input  wire [REQ_W-1:0] b_req2_data,
    output wire             a_req2_valid,
    input  wire             a_req2_ready,
    output wire [REQ_W-1:0] a_req2_data,

    input  wire             a_rsp2_valid,
    output wire             a_rsp2_ready,
    input  wire [RSP_W-1:0] a_rsp2_data,
    output wire             b_rsp2_valid,
    input  wire             b_rsp2_ready,
    output wire [RSP_W-1:0] b_rsp2_data
);
    generate
    if (KTS == 0) begin : g_links
        sb_link #(.W(REQ_W), .PIPE(PIPE), .CRED(CRED)) u_req_ab (
            .clk(bus_clk), .rst(bus_rst),
            .i_valid(a_req_valid), .i_ready(a_req_ready), .i_data(a_req_data),
            .o_valid(b_req_valid), .o_ready(b_req_ready), .o_data(b_req_data),
            .stat_sent(), .stat_nocred());

        sb_link #(.W(RSP_W), .PIPE(PIPE), .CRED(CRED)) u_rsp_ba (
            .clk(bus_clk), .rst(bus_rst),
            .i_valid(b_rsp_valid), .i_ready(b_rsp_ready), .i_data(b_rsp_data),
            .o_valid(a_rsp_valid), .o_ready(a_rsp_ready), .o_data(a_rsp_data),
            .stat_sent(), .stat_nocred());

        sb_link #(.W(REQ_W), .PIPE(PIPE), .CRED(CRED)) u_req_ba (
            .clk(bus_clk), .rst(bus_rst),
            .i_valid(b_req2_valid), .i_ready(b_req2_ready), .i_data(b_req2_data),
            .o_valid(a_req2_valid), .o_ready(a_req2_ready), .o_data(a_req2_data),
            .stat_sent(), .stat_nocred());

        sb_link #(.W(RSP_W), .PIPE(PIPE), .CRED(CRED)) u_rsp_ab (
            .clk(bus_clk), .rst(bus_rst),
            .i_valid(a_rsp2_valid), .i_ready(a_rsp2_ready), .i_data(a_rsp2_data),
            .o_valid(b_rsp2_valid), .o_ready(b_rsp2_ready), .o_data(b_rsp2_data),
            .stat_sent(), .stat_nocred());
    end
    else begin : g_surface
        // A->B carries a_req and a_rsp2; B->A carries b_req2 and b_rsp.
        sb_link_kts #(.WA(REQ_W), .WB(RSP_W), .VCN(2), .PIPE(PIPE),
                      .CRED(CRED), .CN_W(CN_W), .CDC(0)) u_ab (
            .i_clk(bus_clk), .i_rst(bus_rst),
            .o_clk(bus_clk), .o_rst(bus_rst),
            .a_i_valid(a_req_valid), .a_i_ready(a_req_ready),
            .a_i_data(a_req_data),
            .b_i_valid(a_rsp2_valid), .b_i_ready(a_rsp2_ready),
            .b_i_data(a_rsp2_data),
            .a_o_valid(b_req_valid), .a_o_ready(b_req_ready),
            .a_o_data(b_req_data),
            .b_o_valid(b_rsp2_valid), .b_o_ready(b_rsp2_ready),
            .b_o_data(b_rsp2_data));

        sb_link_kts #(.WA(REQ_W), .WB(RSP_W), .VCN(2), .PIPE(PIPE),
                      .CRED(CRED), .CN_W(CN_W), .CDC(0)) u_ba (
            .i_clk(bus_clk), .i_rst(bus_rst),
            .o_clk(bus_clk), .o_rst(bus_rst),
            .a_i_valid(b_req2_valid), .a_i_ready(b_req2_ready),
            .a_i_data(b_req2_data),
            .b_i_valid(b_rsp_valid), .b_i_ready(b_rsp_ready),
            .b_i_data(b_rsp_data),
            .a_o_valid(a_req2_valid), .a_o_ready(a_req2_ready),
            .a_o_data(a_req2_data),
            .b_o_valid(a_rsp_valid), .b_o_ready(a_rsp_ready),
            .b_o_data(a_rsp_data));
    end
    endgenerate
endmodule

`default_nettype wire

// A whole station-to-station crossing: REQ and RSP, both directions. Replaces
// one slr_cross (3,265 LUT, 1,602 SRL) plus the root_smc master port feeding it.

`default_nettype none

module sb_link_pair #(
    parameter integer REQ_W = 640,
    parameter integer RSP_W = 522,
    parameter integer PIPE  = 4,
    parameter integer CRED  = 16
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
    sb_link #(.W(REQ_W), .PIPE(PIPE), .CRED(CRED)) u_req_ab (
        .clk(bus_clk), .rst(bus_rst),
        .i_valid(a_req_valid), .i_ready(a_req_ready), .i_data(a_req_data),
        .o_valid(b_req_valid), .o_ready(b_req_ready), .o_data(b_req_data));

    sb_link #(.W(RSP_W), .PIPE(PIPE), .CRED(CRED)) u_rsp_ba (
        .clk(bus_clk), .rst(bus_rst),
        .i_valid(b_rsp_valid), .i_ready(b_rsp_ready), .i_data(b_rsp_data),
        .o_valid(a_rsp_valid), .o_ready(a_rsp_ready), .o_data(a_rsp_data));

    sb_link #(.W(REQ_W), .PIPE(PIPE), .CRED(CRED)) u_req_ba (
        .clk(bus_clk), .rst(bus_rst),
        .i_valid(b_req2_valid), .i_ready(b_req2_ready), .i_data(b_req2_data),
        .o_valid(a_req2_valid), .o_ready(a_req2_ready), .o_data(a_req2_data));

    sb_link #(.W(RSP_W), .PIPE(PIPE), .CRED(CRED)) u_rsp_ab (
        .clk(bus_clk), .rst(bus_rst),
        .i_valid(a_rsp2_valid), .i_ready(a_rsp2_ready), .i_data(a_rsp2_data),
        .o_valid(b_rsp2_valid), .o_ready(b_rsp2_ready), .o_data(b_rsp2_data));
endmodule

`default_nettype wire

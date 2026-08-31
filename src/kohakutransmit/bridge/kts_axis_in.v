// AXI4-Stream into a surface: one beat becomes one flit, tlast becomes `last`,
// tdest picks the virtual channel (or VC_FIXED when TDEST_W is 0). The stream's
// `tready` is the sending end's credit for that VC -- the only place a `ready`
// exists, and it is local to this module.

`default_nettype none

module kts_axis_in #(
    parameter integer W       = 288,
    parameter integer VC      = 2,
    parameter integer CMAX    = 64,
    parameter integer CN_W    = 4,
    parameter integer TDEST_W = 1,             // 0: every beat on VC_FIXED
    parameter integer VC_FIXED = 0,
    parameter integer VCW     = (VC <= 1) ? 1 : $clog2(VC),
    parameter integer TDW     = (TDEST_W <= 0) ? 1 : TDEST_W
)(
    input  wire              clk,
    input  wire              rst,

    input  wire              s_tvalid,
    output wire              s_tready,
    input  wire [W-1:0]      s_tdata,
    input  wire              s_tlast,
    input  wire [TDW-1:0]    s_tdest,

    output wire              tx_valid,
    output wire [VCW-1:0]    tx_vc,
    output wire              tx_last,
    output wire [W-1:0]      tx_flit,
    input  wire              crd_valid,
    input  wire [VCW-1:0]    crd_vc,
    input  wire [CN_W-1:0]   crd_n
);
    wire [VCW-1:0] vc = (TDEST_W <= 0) ? VC_FIXED[VCW-1:0] : s_tdest[VCW-1:0];

    reg  [VC-1:0]   req_valid;
    wire [VC-1:0]   req_take;
    integer v;
    always @(*) begin
        req_valid = {VC{1'b0}};
        for (v = 0; v < VC; v = v + 1) begin
            if (s_tvalid && (vc == v[VCW-1:0])) begin
                req_valid[v] = 1'b1;
            end
        end
    end
    assign s_tready = |req_take;

    wire [VC*($clog2(CMAX)+1)-1:0] credits_unused;
    kts_tx #(.W(W), .VC(VC), .CMAX(CMAX), .CN_W(CN_W)) u_tx (
        .clk(clk), .rst(rst),
        .req_valid(req_valid), .req_last({VC{s_tlast}}), .req_flit({VC{s_tdata}}),
        .req_take(req_take),
        .tx_valid(tx_valid), .tx_vc(tx_vc), .tx_last(tx_last), .tx_flit(tx_flit),
        .crd_valid(crd_valid), .crd_vc(crd_vc), .crd_n(crd_n),
        .credits(credits_unused)
    );

endmodule

`default_nettype wire

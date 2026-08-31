// The sending end of a surface. One flit per cycle onto the forward wire, only
// ever with a credit for its virtual channel in hand; credits arrive on the
// backward wire as counts. Nothing here waits on the receiver in the same
// cycle -- there is no `ready` on the wire -- so the wire may be any length.
//
// Per VC the caller offers ONE flit at a time (`req_valid`); `req_take` says it
// went this cycle and the next may be offered. Round-robin among the VCs that
// hold credit; the output is a register.
//
// CREDITS START AT ZERO. The receiver announces its depth by returning it as
// ordinary credits once its buffers are out of reset, so the sender never
// needs to know D and never sends into a buffer that is not yet listening.

`default_nettype none

module kts_tx #(
    parameter integer W    = 288,
    parameter integer VC   = 2,                // 1..8
    parameter integer CMAX = 64,               // most credits a VC can ever hold
    parameter integer CN_W = 4,                // width of a credit-return count
    parameter integer VCW  = (VC <= 1) ? 1 : $clog2(VC),
    parameter integer CW   = $clog2(CMAX) + 1
)(
    input  wire              clk,
    input  wire              rst,

    input  wire [VC-1:0]     req_valid,
    input  wire [VC-1:0]     req_last,
    input  wire [VC*W-1:0]   req_flit,
    output wire [VC-1:0]     req_take,

    output reg               tx_valid,
    output reg  [VCW-1:0]    tx_vc,
    output reg               tx_last,
    output reg  [W-1:0]      tx_flit,

    input  wire              crd_valid,
    input  wire [VCW-1:0]    crd_vc,
    input  wire [CN_W-1:0]   crd_n,

    output wire [VC*CW-1:0]  credits               // observation only
);
    localparam [VCW-1:0] VC_C = VC;

    reg  [CW-1:0]  cred [0:VC-1];
    reg  [VCW-1:0] rr;

    wire [VC-1:0] has;
    genvar g;
    generate
    for (g = 0; g < VC; g = g + 1) begin : g_has
        assign has[g]                = (cred[g] != {CW{1'b0}});
        assign credits[g*CW +: CW]   = cred[g];
    end
    endgenerate

    wire [VC-1:0] elig = req_valid & has;

    // Round-robin: rotate so the pointer's VC is bit 0, isolate the lowest set
    // bit, rotate its index back. VC <= 8 keeps the isolate a single level.
    wire [2*VC-1:0] dbl = {elig, elig} >> rr;
    wire [VC-1:0]   rot = dbl[VC-1:0];
    wire [VC-1:0]   low = rot & (~rot + {{(VC-1){1'b0}}, 1'b1});

    reg [VCW-1:0] low_ix;
    integer i;
    always @(*) begin
        low_ix = {VCW{1'b0}};
        for (i = 0; i < VC; i = i + 1) begin
            if (low[i]) begin
                low_ix = low_ix | i[VCW-1:0];
            end
        end
    end
    wire [VCW:0]   sum   = {1'b0, low_ix} + {1'b0, rr};
    wire [VCW-1:0] pick  = (sum >= VC) ? (sum[VCW-1:0] - VC_C) : sum[VCW-1:0];
    wire           go    = |elig;

    reg [VC-1:0] grant;
    always @(*) begin
        grant = {VC{1'b0}};
        if (go) begin
            grant[pick] = 1'b1;
        end
    end
    assign req_take = grant;

    // A COMPARE PER VC, NOT `req_flit[pick*W +: W]`. The dynamic part-select
    // builds a barrel select across the whole VC*W vector: measured at 433 LUT
    // for VC=2, W=288, where the 2:1 it should be is ~144.
    reg [W-1:0] flit_mux;
    integer f;
    always @(*) begin
        flit_mux = req_flit[0 +: W];
        for (f = 1; f < VC; f = f + 1) begin
            if (pick == f[VCW-1:0]) begin
                flit_mux = req_flit[f*W +: W];
            end
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            tx_valid <= 1'b0;
            tx_vc    <= {VCW{1'b0}};
            tx_last  <= 1'b0;
            rr       <= {VCW{1'b0}};
        end
        else begin
            tx_valid <= go;
            tx_vc    <= pick;
            tx_last  <= req_last[pick];
            if (go) begin
                rr <= (pick == VC - 1) ? {VCW{1'b0}} : pick + 1'b1;
            end
        end
        if (go) begin
            tx_flit <= flit_mux;
        end
    end

    // Credits: minus the grant, plus the return, both possibly on one VC in
    // one cycle.
    integer v;
    always @(posedge clk) begin
        for (v = 0; v < VC; v = v + 1) begin
            if (rst) begin
                cred[v] <= {CW{1'b0}};
            end
            else begin
                cred[v] <= cred[v]
                         - {{(CW-1){1'b0}}, grant[v]}
                         + ((crd_valid && (crd_vc == v[VCW-1:0])) ? crd_n : {CN_W{1'b0}});
            end
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (!rst) begin
        for (v = 0; v < VC; v = v + 1) begin
            if (cred[v] > CMAX) begin
                $display("%0t ERROR kts_tx: VC %0d holds %0d credits, more than CMAX %0d",
                         $time, v, cred[v], CMAX);
            end
        end
    end
`endif

endmodule

`default_nettype wire

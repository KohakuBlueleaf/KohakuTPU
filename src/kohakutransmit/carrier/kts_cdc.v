// A clock crossing on a surface: flits cross a->b through a dual-clock FIFO,
// credits cross b->a as COUNTS -- accumulated per VC on the b side and pushed
// through a second dual-clock FIFO as it has room, so a credit is never lost,
// not even while that FIFO is still coming out of reset. The forward FIFO
// cannot overflow: the sender's credits bound the flits in flight, and no
// credit reaches the sender before this side's forward FIFO is listening.

`default_nettype none

module kts_cdc #(
    parameter integer W     = 288,
    parameter integer VC    = 2,
    parameter integer D     = 32,              // the link's credits per VC
    parameter integer CN_W  = 4,
    parameter integer DEPTH = (VC * D < 16) ? 16 : VC * D,   // power of 2
    parameter         MEM   = "distributed",
    parameter integer VCW   = (VC <= 1) ? 1 : $clog2(VC),
    parameter integer CW    = $clog2(D) + 1
)(
    input  wire              a_clk,
    input  wire              a_rst,
    input  wire              b_clk,
    input  wire              b_rst,

    input  wire              i_valid,
    input  wire [VCW-1:0]    i_vc,
    input  wire              i_last,
    input  wire [W-1:0]      i_flit,
    output reg               o_valid,
    output reg  [VCW-1:0]    o_vc,
    output reg               o_last,
    output reg  [W-1:0]      o_flit,

    input  wire              i_crd_valid,
    input  wire [VCW-1:0]    i_crd_vc,
    input  wire [CN_W-1:0]   i_crd_n,
    output reg               o_crd_valid,
    output reg  [VCW-1:0]    o_crd_vc,
    output reg  [CN_W-1:0]   o_crd_n
);
    localparam integer FW = VCW + 1 + W;
    localparam integer BW = VCW + CN_W;
    localparam [CN_W-1:0] CN_MAX = {CN_W{1'b1}};
    localparam [VCW-1:0]  VC_C   = VC;

    // ---- forward: a -> b ------------------------------------------------------
    wire          f_full, f_empty;
    wire [FW-1:0] f_rd;
    kts_afifo #(.W(FW), .DEPTH(DEPTH), .MEM(MEM)) u_f (
        .wr_clk(a_clk), .wr_rst(a_rst),
        .wr_en(i_valid), .wr_data({i_vc, i_last, i_flit}), .full(f_full),
        .rd_clk(b_clk), .rd_en(!f_empty), .rd_data(f_rd), .empty(f_empty)
    );
    always @(posedge b_clk) begin
        if (b_rst) begin
            o_valid <= 1'b0;
        end
        else begin
            o_valid <= !f_empty;
        end
        {o_vc, o_last, o_flit} <= f_rd;
    end

    // ---- backward: b -> a, accumulated per VC ---------------------------------
    reg  [CW-1:0]  acc [0:VC-1];
    reg  [VCW-1:0] rr;
    wire           b_full, b_empty;
    wire [BW-1:0]  b_rd;

    wire [VC-1:0] due;
    genvar g;
    generate
    for (g = 0; g < VC; g = g + 1) begin : g_due
        assign due[g] = (acc[g] != {CW{1'b0}});
    end
    endgenerate
    wire [2*VC-1:0] dbl = {due, due} >> rr;
    wire [VC-1:0]   rot = dbl[VC-1:0];
    wire [VC-1:0]   low = rot & (~rot + {{(VC-1){1'b0}}, 1'b1});
    reg  [VCW-1:0]  low_ix;
    integer i;
    always @(*) begin
        low_ix = {VCW{1'b0}};
        for (i = 0; i < VC; i = i + 1) begin
            if (low[i]) begin
                low_ix = low_ix | i[VCW-1:0];
            end
        end
    end
    wire [VCW:0]    sum  = {1'b0, low_ix} + {1'b0, rr};
    wire [VCW-1:0]  pick = (sum >= VC) ? (sum[VCW-1:0] - VC_C) : sum[VCW-1:0];
    wire            push = (|due) && !b_full;
    wire [CW-1:0]   have = acc[pick];
    wire [CN_W-1:0] n    = (have > CN_MAX) ? CN_MAX : have[CN_W-1:0];

    integer v;
    always @(posedge b_clk) begin
        if (b_rst) begin
            rr <= {VCW{1'b0}};
        end
        else if (push) begin
            rr <= (pick == VC - 1) ? {VCW{1'b0}} : pick + 1'b1;
        end
        for (v = 0; v < VC; v = v + 1) begin
            if (b_rst) begin
                acc[v] <= {CW{1'b0}};
            end
            else begin
                acc[v] <= acc[v]
                        + ((i_crd_valid && (i_crd_vc == v[VCW-1:0])) ? i_crd_n : {CN_W{1'b0}})
                        - ((push && (pick == v[VCW-1:0])) ? n : {CN_W{1'b0}});
            end
        end
    end

    kts_afifo #(.W(BW), .DEPTH(DEPTH), .MEM("distributed")) u_b (
        .wr_clk(b_clk), .wr_rst(b_rst),
        .wr_en(push), .wr_data({pick, n}), .full(b_full),
        .rd_clk(a_clk), .rd_en(!b_empty && !f_full), .rd_data(b_rd), .empty(b_empty)
    );
    // No credit reaches the sender while the forward FIFO is still in reset.
    always @(posedge a_clk) begin
        if (a_rst) begin
            o_crd_valid <= 1'b0;
        end
        else begin
            o_crd_valid <= !b_empty && !f_full;
        end
        {o_crd_vc, o_crd_n} <= b_rd;
    end

`ifndef SYNTHESIS
    always @(posedge a_clk) if (!a_rst && i_valid && f_full) begin
        $display("%0t ERROR kts_cdc: forward FIFO full -- more flits in flight than credits", $time);
    end
    always @(posedge b_clk) if (!b_rst) begin
        for (v = 0; v < VC; v = v + 1) begin
            if (acc[v] > D) begin
                $display("%0t ERROR kts_cdc: VC %0d accumulated %0d credits, more than D %0d", $time, v, acc[v], D);
            end
        end
    end
`endif

endmodule

`default_nettype wire

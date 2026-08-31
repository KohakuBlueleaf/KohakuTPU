// A surface carried by AXI4-Stream: flits leave on one stream ({vc, last} in
// tuser), credits leave on a second, and the two streams arriving from the far
// end become the surface on this side. The stream's `tready` never reaches
// the surface: flits wait in a FIFO the credits can never overflow, credits
// wait as counts. Whatever sits between the two streams -- register slices,
// clock converters, a switch fabric -- is the wire's length, nothing more.

`default_nettype none

module kts_over_axis #(
    parameter integer W     = 288,
    parameter integer VC    = 2,
    parameter integer D     = 32,              // the link's credits per VC
    parameter integer CN_W  = 4,
    parameter integer DEPTH = (VC * D < 16) ? 16 : VC * D,
    parameter         MEM   = "distributed",
    parameter integer VCW   = (VC <= 1) ? 1 : $clog2(VC),
    parameter integer CW    = $clog2(D) + 1,
    parameter integer UW    = VCW + 1
)(
    input  wire              clk,
    input  wire              rst,

    // the surface, this side
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
    output reg  [CN_W-1:0]   o_crd_n,

    // flits out / in
    output wire              mf_tvalid,
    input  wire              mf_tready,
    output wire [W-1:0]      mf_tdata,
    output wire [UW-1:0]     mf_tuser,
    input  wire              sf_tvalid,
    output wire              sf_tready,
    input  wire [W-1:0]      sf_tdata,
    input  wire [UW-1:0]     sf_tuser,

    // credits out / in
    output wire              mc_tvalid,
    input  wire              mc_tready,
    output wire [VCW+CN_W-1:0] mc_tdata,
    input  wire              sc_tvalid,
    output wire              sc_tready,
    input  wire [VCW+CN_W-1:0] sc_tdata
);
    localparam [CN_W-1:0] CN_MAX = {CN_W{1'b1}};
    localparam [VCW-1:0]  VC_C   = VC;

    // ---- flits out: FIFO, then the stream ------------------------------------
    wire             f_full, f_empty;
    wire [W+UW-1:0]  f_rd;
    kts_fifo #(.W(W + UW), .DEPTH(DEPTH), .MEM(MEM)) u_f (
        .clk(clk), .rst(rst),
        .wr_en(i_valid), .wr_data({i_vc, i_last, i_flit}), .full(f_full),
        .rd_en(mf_tvalid && mf_tready), .rd_data(f_rd), .empty(f_empty)
    );
    assign mf_tvalid = !f_empty;
    assign mf_tuser  = f_rd[W +: UW];
    assign mf_tdata  = f_rd[W-1:0];

    // ---- flits in: the stream, registered onto the surface --------------------
    assign sf_tready = 1'b1;
    always @(posedge clk) begin
        if (rst) begin
            o_valid <= 1'b0;
        end
        else begin
            o_valid <= sf_tvalid;
        end
        {o_vc, o_last} <= sf_tuser;
        o_flit         <= sf_tdata;
    end

    // ---- credits out: accumulated per VC, one count per beat --------------------
    reg  [CW-1:0]  acc [0:VC-1];
    reg  [VCW-1:0] rr;
    wire [VC-1:0]  due;
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
    wire [CW-1:0]   have = acc[pick];
    wire [CN_W-1:0] n    = (have > CN_MAX) ? CN_MAX : have[CN_W-1:0];
    assign mc_tvalid = |due;
    assign mc_tdata  = {pick, n};
    wire   push      = mc_tvalid && mc_tready;

    integer v;
    always @(posedge clk) begin
        if (rst) begin
            rr <= {VCW{1'b0}};
        end
        else if (push) begin
            rr <= (pick == VC - 1) ? {VCW{1'b0}} : pick + 1'b1;
        end
        for (v = 0; v < VC; v = v + 1) begin
            if (rst) begin
                acc[v] <= {CW{1'b0}};
            end
            else begin
                acc[v] <= acc[v]
                        + ((i_crd_valid && (i_crd_vc == v[VCW-1:0])) ? i_crd_n : {CN_W{1'b0}})
                        - ((push && (pick == v[VCW-1:0])) ? n : {CN_W{1'b0}});
            end
        end
    end

    // ---- credits in ---------------------------------------------------------------
    assign sc_tready = 1'b1;
    always @(posedge clk) begin
        if (rst) begin
            o_crd_valid <= 1'b0;
        end
        else begin
            o_crd_valid <= sc_tvalid;
        end
        {o_crd_vc, o_crd_n} <= sc_tdata;
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (!rst && i_valid && f_full) begin
        $display("%0t ERROR kts_over_axis: flit FIFO full -- more flits in flight than credits", $time);
    end
`endif

endmodule

`default_nettype wire

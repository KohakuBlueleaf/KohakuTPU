// A surface carried by AXI4 writes: every flit is a single-beat posted write
// to the far end's flit window, every credit count a write to its credit
// window, and this end's own two windows turn the far end's writes back into
// the surface. Two of these across any AXI4 interconnect -- register slices,
// clock converters, a crossbar -- are one link; the interconnect's latency is
// the round trip the credits absorb. The windows are write-only.

`default_nettype none

module kts_over_axi4 #(
    parameter integer W        = 288,
    parameter integer VC       = 2,
    parameter integer D        = 32,
    parameter integer CN_W     = 4,
    parameter integer DEPTH    = (VC * D < 16) ? 16 : VC * D,
    parameter         MEM      = "distributed",
    parameter integer ADDR_W   = 32,
    parameter integer DATA_W   = 512,                  // >= W + VCW + 1
    parameter integer ID_W     = 4,
    parameter [31:0]  FLIT_AT  = 32'h0000_0000,        // the far end's flit window
    parameter [31:0]  CRD_AT   = 32'h0000_1000,        // the far end's credit window
    parameter [31:0]  MY_FLIT  = 32'h0000_0000,        // this end's windows, as
    parameter [31:0]  MY_CRD   = 32'h0000_1000,        // the interconnect addresses them
    parameter integer VCW      = (VC <= 1) ? 1 : $clog2(VC),
    parameter integer CW       = $clog2(D) + 1
)(
    input  wire                 clk,
    input  wire                 rst,

    input  wire                 i_valid,
    input  wire [VCW-1:0]       i_vc,
    input  wire                 i_last,
    input  wire [W-1:0]         i_flit,
    output reg                  o_valid,
    output reg  [VCW-1:0]       o_vc,
    output reg                  o_last,
    output reg  [W-1:0]         o_flit,
    input  wire                 i_crd_valid,
    input  wire [VCW-1:0]       i_crd_vc,
    input  wire [CN_W-1:0]      i_crd_n,
    output reg                  o_crd_valid,
    output reg  [VCW-1:0]       o_crd_vc,
    output reg  [CN_W-1:0]      o_crd_n,

    output wire [ID_W-1:0]      m_awid,
    output wire [ADDR_W-1:0]    m_awaddr,
    output wire [7:0]           m_awlen,
    output wire [2:0]           m_awsize,
    output wire [1:0]           m_awburst,
    output wire                 m_awvalid,
    input  wire                 m_awready,
    output wire [DATA_W-1:0]    m_wdata,
    output wire [DATA_W/8-1:0]  m_wstrb,
    output wire                 m_wlast,
    output wire                 m_wvalid,
    input  wire                 m_wready,
    input  wire [ID_W-1:0]      m_bid,
    input  wire [1:0]           m_bresp,
    input  wire                 m_bvalid,
    output wire                 m_bready,

    input  wire [ID_W-1:0]      s_awid,
    input  wire [ADDR_W-1:0]    s_awaddr,
    input  wire [7:0]           s_awlen,
    input  wire                 s_awvalid,
    output wire                 s_awready,
    input  wire [DATA_W-1:0]    s_wdata,
    input  wire                 s_wlast,
    input  wire                 s_wvalid,
    output wire                 s_wready,
    output reg  [ID_W-1:0]      s_bid,
    output wire [1:0]           s_bresp,
    output reg                  s_bvalid,
    input  wire                 s_bready
);
    localparam integer FW = VCW + 1 + W;
    localparam [CN_W-1:0] CN_MAX = {CN_W{1'b1}};
    localparam [VCW-1:0]  VC_C   = VC;
    localparam [2:0] SIZE = $clog2(DATA_W / 8);

    // ---- outgoing: flit FIFO and credit accumulators feed one write master --------
    wire          f_full, f_empty;
    wire [FW-1:0] f_rd;
    kts_fifo #(.W(FW), .DEPTH(DEPTH), .MEM(MEM)) u_f (
        .clk(clk), .rst(rst),
        .wr_en(i_valid), .wr_data({i_vc, i_last, i_flit}), .full(f_full),
        .rd_en(f_go), .rd_data(f_rd), .empty(f_empty)
    );

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

    // One write at a time: AW and W offered together, each accepted once; a
    // credit write goes before a flit write.
    reg        busy, aw_done, w_done, is_crd;
    reg [ADDR_W-1:0] addr_q;
    reg [DATA_W-1:0] data_q;
    wire crd_go = !busy && (|due);
    wire f_go   = !busy && !(|due) && !f_empty;
    wire push   = crd_go;

    always @(posedge clk) begin
        if (rst) begin
            busy <= 1'b0; aw_done <= 1'b0; w_done <= 1'b0; is_crd <= 1'b0;
            rr <= {VCW{1'b0}};
        end
        else begin
            if (crd_go) begin
                busy <= 1'b1; aw_done <= 1'b0; w_done <= 1'b0; is_crd <= 1'b1;
                addr_q <= CRD_AT[ADDR_W-1:0];
                data_q <= {{(DATA_W-VCW-CN_W){1'b0}}, pick, n};
                rr <= (pick == VC - 1) ? {VCW{1'b0}} : pick + 1'b1;
            end
            else if (f_go) begin
                busy <= 1'b1; aw_done <= 1'b0; w_done <= 1'b0; is_crd <= 1'b0;
                addr_q <= FLIT_AT[ADDR_W-1:0];
                data_q <= {{(DATA_W-FW){1'b0}}, f_rd};
            end
            else if (busy) begin
                if (m_awvalid && m_awready) begin
                    aw_done <= 1'b1;
                end
                if (m_wvalid && m_wready) begin
                    w_done <= 1'b1;
                end
                if ((aw_done || (m_awvalid && m_awready)) && (w_done || (m_wvalid && m_wready))) begin
                    busy <= 1'b0;
                end
            end
        end
        for (i = 0; i < VC; i = i + 1) begin
            if (rst) begin
                acc[i] <= {CW{1'b0}};
            end
            else begin
                acc[i] <= acc[i]
                        + ((i_crd_valid && (i_crd_vc == i[VCW-1:0])) ? i_crd_n : {CN_W{1'b0}})
                        - ((push && (pick == i[VCW-1:0])) ? n : {CN_W{1'b0}});
            end
        end
    end

    assign m_awid    = {ID_W{1'b0}};
    assign m_awaddr  = addr_q;
    assign m_awlen   = 8'd0;
    assign m_awsize  = SIZE;
    assign m_awburst = 2'b01;
    assign m_awvalid = busy && !aw_done;
    assign m_wdata   = data_q;
    assign m_wstrb   = {(DATA_W/8){1'b1}};
    assign m_wlast   = 1'b1;
    assign m_wvalid  = busy && !w_done;
    assign m_bready  = 1'b1;                           // posted: B is discarded

    // ---- incoming: the two windows ----------------------------------------------------
    // AW and W are accepted whenever offered; the address of the last AW pairs
    // with the next W (AXI keeps them in order).
    reg              aw_q_v;
    reg [ADDR_W-1:0] aw_q;
    reg [ID_W-1:0]   awid_q;
    assign s_awready = !aw_q_v || (s_wvalid && s_wready);
    assign s_wready  = aw_q_v || s_awvalid;
    wire   w_take    = s_wvalid && s_wready;
    wire [ADDR_W-1:0] w_addr = aw_q_v ? aw_q : s_awaddr;
    wire   hit_flit  = (w_addr[ADDR_W-1:4] == MY_FLIT[ADDR_W-1:4]);
    wire   hit_crd   = (w_addr[ADDR_W-1:4] == MY_CRD[ADDR_W-1:4]);
    assign s_bresp   = 2'b00;

    always @(posedge clk) begin
        if (rst) begin
            aw_q_v      <= 1'b0;
            o_valid     <= 1'b0;
            o_crd_valid <= 1'b0;
            s_bvalid    <= 1'b0;
        end
        else begin
            if (s_awvalid && s_awready && !(w_take && !aw_q_v)) begin
                aw_q_v <= 1'b1; aw_q <= s_awaddr; awid_q <= s_awid;
            end
            else if (w_take) begin
                aw_q_v <= 1'b0;
            end
            o_valid     <= w_take && hit_flit;
            o_crd_valid <= w_take && hit_crd;
            if (w_take && s_wlast) begin
                s_bvalid <= 1'b1;
                s_bid    <= aw_q_v ? awid_q : s_awid;
            end
            else if (s_bready) begin
                s_bvalid <= 1'b0;
            end
        end
        {o_vc, o_last, o_flit} <= s_wdata[FW-1:0];
        {o_crd_vc, o_crd_n}    <= s_wdata[VCW+CN_W-1:0];
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (!rst) begin
        if (i_valid && f_full) begin
            $display("%0t ERROR kts_over_axi4: flit FIFO full -- more flits in flight than credits", $time);
        end
        if (w_take && !hit_flit && !hit_crd) begin
            $display("%0t ERROR kts_over_axi4: write to %h hits neither window", $time, w_addr);
        end
    end
`endif

endmodule

`default_nettype wire

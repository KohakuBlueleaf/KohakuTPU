// The receiving end of a surface. A FIFO per virtual channel, exactly D deep.
// Every credit the sender will ever hold is issued from here: D per VC once
// the buffers are out of reset, then one for every flit the consumer pops --
// so the FIFO can never overflow and no flit is ever dropped. Returns are
// batched CRD_BATCH at a time (or after TIMEOUT idle cycles) to keep the
// backward wire quiet. The incoming flit and the outgoing credit are both
// registers.

`default_nettype none

module kts_rx #(
    parameter integer W         = 288,
    parameter integer VC        = 2,
    parameter integer D         = 32,           // power of 2, >= 16 (xpm)
    parameter integer CN_W      = 4,
    parameter integer CRD_BATCH = 4,            // 1 .. 2**CN_W - 1
    parameter integer TIMEOUT   = 16,           // 0: batch only
    parameter         MEM       = "distributed",
    parameter integer VCW       = (VC <= 1) ? 1 : $clog2(VC),
    parameter integer CW        = $clog2(D) + 1
)(
    input  wire              clk,
    input  wire              rst,

    input  wire              rx_valid,
    input  wire [VCW-1:0]    rx_vc,
    input  wire              rx_last,
    input  wire [W-1:0]      rx_flit,

    output wire [VC-1:0]     out_valid,
    output wire [VC-1:0]     out_last,
    output wire [VC*W-1:0]   out_flit,
    input  wire [VC-1:0]     out_pop,

    output reg               crd_valid,
    output reg  [VCW-1:0]    crd_vc,
    output reg  [CN_W-1:0]   crd_n
);
    localparam integer TW = (TIMEOUT <= 1) ? 1 : $clog2(TIMEOUT + 1);
    localparam [CN_W-1:0] CN_MAX  = {CN_W{1'b1}};
    localparam [CW-1:0]   BATCH_C = CRD_BATCH;
    localparam [CW-1:0]   D_C     = D;
    localparam [TW-1:0]   TO_C    = TIMEOUT;
    localparam [VCW-1:0]  VC_C    = VC;

    // ---- landing register --------------------------------------------------
    reg           q_valid;
    reg [VCW-1:0] q_vc;
    reg           q_last;
    reg [W-1:0]   q_flit;
    always @(posedge clk) begin
        if (rst) begin
            q_valid <= 1'b0;
        end
        else begin
            q_valid <= rx_valid;
        end
        q_vc   <= rx_vc;
        q_last <= rx_last;
        q_flit <= rx_flit;
    end

    // ---- one buffer per VC ---------------------------------------------------
    wire [VC-1:0] pop   = out_pop & out_valid;
    wire [VC-1:0] fullv;

    genvar g;
    generate
    for (g = 0; g < VC; g = g + 1) begin : g_vc
        wire         wr = q_valid && (q_vc == g[VCW-1:0]);
        wire         empty;
        wire [W:0]   rd;
        kts_fifo #(.W(W + 1), .DEPTH(D), .MEM(MEM)) u_f (
            .clk(clk), .rst(rst),
            .wr_en(wr), .wr_data({q_last, q_flit}), .full(fullv[g]),
            .rd_en(pop[g]), .rd_data(rd), .empty(empty)
        );
        assign out_valid[g]         = !empty;
        assign out_last[g]          = rd[W];
        assign out_flit[g*W +: W]   = rd[W-1:0];
    end
    endgenerate

    // ---- credits owed, returned in batches ----------------------------------
    reg [CW-1:0] owed [0:VC-1];
    reg [TW-1:0] age  [0:VC-1];
    reg [VCW-1:0] rr;

    wire [VC-1:0] due;
    generate
    for (g = 0; g < VC; g = g + 1) begin : g_due
        assign due[g] = (owed[g] >= BATCH_C)
                     || ((TIMEOUT != 0) && (owed[g] != {CW{1'b0}})
                         && (age[g] >= TO_C));
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
    wire [VCW:0]   sum  = {1'b0, low_ix} + {1'b0, rr};
    wire [VCW-1:0] pick = (sum >= VC) ? (sum[VCW-1:0] - VC_C) : sum[VCW-1:0];
    // No credit leaves while a buffer is still in reset (xpm holds `full`
    // for several cycles after `rst` drops).
    wire           emit = (|due) && !(|fullv);
    wire [CW-1:0]  have = owed[pick];
    wire [CN_W-1:0] n   = (have > CN_MAX) ? CN_MAX : have[CN_W-1:0];

    integer v;
    always @(posedge clk) begin
        if (rst) begin
            crd_valid <= 1'b0;
            crd_vc    <= {VCW{1'b0}};
            crd_n     <= {CN_W{1'b0}};
            rr        <= {VCW{1'b0}};
        end
        else begin
            crd_valid <= emit;
            crd_vc    <= pick;
            crd_n     <= n;
            if (emit) begin
                rr <= (pick == VC - 1) ? {VCW{1'b0}} : pick + 1'b1;
            end
        end
        for (v = 0; v < VC; v = v + 1) begin
            if (rst) begin
                owed[v] <= D_C;
                age[v]  <= {TW{1'b0}};
            end
            else begin
                owed[v] <= owed[v]
                         + {{(CW-1){1'b0}}, pop[v]}
                         - ((emit && (pick == v[VCW-1:0])) ? n : {CN_W{1'b0}});
                if (pop[v] || (emit && (pick == v[VCW-1:0]))) begin
                    age[v] <= {TW{1'b0}};
                end
                else if ((owed[v] != {CW{1'b0}}) && (age[v] != {TW{1'b1}})) begin
                    age[v] <= age[v] + 1'b1;
                end
            end
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) if (!rst) begin
        for (v = 0; v < VC; v = v + 1) begin
            if (q_valid && (q_vc == v[VCW-1:0]) && fullv[v]) begin
                $display("%0t ERROR kts_rx: VC %0d buffer full on arrival -- the sender spent a credit it did not have",
                         $time, v);
            end
        end
    end
`endif

endmodule

`default_nettype wire

// A surface out as AXI4-Stream: the receiving end's per-VC heads, round-robin
// between VCs at packet boundaries, `last` as tlast, the VC as tdest. The
// stream's `tready` pops the head; credits go back as the beats leave.

`default_nettype none

module kts_axis_out #(
    parameter integer W     = 288,
    parameter integer VC    = 2,
    parameter integer D     = 32,
    parameter integer CN_W  = 4,
    parameter         MEM   = "distributed",
    parameter integer VCW   = (VC <= 1) ? 1 : $clog2(VC)
)(
    input  wire              clk,
    input  wire              rst,

    input  wire              rx_valid,
    input  wire [VCW-1:0]    rx_vc,
    input  wire              rx_last,
    input  wire [W-1:0]      rx_flit,
    output wire              crd_valid,
    output wire [VCW-1:0]    crd_vc,
    output wire [CN_W-1:0]   crd_n,

    output wire              m_tvalid,
    input  wire              m_tready,
    output wire [W-1:0]      m_tdata,
    output wire              m_tlast,
    output wire [VCW-1:0]    m_tdest
);
    wire [VC-1:0]   hv, hl;
    wire [VC*W-1:0] hf;
    reg  [VC-1:0]   pop;
    kts_rx #(.W(W), .VC(VC), .D(D), .CN_W(CN_W), .MEM(MEM)) u_rx (
        .clk(clk), .rst(rst),
        .rx_valid(rx_valid), .rx_vc(rx_vc), .rx_last(rx_last), .rx_flit(rx_flit),
        .out_valid(hv), .out_last(hl), .out_flit(hf), .out_pop(pop),
        .crd_valid(crd_valid), .crd_vc(crd_vc), .crd_n(crd_n)
    );

    // Packet-locked round-robin over the VCs.
    reg           locked;
    reg [VCW-1:0] cur, rr;
    wire [2*VC-1:0] dbl = {hv, hv} >> rr;
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
    wire [VCW:0]   sum = {1'b0, low_ix} + {1'b0, rr};
    wire [VCW-1:0] nxt = (sum >= VC) ? (sum[VCW-1:0] - VC[VCW-1:0]) : sum[VCW-1:0];
    wire [VCW-1:0] sel = locked ? cur : nxt;

    // A compare per VC, not `hf[sel*W +: W]`: the dynamic part-select builds a
    // barrel select across the whole VC*W vector instead of the VC:1 it is.
    reg [W-1:0] data_mux;
    integer d;
    always @(*) begin
        data_mux = hf[0 +: W];
        for (d = 1; d < VC; d = d + 1) begin
            if (sel == d[VCW-1:0]) begin
                data_mux = hf[d*W +: W];
            end
        end
    end

    assign m_tvalid = locked ? hv[cur] : (|hv);
    assign m_tdata  = data_mux;
    assign m_tlast  = hl[sel];
    assign m_tdest  = sel;
    wire   beat     = m_tvalid && m_tready;

    always @(*) begin
        pop = {VC{1'b0}};
        if (beat) begin
            pop[sel] = 1'b1;
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            locked <= 1'b0;
            cur    <= {VCW{1'b0}};
            rr     <= {VCW{1'b0}};
        end
        else if (beat) begin
            locked <= !m_tlast;
            cur    <= sel;
            if (m_tlast) begin
                rr <= (sel == VC - 1) ? {VCW{1'b0}} : sel + 1'b1;
            end
        end
    end

endmodule

`default_nettype wire

// kx_lane -- one source's stream through NT partitions in one direction: a
// chain of kx_hop, one per boundary, with a tap after each. At tap t the
// landing buffer's head is examined once: TAKE[t][dst] says whether this
// partition consumes it (t_valid) or the next hop forwards it. The head feeds
// both the tap and the next hop's TX register -- only the valids differ, so
// nothing is muxed in transit. In order per lane by construction. Each hop's
// halves take the resets of the partitions they sit in.

`default_nettype none

module kx_lane #(
    parameter integer W     = 64,               // payload
    parameter integer DW    = 2,                // destination index width
    parameter integer NT    = 1,                // taps = boundaries crossed
    // TAKE[t*(1<<DW) + d] = 1: tap t consumes flits for destination d
    parameter [NT*(1<<DW)-1:0] TAKE = {(NT*(1<<DW)){1'b0}},
    parameter integer DEPTH = 16,
    parameter         MEM   = "block",
    parameter         BUF   = "lean",
    parameter integer RX_REG = 0
)(
    input  wire             clk,
    input  wire             rstn_s,             // the source partition's reset
    input  wire [NT-1:0]    rstn_t,             // tap t's partition's reset

    input  wire             s_valid,
    output wire             s_ready,
    input  wire [DW-1:0]    s_dst,
    input  wire [W-1:0]     s_data,

    output wire [NT-1:0]    t_valid,
    input  wire [NT-1:0]    t_ready,
    output wire [NT*DW-1:0] t_dst,
    output wire [NT*W-1:0]  t_data
);
    localparam integer ND = 1 << DW;
    localparam integer FW = DW + W;
    wire [NT-1:0] hv, hr, fv, fr;
    wire [FW-1:0] hd [0:NT-1];
    genvar t;
    generate for (t = 0; t < NT; t = t + 1) begin : g_t
        wire          iv, ir, srst;
        wire [FW-1:0] id;
        if (t == 0) begin : g_src
            assign iv = s_valid;  assign s_ready = ir;
            assign id = {s_dst, s_data};
            assign srst = rstn_s;
        end else begin : g_fwd
            assign iv = fv[t-1];  assign fr[t-1] = ir;
            assign id = hd[t-1];
            assign srst = rstn_t[t-1];
        end
        // dst and the payload's top bit (a flit's kind) decode the head: fast bits
        kx_hop #(.WIDTH(FW), .DEPTH(DEPTH), .MEM(MEM), .BUF(BUF), .FASTW(DW + 1),
                 .RX_REG(RX_REG)) u_h (
            .clk(clk), .s_rstn(srst), .m_rstn(rstn_t[t]),
            .s_valid(iv), .s_ready(ir), .s_data(id),
            .m_valid(hv[t]), .m_ready(hr[t]), .m_data(hd[t]));
        wire [DW-1:0] dst  = hd[t][W +: DW];
        wire          take = TAKE[t*ND + dst];
        assign t_valid[t]          = hv[t] && take;
        assign t_dst[t*DW +: DW]   = dst;
        assign t_data[t*W +: W]    = hd[t][W-1:0];
        if (t < NT - 1) begin : g_more
            assign fv[t] = hv[t] && !take;
            assign hr[t] = take ? t_ready[t] : fr[t];
        end else begin : g_last
            // nothing beyond the last tap: a flit no tap takes is consumed,
            // so a mapping error cannot wedge the lane
            assign fv[t] = 1'b0;
            assign fr[t] = 1'b0;
            assign hr[t] = take ? t_ready[t] : 1'b1;
`ifndef SYNTHESIS
            always @(posedge clk) begin
                if (rstn_t[t] && hv[t] && !take) begin
                    $display("%0t ERROR kx_lane: flit for destination %0d passed the last tap untaken", $time, dst);
                end
            end
`endif
        end
    end endgenerate
endmodule

`default_nettype wire

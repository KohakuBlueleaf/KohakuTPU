// kohaku_aring -- the lean dual-clock ring: one LUTRAM array, one gray-coded
// pointer crossing, one output register. A 523 x 16 ring is ~300 LUT, all of
// it the LUTRAM but for the pointers.
//
// FULL 0: the sender's credits bound occupancy to DEPTH, so there is no full
// flag and no read-pointer crossing. FULL 1: a full flag from the read pointer
// crossed back, for a valid/ready edge with no credits.

`default_nettype none

module kohaku_aring #(
    parameter integer WIDTH = 64,
    parameter integer DEPTH = 16,               // power of two
    parameter integer FULL  = 0
)(
    input  wire             wr_clk,
    input  wire             wr_rstn,
    input  wire             wr_en,
    input  wire [WIDTH-1:0] wr_data,
    output wire             wr_busy,            // FULL 1: full; else in reset

    input  wire             clk,                // read domain
    input  wire             rstn,
    input  wire             rd_en,
    output reg  [WIDTH-1:0] rd_data,
    output wire             rd_busy             // no word presented
);
    localparam integer AW = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    // ---- storage: written on wr_clk, read asynchronously, registered once
    (* ram_style = "distributed" *) reg [WIDTH-1:0] mem [0:DEPTH-1];
    reg [AW:0] wp;                              // the extra bit tells full from empty
    always @(posedge wr_clk) begin
        if (wr_en) begin mem[wp[AW-1:0]] <= wr_data; end
    end

    // ---- write pointer and its gray image, two flops into the read domain
    wire [AW:0] wp_n = wp + 1'b1;
    reg  [AW:0] wp_g;
    always @(posedge wr_clk) begin
        if (!wr_rstn) begin wp <= 0; wp_g <= 0; end
        else if (wr_en) begin wp <= wp_n; wp_g <= wp_n ^ (wp_n >> 1); end
    end
    reg [AW:0] wg_s1, wg_s2;
    always @(posedge clk) begin
        if (!rstn) begin wg_s1 <= 0; wg_s2 <= 0; end
        else begin wg_s1 <= wp_g; wg_s2 <= wg_s1; end
    end
    reg [AW:0] wp_r;
    integer gi;
    always @(*) begin
        wp_r[AW] = wg_s2[AW];
        for (gi = AW - 1; gi >= 0; gi = gi - 1) begin
            wp_r[gi] = wp_r[gi+1] ^ wg_s2[gi];
        end
    end

    // ---- read side: one stage, issued when the output is free or being taken
    reg  [AW:0] rp;
    reg         o_v;
    wire        empty  = (rp == wp_r);
    wire        o_take = o_v && rd_en;
    wire        issue  = !empty && (!o_v || o_take);
    always @(posedge clk) begin
        if (!rstn) begin rp <= 0; o_v <= 1'b0; end
        else begin
            if (issue) begin rp <= rp + 1'b1; rd_data <= mem[rp[AW-1:0]]; end
            o_v <= issue || (o_v && !o_take);
        end
    end
    assign rd_busy = !o_v;

    // ---- full: the read pointer crossed back, where credits do not bound it
    generate if (FULL != 0) begin : g_full
        wire [AW:0] rp_n = rp + 1'b1;
        reg  [AW:0] rp_g, rg_s1, rg_s2, rp_w;
        always @(posedge clk) begin
            if (!rstn) begin rp_g <= 0; end
            else if (issue) begin rp_g <= rp_n ^ (rp_n >> 1); end
        end
        always @(posedge wr_clk) begin
            if (!wr_rstn) begin rg_s1 <= 0; rg_s2 <= 0; end
            else begin rg_s1 <= rp_g; rg_s2 <= rg_s1; end
        end
        integer gj;
        always @(*) begin
            rp_w[AW] = rg_s2[AW];
            for (gj = AW - 1; gj >= 0; gj = gj - 1) begin
                rp_w[gj] = rp_w[gj+1] ^ rg_s2[gj];
            end
        end
        assign wr_busy = !wr_rstn
                       || ((wp[AW-1:0] == rp_w[AW-1:0]) && (wp[AW] != rp_w[AW]));
    end else begin : g_nofull
        assign wr_busy = !wr_rstn;
    end endgenerate
endmodule

`default_nettype wire

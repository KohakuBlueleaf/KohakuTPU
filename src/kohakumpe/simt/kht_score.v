// kht_score -- the SIMT PE's scoreboard: one pending bit per vector register per
// wave, for every result that arrives later than the pipe (float, multiply,
// load, LDS atomic).
//
// It answers one question about the instruction in EX -- is any register it
// reads, or the one it writes, still owed a result -- and it reports every
// clear by wave, because a wave put to sleep on the scoreboard wakes on the next
// result its own registers receive (compute-unit.md s4.2).
//
// SET WINS OVER CLEAR on the same bit in the same cycle. That case cannot arise
// from a correct core -- an instruction whose destination is pending replays
// instead of issuing -- so it is asserted rather than relied on.

`default_nettype none

module kht_score #(
    parameter integer WAVES = 16,
    parameter integer WID   = (WAVES > 1) ? $clog2(WAVES) : 1
)(
    input  wire              clk,
    input  wire              resetn,

    // ---- the check, combinational, on the instruction in EX ----
    input  wire [WID-1:0]    chk_wave,
    input  wire [4:0]        chk_rs1,
    input  wire [4:0]        chk_rs2,
    input  wire [4:0]        chk_rd,
    // Which of the three fields this instruction actually uses.
    input  wire [2:0]        chk_use,
    output wire              hit,

    // ---- issue of a long-latency result: its destination becomes pending ----
    input  wire              set_v,
    input  wire [WID-1:0]    set_wave,
    input  wire [4:0]        set_rd,

    // ---- that result reaching the file ----
    input  wire              clr_v,
    input  wire [WID-1:0]    clr_wave,
    input  wire [4:0]        clr_rd,

    // One bit per wave: this wave had a register cleared this cycle.
    output wire [WAVES-1:0]  woke,
    // One bit per wave: some register of this wave is still owed a result. An
    // `ecall` waits on it, so a wave never ends with a result still in flight.
    output wire [WAVES-1:0]  wpend,
    // Any register of any wave still pending (the launch waits on this).
    output wire              any
);
    reg [31:0] pend [0:WAVES-1];

    wire [31:0] row = pend[chk_wave];
    // x0 is never owed anything: nothing writes it.
    assign hit = (chk_use[0] && (chk_rs1 != 5'd0) && row[chk_rs1])
              || (chk_use[1] && (chk_rs2 != 5'd0) && row[chk_rs2])
              || (chk_use[2] && (chk_rd  != 5'd0) && row[chk_rd]);

    genvar g;
    generate
    for (g = 0; g < WAVES; g = g + 1) begin : g_woke
        assign woke[g]  = clr_v && (clr_wave == g[WID-1:0]);
        assign wpend[g] = |pend[g];
    end
    endgenerate

    reg any_q;
    assign any = any_q;

    integer w;
    always @(posedge clk) begin
        if (!resetn) begin
            for (w = 0; w < WAVES; w = w + 1) begin
                pend[w] <= 32'd0;
            end
            any_q <= 1'b0;
        end else begin
            if (clr_v) begin
                pend[clr_wave][clr_rd] <= 1'b0;
            end
            if (set_v && (set_rd != 5'd0)) begin
                pend[set_wave][set_rd] <= 1'b1;
            end
            any_q <= 1'b0;
            for (w = 0; w < WAVES; w = w + 1) begin
                if (pend[w] != 32'd0) begin
                    any_q <= 1'b1;
                end
            end
        end
    end

`ifndef SYNTHESIS
    always @(posedge clk) begin
        if (resetn && set_v && clr_v && (set_wave == clr_wave) && (set_rd == clr_rd)
            && (set_rd != 5'd0)) begin
            $display("%0t ERROR kht_score: wave %0d x%0d set and cleared in one cycle -- an instruction issued over its own pending destination",
                     $time, set_wave, set_rd);
        end
        if (resetn && set_v && (set_rd != 5'd0) && pend[set_wave][set_rd]) begin
            $display("%0t ERROR kht_score: wave %0d x%0d set while already pending -- a WAW the check should have replayed",
                     $time, set_wave, set_rd);
        end
    end
`endif

endmodule

`default_nettype wire

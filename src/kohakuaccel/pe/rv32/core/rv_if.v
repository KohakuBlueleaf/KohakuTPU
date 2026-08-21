// rv_if -- instruction fetch: next-PC selection, the instruction window read,
// and branch prediction.
//
// TWO CYCLES, NOT ONE. The instruction window is block RAM with a registered
// output, so the address leaves this stage one cycle before the instruction
// arrives. That is the first of the two extra register boundaries this core
// has over the textbook five (docs/arch/pe/README.md s3): the address path is
// PC -> mux -> RAM address register and nothing else, which is what lets the
// fetch loop close at the target period.
//
// The predictor is read with the SAME address as the window, so the prediction
// for an instruction is available in the cycle that instruction's bits are, and
// a correctly predicted taken branch costs no bubble at all.
//
// A REDIRECT IS REGISTERED, deliberately. Resolving in EX and steering the same
// cycle would put the ALU output in the next-PC mux; taking one more cycle
// instead costs a third bubble on a mispredict and keeps the ALU output going
// nowhere but a flop. s12.2 of the design note prefers depth over that path.

`default_nettype none

module rv_if #(
    parameter integer IMEM_WORDS  = 2048,
    parameter integer BTB_ENTRIES = 32,
    parameter integer BTB_TAG_W   = 8
)(
    input  wire        clk,
    input  wire        resetn,

    input  wire        run,          // the core has been kicked and has not halted
    input  wire        hold,         // the whole front end is frozen this cycle
    input  wire        kill,         // discard what is in flight, at this edge

    input  wire        boot_v,       // load boot_pc and start fetching
    input  wire [31:0] boot_pc,

    input  wire        ex_redir,     // EX resolved something the fetch got wrong
    input  wire [31:0] ex_redir_pc,

    output wire [$clog2(IMEM_WORDS)-1:0] imem_addr,
    input  wire [31:0] imem_data,

    output reg  [31:0] f2_pc,
    output reg         f2_valid,
    output wire [31:0] f2_instr,
    output wire        f2_pred_taken,
    output wire [31:0] f2_pred_target,

    input  wire        u_valid,
    input  wire [31:0] u_pc,
    input  wire        u_taken,
    input  wire        u_is_jump,
    input  wire [31:0] u_target
);
    localparam integer IAW = $clog2(IMEM_WORDS);

    reg         redir_q;
    reg  [31:0] redir_pc_q;

    wire        pred_taken;
    wire [31:0] pred_target;

    wire fetch_en = !hold;

    wire [31:0] pc_seq = f2_pc + 32'd4;
    wire [31:0] pc_n   = redir_q                     ? redir_pc_q
                       : (f2_valid && pred_taken)    ? pred_target
                                                     : pc_seq;

    // One expression drives the PC register, the window address and the
    // predictor address, so the three can never disagree about which
    // instruction is being fetched.
    wire [31:0] pc_fetch = boot_v   ? boot_pc
                         : fetch_en ? pc_n
                                    : f2_pc;

    assign imem_addr = pc_fetch[IAW+1:2];

    always @(posedge clk) begin
        if (!resetn) begin
            f2_valid   <= 1'b0;
            f2_pc      <= 32'd0;
            redir_q    <= 1'b0;
            redir_pc_q <= 32'd0;
        end else begin
            if (boot_v) begin
                f2_pc    <= boot_pc;
                f2_valid <= 1'b1;
            end else if (!run) begin
                f2_valid <= 1'b0;
            end else if (fetch_en) begin
                f2_pc    <= pc_n;
                f2_valid <= 1'b1;
            end
            // After the enables above, so a redirect registered this edge wins
            // over the wrong-path fetch it is replacing.
            if (kill) f2_valid <= 1'b0;

            if (ex_redir) begin
                redir_q    <= 1'b1;
                redir_pc_q <= ex_redir_pc;
            end else if (fetch_en) begin
                // Held across a stall: the redirect must survive to the cycle
                // the front end is allowed to move again.
                redir_q <= 1'b0;
            end
        end
    end

    // AT ZERO ENTRIES THERE IS NO PREDICTOR, not a predictor of size zero:
    // static not-taken, and every taken branch pays the redirect. That is a
    // cycles-for-LUT trade and it has to remove the whole block to be one.
    generate
    if (BTB_ENTRIES == 0) begin : g_static
        assign pred_taken  = 1'b0;
        assign pred_target = 32'd0;
    end else begin : g_bpred
        rv_bpred #(.ENTRIES(BTB_ENTRIES), .TAG_W(BTB_TAG_W)) u_bp (
            .clk(clk), .resetn(resetn),
            .q_en(1'b1), .q_addr(pc_fetch), .q_pc(f2_pc),
            .q_taken(pred_taken), .q_target(pred_target),
            .u_valid(u_valid), .u_pc(u_pc), .u_taken(u_taken),
            .u_is_jump(u_is_jump), .u_target(u_target)
        );
    end
    endgenerate

    assign f2_instr       = imem_data;
    assign f2_pred_taken  = f2_valid && pred_taken;
    assign f2_pred_target = pred_target;

`ifndef SYNTHESIS
    // A PC outside the window aliases instead of faulting, and the symptom is a
    // program that executes something plausible from the wrong address.
    always @(posedge clk)
        if (resetn && run && f2_valid && (f2_pc[31:IAW+2] != 0))
            $display("%0t ERROR rv_if: PC %h is outside the %0d-word instruction window -- the fetch aliased",
                     $time, f2_pc, IMEM_WORDS);
`endif

endmodule

`default_nettype wire

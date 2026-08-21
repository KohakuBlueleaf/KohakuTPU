// rv_bpred -- a small BTB plus a 2-bit saturating table.
//
// Its job is not accuracy. It is to remove the taken-branch penalty from
// ordinary compiled loops, which a predict-not-taken core pays on every
// iteration. Nothing here is speculative state that has to be repaired: EX
// resolves every branch and jump against the architectural answer, so a wrong
// prediction costs the redirect penalty and never correctness. That is why the
// tag can be short and the table can alias.
//
// ONE WORD PER ENTRY: {valid, cnt, tag, target}. Held as indexed flop arrays,
// `valid` and `cnt` cost the entry count in flops AND in the mux that reads one
// -- measured at 102 / 228 / 455 LUT for 16 / 32 / 64 entries, while the tag
// LUTRAM did not move. Everything is in the array now, and the entry count buys
// depth rather than logic.
//
// THE ARRAY HAS NO RESET, so a power-on sweep writes every entry clean before
// the first prediction is allowed out.
//
// THE UPDATE ARRIVES ONE CYCLE AFTER THE RESOLVE. EX's comparator reaching the
// counter's read-modify-write was the binding path once the memory stalls were
// out of the way, so the resolve is registered on the way in. Nothing here is
// architectural, so a cycle of staleness can only cost a prediction.

`default_nettype none

module rv_bpred #(
    parameter integer ENTRIES = 32,             // power of two
    parameter integer TAG_W   = 8
)(
    input  wire        clk,
    input  wire        resetn,

    // Lookup. The address is the PC being fetched THIS cycle; the answer comes
    // out with that instruction, one cycle later, and is checked against
    // `q_pc` -- the PC the answer belongs to.
    input  wire        q_en,
    input  wire [31:0] q_addr,
    input  wire [31:0] q_pc,
    output wire        q_taken,
    output wire [31:0] q_target,

    // Resolve, from EX. `is_jump` covers JAL and JALR: unconditional, so the
    // counter is slammed rather than nudged.
    input  wire        u_valid,
    input  wire [31:0] u_pc,
    input  wire        u_taken,
    input  wire        u_is_jump,
    input  wire [31:0] u_target
);
    localparam integer IDX_W = $clog2(ENTRIES);
    // {valid, cnt[1:0], tag[TAG_W-1:0], target[31:1]}
    localparam integer W = TAG_W + 34;

    function [IDX_W-1:0] idx_of;
        input [31:0] a;
        idx_of = a[IDX_W+1:2];
    endfunction

    function [TAG_W-1:0] tag_of;
        input [31:0] a;
        tag_of = a[IDX_W+2 +: TAG_W];
    endfunction

    wire [IDX_W-1:0] q_idx = idx_of(q_addr);

    // THE UPDATE IS REGISTERED: EX's comparator landing on a counter's
    // read-modify-write bound the design, and staleness here costs accuracy.
    reg               r_valid, r_taken, r_jump;
    reg [31:0]        r_pc, r_target;
    wire [IDX_W-1:0]  r_idx = idx_of(r_pc);

    // ---- the power-on sweep -------------------------------------------------
    // One entry a cycle. `init_q` matches the array's read latency, so no
    // prediction escapes while an entry still reads back undefined.
    reg [IDX_W:0] init_a;
    reg           init_q;
    wire          init_busy = !init_a[IDX_W];

    // ---- the arrays ---------------------------------------------------------
    wire [W-1:0]     e_q;
    wire             q_v = e_q[W-1];
    wire [1:0]       q_c = e_q[W-2 -: 2];
    wire [TAG_W-1:0] q_t = e_q[W-4 -: TAG_W];
    wire [30:0]      q_g = e_q[30:0];

    wire [W-1:0] u_e;
    wire [1:0]   o_cnt = u_e[W-2 -: 2];
    wire [IDX_W-1:0] u_idx = idx_of(u_pc);

    // EVERY RESOLVE WRITES THE WHOLE ENTRY, so nothing has to be read back and
    // preserved. `valid` therefore gets set by a branch that resolved NOT taken,
    // where it previously did not -- the counter still says not-taken, so the
    // prediction is the same and only which entry wins an aliasing fight moves.
    wire [1:0] n_cnt = r_jump  ? 2'b11
                     : r_taken ? ((o_cnt == 2'b11) ? 2'b11 : o_cnt + 2'b01)
                               : ((o_cnt == 2'b00) ? 2'b00 : o_cnt - 2'b01);

    wire             wr_en = init_busy || r_valid;
    wire [IDX_W-1:0] wr_a  = init_busy ? init_a[IDX_W-1:0] : r_idx;
    wire [W-1:0]     wr_d  = init_busy
                           ? {1'b0, 2'b01, {(TAG_W+31){1'b0}}}
                           : {1'b1, n_cnt, tag_of(r_pc), r_target[31:1]};

    kohaku_sdpram #(.WIDTH(W), .DEPTH(ENTRIES),
                    .MEM_PRIM("distributed"), .READ_LAT(1)) u_ent (
        .clk(clk),
        .wr_en(wr_en), .wr_addr(wr_a), .wr_data(wr_d),
        .rd_en(q_en), .rd_addr(q_idx), .rd_data(e_q)
    );

    // The same entry again, read at the UNREGISTERED resolve index so it arrives
    // in the same cycle as the registered resolve. A mirror rather than a second
    // port on the array above, because the lookup owns that port every cycle.
    //
    // FULL WIDTH, not the two counter bits it uses: a 64-bit xpm_memory_sdpram
    // reads back X, which reached the saturating update, then the entry, then
    // `q_taken`, then the PC -- a core frozen on an X fetch address with nothing
    // stalled, and only the multi-core ping-pong caught it.
    kohaku_sdpram #(.WIDTH(W), .DEPTH(ENTRIES),
                    .MEM_PRIM("distributed"), .READ_LAT(1)) u_mir (
        .clk(clk),
        .wr_en(wr_en), .wr_addr(wr_a), .wr_data(wr_d),
        .rd_en(1'b1), .rd_addr(u_idx), .rd_data(u_e)
    );

    always @(posedge clk) begin
        if (!resetn) begin
            r_valid <= 1'b0;
            init_a  <= {(IDX_W+1){1'b0}};
            init_q  <= 1'b1;
        end else begin
            r_valid  <= u_valid;
            r_taken  <= u_taken;
            r_jump   <= u_is_jump;
            r_pc     <= u_pc;
            r_target <= u_target;

            if (init_busy) init_a <= init_a + 1'b1;
            init_q <= init_busy;
        end
    end

    assign q_taken  = !init_q && q_v && (q_t == tag_of(q_pc)) && q_c[1];
    assign q_target = {q_g, 1'b0};

`ifndef SYNTHESIS
    // The array has no reset, so an X out of it after the sweep means the sweep
    // missed an entry, and an X into it means the resolve carried one. Both are
    // one-shot: an X here propagates to the PC and would otherwise spam.
    reg said_r = 1'b0, said_w = 1'b0;
    always @(posedge clk) begin
        if (resetn && !init_q && !said_r && (^e_q === 1'bx)) begin
            said_r <= 1'b1;
            // q_pc, not q_idx: the word out now belongs to the PREVIOUS
            // cycle's index, and q_pc is the PC it answers for.
            $display("%0t ERROR rv_bpred: the entry for pc %08x reads X after the power-on sweep",
                     $time, q_pc);
        end
        if (resetn && !said_w && wr_en && !init_busy && (^wr_d === 1'bx)) begin
            said_w <= 1'b1;
            $display("%0t ERROR rv_bpred: X written to entry %0d -- taken %b jump %b pc %08x target %08x",
                     $time, r_idx, r_taken, r_jump, r_pc, r_target);
        end
    end

    // Two resolves at ONE index in consecutive cycles would compute the second
    // counter from the first's pre-update value: the mirror is read_first. It
    // needs two branches an entry-span apart resolving back to back, which the
    // redirect penalty between them makes unreachable -- say so if it happens.
    always @(posedge clk)
        if (resetn && r_valid && u_valid && (u_idx == r_idx))
            $display("%0t rv_bpred: back-to-back resolve at index %0d -- one counter update is computed from a stale value",
                     $time, r_idx);
`endif

endmodule

`default_nettype wire

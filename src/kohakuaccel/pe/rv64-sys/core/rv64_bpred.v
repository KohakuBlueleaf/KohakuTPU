// SysCore's branch predictor: a gshare-indexed BTB plus a return-address stack.
//
// BUILT FOR OS CODE, WHICH IS NOT A KERNEL LOOP. `rv_bpred` on the compute PE
// is a 32-entry BTB and that is right for a unit running one hot loop. A
// runtime is call/return dense with a wide branch footprint, so this adds the
// two things that actually pay there:
//
//   RAS      a BTB predicts returns BADLY -- a function called from N sites has
//            N return targets, so one `ret` entry thrashes. A stack does not.
//   gshare   global history XOR the index, for branches whose direction depends
//            on data rather than on position.
//
// THE TARGET IS 39 BITS, NOT 64, AND THAT IS A BLOCK-RAM DECISION.
// {valid, tag[11], target[38:1]} is 51 bits and maps; carrying a full 64-bit
// target would make the entry 76 and a block-RAM port is 72 at its widest, so
// it would silently become LUTs. Sv39 makes 39 bits the real address space.
//
// NOTHING HERE IS ARCHITECTURAL. E resolves every branch against the real
// answer, so a wrong prediction costs the redirect penalty and never
// correctness -- which is why the tag can be short and the tables can alias.

`default_nettype none

module rv64_bpred #(
    parameter integer BTB_ENTRIES = 256,        // power of two
    parameter integer PHT_ENTRIES = 1024,       // power of two
    parameter integer HIST_W      = 8,
    parameter integer RAS_DEPTH   = 16,
    parameter integer TAG_W       = 11
)(
    input  wire        clk,
    input  wire        resetn,

    // Lookup: `q_addr` is the PC being fetched now; the answer arrives with
    // that instruction one cycle later and is checked against `q_pc`.
    input  wire        q_en,
    input  wire [63:0] q_addr,
    input  wire [63:0] q_pc,
    output wire        q_taken,
    output wire [63:0] q_target,

    // The fetch side tells us what the predicted instruction turned out to be,
    // so the stack moves in fetch order rather than in execute order.
    input  wire        p_call,      // JAL/JALR writing x1 or x5
    input  wire        p_ret,       // JALR x0, rs1 where rs1 is x1 or x5
    input  wire [63:0] p_link,      // the return address to push

    // Resolve, from E.
    input  wire        u_valid,
    input  wire [63:0] u_pc,
    input  wire        u_taken,
    input  wire        u_is_jump,
    input  wire        u_is_cond,
    input  wire [63:0] u_target
);
    localparam integer BIDX_W = $clog2(BTB_ENTRIES);
    localparam integer PIDX_W = $clog2(PHT_ENTRIES);
    localparam integer RSP_W  = $clog2(RAS_DEPTH);
    localparam integer BW     = 1 + TAG_W + 38;      // valid, tag, target[38:1]

    function [BIDX_W-1:0] bidx; input [63:0] a; bidx = a[BIDX_W+1:2]; endfunction
    function [TAG_W-1:0] btag;
        input [63:0] a;
        begin
            btag = a[BIDX_W+2 +: TAG_W];
        end
    endfunction

    // ---- global history -----------------------------------------------------
    // Updated on the RESOLVE, not the prediction: a speculative history needs
    // repair on every misprediction, and repair is state this design does not
    // want. The cost is staleness, which costs accuracy and never correctness.
    reg [HIST_W-1:0] ghist;
    always @(posedge clk) begin
        if (!resetn) begin
            ghist <= {HIST_W{1'b0}};
        end else if (u_valid && u_is_cond) begin
            ghist <= {ghist[HIST_W-2:0], u_taken};
        end
    end

    function [PIDX_W-1:0] pidx;
        input [63:0] a;
        input [HIST_W-1:0] h;
        pidx = a[PIDX_W+1:2] ^ {{(PIDX_W-HIST_W){1'b0}}, h};
    endfunction

    // ---- the power-on sweep -------------------------------------------------
    // Neither array has a reset, so every entry is written clean before a
    // prediction is allowed out. `init_q` matches the array read latency.
    localparam integer BTB_BIGGER = (BTB_ENTRIES > PHT_ENTRIES);
    localparam integer SWEEP = BTB_BIGGER ? BTB_ENTRIES : PHT_ENTRIES;
    localparam integer SW_W  = $clog2(SWEEP);
    reg [SW_W:0] init_a;
    reg          init_q;
    wire         init_busy = !init_a[SW_W];

    // ---- the registered resolve --------------------------------------------
    // `rv_bpred` records that EX's comparator reaching the counter's
    // read-modify-write was its binding path; the same shape applies here.
    reg              r_valid, r_taken, r_jump, r_cond;
    reg [63:0]       r_pc, r_target;
    reg [PIDX_W-1:0] r_pi;

    always @(posedge clk) begin
        if (!resetn) begin
            r_valid <= 1'b0;
            init_a  <= {(SW_W+1){1'b0}};
            init_q  <= 1'b1;
        end
        else begin
            r_valid  <= u_valid;
            r_taken  <= u_taken;
            r_jump   <= u_is_jump;
            r_cond   <= u_is_cond;
            r_pc     <= u_pc;
            r_target <= u_target;
            r_pi     <= pidx(u_pc, ghist);
            if (init_busy) begin
                init_a <= init_a + 1'b1;
            end
            init_q <= init_busy;
        end
    end

    // ---- the BTB ------------------------------------------------------------
    wire [BW-1:0] b_q;
    wire          b_v = b_q[BW-1];
    wire [TAG_W-1:0] b_t = b_q[BW-2 -: TAG_W];
    wire [37:0]   b_g = b_q[37:0];

    wire btb_wr = init_busy || (r_valid && r_taken);
    wire [BIDX_W-1:0] btb_wa = init_busy ? init_a[BIDX_W-1:0] : bidx(r_pc);
    wire [BW-1:0]     btb_wd = init_busy ? {BW{1'b0}}
                                         : {1'b1, btag(r_pc), r_target[38:1]};

    kohaku_sdpram #(.WIDTH(BW), .DEPTH(BTB_ENTRIES),
                    .MEM_PRIM("block"), .READ_LAT(1)) u_btb (
        .clk(clk),
        .wr_en(btb_wr), .wr_addr(btb_wa), .wr_data(btb_wd),
        .rd_en(q_en), .rd_addr(bidx(q_addr)), .rd_data(b_q)
    );

    // ---- the direction table ------------------------------------------------
    wire [1:0] p_q, p_old;

    wire [1:0] n_cnt = r_jump  ? 2'b11
                     : r_taken ? ((p_old == 2'b11) ? 2'b11 : p_old + 2'b01)
                               : ((p_old == 2'b00) ? 2'b00 : p_old - 2'b01);

    wire pht_wr = init_busy || r_valid;
    wire [PIDX_W-1:0] pht_wa = init_busy ? init_a[PIDX_W-1:0] : r_pi;
    wire [1:0]        pht_wd = init_busy ? 2'b01 : n_cnt;

    kohaku_sdpram #(.WIDTH(2), .DEPTH(PHT_ENTRIES),
                    .MEM_PRIM("block"), .READ_LAT(1)) u_pht (
        .clk(clk),
        .wr_en(pht_wr), .wr_addr(pht_wa), .wr_data(pht_wd),
        .rd_en(q_en), .rd_addr(pidx(q_addr, ghist)), .rd_data(p_q)
    );

    // The same counter read at the UNREGISTERED index, so the old value arrives
    // in the same cycle as the registered resolve -- a mirror rather than a
    // second port, because the lookup owns the port above every cycle.
    kohaku_sdpram #(.WIDTH(2), .DEPTH(PHT_ENTRIES),
                    .MEM_PRIM("block"), .READ_LAT(1)) u_pht_mir (
        .clk(clk),
        .wr_en(pht_wr), .wr_addr(pht_wa), .wr_data(pht_wd),
        .rd_en(1'b1), .rd_addr(pidx(u_pc, ghist)), .rd_data(p_old)
    );

    // ---- the return-address stack -------------------------------------------
    // TOP OF STACK IS A FLOP and the rest is an array: the prediction needs the
    // top in the same cycle, and a 16:1 mux on 64 bits would be ~320 LUT.
    reg [63:0]     ras_tos;
    reg [RSP_W-1:0] ras_sp;
    reg            ras_v;
    wire [63:0]    ras_under;

    kohaku_sdpram #(.WIDTH(64), .DEPTH(RAS_DEPTH),
                    .MEM_PRIM("distributed"), .READ_LAT(0)) u_ras (
        .clk(clk),
        .wr_en(p_call), .wr_addr(ras_sp), .wr_data(ras_tos),
        .rd_en(1'b1), .rd_addr(ras_sp - {{(RSP_W-1){1'b0}}, 1'b1}),
        .rd_data(ras_under)
    );

    always @(posedge clk) begin
        if (!resetn) begin
            ras_sp  <= {RSP_W{1'b0}};
            ras_v   <= 1'b0;
            ras_tos <= 64'd0;
        end
        else if (p_call) begin
            // The old top goes to the array, the link becomes the new top.
            ras_tos <= p_link;
            ras_sp  <= ras_sp + 1'b1;
            ras_v   <= 1'b1;
        end
        else if (p_ret && ras_v) begin
            ras_tos <= ras_under;
            ras_sp  <= ras_sp - 1'b1;
            ras_v   <= (ras_sp != {{(RSP_W-1){1'b0}}, 1'b1});
        end
    end

    // ---- the answer ---------------------------------------------------------
    // A RETURN TAKES THE STACK, NOT THE BTB, and that is the whole point of it.
    reg        pq_ret;
    reg [63:0] pq_tos;
    reg        pq_rv;
    always @(posedge clk) begin
        pq_ret <= p_ret;
        pq_tos <= ras_tos;
        pq_rv  <= ras_v;
    end

    wire hit = !init_q && b_v && (b_t == btag(q_pc));

    assign q_taken  = pq_ret ? pq_rv : (hit && p_q[1]);
    assign q_target = pq_ret ? pq_tos : {25'd0, b_g, 1'b0};

endmodule

`default_nettype wire

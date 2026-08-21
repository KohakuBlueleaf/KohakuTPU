// khd_facc -- the float tier's accumulator: NPART rotating partials per slot,
// and the counter that makes II = 1 possible over a 15-deep lane.
//
// A LANE IS 15 CYCLES DEEP, so `acc = a*b + acc` on ONE partial issues at
// II = 15. The vector core's answer (vec_lanes.v s7.3) is to rotate: the read
// index advances every accepted operation, and the write index is that same
// counter delayed by exactly the lane's latency, so a result lands on the
// partial its addend came from. With NPART > ALAT a partial is never re-read
// before its write returns.
//
// THE ROTATION IS ARCHITECTURAL. Float addition does not associate, so a build
// with a different NPART computes different answers on the same program. The
// golden model rotates identically and the ISA states the order; this is not an
// implementation detail that can be tuned later.
//
// THE FOLD IS SERIAL AND THAT IS DELIBERATE. Combining NPART partials through
// the lane costs NPART*ALAT cycles because each step depends on the last -- 240
// at NPART 16 -- and it runs ONCE per reduction, against a kernel of thousands
// of cycles. A tree would be log2(NPART) float adders standing idle the rest of
// the time.

`default_nettype none

module khd_facc #(
    parameter integer SLOTS = 16,       // 2*SIMD: one per FP16 element
    parameter integer NACC  = 2,
    parameter integer NPART = 16,       // must exceed the lane's latency
    parameter integer ALAT  = 15
)(
    input  wire                        clk,
    input  wire                        resetn,

    // ---- issue: one accumulate, every slot at once ----
    input  wire                        acc_valid,
    // WIDTHS GUARDED AGAINST NACC = 1: $clog2(1) is 0, so a bare
    // [$clog2(NACC)-1:0] is [-1:0] and every address built from it reads X.
    input  wire [((NACC>1)?$clog2(NACC):1)-1:0] acc_sel,
    output wire [24*SLOTS-1:0]         rd_part,   // the addend for each lane
    output wire [((NPART>1)?$clog2(NPART):1)-1:0] rd_idx,

    // ---- retire: the lane results, ALAT later ----
    input  wire                        wb_valid,
    input  wire [24*SLOTS-1:0]         wb_data,

    // ---- control ----
    input  wire                        do_zero,
    input  wire                        do_seed,
    input  wire [((NACC>1)?$clog2(NACC):1)-1:0] ctl_sel,
    input  wire [24*SLOTS-1:0]         seed_data,

    // ---- read one partial, for the fold ----
    input  wire [((NACC>1)?$clog2(NACC):1)-1:0]  fold_sel,
    input  wire [((NPART>1)?$clog2(NPART):1)-1:0] fold_idx,
    output wire [24*SLOTS-1:0]         fold_part,

    // High while a zero or a seed is sweeping the partials; the unit holds the
    // instruction until it falls.
    output wire                        busy_sweep
);
    localparam integer AW = (NACC  > 1) ? $clog2(NACC)  : 1;
    localparam integer PW = (NPART > 1) ? $clog2(NPART) : 1;
    localparam integer DW = 24 * SLOTS;
    localparam integer DEPTH = NACC * NPART;
    localparam integer XW = $clog2(DEPTH);

    // THE PARTIALS ARE A MEMORY, NOT AN INDEXED FLOP ARRAY, and the difference
    // is 28k LUT at SIMD 8. As flops, each of the 12,288 bits carries a D-input
    // mux between an accumulate result, a seed and zero, and two variable-index
    // 32:1 read muxes sit on top: 29,409 LUT measured, 56% of the whole unit.
    // As two mirrored distributed RAMs -- the construction khd_vregfile already
    // uses for the same reason -- it is the depth of one LUTRAM primitive.
    reg  [PW-1:0] turn [0:NACC-1];
    reg  [PW-1:0] wr_pipe [0:ALAT-1];
    reg  [AW-1:0] wa_pipe [0:ALAT-1];

    wire [PW-1:0] rd_now = turn[acc_sel];
    assign rd_idx = rd_now;

    // vfaccz and vfaccwr SWEEP rather than clearing in parallel: a memory has
    // one write port. NPART cycles, once per reduction, and rv_l1's
    // invalidate-all is the same shape for the same reason.
    reg           sweep;
    reg [PW-1:0]  sweep_k;
    reg           sweep_seed;
    reg [AW-1:0]  sweep_acc;

    // ARITHMETIC, NOT CONCATENATION. At NACC = 1 the accumulator select is
    // $clog2(1) = 0 bits wide, so concatenating it builds a malformed address
    // and every read comes back zero -- which is exactly how this failed.
    wire [XW-1:0] rd_a_addr = acc_sel  * NPART + rd_now;
    wire [XW-1:0] fd_a_addr = fold_sel * NPART + fold_idx;
    wire [XW-1:0] wr_addr = sweep ? (sweep_acc * NPART + sweep_k)
                                  : (wa_pipe[ALAT-1] * NPART + wr_pipe[ALAT-1]);
    wire [DW-1:0] wr_data = sweep ? ((sweep_seed && (sweep_k == {PW{1'b0}}))
                                     ? seed_data : {DW{1'b0}})
                                  : wb_data;
    wire          wr_en   = sweep || wb_valid;

    kohaku_sdpram #(.WIDTH(DW), .DEPTH(DEPTH), .MEM_PRIM("distributed"),
                    .READ_LAT(0)) u_p_acc (
        .clk(clk), .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data),
        .rd_en(1'b1), .rd_addr(rd_a_addr), .rd_data(rd_part));

    kohaku_sdpram #(.WIDTH(DW), .DEPTH(DEPTH), .MEM_PRIM("distributed"),
                    .READ_LAT(0)) u_p_fold (
        .clk(clk), .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data),
        .rd_en(1'b1), .rd_addr(fd_a_addr), .rd_data(fold_part));

    assign busy_sweep = sweep;

    integer i, j;
    always @(posedge clk) begin
        if (!resetn) begin
            for (i = 0; i < NACC; i = i + 1) turn[i] <= {PW{1'b0}};
            sweep <= 1'b0;
        end else begin
            // The write index is the read index delayed by the lane's depth,
            // which is why ALAT here and the lane's own latency must be one
            // number. vec_lanes shares a localparam for exactly this reason.
            wr_pipe[0] <= rd_now;
            wa_pipe[0] <= acc_sel;
            for (j = 1; j < ALAT; j = j + 1) begin
                wr_pipe[j] <= wr_pipe[j-1];
                wa_pipe[j] <= wa_pipe[j-1];
            end

            if (do_zero || do_seed) begin
                sweep      <= 1'b1;
                sweep_k    <= {PW{1'b0}};
                sweep_seed <= do_seed;
                sweep_acc  <= ctl_sel;
                turn[ctl_sel] <= {PW{1'b0}};
            end else if (sweep) begin
                if (sweep_k == (NPART-1)) sweep <= 1'b0;
                sweep_k <= sweep_k + 1'b1;
            end else if (acc_valid) begin
                turn[acc_sel] <= (rd_now == (NPART-1)) ? {PW{1'b0}}
                                                       : (rd_now + 1'b1);
            end
        end
    end

endmodule

`default_nettype wire

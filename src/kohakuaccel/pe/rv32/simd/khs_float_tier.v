// khs_float_tier -- the float tier alone, with the LANE COUNT decoupled from
// the slot count, so the area cost of `vfmacc` at II > 1 is a measurement.
//
// MEASUREMENT PROBE, NOT A DATAPATH: "how many float lanes fit under the 15k
// ceiling" is the question the tier turns on. khs_unit now carries the decoupled
// form itself, as FLOAT_LANES; this stays the area probe, and it FOLDS
// DIFFERENTLY -- flat over NPART*PASSES, where khs_unit walks one pass's own
// strided subset. So its lane count is comparable and its ARITHMETIC is not.

`default_nettype none

module khs_float_tier #(
    parameter integer NARROW_SLOTS = 16,      // 2*SIMD: FP16 elements per vector
    parameter integer FLANES = 16,      // lanes built; NARROW_SLOTS/FLANES is the II
    parameter integer NACC   = 2,
    parameter integer NPART  = 16,
    parameter integer ALAT   = 15,
    parameter integer MODEL  = 0
)(
    input  wire                    clk,
    input  wire                    resetn,

    input  wire                    acc_valid,
    input  wire                    sub,
    input  wire [16*NARROW_SLOTS-1:0]    v1,
    input  wire [16*NARROW_SLOTS-1:0]    v2,
    input  wire [$clog2(NARROW_SLOTS/FLANES)+0:0] pass,
    input  wire [((NACC>1)?$clog2(NACC):1)-1:0] acc_sel,

    input  wire                    do_zero,
    input  wire                    do_seed,
    input  wire                    fold_start,

    output reg  [16*NARROW_SLOTS-1:0]    result,
    output wire                    busy
);
    localparam integer PASSES = NARROW_SLOTS / FLANES;
    localparam integer DEEP   = NPART * PASSES;
    localparam integer SW     = (NARROW_SLOTS > 1) ? $clog2(NARROW_SLOTS) : 1;
    localparam integer PW     = (DEEP > 1) ? $clog2(DEEP) : 1;

    reg [16*NARROW_SLOTS-1:0] v1_q, v2_q;
    reg                 av_q, sub_q, dz_q, ds_q, fs_q;
    reg [$clog2(NARROW_SLOTS/FLANES)+0:0] pass_q;
    reg [((NACC>1)?$clog2(NACC):1)-1:0] sel_q;
    always @(posedge clk) begin
        v1_q <= v1; v2_q <= v2; av_q <= acc_valid; sub_q <= sub;
        dz_q <= do_zero; ds_q <= do_seed; fs_q <= fold_start;
        pass_q <= pass; sel_q <= acc_sel;
    end

    // The operand slice this pass drives into the lanes.
    wire [16*FLANES-1:0] a_sl = v1_q[16*FLANES*pass_q +: 16*FLANES];
    wire [16*FLANES-1:0] b_sl = v2_q[16*FLANES*pass_q +: 16*FLANES];

    wire [24*FLANES-1:0] part_rd, lane_out, fold_part;
    wire [FLANES-1:0]    lane_ov;
    wire                 lane_ovld = lane_ov[0];
    reg  [24*FLANES-1:0] total;
    reg  [24*FLANES-1:0] seed_r;

    wire            f_busy, f_done, f_iss, f_raw, f_sweep;
    wire [PW-1:0]   f_idx;
    reg             folding;

    // One conversion each way, walked over the slots: both instructions that
    // use them already hold the stage for hundreds of cycles.
    reg [SW-1:0] sd_k, pk_k;
    wire [23:0]  sd_e8;
    wire [15:0]  pk_narrow;
    vec_cvt_f16_to_e8 u_sd (.f16(v1_q[16*sd_k +: 16]), .e8(sd_e8));
    vec_cvt_e8_to_f16 u_pk (.e8(total[24*pk_k +: 24]),  .f16(pk_narrow));

    always @(posedge clk) begin
        if (!resetn) begin
            sd_k <= {SW{1'b0}}; pk_k <= {SW{1'b0}}; folding <= 1'b0;
        end else begin
            seed_r[24*(sd_k % FLANES) +: 24] <= sd_e8;
            sd_k <= sd_k + 1'b1;
            result[16*pk_k +: 16] <= pk_narrow;
            pk_k <= pk_k + 1'b1;
            if (fs_q) begin
                folding <= 1'b1;
            end
            else if (f_done) begin
                folding <= 1'b0;
            end
        end
    end

    khs_ffold #(.NPART(DEEP), .ALAT(ALAT)) u_ffold (
        .clk(clk), .resetn(resetn),
        .start(fs_q), .busy(f_busy), .done(f_done),
        .part_idx(f_idx), .iss_valid(f_iss), .iss_raw(f_raw)
    );

    genvar S;
    generate
    for (S = 0; S < FLANES; S = S + 1) begin : g_flane
        wire [23:0] c_sel = folding ? total[24*S +: 24] : part_rd[24*S +: 24];
        // This probe drives the FP16 edge, so `wide` is tied low and the
        // operands are zero-extended into the lane's 32-bit ports.
        khs_float_lane #(.PIPE_MUX(1), .MODEL(MODEL)) u_fl (
            .clk(clk), .rst(!resetn),
            .in_valid(av_q | f_iss), .op(5'd6), .wide(1'b0),
            .a({16'd0, a_sl[16*S +: 16]}),
            .b(sub_q ? {16'd0, b_sl[16*S +: 16] ^ 16'h8000}
                     : {16'd0, b_sl[16*S +: 16]}),
            .c(c_sel),
            .raw_e8(f_raw), .a_e8(fold_part[24*S +: 24]),
            .out_valid(lane_ov[S]), .out(lane_out[24*S +: 24]), .out_pred()
        );
    end
    endgenerate

    always @(posedge clk) begin
        if (fs_q) begin
            total <= {(24*FLANES){1'b0}};
        end
        else if (folding && lane_ovld) begin
            total <= lane_out;
        end
    end

    khs_facc #(.SLOTS(FLANES), .NACC(NACC), .NPART(DEEP), .ALAT(ALAT)) u_facc (
        .clk(clk), .resetn(resetn),
        .acc_valid(av_q), .acc_sel(sel_q),
        .rd_part(part_rd), .rd_idx(),
        .wb_valid(lane_ovld && !folding), .wb_data(lane_out),
        .do_zero(dz_q), .do_seed(ds_q),
        .ctl_sel(sel_q), .seed_data(seed_r),
        .fold_sel(sel_q), .fold_idx(f_idx),
        .fold_part(fold_part), .busy_sweep(f_sweep)
    );

    assign busy = f_busy | f_sweep | folding;

endmodule

`default_nettype wire

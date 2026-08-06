// One cluster: manager + 4-TCU cascade + accumulator.
//
//   mgr  ->  tcu -> tcu -> tcu -> tcu  ->  acu
//   L1       direct DSP cascade (PCOUT/PCIN + W)   resident output tile
//
// This is the compute half of the 2-port cluster. The NoC attachment lives
// above it: the manager takes one port for operands and its own memory
// requests, the accumulator takes one for results and peer transfer.
//
// The accumulator's control is muxed between two sources. During a sweep it
// comes from the manager (one command per part_valid, via the ordering FIFO).
// During a drain it comes from the drain sequencer here, which walks tile
// addresses and waits on emit_valid for each -- EMIT folds the accumulator
// banks, so it takes several cycles and cannot be pipelined blind.

`default_nettype none

module mx_cluster_node #(
    parameter integer TILES  = 256,     // resident output sub-tiles
    parameter integer GA     = 32,
    parameter integer GB     = 64,
    parameter integer ACC_MW = 14,
    parameter integer MODEL  = 0,
    parameter         L1_PRIM = "distributed"
)(
    input  wire         clk,
    input  wire         rst,

    // ---- L1 load -------------------------------------------------------
    input  wire         l1_we,
    input  wire         l1_sel,
    input  wire [15:0]  l1_addr,
    input  wire [927:0] l1_data,

    // ---- sweep ---------------------------------------------------------
    input  wire         gemm_start,
    input  wire [7:0]   gemm_gm,
    input  wire [7:0]   gemm_gn,
    input  wire [7:0]   gemm_nk,
    input  wire [7:0]   gemm_anchor,
    output wire         gemm_busy,

    // ---- drain ---------------------------------------------------------
    input  wire         drain_start,
    input  wire [15:0]  drain_n,        // number of sub-tiles to emit
    output reg          drain_busy,
    output reg  [255:0] drain_data,     // 16 x FP16, one sub-tile
    output reg  [15:0]  drain_idx,
    output reg          drain_valid
);
    localparam integer TAW = (TILES <= 1) ? 1 : $clog2(TILES);
    localparam [2:0] OP_NOP = 3'd0, OP_EMIT = 3'd5;

    // ---- manager --------------------------------------------------------
    wire [895:0] a_bus, b_bus;
    wire         core_valid, core_first, part_valid;
    wire [2:0]      m_op;
    wire [TAW-1:0]  m_addr;
    wire            m_cmd;
    wire [31:0]     m_sa, m_sb;
    wire [7:0]      m_anchor;

    mx_cluster_mgr #(.GA(GA), .GB(GB), .TAW(TAW), .L1_PRIM(L1_PRIM)) u_mgr (
        .clk(clk), .rst(rst),
        .l1_we(l1_we), .l1_sel(l1_sel), .l1_addr(l1_addr), .l1_data(l1_data),
        .gemm_start(gemm_start), .gemm_gm(gemm_gm), .gemm_gn(gemm_gn),
        .gemm_nk(gemm_nk), .gemm_anchor(gemm_anchor), .gemm_busy(gemm_busy),
        .a_out(a_bus), .b_out(b_bus),
        .core_valid(core_valid), .core_first(core_first),
        .part_valid(part_valid),
        .acu_op(m_op), .acu_addr(m_addr), .acu_cmd(m_cmd),
        .acu_sa(m_sa), .acu_sb(m_sb), .acu_anchor(m_anchor)
    );

    // ---- the cascade ----------------------------------------------------
    wire [383:0] part_bus;
    wire         part_first;

    mx_cluster_core #(.MODEL(MODEL)) u_core (
        .clk(clk), .rst(rst), .en(1'b1),
        .a_in(a_bus), .b_in(b_bus),
        .in_valid(core_valid), .in_first(core_first),
        .part_out(part_bus), .part_valid(part_valid), .part_first(part_first)
    );

    // ---- drain sequencer -------------------------------------------------
    reg [15:0] d_idx, d_n;
    reg [1:0]  d_st;
    localparam [1:0] D_IDLE = 2'd0, D_ISSUE = 2'd1, D_WAIT = 2'd2;

    wire [255:0] emit_out;
    wire         emit_valid;

    reg [2:0]      d_op;
    reg [TAW-1:0]  d_addr;
    reg            d_cmd;

    always @(posedge clk) begin
        if (rst) begin
            d_st <= D_IDLE; d_idx <= 16'd0; d_n <= 16'd0;
            drain_busy <= 1'b0; drain_valid <= 1'b0;
            drain_data <= 256'd0; drain_idx <= 16'd0;
            d_op <= OP_NOP; d_addr <= {TAW{1'b0}}; d_cmd <= 1'b0;
        end else begin
            d_cmd       <= 1'b0;
            drain_valid <= 1'b0;

            case (d_st)
            D_IDLE: if (drain_start) begin
                d_n <= (drain_n == 16'd0) ? 16'd1 : drain_n;
                d_idx <= 16'd0;
                drain_busy <= 1'b1;
                d_st <= D_ISSUE;
            end
            D_ISSUE: begin
                d_op   <= OP_EMIT;
                d_addr <= d_idx[TAW-1:0];
                d_cmd  <= 1'b1;
                d_st   <= D_WAIT;
            end
            D_WAIT: if (emit_valid) begin
                drain_data  <= emit_out;
                drain_idx   <= d_idx;
                drain_valid <= 1'b1;
                if (d_idx + 16'd1 == d_n) begin
                    drain_busy <= 1'b0;
                    d_st <= D_IDLE;
                end else begin
                    d_idx <= d_idx + 16'd1;
                    d_st  <= D_ISSUE;
                end
            end
            default: d_st <= D_IDLE;
            endcase
        end
    end

    // ---- accumulator ----------------------------------------------------
    wire acu_busy_drain = drain_busy;

    mx_acu_fp #(.DEPTH(TILES), .ACC_MW(ACC_MW)) u_acu (
        .clk(clk), .rst(rst), .en(1'b1),
        .part_in(part_bus),
        .sa(acu_busy_drain ? 32'd0 : m_sa),
        .sb(acu_busy_drain ? 32'd0 : m_sb),
        .anchor(acu_busy_drain ? 8'd0 : m_anchor),
        .op(acu_busy_drain ? d_op : m_op),
        .tile_addr(acu_busy_drain ? d_addr : m_addr),
        .cmd_valid(acu_busy_drain ? d_cmd : m_cmd),
        .peer_in({(16*(ACC_MW+8)){1'b0}}), .peer_out(), .peer_valid(),
        .emit_out(emit_out), .emit_valid(emit_valid),
        .busy()
    );

endmodule

`default_nettype wire

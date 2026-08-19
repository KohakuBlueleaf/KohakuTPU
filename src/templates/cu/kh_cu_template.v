// A compute unit, reduced to the part you replace. noc_cu_base handles
// framing, discovery, completion and credits (docs/spec/compute-unit-port.md,
// flit-format.md); this file is only the datapath. Worked full-size examples:
// kohakutpu/vector/vec_cu.v and matmul/mx_cluster_cu.v.
`default_nettype none

module kh_cu_template #(
    parameter FLIT_WIDTH = 288,
    parameter POS_WIDTH  = 4,
    parameter POS_X      = 2,
    parameter POS_Y      = 2,
    // Your unit's identity. CU_VERSION is a MESH-WIDE build number -- see the
    // ledger in vec_cu.v; bump every endpoint together.
    parameter CU_TYPE    = 16'h4B48,     // "KH"
    parameter CU_VERSION = 8'h01,
    parameter INST_DEPTH = 32,
    parameter MEM_TYPE   = "distributed"
)(
    input  wire                   clk,
    input  wire                   resetn,
    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,
    output wire                   busy
);
    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit;
    wire                  inst_valid, recv_valid, send_ready;
    reg                   inst_ready, exec_done, exec_fault;
    reg  [31:0]           exec_result;
    wire [15:0]           inst_space;

    // ---- your ISA -------------------------------------------------------
    // Payload [255:252] = op, [31:0] = operand. Two ops are enough to prove
    // the round trip: 0 accumulates, 1 reports the accumulator as the result.
    localparam [3:0] OP_ACC = 4'd0, OP_REPORT = 4'd1;
    wire [3:0]  i_op  = inst_flit[255 -: 4];
    wire [31:0] i_arg = inst_flit[31:0];

    reg [31:0] acc;
    reg [31:0] n_inst;
    reg        running;

    always @(posedge clk) begin
        inst_ready <= 1'b0;
        exec_done  <= 1'b0;
        exec_fault <= 1'b0;
        if (!resetn) begin
            running <= 1'b0;
            acc     <= 32'd0;
            n_inst  <= 32'd0;
        end else begin
            // Accept, THEN retire on a later cycle: exec_done in the same
            // cycle as inst_ready loses the completion (noc_cu_base:221).
            if (inst_valid && !inst_ready && !running) begin
                inst_ready <= 1'b1;
                running    <= 1'b1;
                case (i_op)
                    OP_ACC:    begin acc <= acc + i_arg; exec_result <= 32'd0; end
                    OP_REPORT: exec_result <= acc;
                    default:   exec_result <= 32'd0;
                endcase
                n_inst <= n_inst + 32'd1;
            end else if (running) begin
                // Your datapath runs here for as many cycles as it needs;
                // OP_REPORT's result was latched at issue.
                exec_done <= 1'b1;
                running   <= 1'b0;
            end
        end
    end

    // ---- non-instruction traffic ---------------------------------------
    // FORCED convention: accept and DROP types you do not understand, with a
    // message naming the type -- held, they wedge the queue behind them.
    wire recv_ready_w = 1'b1;
    always @(posedge clk) begin
        if (resetn && recv_valid) begin
            // synthesis translate_off
            $display("%0t kh_cu_template: dropping flit type %0h", $time,
                     recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4]);
            // synthesis translate_on
        end
    end

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y),
        .CU_TYPE(CU_TYPE), .CU_VERSION(CU_VERSION), .N_BUFFERS(1),
        .INST_DEPTH(INST_DEPTH), .MEM_TYPE(MEM_TYPE)
    ) u_base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result),
        .exec_fault(exec_fault),
        // Spend this on something diagnostic: it separates "slow" from
        // "waiting" over one JTAG read.
        .dbg_ctr({acc, n_inst}),
        .send_flit({FLIT_WIDTH{1'b0}}), .send_valid(1'b0),
        .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid),
        .recv_ready(recv_ready_w),
        .inst_space(inst_space), .busy(busy)
    );

endmodule

`default_nettype wire

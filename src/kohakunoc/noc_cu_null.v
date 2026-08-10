// The minimum conforming compute unit: all of the NoC obligations, none of the
// compute. Two purposes.
//
// As a measurement instrument, it isolates what attaching an endpoint to the mesh
// costs. The CU framework is part of the NoC system, not part of the datapath, so
// "how much silicon does one more node cost before any arithmetic exists" is the
// number that decides between many-small-CUs and few-large-CUs. Subtract this from
// a real CU and the remainder is genuinely compute.
//
// As a template, it is the smallest thing that is a legal node: copy it, replace
// the retire logic with a datapath, and the framework obligations are already met.
//
// Nothing here may be optimised away, or the measurement is worthless. Two things
// guarantee that: every one of the FLIT_WIDTH bits is folded into sig_out, which
// reaches a port, so no part of the flit is dead; and traffic originates from the
// ext_* inputs, so the mesh cannot be proven idle and constant-folded.

`default_nettype none

module noc_cu_null #(
    parameter FLIT_WIDTH = 288,
    parameter POS_WIDTH  = 4,
    parameter POS_X      = 1,
    parameter POS_Y      = 1,
    parameter CU_TYPE    = 16'h0000,
    parameter INST_DEPTH = 32,
    parameter MEM_TYPE   = "distributed"
) (
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    // external stimulus, so the node is never provably idle
    input  wire                   ext_kick,
    input  wire [2*POS_WIDTH-1:0] ext_dst,
    input  wire [31:0]            ext_data,

    // observable state, so nothing upstream of it is dead logic
    output reg  [31:0]            sig_out,
    output wire [15:0]            inst_space,
    output wire                   busy
);
    localparam [3:0] T_CU_DATA = 4'h4;
    localparam WORDS = FLIT_WIDTH / 32;          // 9 at 288 bits

    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit;
    wire                  inst_valid, recv_valid, send_ready;
    reg                   inst_ready, recv_ready;
    reg                   exec_done;
    reg  [FLIT_WIDTH-1:0] send_flit;
    reg                   send_valid;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y),
        // Mesh-wide build number -- see vec_cu.v.
        .CU_TYPE(CU_TYPE), .CU_VERSION(8'h03), .N_BUFFERS(1),
        .INST_DEPTH(INST_DEPTH), .MEM_TYPE(MEM_TYPE)
    ) base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid), .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid), .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(sig_out), .exec_fault(1'b0),
        .dbg_ctr(64'd0),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(recv_ready),
        .inst_space(inst_space), .busy(busy)
    );

    // Fold every bit of both flits into one word. This is the whole "datapath":
    // it exists so that no part of a FLIT_WIDTH path is dead, and it is a XOR tree
    // rather than anything arithmetic so its own cost stays negligible and
    // subtractable.
    integer w;
    reg [31:0] fold_inst, fold_recv;
    always @(*) begin
        fold_inst = 32'd0;
        fold_recv = 32'd0;
        for (w = 0; w < WORDS; w = w + 1) begin
            fold_inst = fold_inst ^ inst_flit[w*32 +: 32];
            fold_recv = fold_recv ^ recv_flit[w*32 +: 32];
        end
    end

    // Retire in the cycle the instruction is accepted -- zero execution latency is
    // the point. The framework still does the full reply-context bookkeeping and
    // still emits a CU_SIGNAL, so the NoC-side cost is unchanged by there being
    // nothing to compute.
    always @(posedge clk) begin
        inst_ready <= 1'b0;
        exec_done  <= 1'b0;
        recv_ready <= 1'b1;
        if (!resetn) begin
            sig_out    <= 32'd0;
            recv_ready <= 1'b0;
            send_valid <= 1'b0;
            send_flit  <= {FLIT_WIDTH{1'b0}};
        end else begin
            if (inst_valid && !inst_ready) begin
                inst_ready <= 1'b1;
                exec_done  <= 1'b1;
                sig_out    <= sig_out ^ fold_inst;
            end
            if (recv_valid && recv_ready)
                sig_out <= sig_out ^ fold_recv;

            // externally triggered CU_DATA, so traffic has an origin outside the
            // module and the mesh cannot be constant-folded to idle
            if (send_valid && send_ready) begin
                send_valid <= 1'b0;
            end else if (!send_valid && ext_kick) begin
                send_flit <= { ext_dst[POS_WIDTH-1:0], ext_dst[2*POS_WIDTH-1:POS_WIDTH],
                               POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                               T_CU_DATA, 8'h00, 1'b1, 3'b000,
                               {(FLIT_WIDTH-4*POS_WIDTH-16-32){1'b0}}, ext_data };
                send_valid <= 1'b1;
            end
        end
    end

endmodule

`default_nettype wire

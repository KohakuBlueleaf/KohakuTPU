// A compute-shaped CU on noc_cu_base: decodes an opcode, takes a
// class-dependent number of cycles, and reports a real result.
//
// Instruction body (low 64 bits of the payload):
//   [63:56] opcode   [55:24] operand A   [23:0] operand B (24-bit)
//
// opcodes: 0=ADD 1=SUB 2=AND 3=OR 4=XOR 5=SHL 6=MUL 0xFF=FAULT
//
// Execution takes 1 + (opcode & 3) cycles, so instructions retire out of step
// with issue and the framework's in-flight tracking is actually exercised.

`default_nettype none

module cu_alu #(
    parameter FLIT_WIDTH = 288,
    parameter POS_WIDTH  = 4,
    parameter POS_X      = 2,
    parameter POS_Y      = 2,
    parameter INST_DEPTH = 32
) (
    input  wire                  clk,
    input  wire                  resetn,
    input  wire [FLIT_WIDTH-1:0] noc_in_data,
    input  wire                  noc_in_valid,
    output wire                  noc_in_busy,
    output wire [FLIT_WIDTH-1:0] noc_out_data,
    output wire                  noc_out_valid,
    input  wire                  noc_out_busy,
    output reg  [31:0]           retired,
    output reg  [31:0]           faults,
    output reg  [31:0]           acc          // running sum of results
);
    wire [FLIT_WIDTH-1:0] inst_flit;
    wire                  inst_valid;
    reg                   inst_ready;
    reg                   exec_done, exec_fault;
    reg  [31:0]           exec_result;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y),
        .CU_TYPE(16'h0A10), .CU_VERSION(8'h01), .N_BUFFERS(4),
        .INST_DEPTH(INST_DEPTH)
    ) base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid), .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid), .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result), .exec_fault(exec_fault),
        .send_flit({FLIT_WIDTH{1'b0}}), .send_valid(1'b0), .send_ready(),
        .recv_flit(), .recv_valid(), .recv_ready(1'b1),
        .inst_space(), .busy()
    );

    wire [7:0]  op = inst_flit[63 -: 8];
    wire [31:0] a  = inst_flit[55 -: 32];
    wire [23:0] b  = inst_flit[23 -: 24];

    reg [7:0]  cur_op;
    reg [31:0] cur_a;
    reg [23:0] cur_b;
    reg [3:0]  timer;
    reg        running;

    // Combinational, so the same value can be registered into exec_result AND
    // added to acc in one cycle. Reading exec_result there instead would pick up
    // the previous instruction's value, because it is a non-blocking assign.
    reg [31:0] res;
    always @(*) begin
        case (cur_op)
            8'd0: res = cur_a + {8'd0, cur_b};
            8'd1: res = cur_a - {8'd0, cur_b};
            8'd2: res = cur_a & {8'd0, cur_b};
            8'd3: res = cur_a | {8'd0, cur_b};
            8'd4: res = cur_a ^ {8'd0, cur_b};
            8'd5: res = cur_a << cur_b[4:0];
            8'd6: res = cur_a * {8'd0, cur_b};
            default: res = 32'd0;
        endcase
    end

    always @(posedge clk) begin
        inst_ready <= 1'b0;
        exec_done  <= 1'b0;
        exec_fault <= 1'b0;
        if (!resetn) begin
            running <= 1'b0; timer <= 4'd0;
            retired <= 32'd0; faults <= 32'd0; acc <= 32'd0;
            cur_op <= 0; cur_a <= 0; cur_b <= 0; exec_result <= 0;
        end else begin
            if (!running && inst_valid && !inst_ready) begin
                cur_op     <= op;
                cur_a      <= a;
                cur_b      <= b;
                timer      <= 4'd1 + {2'b0, op[1:0]};   // class-dependent latency
                running    <= 1'b1;
                inst_ready <= 1'b1;
            end else if (running) begin
                if (timer > 4'd1) begin
                    timer <= timer - 4'd1;
                end
                else begin
                    running   <= 1'b0;
                    exec_done <= 1'b1;
                    if (cur_op == 8'hFF) begin
                        exec_fault  <= 1'b1;
                        exec_result <= 32'hDEAD_FA17;
                        faults      <= faults + 32'd1;
                    end else begin
                        exec_result <= res;
                        acc         <= acc + res;
                        retired     <= retired + 32'd1;
                    end
                end
            end
        end
    end

endmodule

`default_nettype wire

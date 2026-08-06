// A traffic-originating CU on noc_cu_base: each instruction makes it send a
// CU_DATA packet to a peer, and it counts the CU_DATA packets it receives.
//
// Deliberately a different shape from cu_alu -- that one only consumes
// instructions, this one drives send_* and recv_* as well. If the framework were
// accidentally fitted to a single CU style, this is where it would show.
//
// Instruction body (low 64 bits):
//   [63:56] peer_x   [55:48] peer_y   [47:16] payload   [15:0] buf_id/offset
//
// The instruction only retires once the packet has actually been handed to the
// NoC, so completion means "sent", not "queued".

`default_nettype none

module cu_relay #(
    parameter FLIT_WIDTH = 288,
    parameter POS_WIDTH  = 4,
    parameter POS_X      = 1,
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
    output reg  [31:0]           sent_count,
    output reg  [31:0]           recv_count,
    output reg  [63:0]           recv_xor
);
    localparam [3:0] T_CU_DATA = 4'h4;

    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit;
    wire                  inst_valid, recv_valid, send_ready;
    reg                   inst_ready, recv_ready;
    reg                   exec_done;
    reg  [31:0]           exec_result;
    reg  [FLIT_WIDTH-1:0] send_flit;
    reg                   send_valid;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y),
        .CU_TYPE(16'h0E11), .CU_VERSION(8'h01), .N_BUFFERS(2),
        .INST_DEPTH(INST_DEPTH)
    ) base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid), .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid), .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result), .exec_fault(1'b0),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(recv_ready),
        .inst_space(), .busy()
    );

    wire [POS_WIDTH-1:0] peer_x  = inst_flit[63 -: 8];
    wire [POS_WIDTH-1:0] peer_y  = inst_flit[55 -: 8];
    wire [31:0]          payload = inst_flit[47 -: 32];
    wire [15:0]          bufoff  = inst_flit[15 -: 16];

    // payload is bufoff(16) + PAD + payload(32) and must total 256
    localparam PAD = FLIT_WIDTH - 4*POS_WIDTH - 16 - 16 - 32;

    always @(posedge clk) begin
        inst_ready <= 1'b0;
        exec_done  <= 1'b0;
        if (!resetn) begin
            send_valid <= 1'b0; send_flit <= {FLIT_WIDTH{1'b0}};
            sent_count <= 32'd0; exec_result <= 32'd0;
        end else begin
            // hold send_valid until the framework takes it; retire only then, so
            // "instruction complete" means the packet reached the NoC
            if (send_valid && send_ready) begin
                send_valid  <= 1'b0;
                exec_done   <= 1'b1;
                exec_result <= sent_count + 32'd1;
                sent_count  <= sent_count + 32'd1;
            end else if (!send_valid && inst_valid && !inst_ready) begin
                send_flit <= { peer_x, peer_y,
                               POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                               T_CU_DATA, 8'h00, 1'b1, 3'b000,
                               bufoff, {(PAD){1'b0}}, payload };
                send_valid <= 1'b1;
                inst_ready <= 1'b1;
            end
        end
    end

    // count and fingerprint inbound CU_DATA
    wire [3:0] rtype = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    always @(posedge clk) begin
        recv_ready <= 1'b1;
        if (!resetn) begin
            recv_count <= 32'd0; recv_xor <= 64'd0; recv_ready <= 1'b0;
        end else if (recv_valid && recv_ready) begin
            if (rtype == T_CU_DATA) begin
                recv_count <= recv_count + 32'd1;
                recv_xor   <= recv_xor ^ recv_flit[63:0];
            end
        end
    end

endmodule

`default_nettype wire

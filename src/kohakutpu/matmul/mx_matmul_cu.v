// A matmul cluster as a NoC compute unit.
//
//   noc_cu_base ....... instruction FIFO, CU_CTRL, completion signalling
//   sequencer ......... decodes CU_INST, fetches operands, runs the datapath
//   mx_cluster_core ... 4 TCU chained, 4 x 32 x 4 per cycle
//   mx_acu_fp ......... FP22 accumulation into a resident output tile
//
// Operands are fetched from memory by the CU itself, as MEM_RD_REQ naming this
// CU as src -- so the response comes straight here and never passes through the
// orchestrator. That is the whole point of the orchestrator having no AXI
// master (spec s10.3).
//
// Instruction, in the CU_INST payload:
//
//   [255:252] opcode   1 = BLOCK, 2 = EMIT
//   [251:218] addr_a   (BLOCK) A operand address   / (EMIT) destination
//   [217:184] addr_b   (BLOCK) B operand address
//   [183:180] tile     which resident sub-tile
//   [179]     first    1 = start the tile, 0 = accumulate into it
//   [178:171] anchor   common exponent for this output tile
//
// BLOCK is one K=32 step: read 4 flits of A and 4 of B, run them through the
// cluster, accumulate. EMIT converts a tile to FP16 and writes it back.
//
// Operand flit layout, 256-bit payload:
//   [255:32]  32 x int7   A: element (i,k) at [255 - (i*8+k)*7 -: 7]
//                         B: element (k,j) at [255 - (k*4+j)*7 -: 7]
//   [31:0]    4 x E5M3    A: sa[i] at [31 - i*8 -: 8]
//                         B: sb[j] at [31 - j*8 -: 8]
// which is 32*7 + 4*8 = 256 exactly -- int7 is the width that fits.

`default_nettype none

module mx_matmul_cu #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer POS_X      = 1,
    parameter integer POS_Y      = 0,
    parameter integer MEM_X      = 2,      // where operands live
    parameter integer MEM_Y      = 1,
    parameter integer INST_DEPTH = 32,
    parameter integer TILES      = 16,
    // NOT passed to mx_acu_fp -- it derives its own from DEPTH. Kept here only
    // as the width of this module's own tile-address registers.
    parameter integer TAW        = 4,
    // 14 (FP22), not 16: measures identically against FP64 and exact-int, costs
    // less, and carries the slack that takes the CU past 300 MHz. See
    // docs/compute/accumulator.md.
    parameter integer ACC_MW     = 14,
    parameter integer MODEL      = 0
)(
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    output wire [15:0]            blocks_done,
    output wire [15:0]            emits_done
);
    localparam [3:0] T_MEM_RD_REQ  = 4'h0,
                     T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2,
                     T_MEM_WR_ACK  = 4'h3;

    localparam [3:0] OP_BLOCK = 4'd1, OP_EMIT = 4'd2;

    localparam [2:0] A_LOAD = 3'd1, A_ADD = 3'd2, A_EMIT = 3'd5;

    // ------------------------------------------------------------ framework
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
        // Mesh-wide build number -- see vec_cu.v.
        .CU_TYPE(16'h4D58), .CU_VERSION(8'h04), .N_BUFFERS(2),
        .INST_DEPTH(INST_DEPTH)
    ) base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid), .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid), .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result), .exec_fault(1'b0),
        .dbg_ctr(64'd0),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(recv_ready),
        .inst_space(), .busy()
    );

    // ------------------------------------------------------- instruction bits
    wire [3:0]  i_op    = inst_flit[255 -: 4];
    wire [33:0] i_adr_a = inst_flit[251 -: 34];
    wire [33:0] i_adr_b = inst_flit[217 -: 34];
    wire [3:0]  i_tile  = inst_flit[183 -: 4];
    wire        i_first = inst_flit[179];
    wire [7:0]  i_anch  = inst_flit[178 -: 8];

    reg [3:0]  op_r;
    reg [33:0] adr_a_r, adr_b_r;
    reg [3:0]  tile_r;
    reg        first_r;
    reg [7:0]  anch_r;

    // ------------------------------------------------------- operand buffers
    reg [895:0] a_buf, b_buf;
    reg [31:0]  sa_r, sb_r;
    reg [2:0]   fl;                       // which of the 4 flits is arriving

    wire [3:0] rtype = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];

    // ------------------------------------------------------------- datapath
    reg          cl_valid, cl_first;
    wire [383:0] part_out;
    wire         part_valid, part_first;

    mx_cluster_core #(.MODEL(MODEL)) u_core (
        .clk(clk), .rst(!resetn), .en(1'b1),
        .a_in(a_buf), .b_in(b_buf),
        .in_valid(cl_valid), .in_first(cl_first),
        .part_out(part_out), .part_valid(part_valid), .part_first(part_first)
    );

    reg  [2:0]     acu_op;
    reg  [TAW-1:0] acu_addr;
    reg            acu_cmd;
    wire [255:0]   emit_out;
    wire           emit_valid;

    mx_acu_fp #(.DEPTH(TILES), .ACC_MW(ACC_MW)) u_acu (
        .clk(clk), .rst(!resetn), .en(1'b1),
        .part_in(part_out), .sa(sa_r), .sb(sb_r), .anchor(anch_r),
        .op(acu_op), .tile_addr(acu_addr), .cmd_valid(acu_cmd),
        .peer_in({(16*(ACC_MW+8)){1'b0}}), .peer_out(), .peer_valid(),
        .emit_out(emit_out), .emit_valid(emit_valid), .busy()
    );

    // ------------------------------------------------------------- sequencer
    localparam [3:0] S_IDLE  = 4'd0,  S_REQA  = 4'd1,  S_RCVA = 4'd2,
                     S_REQB  = 4'd3,  S_RCVB  = 4'd4,  S_RUN  = 4'd5,
                     S_WAIT  = 4'd6,  S_ACC   = 4'd7,  S_EMIT = 4'd8,
                     S_WREQ  = 4'd9,  S_WDAT  = 4'd10, S_EGAP = 4'd12;

    // The accumulator is single-bank: consecutive commands to the SAME tile
    // address must be at least its reuse distance apart. This CU sweeps K on the
    // inside and emits the same tile immediately, so it has to wait -- 8 cycles
    // once per emitted tile. See mx_acu_fp.v; the ISA avoids this by sweeping K
    // outermost.
    localparam [3:0] EMIT_GAP = 4'd8;

    reg [3:0]  egap;
    reg [3:0]  st;
    reg [15:0] nblk, nemit;
    reg [255:0] emit_hold;
    reg         emit_got;       // emit_hold is loaded; the ACU's latency is
                                // a design variable, so do not count cycles

    assign blocks_done = nblk;
    assign emits_done  = nemit;

    // read request for `adr`, four 256-bit words (len = 3)
    function [FLIT_WIDTH-1:0] rd_req;
        input [33:0] adr;
        begin
            rd_req = { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                       POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                       T_MEM_RD_REQ, 8'h01, 1'b1, 3'b000,
                       adr, 6'd0, 8'd3, 8'd0, 200'd0 };
        end
    endfunction

    integer i, k, j, f;
    always @(posedge clk) begin
        if (!resetn) begin
            st <= S_IDLE; fl <= 3'd0;
            inst_ready <= 1'b0; recv_ready <= 1'b0;
            exec_done <= 1'b0; exec_result <= 32'd0;
            send_valid <= 1'b0; send_flit <= {FLIT_WIDTH{1'b0}};
            cl_valid <= 1'b0; cl_first <= 1'b0;
            acu_cmd <= 1'b0; acu_op <= 3'd0; acu_addr <= {TAW{1'b0}};
            a_buf <= 896'd0; b_buf <= 896'd0; sa_r <= 32'd0; sb_r <= 32'd0;
            nblk <= 16'd0; nemit <= 16'd0; anch_r <= 8'd0;
            op_r <= 4'd0; adr_a_r <= 34'd0; adr_b_r <= 34'd0;
            tile_r <= 4'd0; first_r <= 1'b0; emit_hold <= 256'd0;
            emit_got <= 1'b0;
        end else begin
            inst_ready <= 1'b0;
            exec_done  <= 1'b0;
            cl_valid   <= 1'b0;
            acu_cmd    <= 1'b0;
            recv_ready <= 1'b1;

            if (send_valid && send_ready) send_valid <= 1'b0;

            case (st)
            S_IDLE: begin
                if (inst_valid && !inst_ready) begin
                    op_r    <= i_op;
                    adr_a_r <= i_adr_a;
                    adr_b_r <= i_adr_b;
                    tile_r  <= i_tile;
                    first_r <= i_first;
                    anch_r  <= i_anch;
                    inst_ready <= 1'b1;
                    fl  <= 3'd0;
                    egap <= EMIT_GAP;
                    st  <= (i_op == OP_BLOCK) ? S_REQA : S_EGAP;
                end
            end

            // ---- fetch A ----
            S_REQA: if (!send_valid) begin
                send_flit  <= rd_req(adr_a_r);
                send_valid <= 1'b1;
                st <= S_RCVA;
            end
            S_RCVA: if (recv_valid && recv_ready && rtype == T_MEM_RD_RESP) begin
                // Unrolled over the flit index, not indexed by it: a variable
                // part-select write tells synthesis any of the 896 bits might
                // come from any position, so it builds a barrel mux across the
                // whole buffer. Each bit is written by exactly one value of fl,
                // so unrolling leaves one fixed source and a plain enable.
                for (f = 0; f < 4; f = f + 1)
                    if (fl == f[2:0])
                        for (i = 0; i < 4; i = i + 1)
                            for (k = 0; k < 8; k = k + 1)
                                a_buf[(i*32 + f*8 + k)*7 +: 7]
                                    <= recv_flit[255 - (i*8+k)*7 -: 7];
                if (fl == 3'd0)
                    for (i = 0; i < 4; i = i + 1)
                        sa_r[i*8 +: 8] <= recv_flit[31 - i*8 -: 8];
                if (fl == 3'd3) begin fl <= 3'd0; st <= S_REQB; end
                else                 fl <= fl + 3'd1;
            end

            // ---- fetch B ----
            S_REQB: if (!send_valid) begin
                send_flit  <= rd_req(adr_b_r);
                send_valid <= 1'b1;
                st <= S_RCVB;
            end
            S_RCVB: if (recv_valid && recv_ready && rtype == T_MEM_RD_RESP) begin
                for (f = 0; f < 4; f = f + 1)
                    if (fl == f[2:0])
                        for (k = 0; k < 8; k = k + 1)
                            for (j = 0; j < 4; j = j + 1)
                                b_buf[((f*8 + k)*4 + j)*7 +: 7]
                                    <= recv_flit[255 - (k*4+j)*7 -: 7];
                if (fl == 3'd0)
                    for (j = 0; j < 4; j = j + 1)
                        sb_r[j*8 +: 8] <= recv_flit[31 - j*8 -: 8];
                if (fl == 3'd3) begin fl <= 3'd0; st <= S_RUN; end
                else                 fl <= fl + 3'd1;
            end

            // ---- one pass through the cluster ----
            S_RUN: begin
                cl_valid <= 1'b1;
                cl_first <= first_r;
                st  <= S_WAIT;
            end
            S_WAIT: if (part_valid) begin
                acu_op   <= first_r ? A_LOAD : A_ADD;
                acu_addr <= tile_r[TAW-1:0];
                acu_cmd  <= 1'b1;
                st <= S_ACC;
            end
            S_ACC: begin
                nblk <= nblk + 16'd1;
                exec_result <= {16'd0, nblk + 16'd1};
                exec_done <= 1'b1;
                st <= S_IDLE;
            end

            // ---- let the accumulator's reuse distance elapse ----
            S_EGAP: if (egap == 4'd0) st <= S_EMIT;
                    else              egap <= egap - 4'd1;

            // ---- emit a tile to memory ----
            S_EMIT: begin
                acu_op   <= A_EMIT;
                acu_addr <= tile_r[TAW-1:0];
                acu_cmd  <= 1'b1;
                emit_got <= 1'b0;
                st  <= S_WREQ;
            end
            S_WREQ: begin
                if (emit_valid) begin
                    emit_hold <= emit_out;
                    emit_got  <= 1'b1;
                end
                // wait on emit_got, not on emit_valid: emit_hold is a
                // non-blocking assignment, so it is only readable the cycle
                // after the ACU raised valid
                if (emit_got && !send_valid) begin
                    // descriptor: one 256-bit word follows
                    send_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                   POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                   T_MEM_WR_REQ, 8'h02, 1'b0, 3'b000,
                                   adr_a_r, 6'd0, 8'd0, 8'd0, 200'd0 };
                    send_valid <= 1'b1;
                    st <= S_WDAT;
                end
            end
            S_WDAT: if (!send_valid) begin
                send_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                               POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                               T_MEM_WR_REQ, 8'h02, 1'b1, 3'b000, emit_hold };
                send_valid <= 1'b1;
                nemit <= nemit + 16'd1;
                exec_result <= {16'd1, nemit + 16'd1};
                exec_done <= 1'b1;
                st <= S_IDLE;
            end

            default: st <= S_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire

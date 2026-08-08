// The vector core as a NoC compute unit.
//
// isa/vector.md s8: the vector core is a sixth CONSUMER, not a sixth layer.
// The agent stages a kernel and kicks it exactly as it stages a GEMM, so the
// framing here is the ordinary noc_cu_base contract and nothing above changes.
//
// CU instruction, in the CU_INST payload:
//
//   [255:252] op   1 = IMEM  [251:243] addr, [31:0] word
//                  2 = DESC  [251:249] ad, [248:246] fld, [245:212] value
//                  3 = RUN   [251:243] start pc
//
// A RUN retires when the kernel reaches VHALT, and reports its cycle count;
// a kernel fault retires as SIG_FAULT with the fault code.
//
// ONE PORT, not the two a cluster has. The second port is a bandwidth decision
// (vector-core.md s7.2 counts 512 payload bit/cycle from two), not a
// correctness one, and nothing here depends on it.
//
// FILL TAGGING. A read response names its L1 slot through the 8-bit NoC txn
// field, so one VFILL may be outstanding at a time and vec_core holds a second
// until the first drains. The bank bit is latched per fill rather than sent.

`default_nettype none

module vec_cu #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer POS_X      = 3,
    parameter integer POS_Y      = 3,
    parameter integer MEM_X      = 1,
    parameter integer MEM_Y      = 1,
    parameter integer INST_DEPTH = 32,
    parameter integer RECV_DEPTH = 64,
    parameter integer MODEL      = 1,
    parameter integer L1_DEPTH   = 512,
    parameter         L1_PRIM    = "block"
)(
    input  wire                   clk,
    input  wire                   resetn,

    input  wire [FLIT_WIDTH-1:0]  noc_in_data,
    input  wire                   noc_in_valid,
    output wire                   noc_in_busy,
    output wire [FLIT_WIDTH-1:0]  noc_out_data,
    output wire                   noc_out_valid,
    input  wire                   noc_out_busy,

    output wire [31:0]            dbg_cycles,
    output wire                   dbg_fault
);
    localparam [3:0] T_MEM_RD_REQ  = 4'h0, T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2, T_MEM_WR_DATA = 4'h4;

    localparam [3:0] C_IMEM = 4'd1, C_DESC = 4'd2, C_RUN = 4'd3;

    localparam [2:0] W_IDLE = 3'd0, W_REQ = 3'd1, W_DATA = 3'd2;

    // ================================================ framework
    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit;
    wire                  inst_valid, recv_valid, send_ready;
    reg                   inst_ready, recv_ready;
    reg                   exec_done, exec_fault;
    reg  [31:0]           exec_result;
    reg  [FLIT_WIDTH-1:0] send_flit;
    reg                   send_valid;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X), .POS_Y(POS_Y),
        .CU_TYPE(16'h5643), .CU_VERSION(8'h01), .N_BUFFERS(2),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH)
    ) u_base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result),
        .exec_fault(exec_fault),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(recv_ready),
        .inst_space(), .busy()
    );

    wire [3:0]  c_op   = inst_flit[255 -: 4];
    wire [8:0]  c_addr = inst_flit[251 -: 9];
    wire [2:0]  c_ad   = inst_flit[251 -: 3];
    wire [2:0]  c_fld  = inst_flit[248 -: 3];
    wire [33:0] c_val  = inst_flit[245 -: 34];
    wire [31:0] c_word = inst_flit[31:0];

    wire [3:0] rtype = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];
    wire [7:0] rtag  = recv_flit[FLIT_WIDTH-4*POS_WIDTH-5 -: 8];

    // ================================================ the core
    reg         ld_en, ld_kind, start;
    reg  [8:0]  ld_addr, start_pc;
    reg  [33:0] ld_data;
    wire        busy, halted, fault;
    wire [7:0]  fault_code;
    wire [31:0] cycles;

    wire        rd_req_valid, wr_req_valid;
    wire [33:0] rd_req_addr, wr_req_addr;
    wire [8:0]  rd_req_tag;
    wire [255:0] wr_req_data;
    reg         rd_req_ready, wr_req_ready;

    reg         rr_valid;
    reg  [8:0]  rr_tag;
    reg  [255:0] rr_data;
    reg         fill_bank;

    vec_core #(.MODEL(MODEL), .L1_DEPTH(L1_DEPTH), .L1_PRIM(L1_PRIM)) u_core (
        .clk(clk), .rst(!resetn),
        .ld_en(ld_en), .ld_kind(ld_kind), .ld_addr(ld_addr), .ld_data(ld_data),
        .start(start), .start_pc(start_pc),
        .busy(busy), .halted(halted), .fault(fault), .fault_code(fault_code),
        .cycles(cycles),
        .rd_req_valid(rd_req_valid), .rd_req_addr(rd_req_addr),
        .rd_req_tag(rd_req_tag), .rd_req_ready(rd_req_ready),
        .rr_valid(rr_valid), .rr_tag(rr_tag), .rr_data(rr_data),
        .wr_req_valid(wr_req_valid), .wr_req_addr(wr_req_addr),
        .wr_req_data(wr_req_data), .wr_req_ready(wr_req_ready)
    );

    assign dbg_cycles = cycles;
    assign dbg_fault  = fault;

    // ================================================ read responses
    always @(posedge clk) begin
        if (!resetn) begin
            rr_valid <= 1'b0; rr_tag <= 9'd0; rr_data <= 256'd0;
            recv_ready <= 1'b0;
        end else begin
            recv_ready <= 1'b1;
            rr_valid   <= 1'b0;
            if (recv_valid && recv_ready && (rtype == T_MEM_RD_RESP)) begin
                rr_valid <= 1'b1;
                rr_tag   <= {fill_bank, rtag};
                rr_data  <= recv_flit[255:0];
            end
        end
    end

    // ================================================ outbound memory traffic
    // A fill and a drain never share the send path: they belong to different
    // instructions and vec_core runs one at a time.
    reg [2:0]  wst;
    reg [33:0] w_addr;
    reg [255:0] w_data;

    always @(posedge clk) begin
        if (!resetn) begin
            send_valid <= 1'b0; send_flit <= {FLIT_WIDTH{1'b0}};
            rd_req_ready <= 1'b0; wr_req_ready <= 1'b0;
            wst <= W_IDLE; fill_bank <= 1'b0;
            w_addr <= 34'd0; w_data <= 256'd0;
        end else begin
            rd_req_ready <= 1'b0;
            wr_req_ready <= 1'b0;
            if (send_valid && send_ready) send_valid <= 1'b0;

            if (!send_valid || send_ready) begin
                case (wst)
                W_IDLE: begin
                    if (rd_req_valid && !rd_req_ready) begin
                        send_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                       POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                       T_MEM_RD_REQ, rd_req_tag[7:0], 1'b1, 3'b000,
                                       rd_req_addr, 6'd0, 8'd0, 8'd0, 200'd0 };
                        send_valid   <= 1'b1;
                        rd_req_ready <= 1'b1;
                        fill_bank    <= rd_req_tag[8];
                    end else if (wr_req_valid && !wr_req_ready) begin
                        w_addr <= wr_req_addr;
                        w_data <= wr_req_data;
                        send_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                       POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                       T_MEM_WR_REQ, 8'h01, 1'b0, 3'b000,
                                       wr_req_addr, 6'd0, 8'd0, 8'd0, 200'd0 };
                        send_valid   <= 1'b1;
                        wr_req_ready <= 1'b1;
                        wst <= W_DATA;
                    end
                end
                W_DATA: begin
                    send_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                   POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                   T_MEM_WR_DATA, 8'h01, 1'b1, 3'b000, w_data };
                    send_valid <= 1'b1;
                    wst <= W_IDLE;
                end
                default: wst <= W_IDLE;
                endcase
            end
        end
    end

    // ================================================ CU instruction handling
    localparam [1:0] C_IDLE = 2'd0, C_ACT = 2'd1, C_RUNW = 2'd2, C_RET = 2'd3;
    reg [1:0] cst;

    always @(posedge clk) begin
        if (!resetn) begin
            cst <= C_IDLE;
            inst_ready <= 1'b0; exec_done <= 1'b0; exec_fault <= 1'b0;
            exec_result <= 32'd0;
            ld_en <= 1'b0; ld_kind <= 1'b0; ld_addr <= 9'd0; ld_data <= 34'd0;
            start <= 1'b0; start_pc <= 9'd0;
        end else begin
            inst_ready <= 1'b0;
            exec_done  <= 1'b0;
            exec_fault <= 1'b0;
            ld_en      <= 1'b0;
            start      <= 1'b0;

            case (cst)
            C_IDLE: if (inst_valid && !inst_ready) begin
                inst_ready <= 1'b1;
                case (c_op)
                C_IMEM: begin
                    ld_en <= 1'b1; ld_kind <= 1'b0;
                    ld_addr <= c_addr; ld_data <= {2'd0, c_word};
                    cst <= C_RET;
                end
                C_DESC: begin
                    ld_en <= 1'b1; ld_kind <= 1'b1;
                    ld_addr <= {3'd0, c_ad, c_fld};
                    ld_data <= c_val;
                    cst <= C_RET;
                end
                C_RUN: begin
                    start <= 1'b1; start_pc <= c_addr;
                    cst <= C_ACT;
                end
                default: cst <= C_RET;
                endcase
            end

            // one cycle for `start` to be seen before `busy` is trusted
            C_ACT: cst <= C_RUNW;

            C_RUNW: if (halted || fault) begin
                exec_done   <= 1'b1;
                exec_fault  <= fault;
                exec_result <= fault ? {24'd0, fault_code} : cycles;
                cst <= C_IDLE;
            end

            C_RET: begin
                exec_done   <= 1'b1;
                exec_result <= 32'd0;
                cst <= C_IDLE;
            end
            default: cst <= C_IDLE;
            endcase
        end
    end

endmodule

`default_nettype wire

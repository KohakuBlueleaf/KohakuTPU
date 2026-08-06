// A cluster as a TWO-PORT NoC compute unit.
//
//   NoC <-> manager <-> tcu -> tcu -> tcu -> tcu -> acu <-> NoC
//            L1            direct DSP cascade        resident tile
//
// Port 0 (manager)  instructions in, operand fetch out, completion signals.
// Port 1 (acu)      result write-back.
//
// Two ports, not five. The chain eats eight 256-bit operand words per cycle and
// a port delivers one, so feeding the TCUs directly from the NoC is an 8x
// deficit no matter how many ports are spent on it. Reuse closes the gap
// instead: a Gm x Gn sub-tile block needs 4(Gm+Gn)/(Gm*Gn) words per cycle,
// which is 0.375 at 16x32. See docs/compute/tensor-isa.md s1.
//
// Instruction, in the CU_INST payload:
//
//   [255:252] opcode  1 = FILL, 2 = GEMM, 3 = DRAIN
//   [251:218] addr    FILL: operand base   DRAIN: destination base
//   [217:210] n       FILL: entries        DRAIN: sub-tiles
//   [209]     sel     FILL: 0 = A, 1 = B
//   [207:200] gm      GEMM: row groups
//   [199:192] gn      GEMM: column groups
//   [191:184] nk      GEMM: K blocks
//   [183:176] anchor  GEMM/DRAIN: common output exponent
//
// One L1 entry is four consecutive 256-bit memory words -- the four K-slices of
// one row group (A) or column group (B) -- so FILL issues one burst of 4 per
// entry and assembles them into the 928-bit entry the sweep consumes.

`default_nettype none

module mx_cluster_cu #(
    parameter integer FLIT_WIDTH = 288,
    parameter integer POS_WIDTH  = 4,
    parameter integer MGR_X      = 0,      // manager's NoC coordinates
    parameter integer MGR_Y      = 0,
    parameter integer ACU_X      = 0,      // accumulator's NoC coordinates
    parameter integer ACU_Y      = 1,
    parameter integer MEM_X      = 1,
    parameter integer MEM_Y      = 1,
    parameter integer TILES      = 256,
    parameter integer GA         = 32,
    parameter integer GB         = 32,
    parameter integer ACC_MW     = 14,
    parameter integer INST_DEPTH = 32,
    parameter integer MODEL      = 0,
    parameter         L1_PRIM    = "distributed"
)(
    input  wire                   clk,
    input  wire                   resetn,

    // ---- port 0: manager ----
    input  wire [FLIT_WIDTH-1:0]  m_in_data,
    input  wire                   m_in_valid,
    output wire                   m_in_busy,
    output wire [FLIT_WIDTH-1:0]  m_out_data,
    output wire                   m_out_valid,
    input  wire                   m_out_busy,

    // ---- port 1: accumulator ----
    input  wire [FLIT_WIDTH-1:0]  a_in_data,
    input  wire                   a_in_valid,
    output wire                   a_in_busy,
    output wire [FLIT_WIDTH-1:0]  a_out_data,
    output wire                   a_out_valid,
    input  wire                   a_out_busy,

    output wire [15:0]            fills_done,
    output wire [15:0]            gemms_done,
    output wire [15:0]            drains_done
);
    localparam [3:0] T_MEM_RD_REQ  = 4'h0,
                     T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2;

    localparam [3:0] OP_FILL = 4'd1, OP_GEMM = 4'd2, OP_DRAIN = 4'd3;

    // ================================================ framework, port 0
    wire [FLIT_WIDTH-1:0] inst_flit, recv_flit;
    wire                  inst_valid, recv_valid, send_ready;
    reg                   inst_ready, recv_ready;
    reg                   exec_done;
    reg  [31:0]           exec_result;
    reg  [FLIT_WIDTH-1:0] send_flit;
    reg                   send_valid;

    noc_cu_base #(
        .FLIT_WIDTH(FLIT_WIDTH), .POS_WIDTH(POS_WIDTH),
        .POS_X(MGR_X), .POS_Y(MGR_Y),
        .CU_TYPE(16'h4D47), .CU_VERSION(8'h01), .N_BUFFERS(2),
        .INST_DEPTH(INST_DEPTH)
    ) u_base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(m_in_data), .noc_in_valid(m_in_valid), .noc_in_busy(m_in_busy),
        .noc_out_data(m_out_data), .noc_out_valid(m_out_valid), .noc_out_busy(m_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result), .exec_fault(1'b0),
        .send_flit(send_flit), .send_valid(send_valid), .send_ready(send_ready),
        .recv_flit(recv_flit), .recv_valid(recv_valid), .recv_ready(recv_ready),
        .inst_space(), .busy()
    );

    wire [3:0]  i_op   = inst_flit[255 -: 4];
    wire [33:0] i_addr = inst_flit[251 -: 34];
    wire [7:0]  i_n    = inst_flit[217 -: 8];
    wire        i_sel  = inst_flit[209];
    wire [7:0]  i_gm   = inst_flit[207 -: 8];
    wire [7:0]  i_gn   = inst_flit[199 -: 8];
    wire [7:0]  i_nk   = inst_flit[191 -: 8];
    wire [7:0]  i_anch = inst_flit[183 -: 8];

    wire [3:0] rtype = recv_flit[FLIT_WIDTH-4*POS_WIDTH-1 -: 4];

    // ================================================ the cluster
    reg          l1_we, l1_sel;
    reg  [15:0]  l1_addr;
    reg  [927:0] l1_data;
    reg          gemm_start;
    reg  [7:0]   gm_r, gn_r, nk_r, anch_r;
    wire         gemm_busy;
    reg          drain_start;
    reg  [15:0]  drain_n;
    wire         drain_busy, drain_valid;
    wire [255:0] drain_data;
    wire [15:0]  drain_idx;

    mx_cluster_node #(.TILES(TILES), .GA(GA), .GB(GB),
                      .ACC_MW(ACC_MW), .MODEL(MODEL), .L1_PRIM(L1_PRIM)) u_node (
        .clk(clk), .rst(!resetn),
        .l1_we(l1_we), .l1_sel(l1_sel), .l1_addr(l1_addr), .l1_data(l1_data),
        .gemm_start(gemm_start), .gemm_gm(gm_r), .gemm_gn(gn_r),
        .gemm_nk(nk_r), .gemm_anchor(anch_r), .gemm_busy(gemm_busy),
        .drain_start(drain_start), .drain_n(drain_n), .drain_busy(drain_busy),
        .drain_data(drain_data), .drain_idx(drain_idx), .drain_valid(drain_valid)
    );

    // ================================================ fill / sequencer
    localparam [3:0] S_IDLE = 4'd0, S_FREQ = 4'd1, S_FRCV = 4'd2,
                     S_GEMM = 4'd3, S_GWAIT = 4'd4,
                     S_DRAIN = 4'd5, S_DWAIT = 4'd6, S_DONE = 4'd7,
                     S_FWR   = 4'd8;

    reg [3:0]  st;
    reg [33:0] base_r;
    reg [7:0]  n_r, ent;
    reg [2:0]  fl;
    reg [15:0] nfill, ngemm, ndrain;

    assign fills_done  = nfill;
    assign gemms_done  = ngemm;
    assign drains_done = ndrain;

    function [FLIT_WIDTH-1:0] rd_req;
        input [33:0] adr;
        begin
            rd_req = { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                       MGR_X[POS_WIDTH-1:0], MGR_Y[POS_WIDTH-1:0],
                       T_MEM_RD_REQ, 8'h01, 1'b1, 3'b000,
                       adr, 6'd0, 8'd3, 8'd0, 200'd0 };
        end
    endfunction

    integer bi, bk, bj;
    always @(posedge clk) begin
        if (!resetn) begin
            st <= S_IDLE; fl <= 3'd0; ent <= 8'd0;
            inst_ready <= 1'b0; recv_ready <= 1'b0;
            exec_done <= 1'b0; exec_result <= 32'd0;
            send_valid <= 1'b0; send_flit <= {FLIT_WIDTH{1'b0}};
            l1_we <= 1'b0; l1_sel <= 1'b0; l1_addr <= 16'd0; l1_data <= 928'd0;
            gemm_start <= 1'b0; drain_start <= 1'b0; drain_n <= 16'd0;
            gm_r <= 8'd1; gn_r <= 8'd1; nk_r <= 8'd1; anch_r <= 8'd0;
            base_r <= 34'd0; n_r <= 8'd1;
            nfill <= 16'd0; ngemm <= 16'd0; ndrain <= 16'd0;
        end else begin
            inst_ready  <= 1'b0;
            exec_done   <= 1'b0;
            l1_we       <= 1'b0;
            gemm_start  <= 1'b0;
            drain_start <= 1'b0;
            recv_ready  <= 1'b1;

            if (send_valid && send_ready) send_valid <= 1'b0;

            case (st)
            S_IDLE: if (inst_valid && !inst_ready) begin
                base_r <= i_addr;
                n_r    <= (i_n == 8'd0) ? 8'd1 : i_n;
                l1_sel <= i_sel;
                gm_r   <= i_gm; gn_r <= i_gn; nk_r <= i_nk; anch_r <= i_anch;
                inst_ready <= 1'b1;
                ent <= 8'd0; fl <= 3'd0;
                case (i_op)
                    OP_FILL:  st <= S_FREQ;
                    OP_GEMM:  st <= S_GEMM;
                    OP_DRAIN: st <= S_DRAIN;
                    default:  st <= S_DONE;
                endcase
            end

            // ---- FILL: one 4-word burst per L1 entry -------------------
            S_FREQ: if (!send_valid) begin
                send_flit  <= rd_req(base_r + {24'd0, ent} * 34'd128);
                send_valid <= 1'b1;
                fl <= 3'd0;
                st <= S_FRCV;
            end
            S_FRCV: if (recv_valid && recv_ready && rtype == T_MEM_RD_RESP) begin
                // word `fl` carries K-slice fl: elements 8*fl .. 8*fl+7.
                // Unrolled over fl rather than indexed by it -- a variable
                // part-select write builds a barrel mux across all 928 bits.
                for (bk = 0; bk < 8; bk = bk + 1) begin
                    if (!l1_sel) begin
                        for (bi = 0; bi < 4; bi = bi + 1) begin
                            if (fl == 3'd0) l1_data[(bi*32 + 0 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                            if (fl == 3'd1) l1_data[(bi*32 + 8 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                            if (fl == 3'd2) l1_data[(bi*32 +16 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                            if (fl == 3'd3) l1_data[(bi*32 +24 + bk)*7 +: 7] <= recv_flit[255 - (bi*8+bk)*7 -: 7];
                        end
                    end else begin
                        for (bj = 0; bj < 4; bj = bj + 1) begin
                            if (fl == 3'd0) l1_data[(( 0+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                            if (fl == 3'd1) l1_data[(( 8+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                            if (fl == 3'd2) l1_data[((16+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                            if (fl == 3'd3) l1_data[((24+bk)*4 + bj)*7 +: 7] <= recv_flit[255 - (bk*4+bj)*7 -: 7];
                        end
                    end
                end
                // scales ride in every word; take them from the first
                if (fl == 3'd0)
                    for (bi = 0; bi < 4; bi = bi + 1)
                        l1_data[896 + bi*8 +: 8] <= recv_flit[31 - bi*8 -: 8];

                if (fl == 3'd3) st <= S_FWR;
                else            fl <= fl + 3'd1;
            end
            S_FWR: begin
                l1_we   <= 1'b1;
                l1_addr <= {8'd0, ent};
                if (ent + 8'd1 == n_r) begin
                    nfill <= nfill + 16'd1;
                    st <= S_DONE;
                end else begin
                    ent <= ent + 8'd1;
                    st  <= S_FREQ;
                end
            end

            // ---- GEMM -------------------------------------------------
            S_GEMM: begin
                gemm_start <= 1'b1;
                st <= S_GWAIT;
            end
            S_GWAIT: if (!gemm_start && !gemm_busy) begin
                ngemm <= ngemm + 16'd1;
                st <= S_DONE;
            end

            // ---- DRAIN -------------------------------------------------
            S_DRAIN: begin
                drain_n     <= {8'd0, n_r};
                drain_start <= 1'b1;
                st <= S_DWAIT;
            end
            S_DWAIT: if (!drain_start && !drain_busy) begin
                ndrain <= ndrain + 16'd1;
                st <= S_DONE;
            end

            S_DONE: begin
                exec_done   <= 1'b1;
                exec_result <= {16'd0, nfill + ngemm + ndrain};
                st <= S_IDLE;
            end
            default: st <= S_IDLE;
            endcase
        end
    end

    // ================================================ port 1: result write-back
    // The accumulator's own port. Each drained sub-tile is one 256-bit word:
    // a two-flit MEM_WR_REQ, descriptor then data.
    reg [FLIT_WIDTH-1:0] w_flit;
    reg                  w_valid;
    reg [255:0]          w_data;
    reg [15:0]           w_idx;
    reg [1:0]            w_st;
    reg [33:0]           w_base;

    always @(posedge clk) begin
        if (!resetn) begin
            w_valid <= 1'b0; w_st <= 2'd0; w_flit <= {FLIT_WIDTH{1'b0}};
            w_data <= 256'd0; w_idx <= 16'd0; w_base <= 34'd0;
        end else begin
            if (w_valid && !a_out_busy) w_valid <= 1'b0;
            if (st == S_DRAIN) w_base <= base_r;

            case (w_st)
            2'd0: if (drain_valid) begin
                w_data <= drain_data;
                w_idx  <= drain_idx;
                w_st   <= 2'd1;
            end
            2'd1: if (!w_valid) begin
                w_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                            ACU_X[POS_WIDTH-1:0], ACU_Y[POS_WIDTH-1:0],
                            T_MEM_WR_REQ, 8'h02, 1'b0, 3'b000,
                            w_base + {18'd0, w_idx} * 34'd32,
                            6'd0, 8'd0, 8'd0, 200'd0 };
                w_valid <= 1'b1;
                w_st    <= 2'd2;
            end
            2'd2: if (!w_valid) begin
                w_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                            ACU_X[POS_WIDTH-1:0], ACU_Y[POS_WIDTH-1:0],
                            T_MEM_WR_REQ, 8'h02, 1'b1, 3'b000, w_data };
                w_valid <= 1'b1;
                w_st    <= 2'd0;
            end
            default: w_st <= 2'd0;
            endcase
        end
    end

    assign a_out_data  = w_flit;
    assign a_out_valid = w_valid;
    assign a_in_busy   = 1'b0;       // acknowledgements are discarded

endmodule

`default_nettype wire

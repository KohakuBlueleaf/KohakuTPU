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
    parameter         L1_PRIM    = "block",
    // A/B'd at RECV_DEPTH=64: -312 LUT, -280 FF, +4 BRAM tiles, and Fmax
    // identical to the digit (WNS 0.335 both arms, same path). The recv data
    // path is nowhere near critical here -- unlike the register file's port a.
    parameter         RECV_MEM   = "block"
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
    // CU_DATA is 0x8, NOT the 0x4 docs/noc/spec.md s4 still prints: 0x4 is
    // MEM_WR_DATA here and in mag_mem_port.v, so a CU_DATA flit carrying it
    // would have entered MAG's write queue as data. noc_pkt.vh records the
    // resolution and is included by nothing, so the value is re-declared here.
    localparam [3:0] T_MEM_RD_REQ  = 4'h0, T_MEM_WR_REQ  = 4'h1,
                     T_MEM_RD_RESP = 4'h2, T_MEM_WR_DATA = 4'h4,
                     T_CU_SIGNAL   = 4'h6, T_CU_DATA     = 4'h8;
    localparam [7:0] SIG_DATA_RECEIVED = 8'h03;

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
        // CU_VERSION IS A MESH-WIDE BUILD NUMBER, not this endpoint's revision:
        // the question a driver asks is "is this bitstream the one my compiler
        // targets". Bump it in EVERY endpoint whenever any ISA or datapath
        // changes. 0x01 shipped 2026-08; 0x02 is the vector datapath rebuild
        // (register file to BRAM, VRED kind 5) plus the merged cluster; 0x03 is
        // CU_DATA -- peer writes into L1, and VDRAIN's to_node sink. Against an
        // 0x02 bitstream a to_node drain would go to MEMORY in silence, which
        // is the case this field exists to catch.
        .CU_TYPE(16'h5643), .CU_VERSION(8'h03), .N_BUFFERS(2),
        .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH), .RECV_MEM(RECV_MEM)
    ) u_base (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(noc_out_busy),
        .inst_flit(inst_flit), .inst_valid(inst_valid), .inst_ready(inst_ready),
        .exec_done(exec_done), .exec_result(exec_result),
        .exec_fault(exec_fault),
        // The kernel's own run against the base's busy count: the difference
        // between them IS the dispatch overhead.
        .dbg_ctr({32'd0, cycles}),
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
    wire [POS_WIDTH-1:0] rsx = recv_flit[FLIT_WIDTH-2*POS_WIDTH-1 -: POS_WIDTH];
    wire [POS_WIDTH-1:0] rsy = recv_flit[FLIT_WIDTH-3*POS_WIDTH-1 -: POS_WIDTH];

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

    reg         cd_valid, cd_fault;
    reg  [8:0]  cd_addr;
    reg  [255:0] cd_data;

    wire        nd_valid, nd_sig;
    wire [3:0]  nd_x, nd_y, nd_buf;
    wire [15:0] nd_off;
    wire [7:0]  nd_len, nd_ack;
    wire [1:0]  nd_mesh;
    wire [7:0]  nd_fin;
    // Assigned rather than part-selected, so POS_WIDTH stays a free parameter.
    wire [POS_WIDTH-1:0] nd_dx = nd_x;
    wire [POS_WIDTH-1:0] nd_dy = nd_y;

    // A drain whose sink is in another mesh. `nd_x`/`nd_y` then address the
    // LOCAL MAG port, so the routers see an ordinary local flit and the NoC
    // never learns another mesh exists; the real destination rides in the txn
    // field, which CU_DATA does not use, and the mesh id in the reserved header
    // bits. Both on EVERY flit of the burst -- MAG's encapsulator would
    // otherwise need a CAM keyed on source to reunite a burst whose flits the
    // routers interleaved with another sender's.
    wire        nd_rem  = (nd_fin != 8'd0);
    wire [7:0]  nd_txn  = nd_rem ? nd_fin : 8'h00;
    wire [2:0]  nd_rsvd = nd_rem ? {1'b1, nd_mesh} : 3'b000;

    vec_core #(.MODEL(MODEL), .L1_DEPTH(L1_DEPTH), .L1_PRIM(L1_PRIM)) u_core (
        .clk(clk), .rst(!resetn),
        .ld_en(ld_en), .ld_kind(ld_kind), .ld_addr(ld_addr), .ld_data(ld_data),
        .start(start), .start_pc(start_pc),
        .busy(busy), .halted(halted), .fault(fault), .fault_code(fault_code),
        .cycles(cycles),
        .rd_req_valid(rd_req_valid), .rd_req_addr(rd_req_addr),
        .rd_req_tag(rd_req_tag), .rd_req_ready(rd_req_ready),
        .rr_valid(rr_valid), .rr_tag(rr_tag), .rr_data(rr_data),
        .cd_valid(cd_valid), .cd_addr(cd_addr), .cd_data(cd_data),
        .cd_fault(cd_fault),
        .wr_req_valid(wr_req_valid), .wr_req_addr(wr_req_addr),
        .wr_req_data(wr_req_data), .wr_req_ready(wr_req_ready),
        .nd_valid(nd_valid), .nd_x(nd_x), .nd_y(nd_y), .nd_buf(nd_buf),
        .nd_off(nd_off), .nd_len(nd_len), .nd_sig(nd_sig), .nd_ack(nd_ack),
        .nd_mesh(nd_mesh), .nd_fin(nd_fin)
    );

    assign dbg_cycles = cycles;
    assign dbg_fault  = fault;

    // ================================================ inbound flits
    // Fill responses and a peer's CU_DATA share the one receive queue, and the
    // burst is demultiplexed BY TYPE rather than by position: the router
    // interleaves whatever arrives, so a MEM_RD_RESP landing between a
    // descriptor and its data is ordinary rather than a protocol error.
    // ONE POP PER CYCLE is what keeps `rr_valid` and `cd_valid` exclusive.
    wire [7:0]  cud_buf = recv_flit[255 -: 8];
    wire [15:0] cud_off = recv_flit[247 -: 16];
    wire [7:0]  cud_len = recv_flit[231 -: 8];
    wire [7:0]  cud_flg = recv_flit[223 -: 8];
    // Where the completion goes: {ack_y, ack_x}, or 0 for the sender. A CU
    // answering its sender is useless when the SENDER is another CU -- nothing
    // consumes it and the host cannot sequence a reader behind the writer. 0 is
    // an unambiguous sentinel because (0,0) is a mesh CORNER, which touches no
    // router and can never hold an endpoint (scripts/py/gen_mesh.py).
    wire [7:0]  cud_ack = recv_flit[215 -: 8];

    // ONE FLAT L1, so `buf_id` is 0 or the burst is not addressed at anything
    // here; and `offset` is 16 bits against a 9-bit L1, so an out-of-range
    // burst would wrap and overwrite the bottom of the scratchpad.
    wire [16:0] cud_end = {1'b0, cud_off} + {9'd0, cud_len};
    wire        cud_bad = (cud_buf != 8'd0) || (cud_end >= L1_DEPTH[16:0]);

    reg         cd_st, cd_drop, cd_sig;
    reg  [7:0]  cd_left;
    reg  [8:0]  cd_ptr;

    reg         sg_pend;
    reg  [7:0]  sg_buf;
    // Where the ack GOES, and where the burst CAME FROM. Not the same fact
    // since the ack can be redirected, and conflating them made every data
    // flit of a redirected burst look like a second sender's.
    reg  [POS_WIDTH-1:0] sg_x, sg_y;
    reg  [POS_WIDTH-1:0] cd_sx, cd_sy;

    reg  [2:0]  wst;                 // declared here because `sg_go` reads it
    wire        sg_go = sg_pend && (wst == W_IDLE)
                        && (!send_valid || send_ready);

    wire cd_end_now = recv_valid && recv_ready && (rtype == T_CU_DATA)
                      && cd_st && (cd_left == 8'd0);

    // A data flit from someone other than the open burst's sender belongs to a
    // SECOND burst. There is one descriptor and one pointer here, so the two
    // cannot be told apart by content and merging them would be silent; the
    // SOURCE tells them apart for the price of one compare. Flits of one burst
    // cannot arrive out of order -- same source, same destination, so the same
    // dimension-ordered path -- which is what makes a mismatch conclusive.
    wire cd_alien = (rsx != cd_sx) || (rsy != cd_sy);

    always @(posedge clk) begin
        if (!resetn) begin
            rr_valid <= 1'b0; rr_tag <= 9'd0; rr_data <= 256'd0;
            cd_valid <= 1'b0; cd_addr <= 9'd0; cd_data <= 256'd0;
            cd_fault <= 1'b0;
            cd_st <= 1'b0; cd_drop <= 1'b0; cd_sig <= 1'b0;
            cd_left <= 8'd0; cd_ptr <= 9'd0;
            sg_pend <= 1'b0; sg_buf <= 8'd0;
            sg_x <= {POS_WIDTH{1'b0}}; sg_y <= {POS_WIDTH{1'b0}};
            cd_sx <= {POS_WIDTH{1'b0}}; cd_sy <= {POS_WIDTH{1'b0}};
            recv_ready <= 1'b0;
        end else begin
            rr_valid <= 1'b0;
            cd_valid <= 1'b0;
            cd_fault <= 1'b0;
            if (sg_go) sg_pend <= 1'b0;

            // Take nothing while a DATA_RECEIVED is waiting for the link: a
            // second burst could finish before this one is reported, and one
            // register would drop a completion, leaving its sender waiting
            // forever. Bounded -- the send path never waits on this one.
            recv_ready <= !((sg_pend || (cd_end_now && cd_sig)) && !sg_go);

            if (recv_valid && recv_ready) begin
                if (rtype == T_MEM_RD_RESP) begin
                    rr_valid <= 1'b1;
                    rr_tag   <= {fill_bank, rtag};
                    rr_data  <= recv_flit[255:0];
                end else if (rtype == T_CU_DATA) begin
                    if (!cd_st) begin
                        cd_st    <= 1'b1;
                        cd_left  <= cud_len;
                        cd_ptr   <= cud_off[8:0];
                        // A rejected burst is still COUNTED OUT, or its data
                        // flits would be read as the next descriptor.
                        cd_drop  <= cud_bad;
                        cd_fault <= cud_bad;
                        cd_sig   <= cud_flg[0] && !cud_bad;
                        sg_buf   <= cud_buf;
                        sg_x     <= (cud_ack != 8'd0) ? cud_ack[3:0] : rsx;
                        sg_y     <= (cud_ack != 8'd0) ? cud_ack[7:4] : rsy;
                        cd_sx    <= rsx;
                        cd_sy    <= rsy;
                    end else begin
                        cd_valid <= !cd_drop && !cd_alien;
                        cd_addr  <= cd_ptr;
                        cd_data  <= recv_flit[255:0];
                        cd_ptr   <= cd_ptr + 9'd1;
                        // Neither burst can be completed from here, so the rest
                        // of this one is dropped and the kernel is told.
                        if (cd_alien) begin
                            cd_fault <= 1'b1;
                            cd_drop  <= 1'b1;
                            cd_sig   <= 1'b0;
                        end
                        if (cd_left == 8'd0) begin
                            cd_st <= 1'b0;
                            if (cd_sig && !cd_alien) sg_pend <= 1'b1;
                        end else cd_left <= cd_left - 8'd1;
                    end
                end
            end
        end
    end

    // ================================================ outbound memory traffic
    // A fill and a drain never share the send path: they belong to different
    // instructions and vec_core runs one at a time.
    reg [33:0] w_addr;
    reg [255:0] w_data;
    reg        nd_hdr;
    reg [7:0]  nd_cnt;

    always @(posedge clk) begin
        if (!resetn) begin
            send_valid <= 1'b0; send_flit <= {FLIT_WIDTH{1'b0}};
            rd_req_ready <= 1'b0; wr_req_ready <= 1'b0;
            wst <= W_IDLE; fill_bank <= 1'b0;
            w_addr <= 34'd0; w_data <= 256'd0;
            nd_hdr <= 1'b0; nd_cnt <= 8'd0;
        end else begin
            rd_req_ready <= 1'b0;
            wr_req_ready <= 1'b0;
            if (send_valid && send_ready) send_valid <= 1'b0;

            if (!send_valid || send_ready) begin
                case (wst)
                W_IDLE: begin
                    // First: the peer that sent us a burst is blocked on this.
                    if (sg_pend) begin
                        send_flit <= { sg_x, sg_y,
                                       POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                       T_CU_SIGNAL, 8'h00, 1'b1, 3'b000,
                                       SIG_DATA_RECEIVED, {24'd0, sg_buf},
                                       216'd0 };
                        send_valid <= 1'b1;
                    end else if (rd_req_valid && !rd_req_ready) begin
                        send_flit <= { MEM_X[POS_WIDTH-1:0], MEM_Y[POS_WIDTH-1:0],
                                       POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                       T_MEM_RD_REQ, rd_req_tag[7:0], 1'b1, 3'b000,
                                       rd_req_addr, 6'd0, 8'd0, 8'd0, 200'd0 };
                        send_valid   <= 1'b1;
                        rd_req_ready <= 1'b1;
                        fill_bank    <= rd_req_tag[8];
                    // A peer drain: ONE descriptor for the whole walk, then the
                    // words. The first word waits a beat for the descriptor;
                    // the walk itself is the memory drain's, unchanged.
                    end else if (wr_req_valid && !wr_req_ready && nd_valid) begin
                        if (!nd_hdr) begin
                            send_flit <= { nd_dx, nd_dy,
                                POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                T_CU_DATA, nd_txn, 1'b0, nd_rsvd,
                                {4'd0, nd_buf}, nd_off, nd_len,
                                7'd0, nd_sig, nd_ack, 208'd0 };
                            send_valid <= 1'b1;
                            nd_hdr     <= 1'b1;
                            nd_cnt     <= 8'd0;
                        end else begin
                            send_flit <= { nd_dx, nd_dy,
                                POS_X[POS_WIDTH-1:0], POS_Y[POS_WIDTH-1:0],
                                T_CU_DATA, nd_txn, (nd_cnt == nd_len), nd_rsvd,
                                wr_req_data };
                            send_valid   <= 1'b1;
                            wr_req_ready <= 1'b1;
                            nd_cnt <= nd_cnt + 8'd1;
                            if (nd_cnt == nd_len) nd_hdr <= 1'b0;
                        end
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

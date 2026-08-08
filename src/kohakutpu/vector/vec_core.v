// The vector core: sequencer, L1 scratchpad, address generator and lanes.
//
// Executes docs/isa/vector.md against vec_lanes. The memory port is ABSTRACT
// -- one 256-bit word per request, tagged -- so the core carries no NoC
// knowledge; vec_cu.v does the framing.
//
// Three rules, and the reason for each:
//
//   ALU ops issue back to back, gated by a per-register pending-write count.
//   The lane is 14 deep with no bypass, so without it a program could read a
//   register whose write has not landed and be plausibly wrong.
//
//   VLD/VST/VSHUF/VBCAST/VCVT drain the lanes first. They drive the register
//   file's whole-chunk port, which shares write ports with ALU write-back.
//
//   VFILL is non-blocking, VDRAIN is not: a fill only WRITES L1, so it can
//   overlap compute; a drain reads L1 through the port VLD uses.

`default_nettype none

module vec_core #(
    parameter integer MODEL      = 1,
    parameter integer IMEM_DEPTH = 512,
    parameter integer L1_DEPTH   = 512,
    parameter         L1_PRIM    = "block",
    parameter         RF_PRIM    = "distributed"
)(
    input  wire         clk,
    input  wire         rst,

    input  wire         ld_en,
    input  wire         ld_kind,          // 0 = imem, 1 = descriptor
    input  wire [8:0]   ld_addr,          // desc: {ad[2:0], fld[2:0]}
    input  wire [33:0]  ld_data,

    input  wire         start,
    input  wire [8:0]   start_pc,
    output reg          busy,
    output reg          halted,
    output reg          fault,
    output reg  [7:0]   fault_code,
    output reg  [31:0]  cycles,

    output reg          rd_req_valid,
    output reg  [33:0]  rd_req_addr,
    output reg  [8:0]   rd_req_tag,
    input  wire         rd_req_ready,
    input  wire         rr_valid,
    input  wire [8:0]   rr_tag,
    input  wire [255:0] rr_data,

    output reg          wr_req_valid,
    output reg  [33:0]  wr_req_addr,
    output reg  [255:0] wr_req_data,
    input  wire         wr_req_ready
);
    localparam [4:0] O_VCVT = 5'h12, O_VRED = 5'h13, O_VLD = 5'h14,
                     O_VST = 5'h15, O_VBCAST = 5'h16, O_VSHUF = 5'h17,
                     O_VSETVL = 5'h18, O_VSETMD = 5'h19, O_VSETI = 5'h1A,
                     O_VLOOP = 5'h1B, O_VBAR = 5'h1C, O_VFILL = 5'h1D,
                     O_VDRAIN = 5'h1E, O_VHALT = 5'h1F;

    localparam [1:0] SRC_V = 2'd0, SRC_S = 2'd1, SRC_C = 2'd2, SRC_K = 2'd3;
    localparam [1:0] M_FLAT = 2'd0, M_D2 = 2'd1, M_TREE = 2'd3;
    localparam [2:0] DT_FP16 = 3'd1, DT_FP32 = 3'd2;
    localparam [23:0] E8_ONE = 24'h3F8000;

    localparam [7:0] F_DTYPE = 8'd1, F_VSRC = 8'd2, F_CHAIN = 8'd3,
                     F_OPCODE = 8'd4, F_LEN = 8'd5, F_LOOP = 8'd6,
                     F_VL = 8'd7, F_REDVL = 8'd8;

    localparam [4:0] S_IDLE = 5'd0,  S_F1 = 5'd1,   S_F2 = 5'd2,
                     S_DEC = 5'd3,   S_EXEC = 5'd4,
                     S_GA = 5'd5,    S_GB = 5'd6,   S_GC = 5'd7, S_GD = 5'd8,
                     S_ALU = 5'd9,   S_RED = 5'd10, S_RDRAIN = 5'd11,
                     S_RTAIL = 5'd12, S_RWAIT = 5'd13,
                     S_LDA = 5'd14,  S_LDW = 5'd15, S_LDD = 5'd16,
                     S_STR = 5'd17,  S_STW = 5'd18, S_STD = 5'd19,
                     S_FILL = 5'd20, S_DRA = 5'd21, S_DRW = 5'd22,
                     S_DRD = 5'd23,  S_BAR = 5'd24, S_WAITP = 5'd25,
                     S_SETI = 5'd26, S_SETI2 = 5'd27,
                     S_HALT = 5'd28, S_FAULT = 5'd29, S_MEM0 = 5'd30,
                     S_AGW = 5'd31;

    // ================================================== architectural state
    reg [4:0]  st;
    reg [8:0]  pc;
    reg [31:0] ir;
    reg [1:0]  vmode;
    reg [7:0]  vl;
    reg [23:0] sreg [0:15];
    reg [23:0] kreg [0:3];

    reg [8:0]  lp_top, lp_end;
    reg [23:0] lp_cnt;
    reg        lp_act;

    reg [19:0] g_op;
    reg [7:0]  g_sa, g_sb, g_sc;
    reg [95:0] g_ka, g_kb, g_kc;
    reg [2:0]  g_have;
    reg [3:0]  g_ra, g_rb, g_rc, g_wd;
    reg        g_cmp;

    reg [5:0]  bcnt, nbeat;
    reg [2:0]  cchunk;
    reg [1:0]  cphase;
    reg [3:0]  ls_reg;
    reg [2:0]  ls_dt;
    reg        ls_hi;
    reg [191:0] ls_hold;
    reg [8:0]  l1_base, l1_cur;
    reg [15:0] fill_out;
    reg [31:0] mem_left;
    reg [3:0]  shuf_k;
    reg [4:0]  ls_kind;
    reg        bc_to_s;

    reg [8:0]  im_addr;
    integer    ii;

    // ================================================== memories
    wire [31:0] im_q;
    kohaku_sdpram #(.WIDTH(32), .DEPTH(IMEM_DEPTH), .MEM_PRIM("distributed"),
                    .READ_LAT(1)) u_imem (
        .clk(clk), .wr_en(ld_en && !ld_kind), .wr_addr(ld_addr),
        .wr_data(ld_data[31:0]),
        .rd_en(1'b1), .rd_addr(im_addr), .rd_data(im_q)
    );

    reg  [8:0]   l1_waddr, l1_raddr;
    reg  [255:0] l1_wdata;
    reg          l1_we;
    wire [255:0] l1_q;
    kohaku_sdpram #(.WIDTH(256), .DEPTH(L1_DEPTH), .MEM_PRIM(L1_PRIM),
                    .READ_LAT(1)) u_l1 (
        .clk(clk), .wr_en(l1_we), .wr_addr(l1_waddr), .wr_data(l1_wdata),
        .rd_en(1'b1), .rd_addr(l1_raddr), .rd_data(l1_q)
    );

    // ================================================== address generator
    reg         ag_start, ag_step;
    reg  [2:0]  ag_sel;
    reg signed [17:0] ag_off;
    wire [33:0] ag_addr;
    wire        ag_last, ag_busy;
    wire [31:0] ag_total;

    vec_agu u_agu (
        .clk(clk), .rst(rst),
        .wr_en(ld_en && ld_kind), .wr_ad(ld_addr[5:3]), .wr_fld(ld_addr[2:0]),
        .wr_val(ld_data),
        .start(ag_start), .sel(ag_sel), .off(ag_off), .step(ag_step),
        .addr(ag_addr), .last(ag_last), .total(ag_total), .busy(ag_busy)
    );

    // ================================================== lanes
    reg          iss_valid, iss_is_cmp, iss_tail, red_init;
    reg  [1:0]   iss_phase, g_pm, g_pr;
    reg  [6:0]   iss_ra, iss_rb, iss_rc, iss_wa;
    reg  [2:0]   iss_chunk, red_kind;
    reg  [15:0]  iss_tmask;
    reg          lw_we, lw_ract;
    reg  [6:0]   lw_waddr, lw_raddr;
    reg  [383:0] lw_wdata;
    wire [383:0] lw_rdata;
    wire [23:0]  red_result;
    wire         red_valid, pipe_empty, wb_fire;
    wire [3:0]   wb_vreg;
    wire [127:0] p_bits;

    vec_lanes #(.MODEL(MODEL), .RF_PRIM(RF_PRIM)) u_lanes (
        .clk(clk), .rst(rst), .mode(vmode),
        .ls_we(lw_we), .ls_waddr(lw_waddr), .ls_wdata(lw_wdata),
        .ls_raddr(lw_raddr), .ls_ractive(lw_ract), .ls_rdata(lw_rdata),
        .iss_valid(iss_valid), .iss_phase(iss_phase),
        .iss_ra(iss_ra), .iss_rb(iss_rb), .iss_rc(iss_rc), .iss_wa(iss_wa),
        .iss_pm(g_pm), .iss_pr(g_pr), .iss_chunk(iss_chunk),
        .iss_is_cmp(iss_is_cmp), .iss_tail(iss_tail), .iss_tmask(iss_tmask),
        .st_op(g_op), .st_sa(g_sa), .st_sb(g_sb), .st_sc(g_sc),
        .st_ka(g_ka), .st_kb(g_kb), .st_kc(g_kc),
        .red_init(red_init), .red_kind(red_kind),
        .p_rd_sel(g_pr), .p_rd_bits(p_bits),
        .red_result(red_result), .red_valid(red_valid),
        .pipe_empty(pipe_empty), .wb_fire(wb_fire), .wb_vreg(wb_vreg)
    );

    // ================================================== decode
    wire [4:0] d_op = ir[31:27];
    wire [1:0] d_sa = ir[26:25], d_sb = ir[24:23], d_sc = ir[22:21];
    wire [3:0] d_vd = ir[20:17], d_va = ir[16:13];
    wire [3:0] d_vb = ir[12:9],  d_vc = ir[8:5];
    wire [1:0] d_pr = ir[4:3],   d_pm = ir[2:1];
    wire [2:0] d_dt = ir[26:24], d_ad = ir[23:21];
    wire [1:0] d_lpm = ir[15:14];
    wire signed [13:0] d_off = ir[13:0];

    wire is_alu = (d_op <= 5'h11);
    wire is_cmp = (d_op >= 5'h0B) && (d_op <= 5'h0D);
    // vec_alu numbers the four seeds 16..19; the ISA numbers them 14..17.
    wire [4:0] alu_op = (d_op >= 5'h0E && d_op <= 5'h11) ? (d_op + 5'd2) : d_op;
    wire dt_ok = (d_dt == DT_FP16) || (d_dt == DT_FP32);

    wire [2:0] dep = (vmode == M_FLAT) ? 3'd1 : (vmode == M_D2) ? 3'd2 : 3'd4;
    wire [3:0] nchunk = {1'b0, vl[7:4]} + (|vl[3:0] ? 4'd1 : 4'd0);

    wire [15:0] tail_full = 16'hFFFF;
    wire [15:0] tail_part = (16'd1 << vl[3:0]) - 16'd1;
    wire [15:0] tmask_now = ({1'b0, cchunk} < vl[7:4]) ? tail_full : tail_part;

    reg [4:0] pend [0:15];
    wire haz = (|pend[g_ra]) | (|pend[g_rb]) | (|pend[g_rc]) | (|pend[g_wd]);

    wire fill_issue = (st == S_FILL) && (mem_left != 32'd0) && !rd_req_valid;
    wire alu_issue  = (st == S_ALU) && (bcnt != nbeat)
                                    && !((bcnt == 6'd0) && haz);
    wire pend_inc   = alu_issue && !g_cmp;

    function [23:0] pick;
        input [1:0] sel;
        input [3:0] rn;
        begin
            pick = (sel == SRC_S) ? sreg[rn]
                 : (sel == SRC_K) ? kreg[rn[1:0]] : 24'd0;
        end
    endfunction

    // ================================================== conversion
    // VCVT.FP16 reuses the store converters feeding the load converters, so a
    // round trip costs a mux rather than a third set. VCVT.FP32 is the
    // identity -- E8M15 -> FP32 -> E8M15 is exact (tests/vector/vec_cvt_tb s4).
    wire [255:0] o_f16, o_f32lo, o_f32hi;
    wire [255:0] cv_src = (ls_kind == O_VCVT) ? o_f16 : l1_q;
    wire [383:0] i_f16;
    wire [191:0] i_f32;

    genvar e;
    generate
    for (e = 0; e < 16; e = e + 1) begin : g_c16
        vec_cvt_f16_to_e8 u_i (.f16(cv_src[e*16 +: 16]),
                               .e8(i_f16[e*24 +: 24]));
        vec_cvt_e8_to_f16 u_o (.e8(lw_rdata[e*24 +: 24]),
                               .f16(o_f16[e*16 +: 16]));
    end
    for (e = 0; e < 8; e = e + 1) begin : g_c32
        vec_cvt_f32_to_e8 u_i (.f32(cv_src[e*32 +: 32]),
                               .e8(i_f32[e*24 +: 24]));
        vec_cvt_e8_to_f32 u_ol (.e8(lw_rdata[e*24 +: 24]),
                                .f32(o_f32lo[e*32 +: 32]));
        vec_cvt_e8_to_f32 u_oh (.e8(lw_rdata[(e+8)*24 +: 24]),
                                .f32(o_f32hi[e*32 +: 32]));
    end
    endgenerate

    // lane rotate: vd[i] = va[(i+k) mod 16]
    reg [383:0] shuf_out;
    integer sh;
    always @(*) begin
        shuf_out = 384'd0;
        for (sh = 0; sh < 16; sh = sh + 1)
            shuf_out[sh*24 +: 24] = lw_rdata[(((sh + shuf_k) & 15))*24 +: 24];
    end

    reg [383:0] bcast_out;
    integer bi;
    always @(*) begin
        bcast_out = 384'd0;
        for (bi = 0; bi < 16; bi = bi + 1)
            bcast_out[bi*24 +: 24] = sreg[g_ra];
    end

    // ANY/ALL reduce the PREDICATE file, not the vector file: they never
    // enter the tree, so they cost one cycle and no ALU.
    wire [127:0] vlmask = (vl >= 8'd128) ? {128{1'b1}}
                                         : (({128'd1} << vl[6:0]) - 128'd1);
    wire p_any = |(p_bits & vlmask);
    wire p_all = ((p_bits & vlmask) == vlmask);

    // ================================================== sequencer
    always @(posedge clk) begin
        if (rst) begin
            st <= S_IDLE; pc <= 9'd0; ir <= 32'd0; im_addr <= 9'd0;
            vmode <= M_FLAT; vl <= 8'd128;
            busy <= 1'b0; halted <= 1'b0; fault <= 1'b0; fault_code <= 8'd0;
            cycles <= 32'd0; bcnt <= 6'd0; nbeat <= 6'd0;
            cchunk <= 3'd0; cphase <= 2'd0;
            lp_act <= 1'b0; lp_cnt <= 24'd0; lp_top <= 9'd0; lp_end <= 9'd0;
            g_have <= 3'd0; fill_out <= 16'd0; mem_left <= 32'd0;
            g_op <= 20'd0; g_sa <= 8'd0; g_sb <= 8'd0; g_sc <= 8'd0;
            g_ka <= 96'd0; g_kb <= 96'd0; g_kc <= 96'd0;
            g_ra <= 4'd0; g_rb <= 4'd0; g_rc <= 4'd0; g_wd <= 4'd0;
            g_pm <= 2'd0; g_pr <= 2'd0; g_cmp <= 1'b0;
            iss_valid <= 1'b0; iss_tail <= 1'b0; red_init <= 1'b0;
            iss_is_cmp <= 1'b0; iss_tmask <= 16'hFFFF;
            iss_ra <= 7'd0; iss_rb <= 7'd0; iss_rc <= 7'd0; iss_wa <= 7'd0;
            iss_chunk <= 3'd0; iss_phase <= 2'd0; red_kind <= 3'd0;
            lw_we <= 1'b0; lw_ract <= 1'b0; lw_waddr <= 7'd0; lw_raddr <= 7'd0;
            lw_wdata <= 384'd0;
            rd_req_valid <= 1'b0; wr_req_valid <= 1'b0;
            rd_req_addr <= 34'd0; rd_req_tag <= 9'd0;
            wr_req_addr <= 34'd0; wr_req_data <= 256'd0;
            ag_start <= 1'b0; ag_step <= 1'b0; ag_sel <= 3'd0; ag_off <= 18'd0;
            l1_we <= 1'b0; l1_waddr <= 9'd0; l1_raddr <= 9'd0;
            l1_wdata <= 256'd0; l1_base <= 9'd0; l1_cur <= 9'd0;
            ls_reg <= 4'd0; ls_dt <= 3'd0; ls_hi <= 1'b0; ls_hold <= 192'd0;
            ls_kind <= 5'd0; shuf_k <= 4'd0; bc_to_s <= 1'b0;
            for (ii = 0; ii < 16; ii = ii + 1) begin
                sreg[ii] <= 24'd0;
                pend[ii] <= 5'd0;
            end
            kreg[0] <= 24'h000000; kreg[1] <= E8_ONE;
            kreg[2] <= 24'hBF8000; kreg[3] <= 24'h000000;
        end else begin
            iss_valid <= 1'b0;
            iss_tail  <= 1'b0;
            red_init  <= 1'b0;
            lw_we     <= 1'b0;
            ag_start  <= 1'b0;
            ag_step   <= 1'b0;
            l1_we     <= 1'b0;
            if (rd_req_valid && rd_req_ready) rd_req_valid <= 1'b0;
            if (wr_req_valid && wr_req_ready) wr_req_valid <= 1'b0;
            if (busy) cycles <= cycles + 32'd1;

            // A fill response lands where its tag says, whatever the order.
            // `fill_out` is updated ONCE, below: issuing and retiring can land
            // in the same cycle, and two assignments would drop one of them.
            if (rr_valid) begin
                l1_we    <= 1'b1;
                l1_waddr <= rr_tag;
                l1_wdata <= rr_data;
            end
            fill_out <= fill_out + (fill_issue ? 16'd1 : 16'd0)
                                 - (rr_valid   ? 16'd1 : 16'd0);

            // One update per register per cycle: an issue and a retire can
            // land together, and two assignments would drop one of them.
            for (ii = 0; ii < 16; ii = ii + 1)
                pend[ii] <= pend[ii]
                          + ((pend_inc && (g_wd == ii[3:0])) ? 5'd1 : 5'd0)
                          - ((wb_fire && (wb_vreg == ii[3:0])
                              && (pend[ii] != 5'd0)) ? 5'd1 : 5'd0);

            case (st)
            // ---------------------------------------------------- fetch
            S_IDLE: if (start) begin
                pc <= start_pc; im_addr <= start_pc;
                busy <= 1'b1; halted <= 1'b0; fault <= 1'b0;
                cycles <= 32'd0; g_have <= 3'd0; lp_act <= 1'b0;
                st <= S_F2;
            end

            // The hardware loop closes HERE, not after the case: inside the
            // block `st` still reads its old value, so testing it there would
            // never fire.
            S_F1: begin
                if (lp_act && (pc == lp_end)) begin
                    if (lp_cnt > 24'd1) begin
                        lp_cnt  <= lp_cnt - 24'd1;
                        pc      <= lp_top;
                        im_addr <= lp_top;
                    end else begin
                        lp_act  <= 1'b0;
                        im_addr <= pc;
                    end
                end else im_addr <= pc;
                st <= S_F2;
            end
            S_F2: st <= S_DEC;
            S_DEC: begin ir <= im_q; st <= S_EXEC; end

            // ---------------------------------------------------- issue
            S_EXEC: begin
                pc <= pc + 9'd1;
                ls_kind <= d_op;
                ls_reg  <= d_vd;
                ls_dt   <= d_dt;
                cchunk  <= 3'd0;
                cphase  <= 2'd0;
                bcnt    <= 6'd0;
                ls_hi   <= 1'b0;
                l1_base <= d_off[8:0];
                l1_cur  <= d_off[8:0];
                ag_sel  <= d_ad;
                ag_off  <= 18'd0;

                if (is_alu) begin
                    g_ra <= d_va; g_rb <= d_vb; g_rc <= d_vc; g_wd <= d_vd;
                    g_pm <= d_pm; g_pr <= d_pr; g_cmp <= is_cmp;
                    g_op[0 +: 5] <= alu_op;
                    g_sa[0 +: 2] <= d_sa; g_sb[0 +: 2] <= d_sb;
                    g_sc[0 +: 2] <= d_sc;
                    g_ka[0 +: 24] <= pick(d_sa, d_va);
                    g_kb[0 +: 24] <= pick(d_sb, d_vb);
                    g_kc[0 +: 24] <= pick(d_sc, d_vc);
                    if ((d_sa == SRC_C) || (d_sb == SRC_C) || (d_sc == SRC_C)) begin
                        fault_code <= F_CHAIN; st <= S_FAULT;
                    end else if (dep == 3'd1) begin
                        nbeat <= {2'd0, nchunk};
                        st <= S_ALU;
                    end else begin
                        g_have <= 3'd1;
                        st <= S_GA;
                    end
                end else begin
                    case (d_op)
                    O_VSETVL: begin
                        if ((sreg[d_va] == 24'd0) || (sreg[d_va] > 24'd128)) begin
                            fault_code <= F_VL; st <= S_FAULT;
                        end else begin
                            vl <= sreg[d_va][7:0];
                            st <= S_F1;
                        end
                    end
                    O_VSETMD: if (pipe_empty) begin
                        vmode <= d_va[1:0];
                        st <= S_F1;
                    end else begin
                        pc <= pc;            // hold; re-enter next cycle
                    end
                    O_VSETI: begin im_addr <= pc + 9'd1; st <= S_SETI; end
                    O_VLOOP: begin
                        if (lp_act) begin
                            fault_code <= F_LOOP; st <= S_FAULT;
                        end else begin
                            lp_act <= 1'b1;
                            lp_cnt <= sreg[d_va];
                            lp_top <= pc + 9'd1;
                            lp_end <= pc + 9'd1 + {5'd0, d_vb};
                            st <= S_F1;
                        end
                    end
                    O_VRED: begin
                        g_ra <= d_va; g_rb <= d_vb; g_rc <= d_va;
                        g_wd <= 4'd0; g_pr <= d_pr;
                        red_kind <= d_vc[2:0];
                        if (d_vc[2:0] == 3'd5) begin
                            fault_code <= F_OPCODE; st <= S_FAULT;
                        end else if (d_vc[2:0] >= 3'd6) begin
                            sreg[d_vd] <= (d_vc[2:0] == 3'd6)
                                        ? (p_any ? E8_ONE : 24'd0)
                                        : (p_all ? E8_ONE : 24'd0);
                            st <= S_F1;
                        end else if (|vl[3:0]) begin
                            // a partial chunk would feed stale slots into the
                            // tree, and the tree has no per-slot mask
                            fault_code <= F_REDVL; st <= S_FAULT;
                        end else if (vmode != M_TREE) begin
                            fault_code <= F_OPCODE; st <= S_FAULT;
                        end else begin
                            ls_reg <= d_vd;
                            nbeat  <= ((d_vc[2:0] == 3'd3) || (d_vc[2:0] == 3'd4))
                                    ? {1'd0, nchunk, 1'b0} : {2'd0, nchunk};
                            red_init <= 1'b1;
                            st <= S_RED;
                        end
                    end
                    O_VLD, O_VST, O_VCVT, O_VSHUF, O_VBCAST: begin
                        // VST's register field is a SOURCE, and in the
                        // load/store encoding it sits at vd.
                        g_ra <= (d_op == O_VST) ? d_vd : d_va;
                        shuf_k <= sreg[d_vb][3:0];
                        bc_to_s <= (d_op == O_VBCAST) && (d_sa != SRC_S);
                        if ((d_op == O_VLD || d_op == O_VST || d_op == O_VCVT)
                            && !dt_ok) begin
                            fault_code <= F_DTYPE; st <= S_FAULT;
                        end else if (!pipe_empty) begin
                            pc <= pc;
                        end else begin
                            ag_start <= 1'b1;
                            ag_off   <= {{4{d_off[13]}}, d_off};
                            st <= S_AGW;
                        end
                    end
                    O_VFILL, O_VDRAIN: begin
                        // A response names its L1 slot through the NoC's 8-bit
                        // txn field, so only one fill may be in flight; a
                        // second waits rather than aliasing the first's tags.
                        if ((d_op == O_VFILL) && (fill_out != 16'd0)) begin
                            pc <= pc;
                        end else begin
                            ag_start <= 1'b1;
                            st <= S_AGW;
                        end
                    end
                    O_VBAR:  st <= S_BAR;
                    O_VHALT: st <= S_WAITP;
                    default: begin fault_code <= F_OPCODE; st <= S_FAULT; end
                    endcase
                end
            end

            // ---- gather the rest of a chain, one instruction per 4 cycles --
            S_GA: begin im_addr <= pc; st <= S_GB; end
            S_GB: st <= S_GC;
            S_GC: begin ir <= im_q; st <= S_GD; end
            S_GD: begin
                pc <= pc + 9'd1;
                g_op[g_have*5 +: 5] <= alu_op;
                g_sa[g_have*2 +: 2] <= d_sa;
                g_sb[g_have*2 +: 2] <= d_sb;
                g_sc[g_have*2 +: 2] <= d_sc;
                g_ka[g_have*24 +: 24] <= pick(d_sa, d_va);
                g_kb[g_have*24 +: 24] <= pick(d_sb, d_vb);
                g_kc[g_have*24 +: 24] <= pick(d_sc, d_vc);
                g_wd  <= d_vd;
                g_pm  <= d_pm;
                g_pr  <= d_pr;
                g_cmp <= is_cmp;
                if ((d_sa == SRC_V) || (d_sb == SRC_V) || (d_sc == SRC_V)) begin
                    fault_code <= F_VSRC; st <= S_FAULT;
                end else if (g_have + 3'd1 == dep) begin
                    nbeat <= {2'd0, nchunk} * {3'd0, dep};
                    st <= S_ALU;
                end else begin
                    g_have <= g_have + 3'd1;
                    st <= S_GA;
                end
            end

            // ---------------------------------------------------- ALU beats
            S_ALU: begin
                if ((bcnt == 6'd0) && haz) begin
                    st <= S_ALU;
                end else if (bcnt == nbeat) begin
                    g_have <= 3'd0;
                    st <= S_F1;
                end else begin
                    iss_valid  <= 1'b1;
                    iss_is_cmp <= g_cmp;
                    iss_chunk  <= cchunk;
                    iss_phase  <= cphase;
                    iss_tmask  <= tmask_now;
                    iss_ra <= {g_ra, cchunk};
                    iss_rb <= {g_rb, cchunk};
                    iss_rc <= {g_rc, cchunk};
                    iss_wa <= {g_wd, cchunk};
                    bcnt <= bcnt + 6'd1;
                    if (cphase + 2'd1 == dep[1:0] || dep == 3'd1) begin
                        cphase <= 2'd0;
                        cchunk <= cchunk + 3'd1;
                    end else begin
                        cphase <= cphase + 2'd1;
                    end
                end
            end

            // ---------------------------------------------------- reduction
            S_RED: begin
                if (bcnt == nbeat) st <= S_RDRAIN;
                else begin
                    iss_valid  <= 1'b1;
                    iss_is_cmp <= 1'b0;
                    iss_chunk  <= cchunk;
                    iss_phase  <= cphase;
                    iss_tmask  <= 16'hFFFF;
                    iss_ra <= {g_ra, cchunk};
                    iss_rb <= {g_rb, cchunk};
                    iss_rc <= {g_ra, cchunk};
                    bcnt <= bcnt + 6'd1;
                    if ((red_kind == 3'd3) || (red_kind == 3'd4)) begin
                        if (cphase == 2'd1) begin
                            cphase <= 2'd0; cchunk <= cchunk + 3'd1;
                        end else cphase <= 2'd1;
                    end else cchunk <= cchunk + 3'd1;
                end
            end
            S_RDRAIN: if (pipe_empty) begin
                iss_valid <= 1'b1;
                iss_tail  <= 1'b1;
                iss_phase <= 2'd0;
                iss_chunk <= 3'd0;
                iss_tmask <= 16'hFFFF;
                iss_ra <= {g_ra, 3'd0};
                iss_rb <= {g_rb, 3'd0};
                iss_rc <= {g_ra, 3'd0};
                st <= S_RWAIT;
            end
            S_RTAIL: st <= S_RWAIT;
            S_RWAIT: if (red_valid) begin
                sreg[ls_reg] <= red_result;
                st <= S_F1;
            end

            // ---------------------------------------------------- VLD
            S_LDA: begin
                l1_raddr <= ag_addr[8:0];
                ag_step  <= 1'b1;
                st <= S_LDW;
            end
            S_LDW: st <= S_LDD;
            S_LDD: begin
                if (ls_dt == DT_FP16) begin
                    lw_wdata <= i_f16;
                    lw_waddr <= {ls_reg, cchunk};
                    lw_we    <= 1'b1;
                    if (cchunk + 3'd1 == nchunk[2:0]) st <= S_F1;
                    else begin cchunk <= cchunk + 3'd1; st <= S_LDA; end
                end else begin
                    if (!ls_hi) begin
                        ls_hold <= i_f32;
                        ls_hi   <= 1'b1;
                        st <= S_LDA;
                    end else begin
                        lw_wdata <= {i_f32, ls_hold};
                        lw_waddr <= {ls_reg, cchunk};
                        lw_we    <= 1'b1;
                        ls_hi    <= 1'b0;
                        if (cchunk + 3'd1 == nchunk[2:0]) st <= S_F1;
                        else begin cchunk <= cchunk + 3'd1; st <= S_LDA; end
                    end
                end
            end

            // ------------------------------- VST / VCVT / VSHUF / VBCAST
            S_STR: begin
                lw_ract  <= 1'b1;
                lw_raddr <= {g_ra, cchunk};
                st <= S_STW;
            end
            S_STW: st <= S_STD;
            // A fill response owns L1's single write port, so a VST that would
            // land on the same cycle holds instead of being silently dropped.
            S_STD: if (rr_valid && (ls_kind == O_VST)) begin
                st <= S_STD;
            end else begin
                case (ls_kind)
                O_VST: begin
                    l1_waddr <= ag_addr[8:0];
                    ag_step  <= 1'b1;
                    l1_we    <= 1'b1;
                    l1_wdata <= (ls_dt == DT_FP16) ? o_f16
                              : (ls_hi ? o_f32hi : o_f32lo);
                end
                O_VCVT: begin
                    lw_wdata <= (ls_dt == DT_FP16) ? i_f16 : lw_rdata;
                    lw_waddr <= {ls_reg, cchunk};
                    lw_we    <= 1'b1;
                end
                O_VSHUF: begin
                    lw_wdata <= shuf_out;
                    lw_waddr <= {ls_reg, cchunk};
                    lw_we    <= 1'b1;
                end
                default: begin                      // VBCAST
                    if (bc_to_s) sreg[ls_reg] <= lw_rdata[23:0];
                    else begin
                        lw_wdata <= bcast_out;
                        lw_waddr <= {ls_reg, cchunk};
                        lw_we    <= 1'b1;
                    end
                end
                endcase

                if ((ls_kind == O_VST) && (ls_dt == DT_FP32) && !ls_hi) begin
                    ls_hi <= 1'b1;
                    st <= S_STD;
                end else begin
                    ls_hi <= 1'b0;
                    if (bc_to_s || (cchunk + 3'd1 == nchunk[2:0])) begin
                        lw_ract <= 1'b0;
                        st <= S_F1;
                    end else begin
                        cchunk <= cchunk + 3'd1;
                        st <= S_STR;
                    end
                end
            end

            // ---------------------------------------------------- VFILL
            S_FILL: begin
                if (mem_left == 32'd0) st <= S_F1;
                else if (fill_issue) begin
                    rd_req_valid <= 1'b1;
                    rd_req_addr  <= ag_addr;
                    rd_req_tag   <= l1_cur;
                    ag_step      <= 1'b1;
                    l1_cur       <= l1_cur + 9'd1;
                    mem_left     <= mem_left - 32'd1;
                end
            end

            // ---------------------------------------------------- VDRAIN
            S_DRA: if (mem_left == 32'd0) st <= S_F1;
                   else begin
                       l1_raddr <= l1_cur;
                       st <= S_DRW;
                   end
            S_DRW: st <= S_DRD;
            S_DRD: if (!wr_req_valid) begin
                wr_req_valid <= 1'b1;
                wr_req_addr  <= ag_addr;
                wr_req_data  <= l1_q;
                ag_step      <= 1'b1;
                l1_cur       <= l1_cur + 9'd1;
                mem_left     <= mem_left - 32'd1;
                st <= S_DRA;
            end

            // ---------------------------------------------------- misc
            // ag_start is still high in THIS state and the AGU latches its
            // descriptor at the end of it, so neither ag_addr nor ag_total is
            // trustworthy until the state after. Every walk waits here first.
            S_AGW: st <= (ls_kind == O_VLD) ? S_LDA
                       : ((ls_kind == O_VFILL) || (ls_kind == O_VDRAIN))
                         ? S_MEM0 : S_STR;

            S_MEM0: if (ag_total > 32'd256) begin
                fault_code <= F_LEN; st <= S_FAULT;
            end else begin
                mem_left <= ag_total;
                st <= (ls_kind == O_VFILL) ? S_FILL : S_DRA;
            end

            S_BAR:  if (fill_out == 16'd0) st <= S_F1;
            S_SETI: st <= S_SETI2;
            S_SETI2: begin
                if (d_sa == SRC_K) kreg[3] <= im_q[23:0];
                else               sreg[d_vd] <= im_q[23:0];
                pc <= pc + 9'd1;
                st <= S_F1;
            end
            S_WAITP: if (pipe_empty && (fill_out == 16'd0)) st <= S_HALT;
            S_HALT: begin
                busy <= 1'b0; halted <= 1'b1;
                st <= S_IDLE;
            end
            S_FAULT: begin
                busy <= 1'b0; fault <= 1'b1;
                st <= S_IDLE;
            end
            default: st <= S_FAULT;
            endcase
        end
    end

endmodule

`default_nettype wire

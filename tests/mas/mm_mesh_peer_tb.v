// A CLUSTER DRAINING INTO A VECTOR CORE, over the real mesh.

// mx_cluster_data_tb proves a cluster SENDS CU_DATA and vec_cu_tb proves a
// vector core RECEIVES one. Neither joins them; the fused epilogue does.

//   pass A   DRAIN dnode=1 -> the BENCH, giving the golden FP16 sub-tiles
//   pass B   DRAIN dnode=1 -> the VECTOR CORE, which writes L1 back to DRAM

// The same GEMM over the same L1 is deterministic, so equality needs no float
// model and any difference between the two passes is the transport.

`default_nettype none
`timescale 1ns/1ps

module mm_mesh_peer_tb #(
    // HALF periods in ns, against the mesh's 2.0. One rate per TYPE, so a peer
    // drain crosses from the matmul domain to a genuinely different vector one.
    parameter integer UNIT_CDC = 0,
    parameter real    MHP      = 2.0,
    parameter real    VHP      = 2.0
);
    // 34, NOT 40: at AW=40 mm_mover.v:347/:650 fail elaboration on a
    // non-positive replication constant. Raise once that lands.
    localparam FW = 288, PW = 4, DW = 256, AW = 40, IDW = 4;
    localparam MEMP = 1, NCH = 1;

    localparam [3:0] T_CU_INST = 4'h5, T_CU_SIGNAL = 4'h6, T_CU_DATA = 4'h8;
    localparam [7:0] SIG_DATA_RECEIVED = 8'h03;
    // Granules the cluster gathers into one CU_DATA descriptor, so a drain of
    // `n` FP16 sub-tiles is ceil(n/8) bursts and that many acknowledgements.
    localparam integer WBURST = 8;

    // The bench is the agent, on r11's local, so its coordinate is the
    // router's. The cluster and the vector core are where mm_mesh puts them.
    localparam CX = 1, CY = 1;
    localparam VX = 1, VY = 0;
    localparam LX = 2, LY = 1;

    localparam integer SBIAS = 20;
    localparam [7:0] ANCHOR = 8'd40;             // 2*SBIAS, cancels both biases

    localparam [33:0] A_OUT = 34'h8000;          // passthrough drain lands here
    localparam [33:0] A_OUT2 = 34'h8800;         // the epilogue's own drain
    // Well clear of the delivered tile, which starts at L1 word 0.
    localparam integer OUT_W = 64;

    reg clk = 0, rst = 1;
    always #2 clk = ~clk;

    reg mat_clk = 0, vec_clk = 0;
    always #MHP mat_clk = ~mat_clk;
    always #VHP vec_clk = ~vec_clk;

    reg  [FW-1:0] ext_i;
    reg           ext_iv;
    wire          ext_ib;
    wire [FW-1:0] ext_o;
    wire          ext_ov;

    wire [NCH*IDW-1:0]  r_awid, r_arid, r_bid, r_rid;
    wire [NCH*AW-1:0]   r_awaddr, r_araddr;
    wire [NCH*8-1:0]    r_awlen, r_arlen;
    wire [NCH*3-1:0]    r_awsize, r_arsize;
    wire [NCH*2-1:0]    r_awburst, r_arburst, r_bresp, r_rresp;
    wire [NCH-1:0]      r_awvalid, r_awready, r_wvalid, r_wready, r_wlast;
    wire [NCH-1:0]      r_bvalid, r_bready, r_arvalid, r_arready;
    wire [NCH-1:0]      r_rvalid, r_rready, r_rlast;
    wire [NCH*DW-1:0]   r_wdata, r_rdata;
    wire [NCH*DW/8-1:0] r_wstrb;

    wire [47:0] dbg_cluster;
    wire [31:0] dbg_vcyc, obs;
    wire        dbg_vflt;

    mm_mesh #(.FW(FW), .PW(PW), .DW(DW), .AW(AW), .IDW(IDW), .MEMP(MEMP),
              .UNIT_CDC(UNIT_CDC), .MODEL(1)) dut (
        .clk(clk), .mat_clk(mat_clk), .vec_clk(vec_clk), .rst(rst),
        .sm_awaddr({AW{1'b0}}), .sm_awlen(8'd0), .sm_awvalid(1'b0),
        .sm_wdata({DW{1'b0}}), .sm_wlast(1'b0), .sm_wvalid(1'b0),
        .sc_awaddr(32'd0), .sc_awvalid(1'b0), .sc_awready(),
        .sc_wdata(64'd0), .sc_wvalid(1'b0), .sc_wready(),
        .sc_bvalid(),
        .sc_araddr(32'd0), .sc_arvalid(1'b0),
        .sc_rdata(), .sc_rvalid(),
        .mv_busy(), .mv_fault(), .mv_done(),
        .dram_aclk(clk), .dram_aresetn(!rst),
        .dram_awid(r_awid), .dram_awaddr(r_awaddr), .dram_awlen(r_awlen),
        .dram_awsize(r_awsize), .dram_awburst(r_awburst),
        .dram_awvalid(r_awvalid), .dram_awready(r_awready),
        .dram_wdata(r_wdata), .dram_wstrb(r_wstrb), .dram_wlast(r_wlast),
        .dram_wvalid(r_wvalid), .dram_wready(r_wready),
        .dram_bid(r_bid), .dram_bresp(r_bresp), .dram_bvalid(r_bvalid),
        .dram_bready(r_bready),
        .dram_arid(r_arid), .dram_araddr(r_araddr), .dram_arlen(r_arlen),
        .dram_arsize(r_arsize), .dram_arburst(r_arburst),
        .dram_arvalid(r_arvalid), .dram_arready(r_arready),
        .dram_rid(r_rid), .dram_rdata(r_rdata), .dram_rresp(r_rresp),
        .dram_rlast(r_rlast), .dram_rvalid(r_rvalid), .dram_rready(r_rready),
        .ext_in_data(ext_i), .ext_in_valid(ext_iv), .ext_in_busy(ext_ib),
        .ext_out_data(ext_o), .ext_out_valid(ext_ov), .ext_out_busy(1'b0),
        .dbg_cluster(dbg_cluster), .dbg_vec_cycles(dbg_vcyc),
        .dbg_vec_fault(dbg_vflt), .obs(obs)
    );

    reg           bd_we = 0;
    reg  [15:0]   bd_addr = 0;
    reg  [DW-1:0] bd_wdata = 0;
    wire [DW-1:0] bd_rdata;

    axi_ram #(.DATA_W(DW), .ADDR_W(AW), .ID_W(IDW), .WORDS(2048),
              .PORTS(NCH)) u_ram (
        .clk(clk), .resetn(!rst),
        .s_awid(r_awid), .s_awaddr(r_awaddr), .s_awlen(r_awlen),
        .s_awsize(r_awsize), .s_awburst(r_awburst),
        .s_awvalid(r_awvalid), .s_awready(r_awready),
        .s_wdata(r_wdata), .s_wstrb(r_wstrb), .s_wlast(r_wlast),
        .s_wvalid(r_wvalid), .s_wready(r_wready),
        .s_bid(r_bid), .s_bresp(r_bresp), .s_bvalid(r_bvalid),
        .s_bready(r_bready),
        .s_arid(r_arid), .s_araddr(r_araddr), .s_arlen(r_arlen),
        .s_arsize(r_arsize), .s_arburst(r_arburst),
        .s_arvalid(r_arvalid), .s_arready(r_arready),
        .s_rid(r_rid), .s_rdata(r_rdata), .s_rresp(r_rresp),
        .s_rlast(r_rlast), .s_rvalid(r_rvalid), .s_rready(r_rready),
        .bd_we(bd_we), .bd_addr(bd_addr), .bd_wdata(bd_wdata),
        .bd_rdata(bd_rdata)
    );

    integer errors = 0, checks = 0, spin;

    task chk(input cond, input [255:0] what, input integer where);
        begin
            checks = checks + 1;
            if (!cond) begin
                errors = errors + 1;
                if (errors < 20) $display("  FAIL %0s [%0d]", what, where);
            end
        end
    endtask

    // ================================================ the bench as the agent
    task send_flit(input [3:0] dx, input [3:0] dy, input [3:0] ty,
                   input [7:0] txn, input lst, input [255:0] payload);
        begin
            @(negedge clk);
            while (ext_ib) @(negedge clk);
            ext_i  <= {dx, dy, CX[3:0], CY[3:0], ty, txn, lst, 3'b000, payload};
            ext_iv <= 1'b1;
            @(negedge clk);
            ext_iv <= 1'b0;
        end
    endtask

    // Every CU_INST retires with a signal, so counting what was sent is the
    // only exact wait: a RUN's own completion is not the next signal to arrive.
    integer vec_n = 0;

    task vec_inst(input [255:0] p);
        begin
            vec_n = vec_n + 1;
            send_flit(VX[3:0], VY[3:0], T_CU_INST, 8'h40, 1'b0, p);
        end
    endtask

    task cl_inst(input [255:0] p);
        begin send_flit(LX[3:0], LY[3:0], T_CU_INST, 8'h40, 1'b0, p); end
    endtask
    task put_imem(input [8:0] a, input [31:0] w);
        begin vec_inst({4'd1, a, 211'd0, w}); end
    endtask
    task put_desc(input [2:0] ad, input [2:0] f, input [33:0] v);
        begin vec_inst({4'd2, ad, f, v, 212'd0}); end
    endtask

    // ================================================ what comes back
    // Bucketed by SOURCE, which is what noc_orchestrator does: NODE_STATUS is
    // indexed by {src_y, src_x}, not by where the signal was addressed.
    integer sig_vec = 0, sig_cl = 0, ack_vec = 0;
    integer ack_arg = -1;
    reg [255:0] gold [0:63];
    integer gold_n = 0, desc_n = 0;
    reg         cd_open = 0;
    reg [15:0]  cd_off = 0;
    reg [7:0]   cd_left = 0;

    wire [3:0]  o_type = ext_o[FW-4*PW-1 -: 4];
    wire [3:0]  o_sx   = ext_o[FW-2*PW-1 -: PW];
    wire [3:0]  o_sy   = ext_o[FW-3*PW-1 -: PW];

    task vec_settle(input integer limit);
        begin
            spin = 0;
            while (((sig_vec - ack_vec) < vec_n) && (spin < limit)) begin
                spin = spin + 1;
                @(negedge clk);
            end
            chk(spin < limit, "the vector core retired every instruction", vec_n);
            repeat (400) @(negedge clk);
        end
    endtask

    always @(posedge clk) if (!rst && ext_ov) begin
        if (o_type == T_CU_SIGNAL) begin
            if ((o_sx == VX) && (o_sy == VY)) begin
                sig_vec = sig_vec + 1;
                if (ext_o[255 -: 8] == SIG_DATA_RECEIVED) begin
                    ack_vec = ack_vec + 1;
                    ack_arg = ext_o[247 -: 32];
                end
            end else sig_cl = sig_cl + 1;
        end else if (o_type == T_CU_DATA) begin
            if (!cd_open) begin
                cd_off  <= ext_o[247 -: 16];
                cd_left <= ext_o[231 -: 8];
                cd_open <= 1'b1;
                desc_n   = desc_n + 1;
            end else begin
                gold[cd_off[5:0]] = ext_o[255:0];
                gold_n  = gold_n + 1;
                cd_off  <= cd_off + 16'd1;
                if (cd_left == 8'd0) cd_open <= 1'b0;
                else                 cd_left <= cd_left - 8'd1;
            end
        end
    end

    // ================================================ operands, over CU_DATA
    localparam integer MAXG = 4, MAXNK = 2;
    localparam integer MAXM = MAXG*4, MAXK = MAXNK*32;

    integer signed A [0:MAXM-1][0:MAXK-1];
    integer signed B [0:MAXK-1][0:MAXM-1];
    integer        SA [0:MAXM-1][0:MAXNK-1];
    integer        SB [0:MAXM-1][0:MAXNK-1];
    integer        seed = 7;

    // Word `w` carries K-slice `w`: elements 8w..8w+7 of every lane, in 7-bit
    // fields counting DOWN from bit 255. The transpose is which index runs fast.
    function [255:0] word_a(input integer row0, input integer kb, input integer w);
        integer ii, kk;
        begin
            word_a = 256'd0;
            for (ii = 0; ii < 4; ii = ii + 1)
                for (kk = 0; kk < 8; kk = kk + 1)
                    word_a[255 - (ii*8+kk)*7 -: 7] = A[row0+ii][kb*32 + w*8 + kk][6:0];
        end
    endfunction

    function [255:0] word_b(input integer col0, input integer kb, input integer w);
        integer jj, kk;
        begin
            word_b = 256'd0;
            for (kk = 0; kk < 8; kk = kk + 1)
                for (jj = 0; jj < 4; jj = jj + 1)
                    word_b[255 - (kk*4+jj)*7 -: 7] = B[kb*32 + w*8 + kk][col0+jj][6:0];
        end
    endfunction

    function [255:0] with_scales(input [255:0] w, input integer lane0,
                                 input integer kb, input integer side);
        integer ii;
        begin
            with_scales = w;
            for (ii = 0; ii < 4; ii = ii + 1)
                with_scales[31 - ii*8 -: 8] =
                    ((side == 0 ? SA[lane0+ii][kb] : SB[lane0+ii][kb]) + SBIAS) << 3;
        end
    endfunction

    task fill_side(input integer side, input integer ng, input integer nk);
        integer e, w, ne;
        reg [255:0] p;
        begin
            ne = ng*nk;
            p = 256'd0;
            p[255 -: 8]  = side[7:0];
            p[247 -: 16] = 16'd0;
            p[231 -: 8]  = (ne*4 - 1);
            send_flit(LX[3:0], LY[3:0], T_CU_DATA, 8'h00, 1'b0, p);
            for (e = 0; e < ne; e = e + 1)
                for (w = 0; w < 4; w = w + 1) begin
                    p = (side == 0) ? word_a((e/nk)*4, e % nk, w)
                                    : word_b((e/nk)*4, e % nk, w);
                    if (w == 0) p = with_scales(p, (e/nk)*4, e % nk, side);
                    send_flit(LX[3:0], LY[3:0], T_CU_DATA, 8'h00,
                              (e == ne-1) && (w == 3), p);
                end
        end
    endtask

    task send_gemm(input integer gm, input integer gn, input integer nk);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 4] = 4'd2;
            p[199 -: 8] = gm[7:0];
            p[191 -: 8] = gn[7:0];
            p[183 -: 8] = nk[7:0];
            p[175 -: 8] = ANCHOR;
            cl_inst(p);
        end
    endtask

    // The compiler's encoding, field for field. `addr` is a BYTE address whose
    // [20:5] the CU sends as the descriptor's granule offset, hence `word * 32`.
    task drain_to_node(input integer nt, input [3:0] dx, input [3:0] dy,
                       input integer word, input integer bufid,
                       input integer flags, input [3:0] acky, input [3:0] ackx);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 4]  = 4'd3;
            p[251 -: 34] = word * 32;
            p[217 -: 16] = nt[15:0];
            p[175 -: 8]  = ANCHOR;
            p[111]       = 1'b1;
            p[110 -: 4]  = dx;
            p[106 -: 4]  = dy;
            p[102 -: 8]  = bufid[7:0];
            p[94 -: 8]   = flags[7:0];
            p[86 -: 4]   = acky;
            p[82 -: 4]   = ackx;
            cl_inst(p);
        end
    endtask

    // ================================================ the vector programs
    localparam [31:0] I_VSETI  = 32'hD0000000;
    localparam [31:0] I_VSETVL = 32'hC0000000;
    localparam [31:0] I_VSETMD = 32'hC8000000;
    localparam [31:0] I_VADD   = 32'h18020000;   // v1 = v0 + v0, exact in FP16
    localparam [31:0] I_VHALT  = 32'hF8000000;
    // VLD v0 <- A1, VST v1 -> A2, both FP16; the low bits are the word offset.
    localparam [31:0] I_VLD_A1 = 32'hA1200000;
    localparam [31:0] I_VST_A2 = 32'hA9420000;
    // VDRAIN, ad in [23:21] and the L1 start word in the low bits.
    localparam [31:0] I_VDRAIN_A0 = 32'hF0000000;
    localparam [31:0] I_VDRAIN_A3 = 32'hF0600000;

    integer i, t, nt, gm, gn, nk, pc;
    reg [15:0] got16, want16;
    reg [255:0] line;

    initial begin
        ext_i = 0; ext_iv = 0;
        for (i = 0; i < 2048; i = i + 1) u_ram.mem[i] = 256'd0;
        for (i = 0; i < 64; i = i + 1) gold[i] = 256'd0;

        repeat (10) @(negedge clk);
        rst = 0;
        repeat (10) @(negedge clk);

        gm = 3; gn = 3; nk = 2; nt = gm*gn;

        // Small, at scale zero: section 5 DOUBLES the tile, and at the full int7
        // range the drained value saturates FP16 rather than reaching infinity.
        for (i = 0; i < gm*4; i = i + 1)
            for (t = 0; t < nk*32; t = t + 1) A[i][t] = ($random(seed) & 7) - 4;
        for (t = 0; t < nk*32; t = t + 1)
            for (i = 0; i < gn*4; i = i + 1) B[t][i] = ($random(seed) & 7) - 4;
        for (i = 0; i < gm*4; i = i + 1)
            for (t = 0; t < nk; t = t + 1) SA[i][t] = 0;
        for (i = 0; i < gn*4; i = i + 1)
            for (t = 0; t < nk; t = t + 1) SB[i][t] = 0;

        $display("--- 1. operands into the cluster's L1 as CU_DATA ---");
        fill_side(0, gm, nk);
        fill_side(1, gn, nk);

        // ============ pass A: the golden tile, drained to the bench ============
        $display("--- 2. DRAIN dnode=1 to the agent: the golden sub-tiles ---");
        send_gemm(gm, gn, nk);
        drain_to_node(nt, CX[3:0], CY[3:0], 0, 0, 0, 4'd0, 4'd0);

        spin = 0;
        while ((gold_n < nt) && (spin < 200000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(spin < 200000, "the agent received the whole tile", 0);
        chk(gold_n == nt, "one granule per sub-tile at buf 0", gold_n);
        // buf 0 is OP_EMIT -- one FP16 sub-tile per granule, so `n` sub-tiles
        // are `n` granules and ceil(n/WBURST) descriptors.
        chk(desc_n == (nt + WBURST - 1)/WBURST, "bursts are ceil(n/8)", desc_n);

        // ============ pass B: the same tile into the vector core ============
        $display("--- 3. DRAIN dnode=1 to the vector core at (1,0) ---");
        send_gemm(gm, gn, nk);
        drain_to_node(nt, VX[3:0], VY[3:0], 0, 0, 1, CY[3:0], CX[3:0]);

        spin = 0;
        while ((ack_vec < (nt + WBURST - 1)/WBURST) && (spin < 200000)) begin
            spin = spin + 1; @(negedge clk);
        end
        chk(spin < 200000, "the vector core acknowledged the transfer", 0);
        chk(ack_vec == (nt + WBURST - 1)/WBURST,
            "one DATA_RECEIVED per burst", ack_vec);
        chk(ack_arg == 0, "the ack's arg is the buf_id", ack_arg);
        chk(dbg_vflt === 1'b0, "an inbound peer burst is not a fault", 0);

        $display("--- 4. the vector core writes what it received to DRAM ---");
        // No VFILL anywhere: the operand is already in L1, which is the point
        // and also dodges the VFILL-through-MAG path mm_mesh_tb calls broken.
        put_imem(9'd0, I_VDRAIN_A0);
        put_imem(9'd1, I_VHALT);
        put_desc(3'd0, 3'd0, A_OUT);
        put_desc(3'd0, 3'd1, {18'd32, 16'(nt)});

        vec_inst({4'd3, 9'd0, 243'd0});
        vec_settle(400000);

        for (t = 0; t < nt; t = t + 1) begin
            @(negedge clk); bd_addr = (A_OUT >> 5) + t[15:0];
            @(negedge clk); @(negedge clk);
            chk(bd_rdata === gold[t], "the tile crossed the NoC unchanged", t);
        end

        // ============ an epilogue ON what the NoC delivered ============
        $display("--- 5. an elementwise pass over the delivered tile ---");
        pc = 8;
        put_imem(pc[8:0], I_VSETI);      put_imem(9'(pc+1), 32'd16);
        put_imem(9'(pc+2), I_VSETVL);
        put_imem(9'(pc+3), I_VSETMD);
        for (t = 0; t < nt; t = t + 1) begin
            put_imem(9'(pc + 4 + t*3),     I_VLD_A1 | 32'(t));
            put_imem(9'(pc + 4 + t*3 + 1), I_VADD);
            put_imem(9'(pc + 4 + t*3 + 2), I_VST_A2 | 32'(t));
        end
        put_imem(9'(pc + 4 + nt*3),     I_VDRAIN_A3 | 32'(OUT_W));
        put_imem(9'(pc + 4 + nt*3 + 1), I_VHALT);

        put_desc(3'd1, 3'd0, 34'd0);                    // A1: the delivered tile
        put_desc(3'd1, 3'd1, {18'd1, 16'd1});
        put_desc(3'd2, 3'd0, 34'(OUT_W));               // A2: the result, in L1
        put_desc(3'd2, 3'd1, {18'd1, 16'd1});
        put_desc(3'd3, 3'd0, A_OUT2);                   // A3: the result, in DRAM
        put_desc(3'd3, 3'd1, {18'd32, 16'(nt)});

        vec_inst({4'd3, 9'(pc), 243'd0});
        vec_settle(400000);

        chk(dbg_vflt === 1'b0, "the epilogue did not fault", 0);
        for (t = 0; t < nt; t = t + 1) begin
            @(negedge clk); bd_addr = (A_OUT2 >> 5) + t[15:0];
            @(negedge clk); @(negedge clk);
            line = bd_rdata;
            for (i = 0; i < 16; i = i + 1) begin
                got16  = line[i*16 +: 16];
                want16 = gold[t][i*16 +: 16];
                // x + x in FP16 is the exponent plus one and nothing else, so
                // this is an equality rather than a tolerance.
                if (want16[14:10] != 5'd0)
                    want16 = {want16[15], 5'(want16[14:10] + 5'd1), want16[9:0]};
                chk(got16 === want16, "the epilogue doubled the delivered tile",
                    t*16 + i);
            end
        end

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    initial begin
        #8000000;
        $display("  FAIL -- watchdog (gold=%0d desc=%0d ack=%0d sigv=%0d)",
                 gold_n, desc_n, ack_vec, sig_vec);
        $finish;
    end

endmodule

`default_nettype wire

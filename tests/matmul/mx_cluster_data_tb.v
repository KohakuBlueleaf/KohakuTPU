// CU_DATA into a cluster: another unit writing L1 and the resident tile.
//
// docs/isa/cluster.md s9. Nothing in the machine sends CU_DATA yet, so this
// bench is the sender: it drives flits straight at the CU's NoC local and
// reads the drain's memory writes back off it.
//
// Two things are being tested, and only one of them is arithmetic:
//
//   buf 0/1  operands arrive as CU_DATA instead of as MAG responses, and the
//            GEMM over them must give the same answer. That covers the 928-bit
//            placement permutation, the A/B transpose and the entry/word
//            mapping off `off`.
//   buf 2    a peer sub-tile is added into the resident tile. Checked against
//            an EXACTLY ZERO tile so no float model is needed: zero plus 1.0
//            is 1.0, and FP16 1.0 is 0x3C00 or the datapath is wrong.
//
// The operand model is mx_cluster_node_tb's -- int7 values with a power-of-two
// block scale, graded against the tile's peak because K blocks carry
// independent scales and an element can cancel from partials far larger than
// itself.

`timescale 1ns/1ps
`default_nettype none

module mx_cluster_data_tb;

    localparam integer MODEL  = `MX_MODEL;

    localparam integer FLIT   = 288;
    localparam integer PWID   = 4;
    localparam integer CU_X   = 2, CU_Y = 1;
    localparam integer MEM_X  = 1, MEM_Y = 1;
    // NOT (0,0): that is the "answer the descriptor's source" sentinel, so a
    // bench living there could not tell a redirect to itself from the default.
    localparam integer SRC_X  = 3, SRC_Y = 3;
    localparam integer ACK_X  = 2, ACK_Y = 3;   // a third party, for redirects

    localparam [3:0] T_MEM_WR_REQ  = 4'h1,
                     T_MEM_WR_DATA = 4'h4,
                     T_CU_INST     = 4'h5,
                     T_CU_SIGNAL   = 4'h6,
                     T_CU_DATA     = 4'h8;
    localparam [7:0] SIG_DATA_RECEIVED = 8'h03;
    localparam integer WBURST = 8;

    localparam [7:0] BUF_L1A = 8'd0, BUF_L1B = 8'd1, BUF_PEER = 8'd2;

    localparam integer SBIAS = 20, HEADROOM = 8;
    localparam integer MW    = 14;
    localparam integer PW    = 16*(MW+8);       // one peer sub-tile, 352 bits

    // 1.0 in the accumulator's float: {sign, exp7 + 63, mantissa14}.
    localparam [PW/16-1:0] ACC_ONE = {1'b0, 7'd63, 14'd0};

    localparam integer MAXGM = 4, MAXGN = 4, MAXNK = 4;
    localparam integer MAXM  = MAXGM*4, MAXN = MAXGN*4, MAXK = MAXNK*32;

    reg clk = 0, resetn = 0;
    always #2 clk = ~clk;

    reg  [FLIT-1:0] noc_in_data = 0;
    reg             noc_in_valid = 0;
    wire            noc_in_busy;
    wire [FLIT-1:0] noc_out_data;
    wire            noc_out_valid;

    // GA/GB = 512 and block RAM, the production shape: two banks of 256, which
    // is the only configuration where the bank bit is not optimised away.
    mx_cluster_cu #(
        .FLIT_WIDTH(FLIT), .POS_WIDTH(PWID),
        .CU_X(CU_X), .CU_Y(CU_Y), .MEM_X(MEM_X), .MEM_Y(MEM_Y),
        .TILES(256), .GA(512), .GB(512), .ACC_MW(MW), .MODEL(MODEL),
        .L1_PRIM("block")
    ) dut (
        .clk(clk), .resetn(resetn),
        .noc_in_data(noc_in_data), .noc_in_valid(noc_in_valid),
        .noc_in_busy(noc_in_busy),
        .noc_out_data(noc_out_data), .noc_out_valid(noc_out_valid),
        .noc_out_busy(1'b0),
        .fills_done(), .gemms_done(), .drains_done()
    );

    // ---- the problem ----
    integer signed A  [0:MAXM-1][0:MAXK-1];
    integer signed B  [0:MAXK-1][0:MAXN-1];
    integer        SA [0:MAXM-1][0:MAXNK-1];
    integer        SB [0:MAXN-1][0:MAXNK-1];
    integer        ANCHOR;

    real  fp64_c [0:MAXM-1][0:MAXN-1];
    real  hw_c   [0:MAXM-1][0:MAXN-1];
    reg   got    [0:MAXGM*MAXGN-1];

    integer errors = 0, checks = 0, seed;
    integer cur_gn = 1;

    function real fp16_to_real(input [15:0] f);
        real m; integer e;
        begin
            if (f[14:10] == 5'd0) fp16_to_real = 0.0;
            else begin
                m = 1.0 + $itor(f[9:0]) / 1024.0;
                e = f[14:10] - 15;
                fp16_to_real = m * (2.0 ** e);
                if (f[15]) fp16_to_real = -fp16_to_real;
            end
        end
    endfunction

    // ================================================ the link
    // One flit per cycle, and `noc_in_busy` is tested BEFORE the flit is
    // presented: it only moves on a clock edge, so a level sampled at the
    // negedge still holds at the posedge that takes the flit.
    task noc_send(input [FLIT-1:0] f);
        begin
            @(negedge clk);
            while (noc_in_busy) @(negedge clk);
            noc_in_data  = f;
            noc_in_valid = 1'b1;
            @(negedge clk);
            noc_in_valid = 1'b0;
        end
    endtask

    function [FLIT-1:0] hdr;
        input [3:0] dx, dy, ty;
        input [7:0] txn;
        input       lst;
        input [255:0] payload;
        begin
            hdr = {dx, dy, SRC_X[3:0], SRC_Y[3:0], ty, txn, lst, 3'b000, payload};
        end
    endfunction

    // ---- what comes back out --------------------------------------------
    // The drain's writes. `w_first` is the sub-tile the burst starts at,
    // recovered from the descriptor's address, so the data flits behind it
    // need carry no index of their own.
    reg [33:0]  w_first = 0;
    reg [15:0]  w_seen  = 0;
    // `w_seen` restarts at every descriptor, so it says where in the BURST a
    // flit is and never reaches the drain's total. `w_tot` is what a wait loop
    // must test; using `w_seen` made every multi-burst drain leave by timeout.
    integer     w_tot = 0;
    integer     ci, cj, ct;

    // CU_DATA the cluster SENDS, held rather than looped straight back. A live
    // loopback deadlocks and the deadlock is real, not an artefact: the receive
    // path cannot accumulate a peer sub-tile until the send drain has retired,
    // so holding the flits backpressures the sender that is trying to retire.
    // Store and forward tests the same two halves against each other.
    reg [FLIT-1:0] lb_buf [0:2047];
    integer        lb_n = 0;
    integer        sig_n = 0, sig_arg = -1, sig_dx = -1, sig_dy = -1;

    always @(posedge clk) if (resetn && noc_out_valid) begin
        case (noc_out_data[FLIT-4*PWID-1 -: 4])
        T_MEM_WR_REQ: begin
            w_first <= noc_out_data[255 -: 34] >> 5;
            w_seen  <= 16'd0;
        end
        T_MEM_WR_DATA: begin
            ct = w_first + w_seen;
            got[ct] = 1'b1;
            for (ci = 0; ci < 4; ci = ci + 1)
                for (cj = 0; cj < 4; cj = cj + 1)
                    hw_c[(ct / cur_gn)*4 + ci][(ct % cur_gn)*4 + cj]
                        = fp16_to_real(noc_out_data[(ci*4+cj)*16 +: 16]);
            w_seen <= w_seen + 16'd1;
            w_tot = w_tot + 1;
        end
        T_CU_DATA: begin
            lb_buf[lb_n] = noc_out_data;
            lb_n = lb_n + 1;
        end
        T_CU_SIGNAL: if (noc_out_data[255 -: 8] == SIG_DATA_RECEIVED) begin
            sig_n   = sig_n + 1;
            sig_arg = noc_out_data[247 -: 32];
            sig_dx  = noc_out_data[FLIT-1 -: PWID];
            sig_dy  = noc_out_data[FLIT-PWID-1 -: PWID];
        end
        default: ;
        endcase
    end

    // ================================================ instruction encoders
    task send_gemm(input integer gm, input integer gn, input integer nk,
                   input integer acc, input integer bank);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 4]   = 4'd2;                 // OP_GEMM
            p[200]        = acc[0];
            p[114]        = bank[0];              // abank
            p[113]        = bank[0];              // bbank
            p[199 -: 8]   = gm[7:0];
            p[191 -: 8]   = gn[7:0];
            p[183 -: 8]   = nk[7:0];
            p[175 -: 8]   = ANCHOR[7:0];
            noc_send(hdr(CU_X[3:0], CU_Y[3:0], T_CU_INST, 8'h40, 1'b0, p));
        end
    endtask

    task send_drain(input integer nt);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 4]   = 4'd3;                 // OP_DRAIN
            p[251 -: 34]  = 34'd0;                // destination base
            p[217 -: 16]  = nt[15:0];
            p[175 -: 8]   = ANCHOR[7:0];
            noc_send(hdr(CU_X[3:0], CU_Y[3:0], T_CU_INST, 8'h40, 1'b0, p));
        end
    endtask

    // A DRAIN addressed to a NoC node instead of to memory. `addr` keeps its
    // meaning -- a byte address -- and the CU sends addr[20:5] as the
    // descriptor's granule offset, so zero here means granule zero.
    task send_drain_node(input integer nt, input integer bufid,
                         input integer flags, input integer acky,
                         input integer ackx);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 4]  = 4'd3;                  // OP_DRAIN
            p[251 -: 34] = 34'd0;
            p[217 -: 16] = nt[15:0];
            p[175 -: 8]  = ANCHOR[7:0];
            p[111]       = 1'b1;                  // dnode
            p[110 -: 4]  = CU_X[3:0];             // back to this very cluster
            p[106 -: 4]  = CU_Y[3:0];
            p[102 -: 8]  = bufid[7:0];
            p[94 -: 8]   = flags[7:0];
            p[86 -: 4]   = acky[3:0];
            p[82 -: 4]   = ackx[3:0];
            noc_send(hdr(CU_X[3:0], CU_Y[3:0], T_CU_INST, 8'h40, 1'b0, p));
        end
    endtask

    task send_desc(input [7:0] bufid, input [15:0] off, input [7:0] len);
        begin
            send_desc_ack(bufid, off, len, 8'd0, 4'd0, 4'd0);
        end
    endtask

    // `ackx`/`acky` zero means "answer the descriptor's source", which is the
    // bench at (SRC_X, SRC_Y).
    task send_desc_ack(input [7:0] bufid, input [15:0] off, input [7:0] len,
                       input [7:0] flags, input [3:0] acky, input [3:0] ackx);
        reg [255:0] p;
        begin
            p = 256'd0;
            p[255 -: 8]  = bufid;
            p[247 -: 16] = off;
            p[231 -: 8]  = len;
            p[223 -: 8]  = flags;
            p[215 -: 4]  = acky;
            p[211 -: 4]  = ackx;
            noc_send(hdr(CU_X[3:0], CU_Y[3:0], T_CU_DATA, 8'h00, 1'b0, p));
        end
    endtask

    task send_data(input [255:0] p, input lst);
        begin
            noc_send(hdr(CU_X[3:0], CU_Y[3:0], T_CU_DATA, 8'h00, lst, p));
        end
    endtask

    // ---- an L1 entry as the four words MAG would have returned -----------
    // Word `w` carries K-slice `w`: elements 8w..8w+7 of every lane, packed in
    // 7-bit fields counting DOWN from bit 255. The two sides differ only in
    // which index runs fastest, which is where the transpose lives.
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

    // The block scales ride in word 0's low 32 bits, one E5M3 per lane.
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

    // ================================================ operand streams
    // One stream per side: `gm*nk` entries of 4 words each, `off` counting the
    // 32-byte granules through them, so the whole side is a single run.
    // `bank` needs no field of its own: `off` counts granules through the whole
    // 512-entry buffer, so the upper bank simply starts at granule 256*4.
    task fill_side(input integer side, input integer ng, input integer nk,
                   input integer bank);
        integer e, w, ne;
        reg [255:0] p;
        begin
            ne = ng*nk;
            send_desc(side[7:0], bank*1024, (ne*4 - 1));
            for (e = 0; e < ne; e = e + 1)
                for (w = 0; w < 4; w = w + 1) begin
                    // entry e is lane group e/nk at K block e%nk -- the same
                    // `g*nk + kb` the sweep reads with aoff/boff at zero
                    p = (side == 0) ? word_a((e/nk)*4, e % nk, w)
                                    : word_b((e/nk)*4, e % nk, w);
                    if (w == 0) p = with_scales(p, (e/nk)*4, e % nk, side);
                    send_data(p, (e == ne-1) && (w == 3));
                end
        end
    endtask

    // ================================================ one operand case
    task run_case(input integer gm, input integer gn, input integer nk);
        integer m, n, kk, nt, ii, jj, kkk, bb, tt;
        integer signed acc;
        real want, peak, err, tol;
        begin
            m = gm*4; n = gn*4; kk = nk*32; nt = gm*gn;
            cur_gn = gn;
            for (tt = 0; tt < nt; tt = tt + 1) got[tt] = 1'b0;
            w_tot = 0;

            for (ii = 0; ii < m; ii = ii + 1)
                for (kkk = 0; kkk < kk; kkk = kkk + 1)
                    A[ii][kkk] = ($random(seed) & 127) - 64;
            for (kkk = 0; kkk < kk; kkk = kkk + 1)
                for (jj = 0; jj < n; jj = jj + 1)
                    B[kkk][jj] = ($random(seed) & 127) - 64;
            for (ii = 0; ii < m; ii = ii + 1)
                for (bb = 0; bb < nk; bb = bb + 1) SA[ii][bb] = ($random(seed) & 3);
            for (jj = 0; jj < n; jj = jj + 1)
                for (bb = 0; bb < nk; bb = bb + 1) SB[jj][bb] = ($random(seed) & 3);

            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    fp64_c[ii][jj] = 0.0;
                    for (bb = 0; bb < nk; bb = bb + 1) begin
                        acc = 0;
                        for (kkk = 0; kkk < 32; kkk = kkk + 1)
                            acc = acc + A[ii][bb*32+kkk]*B[bb*32+kkk][jj];
                        fp64_c[ii][jj] = fp64_c[ii][jj]
                            + $itor(acc) * (2.0 ** (SA[ii][bb] + SB[jj][bb]));
                    end
                    fp64_c[ii][jj] = fp64_c[ii][jj] * (2.0 ** -HEADROOM);
                end

            fill_side(0, gm, nk, 0);
            fill_side(1, gn, nk, 0);
            send_gemm(gm, gn, nk, 0, 0);
            send_drain(nt);

            tt = 0;
            while ((w_tot < nt) && tt < 200000) begin
                @(posedge clk); tt = tt + 1;
            end
            repeat (20) @(posedge clk);

            for (tt = 0; tt < nt; tt = tt + 1) begin
                checks = checks + 1;
                if (!got[tt]) begin
                    errors = errors + 1;
                    if (errors <= 8)
                        $display("  FAIL gm=%0d gn=%0d nk=%0d sub-tile %0d never written",
                                 gm, gn, nk, tt);
                end
            end

            peak = 0.0;
            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    want = fp64_c[ii][jj];
                    if (want < 0.0) want = -want;
                    if (want > peak) peak = want;
                end
            if (peak == 0.0) peak = 1.0;

            tol = 4.0/1024.0 * $itor(nk);
            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    want = fp64_c[ii][jj];
                    checks = checks + 1;
                    err = (hw_c[ii][jj] - want) / peak;
                    if (err < 0.0) err = -err;
                    if (err > tol) begin
                        errors = errors + 1;
                        if (errors <= 8)
                            $display("  FAIL gm=%0d gn=%0d nk=%0d C[%0d][%0d] got %0f want %0f err/peak %0e",
                                     gm, gn, nk, ii, jj, hw_c[ii][jj], want, err);
                    end
                end

            $display("    gm=%0d gn=%0d nk=%0d  %0d sub-tiles over K=%0d  C[0][0] hw %0f fp64 %0f",
                     gm, gn, nk, nt, kk, hw_c[0][0], fp64_c[0][0]);
        end
    endtask

    // ================================================ the two banks
    // Bank 0 gets ZEROS and bank 1 gets the real operands, so one model covers
    // both directions of the bug. A bank bit lost on the READ path sweeps
    // bank 0 and every element comes back zero; lost on the WRITE path, the
    // operands land in bank 0 and the second sweep is not zero. Neither
    // failure is subtle, which is the point of testing it this way rather than
    // against two models that could both be wrong.
    task run_banks(input integer gm, input integer gn, input integer nk);
        integer m, n, kk, nt, ii, jj, kkk, bb, tt;
        integer signed acc;
        real want, peak, err, tol;
        begin
            m = gm*4; n = gn*4; kk = nk*32; nt = gm*gn;
            cur_gn = gn;

            for (ii = 0; ii < m; ii = ii + 1)
                for (kkk = 0; kkk < kk; kkk = kkk + 1) A[ii][kkk] = 0;
            for (kkk = 0; kkk < kk; kkk = kkk + 1)
                for (jj = 0; jj < n; jj = jj + 1) B[kkk][jj] = 0;
            for (ii = 0; ii < m; ii = ii + 1)
                for (bb = 0; bb < nk; bb = bb + 1) SA[ii][bb] = 0;
            for (jj = 0; jj < n; jj = jj + 1)
                for (bb = 0; bb < nk; bb = bb + 1) SB[jj][bb] = 0;
            fill_side(0, gm, nk, 0);
            fill_side(1, gn, nk, 0);

            for (ii = 0; ii < m; ii = ii + 1)
                for (kkk = 0; kkk < kk; kkk = kkk + 1)
                    A[ii][kkk] = ($random(seed) & 127) - 64;
            for (kkk = 0; kkk < kk; kkk = kkk + 1)
                for (jj = 0; jj < n; jj = jj + 1)
                    B[kkk][jj] = ($random(seed) & 127) - 64;
            for (ii = 0; ii < m; ii = ii + 1)
                for (bb = 0; bb < nk; bb = bb + 1) SA[ii][bb] = ($random(seed) & 3);
            for (jj = 0; jj < n; jj = jj + 1)
                for (bb = 0; bb < nk; bb = bb + 1) SB[jj][bb] = ($random(seed) & 3);
            fill_side(0, gm, nk, 1);
            fill_side(1, gn, nk, 1);

            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    fp64_c[ii][jj] = 0.0;
                    for (bb = 0; bb < nk; bb = bb + 1) begin
                        acc = 0;
                        for (kkk = 0; kkk < 32; kkk = kkk + 1)
                            acc = acc + A[ii][bb*32+kkk]*B[bb*32+kkk][jj];
                        fp64_c[ii][jj] = fp64_c[ii][jj]
                            + $itor(acc) * (2.0 ** (SA[ii][bb] + SB[jj][bb]));
                    end
                    fp64_c[ii][jj] = fp64_c[ii][jj] * (2.0 ** -HEADROOM);
                end

            // ---- sweep the UPPER bank: must be the operands, not the zeros
            for (tt = 0; tt < nt; tt = tt + 1) got[tt] = 1'b0;
            w_tot = 0;
            send_gemm(gm, gn, nk, 0, 1);
            send_drain(nt);
            tt = 0;
            while ((w_tot < nt) && tt < 200000) begin
                @(posedge clk); tt = tt + 1;
            end
            repeat (20) @(posedge clk);

            peak = 0.0;
            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    want = fp64_c[ii][jj];
                    if (want < 0.0) want = -want;
                    if (want > peak) peak = want;
                end
            if (peak == 0.0) peak = 1.0;
            tol = 4.0/1024.0 * $itor(nk);
            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    checks = checks + 1;
                    err = (hw_c[ii][jj] - fp64_c[ii][jj]) / peak;
                    if (err < 0.0) err = -err;
                    if (err > tol) begin
                        errors = errors + 1;
                        if (errors <= 8)
                            $display("  FAIL bank 1 C[%0d][%0d] got %0f want %0f",
                                     ii, jj, hw_c[ii][jj], fp64_c[ii][jj]);
                    end
                end

            // ---- sweep the LOWER bank: must still be the zeros
            for (tt = 0; tt < nt; tt = tt + 1) got[tt] = 1'b0;
            w_tot = 0;
            send_gemm(gm, gn, nk, 0, 0);
            send_drain(nt);
            tt = 0;
            while ((w_tot < nt) && tt < 200000) begin
                @(posedge clk); tt = tt + 1;
            end
            repeat (20) @(posedge clk);

            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    checks = checks + 1;
                    if (hw_c[ii][jj] != 0.0) begin
                        errors = errors + 1;
                        if (errors <= 8)
                            $display("  FAIL bank 0 C[%0d][%0d] got %0f want 0.0 -- a fill reached the wrong bank",
                                     ii, jj, hw_c[ii][jj]);
                    end
                end

            $display("    banks %0dx%0d nk=%0d: upper C[0][0] %0f, lower stayed zero",
                     gm, gn, nk, fp64_c[0][0]);
        end
    endtask

    // ================================================ the peer case
    // A tile of EXACT zeros, then 1.0 added into every lane of it. No float
    // model: FP16 1.0 is 0x3C00, and anything else means the peer sub-tile did
    // not reach the accumulator, or reached the wrong address.
    task run_peer(input integer gm, input integer gn);
        integer ii, jj, kkk, bb, tt, nt, e;
        reg [PW-1:0] sub;
        begin
            nt = gm*gn;
            cur_gn = gn;
            for (tt = 0; tt < nt; tt = tt + 1) got[tt] = 1'b0;
            w_tot = 0;

            for (ii = 0; ii < gm*4; ii = ii + 1)
                for (kkk = 0; kkk < 32; kkk = kkk + 1) A[ii][kkk] = 0;
            for (kkk = 0; kkk < 32; kkk = kkk + 1)
                for (jj = 0; jj < gn*4; jj = jj + 1) B[kkk][jj] = 0;
            for (ii = 0; ii < gm*4; ii = ii + 1) SA[ii][0] = 0;
            for (jj = 0; jj < gn*4; jj = jj + 1) SB[jj][0] = 0;

            fill_side(0, gm, 1, 0);
            fill_side(1, gn, 1, 0);
            send_gemm(gm, gn, 1, 0, 0);

            for (ii = 0; ii < 16; ii = ii + 1) sub[ii*(PW/16) +: (PW/16)] = ACC_ONE;
            send_desc(BUF_PEER, 16'd0, (nt*2 - 1));
            for (e = 0; e < nt; e = e + 1) begin
                send_data(sub[255:0], 1'b0);
                send_data({{(512-PW){1'b0}}, sub[PW-1:256]}, (e == nt-1));
            end

            send_drain(nt);
            tt = 0;
            while ((w_tot < nt) && tt < 200000) begin
                @(posedge clk); tt = tt + 1;
            end
            repeat (20) @(posedge clk);

            for (tt = 0; tt < nt; tt = tt + 1) begin
                checks = checks + 1;
                if (!got[tt]) begin
                    errors = errors + 1;
                    $display("  FAIL peer sub-tile %0d never written", tt);
                end
            end
            for (ii = 0; ii < gm*4; ii = ii + 1)
                for (jj = 0; jj < gn*4; jj = jj + 1) begin
                    checks = checks + 1;
                    if (hw_c[ii][jj] != 1.0) begin
                        errors = errors + 1;
                        if (errors <= 8)
                            $display("  FAIL peer C[%0d][%0d] got %0f want 1.0",
                                     ii, jj, hw_c[ii][jj]);
                    end
                end
            $display("    peer %0dx%0d: zero tile + 1.0 -> %0f", gm, gn, hw_c[0][0]);
        end
    endtask

    // ================================================ cu -> cu, round trip
    // The cluster sends its resident tile to ITSELF as CU_DATA `buf` 2 and the
    // bench hands it back. OP_ADD_PEER then adds the tile to itself, so the
    // answer must be exactly 2T -- and 2T needs no model of the accumulator's
    // float, only the tile the ordinary path already produced.
    //
    // That is the whole interconnect in one check: OP_SEND, the two-granule
    // split, the descriptor build, the receive demux, OP_ADD_PEER, and the
    // completion signal. A mismatch anywhere shows up as a wrong factor.
    task run_peer_loop(input integer gm, input integer gn);
        integer m, n, nt, ii, jj, kkk, bb, tt, li, ngr, nb;
        integer signed acc;
        real want, peak, err, tol;
        begin
            m = gm*4; n = gn*4; nt = gm*gn;
            cur_gn = gn;
            lb_n = 0; sig_n = 0; sig_arg = -1;

            for (ii = 0; ii < m; ii = ii + 1)
                for (kkk = 0; kkk < 32; kkk = kkk + 1)
                    A[ii][kkk] = ($random(seed) & 127) - 64;
            for (kkk = 0; kkk < 32; kkk = kkk + 1)
                for (jj = 0; jj < n; jj = jj + 1)
                    B[kkk][jj] = ($random(seed) & 127) - 64;
            for (ii = 0; ii < m; ii = ii + 1) SA[ii][0] = ($random(seed) & 3);
            for (jj = 0; jj < n; jj = jj + 1) SB[jj][0] = ($random(seed) & 3);

            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    acc = 0;
                    for (kkk = 0; kkk < 32; kkk = kkk + 1)
                        acc = acc + A[ii][kkk]*B[kkk][jj];
                    fp64_c[ii][jj] = $itor(acc)
                        * (2.0 ** (SA[ii][0] + SB[jj][0] - HEADROOM));
                end

            fill_side(0, gm, 1, 0);
            fill_side(1, gn, 1, 0);
            send_gemm(gm, gn, 1, 0, 0);

            // ---- OP_SEND out. Two granules a sub-tile, WBURST a descriptor.
            ngr = 2*nt;
            nb  = (ngr + WBURST - 1) / WBURST;
            // flags[0] asks for a report, and the ack is REDIRECTED to the
            // bench. Left at 0 it would go to the descriptor's source, which is
            // this same cluster -- the case that makes a cu->cu completion
            // unobservable by anything that could sequence on it.
            send_drain_node(nt, BUF_PEER, 1, SRC_Y, SRC_X);
            tt = 0;
            while ((lb_n < ngr + nb) && tt < 200000) begin
                @(posedge clk); tt = tt + 1;
            end
            checks = checks + 1;
            if (lb_n != ngr + nb) begin
                errors = errors + 1;
                $display("  FAIL peer loop %0dx%0d: sent %0d flits, wanted %0d descriptors + %0d granules",
                         gm, gn, lb_n, nb, ngr);
            end

            // ---- and back in
            for (li = 0; li < lb_n; li = li + 1) noc_send(lb_buf[li]);
            tt = 0;
            while ((sig_n < nb) && tt < 200000) begin
                @(posedge clk); tt = tt + 1;
            end
            checks = checks + 2;
            if (sig_n != nb) begin
                errors = errors + 1;
                $display("  FAIL peer loop %0dx%0d: %0d DATA_RECEIVED signals, wanted %0d",
                         gm, gn, sig_n, nb);
            end
            if (sig_arg != BUF_PEER) begin
                errors = errors + 1;
                $display("  FAIL DATA_RECEIVED arg is %0d, wanted the buf id %0d",
                         sig_arg, BUF_PEER);
            end
            checks = checks + 1;
            if ((sig_dx != SRC_X) || (sig_dy != SRC_Y)) begin
                errors = errors + 1;
                $display("  FAIL DATA_RECEIVED went to (%0d,%0d), wanted the redirect (%0d,%0d)",
                         sig_dx, sig_dy, SRC_X, SRC_Y);
            end

            // ---- the tile must now be exactly twice what it was
            for (tt = 0; tt < nt; tt = tt + 1) got[tt] = 1'b0;
            w_tot = 0;
            send_drain(nt);
            tt = 0;
            while ((w_tot < nt) && tt < 200000) begin
                @(posedge clk); tt = tt + 1;
            end
            repeat (20) @(posedge clk);

            peak = 0.0;
            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    want = 2.0*fp64_c[ii][jj];
                    if (want < 0.0) want = -want;
                    if (want > peak) peak = want;
                end
            if (peak == 0.0) peak = 1.0;
            tol = 4.0/1024.0;
            for (ii = 0; ii < m; ii = ii + 1)
                for (jj = 0; jj < n; jj = jj + 1) begin
                    checks = checks + 1;
                    err = (hw_c[ii][jj] - 2.0*fp64_c[ii][jj]) / peak;
                    if (err < 0.0) err = -err;
                    if (err > tol) begin
                        errors = errors + 1;
                        if (errors <= 8)
                            $display("  FAIL peer loop C[%0d][%0d] got %0f want %0f (2x %0f)",
                                     ii, jj, hw_c[ii][jj], 2.0*fp64_c[ii][jj],
                                     fp64_c[ii][jj]);
                    end
                end
            $display("    cu->cu %0dx%0d: %0d flits round trip, %0d signals, C[0][0] %0f = 2 x %0f",
                     gm, gn, lb_n, sig_n, hw_c[0][0], fp64_c[0][0]);
        end
    endtask

    // ================================================ where the ack goes
    // One burst per mode, four flits each. The payload is irrelevant -- only
    // which node the completion is addressed to.
    task run_ack(input integer bufid, input integer acky, input integer ackx,
                 input integer want_y, input integer want_x);
        integer f, tt;
        begin
            sig_n = 0; sig_arg = -1; sig_dx = -1; sig_dy = -1;
            send_desc_ack(bufid[7:0], 16'd0, 8'd3, 8'd1, acky[3:0], ackx[3:0]);
            for (f = 0; f < 4; f = f + 1) send_data(256'd0, (f == 3));
            tt = 0;
            while ((sig_n < 1) && tt < 20000) begin @(posedge clk); tt = tt + 1; end

            checks = checks + 3;
            if (sig_n != 1) begin
                errors = errors + 1;
                $display("  FAIL ack buf %0d: %0d signals, wanted 1", bufid, sig_n);
            end
            if ((sig_dx != want_x) || (sig_dy != want_y)) begin
                errors = errors + 1;
                $display("  FAIL ack buf %0d went to (%0d,%0d), wanted (%0d,%0d)",
                         bufid, sig_dx, sig_dy, want_x, want_y);
            end
            if (sig_arg != bufid) begin
                errors = errors + 1;
                $display("  FAIL ack buf %0d carried arg %0d", bufid, sig_arg);
            end
            $display("    buf %0d, ack field (%0d,%0d) -> signal to (%0d,%0d)",
                     bufid, ackx, acky, sig_dx, sig_dy);
        end
    endtask

    integer err_before;

    initial begin
        seed = 32'h5EEDF00D;
        ANCHOR = 2*SBIAS + HEADROOM;
        for (err_before = 0; err_before < MAXGM*MAXGN; err_before = err_before + 1)
            got[err_before] = 1'b0;

        #20; @(negedge clk); resetn = 1; @(negedge clk);

        $display("--- operands delivered as CU_DATA, not as memory responses ---");
        err_before = errors;
        run_case(1, 1, 1);
        run_case(2, 2, 1);
        run_case(3, 3, 2);      // wide enough to overlap sweeps; two K blocks
        run_case(4, 2, 2);      // a drain longer than one WBURST
        if (errors == err_before) $display("    L1 over CU_DATA matches the model");

        $display("--- L1 as two banks of 256 ---");
        err_before = errors;
        run_banks(2, 2, 1);
        run_banks(3, 3, 2);
        if (errors == err_before) $display("    the banks are independent");

        $display("--- a peer sub-tile added into the resident tile ---");
        err_before = errors;
        run_peer(2, 2);
        run_peer(3, 3);
        if (errors == err_before) $display("    OP_ADD_PEER reaches every sub-tile");

        $display("--- signal_on_complete: where the ack is addressed ---");
        err_before = errors;
        run_ack(BUF_L1A, 0, 0, SRC_Y, SRC_X);            // 0 = the sender
        run_ack(BUF_L1B, ACK_Y, ACK_X, ACK_Y, ACK_X);    // redirected
        if (errors == err_before) $display("    0 answers the sender, non-zero redirects");

        $display("--- cu -> cu: the cluster's own tile, out and back ---");
        err_before = errors;
        run_peer_loop(2, 2);        // one burst
        run_peer_loop(3, 3);        // 18 granules: three, and a short tail
        if (errors == err_before) $display("    OP_SEND round trips to OP_ADD_PEER");

        $display("========================================");
        if (errors == 0) $display("  PASS -- %0d checks, 0 errors  (MODEL=%0d)", checks, MODEL);
        else             $display("  FAIL -- %0d checks, %0d errors  (MODEL=%0d)", checks, errors, MODEL);
        $finish;
    end

    initial begin
        #4000000;
        $display("  FAIL -- watchdog (w_seen=%0d)", w_seen);
        $finish;
    end

endmodule

`default_nettype wire

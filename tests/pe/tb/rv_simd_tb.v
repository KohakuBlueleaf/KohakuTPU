// rv_simd_tb -- the DSP workload suite on the real memory substrate.
//
// The same vehicle as rv_sys_tb (one PE, real routers, the real MAG, an AXI
// RAM), asking a different question. rv_sys_tb asks whether the PE is correct;
// this asks what a kernel COSTS, and it is where every denominator in the
// specialization-frontier measurement comes from.
//
// TWO THINGS ARE DIFFERENT FROM rv_sys_tb, and both follow from that.
//
// The halt word is NOT checked, because it is not an answer -- it is the
// kernel's cycle count, read by the program from CTL_CYCLE either side of its
// kernel. The golden model returns 0 for CTL_CYCLE and so cannot predict it.
// Correctness therefore rides entirely on the DRAM comparison: each program
// computes a rotate-xor checksum over its own results and stores that one word,
// and a single wrong element changes it. That check is strict here and a
// mismatch dumps the differing words, because a suite whose numbers are right
// and whose answers are wrong is worse than no suite.
//
// And each row carries the KERNEL'S DYNAMIC INSTRUCTION COUNT, which
// tests/pe/tools/rv_simd_gen.py takes from the model by counting retirements
// between the kern_start and kern_end labels. Cycles alone cannot say whether a
// kernel is slow because it executes many instructions or because it stalls,
// and the frontier is an argument about instructions removed.
//
// Run it with:
//     python tests/pe/tools/rv_simd_run.py

`default_nettype none
`timescale 1ns/1ps

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif

`ifndef RV_L1_LINES
 `define RV_L1_LINES 128
`endif
`ifndef RV_BTB
 `define RV_BTB 32
`endif
`ifndef RV_FWD_X
 `define RV_FWD_X 1
`endif
// The DSP extension is ON here: the scalar baselines and the vector kernels
// must be measured on ONE configuration, or the ratio between them is not a
// speedup. A scalar program runs unchanged on the extended core.
`ifndef RV_SIMD_EN
 `define RV_SIMD_EN 1
`endif
`ifndef RV_SIMD_LANES
 `define RV_SIMD_LANES 8
`endif
// COMPUTE WIDTHS: 0 is not built. Defaults are the full-width machine.
`ifndef RV_SIMD_ILANES
 `define RV_SIMD_ILANES 8
`endif
`ifndef RV_SIMD_SHIFTU
 `define RV_SIMD_SHIFTU 8
`endif
`ifndef RV_SIMD_PERMU
 `define RV_SIMD_PERMU 8
`endif
`ifndef RV_SIMD_WB
 `define RV_SIMD_WB 0
`endif

module rv_simd_tb;
    localparam integer FW  = 288;
    localparam integer PW  = 4;
    localparam integer DW  = 256;
    localparam integer AW  = 40;
    localparam integer IW  = 2048;
    localparam integer SW  = 2048;
    localparam integer DWORDS = 4096;
    localparam integer RAM_DEPTH = 512;         // 256-bit entries = DWORDS/8
    localparam integer MAXCASE = 32;
    localparam integer META_N = 8;

    localparam [PW-1:0] PE_X = 4'd1, PE_Y = 4'd1;
    localparam [7:0] BUF_IMEM = 8'd1;

    reg clk = 1'b0, rstn = 1'b0;
    always begin
        #2 clk = ~clk;
    end

    integer errors = 0, checks = 0;
    task chk(input [63:0] got, input [63:0] want, input [255:0] what);
        begin
            checks = checks + 1;
            if (got !== want) begin
                $display("  FAIL %0s: got %h want %h", what, got, want);
                errors = errors + 1;
            end
        end
    endtask

    wire [FW-1:0] ag_out, ag_in;
    wire          ag_out_valid, ag_out_busy, ag_in_valid, ag_in_busy;
    wire [3:0]    pe_run, pe_halted, pe_busy;

`ifdef RV_RF_BRAM
    localparam RF_PRIM = "block";
`else
    localparam RF_PRIM = "distributed";
`endif

    rv_mesh #(.FW(FW), .PW(PW), .DW(DW), .AW(AW), .NPE(1),
              .IMEM_WORDS(IW), .SPAD_WORDS(SW),
              .L1_LINES(`RV_L1_LINES), .BTB_ENTRIES(`RV_BTB),
              .FWD_X(`RV_FWD_X), .REGFILE_PRIM(RF_PRIM),
              .RAM_DEPTH(RAM_DEPTH),
              .SIMD_EN(`RV_SIMD_EN), .SIMD_LANES(`RV_SIMD_LANES),
              .SIMD_ILANES(`RV_SIMD_ILANES),
              .SIMD_SHIFT_UNITS(`RV_SIMD_SHIFTU),
              .SIMD_PERM_UNITS(`RV_SIMD_PERMU),
              .SIMD_WB(`RV_SIMD_WB)) dut (
        .clk(clk), .rstn(rstn),
        .ext_in_data(ag_out), .ext_in_valid(ag_out_valid),
        .ext_in_busy(ag_out_busy),
        .ext_out_data(ag_in), .ext_out_valid(ag_in_valid),
        .ext_out_busy(ag_in_busy),
        .pe_run(pe_run), .pe_halted(pe_halted), .pe_busy(pe_busy)
    );

    rv_agent #(.FW(FW), .PW(PW), .AX(1), .AY(0), .IMG_WORDS(IW)) u_ag (
        .clk(clk), .resetn(rstn),
        .out_data(ag_out), .out_valid(ag_out_valid), .out_busy(ag_out_busy),
        .in_data(ag_in), .in_valid(ag_in_valid), .in_busy(ag_in_busy)
    );

    // The PE's own counters, which is what CTL_CYCLE and CTL_INSTRET report.
    // Read here rather than through a program store, so a kernel does not have
    // to spend instructions describing itself.
    wire [31:0] pe_cycles  = dut.g_pe[0].g_have.u_pe.cyc_ctr;
    wire [31:0] pe_retired = dut.g_pe[0].g_have.u_pe.ret_ctr;

    // ---- golden data --------------------------------------------------------
    reg [31:0] dinit [0:DWORDS-1];
    reg [31:0] dfin  [0:DWORDS-1];
    reg [31:0] meta  [0:META_N-1];
    reg [31:0] nc    [0:0];

    integer ncase, c, i, k, ok;
    integer nprog;
    reg [31:0] dsum;
    string fn;
    integer sig_before;

    task dram_write_init;
        begin
            for (i = 0; i < RAM_DEPTH; i = i + 1) begin
                for (k = 0; k < 8; k = k + 1) begin
                    dut.u_ram.mem[i][k*32 +: 32] = dinit[i * 8 + k];
                end
            end
        end
    endtask

    task dram_checksum(output reg [31:0] s);
        begin
            s = 32'd0;
            for (i = 0; i < RAM_DEPTH; i = i + 1) begin
                for (k = 0; k < 8; k = k + 1) begin
                    s = s + dut.u_ram.mem[i][k*32 +: 32] * (i * 8 + k + 1);
                end
            end
        end
    endtask

    function [31:0] dram_word;
        input integer wi;
        dram_word = dut.u_ram.mem[wi / 8][(wi % 8) * 32 +: 32];
    endfunction

    task reset_mesh;
        begin
            rstn = 1'b0;
            repeat (12) @(posedge clk);
            rstn = 1'b1;
            repeat (8) @(posedge clk);
        end
    endtask

    task run_case(input integer n);
        begin
            for (i = 0; i < DWORDS; i = i + 1) begin
                dinit[i] = 32'd0;
            end
            for (i = 0; i < DWORDS; i = i + 1) begin
                dfin[i]  = 32'd0;
            end
            for (i = 0; i < META_N; i = i + 1) begin
                meta[i]  = 32'd0;
            end
            for (i = 0; i < IW; i = i + 1) begin
                u_ag.img[i]  = 32'd0;
            end

            fn = $sformatf("%s/simd/simd%02d/prog.hex", `PE_DIR, n);
            $readmemh(fn, u_ag.img);
            fn = $sformatf("%s/simd/simd%02d/dfin.hex", `PE_DIR, n);
            $readmemh(fn, dfin);
            fn = $sformatf("%s/simd/simd%02d/meta.hex", `PE_DIR, n);
            $readmemh(fn, meta);
            nprog = meta[4];

            reset_mesh;
            dram_write_init;
            u_ag.load_image(PE_X, PE_Y, BUF_IMEM, nprog, 0);

            sig_before = u_ag.sig_n;
            u_ag.kick(PE_X, PE_Y, 8'hD0 + n[7:0], 1'b0, 8'd1, 32'd0, 32'd0);
            u_ag.wait_signal(sig_before + 1, 2000000, ok);

            chk(ok, 1, "the program retired");
            if (!ok) begin
                $display("  @@@ DSP %0d cycles 0 kinstr %0d retired 0 NORETIRE",
                         n, meta[3]);
            end else begin
                chk(u_ag.sig_code, 8'h00, "completion code");
                dram_checksum(dsum);
                chk(dsum, meta[2], "DRAM after the program");
                if (dsum !== meta[2]) begin
                    k = 0;
                    for (i = 0; i < DWORDS; i = i + 1) begin
                        if ((dram_word(i) !== dfin[i]) && (k < 8)) begin
                            $display("      dram word %0d: rtl %08x model %08x",
                                     i, dram_word(i), dfin[i]);
                            k = k + 1;
                        end
                    end
                end
                // sig_arg IS the kernel cycle count -- see the header.
                $display("  @@@ DSP %0d cycles %0d kinstr %0d retired %0d total %0d %0s",
                         n, u_ag.sig_arg, meta[3], meta[1], pe_cycles,
                         (dsum === meta[2]) ? "ok" : "WRONG");
            end
        end
    endtask

    // ---- bench-driven: a tile delivered into the vector window by the NoC ----
    // The only exercise of buf_id 2, and the path a mesh really uses. A program
    // cannot write that window this way, so nothing else covers the window
    // writer's third target, and the model cannot predict the answer because
    // the data arrives while the program is stopped.
    localparam [7:0] BUF_VSPAD = 8'd2;
    localparam integer VN_VEC  = 16;
    localparam integer VN_SIMD = `RV_SIMD_LANES;

    integer vn_i, vn_k;
    reg [31:0] vn_lane [0:7];
    reg [31:0] vn_want;

    task run_vspad_noc;
        begin
            for (i = 0; i < DWORDS; i = i + 1) begin
                dinit[i] = 32'd0;
            end
            for (i = 0; i < IW; i = i + 1) begin
                u_ag.img[i] = 32'd0;
            end
            fn = $sformatf("%s/simd/ix00/prog.hex", `PE_DIR);
            $readmemh(fn, u_ag.img);
            nprog = 0;
            for (i = 0; i < IW; i = i + 1) begin
                if (u_ag.img[i] !== 32'd0) begin
                    nprog = i + 1;
                end
            end

            reset_mesh;
            dram_write_init;
            u_ag.load_image(PE_X, PE_Y, BUF_IMEM, nprog + 8, 0);

            for (i = 0; i < IW; i = i + 1) begin
                u_ag.img[i] = 32'd0;
            end
            for (i = 0; i < VN_VEC * VN_SIMD; i = i + 1) begin
                u_ag.img[i] = (32'h1000_0001 * (i + 1)) ^ (i << 7);
            end
            u_ag.load_image(PE_X, PE_Y, BUF_VSPAD, VN_VEC * VN_SIMD, 0);

            // What the program will compute: xor down each lane, then sum them.
            for (vn_k = 0; vn_k < VN_SIMD; vn_k = vn_k + 1) begin
                vn_lane[vn_k] = 32'd0;
            end
            for (vn_i = 0; vn_i < VN_VEC; vn_i = vn_i + 1) begin
                for (vn_k = 0; vn_k < VN_SIMD; vn_k = vn_k + 1) begin
                    vn_lane[vn_k] = vn_lane[vn_k]
                                  ^ u_ag.img[vn_i * VN_SIMD + vn_k];
                end
            end
            vn_want = 32'd0;
            for (vn_k = 0; vn_k < VN_SIMD; vn_k = vn_k + 1) begin
                vn_want = vn_want + vn_lane[vn_k];
            end

            sig_before = u_ag.sig_n;
            u_ag.kick(PE_X, PE_Y, 8'hE0, 1'b0, 8'd1, 32'd0, 32'd0);
            u_ag.wait_signal(sig_before + 1, 400000, ok);
            chk(ok, 1, "the NoC-delivered tile program retired");
            if (ok) begin
                chk(u_ag.sig_code, 8'h00, "completion code");
                chk(u_ag.sig_arg, vn_want,
                    "the tile the NoC wrote into the vector window");
            end
        end
    endtask

    initial begin
        nc[0] = 32'hxxxx_xxxx;
        $readmemh({`PE_DIR, "/simd/ndsp.hex"}, nc);
        ncase = nc[0];
        if ((^nc[0] === 1'bx) || (ncase < 1) || (ncase > MAXCASE)) begin
            $display("  FAIL no DSP cases at %s/simd -- run python tests/pe/tools/rv_simd_gen.py",
                     `PE_DIR);
            $display("========================================");
            $display("  FAIL -- 0 checks, 1 errors");
            $display("========================================");
            $finish;
        end

        $display("--- %0d DSP workload cases, L1 %0d, BTB %0d, FWD_X %0d ---",
                 ncase, `RV_L1_LINES, `RV_BTB, `RV_FWD_X);
        for (c = 0; c < ncase; c = c + 1) begin
            run_case(c);
        end

        $display("--- bench-driven: a tile the NoC wrote into the vector window ---");
        run_vspad_noc;

        $display("========================================");
        if (checks == 0) begin
            $display("  FAIL -- the bench made no checks");
        end
        else if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors",
                     checks, errors);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #400000000;
        $display("  FAIL WATCHDOG -- the DSP workload bench never finished");
        $display("========================================");
        $finish;
    end

endmodule

`default_nettype wire

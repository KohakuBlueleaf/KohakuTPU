// khd_unit_tb -- the DSP datapath against the golden model, on its own.
//
// Level 1 for the vector extension, and the place a wrong saturation bound, a
// slide that reads the wrong lane, or an accumulator that lands a cycle early
// is supposed to fail. No core, no memory agent, no program: instruction words
// and the state they produce.
//
// IT READS THE STATE BACK THROUGH THE ISA, not through the hierarchy. The
// vector file is dumped with `vextr`, the accumulators with `vaccrd` then
// `vextr`, and the scratchpad with `vld` then `vextr` -- so the comparison uses
// only paths software has, and no part of it depends on what an XPM array
// happens to call its internal memory this tool version. The dump order
// matters and is the one thing to preserve: the vector file goes first,
// because everything after it borrows v0.
//
// Each case is replayed with `x_valid` held HIGH across consecutive
// instructions, so the back-to-back dependencies the stall rules exist for are
// actually exercised. A bench that dropped valid between instructions would
// verify a machine nobody builds.
//
// Run it with:
//     python tests/pe/tools/khd_run.py

`default_nettype none
`timescale 1ns/1ps

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif
`ifndef KHD_SIMD
 `define KHD_SIMD 8
`endif
`ifndef KHD_MULS
 `define KHD_MULS 4
`endif
`ifndef KHD_SHIFT
 `define KHD_SHIFT 1
`endif
`ifndef KHD_PERM
 `define KHD_PERM 1
`endif
`ifndef KHD_WB
 `define KHD_WB 0
`endif
`ifndef KHD_VREGS
 `define KHD_VREGS 8
`endif
`ifndef KHD_NACC
 `define KHD_NACC 2
`endif
`ifndef KHD_F16
 `define KHD_F16 0
`endif
// Rotating partials per float accumulator. ARCHITECTURAL: the golden model
// rotates by the same number, and a build that changed it would compute
// different answers on the same program.
`ifndef KHD_NPART
 `define KHD_NPART 16
`endif
`ifndef MX_MODEL
 `define MX_MODEL 1
`endif

module khd_unit_tb;
    localparam integer SIMD    = `KHD_SIMD;
    localparam integer VREGS   = `KHD_VREGS;
    localparam integer NACC    = `KHD_NACC;
    localparam integer ENTRIES = 64;
    localparam integer VW      = 32 * SIMD;
    localparam integer VWORDS  = ENTRIES * SIMD;
    localparam integer NWA     = $clog2(ENTRIES * SIMD);
    localparam integer MAXI    = 4096;      // instructions per case
    localparam integer MAXS    = 256;       // scalar results per case
    localparam integer MAXW    = 4096;      // vector-file writes per case
    localparam integer MAXC    = 12;
    localparam integer WW      = 32 + 8 + 32 * `KHD_SIMD;

`include "khd_isa.vh"

    reg clk = 1'b0, resetn = 1'b0;
    always #2 clk = ~clk;

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

    reg         x_valid = 1'b0;
    reg  [31:0] x_instr = 32'd0, x_addr = 32'd0, x_xdata = 32'd0;
    wire        stall, x_illegal, x_misalign;
    wire        w_sc_valid;
    wire [31:0] w_sc;

    reg            noc_en = 1'b0;
    reg  [3:0]     noc_we = 4'd0;
    reg  [NWA-1:0] noc_word = {NWA{1'b0}};
    reg  [31:0]    noc_wdata = 32'd0;

    wire            wr_valid;
    wire [4:0]      wr_vd;
    wire [VW-1:0]   wr_data;

    // A VALUE-LESS define, following rv_run.py's RV_RF_BRAM: xvlog.bat splits
    // `-d NAME=VALUE` at the `=`, so a quoted string arrives as a bare
    // identifier and elaboration fails on it.
`ifdef KHD_RF_BRAM
    localparam VREG_PRIM = "block";
`else
    localparam VREG_PRIM = "distributed";
`endif

    khd_unit #(.SIMD(SIMD), .VREGS(VREGS), .NACC(NACC),
               .VSPAD_ENTRIES(ENTRIES), .MULS(`KHD_MULS),
               .HAS_SHIFT(`KHD_SHIFT), .HAS_PERM(`KHD_PERM),
               .HAS_F16(`KHD_F16), .NPART(`KHD_NPART),
               .F16_MODEL(`MX_MODEL),
               .WB_STAGE(`KHD_WB),
               .USE_DSP("yes"), .MEM_PRIM("block"),
               .VREG_PRIM(VREG_PRIM)) dut (
        .clk(clk), .resetn(resetn),
        .x_valid(x_valid), .x_instr(x_instr), .x_addr(x_addr),
        .x_xdata(x_xdata), .x_hold(1'b0),
        .stall(stall), .x_illegal(x_illegal), .x_misalign(x_misalign),
        .w_sc_valid(w_sc_valid), .w_sc(w_sc),
        .dbg_wr_valid(wr_valid), .dbg_wr_vd(wr_vd), .dbg_wr_data(wr_data),
        .noc_en(noc_en), .noc_we(noc_we), .noc_word(noc_word),
        .noc_wdata(noc_wdata),
        // The scalar store port belongs to the core, which is not here. Tied
        // off rather than left unconnected: an undriven input is `z`, and it
        // reaches the scratchpad's write enables.
        .sc_st_valid(1'b0), .sc_st_addr(32'd0), .sc_st_be(4'd0),
        .sc_st_data(32'd0)
    );

    // ---- golden data --------------------------------------------------------
    reg [95:0] prog   [0:MAXC*MAXI-1];
    reg [31:0] scal   [0:MAXC*MAXS-1];
    reg [31:0] vfin   [0:MAXC*VREGS*8-1];
    reg [31:0] afin   [0:MAXC*NACC*8-1];
    reg [31:0] spinit [0:MAXC*VWORDS-1];
    reg [31:0] spfin  [0:MAXC*VWORDS-1];
    reg [31:0] meta   [0:7];
    reg [31:0] icount [0:MAXC-1];
    reg [31:0] scount [0:MAXC-1];

    integer ncase, stride, sstride, wstride, c, i, k, v, a;

    // ---- scalar results, captured live -------------------------------------
    reg [31:0] got_sc [0:4095];
    integer    nsc;
    always @(posedge clk) if (resetn && w_sc_valid) begin
        got_sc[nsc] = w_sc;
        nsc = nsc + 1;
    end

    // ---- the writeback probe, compared live --------------------------------
    // The whole reason this exists: a wrong lane fails on the instruction that
    // produced it, with its index and mnemonic, rather than as one word of a
    // state dump after four hundred instructions.
    reg [WW-1:0] wtrace [0:MAXC*MAXW-1];
    reg [31:0]   wcount [0:MAXC-1];
    integer      nwr, wbase, wbad;
    reg          warmed;

    always @(posedge clk) if (resetn && warmed && wr_valid) begin
        if (nwr >= wcount[c]) begin
            if (wbad == 0)
                $display("  FAIL case%0d: the unit wrote the vector file %0d times, the model %0d",
                         c, nwr + 1, wcount[c]);
            wbad = wbad + 1;
        end else begin
            if ({wr_vd, wr_data} !== wtrace[wbase + nwr][VW+7:0]) begin
                if (wbad == 0) begin
                    $display("  FAIL case%0d write %0d (instruction %0d): v%0d = %h",
                             c, nwr, wtrace[wbase + nwr][WW-1 -: 32],
                             wr_vd, wr_data);
                    $display("        the model says              v%0d = %h",
                             wtrace[wbase + nwr][VW+7 -: 8],
                             wtrace[wbase + nwr][VW-1:0]);
                end
                wbad = wbad + 1;
            end
        end
        nwr = nwr + 1;
    end

    // ---- driving ------------------------------------------------------------
    task issue(input [31:0] ins, input [31:0] addr, input [31:0] xd);
        begin
            x_instr <= ins;
            x_addr  <= addr;
            x_xdata <= xd;
            x_valid <= 1'b1;
            @(negedge clk);
            while (stall) @(negedge clk);
            @(posedge clk);
        end
    endtask

    // DROPPING VALID DOES NOT END A FOLD. `vfaccrd` holds MEM for
    // NPART*(ALAT+1) cycles and writes the vector file on the last one, so a
    // case ending in one loses its final write unless the drain is waited for.
    task quiet;
        begin
            x_valid <= 1'b0;
            @(negedge clk);
            while (stall) @(negedge clk);
            repeat (8) @(posedge clk);
        end
    endtask

    // The three dump encodings, built from the GENERATED header rather than
    // from numbers written here twice.
    function [31:0] enc_vextr;
        input [4:0] vs1;
        input [4:0] lane;
        enc_vextr = {KHD_MOV_EXTR, lane, vs1, KHD_F3_VMOV, 5'd0, KHD_OPCODE};
    endfunction

    function [31:0] enc_vaccrd;
        input [4:0] vd;
        input [4:0] acc;
        enc_vaccrd = {KHD_MAC_ACCRD, KHD_ET_S8, 5'd0, acc, KHD_F3_VMAC, vd,
                      KHD_OPCODE};
    endfunction

    function [31:0] enc_vld;
        input [4:0] vd;
        enc_vld = {12'd0, 5'd0, KHD_F3_VLD, vd, KHD_OPCODE};
    endfunction

    localparam [31:0] VSPAD_BASE = 32'h4000_0000;

    task reset_unit;
        begin
            resetn = 1'b0;
            x_valid = 1'b0;
            repeat (6) @(posedge clk);
            resetn = 1'b1;
            repeat (4) @(posedge clk);
        end
    endtask

    task load_vspad(input integer cs);
        begin
            @(negedge clk);
            for (i = 0; i < VWORDS; i = i + 1) begin
                noc_en    <= 1'b1;
                noc_we    <= 4'hF;
                noc_word  <= i[NWA-1:0];
                noc_wdata <= spinit[cs * VWORDS + i];
                @(negedge clk);
            end
            noc_en <= 1'b0;
            noc_we <= 4'd0;
            @(posedge clk);
        end
    endtask

    integer base;
    task run_case(input integer cs);
        begin
            reset_unit;
            load_vspad(cs);

            // ---- phase 1: the program, with the writeback probe live ----
            nsc = 0;
            nwr = 0;
            wbad = 0;
            wbase = cs * wstride;
            warmed = 1'b1;
            for (i = 0; i < icount[cs]; i = i + 1) begin
                base = cs * stride + i;
                issue(prog[base][95:64], prog[base][63:32], prog[base][31:0]);
            end
            quiet;
            warmed = 1'b0;
            checks = checks + 1;
            if (wbad) errors = errors + 1;
            chk(nwr, wcount[cs], "vector-file write count");
            chk(nsc, scount[cs], "scalar result count");
            for (i = 0; i < scount[cs]; i = i + 1)
                if (got_sc[i] !== scal[cs * sstride + i]) begin
                    chk(got_sc[i], scal[cs * sstride + i], "scalar result");
                    i = scount[cs];             // one report is enough
                end

            // ---- phase 2: the vector file, through vextr ----
            nsc = 0;
            for (v = 0; v < VREGS; v = v + 1)
                for (k = 0; k < SIMD; k = k + 1)
                    issue(enc_vextr(v[4:0], k[4:0]), 32'd0, 32'd0);
            quiet;
            chk(nsc, VREGS * SIMD, "vector-file dump length");
            // The generator writes SIMD entries per register, so the stride is
            // SIMD and not 8: a fixed 8 reads past the block at SIMD 2 and 4
            // and compares against nothing, which reads as `want xxxxxxxx`.
            for (i = 0; i < VREGS * SIMD; i = i + 1) begin
                v = i / SIMD;
                k = i % SIMD;
                if (got_sc[i] !== vfin[cs * VREGS * SIMD + v * SIMD + k]) begin
                    $display("  FAIL v%0d lane %0d: got %08x want %08x",
                             v, k, got_sc[i],
                             vfin[cs * VREGS * SIMD + v * SIMD + k]);
                    errors = errors + 1;
                    checks = checks + 1;
                    i = VREGS * SIMD;
                end
            end

            // ---- phase 3: the accumulators, through vaccrd then vextr ----
            nsc = 0;
            for (a = 0; a < NACC; a = a + 1) begin
                issue(enc_vaccrd(5'd0, a[4:0]), 32'd0, 32'd0);
                for (k = 0; k < SIMD; k = k + 1)
                    issue(enc_vextr(5'd0, k[4:0]), 32'd0, 32'd0);
            end
            quiet;
            for (i = 0; i < NACC * SIMD; i = i + 1) begin
                a = i / SIMD;
                k = i % SIMD;
                if (got_sc[i] !== afin[cs * NACC * SIMD + a * SIMD + k]) begin
                    $display("  FAIL acc%0d lane %0d: got %08x want %08x",
                             a, k, got_sc[i],
                             afin[cs * NACC * SIMD + a * SIMD + k]);
                    errors = errors + 1;
                    checks = checks + 1;
                    i = NACC * SIMD;
                end
            end

            // ---- phase 4: the scratchpad, through vld then vextr ----
            nsc = 0;
            for (i = 0; i < ENTRIES; i = i + 1) begin
                issue(enc_vld(5'd0), VSPAD_BASE + i * (SIMD * 4), 32'd0);
                for (k = 0; k < SIMD; k = k + 1)
                    issue(enc_vextr(5'd0, k[4:0]), 32'd0, 32'd0);
            end
            quiet;
            chk(nsc, VWORDS, "scratchpad dump length");
            for (i = 0; i < VWORDS; i = i + 1)
                if (got_sc[i] !== spfin[cs * VWORDS + i]) begin
                    $display("  FAIL vspad word %0d: got %08x want %08x",
                             i, got_sc[i], spfin[cs * VWORDS + i]);
                    errors = errors + 1;
                    checks = checks + 1;
                    i = VWORDS;
                end

            chk(errors === errors, 1'b1, "case completed");
        end
    endtask

    string fn;
    initial begin
        meta[0] = 32'hxxxx_xxxx;
        $readmemh({`PE_DIR, "/khd/cur/meta.hex"}, meta);
        if ((^meta[0] === 1'bx) || (meta[0] < 1) || (meta[0] > MAXC)) begin
            $display("  FAIL no vectors at %s/khd/cur -- run python tests/pe/tools/khd_gen.py",
                     `PE_DIR);
            $display("========================================");
            $display("  FAIL -- 0 checks, 1 errors");
            $display("========================================");
            $finish;
        end
        ncase   = meta[0];
        stride  = meta[1];
        sstride = meta[2];
        wstride = meta[7];
        warmed  = 1'b0;
        // A CONFIGURATION IS VERIFIED AS ITSELF, which means the vectors and
        // the build have to agree about what was built. Vectors for a wider
        // datapath, or for a build that has the permute unit when this one does
        // not, would otherwise report a plausible failure about the wrong
        // machine.
        if ((meta[6] != SIMD) || (meta[8] != `KHD_MULS)
            || (meta[9] != `KHD_SHIFT) || (meta[10] != `KHD_PERM)
            || (meta[11] != `KHD_F16)
            || (meta[3] != VREGS) || (meta[4] != NACC)) begin
            $display("  FAIL vectors are SIMD %0d MULS %0d shift %0d perm %0d f16 %0d vregs %0d nacc %0d; the bench is SIMD %0d MULS %0d shift %0d perm %0d f16 %0d vregs %0d nacc %0d",
                     meta[6], meta[8], meta[9], meta[10], meta[11], meta[3],
                     meta[4], SIMD, `KHD_MULS, `KHD_SHIFT, `KHD_PERM,
                     `KHD_F16, VREGS, NACC);
            $display("========================================");
            $display("  FAIL -- 0 checks, 1 errors");
            $display("========================================");
            $finish;
        end

        $readmemh({`PE_DIR, "/khd/cur/prog.hex"},    prog);
        $readmemh({`PE_DIR, "/khd/cur/scal.hex"},    scal);
        $readmemh({`PE_DIR, "/khd/cur/vfin.hex"},    vfin);
        $readmemh({`PE_DIR, "/khd/cur/afin.hex"},    afin);
        $readmemh({`PE_DIR, "/khd/cur/spinit.hex"},  spinit);
        $readmemh({`PE_DIR, "/khd/cur/spfin.hex"},   spfin);
        $readmemh({`PE_DIR, "/khd/cur/counts.hex"},  icount);
        $readmemh({`PE_DIR, "/khd/cur/scounts.hex"}, scount);
        $readmemh({`PE_DIR, "/khd/cur/wtrace.hex"},  wtrace);
        $readmemh({`PE_DIR, "/khd/cur/wcounts.hex"}, wcount);

        $display("--- %0d cases, SIMD %0d, VREGS %0d, NACC %0d, MULS %0d, shift %0d, perm %0d, f16 %0d ---",
                 ncase, SIMD, VREGS, NACC, `KHD_MULS, `KHD_SHIFT, `KHD_PERM,
                 `KHD_F16);
        for (c = 0; c < ncase; c = c + 1) begin
            run_case(c);
            $display("    case%0d  %0d instructions  %s",
                     c, icount[c], (errors == 0) ? "ok" : "see failures above");
        end

        $display("========================================");
        if (checks == 0)      $display("  FAIL -- the bench made no checks");
        else if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else                  $display("  FAIL -- %0d checks, %0d errors",
                                       checks, errors);
        $display("========================================");
        $finish;
    end

    // A configuration refusing an encoding the vectors contain is a generator
    // and bench that disagree about what was built, and it must be loud.
    always @(posedge clk) if (resetn && x_valid && (x_illegal || x_misalign))
        $display("%0t ERROR khd_unit_tb: the unit refused %08h (illegal %b misaligned %b)",
                 $time, x_instr, x_illegal, x_misalign);

    initial begin
        #500000000;
        $display("  FAIL WATCHDOG -- the DSP datapath bench never finished");
        $display("========================================");
        $finish;
    end

endmodule

`default_nettype wire

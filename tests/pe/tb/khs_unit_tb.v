// khs_unit_tb -- the SIMD datapath against the golden model, on its own.
//
// Level 1 for the vector extension, and the place a wrong saturation bound, a
// slide that reads the wrong lane, or an accumulator that lands a cycle early
// is supposed to fail. No core, no memory agent, no program: instruction words
// and the state they produce.
//
// IT READS THE STATE BACK THROUGH THE ISA, not through the hierarchy. The
// vector file is dumped with `vextr`, the scratchpad with `vld` then
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
//     python tests/pe/tools/khs_run.py

`default_nettype none
`timescale 1ns/1ps

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif
`ifndef KHS_SIMD
 `define KHS_SIMD 8
`endif
// Defaults to what SHIPS, so a bare bench run tests the built machine.
// KHS_WB=0 still elaborates and must still pass -- the answers are identical
// either way and only the latency contract differs.
`ifndef KHS_WB
 `define KHS_WB 1
`endif
`ifndef KHS_VREGS
 `define KHS_VREGS 8
`endif
// Banks in the FLOAT accumulator; there is no integer one.
`ifndef KHS_NACC
 `define KHS_NACC 2
`endif
`ifndef KHS_FLOAT
 `define KHS_FLOAT 0
`endif
// Permute OUTPUT words per pass. NOT architectural: the answer is the same at
// every count, only the cycles change, so one stream grades every width.
`ifndef KHS_PERMU
 `define KHS_PERMU 8
`endif
// int32 <-> float converter units per pass. 0 = not built and f2i/i2f fault;
// f2f needs no unit and is built whenever both memory formats are.
`ifndef KHS_FCVTU
 `define KHS_FCVTU 0
`endif
// The float GROUPS, each its own switch so a run can isolate one. They default
// on so that `-d KHS_FLOAT=1` alone still builds the whole float side.
`ifndef KHS_FALU
 `define KHS_FALU 1
`endif
`ifndef KHS_FSFU
 `define KHS_FSFU 1
`endif
`ifndef KHS_FACC
 `define KHS_FACC 1
`endif
// The memory FORMATS. NOT architectural: an element's answer does not depend on
// which other formats the build carries, so one stream grades either.
// Packed-shift units per pass. NOT architectural: same answer at every width.
`ifndef KHS_SHIFTU
 `define KHS_SHIFTU 8
`endif
// Integer ALU units per pass. NOT architectural, for the same reason the shift
// and permute widths are not: the answer is the same, only the cycles change.
`ifndef KHS_ILANES
 `define KHS_ILANES 8
`endif
// The reduce tree and the rounding-shift adder. Both ARCHITECTURAL -- off, the
// encodings they serve fault -- so a stream using them must not set these to 0.
`ifndef KHS_RED
 `define KHS_RED 1
`endif
`ifndef KHS_SHROUND
 `define KHS_SHROUND 1
`endif
// Rotating partials per float accumulator. ARCHITECTURAL: the golden model
// rotates by the same number, and a build that changed it would compute
// different answers on the same program.
`ifndef KHS_NPART
 `define KHS_NPART 16
`endif
// Float LANES against the vector's element count. ARCHITECTURAL: with fewer
// lanes an element's chain is NPART/PASSES partials, so the accumulation ORDER
// and therefore the answer changes -- the generator must be told the same
// number. 0 IS NOT BUILT; the maximum is SIMD.
`ifndef KHS_FLANES
 `define KHS_FLANES `KHS_SIMD
`endif

module khs_unit_tb;
    localparam integer SIMD    = `KHS_SIMD;
    localparam integer VREGS   = `KHS_VREGS;
    localparam integer NACC    = `KHS_NACC;
    localparam integer ENTRIES = 64;
    localparam integer VW      = 32 * SIMD;
    localparam integer VWORDS  = ENTRIES * SIMD;
    localparam integer NWA     = $clog2(ENTRIES * SIMD);
    localparam integer MAXI    = 4096;      // instructions per case
    localparam integer MAXS    = 256;       // scalar results per case
    localparam integer MAXW    = 4096;      // vector-file writes per case
    localparam integer MAXC    = 16;
    localparam integer WW      = 32 + 8 + 32 * `KHS_SIMD;
    // khs_unit's own FLANES, recomputed here so `KHS_FSFU_ALL` can ask for a
    // seed unit per float unit without the tb knowing the resolution rule twice.
    localparam integer FLANES_TB = `KHS_FLANES;

`include "khs_isa.vh"

    reg clk = 1'b0, resetn = 1'b0;
    always begin
        #2 clk = ~clk;
    end

    integer errors = 0, checks = 0;

    // A WIDTH THAT COSTS NO CYCLES IS NOT WALKING, and its passing checks mean
    // nothing. Phase 1 only: the fixed vextr dump would dilute the number.
    reg [31:0] cyc = 32'd0;
    always @(posedge clk) begin
        cyc <= cyc + 32'd1;
    end
    integer cyc0;
    integer cyc_used [0:MAXC-1];
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
`ifdef KHS_RF_BRAM
    localparam VREG_PRIM = "block";
`else
    localparam VREG_PRIM = "distributed";
`endif

    khs_unit #(.SIMD(SIMD), .VREGS(VREGS), .NACC(NACC),
               .VSPAD_ENTRIES(ENTRIES),
               .SHIFT_UNITS(`KHS_SHIFTU),
               .ILANES(`KHS_ILANES),
               .RED_UNITS(`KHS_RED), .HAS_SHROUND(`KHS_SHROUND),
               .PERM_UNITS(`KHS_PERMU),
               .FLOAT_LANES(`KHS_FLOAT ? `KHS_FLANES : 0),
               // The groups the generator was told to emit, and no others: a
               // bench carrying a group the stream avoids leaves it untested,
               // and one lacking a group the stream uses faults instead of
               // measuring. FCVT is off in both, matching khs_gen's NOT_BUILT.
               .HAS_FALU(`KHS_FLOAT && `KHS_FALU),
               // A UNIT COUNT NOW, not a boolean: `KHS_FSFU` is how many seed
               // units to build, and 1 means ONE -- not "every lane", which is
               // what the old boolean meant. `KHS_FSFU_ALL` restores that.
`ifdef KHS_FSFU_ALL
               .FSFU_UNITS(`KHS_FLOAT ? FLANES_TB : 0),
`else
               .FSFU_UNITS(`KHS_FLOAT ? `KHS_FSFU : 0),
`endif
               .HAS_FACC(`KHS_FLOAT && `KHS_FACC), .FCVT_UNITS(`KHS_FCVTU),
               .NPART(`KHS_NPART),
               .WB_STAGE(`KHS_WB),
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
    reg [31:0] spinit [0:MAXC*VWORDS-1];
    reg [31:0] spfin  [0:MAXC*VWORDS-1];
    // TWELVE, NOT EIGHT. The configuration guard below reads meta[8..11] --
    // MULS, shift, perm and float. Out of range they read X, `if (X)` is false,
    // and the guard silently never fires: a float=1 vector set ran against a
    // float=0 build and reported "the unit refused <opcode>" instead of naming
    // the mismatch it exists to name.
    // THIRTEEN NOW: meta[12] is the float LANE count, which is architectural --
    // it changes the accumulation order and therefore the answers, so vectors
    // for one lane count against a build with another produce a plausible
    // arithmetic failure instead of naming the mismatch.
    reg [31:0] meta   [0:12];
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
            if (wbad == 0) begin
                $display("  FAIL case%0d: the unit wrote the vector file %0d times, the model %0d",
                         c, nwr + 1, wcount[c]);
            end
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
            while (stall) begin
                @(negedge clk);
            end
            @(posedge clk);
        end
    endtask

    // DROPPING VALID DOES NOT END A FOLD. `vfaccrd` holds MEM for
    // NPART*(ALAT+1) cycles and writes the vector file on the last one, so a
    // case ending in one loses its final write unless the drain is waited for.
    // WAIT FOR THE WRITE PORT TO GO QUIET, not a fixed count. The elementwise
    // float pipe is ~15 cycles deep and nothing holds `stall` while it drains,
    // so the old `repeat (8)` dropped the last float write of any case that did
    // not happen to end on an instruction depending on it -- invisible at
    // ILANES=8, where a `vadd.s32` always did, and four lost writes at 0.
    task quiet;
        integer qz;
        begin
            x_valid <= 1'b0;
            @(negedge clk);
            while (stall) begin
                @(negedge clk);
            end
            qz = 0;
            while (qz < 24) begin
                @(posedge clk);
                qz = wr_valid ? 0 : (qz + 1);
            end
        end
    endtask

    // The dump encodings, built from the GENERATED header rather than from
    // numbers written here twice.
    function [31:0] enc_vextr;
        input [4:0] vs1;
        input [4:0] lane;
        enc_vextr = {KHS_MOV_EXTR, lane, vs1, KHS_F3_VMOV, 5'd0, KHS_OPCODE};
    endfunction

    function [31:0] enc_vld;
        input [4:0] vd;
        enc_vld = {12'd0, 5'd0, KHS_F3_VLD, vd, KHS_OPCODE};
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
            cyc0 = cyc;
            for (i = 0; i < icount[cs]; i = i + 1) begin
                base = cs * stride + i;
                issue(prog[base][95:64], prog[base][63:32], prog[base][31:0]);
            end
            quiet;
            cyc_used[cs] = cyc - cyc0;
            warmed = 1'b0;
            checks = checks + 1;
            if (wbad) begin
                errors = errors + 1;
            end
            chk(nwr, wcount[cs], "vector-file write count");
            chk(nsc, scount[cs], "scalar result count");
            for (i = 0; i < scount[cs]; i = i + 1) begin
                if (got_sc[i] !== scal[cs * sstride + i]) begin
                    chk(got_sc[i], scal[cs * sstride + i], "scalar result");
                    i = scount[cs];             // one report is enough
                end
            end

            // ---- phase 2: the vector file, through vextr ----
            nsc = 0;
            for (v = 0; v < VREGS; v = v + 1) begin
                for (k = 0; k < SIMD; k = k + 1) begin
                    issue(enc_vextr(v[4:0], k[4:0]), 32'd0, 32'd0);
                end
            end
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

            // ---- phase 3: the scratchpad, through vld then vextr ----
            nsc = 0;
            for (i = 0; i < ENTRIES; i = i + 1) begin
                issue(enc_vld(5'd0), VSPAD_BASE + i * (SIMD * 4), 32'd0);
                for (k = 0; k < SIMD; k = k + 1) begin
                    issue(enc_vextr(5'd0, k[4:0]), 32'd0, 32'd0);
                end
            end
            quiet;
            chk(nsc, VWORDS, "scratchpad dump length");
            for (i = 0; i < VWORDS; i = i + 1) begin
                if (got_sc[i] !== spfin[cs * VWORDS + i]) begin
                    $display("  FAIL vspad word %0d: got %08x want %08x",
                             i, got_sc[i], spfin[cs * VWORDS + i]);
                    errors = errors + 1;
                    checks = checks + 1;
                    i = VWORDS;
                end
            end

            chk(errors === errors, 1'b1, "case completed");
        end
    endtask

    string fn;
    initial begin
        meta[0] = 32'hxxxx_xxxx;
        $readmemh({`PE_DIR, "/khd/cur/meta.hex"}, meta);
        if ((^meta[0] === 1'bx) || (meta[0] < 1) || (meta[0] > MAXC)) begin
            $display("  FAIL no vectors at %s/khd/cur -- run python tests/pe/tools/khs_gen.py",
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
        // EVERY WIDTH IS CHECKED, not only the ones that existed when this was
        // written: a stream built for a machine with the shifter against a
        // build without it reports a plausible failure about the wrong machine.
        if ((meta[6] != SIMD) || (meta[8] != `KHS_ILANES)
            || (meta[9] != `KHS_SHIFTU) || (meta[10] != `KHS_PERMU)
            || (meta[11] != `KHS_FLOAT) || (meta[12] != `KHS_FLANES)
            || (meta[13] != `KHS_RED)
            || (meta[3] != VREGS) || (meta[4] != NACC)) begin
            $display("  FAIL vectors are SIMD %0d ilanes %0d shiftu %0d permu %0d float %0d flanes %0d red %0d vregs %0d nacc %0d; the bench is SIMD %0d ilanes %0d shiftu %0d permu %0d float %0d flanes %0d red %0d vregs %0d nacc %0d",
                     meta[6], meta[8], meta[9], meta[10], meta[11], meta[12],
                     meta[13], meta[3], meta[4],
                     SIMD, `KHS_ILANES, `KHS_SHIFTU, `KHS_PERMU,
                     `KHS_FLOAT, `KHS_FLANES, `KHS_RED, VREGS, NACC);
            $display("========================================");
            $display("  FAIL -- 0 checks, 1 errors");
            $display("========================================");
            $finish;
        end

        $readmemh({`PE_DIR, "/khd/cur/prog.hex"},    prog);
        $readmemh({`PE_DIR, "/khd/cur/scal.hex"},    scal);
        $readmemh({`PE_DIR, "/khd/cur/vfin.hex"},    vfin);
        $readmemh({`PE_DIR, "/khd/cur/spinit.hex"},  spinit);
        $readmemh({`PE_DIR, "/khd/cur/spfin.hex"},   spfin);
        $readmemh({`PE_DIR, "/khd/cur/counts.hex"},  icount);
        $readmemh({`PE_DIR, "/khd/cur/scounts.hex"}, scount);
        $readmemh({`PE_DIR, "/khd/cur/wtrace.hex"},  wtrace);
        $readmemh({`PE_DIR, "/khd/cur/wcounts.hex"}, wcount);

        $display("--- %0d cases, SIMD %0d, ilanes %0d, shiftu %0d, permu %0d, red %0d, float %0d, flanes %0d, vregs %0d ---",
                 ncase, SIMD, `KHS_ILANES, `KHS_SHIFTU, `KHS_PERMU, `KHS_RED,
                 `KHS_FLOAT, `KHS_FLANES, VREGS);
        for (c = 0; c < ncase; c = c + 1) begin
            run_case(c);
            $display("    case%0d  %0d instructions  %0d cycles  %s",
                     c, icount[c], cyc_used[c],
                     (errors == 0) ? "ok" : "see failures above");
        end

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

    // A configuration refusing an encoding the vectors contain is a generator
    // and bench that disagree about what was built, and it must be loud.
    always @(posedge clk) if (resetn && x_valid && (x_illegal || x_misalign))
        $display("%0t ERROR khs_unit_tb: the unit refused %08h (illegal %b misaligned %b)",
                 $time, x_instr, x_illegal, x_misalign);

    initial begin
        #500000000;
        $display("  FAIL WATCHDOG -- the SIMD datapath bench never finished");
        $display("========================================");
        $finish;
    end

endmodule

`default_nettype wire

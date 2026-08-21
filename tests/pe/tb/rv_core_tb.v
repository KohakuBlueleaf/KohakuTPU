// rv_core_tb -- the RV32I core against a Python golden model, instruction by
// instruction.
//
// This is level 1 of the PE's verification: the pipeline, the register file and
// the instruction window, with a flat memory model in place of the L1. The
// question it answers is only "is the RV32 core correct", and it answers it on
// the instruction the answer changes, not on a wrong result thousands of cycles
// later.
//
// WHAT IS COMPARED. tests/pe/tools/rv_gen.py runs each program through rv_model.py
// and writes the retirement stream: PC, destination register and value, one
// line per committed instruction. This bench compares every retirement against
// that line as it happens. Then, at the end of each case, four things the
// retirement stream cannot show:
//
//   final register file       every architectural register
//   scratchpad checksum       stores, which never appear in a retirement
//   flat-memory checksum      the same for the global region
//   peer-push log             address decode and byte enables of an uncached
//                             store, which leaves the core and comes back never
//
// EVERY CASE IS BOUNDED. A per-case cycle ceiling, a whole-run watchdog, and
// the retirement count from the model: a core that halts early fails on the
// count, and one that never halts fails on the ceiling rather than hanging.
//
// Run it with:
//     python tests/pe/tools/rv_run.py --gate 1

`default_nettype none
`timescale 1ns/1ps

// Relative to the xsim working directory, which scripts/py/xsim.py puts at
// build/xsim_<bench>. A bench run from anywhere else must define it.
`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif

module rv_core_tb;
    localparam integer IW      = 2048;      // instruction window, words
    localparam integer SW      = 2048;      // scratchpad, words
    localparam integer DW      = 1024;      // flat global model, words
    localparam integer MAXTR   = 65536;     // retirements per case
    localparam integer MAXCASE = 64;
    localparam integer CYC_MAX = 400000;    // per case
    localparam integer META_N  = 39;

    localparam integer IAW = $clog2(IW);
    localparam integer SAW = $clog2(SW);

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

    // ---- the DUT ------------------------------------------------------------
    reg         boot_v = 1'b0;
    reg  [31:0] boot_pc = 32'd0;
    wire        core_run, core_halted, pipe_empty;
    wire [1:0]  core_cause;
    wire [31:0] core_halt_word;

    wire [IAW-1:0] imem_addr;
    wire [31:0]    imem_data;
    wire [SAW-1:0] spad_addr;
    wire [3:0]     spad_we;
    wire [31:0]    spad_wdata, spad_rdata;

    wire [31:0] l1_probe, l1_addr, l1_wdata;
    wire        l1_req, l1_we, l1_flush, l1_inval;
    wire [3:0]  l1_be;

    wire        push_valid, push_win;
    wire [3:0]  push_dx, push_dy, push_be;
    wire [13:0] push_gran;
    wire [2:0]  push_sel;
    wire [31:0] push_data;

    wire        retire_valid;
    wire [31:0] retire_pc, retire_val;
    wire [4:0]  retire_rd;

    // The flat memory model standing in for the internal L1: always hits, and
    // its read latency matches the real array's, so the pipeline sees the same
    // shape it will see in level 2.
    reg [31:0] dmem [0:DW-1];
    reg [31:0] dmem_q;
    integer bi;
    always @(posedge clk) begin
        if (l1_req && l1_we)
            for (bi = 0; bi < 4; bi = bi + 1)
                if (l1_be[bi])
                    dmem[l1_addr[$clog2(DW)+1:2]][bi*8 +: 8] <= l1_wdata[bi*8 +: 8];
        dmem_q <= dmem[l1_addr[$clog2(DW)+1:2]];
    end

    // Every configuration has to be CORRECT, not just measurable, and this is
    // the only bench that checks against the golden model -- so it takes the
    // same knobs the mesh benches and the synthesis do, spelled the same way.
`ifndef RV_BTB
 `define RV_BTB 32
`endif
`ifndef RV_FWD_X
 `define RV_FWD_X 1
`endif
`ifdef RV_RF_BRAM
    localparam RF_PRIM = "block";
`else
    localparam RF_PRIM = "distributed";
`endif

    rv_core #(.IMEM_WORDS(IW), .SPAD_WORDS(SW), .BTB_ENTRIES(`RV_BTB),
              .BTB_TAG_W(8), .REGFILE_PRIM(RF_PRIM), .FWD_X(`RV_FWD_X),
              .POS_WIDTH(4)) dut (
        .clk(clk), .resetn(resetn),
        .boot_v(boot_v), .boot_pc(boot_pc),
        .run(core_run), .halted(core_halted), .cause(core_cause),
        .halt_word(core_halt_word), .pipe_empty(pipe_empty),
        .coreid(8'h11), .arg(32'hA5A5_0001), .wr_out(16'd0),
        .imem_addr(imem_addr), .imem_data(imem_data),
        .spad_addr(spad_addr), .spad_we(spad_we), .spad_wdata(spad_wdata),
        .spad_rdata(spad_rdata),
        .l1_probe(l1_probe), .l1_req(l1_req), .l1_we(l1_we), .l1_be(l1_be),
        .l1_addr(l1_addr), .l1_wdata(l1_wdata), .l1_rdata(dmem_q),
        .l1_stall(1'b0), .l1_flush(l1_flush), .l1_inval(l1_inval),
        .l1_flush_busy(1'b0),
        .push_valid(push_valid), .push_ready(1'b1),
        .push_dx(push_dx), .push_dy(push_dy), .push_win(push_win),
        .push_gran(push_gran), .push_sel(push_sel), .push_be(push_be),
        .push_data(push_data),
        .retire_valid(retire_valid), .retire_pc(retire_pc),
        .retire_rd(retire_rd), .retire_val(retire_val),
        .cycle_ctr(), .instret_ctr()
    );

    reg             im_we = 1'b0;
    reg  [IAW-1:0]  im_wa = {IAW{1'b0}};
    reg  [31:0]     im_wd = 32'd0;

    rv_imem #(.WORDS(IW), .MEM_PRIM("block")) u_imem (
        .clk(clk), .wr_en(im_we), .wr_addr(im_wa), .wr_data(im_wd),
        .rd_addr(imem_addr), .rd_data(imem_data)
    );

    reg             sp_en = 1'b0;
    reg  [3:0]      sp_we = 4'd0;
    reg  [SAW-1:0]  sp_a  = {SAW{1'b0}};
    reg  [31:0]     sp_wd = 32'd0;
    wire [31:0]     sp_rd;

    rv_spad #(.WORDS(SW), .MEM_PRIM("block")) u_spad (
        .clk(clk),
        .a_en(sp_en), .a_we(sp_we), .a_addr(sp_a), .a_wdata(sp_wd),
        .a_rdata(sp_rd),
        .b_addr(spad_addr), .b_we(spad_we), .b_wdata(spad_wdata),
        .b_rdata(spad_rdata)
    );

    // ---- golden data --------------------------------------------------------
    reg [31:0] prog  [0:IW-1];
    reg [71:0] trace [0:MAXTR-1];
    reg [31:0] meta  [0:META_N-1];
    reg [31:0] nc    [0:0];

    integer ncase, c, i, k;
    integer ridx, ncmp;
    integer push_n;
    reg [31:0] push_sum;
    reg [31:0] spad_sum, dmem_sum;
    reg [31:0] xshadow [0:31];
    reg        case_fail;
    reg [8*40-1:0] cname;

    // ---- the retirement comparison, live -----------------------------------
    reg  armed = 1'b0;
    wire [71:0] want = trace[ridx];

    always @(posedge clk) if (resetn && armed && retire_valid) begin
        if ({retire_pc, 3'd0, retire_rd, retire_val} !== want) begin
            if (!case_fail) begin
                $display("  FAIL case%0d retirement %0d: pc %h rd %0d val %h, model says pc %h rd %0d val %h",
                         c, ridx, retire_pc, retire_rd, retire_val,
                         want[71:40], want[36:32], want[31:0]);
                errors = errors + 1;
            end
            case_fail <= 1'b1;
        end
        if (retire_rd != 5'd0) xshadow[retire_rd] <= retire_val;
        ridx <= ridx + 1;
    end

    // Every uncached store, reassembled into the address software wrote so the
    // model's log and this one are the same object.
    always @(posedge clk) if (resetn && armed && push_valid) begin
        push_sum <= push_sum +
                    (({4'h3, push_dx, push_dy, push_win, push_gran, push_sel,
                       2'b00} * 3) + (push_data * 5) + ({28'd0, push_be} * 7))
                    * (push_n + 1);
        push_n <= push_n + 1;
    end

    // ---- helpers ------------------------------------------------------------
    task load_case(input integer n);
        string fn;
        begin
            for (i = 0; i < IW; i = i + 1) prog[i] = 32'd0;
            for (i = 0; i < META_N; i = i + 1) meta[i] = 32'd0;
            fn = $sformatf("%s/case%02d/prog.hex", `PE_DIR, n);
            $readmemh(fn, prog);
            fn = $sformatf("%s/case%02d/trace.hex", `PE_DIR, n);
            $readmemh(fn, trace);
            fn = $sformatf("%s/case%02d/meta.hex", `PE_DIR, n);
            $readmemh(fn, meta);
        end
    endtask

    task wipe_and_load;
        begin
            @(posedge clk);
            for (i = 0; i < SW; i = i + 1) begin
                sp_en <= 1'b1; sp_we <= 4'hF; sp_a <= i[SAW-1:0]; sp_wd <= 32'd0;
                @(posedge clk);
            end
            sp_en <= 1'b0; sp_we <= 4'd0;
            for (i = 0; i < DW; i = i + 1) dmem[i] = 32'd0;
            for (i = 0; i < IW; i = i + 1) begin
                im_we <= 1'b1; im_wa <= i[IAW-1:0]; im_wd <= prog[i];
                @(posedge clk);
            end
            im_we <= 1'b0;
        end
    endtask

    task checksums;
        begin
            spad_sum = 32'd0;
            @(posedge clk);
            for (i = 0; i < SW; i = i + 1) begin
                sp_en <= 1'b1; sp_we <= 4'd0; sp_a <= i[SAW-1:0];
                @(posedge clk);
                // one cycle of read latency, so index i-1 is what is out now
                if (i > 0) spad_sum = spad_sum + sp_rd * i;
            end
            @(posedge clk);
            spad_sum = spad_sum + sp_rd * SW;
            sp_en <= 1'b0;
            dmem_sum = 32'd0;
            for (i = 0; i < DW; i = i + 1)
                dmem_sum = dmem_sum + dmem[i] * (i + 1);
        end
    endtask

    integer cyc;
    integer total_retire = 0, total_case = 0, failed_case = 0;

    initial begin
        nc[0] = 32'hxxxx_xxxx;
        $readmemh({`PE_DIR, "/ncase.hex"}, nc);
        ncase = nc[0];
        // A missing file leaves the count at x, and x fails every comparison
        // rather than one of them -- so a bench that found nothing would
        // otherwise run zero cases and report PASS.
        if ((^nc[0] === 1'bx) || (ncase < 1) || (ncase > MAXCASE)) begin
            $display("  FAIL no cases at %s -- run python tests/pe/tools/rv_gen.py first",
                     `PE_DIR);
            $display("========================================");
            $display("  FAIL -- 0 checks, 1 errors");
            $display("========================================");
            $finish;
        end
        $display("--- %0d co-simulation cases, regfile %0s, FWD_X %0d, BTB %0d ---",
                 ncase, RF_PRIM, `RV_FWD_X, `RV_BTB);

        for (c = 0; c < ncase; c = c + 1) begin
            resetn = 1'b0;
            repeat (6) @(posedge clk);
            load_case(c);
            wipe_and_load;
            resetn = 1'b1;
            repeat (4) @(posedge clk);

            ridx = 0; push_n = 0; push_sum = 32'd0; case_fail = 1'b0;
            for (i = 0; i < 32; i = i + 1) xshadow[i] = 32'd0;
            armed = 1'b1;

            boot_pc <= 32'd0;
            boot_v  <= 1'b1;
            @(posedge clk);
            boot_v  <= 1'b0;

            cyc = 0;
            while (!(core_halted && pipe_empty) && (cyc < CYC_MAX)) begin
                @(posedge clk);
                cyc = cyc + 1;
            end
            repeat (4) @(posedge clk);
            armed = 1'b0;

            if (cyc >= CYC_MAX) begin
                $display("  FAIL case%0d ran past %0d cycles without halting",
                         c, CYC_MAX);
                errors = errors + 1;
                case_fail = 1'b1;
            end

            chk(ridx,           meta[0], "retired count");
            chk(core_cause,     meta[1], "halt cause");
            chk(core_halt_word, meta[2], "halt word");
            ncmp = 0;
            for (i = 1; i < 32; i = i + 1)
                if (xshadow[i] !== meta[3 + i]) ncmp = ncmp + 1;
            chk(ncmp, 0, "regs differing");
            if (ncmp != 0)
                for (i = 1; i < 32; i = i + 1)
                    if (xshadow[i] !== meta[3 + i])
                        $display("      x%0d: rtl %h model %h",
                                 i, xshadow[i], meta[3 + i]);

            checksums;
            chk(spad_sum, meta[35], "scratchpad checksum");
            chk(dmem_sum, meta[36], "global-region checksum");
            chk(push_n,   meta[37], "peer pushes");
            chk(push_sum, meta[38], "peer push checksum");

            total_retire = total_retire + ridx;
            total_case = total_case + 1;
            if (case_fail) failed_case = failed_case + 1;
            $display("    case%02d  %6d retired  %7d cycles  %s",
                     c, ridx, cyc, case_fail ? "FAILED" : "ok");
        end

        $display("--- %0d cases, %0d instructions retired, %0d cases failed ---",
                 total_case, total_retire, failed_case);
        $display("========================================");
        if (checks == 0)  $display("  FAIL -- the bench made no checks at all");
        else if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else             $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        $display("========================================");
        $finish;
    end

    // Whole-run bound, independent of the per-case ceiling: a bench that never
    // reaches its own check is a hang, and a hang must end by itself.
    initial begin
        #60000000;
        $display("  FAIL WATCHDOG -- the co-simulation never finished");
        $display("========================================");
        $finish;
    end

endmodule

`default_nettype wire

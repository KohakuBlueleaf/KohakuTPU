// kht_sys_tb -- the SIMT PE's machine. Not a test script.
//
// It loads a shader image, initialises DRAM from a hex image, runs to
// completion, dumps the final memory state and lets Python compare it. ANY
// kernel the toolchain can emit runs here WITHOUT EDITING THIS FILE -- a shader
// is just another program image, and that is the property that makes the
// difference between a machine and a pile of test cases.
//
// The memory behind it is real: MAG's own agent, its read engine, its write
// slots and its converged DRAM port, with an AXI RAM at the end. A coalescer
// verified against a stub that answers instantly is not verified.
//
// THE WITNESS IS THE DUMPED STATE, not a $display line. The bench reports the
// DRAM checksum and, on a mismatch, the first differing words -- and it reports
// REQUESTS and GATHERS separately, because "requests issued per gather" is the
// number the coalescer is judged on and it must be a measurement rather than an
// argument. Before a coalescer exists the LSU serialises lanes, so the ratio
// starts at LANES and is expected to FALL; a witness that only appears once the
// optimisation lands cannot show the optimisation working.
//
//     python tests/pe/tools/rv_simt_run.py shader.s

`default_nettype none
`timescale 1ns/1ps

// xsim.py always overrides this with an absolute path: the default resolves
// from the xsim RUN directory, so it is only right at the default build root.
`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif
`ifndef KHT_LANES
 `define KHT_LANES 8
`endif
`ifndef KHT_WAVES
 `define KHT_WAVES 16
`endif
`ifndef KHT_MASK
 `define KHT_MASK 1
`endif
`ifndef KHT_IPDOM
 `define KHT_IPDOM 1
`endif
`ifndef KHT_FLT
 `define KHT_FLT 1
`endif
// THE THREE UNIT COUNTS, so a shader can be run UNCHANGED at every width. That
// is the only way "the ISA knows no count" is a test rather than a claim: the
// same image, the same golden DRAM, a narrower machine and more passes.
// `KHT_FLT` is kept as the shorthand the ladder rows name: 0 zeroes both counts.
`ifndef KHT_FLANES
 `define KHT_FLANES (`KHT_FLT ? `KHT_LANES : 0)
`endif
`ifndef KHT_FSFU
 `define KHT_FSFU 0
`endif
// Shuffle OUTPUT lanes per pass. NOT architectural: same answer at every width,
// only the cycles change.
`ifndef KHT_SHFLU
 `define KHT_SHFLU -1
`endif
// LDS banks. NOT architectural: fewer banks is more conflicts and more passes,
// and kht_lds' sequencer already drains them.
`ifndef KHT_LDSB
 `define KHT_LDSB -1
`endif

module kht_sys_tb;
    localparam integer FW  = 288;
    localparam integer PW  = 4;
    localparam integer DW  = 256;
    localparam integer AW  = 40;
    localparam integer IW  = 2048;
    localparam integer SW  = 2048;
    localparam integer DWORDS = 4096;
    localparam integer RAM_DEPTH = 512;
    localparam integer META_N = 8;
    localparam integer LANES = `KHT_LANES;

    localparam [PW-1:0] PE_X = 4'd1, PE_Y = 4'd1;
    localparam [7:0] BUF_SPAD = 8'd0, BUF_IMEM = 8'd1;

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
    wire          pe_run, pe_halted, pe_busy;
    wire [31:0]   pe_reqs, pe_gathers;
    wire [LANES-1:0] pe_mask;

    kht_mesh #(.FW(FW), .PW(PW), .DW(DW), .AW(AW),
               .IMEM_WORDS(IW), .SPAD_WORDS(SW), .L1_LINES(128),
               .LANES(LANES), .WAVES(`KHT_WAVES),
               .HAS_MASK(`KHT_MASK), .HAS_IPDOM(`KHT_IPDOM),
               .FLANES(`KHT_FLANES),
               .FSFU_UNITS(`KHT_FSFU),
               .SHFL_UNITS(`KHT_SHFLU), .LDS_BANKS(`KHT_LDSB),
               .VREG_PRIM("block"), .RAM_DEPTH(RAM_DEPTH)) dut (
        .clk(clk), .rstn(rstn),
        .ext_in_data(ag_out), .ext_in_valid(ag_out_valid),
        .ext_in_busy(ag_out_busy),
        .ext_out_data(ag_in), .ext_out_valid(ag_in_valid),
        .ext_out_busy(ag_in_busy),
        .pe_run(pe_run), .pe_halted(pe_halted), .pe_busy(pe_busy),
        .pe_reqs(pe_reqs), .pe_gathers(pe_gathers), .pe_mask(pe_mask)
    );

    rv_agent #(.FW(FW), .PW(PW), .AX(1), .AY(0), .IMG_WORDS(IW)) u_ag (
        .clk(clk), .resetn(rstn),
        .out_data(ag_out), .out_valid(ag_out_valid), .out_busy(ag_out_busy),
        .in_data(ag_in), .in_valid(ag_in_valid), .in_busy(ag_in_busy)
    );

    // A BOUNDED state probe. A shader that does not retire is a hang, and a
    // hang is diagnosed by watching the machine advance rather than by reading
    // the source again; the cap keeps a wedged run from filling the log.
`ifdef KHT_TRACE
    integer tn = 0;
    always @(posedge clk) if (rstn && (tn < 80)) begin
        if (dut.u_pe.u_core.go) begin
            $display("  TR %0t pc %08x ins %08x live %b hold %b halt %b",
                     $time, dut.u_pe.u_core.f2_pc, dut.u_pe.u_core.instr,
                     dut.u_pe.u_core.live, dut.u_pe.u_core.hold,
                     dut.u_pe.u_core.halt_q);
            tn = tn + 1;
        end
    end
    // A WEDGE IS DIAGNOSED BY WATCHING THE MACHINE, NOT BY READING IT AGAIN.
    // The retire trace above stops after 80 lines, which is exactly the wrong
    // 80 when the hang is late; this prints the whole runnable state on a slow
    // heartbeat so a stuck run says WHY rather than just failing to finish.
    integer hb = 0;
    always @(posedge clk) if (rstn) begin
        hb = hb + 1;
        if ((hb % `KHT_HB) == 0) begin
            $display("  HB %0t f2 %08x hold %b [base %b vt %b warm %b lsuw %b shz %b fsoon %b] l1s %b fpend %b",
                     $time, dut.u_pe.u_core.f2_pc, dut.u_pe.u_core.hold,
                     dut.u_pe.u_core.base_hold, dut.u_pe.u_core.vt_stall,
                     dut.u_pe.u_core.warm_stall, dut.u_pe.u_core.lsu_want,
                     dut.u_pe.u_core.s_hz, dut.u_pe.u_core.f_soon,
                     dut.u_pe.u_core.l1_stall, dut.u_pe.u_core.fpend);
        end
    end
    integer bn = 0;
    always @(posedge clk) if (rstn && (bn < 12) && dut.u_pe.boot_v) begin
        $display("  TR %0t BOOT pc %08x", $time, dut.u_pe.k_pc);
        bn = bn + 1;
    end
    // A miss holds the walk for tens of cycles and those cycles say nothing;
    // print only the ones that ADVANCE a lane or CAPTURE a word.
    integer mn = 0;
    always @(posedge clk) if (rstn && (mn < 60)) begin
        if (dut.u_pe.u_core.lsu_run && !dut.u_pe.u_core.l1_stall) begin
            $display("  TR %0t LSU ln %0d ph %b ea %08x req %b stl %b rdata %08x pc %08x sv1 %08x vm %b lin %b warm %0d",
                     $time, dut.u_pe.u_core.ln_q, dut.u_pe.u_core.ph_q,
                     dut.u_pe.u_core.ea, dut.u_pe.u_core.l1_req,
                     dut.u_pe.u_core.l1_stall, dut.u_pe.u_core.l1_rdata,
                     dut.u_pe.u_core.f2_pc, dut.u_pe.u_core.sv1,
                     dut.u_pe.u_core.is_vmem, dut.u_pe.u_core.mem_lin,
                     dut.u_pe.u_core.warm_q);
            mn = mn + 1;
        end
    end
    // The instance only exists when the gate is on, and a hierarchical
    // reference to an unbuilt generate branch is an elaboration error.
    generate
    if (`KHT_LDSB != 0) begin : g_ldstrace
    integer dn = 0;
    always @(posedge clk) if (rstn && (dn < 26)) begin
        if (dut.u_pe.g_banklds.u_lds.run) begin
            $display("  TR %0t LDS ph %b we %b todo %b served %b | row0 %0d row7 %0d wd0 %08x rd0 %08x rd7 %08x",
                     $time, dut.u_pe.g_banklds.u_lds.ph,
                     dut.u_pe.g_banklds.u_lds.we,
                     dut.u_pe.g_banklds.u_lds.todo,
                     dut.u_pe.g_banklds.u_lds.served,
                     dut.u_pe.g_banklds.u_lds.b_addr_r[0],
                     dut.u_pe.g_banklds.u_lds.b_addr_r[7],
                     dut.u_pe.g_banklds.u_lds.b_wdata_r[0],
                     dut.u_pe.g_banklds.u_lds.b_rdata[0],
                     dut.u_pe.g_banklds.u_lds.b_rdata[7]);
            dn = dn + 1;
        end
    end
    end
    endgenerate

    integer wn = 0;
    always @(posedge clk) if (rstn && (wn < 12)) begin
        if (dut.u_pe.u_l1.wb_valid && dut.u_pe.u_l1.wb_ready) begin
            $display("  TR %0t L1 WRITEBACK addr %08x word0 %08x",
                     $time, {dut.u_pe.u_l1.wb_addr, 1'b0},
                     dut.u_pe.u_l1.wb_data[31:0]);
            wn = wn + 1;
        end
    end
`endif

    reg [31:0] dinit [0:DWORDS-1];
    reg [31:0] dfin  [0:DWORDS-1];
    reg [31:0] sinit [0:63];
    reg [31:0] meta  [0:META_N-1];

    integer i, k, ok;
    integer nprog, nspad;
    reg [31:0] dsum;

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

    integer sig_before;

    initial begin
        for (i = 0; i < DWORDS; i = i + 1) begin dinit[i] = 0; dfin[i] = 0; end
        for (i = 0; i < 64; i = i + 1) begin
            sinit[i] = 0;
        end
        for (i = 0; i < META_N; i = i + 1) begin
            meta[i] = 0;
        end
        for (i = 0; i < IW; i = i + 1) begin
            u_ag.img[i] = 0;
        end

        $readmemh({`PE_DIR, "/simt/user/meta.hex"}, meta);
        nprog = meta[6];
        nspad = meta[7];
        if (nprog == 0) begin
            // THE PATH IS THE WHOLE OF THIS CHECK. It read /gpu/user, which
            // rv_simt_run.py stopped writing at the khg->kht rename, so every
            // shader silently re-ran one stale image and every case passed.
            $display("  FAIL no shader at %s/simt/user -- rv_simt_run.py writes it",
                     `PE_DIR);
            $display("========================================");
            $display("  FAIL -- 0 checks, 1 errors");
            $display("========================================");
            $finish;
        end

        $readmemh({`PE_DIR, "/simt/user/prog.hex"}, u_ag.img);
        $readmemh({`PE_DIR, "/simt/user/dram.hex"}, dinit);
        $readmemh({`PE_DIR, "/simt/user/dfin.hex"}, dfin);
        $readmemh({`PE_DIR, "/simt/user/spad.hex"}, sinit);

        $display("--- one shader through SIMT PE + router + MAG + RAM, %0d words, %0d lanes x %0d waves, %0d launched ---",
                 nprog, LANES, `KHT_WAVES, meta[4]);

        rstn = 1'b0;
        repeat (12) @(posedge clk);
        rstn = 1'b1;
        repeat (8) @(posedge clk);
        dram_write_init;

        u_ag.load_image(PE_X, PE_Y, BUF_IMEM, nprog, 0);
        if (nspad > 0) begin
            for (i = 0; i < IW; i = i + 1) begin
                u_ag.img[i] = 32'd0;
            end
            for (i = 0; i < nspad; i = i + 1) begin
                u_ag.img[i] = sinit[i];
            end
            u_ag.load_image(PE_X, PE_Y, BUF_SPAD, nspad, 0);
        end

        sig_before = u_ag.sig_n;
        // The kick's op IS the wave count; meta[4] carries what the model ran.
        u_ag.kick(PE_X, PE_Y, 8'hB0, 1'b0, meta[4][7:0], 32'd0, meta[5]);
        u_ag.wait_signal(sig_before + 1, 400000, ok);

        chk(ok, 1, "the shader retired");
        if (ok) begin
            $display("    halt word      %08x   (model %08x)",
                     u_ag.sig_arg, meta[1]);
            $display("    halt cause     %0d          (model %0d)",
                     (u_ag.sig_code == 8'h00) ? 1 : 3, meta[0]);
            $display("    kick to done   %0d cycles", u_ag.ws_guard);
            // The coalescer witness. Requests per gather starts at LANES with a
            // serialising LSU and must FALL when a coalescer lands.
            $display("    memory         %0d request(s) over %0d gather(s)",
                     pe_reqs, pe_gathers);
            chk(u_ag.sig_code, (meta[0] == 32'd1) ? 8'h00 : 8'h04,
                "completion code");
            chk(u_ag.sig_arg, meta[1], "halt word");
            dram_checksum(dsum);
            chk(dsum, meta[3], "DRAM after the shader");
            if (dsum !== meta[3]) begin
                k = 0;
                for (i = 0; i < DWORDS; i = i + 1) begin
                    if ((dram_word(i) !== dfin[i]) && (k < 16)) begin
                        $display("      dram word %0d (byte %0d): rtl %08x model %08x",
                                 i, i * 4, dram_word(i), dfin[i]);
                        k = k + 1;
                    end
                end
            end
        end

        $display("========================================");
        if (checks == 0) begin
            $display("  FAIL -- the bench made no checks");
        end
        else if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #200000000;
        $display("  FAIL WATCHDOG -- the GPU bench never finished");
        $display("========================================");
        $finish;
    end

endmodule

`default_nettype wire

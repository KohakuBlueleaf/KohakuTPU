// rv_sys_tb -- the PRIMARY PE integration test: one RV32 PE, real routers, the
// real MAG, an AXI RAM, and real programs.
//
// Level 3. Nothing here is a model. A load in the program becomes a
// MEM_RD_REQ, crosses two routers, is served by MAG's read engine as a
// one-entry streaming fetch, comes back as a MEM_RD_RESP and fills a line. A
// dirty eviction becomes a MEM_WR_REQ and one MEM_WR_DATA beat and lands in
// the RAM. If that path is wrong the program gets a wrong answer, and the
// answer is checked against the Python model that ran the same image.
//
// BOOT IS THE SAME WRITE PATH EVERYTHING ELSE USES. The image arrives as a
// CU_DATA burst into the instruction window and is then EXECUTED, which proves
// the window write landed correctly far more strongly than reading the array
// back would.
//
// Two cases are driven by the bench rather than by the model, because they
// depend on something arriving from outside while the program runs:
//   inval_recheck  the bench changes DRAM under a cached line, and only a real
//                  invalidate-all makes the program see it
//   doorbell       a command ring in the scratchpad, pushed word by word with
//                  the producer index last -- the protocol of design note s16.8
//                  with the bench standing in for a peer core
//
// Run it with:
//     python tests/pe/tools/rv_run.py --gate 3

`default_nettype none
`timescale 1ns/1ps

`ifndef PE_DIR
 `define PE_DIR "../../tests/pe/build"
`endif
`ifndef NPE_BISECT
 `define NPE_BISECT 1
`endif

// The variant knobs, same defaults and same names as the multi-core bench, so a
// frontier point drives every level from one set of numbers.
`ifndef RV_L1_LINES
 `define RV_L1_LINES 128
`endif
`ifndef RV_BTB
 `define RV_BTB 32
`endif
`ifndef RV_FWD_X
 `define RV_FWD_X 1
`endif

module rv_sys_tb;
    localparam integer FW  = 288;
    localparam integer PW  = 4;
    localparam integer DW  = 256;
    localparam integer AW  = 40;
    localparam integer IW  = 2048;
    localparam integer SW  = 2048;
    localparam integer DWORDS = 4096;          // 32-bit words of DRAM checked
    localparam integer RAM_DEPTH = 512;        // 256-bit entries = DWORDS/8
    localparam integer MAXSYS = 32;
    localparam integer META_N = 8;

    localparam [PW-1:0] PE_X = 4'd1, PE_Y = 4'd1;
    localparam [7:0] BUF_SPAD = 8'd0, BUF_IMEM = 8'd1, BUF_SPAD_W = 8'd4;

    reg clk = 1'b0, rstn = 1'b0;
    always #2 clk = ~clk;

`ifndef RV_TRACE_FROM
 `define RV_TRACE_FROM 293000
`endif
`ifdef RV_TRACE_LINE
    wire [30:0] tr_wb   = dut.g_pe[0].g_have.u_pe.u_l1.wb_addr;
    wire        tr_wbv  = dut.g_pe[0].g_have.u_pe.u_l1.wb_valid;
    wire        tr_wbr  = dut.g_pe[0].g_have.u_pe.u_l1.wb_ready;
    wire [31:0] tr_addr = dut.g_pe[0].g_have.u_pe.u_l1.addr;
    wire        tr_req  = dut.g_pe[0].g_have.u_pe.u_l1.req;
    wire        tr_we   = dut.g_pe[0].g_have.u_pe.u_l1.we;
    wire        tr_hit  = dut.g_pe[0].g_have.u_pe.u_l1.hit;
    wire        tr_stl  = dut.g_pe[0].g_have.u_pe.u_l1.stall;
    always @(posedge clk) if (rstn) begin
        if (tr_wbv && tr_wbr && (tr_wb[30:5] == `RV_TRACE_LINE))
            $display("  TR %0t writeback line %0d word0 %08x",
                     $time, tr_wb[30:5], dut.g_pe[0].g_have.u_pe.u_l1.wb_data[31:0]);
        if (tr_req && tr_we && tr_hit && !tr_stl &&
            (tr_addr[30:5] == `RV_TRACE_LINE))
            $display("  TR %0t store commit line %0d data %08x",
                     $time, tr_addr[30:5],
                     dut.g_pe[0].g_have.u_pe.u_l1.wdata);
        if (tr_req && !tr_we && tr_hit && !tr_stl &&
            (tr_addr[30:5] == `RV_TRACE_LINE))
            $display("  TR %0t load done  line %0d data %08x",
                     $time, tr_addr[30:5],
                     dut.g_pe[0].g_have.u_pe.u_l1.rdata);
        if (dut.g_pe[0].g_have.u_pe.u_req.send_valid &&
            dut.g_pe[0].g_have.u_pe.u_req.send_ready && ($time > `RV_TRACE_FROM))
            $display("  TR %0t SEND ty %h addr %h w0 %08x",
                     $time,
                     dut.g_pe[0].g_have.u_pe.u_req.send_flit[271:268],
                     dut.g_pe[0].g_have.u_pe.u_req.send_flit[255:216],
                     dut.g_pe[0].g_have.u_pe.u_req.send_flit[31:0]);
        if (dut.u_mag.g_port[0].u_eng.take_wr_req && ($time > `RV_TRACE_FROM))
            $display("  TR %0t MAG slot %0d open addr %h",
                     $time, dut.u_mag.g_port[0].u_eng.ws_free,
                     dut.u_mag.g_port[0].u_eng.wi_addr);
        if (dut.u_mag.g_port[0].u_eng.take_wr_data && ($time > `RV_TRACE_FROM))
            $display("  TR %0t MAG slot %0d data w0 %08x",
                     $time, dut.u_mag.g_port[0].u_eng.ws_match,
                     dut.u_mag.g_port[0].u_eng.wq_flit[31:0]);
        if (dut.u_mag.g_port[0].u_eng.ws_issue && ($time > `RV_TRACE_FROM))
            $display("  TR %0t MAG issue slot %0d addr %h",
                     $time, dut.u_mag.g_port[0].u_eng.ws_pick,
                     dut.u_mag.g_port[0].u_eng.ws_addr[
                         dut.u_mag.g_port[0].u_eng.ws_pick]);
        if (dut.u_mag.g_port[0].u_eng.ws_done && ($time > `RV_TRACE_FROM))
            $display("  TR %0t MAG free  slot %0d",
                     $time, dut.u_mag.g_port[0].u_eng.ws_cur);
        if (dut.u_ram.s_axi_awvalid && dut.u_ram.s_axi_awready &&
            (dut.u_ram.s_axi_awaddr[30:5] == `RV_TRACE_LINE))
            $display("  TR %0t AXI AW line %0d len %0d",
                     $time, dut.u_ram.s_axi_awaddr[30:5], dut.u_ram.s_axi_awlen);
        if (dut.u_ram.s_axi_wvalid && dut.u_ram.s_axi_wready &&
            (dut.u_ram.waddr[30:5] == `RV_TRACE_LINE))
            $display("  TR %0t AXI W  line %0d word0 %08x strb %h",
                     $time, dut.u_ram.waddr[30:5],
                     dut.u_ram.s_axi_wdata[31:0], dut.u_ram.s_axi_wstrb[3:0]);
    end
`endif

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

    rv_mesh #(.FW(FW), .PW(PW), .DW(DW), .AW(AW), .NPE(`NPE_BISECT),
              .IMEM_WORDS(IW), .SPAD_WORDS(SW),
              .L1_LINES(`RV_L1_LINES), .BTB_ENTRIES(`RV_BTB),
              .FWD_X(`RV_FWD_X), .REGFILE_PRIM(RF_PRIM),
              .RAM_DEPTH(RAM_DEPTH)) dut (
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

    // ---- golden data --------------------------------------------------------
    reg [31:0] dinit [0:DWORDS-1];
    reg [31:0] dfin  [0:DWORDS-1];
    reg [31:0] sinit [0:63];
    reg [31:0] meta  [0:META_N-1];
    reg [31:0] ns    [0:0];

    integer nsys, c, i, k, ok;
    integer nprog, nspad;
    reg [31:0] dsum;
    reg [8*24-1:0] cname;

    task clear_arrays;
        begin
            for (i = 0; i < DWORDS; i = i + 1) dinit[i] = 32'd0;
            for (i = 0; i < DWORDS; i = i + 1) dfin[i] = 32'd0;
            for (i = 0; i < 64; i = i + 1) sinit[i] = 32'd0;
            for (i = 0; i < META_N; i = i + 1) meta[i] = 32'd0;
            for (i = 0; i < IW; i = i + 1) u_ag.img[i] = 32'd0;
        end
    endtask

    // The RAM is 256 bits wide and the model thinks in 32-bit words; one place
    // converts, so the two can never disagree about which word is which.
    task dram_write_init;
        begin
            for (i = 0; i < RAM_DEPTH; i = i + 1)
                for (k = 0; k < 8; k = k + 1)
                    dut.u_ram.mem[i][k*32 +: 32] = dinit[i * 8 + k];
        end
    endtask

    task dram_checksum(output reg [31:0] s);
        begin
            s = 32'd0;
            for (i = 0; i < RAM_DEPTH; i = i + 1)
                for (k = 0; k < 8; k = k + 1)
                    s = s + dut.u_ram.mem[i][k*32 +: 32] * (i * 8 + k + 1);
        end
    endtask

    function [31:0] dram_word;
        input integer wi;
        dram_word = dut.u_ram.mem[wi / 8][(wi % 8) * 32 +: 32];
    endfunction

    task poke_dram(input integer wi, input [31:0] v);
        begin dut.u_ram.mem[wi / 8][(wi % 8) * 32 +: 32] = v; end
    endtask

    task reset_mesh;
        begin
            rstn = 1'b0;
            repeat (12) @(posedge clk);
            rstn = 1'b1;
            repeat (8) @(posedge clk);
        end
    endtask

    string fn;
    integer sig_before;

    // ---- one model-checked case --------------------------------------------
    task run_sys(input integer n);
        begin
            clear_arrays;
            fn = $sformatf("%s/sys/sys%02d/prog.hex", `PE_DIR, n);
            $readmemh(fn, u_ag.img);
            fn = $sformatf("%s/sys/sys%02d/dram.hex", `PE_DIR, n);
            $readmemh(fn, dinit);
            fn = $sformatf("%s/sys/sys%02d/dfin.hex", `PE_DIR, n);
            $readmemh(fn, dfin);
            fn = $sformatf("%s/sys/sys%02d/spad.hex", `PE_DIR, n);
            $readmemh(fn, sinit);
            fn = $sformatf("%s/sys/sys%02d/meta.hex", `PE_DIR, n);
            $readmemh(fn, meta);
            nprog = meta[6];
            nspad = meta[7];

            $display("    sys%02d files read, nprog %0d nspad %0d", n, nprog, nspad);
            reset_mesh;
            dram_write_init;
            $display("    sys%02d reset done at %0t", n, $time);

            u_ag.load_image(PE_X, PE_Y, BUF_IMEM, nprog, 0);
            $display("    sys%02d image sent at %0t", n, $time);
            if (nspad > 0) begin
                for (i = 0; i < IW; i = i + 1) u_ag.img[i] = 32'd0;
                for (i = 0; i < nspad; i = i + 1) u_ag.img[i] = sinit[i];
                u_ag.load_image(PE_X, PE_Y, BUF_SPAD, nspad, 0);
            end

            sig_before = u_ag.sig_n;
            // `last` CLEAR on the kick: noc_cu_base reports a batch completion
            // with the program id in place of exec_result, and the halt word is
            // what this bench is here to read.
            $display("    sys%02d booted, %0d words, kicking", n, nprog);
            u_ag.kick(PE_X, PE_Y, 8'h40 + n[7:0], 1'b0, 8'd1, 32'd0, meta[5]);
            u_ag.wait_signal(sig_before + 1, 300000, ok);

            chk(ok, 1, "the program retired");
            if (ok) begin
                chk(u_ag.sig_code, (meta[0] == 32'd1) ? 8'h00 : 8'h04,
                    "completion code");
                chk(u_ag.sig_arg, meta[1], "halt word");
                chk(u_ag.sig_sx, PE_X, "completion source x");
                chk(u_ag.sig_sy, PE_Y, "completion source y");
                dram_checksum(dsum);
                chk(dsum, meta[3], "DRAM after the program");
                if (dsum !== meta[3]) begin
                    k = 0;
                    for (i = 0; i < DWORDS; i = i + 1)
                        if ((dram_word(i) !== dfin[i]) && (k < 8)) begin
                            $display("      dram word %0d (byte %0d): rtl %08x model %08x",
                                     i, i * 4, dram_word(i), dfin[i]);
                            k = k + 1;
                        end
                end
            end
        end
    endtask

    // ---- the two bench-driven cases ----------------------------------------
    task run_inval;
        begin
            clear_arrays;
            fn = $sformatf("%s/sys/ix00/prog.hex", `PE_DIR);
            $readmemh(fn, u_ag.img);
            nprog = 0;
            for (i = 0; i < IW; i = i + 1) if (u_ag.img[i] !== 32'd0) nprog = i + 1;
            for (i = 0; i < DWORDS; i = i + 1) dinit[i] = 32'd0;
            dinit[0] = 32'h1111_2222;

            reset_mesh;
            dram_write_init;
            u_ag.load_image(PE_X, PE_Y, BUF_IMEM, nprog + 8, 0);
            sig_before = u_ag.sig_n;
            u_ag.kick(PE_X, PE_Y, 8'h70, 1'b0, 8'd1, 32'd0, 32'd0);

            // the program publishes a flag in DRAM once it has cached word 0
            k = 0;
            while ((dram_word(1025) != 32'd1) && (k < 200000)) begin
                @(posedge clk); k = k + 1;
            end
            chk((k < 200000), 1, "the program reached its ready flag");
            chk(dram_word(1024), 32'h1111_2222, "the value it cached");

            // change DRAM under the cached line, then release the program
            poke_dram(0, 32'h3333_4444);
            u_ag.push_word(PE_X, PE_Y, BUF_SPAD_W, 14'd0, 3'd0, 4'hF, 32'd1);

            u_ag.wait_signal(sig_before + 1, 400000, ok);
            chk(ok, 1, "the program retired after the invalidate");
            if (ok) begin
                chk(u_ag.sig_code, 8'h00, "completion code");
                // Without invalidate-all the second load hits the stale line
                // and this is zero.
                chk(u_ag.sig_arg, 32'h3333_4444 - 32'h1111_2222,
                    "the second read saw memory, not the cached line");
            end
        end
    endtask

    integer cmd_n;
    task push_cmd(input [31:0] cmd, input [31:0] opnd);
        begin
            u_ag.push_word(PE_X, PE_Y, BUF_SPAD_W,
                           (16 + 2 * cmd_n) >> 3, (16 + 2 * cmd_n) & 7,
                           4'hF, cmd);
            u_ag.push_word(PE_X, PE_Y, BUF_SPAD_W,
                           (17 + 2 * cmd_n) >> 3, (17 + 2 * cmd_n) & 7,
                           4'hF, opnd);
            cmd_n = cmd_n + 1;
            // The producer index LAST. Everything about the protocol is here:
            // the payload stores and this one go to the same destination, and
            // the mesh keeps one sender's flits in order, so the consumer
            // cannot see the index move before the entry exists.
            u_ag.push_word(PE_X, PE_Y, BUF_SPAD_W, 14'd0, 3'd0, 4'hF, cmd_n);
        end
    endtask

    task run_doorbell;
        begin
            clear_arrays;
            fn = $sformatf("%s/sys/ix01/prog.hex", `PE_DIR);
            $readmemh(fn, u_ag.img);
            nprog = 0;
            for (i = 0; i < IW; i = i + 1) if (u_ag.img[i] !== 32'd0) nprog = i + 1;

            reset_mesh;
            dram_write_init;
            u_ag.load_image(PE_X, PE_Y, BUF_IMEM, nprog + 8, 0);
            sig_before = u_ag.sig_n;
            u_ag.kick(PE_X, PE_Y, 8'h71, 1'b0, 8'd1, 32'd0, 32'd0);
            repeat (200) @(posedge clk);

            cmd_n = 0;
            push_cmd(32'd1, 32'd5);
            push_cmd(32'd1, 32'd7);
            push_cmd(32'd1, 32'd11);
            push_cmd(32'd3, 32'd999);         // unknown command, ignored
            push_cmd(32'd1, 32'd13);
            push_cmd(32'd2, 32'd0);           // stop

            u_ag.wait_signal(sig_before + 1, 400000, ok);
            chk(ok, 1, "the command loop retired");
            if (ok) begin
                chk(u_ag.sig_code, 8'h00, "completion code");
                chk(u_ag.sig_arg, 32'd36, "the sum the command ring produced");
            end
        end
    endtask

    // ---- one user program, from tests/pe/tools/rv_run.py --------------------
    // A generated case's files and meta layout exactly, so nothing can disagree.
    task run_user;
        begin
            clear_arrays;
            $readmemh({`PE_DIR, "/sys/user/prog.hex"}, u_ag.img);
            $readmemh({`PE_DIR, "/sys/user/dram.hex"}, dinit);
            $readmemh({`PE_DIR, "/sys/user/dfin.hex"}, dfin);
            $readmemh({`PE_DIR, "/sys/user/spad.hex"}, sinit);
            $readmemh({`PE_DIR, "/sys/user/meta.hex"}, meta);
            nprog = meta[6];
            nspad = meta[7];

            reset_mesh;
            dram_write_init;
            u_ag.load_image(PE_X, PE_Y, BUF_IMEM, nprog, 0);
            if (nspad > 0) begin
                for (i = 0; i < IW; i = i + 1) u_ag.img[i] = 32'd0;
                for (i = 0; i < nspad; i = i + 1) u_ag.img[i] = sinit[i];
                u_ag.load_image(PE_X, PE_Y, BUF_SPAD, nspad, 0);
            end

            sig_before = u_ag.sig_n;
            u_ag.kick(PE_X, PE_Y, 8'hA0, 1'b0, 8'd1, 32'd0, meta[5]);
            u_ag.wait_signal(sig_before + 1, 300000, ok);

            chk(ok, 1, "the program retired");
            if (ok) begin
                // ws_guard polls once per clock, so it is the whole cost the
                // machine saw: fetch, misses, NoC round trips and drain.
                $display("    halt word      %08x   (model %08x)",
                         u_ag.sig_arg, meta[1]);
                $display("    halt cause     %0d          (model %0d)",
                         (u_ag.sig_code == 8'h00) ? 1 : 3, meta[0]);
                $display("    retired        %0d instructions in the model",
                         meta[2]);
                $display("    kick to done   %0d cycles", u_ag.ws_guard);
                chk(u_ag.sig_code, (meta[0] == 32'd1) ? 8'h00 : 8'h04,
                    "completion code");
                chk(u_ag.sig_arg, meta[1], "halt word");
                dram_checksum(dsum);
                chk(dsum, meta[3], "DRAM after the program");
                if (dsum !== meta[3]) begin
                    k = 0;
                    for (i = 0; i < DWORDS; i = i + 1)
                        if ((dram_word(i) !== dfin[i]) && (k < 16)) begin
                            $display("      dram word %0d (byte %0d): rtl %08x model %08x",
                                     i, i * 4, dram_word(i), dfin[i]);
                            k = k + 1;
                        end
                end
            end
        end
    endtask

    initial begin
`ifdef RV_USER
        // A missing image is all zeroes, which decodes as an illegal
        // instruction: without this the bench reports a fault nobody wrote.
        $readmemh({`PE_DIR, "/sys/user/meta.hex"}, meta);
        if (meta[6] == 32'd0) begin
            $display("  FAIL no user program at %s/sys/user -- rv_run.py writes it",
                     `PE_DIR);
            $display("========================================");
            $display("  FAIL -- 0 checks, 1 errors");
            $display("========================================");
            $finish;
        end
        $display("--- one user program through PE + router + MAG + RAM, %0d words ---",
                 meta[6]);
        run_user;
        $display("========================================");
        if (checks == 0)      $display("  FAIL -- the bench made no checks");
        else if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else                  $display("  FAIL -- %0d checks, %0d errors",
                                       checks, errors);
        $display("========================================");
        $finish;
`else
        ns[0] = 32'hxxxx_xxxx;
        $readmemh({`PE_DIR, "/sys/nsys.hex"}, ns);
        nsys = ns[0];
        if ((^ns[0] === 1'bx) || (nsys < 1) || (nsys > MAXSYS)) begin
            $display("  FAIL no system cases at %s/sys -- run python tests/pe/tools/rv_sys_gen.py",
                     `PE_DIR);
            $display("========================================");
            $display("  FAIL -- 0 checks, 1 errors");
            $display("========================================");
            $finish;
        end

        $display("--- %0d model-checked programs through PE + router + MAG + RAM ---",
                 nsys);
        for (c = 0; c < nsys; c = c + 1) begin
            run_sys(c);
            $display("    sys%02d  halt %08x  code %02h  %0s",
                     c, u_ag.sig_arg, u_ag.sig_code,
                     (errors == 0) ? "ok" : "see failures above");
        end

        $display("--- bench-driven: invalidate-all against a stale line ---");
        run_inval;
        $display("--- bench-driven: a command ring behind a doorbell ---");
        run_doorbell;

        $display("========================================");
        if (checks == 0)      $display("  FAIL -- the bench made no checks");
        else if (errors == 0) $display("  PASS -- %0d checks, 0 errors", checks);
        else                  $display("  FAIL -- %0d checks, %0d errors",
                                       checks, errors);
        $display("========================================");
        $finish;
`endif
    end

    initial begin
        #200000000;
        $display("  FAIL WATCHDOG -- the integration bench never finished");
        $display("========================================");
        $finish;
    end

endmodule

`default_nettype wire

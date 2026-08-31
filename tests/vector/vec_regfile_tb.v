// One register-file slice, and then sixteen wired the way vec_lanes wires them.
//
// Two separate claims, because a failure in vec_lanes could not distinguish
// them: that a slice stores and returns what it was given on all three read
// ports, and that connecting sixteen slices through part-selects of one packed
// bus keeps each slice's data in its own field.

`default_nettype none
`timescale 1ns/1ps

module vec_regfile_tb;
    reg clk = 0;
    always begin
        #2 clk = ~clk;
    end

    integer errors = 0, checks = 0;

    task chk(input [23:0] got, input [23:0] want, input [255:0] what,
             input integer where);
        begin
            checks = checks + 1;
            if (got !== want) begin
                errors = errors + 1;
                if (errors < 20) begin
                    $display("  FAIL %0s [%0d]: got %06h want %06h",
                             what, where, got, want);
                end
            end
        end
    endtask

    // ---------------- one slice ----------------
    reg         we;
    reg  [6:0]  wa, ra, rb, rc;
    reg  [23:0] wd;
    wire [23:0] qa, qb, qc;

    vec_regfile #(.AW(7), .DW(24), .PRIM("distributed")) u_one (
        .clk(clk), .wr_en(we), .wr_addr(wa), .wr_data(wd),
        .ra_addr(ra), .rb_addr(rb), .rc_addr(rc),
        .ra_data(qa), .rb_data(qb), .rc_data(qc)
    );

    // ---------------- sixteen, packed exactly as vec_lanes does ----------------
    reg  [15:0]  m_we;
    reg  [6:0]   m_wa;
    reg  [383:0] m_wd;
    reg  [6:0]   m_ra;
    wire [383:0] m_qa, m_qb, m_qc;

    genvar s;
    generate
    for (s = 0; s < 16; s = s + 1) begin : g_rf
        vec_regfile #(.AW(7), .DW(24), .PRIM("distributed")) u_rf (
            .clk(clk),
            .wr_en(m_we[s]), .wr_addr(m_wa), .wr_data(m_wd[s*24 +: 24]),
            .ra_addr(m_ra), .rb_addr(m_ra), .rc_addr(m_ra),
            .ra_data(m_qa[s*24 +: 24]),
            .rb_data(m_qb[s*24 +: 24]),
            .rc_data(m_qc[s*24 +: 24])
        );
    end
    endgenerate

    // ---------------- the same sixteen, padded to 36 and packed two per word ----
    // Both shapes must be indistinguishable from the flat one at the ports:
    // same data back, and lane s's write enable touching lane s alone.
    wire [383:0] p_qa, p_qb, p_qc;
    wire [383:0] k_qa, k_qb, k_qc;

    generate
    for (s = 0; s < 16; s = s + 1) begin : g_pad
        vec_regfile #(.AW(7), .DW(24), .PRIM("distributed"), .PAD_W(36)) u_rf (
            .clk(clk),
            .wr_en(m_we[s]), .wr_addr(m_wa), .wr_data(m_wd[s*24 +: 24]),
            .ra_addr(m_ra), .rb_addr(m_ra), .rc_addr(m_ra),
            .ra_data(p_qa[s*24 +: 24]),
            .rb_data(p_qb[s*24 +: 24]),
            .rc_data(p_qc[s*24 +: 24])
        );
    end
    for (s = 0; s < 8; s = s + 1) begin : g_pack
        vec_regfile #(.AW(7), .DW(24), .PRIM("distributed"), .LANES(2)) u_rf (
            .clk(clk),
            .wr_en(m_we[2*s +: 2]), .wr_addr(m_wa), .wr_data(m_wd[s*48 +: 48]),
            .ra_addr(m_ra), .rb_addr(m_ra), .rc_addr(m_ra),
            .ra_data(k_qa[s*48 +: 48]),
            .rb_data(k_qb[s*48 +: 48]),
            .rc_data(k_qc[s*48 +: 48])
        );
    end
    endgenerate

    integer i;
    reg [6:0] aw;
    reg [3:0] ix;

    initial begin
        we = 0; wa = 0; ra = 0; rb = 0; rc = 0; wd = 0;
        m_we = 0; m_wa = 0; m_wd = 0; m_ra = 0;
        repeat (4) @(negedge clk);

        $display("--- 1. one slice, 128 entries, three read ports ---");
        for (i = 0; i < 128; i = i + 1) begin
            @(negedge clk);
            we = 1'b1; wa = i[6:0]; wd = 24'h010000 + i[23:0];
        end
        @(negedge clk); we = 1'b0;

        for (i = 0; i < 128; i = i + 1) begin
            @(negedge clk);
            ra = i[6:0];
            rb = (127 - i);
            rc = (i * 7) % 128;
            @(negedge clk);
            chk(qa, 24'h010000 + i[23:0],            "port A", i);
            chk(qb, 24'h010000 + (127 - i),          "port B", i);
            chk(qc, 24'h010000 + ((i * 7) % 128),    "port C", i);
        end

        $display("--- 2. sixteen slices behind one packed bus ---");
        // Slice s entry k gets a value that depends on BOTH, so a broadcast
        // from one slice or one address cannot pass.
        // Data, address and enable are all set AFTER the edge and held across
        // the next one, matching section 1. Setting data before the edge and
        // the enable after it writes the following iteration's data.
        // Address and index are SIZED regs, and the value is built by
        // concatenation rather than by arithmetic on bare `integer`s.
        for (aw = 0; aw < 8; aw = aw + 1) begin
            @(negedge clk);
            for (i = 0; i < 16; i = i + 1) begin
                ix = i[3:0];
                m_wd[i*24 +: 24] = 24'h020000 + {16'd0, aw[3:0], ix};
            end
            m_we = 16'hFFFF;
            m_wa = {3'd0, aw[3:0]};
        end
        @(negedge clk); m_we = 16'd0;

        for (aw = 0; aw < 8; aw = aw + 1) begin
            @(negedge clk);
            m_ra = {3'd0, aw[3:0]};
            @(negedge clk);
            for (i = 0; i < 16; i = i + 1) begin
                ix = i[3:0];
                chk(m_qa[i*24 +: 24], 24'h020000 + {16'd0, aw[3:0], ix},
                    "packed A", i);
                chk(m_qc[i*24 +: 24], 24'h020000 + {16'd0, aw[3:0], ix},
                    "packed C", i);
                chk(p_qb[i*24 +: 24], 24'h020000 + {16'd0, aw[3:0], ix},
                    "pad36 B", i);
                chk(p_qc[i*24 +: 24], 24'h020000 + {16'd0, aw[3:0], ix},
                    "pad36 C", i);
                chk(k_qa[i*24 +: 24], 24'h020000 + {16'd0, aw[3:0], ix},
                    "pack2 A", i);
                chk(k_qb[i*24 +: 24], 24'h020000 + {16'd0, aw[3:0], ix},
                    "pack2 B", i);
                chk(k_qc[i*24 +: 24], 24'h020000 + {16'd0, aw[3:0], ix},
                    "pack2 C", i);
            end
        end

        $display("--- 3. per-slice write enables must be independent ---");
        m_wd = 384'd0;
        for (i = 0; i < 16; i = i + 1) begin
            m_wd[i*24 +: 24] = 24'h0AAAAA;
        end
        @(negedge clk);
        m_we = 16'h00FF; m_wa = 7'd0;
        @(negedge clk); m_we = 16'd0;
        @(negedge clk); m_ra = 7'd0;
        @(negedge clk);
        for (i = 0; i < 16; i = i + 1) begin
            ix = i[3:0];
            chk(m_qa[i*24 +: 24],
                (i < 8) ? 24'h0AAAAA : (24'h020000 + {20'd0, ix}),
                "selective write", i);
            chk(p_qb[i*24 +: 24],
                (i < 8) ? 24'h0AAAAA : (24'h020000 + {20'd0, ix}),
                "selective write pad36", i);
            chk(k_qb[i*24 +: 24],
                (i < 8) ? 24'h0AAAAA : (24'h020000 + {20'd0, ix}),
                "selective write pack2", i);
        end

        $display("--- 4. a packed pair: the ODD lane alone ---");
        // Lane 9 written, lane 8 (its word-mate) untouched: the strobe groups
        // split the word where the lanes meet, not somewhere inside one.
        for (i = 0; i < 16; i = i + 1) begin
            m_wd[i*24 +: 24] = 24'h055555;
        end
        @(negedge clk);
        m_we = 16'h0200; m_wa = 7'd0;
        @(negedge clk); m_we = 16'd0;
        @(negedge clk); m_ra = 7'd0;
        @(negedge clk);
        chk(k_qb[8*24 +: 24], 24'h020000 + {20'd0, 4'd8}, "pack2 lane 8 kept", 8);
        chk(k_qb[9*24 +: 24], 24'h055555, "pack2 lane 9 written", 9);
        chk(k_qc[9*24 +: 24], 24'h055555, "pack2 lane 9 written C", 9);
        chk(k_qa[9*24 +: 24], 24'h055555, "pack2 lane 9 written A", 9);
        chk(m_qa[8*24 +: 24], 24'h020000 + {20'd0, 4'd8}, "flat lane 8 kept", 8);

        $display("========================================");
        if (errors == 0) begin
            $display("  PASS -- %0d checks, 0 errors", checks);
        end
        else begin
            $display("  FAIL -- %0d checks, %0d errors", checks, errors);
        end
        $display("========================================");
        $finish;
    end

    initial begin
        #200000;
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire

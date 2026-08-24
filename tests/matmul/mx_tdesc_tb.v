// Tensor descriptor walker: address sequence and padding, against a software
// model of the same affine map.
//
// Two shapes, because they are the two the ISA claims to unify:
//
//   1. a 2D matmul operand tile   -- the easy case, no bounds
//   2. a conv2d im2col walk       -- 6 dims, two bound axes, real padding
//
// If (2) is right then convolution really is a memory request, which is the
// central claim of docs/compute/tensor-isa.md.

`default_nettype none
`timescale 1ns/1ps

module mx_tdesc_tb;

    localparam integer NDIM = 6;
    localparam integer AW   = 40;
    localparam integer CW   = 16;
    localparam integer SW   = 32;
    localparam integer XW   = 16;

    reg clk = 0, rst = 1;
    always begin
        #2 clk = ~clk;
    end

    reg               ld_dim_en = 0, ld_hdr_en = 0, ld_ax_en = 0;
    reg  [2:0]        ld_dim = 0, ld_ndim = 1;
    reg  [CW-1:0]     ld_count = 0;
    reg  signed [SW-1:0] ld_stride = 0;
    reg  [1:0]        ld_axis = 0;
    reg  signed [XW-1:0] ld_astep = 0;
    reg  [AW-1:0]     ld_base = 0;
    reg               ld_ax_sel = 0;
    reg  signed [XW-1:0] ld_abase = 0;
    reg  [XW-1:0]     ld_aext = 0;
    reg               start = 0, next = 0;

    wire              active, last, valid;
    wire [AW-1:0]     addr;

    mx_tdesc #(.NDIM(NDIM), .AW(AW), .CW(CW), .SW(SW), .XW(XW)) dut (
        .clk(clk), .rst(rst),
        .ld_dim_en(ld_dim_en), .ld_dim(ld_dim), .ld_count(ld_count),
        .ld_stride(ld_stride), .ld_axis(ld_axis), .ld_astep(ld_astep),
        .ld_hdr_en(ld_hdr_en), .ld_base(ld_base), .ld_ndim(ld_ndim),
        .ld_ax_en(ld_ax_en), .ld_ax_sel(ld_ax_sel),
        .ld_abase(ld_abase), .ld_aext(ld_aext),
        .start(start), .next(next),
        .active(active), .last(last), .valid(valid), .addr(addr)
    );

    integer errors = 0, checks = 0;

    task set_dim(input [2:0] d, input [CW-1:0] cnt,
                 input signed [SW-1:0] str, input [1:0] ax,
                 input signed [XW-1:0] ast);
        begin
            @(negedge clk);
            ld_dim_en <= 1'b1; ld_dim <= d; ld_count <= cnt;
            ld_stride <= str; ld_axis <= ax; ld_astep <= ast;
            @(negedge clk);
            ld_dim_en <= 1'b0;
        end
    endtask

    task set_hdr(input [AW-1:0] b, input [2:0] nd);
        begin
            @(negedge clk);
            ld_hdr_en <= 1'b1; ld_base <= b; ld_ndim <= nd;
            @(negedge clk);
            ld_hdr_en <= 1'b0;
        end
    endtask

    task set_axis(input sel, input signed [XW-1:0] ab, input [XW-1:0] ae);
        begin
            @(negedge clk);
            ld_ax_en <= 1'b1; ld_ax_sel <= sel; ld_abase <= ab; ld_aext <= ae;
            @(negedge clk);
            ld_ax_en <= 1'b0;
        end
    endtask

    task chk(input [AW-1:0] got_a, input got_v,
             input [AW-1:0] want_a, input want_v, input [255:0] what);
        begin
            checks = checks + 1;
            if (got_v !== want_v) begin
                errors = errors + 1;
                if (errors <= 10) begin
                    $display("  FAIL %0s valid got %0d want %0d", what, got_v, want_v);
                end
            end else if (want_v && (got_a !== want_a)) begin
                errors = errors + 1;
                if (errors <= 10) begin
                    $display("  FAIL %0s addr got %0d want %0d", what, got_a, want_a);
                end
            end
        end
    endtask

    // ---- conv geometry ----
    localparam integer CN = 1, CH = 4, CW_ = 4, CC = 2;
    localparam integer KH = 3, KW = 3, ST = 1, PD = 1;
    localparam integer OH = 4, OW = 4;
    localparam integer sC = 1, sW = CC, sH = CW_*CC, sN = CH*CW_*CC;
    localparam integer TBASE = 4096;

    integer n, oy, ox, ky, kx, c;
    integer g, kw2, i;
    integer hpos, wpos;
    integer nvalid, npad;
    // AW bits wide, not `integer`: an integer is 32 bits, so want_addr[AW-1:0]
    // reads past the end and yields X. Same family as the unsized-literal trap.
    reg [AW-1:0] want_addr;
    reg     want_valid;

    initial begin
        #20; @(negedge clk); rst = 0; @(negedge clk);

        // =============================================================
        $display("--- 1. 2D operand tile: 8 row groups x 4 K words ---");
        set_hdr(40'd0, 3'd2);
        // dims are outermost-first; NDIM-1 is innermost
        for (i = 0; i < 4; i = i + 1) begin
            set_dim(i[2:0], 16'd1, 32'sd0, 2'd0, 16'sd0);
        end
        set_dim(3'd4, 16'd8, 32'sd128, 2'd0, 16'sd0);   // row group
        set_dim(3'd5, 16'd4, 32'sd32,  2'd0, 16'sd0);   // K word
        set_hdr(40'd0, 3'd6);
        set_axis(1'b0, 16'sd0, 16'd0);
        set_axis(1'b1, 16'sd0, 16'd0);

        @(negedge clk); start <= 1'b1; @(negedge clk); start <= 1'b0;
        for (g = 0; g < 8; g = g + 1) begin
            for (kw2 = 0; kw2 < 4; kw2 = kw2 + 1) begin
                chk(addr, valid, g*128 + kw2*32, 1'b1, "2d");
                @(negedge clk); next <= 1'b1; @(negedge clk); next <= 1'b0;
            end
        end
        checks = checks + 1;
        if (active !== 1'b0) begin
            errors = errors + 1;
            $display("  FAIL 2d walk did not terminate after 32 elements");
        end

        // =============================================================
        $display("--- 2. conv2d im2col, 3x3 stride 1 pad 1, with bounds ---");
        // base is offset by -P*(sH+sW): the axis carries the padding, the
        // address does not, so the origin absorbs it
        set_hdr(TBASE - PD*sH - PD*sW, 3'd6);
        set_dim(3'd0, CN[15:0],  ST*sN,  2'd0, 16'sd0);      // n
        set_dim(3'd1, OH[15:0],  ST*sH,  2'd1, ST[15:0]);    // oy -> axis H
        set_dim(3'd2, OW[15:0],  ST*sW,  2'd2, ST[15:0]);    // ox -> axis W
        set_dim(3'd3, KH[15:0],  sH,     2'd1, 16'sd1);      // ky -> axis H
        set_dim(3'd4, KW[15:0],  sW,     2'd2, 16'sd1);      // kx -> axis W
        set_dim(3'd5, CC[15:0],  sC,     2'd0, 16'sd0);      // c
        set_axis(1'b0, -PD[15:0], CH[15:0]);                 // H in [0, CH)
        set_axis(1'b1, -PD[15:0], CW_[15:0]);                // W in [0, CW_)

        @(negedge clk); start <= 1'b1; @(negedge clk); start <= 1'b0;

        nvalid = 0; npad = 0;
        for (n = 0; n < CN; n = n + 1) begin
            for (oy = 0; oy < OH; oy = oy + 1) begin
                for (ox = 0; ox < OW; ox = ox + 1) begin
                    for (ky = 0; ky < KH; ky = ky + 1) begin
                        for (kx = 0; kx < KW; kx = kx + 1) begin
                            for (c = 0; c < CC; c = c + 1) begin
                                hpos = oy*ST + ky - PD;
                                wpos = ox*ST + kx - PD;
                                want_valid = (hpos >= 0) && (hpos < CH) && (wpos >= 0) && (wpos < CW_);
                                want_addr  = TBASE + n*sN + hpos*sH + wpos*sW + c*sC;
                                if (want_valid) begin
                                    nvalid = nvalid + 1;
                                end
                                else begin
                                    npad = npad + 1;
                                end
                                chk(addr, valid, want_addr, want_valid, "conv");
                                @(negedge clk); next <= 1'b1; @(negedge clk); next <= 1'b0;
                            end
                        end
                    end
                end
            end
        end

        $display("    %0d elements: %0d in bounds, %0d padded",
                 nvalid + npad, nvalid, npad);
        checks = checks + 1;
        if (active !== 1'b0) begin
            errors = errors + 1;
            $display("  FAIL conv walk did not terminate");
        end

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
        #500000;
        $display("  FAIL -- watchdog");
        $finish;
    end

endmodule

`default_nettype wire

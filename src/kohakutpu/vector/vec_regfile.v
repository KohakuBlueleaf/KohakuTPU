// One lane's slice of the vector register file: 3 read ports, 1 write port.
//
// docs/compute/vector-core.md s8. The file is striped BY LANE -- lane i holds
// elements i, i+16, i+32 -- so a slice is entirely lane-local and 3R1W is three
// duplicated single-read arrays rather than a 48-port monolith. Writes go to all
// three copies; each answers one port.
//
//   16 vector registers x 8 elements per lane = 128 entries of 24 bit
//   address = {vreg[3:0], chunk[2:0]}
//
// READ_LAT = 1, so an address issued in cycle N presents data in cycle N+1.
// That number is load-bearing for the sequencer's issue pipeline, which is why
// the primitive is named rather than inferred -- see kohaku_sdpram.v.
//
// "block", not "distributed": as LUTRAM this cost 168 LUT per slice, 2,688
// across the lanes. One RAMB18 per lane per block copy is the floor -- the
// copies cannot share a primitive (a RAMB36 pair of ports is 2 x 36 b and the
// write takes one), and RAMB write enables are 9-bit groups, so packing two
// 27-bit lanes into one word (LANES 2) lands on the same 16 tiles per core.
// PAD_W 36 names the RAMB18 36 x 512 word explicitly; measured, it changes
// nothing. Both stay as knobs; the default is today's netlist.
//
// PORT A IS THE EXCEPTION AND STAYS LUTRAM. b and c feed ALU operands, which
// get a whole cycle. a also feeds `ls_rdata` -> the store converters, and a
// RAMB18's CLKARDCLK -> DO (~1.5 ns on -2L) in series with a 16-lane E8->FP16
// normalise measured 286.0 MHz -- below the 300 floor. Registering between the
// two would cost a cycle on every VST beat, on a machine that is drain-bound.

`default_nettype none

module vec_regfile #(
    parameter integer AW  = 7,
    parameter integer DW  = 24,
    parameter         PRIM = "distributed",
    // Port A alone, because it is the only one with a consumer that cannot
    // afford a block RAM's clock-to-out -- see the note above.
    parameter         PRIM_A = "distributed",
    parameter integer PAD_W  = DW,
    parameter integer LANES  = 1
)(
    input  wire                clk,

    input  wire [LANES-1:0]    wr_en,
    input  wire [AW-1:0]       wr_addr,
    input  wire [LANES*DW-1:0] wr_data,

    input  wire [AW-1:0]       ra_addr,
    input  wire [AW-1:0]       rb_addr,
    input  wire [AW-1:0]       rc_addr,
    output wire [LANES*DW-1:0] ra_data,
    output wire [LANES*DW-1:0] rb_data,
    output wire [LANES*DW-1:0] rc_data
);
    localparam integer DEPTH = (1 << AW);
    localparam integer GW    = ((DW + 8) / 9) * 9;   // one lane, whole strobe groups
    localparam integer PW    = LANES * GW;            // the packed b/c word
    localparam integer NSB   = PW / 9;

    genvar l;
    generate
    for (l = 0; l < LANES; l = l + 1) begin : g_a
        kohaku_sdpram #(.WIDTH(DW), .DEPTH(DEPTH), .MEM_PRIM(PRIM_A), .READ_LAT(1))
        u_a (.clk(clk), .wr_en(wr_en[l]), .wr_addr(wr_addr),
             .wr_data(wr_data[l*DW +: DW]),
             .rd_en(1'b1), .rd_addr(ra_addr), .rd_data(ra_data[l*DW +: DW]));
    end

    if (LANES == 1) begin : g_one
        if (PAD_W == DW) begin : g_flat
            kohaku_sdpram #(.WIDTH(DW), .DEPTH(DEPTH), .MEM_PRIM(PRIM), .READ_LAT(1))
            u_b (.clk(clk), .wr_en(wr_en[0]), .wr_addr(wr_addr), .wr_data(wr_data),
                 .rd_en(1'b1), .rd_addr(rb_addr), .rd_data(rb_data));

            kohaku_sdpram #(.WIDTH(DW), .DEPTH(DEPTH), .MEM_PRIM(PRIM), .READ_LAT(1))
            u_c (.clk(clk), .wr_en(wr_en[0]), .wr_addr(wr_addr), .wr_data(wr_data),
                 .rd_en(1'b1), .rd_addr(rc_addr), .rd_data(rc_data));
        end
        else begin : g_pad
            wire [PAD_W-1:0] wd = {{(PAD_W-DW){1'b0}}, wr_data};
            wire [PAD_W-1:0] qb, qc;

            kohaku_sdpram #(.WIDTH(PAD_W), .DEPTH(DEPTH), .MEM_PRIM(PRIM), .READ_LAT(1))
            u_b (.clk(clk), .wr_en(wr_en[0]), .wr_addr(wr_addr), .wr_data(wd),
                 .rd_en(1'b1), .rd_addr(rb_addr), .rd_data(qb));

            kohaku_sdpram #(.WIDTH(PAD_W), .DEPTH(DEPTH), .MEM_PRIM(PRIM), .READ_LAT(1))
            u_c (.clk(clk), .wr_en(wr_en[0]), .wr_addr(wr_addr), .wr_data(wd),
                 .rd_en(1'b1), .rd_addr(rc_addr), .rd_data(qc));

            assign rb_data = qb[DW-1:0];
            assign rc_data = qc[DW-1:0];
        end
    end
    else begin : g_pack
        wire [PW-1:0]  wd;
        wire [NSB-1:0] strb;
        wire [PW-1:0]  qb, qc;

        for (l = 0; l < LANES; l = l + 1) begin : g_l
            if (GW == DW) begin : g_exact
                assign wd[l*GW +: GW] = wr_data[l*DW +: DW];
            end
            else begin : g_round
                assign wd[l*GW +: GW] = {{(GW-DW){1'b0}}, wr_data[l*DW +: DW]};
            end
            assign strb[l*(GW/9) +: GW/9] = {(GW/9){wr_en[l]}};
            assign rb_data[l*DW +: DW]    = qb[l*GW +: DW];
            assign rc_data[l*DW +: DW]    = qc[l*GW +: DW];
        end

        kohaku_sdpram_be #(.WIDTH(PW), .DEPTH(DEPTH), .BYTE_W(9), .MEM_PRIM(PRIM),
                           .READ_LAT(1))
        u_b (.clk(clk), .wr_en(|wr_en), .wr_strb(strb), .wr_addr(wr_addr),
             .wr_data(wd), .rd_en(1'b1), .rd_addr(rb_addr), .rd_data(qb));

        kohaku_sdpram_be #(.WIDTH(PW), .DEPTH(DEPTH), .BYTE_W(9), .MEM_PRIM(PRIM),
                           .READ_LAT(1))
        u_c (.clk(clk), .wr_en(|wr_en), .wr_strb(strb), .wr_addr(wr_addr),
             .wr_data(wd), .rd_en(1'b1), .rd_addr(rc_addr), .rd_data(qc));
    end
    endgenerate

endmodule

`default_nettype wire

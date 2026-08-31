// Width conversion on a surface: a receiving end at WI, a sending end at WO,
// and a per-VC shift between them. A wide flit leaves as ceil(WI/WO) narrow
// flits (`last` on the final one); narrow flits gather into a wide one, the
// tail zero-padded when `last` arrives early. Each side has its own credits,
// so this is a repeater as well: the upstream wire and the downstream wire
// are independent in length.

`default_nettype none

module kts_wconv #(
    parameter integer WI    = 288,
    parameter integer WO    = 144,
    parameter integer VC    = 2,
    parameter integer D     = 32,              // receive depth per VC (WI flits)
    parameter integer CMAX  = 64,              // credits the downstream may grant
    parameter integer CN_W  = 4,
    parameter         MEM   = "distributed",
    parameter integer VCW   = (VC <= 1) ? 1 : $clog2(VC)
)(
    input  wire              clk,
    input  wire              rst,

    input  wire              i_valid,
    input  wire [VCW-1:0]    i_vc,
    input  wire              i_last,
    input  wire [WI-1:0]     i_flit,
    output wire              i_crd_valid,
    output wire [VCW-1:0]    i_crd_vc,
    output wire [CN_W-1:0]   i_crd_n,

    output wire              o_valid,
    output wire [VCW-1:0]    o_vc,
    output wire              o_last,
    output wire [WO-1:0]     o_flit,
    input  wire              o_crd_valid,
    input  wire [VCW-1:0]    o_crd_vc,
    input  wire [CN_W-1:0]   o_crd_n
);
    // ---- the receiving end ----------------------------------------------------
    wire [VC-1:0]    rv, rl, pop;
    wire [VC*WI-1:0] rf;
    kts_rx #(.W(WI), .VC(VC), .D(D), .CN_W(CN_W), .MEM(MEM)) u_rx (
        .clk(clk), .rst(rst),
        .rx_valid(i_valid), .rx_vc(i_vc), .rx_last(i_last), .rx_flit(i_flit),
        .out_valid(rv), .out_last(rl), .out_flit(rf), .out_pop(pop),
        .crd_valid(i_crd_valid), .crd_vc(i_crd_vc), .crd_n(i_crd_n)
    );

    // ---- the shift, per VC ----------------------------------------------------
    wire [VC-1:0]    tv, tl, take;
    wire [VC*WO-1:0] tf;

    genvar g;
    generate
    for (g = 0; g < VC; g = g + 1) begin : g_vc
        if (WI == WO) begin : g_eq
            assign tv[g]            = rv[g];
            assign tl[g]            = rl[g];
            assign tf[g*WO +: WO]   = rf[g*WI +: WI];
            assign pop[g]           = take[g];
        end
        else if (WI > WO) begin : g_down
            // K narrow flits per wide flit, index `ix`; the wide flit stays at
            // the FIFO head until its last piece has gone.
            localparam integer K  = (WI + WO - 1) / WO;
            localparam integer KW = (K <= 1) ? 1 : $clog2(K);
            localparam integer PW = K * WO;
            reg  [KW-1:0]  ix;
            wire [PW-1:0]  padded = {{(PW-WI){1'b0}}, rf[g*WI +: WI]};
            wire           fin    = (ix == K - 1);
            // A compare per piece, not `padded[ix*WO +: WO]`: the dynamic
            // part-select builds a barrel select across the whole wide flit.
            reg  [WO-1:0]  piece;
            integer q;
            always @(*) begin
                piece = padded[0 +: WO];
                for (q = 1; q < K; q = q + 1) begin
                    if (ix == q[KW-1:0]) begin
                        piece = padded[q*WO +: WO];
                    end
                end
            end
            assign tv[g]          = rv[g];
            assign tl[g]          = rl[g] && fin;
            assign tf[g*WO +: WO] = piece;
            assign pop[g]         = take[g] && fin;
            always @(posedge clk) begin
                if (rst) begin
                    ix <= {KW{1'b0}};
                end
                else if (take[g]) begin
                    ix <= fin ? {KW{1'b0}} : ix + 1'b1;
                end
            end
        end
        else begin : g_up
            // K narrow flits gather into one wide flit; `last` closes it early,
            // the rest of the wide flit is zero.
            localparam integer K  = (WO + WI - 1) / WI;
            localparam integer KW = (K <= 1) ? 1 : $clog2(K);
            reg  [KW-1:0]  ix;
            reg  [WO-1:0]  acc;
            reg            full;             // a wide flit is complete
            reg            acc_last;
            wire           closing = rv[g] && ((ix == K - 1) || rl[g]);
            // gather while not holding a completed flit, or as it leaves
            assign pop[g] = rv[g] && (!full || take[g]);
            assign tv[g]  = full;
            assign tl[g]  = acc_last;
            assign tf[g*WO +: WO] = acc;
            integer p;
            always @(posedge clk) begin
                if (rst) begin
                    ix       <= {KW{1'b0}};
                    full     <= 1'b0;
                    acc      <= {WO{1'b0}};
                    acc_last <= 1'b0;
                end
                else begin
                    if (full && take[g]) begin
                        full <= 1'b0;
                        acc  <= {WO{1'b0}};
                    end
                    if (pop[g]) begin
                        for (p = 0; p < K; p = p + 1) begin
                            if (p == ix) begin
                                acc[p*WI +: WI] <= rf[g*WI +: WI];
                            end
                        end
                        ix       <= closing ? {KW{1'b0}} : ix + 1'b1;
                        full     <= closing;
                        acc_last <= rl[g];
                    end
                end
            end
        end
    end
    endgenerate

    // ---- the sending end ------------------------------------------------------
    wire [VC*($clog2(CMAX)+1)-1:0] credits_unused;
    kts_tx #(.W(WO), .VC(VC), .CMAX(CMAX), .CN_W(CN_W)) u_tx (
        .clk(clk), .rst(rst),
        .req_valid(tv), .req_last(tl), .req_flit(tf), .req_take(take),
        .tx_valid(o_valid), .tx_vc(o_vc), .tx_last(o_last), .tx_flit(o_flit),
        .crd_valid(o_crd_valid), .crd_vc(o_crd_vc), .crd_n(o_crd_n),
        .credits(credits_unused)
    );

endmodule

`default_nettype wire

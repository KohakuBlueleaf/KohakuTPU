// 5-port mesh router. See docs/noc/spec.md.
//
// FIFO_DEPTH only has to cover the backpressure round-trip, so 32 in distributed
// RAM rather than a URAM-backed 4096: depth does not prevent deadlock (XY routing
// does), and an SRL32 costs ~1 LUT/bit at any depth up to 32. Measured on
// xcvu13p: 15 URAM -> 0, for +400 LUT per router.
module NoCRouter #(
    parameter DATA_WIDTH  = 288,
    parameter FIFO_DEPTH  = 32,
    parameter MEMORY_TYPE = "distributed",
    parameter POS_WIDTH   = 4,
    parameter POS_X       = 1,
    parameter POS_Y       = 1,
    parameter GRID_LO     = 1,
    parameter GRID_HI     = 14
)(
    input clk,
    input rst,
    
    // North Port
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_in" *)
    input wire [DATA_WIDTH-1:0] north_in_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_in" *)
    input wire north_in_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_in" *)
    output wire north_in_busy,

    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_out" *)
    output wire [DATA_WIDTH-1:0] north_out_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_out" *)
    output wire north_out_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP north_out" *)
    input wire north_out_busy,

    // East Port
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_in" *)
    input wire [DATA_WIDTH-1:0] east_in_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_in" *)
    input wire east_in_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_in" *)
    output wire east_in_busy,

    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_out" *)
    output wire [DATA_WIDTH-1:0] east_out_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_out" *)
    output wire east_out_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP east_out" *)
    input wire east_out_busy,

    // South Port
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_in" *)
    input wire [DATA_WIDTH-1:0] south_in_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_in" *)
    input wire south_in_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_in" *)
    output wire south_in_busy,

    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_out" *)
    output wire [DATA_WIDTH-1:0] south_out_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_out" *)
    output wire south_out_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP south_out" *)
    input wire south_out_busy,

    // West Port
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_in" *)
    input wire [DATA_WIDTH-1:0] west_in_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_in" *)
    input wire west_in_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_in" *)
    output wire west_in_busy,

    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_out" *)
    output wire [DATA_WIDTH-1:0] west_out_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_out" *)
    output wire west_out_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP west_out" *)
    input wire west_out_busy,

    // Local Port
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_in" *)
    input wire [DATA_WIDTH-1:0] local_in_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_in" *)
    input wire local_in_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "slave" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_in" *)
    output wire local_in_busy,

    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_out" *)
    output wire [DATA_WIDTH-1:0] local_out_data,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_out" *)
    output wire local_out_valid,
    (* X_INTERFACE_INFO = "kblueleaf.net:user:HakuNoCPort:1.0" *)
    (* X_INTERFACE_MODE = "master" *)
    (* X_INTERFACE_PARAMETER = "GROUP local_out" *)
    input wire local_out_busy
);
    wire [DATA_WIDTH-1:0] n2n, n2e, n2s, n2w, n2l;
    wire [DATA_WIDTH-1:0] e2n, e2e, e2s, e2w, e2l;
    wire [DATA_WIDTH-1:0] s2n, s2e, s2s, s2w, s2l;
    wire [DATA_WIDTH-1:0] w2n, w2e, w2s, w2w, w2l;
    wire [DATA_WIDTH-1:0] l2n, l2e, l2s, l2w, l2l;

    wire n2nv, n2ev, n2sv, n2wv, n2lv;
    wire e2nv, e2ev, e2sv, e2wv, e2lv;
    wire s2nv, s2ev, s2sv, s2wv, s2lv;
    wire w2nv, w2ev, w2sv, w2wv, w2lv;
    wire l2nv, l2ev, l2sv, l2wv, l2lv;

    wire n2nc, n2ec, n2sc, n2wc, n2lc;
    wire e2nc, e2ec, e2sc, e2wc, e2lc;
    wire s2nc, s2ec, s2sc, s2wc, s2lc;
    wire w2nc, w2ec, w2sc, w2wc, w2lc;
    wire l2nc, l2ec, l2sc, l2wc, l2lc;

    InPortSwitch #(
        .DATA_WIDTH(DATA_WIDTH),
        .FIFO_DEPTH(FIFO_DEPTH),
        .MEMORY_TYPE(MEMORY_TYPE),
        .POS_WIDTH(POS_WIDTH),
        .POS_X(POS_X),
        .POS_Y(POS_Y),
        .GRID_LO(GRID_LO),
        .GRID_HI(GRID_HI)
    ) north_in_switch (
        .clk(clk),
        .rst(rst),
        .data_in(north_in_data),
        .data_valid(north_in_valid),
        .port_busy(north_in_busy),
        .port_out({n2l, n2w, n2s, n2e, n2n}),
        .port_valid({n2lv, n2wv, n2sv, n2ev, n2nv}),
        .clear({n2lc, n2wc, n2sc, n2ec, n2nc})
    ),
    east_in_switch (
        .clk(clk),
        .rst(rst),
        .data_in(east_in_data),
        .data_valid(east_in_valid),
        .port_busy(east_in_busy),
        .port_out({e2l, e2w, e2s, e2e, e2n}),
        .port_valid({e2lv, e2wv, e2sv, e2ev, e2nv}),
        .clear({e2lc, e2wc, e2sc, e2ec, e2nc})
    ),
    south_in_switch (
        .clk(clk),
        .rst(rst),
        .data_in(south_in_data),
        .data_valid(south_in_valid),
        .port_busy(south_in_busy),
        .port_out({s2l, s2w, s2s, s2e, s2n}),
        .port_valid({s2lv, s2wv, s2sv, s2ev, s2nv}),
        .clear({s2lc, s2wc, s2sc, s2ec, s2nc})
    ),
    west_in_switch (
        .clk(clk),
        .rst(rst),
        .data_in(west_in_data),
        .data_valid(west_in_valid),
        .port_busy(west_in_busy),
        .port_out({w2l, w2w, w2s, w2e, w2n}),
        .port_valid({w2lv, w2wv, w2sv, w2ev, w2nv}),
        .clear({w2lc, w2wc, w2sc, w2ec, w2nc})
    ),
    local_in_switch (
        .clk(clk),
        .rst(rst),
        .data_in(local_in_data),
        .data_valid(local_in_valid),
        .port_busy(local_in_busy),
        .port_out({l2l, l2w, l2s, l2e, l2n}),
        .port_valid({l2lv, l2wv, l2sv, l2ev, l2nv}),
        .clear({l2lc, l2wc, l2sc, l2ec, l2nc})
    );
    
    OutPortSwitch #(
        .DATA_WIDTH(DATA_WIDTH)
    ) north_out_switch (
        .clk(clk),
        .rst(rst),
        .in_ports({l2n, w2n, s2n, e2n, n2n}),
        .ports_valid({l2nv, w2nv, s2nv, e2nv, n2nv}),
        .ports_clear({l2nc, w2nc, s2nc, e2nc, n2nc}),
        .port_out(north_out_data),
        .out_valid(north_out_valid),
        .busy(north_out_busy)
    ),
    east_out_switch (
        .clk(clk),
        .rst(rst),
        .in_ports({l2e, w2e, s2e, e2e, n2e}),
        .ports_valid({l2ev, w2ev, s2ev, e2ev, n2ev}),
        .ports_clear({l2ec, w2ec, s2ec, e2ec, n2ec}),
        .port_out(east_out_data),
        .out_valid(east_out_valid),
        .busy(east_out_busy)
    ),
    south_out_switch (
        .clk(clk),
        .rst(rst),
        .in_ports({l2s, w2s, s2s, e2s, n2s}),
        .ports_valid({l2sv, w2sv, s2sv, e2sv, n2sv}),
        .ports_clear({l2sc, w2sc, s2sc, e2sc, n2sc}),
        .port_out(south_out_data),
        .out_valid(south_out_valid),
        .busy(south_out_busy)
    ),
    west_out_switch (
        .clk(clk),
        .rst(rst),
        .in_ports({l2w, w2w, s2w, e2w, n2w}),
        .ports_valid({l2wv, w2wv, s2wv, e2wv, n2wv}),
        .ports_clear({l2wc, w2wc, s2wc, e2wc, n2wc}),
        .port_out(west_out_data),
        .out_valid(west_out_valid),
        .busy(west_out_busy)
    ),
    local_out_switch (
        .clk(clk),
        .rst(rst),
        .in_ports({l2l, w2l, s2l, e2l, n2l}),
        .ports_valid({l2lv, w2lv, s2lv, e2lv, n2lv}),
        .ports_clear({l2lc, w2lc, s2lc, e2lc, n2lc}),
        .port_out(local_out_data),
        .out_valid(local_out_valid),
        .busy(local_out_busy)
    );
endmodule
// The endpoint-link adapter slot: sits between a router's local port and its
// endpoint, same six signals on both faces (integrate/what-you-own.md s2).
// Shipped production example of the slot: kohakuaccel/noc/endpoint/
// noc_l2_adapter.v. Anything that observes or intercepts endpoint traffic
// goes here without touching the router or the unit.
`default_nettype none

module kh_endpoint_adapter_template #(
    parameter integer FLIT_WIDTH = 288,
    // 0 = combinational pass-through (a straight wire). 1 = one registered
    // stage each way -- the shape to copy when your adapter adds logic.
    parameter integer STAGE = 0
)(
    input  wire                   clk,
    input  wire                   rst,

    input  wire [FLIT_WIDTH-1:0]  rt_data,     // router -> here
    input  wire                   rt_valid,
    output wire                   rt_busy,
    output wire [FLIT_WIDTH-1:0]  ru_data,     // here -> router
    output wire                   ru_valid,
    input  wire                   ru_busy,

    output wire [FLIT_WIDTH-1:0]  ep_data,     // here -> endpoint
    output wire                   ep_valid,
    input  wire                   ep_busy,
    input  wire [FLIT_WIDTH-1:0]  eu_data,     // endpoint -> here
    input  wire                   eu_valid,
    output wire                   eu_busy,

    // ---- your observation taps ----
    output reg  [31:0]            n_down,      // flits router -> endpoint
    output reg  [31:0]            n_up
);
    // A transfer is any cycle with valid && !busy; count without touching.
    always @(posedge clk) begin
        if (rst) begin
            n_down <= 32'd0;
            n_up   <= 32'd0;
        end else begin
            if (ep_valid && !ep_busy) begin
                n_down <= n_down + 32'd1;
            end
            if (ru_valid && !ru_busy) begin
                n_up <= n_up + 32'd1;
            end
        end
    end

    generate if (STAGE == 0) begin : g_pass

        assign ep_data  = rt_data;
        assign ep_valid = rt_valid;
        assign rt_busy  = ep_busy;
        assign ru_data  = eu_data;
        assign ru_valid = eu_valid;
        assign eu_busy  = ru_busy;

    end else begin : g_stage
        // One held register per direction. The hold rule is the whole trick:
        // the port consumes on every valid && !busy edge, so the stage keeps
        // its flit while downstream is busy and refuses upstream meanwhile.
        reg [FLIT_WIDTH-1:0] d_q, u_q;
        reg                  d_v, u_v;

        wire d_take = rt_valid && !rt_busy;
        wire u_take = eu_valid && !eu_busy;
        assign rt_busy = d_v && ep_busy;
        assign eu_busy = u_v && ru_busy;

        always @(posedge clk) begin
            if (rst) begin
                d_v <= 1'b0;
                u_v <= 1'b0;
            end else begin
                d_v <= d_take | (d_v && ep_busy);
                if (d_take) begin
                    d_q <= rt_data;
                end
                u_v <= u_take | (u_v && ru_busy);
                if (u_take) begin
                    u_q <= eu_data;
                end
            end
        end
        assign ep_data  = d_q;
        assign ep_valid = d_v;
        assign ru_data  = u_q;
        assign ru_valid = u_v;
    end endgenerate

endmodule

`default_nettype wire

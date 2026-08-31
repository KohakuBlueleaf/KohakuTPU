// Configurable read engine: M masters -> N homes, MODE 0 = SASD (one global read in
// flight) or 1 = SAMD (per-home parallel read, clean per-master data mux). ARID is
// owner-tagged so R routes back. Pairs with kaxi_wr in kaxi_xbar.

`default_nettype none

module kaxi_rd #(
    parameter integer M        = 4,
    parameter integer N_HOME   = 4,
    parameter integer ADDR_W   = 40,
    parameter integer DATA_W   = 512,
    parameter integer ID_W     = 4,
    parameter integer HOME_LSB = 32,
    parameter integer MODE     = 1,
    parameter integer HIDX_W   = (N_HOME <= 1) ? 1 : $clog2(N_HOME),
    parameter integer MIDX_W   = (M <= 1) ? 1 : $clog2(M),
    parameter integer SID_W    = ID_W + MIDX_W
)(
    input  wire                    clk,
    input  wire                    resetn,
    input  wire [M*ID_W-1:0]       s_arid,
    input  wire [M*ADDR_W-1:0]     s_araddr,
    input  wire [M*8-1:0]          s_arlen,
    input  wire [M*3-1:0]          s_arsize,
    input  wire [M*2-1:0]          s_arburst,
    input  wire [M-1:0]            s_arvalid,
    output reg  [M-1:0]            s_arready,
    output reg  [M*ID_W-1:0]       s_rid,
    output reg  [M*DATA_W-1:0]     s_rdata,
    output reg  [M*2-1:0]          s_rresp,
    output reg  [M-1:0]            s_rlast,
    output reg  [M-1:0]            s_rvalid,
    input  wire [M-1:0]            s_rready,
    output reg  [N_HOME*SID_W-1:0]     m_arid,
    output reg  [N_HOME*ADDR_W-1:0]    m_araddr,
    output reg  [N_HOME*8-1:0]         m_arlen,
    output reg  [N_HOME*3-1:0]         m_arsize,
    output reg  [N_HOME*2-1:0]         m_arburst,
    output reg  [N_HOME-1:0]           m_arvalid,
    input  wire [N_HOME-1:0]           m_arready,
    input  wire [N_HOME*SID_W-1:0]     m_rid,
    input  wire [N_HOME*DATA_W-1:0]    m_rdata,
    input  wire [N_HOME*2-1:0]         m_rresp,
    input  wire [N_HOME-1:0]           m_rlast,
    input  wire [N_HOME-1:0]           m_rvalid,
    output reg  [N_HOME-1:0]           m_rready
);
    localparam RI=1'b0, RD=1'b1;
    integer h, k, mm;
    reg [MIDX_W:0] rot;

    generate
    if (MODE != 0) begin : g_samd
        reg               rst  [0:N_HOME-1];
        reg  [MIDX_W-1:0] curr [0:N_HOME-1];
        reg  [MIDX_W-1:0] rrr  [0:N_HOME-1];
        reg  [M-1:0]      rbusy;
        reg  [HIDX_W-1:0] rhome [0:M-1];
        reg  [MIDX_W-1:0] arg [0:N_HOME-1]; reg [N_HOME-1:0] arg_v; reg [MIDX_W-1:0] cr;
        always @(*) begin
            for (h = 0; h < N_HOME; h = h + 1) begin
                arg[h] = 0; arg_v[h] = 1'b0;
                for (k = 0; k < M; k = k + 1) begin
                    rot = {1'b0, rrr[h]} + k[MIDX_W:0];
                    if (rot >= M[MIDX_W:0]) begin
                        rot = rot - M[MIDX_W:0];
                    end
                    cr  = rot[MIDX_W-1:0];
                    if (!arg_v[h] && s_arvalid[cr] && !rbusy[cr]
                        && (s_araddr[cr*ADDR_W + HOME_LSB +: HIDX_W] == h[HIDX_W-1:0]))
                        begin arg[h] = cr; arg_v[h] = 1'b1; end
                end
            end
        end
        always @(*) begin
            s_arready = {M{1'b0}}; s_rvalid = {M{1'b0}};
            s_rid = 0; s_rdata = 0; s_rresp = 0; s_rlast = 0;
            m_arvalid = 0; m_rready = 0;
            m_arid = 0; m_araddr = 0; m_arlen = 0; m_arsize = 0; m_arburst = 0;
            for (h = 0; h < N_HOME; h = h + 1) begin
                if ((rst[h] == RI) && arg_v[h] && m_arready[h]) begin
                    s_arready[arg[h]] = 1'b1;
                end
                if (rst[h] == RD) begin
                    s_rvalid[curr[h]] = m_rvalid[h];
                end
                if (rst[h] == RI) begin
                    m_arvalid[h] = arg_v[h];
                    m_arid   [h*SID_W +: SID_W]   = {arg[h], s_arid[arg[h]*ID_W +: ID_W]};
                    m_araddr [h*ADDR_W +: ADDR_W] = s_araddr[arg[h]*ADDR_W +: ADDR_W];
                    m_arlen  [h*8 +: 8]           = s_arlen [arg[h]*8 +: 8];
                    m_arsize [h*3 +: 3]           = s_arsize[arg[h]*3 +: 3];
                    m_arburst[h*2 +: 2]           = s_arburst[arg[h]*2 +: 2];
                end
                if (rst[h] == RD) begin
                    m_rready[h] = s_rready[curr[h]];
                end
            end
            for (mm = 0; mm < M; mm = mm + 1) begin
                s_rid  [mm*ID_W +: ID_W]      = m_rid  [rhome[mm]*SID_W +: ID_W];
                s_rdata[mm*DATA_W +: DATA_W]  = m_rdata[rhome[mm]*DATA_W +: DATA_W];
                s_rresp[mm*2 +: 2]            = m_rresp[rhome[mm]*2 +: 2];
                s_rlast[mm]                   = m_rlast[rhome[mm]];
            end
        end
        always @(posedge clk) begin
            if (!resetn) begin
                rbusy <= 0;
                for (h = 0; h < N_HOME; h = h + 1) begin rst[h] <= RI; rrr[h] <= 0; end
                for (mm = 0; mm < M; mm = mm + 1) begin
                    rhome[mm] <= 0;
                end
            end else begin
                for (h = 0; h < N_HOME; h = h + 1) begin
                    case (rst[h])
                        RI: if (arg_v[h] && m_arready[h]) begin
                                curr[h] <= arg[h]; rhome[arg[h]] <= h[HIDX_W-1:0];
                                rrr[h] <= (arg[h] == (M-1)) ? {MIDX_W{1'b0}} : (arg[h] + 1'b1);
                                rbusy[arg[h]] <= 1'b1; rst[h] <= RD;
                            end
                        RD: if (m_rvalid[h] && s_rready[curr[h]] && m_rlast[h]) begin
                                rbusy[curr[h]] <= 1'b0; rst[h] <= RI;
                            end
                        default: rst[h] <= RI;
                    endcase
                end
            end
        end
    end else begin : g_sasd
        reg               gr;
        reg  [MIDX_W-1:0] gr_m; reg [HIDX_W-1:0] gr_h; reg [MIDX_W-1:0] gr_rr;
        reg  [MIDX_W-1:0] am; reg av; reg [HIDX_W-1:0] ah;
        always @(*) begin
            am = 0; av = 1'b0; ah = 0;
            for (k = 0; k < M; k = k + 1) begin
                rot = {1'b0, gr_rr} + k[MIDX_W:0];
                if (rot >= M[MIDX_W:0]) begin
                    rot = rot - M[MIDX_W:0];
                end
                if (!av && s_arvalid[rot[MIDX_W-1:0]]) begin
                    am = rot[MIDX_W-1:0]; av = 1'b1;
                    ah = s_araddr[am*ADDR_W + HOME_LSB +: HIDX_W];
                end
            end
        end
        always @(*) begin
            s_arready = {M{1'b0}}; s_rvalid = {M{1'b0}};
            s_rid = 0; s_rdata = 0; s_rresp = 0; s_rlast = 0;
            m_arvalid = 0; m_rready = 0;
            m_arid = 0; m_araddr = 0; m_arlen = 0; m_arsize = 0; m_arburst = 0;
            if ((gr == RI) && av && m_arready[ah]) begin
                s_arready[am] = 1'b1;
            end
            if (gr == RD) begin
                s_rvalid[gr_m]                 = m_rvalid[gr_h];
                s_rid  [gr_m*ID_W +: ID_W]     = m_rid[gr_h*SID_W +: ID_W];
                s_rdata[gr_m*DATA_W +: DATA_W] = m_rdata[gr_h*DATA_W +: DATA_W];
                s_rresp[gr_m*2 +: 2]           = m_rresp[gr_h*2 +: 2];
                s_rlast[gr_m]                  = m_rlast[gr_h];
            end
            for (h = 0; h < N_HOME; h = h + 1) begin
                if ((gr == RI) && av && (ah == h[HIDX_W-1:0])) begin
                    m_arvalid[h] = 1'b1;
                    m_arid   [h*SID_W +: SID_W]   = {am, s_arid[am*ID_W +: ID_W]};
                    m_araddr [h*ADDR_W +: ADDR_W] = s_araddr[am*ADDR_W +: ADDR_W];
                    m_arlen  [h*8 +: 8]           = s_arlen [am*8 +: 8];
                    m_arsize [h*3 +: 3]           = s_arsize[am*3 +: 3];
                    m_arburst[h*2 +: 2]           = s_arburst[am*2 +: 2];
                end
                if ((gr == RD) && (gr_h == h[HIDX_W-1:0])) begin
                    m_rready[h] = s_rready[gr_m];
                end
            end
        end
        always @(posedge clk) begin
            if (!resetn) begin
                gr <= RI; gr_rr <= 0;
            end else begin
                case (gr)
                    RI: if (av && m_arready[ah]) begin
                            gr_m <= am; gr_h <= ah;
                            gr_rr <= (am == (M-1)) ? {MIDX_W{1'b0}} : (am + 1'b1);
                            gr <= RD;
                        end
                    RD: if (m_rvalid[gr_h] && s_rready[gr_m] && m_rlast[gr_h]) begin
                            gr <= RI;
                        end
                    default: gr <= RI;
                endcase
            end
        end
    end
    endgenerate
endmodule

`default_nettype wire

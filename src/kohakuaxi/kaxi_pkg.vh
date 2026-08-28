// KohakuAXI shared AXI4 constants, region attributes, and pure helpers.
// `include "kaxi_pkg.vh" once per module.

`ifndef KAXI_PKG_VH
`define KAXI_PKG_VH

// ---- AXI4 response codes (A3.3) --------------------------------------------
localparam [1:0] KAXI_OKAY   = 2'b00;
localparam [1:0] KAXI_EXOKAY = 2'b01;   // exclusive success only
localparam [1:0] KAXI_SLVERR = 2'b10;
localparam [1:0] KAXI_DECERR = 2'b11;   // nothing decoded the address

// ---- AXI4 burst types (A3.1) -----------------------------------------------
localparam [1:0] KAXI_FIXED = 2'b00;
localparam [1:0] KAXI_INCR  = 2'b01;
localparam [1:0] KAXI_WRAP  = 2'b10;

// ---- region / cacheability attributes (drives the L3 allocate vs bypass) ---
// Set by a region table + AxCACHE; see kaxi_region.v.
localparam [1:0] KAXI_RGN_CACHE_LOCAL  = 2'd0;   // private, L1+L3 cacheable
localparam [1:0] KAXI_RGN_CACHE_SHARED = 2'd1;   // shared, L3-cacheable (L1-uncached in SW)
localparam [1:0] KAXI_RGN_UNCACHED     = 2'd2;   // bypass L3, order at home
localparam [1:0] KAXI_RGN_DEVICE       = 2'd3;   // bypass, device semantics

// ---- response merge (A3.3 / 03-converters): worst-case precedence ----------
// A component that split one transaction into several reports the worst of the
// sub-responses: DECERR > SLVERR > OKAY > EXOKAY. (Not a numeric max: EXOKAY=1
// but ranks best.) Reused by the cache (miss+writeback) and any splitter.
function [1:0] kaxi_resp_merge;
    input [1:0] a;
    input [1:0] b;
    begin
        if (a == KAXI_DECERR || b == KAXI_DECERR)
            kaxi_resp_merge = KAXI_DECERR;
        else if (a == KAXI_SLVERR || b == KAXI_SLVERR)
            kaxi_resp_merge = KAXI_SLVERR;
        else if (a == KAXI_OKAY || b == KAXI_OKAY)
            kaxi_resp_merge = KAXI_OKAY;
        else
            kaxi_resp_merge = KAXI_EXOKAY;
    end
endfunction

// ---- ceil(log2(n)), n>=1; KAXI_CLOG2(1)=1 so a 1-wide field always exists ---
function integer kaxi_clog2;
    input integer n;
    integer i;
    begin
        kaxi_clog2 = 1;
        for (i = 1; (1 << i) < n; i = i + 1)
            kaxi_clog2 = i + 1;
    end
endfunction

`endif

// Packet format for KohakuNoC. Single source of truth -- docs/noc/spec.md s3-s5
// describes the same layout in prose.
//
// A flit is NOC_FLIT_W bits: a 32-bit header followed by a 256-bit payload.
// The router reads only dst_x/dst_y; everything else is opaque to it.

`ifndef NOC_PKT_VH
`define NOC_PKT_VH

`define NOC_POS_W     4
`define NOC_FLIT_W    288
`define NOC_PAYLOAD_W 256

// header field positions, MSB-first
`define NOC_DST_X   287:284
`define NOC_DST_Y   283:280
`define NOC_SRC_X   279:276
`define NOC_SRC_Y   275:272
`define NOC_TYPE    271:268
`define NOC_TXN_ID  267:260
`define NOC_LAST    259
`define NOC_RSVD    258:256
`define NOC_PAYLOAD 255:0

// Message classes.
//
// INCLUDED BY NOTHING -- every module re-declares these, so a divergence is
// silent, and one happened: CU_DATA was 0x4 here while mag_mem_port.v and
// vec_cu.v used 0x4 for MEM_WR_DATA, so a CU_DATA flit reaching MAG would have
// entered the write queue as data. Resolved in favour of the silicon.
//
// Bit 3 no longer partitions memory from CU traffic: five memory messages do
// not fit in four codes, and no cache exists to filter on it.
`define NOC_T_MEM_RD_REQ  4'h0
`define NOC_T_MEM_WR_REQ  4'h1
`define NOC_T_MEM_RD_RESP 4'h2
`define NOC_T_MEM_WR_ACK  4'h3
`define NOC_T_MEM_WR_DATA 4'h4
`define NOC_T_CU_INST     4'h5
`define NOC_T_CU_SIGNAL   4'h6
`define NOC_T_CU_CTRL     4'h7
`define NOC_T_CU_DATA     4'h8
`define NOC_T_ERROR       4'hF
`define NOC_T_IS_MEM(t)   ((t) <= 4'h4)

// MEM_RD_REQ / MEM_WR_REQ descriptor payload
`define NOC_MEM_ADDR       255:222   // 34 bits: exactly the 16 GB physical map
`define NOC_MEM_ADDR_SPARE 221:216   // reserved, must be 0
`define NOC_MEM_LEN        215:208   // payload flits minus 1
`define NOC_MEM_FLAGS      207:200
`define NOC_MEM_F_CACHEABLE  0
`define NOC_MEM_F_INVALIDATE 1
`define NOC_MEM_F_FLUSH      2

// CU_DATA descriptor payload
`define NOC_CUD_BUF_ID 255:248
`define NOC_CUD_OFFSET 247:232       // in 32-byte granules
`define NOC_CUD_LEN    231:224
`define NOC_CUD_FLAGS  223:216
`define NOC_CUD_F_SIGNAL_ON_COMPLETE 0

// CU_INST payload
`define NOC_INST_LEN   255:248
`define NOC_INST_CLASS 247:240
`define NOC_INST_BODY  239:0

// CU_SIGNAL payload. Codes below 0x40 are centrally allocated; arg is always
// CU-defined content.
`define NOC_SIG_CODE 255:248
`define NOC_SIG_ARG  247:216
`define NOC_SIG_INST_COMPLETE   8'h00
`define NOC_SIG_BATCH_COMPLETE  8'h01
`define NOC_SIG_BARRIER_REACHED 8'h02
`define NOC_SIG_DATA_RECEIVED   8'h03
`define NOC_SIG_FAULT           8'h04

// Build a header. Payload is OR-ed in by the caller.
`define NOC_HDR(dx,dy,sx,sy,ty,id,lst) \
    { dx[3:0], dy[3:0], sx[3:0], sy[3:0], ty[3:0], id[7:0], lst, 3'b000 }

`endif

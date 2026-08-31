// The header flit of a KTS packet: fixed fields from bit 0, the rest `user`.
// W >= 64. `len` is PAYLOAD BYTES, so a packet keeps its meaning through a
// width conversion (the flit count changes, the byte count does not).
//
//   [3:0]   kind      [7:4]   vc (self-describing copy of the wire's)
//   [15:8]  dst       [23:16] src
//   [39:24] len       [47:40] tag
//   [W-1:48] user

localparam integer KTS_H_KIND_LSB = 0;   localparam integer KTS_H_KIND_W = 4;
localparam integer KTS_H_VC_LSB   = 4;   localparam integer KTS_H_VC_W   = 4;
localparam integer KTS_H_DST_LSB  = 8;   localparam integer KTS_H_DST_W  = 8;
localparam integer KTS_H_SRC_LSB  = 16;  localparam integer KTS_H_SRC_W  = 8;
localparam integer KTS_H_LEN_LSB  = 24;  localparam integer KTS_H_LEN_W  = 16;
localparam integer KTS_H_TAG_LSB  = 40;  localparam integer KTS_H_TAG_W  = 8;
localparam integer KTS_H_USER_LSB = 48;

localparam [3:0] KTS_K_DATA  = 4'd0;   // raw payload, no reply
localparam [3:0] KTS_K_RDREQ = 4'd1;   // read request: user = address, size
localparam [3:0] KTS_K_WRREQ = 4'd2;   // write request: user = address; payload = data
localparam [3:0] KTS_K_RDRSP = 4'd3;   // read response: payload = data
localparam [3:0] KTS_K_WRRSP = 4'd4;   // write response: user = status
localparam [3:0] KTS_K_CRD   = 4'd5;   // serial carrier only: credit frame
localparam [3:0] KTS_K_ACK   = 4'd6;   // serial carrier only: ack frame

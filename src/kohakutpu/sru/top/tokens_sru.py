"""The SRU's generator vocabulary, for gen_mesh.py --tokens.

`sru` instantiates one sru_cu on the NoC clock (sru_cu synchronises `resetn`
itself). TOKENS uses sru_cu's own defaults (the strong profile);
`tokens(params)` binds a profile's knobs (scripts/py/sru/profiles.py).
"""


def _param(k, v):
    return f'.{k}("{v}")' if isinstance(v, str) else f".{k}({v})"


def tokens(params=None):
    prof = ", ".join(_param(k, v) for k, v in (params or {}).items())
    prof = f",\n             {prof}" if prof else ""

    def sru(i, x, y, mem_x, mem_y, conn):
        return f"""    sru_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X({x}), .POS_Y({y}),
             .MEM_X({mem_x}), .MEM_Y({mem_y}), .MODEL(MODEL),
             .INST_DEPTH(INST_DEPTH), .RECV_DEPTH(RECV_DEPTH){prof}) u_sru{i} (
        .clk(clk), .resetn(resetn),
        {conn}
    );"""

    return {"sru": sru}


TOKENS = tokens()

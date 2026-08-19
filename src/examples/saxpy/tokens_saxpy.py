"""The saxpy project's generator vocabulary, for gen_mesh.py --tokens.

This file is the whole of what a project supplies to compose its units into
a mesh: token name -> the instance text. FW/PW/INST_DEPTH and clk/resetn are
the generated top's own; `conn` arrives bound to the right router link.
"""


def sax(i, x, y, mem_x, mem_y, conn):
    return f"""    saxpy_cu #(.FLIT_WIDTH(FW), .POS_WIDTH(PW), .POS_X({x}), .POS_Y({y}),
              .MEM_X({mem_x}), .MEM_Y({mem_y}), .INST_DEPTH(INST_DEPTH)) u_sax{i} (
        .clk(clk), .resetn(resetn),
        {conn},
        .busy()
    );"""


TOKENS = {"sax": sax}

# sb_link_pair has only bus_clk. Kept separate because create_clock on an empty
# object list is an error, and guarding it with Tcl inside an XDC silently
# stopped every clock from being created.

create_clock -name bus_clk -period 2.500 [get_ports bus_clk]

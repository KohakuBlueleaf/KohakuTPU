# v8t2: the synthesis analysis on its own, on a READ-ONLY open of the project
# (the implementation session holds the writable one), so the two run side
# by side. open_run links the OOC IP and module references into the netlist;
# the synth_1 checkpoint alone has them as black boxes.
#   vivado -mode batch -source scripts/tcl/v8t2_analyze.tcl
set here [file dirname [file normalize [info script]]]
source $here/v8t2/00_config.tcl
set V8_MODE ro
open_project -read_only $proj_dir/${design_name}.xpr
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth_1 is [get_property PROGRESS [get_runs synth_1]], not complete"
}
source $here/v8t2/70_analyze.tcl

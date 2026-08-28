# Where do kaxi_xbar4b's 8,158 LUT actually go? Bucket by driven signal so the
# overhead (arbiter/FSM/gating) is separated from the datapath muxes.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
read_verilog [file join $root src kohakuaxi kaxi_xbar4b.v]
synth_design -top kaxi_xbar4b -part $part -mode out_of_context \
    -generic M=4 -generic N_HOME=4 -generic DATA_W=512 -flatten_hierarchy rebuilt
ooc_count TOTAL
ooc_lut_census xbar4b "" 30
puts "CENSUS_DONE"

# kaxi_xbar4 mux cost vs DATA_W (4x4). If ~linear, a narrower internal datapath at
# a higher clock (+ BRAM width-convert FIFOs) trades LUT for BRAM at equal bandwidth
# -- the axi-ic-perf mechanism.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set x4 [file join $root src kohakuaxi kaxi_xbar4.v]
proc syn {f part dw} {
    catch {close_design}
    read_verilog $f
    synth_design -top kaxi_xbar4 -part $part -mode out_of_context \
        -generic M=4 -generic N_HOME=4 -generic DATA_W=$dw
    ooc_count "xbar4-4x4-dw$dw"
}
syn $x4 $part 128
syn $x4 $part 256
syn $x4 $part 512
puts "XW_DONE"

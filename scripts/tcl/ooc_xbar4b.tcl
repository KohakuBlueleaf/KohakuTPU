# kaxi_xbar4b (clean per-master read mux) vs xbar4 (home scatter): does mux
# efficiency shrink full-SAMD LUT without BRAM? DW ablation, 4x4.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set f [file join $root src kohakuaxi kaxi_xbar4b.v]
proc syn {f part dw} {
    catch {close_design}
    read_verilog $f
    synth_design -top kaxi_xbar4b -part $part -mode out_of_context \
        -generic M=4 -generic N_HOME=4 -generic DATA_W=$dw
    ooc_count "xbar4b-4x4-dw$dw"
}
syn $f $part 128
syn $f $part 256
syn $f $part 512
puts "X4B_DONE"

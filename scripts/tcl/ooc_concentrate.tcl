# O(M) cost of the shared concentrator (M AXI -> 1 AXI + owner tag). This is the
# arbiter+mux overhead the fused station engine and cheap-SAMD kaxi both pay once.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set f [file join $root src kohakuaccel axi station sb_axi_concentrate.v]

proc syn {root part f M dw} {
    catch {close_design}
    read_verilog $f
    synth_design -top sb_axi_concentrate -part $part -mode out_of_context \
        -generic M=$M -generic DW=$dw -generic AW=40 -generic IDW=4
    ooc_count "conc-M${M}-dw${dw}"
}
syn $root $part $f 3  512
syn $root $part $f 4  512
syn $root $part $f 8  512
syn $root $part $f 16 512
syn $root $part $f 4  32
puts "CONC_DONE"

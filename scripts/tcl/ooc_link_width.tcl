# Backbone (cross-station link) width ablation: sb_link cost vs W, distributed vs
# block RX buffer. Shows O(W) scaling and the per-bit shrink (move the FW-wide RX
# FIFO + credit buffer off LUTRAM into BRAM).
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set files [list [file join $root src kohakuaccel common sync_fifo.v] \
                [file join $root src kohakuaccel axi link sb_link.v]]

proc syn {files part w mem} {
    catch {close_design}
    read_verilog $files
    synth_design -top sb_link -part $part -mode out_of_context \
        -generic W=$w -generic PIPE=4 -generic CRED=16 -generic MEMORY_TYPE=$mem
    create_clock -name c -period 2.0 [get_ports clk]
    ooc_count "link-w${w}-$mem"
}
foreach w {128 256 512 1024} {
    syn $files $part $w distributed
    syn $files $part $w block
}
puts "LINKW_DONE"

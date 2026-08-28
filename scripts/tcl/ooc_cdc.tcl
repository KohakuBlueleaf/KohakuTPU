# Per-master AXI CDC cost -- the crossing that does NOT fuse (M masters, M clocks).
# Multi-clock fused input engine = single-clock fused + (M-1) of these.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set files [list [file join $root src kohakuaccel common async_fifo.v] \
                [file join $root src kohakuaccel axi station sb_axi_cdc.v]]

proc syn {files part dw mem} {
    catch {close_design}
    read_verilog $files
    synth_design -top sb_axi_cdc -part $part -mode out_of_context \
        -generic DW=$dw -generic AW=40 -generic IDW=4 -generic MEM=$mem
    create_clock -name sc -period 2.0 [get_ports s_aclk]
    create_clock -name mc -period 2.0 [get_ports m_aclk]
    set_clock_groups -async -group [get_clocks sc] -group [get_clocks mc]
    ooc_count "cdc-dw${dw}-$mem"
}
syn $files $part 512 distributed
syn $files $part 512 block
syn $files $part 32  distributed
puts "CDC_DONE"

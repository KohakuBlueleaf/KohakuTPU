# The per-home L3 cache alone: what one home's cache costs, URAM vs BRAM, 2MB vs 512KB.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set files [list [file join $root src kohakuaccel common kohaku_sdpram.v] \
                [file join $root src kohakuaxi kaxi_l3.v]]
proc syn {files part gen tag} {
    catch {close_design}
    read_verilog $files
    eval synth_design -top kaxi_l3 -part $part -mode out_of_context $gen
    create_clock -name clk -period 3.333 [get_ports clk]
    ooc_record $tag "kaxi_l3 $gen" 2000 1
}
syn $files $part {-generic DATA_W=512 -generic ID_W=6 -generic SETS=32768 -generic RAM_STYLE=ultra} l3-2MB-uram
syn $files $part {-generic DATA_W=512 -generic ID_W=6 -generic SETS=32768 -generic RAM_STYLE=block} l3-2MB-bram
syn $files $part {-generic DATA_W=512 -generic ID_W=6 -generic SETS=8192  -generic SET_W=13 -generic RAM_STYLE=ultra} l3-512KB-uram
puts "CACHE_DONE"

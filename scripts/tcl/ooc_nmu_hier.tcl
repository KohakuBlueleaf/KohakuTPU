# Hierarchical LUT breakdown of one 32-bit NMU at ship width, to see whether the
# cost is the CDC FIFOs (shareable) or the packing/credit/ID logic (not).
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set common [file join $root src kohakuaccel common]
set stn    [file join $root src kohakuaccel axi station]
read_verilog [list [file join $common sync_fifo.v] [file join $common async_fifo.v] \
    [file join $common sb_skid.v] [file join $stn sb_nmu.v]]
synth_design -top sb_nmu -part $part -mode out_of_context -flatten_hierarchy none \
    -generic MW=32 -generic FW=256 -generic AW=43
# per-sub-instance LUT (async FIFOs vs the NMU's own logic)
set total [llength [get_cells -hier -filter {REF_NAME =~ LUT*}]]
puts "@@@ NMU_TOTAL_LUT $total"
foreach inst {u_reqf u_rspf u_tokf u_credf} {
    if {[llength [get_cells -quiet $inst]]} {
        set n [llength [get_cells -hier -filter "REF_NAME =~ LUT* && NAME =~ $inst/*"]]
        puts "@@@ SUB $inst LUT $n"
    }
}
puts "@@@ nmu_hier done"

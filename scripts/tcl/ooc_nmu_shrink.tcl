# Measure the two sb_nmu shrink levers as LUT/Fmax deltas on ONE port:
#   OUTST       : 0 (=16 tags) vs 4  -- tag table + rf_tag muxes + ID-order scan
#   FORCE_PLACE : 0 (pack) vs 1      -- drops the 256b pack RMW on a narrow port
# nmu_probe wraps sb_nmu with a realistic 2-segment decode so nothing folds.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set common [file join $root src kohakuaccel common]
set stn    [file join $root src kohakuaccel axi station]
set probe  [file join $root tests axi nmu_probe.v]

proc run_variant {root common stn probe part tag mw outst place} {
    catch {close_design}
    read_verilog [list [file join $common async_fifo.v] \
                       [file join $stn sb_nmu.v] $probe]
    synth_design -top nmu_probe -part $part -mode out_of_context \
        -generic MW=$mw -generic FW=256 -generic AW=43 \
        -generic OUTST=$outst -generic FORCE_PLACE=$place
    create_clock -name s_aclk  -period 2.0 [get_ports s_aclk]
    create_clock -name bus_clk -period 2.0 [get_ports bus_clk]
    set_clock_groups -asynchronous -group [get_clocks s_aclk] \
                                   -group [get_clocks bus_clk]
    ooc_record $tag "mw=$mw outst=$outst place=$place"
}

run_variant $root $common $stn $probe $part nmu32-base   32 0 0
run_variant $root $common $stn $probe $part nmu32-t4     32 4 0
run_variant $root $common $stn $probe $part nmu32-t4pl   32 4 1
run_variant $root $common $stn $probe $part nmu512-base 512 0 0
run_variant $root $common $stn $probe $part nmu512-t4   512 4 0
puts "NMU_SHRINK_DONE"

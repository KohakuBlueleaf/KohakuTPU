# Configurable crossbar: ALL FOUR read/write mode combos (SASD/SAMD each), at 4x4
# and 8x8. Whole table: LUT/FF/BRAM/Fmax per config. Target: half SMC / near axi-ic.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set files [list [file join $root src kohakuaxi kaxi_wr.v] \
                [file join $root src kohakuaxi kaxi_rd.v] \
                [file join $root src kohakuaxi kaxi_xbar5.v]]
proc syn {files part M N wr rd} {
    catch {close_design}
    read_verilog $files
    synth_design -top kaxi_xbar5 -part $part -mode out_of_context \
        -generic M=$M -generic N_HOME=$N -generic DATA_W=512 \
        -generic WR_MODE=$wr -generic RD_MODE=$rd
    create_clock -name clk -period 2.0 [get_ports clk]
    set wn [expr {$wr ? "SAMD" : "SASD"}]
    set rn [expr {$rd ? "SAMD" : "SASD"}]
    ooc_record "x5-${M}x${N}-w${wn}-r${rn}" "M=$M N=$N wr=$wn rd=$rn" 2000 1
}
foreach shp {4 8} {
    foreach {wr rd} {0 0  0 1  1 0  1 1} { syn $files $part $shp $shp $wr $rd }
}
puts "X5_DONE"

# General-config sweep for kaxi_xbar5: the shapes the goal names (4x4/8x8/16x16 +
# 4x8/4x12/4x16), mixture (SASD-wr/SAMD-rd, ship) and full SAMD. LUT + Fmax; the
# bandwidth column is computed (SAMD read = N*W*f, SASD write = W*f, SAMD write = N*W*f).
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
    create_clock -name clk -period 3.333 [get_ports clk]
    set wn [expr {$wr ? "SAMD" : "SASD"}]
    set rn [expr {$rd ? "SAMD" : "SASD"}]
    ooc_record "x5-${M}x${N}-w${wn}-r${rn}" "M=$M N=$N wr=$wn rd=$rn" 2000 1
}
# mixture (ship) across all shapes; full SAMD on the squares.
foreach {M N} {4 4  8 8  16 16  4 8  4 12  4 16} { syn $files $part $M $N 0 1 }
foreach {M N} {4 4  8 8  16 16} { syn $files $part $M $N 1 1 }
puts "X5SHAPES_DONE"

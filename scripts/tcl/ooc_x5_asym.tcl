# Asymmetric / N<M crossbar shapes (point 6): read-mux is N:1 per master, write-mux is
# M:1 per home, so M!=N loads the two directions unequally. mixture + full-SAMD, 512b.
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
    set wn [expr {$wr ? "SAMD" : "SASD"}]; set rn [expr {$rd ? "SAMD" : "SASD"}]
    ooc_record "x5-${M}x${N}-w${wn}-r${rn}" "M=$M N=$N wr=$wn rd=$rn" 2000 1
}
foreach {M N} {3 9  9 3  8 4  16 4  16 8} { syn $files $part $M $N 0 1 }
foreach {M N} {3 9  9 3  8 4  16 4  16 8} { syn $files $part $M $N 1 1 }
puts "X5ASYM_DONE"

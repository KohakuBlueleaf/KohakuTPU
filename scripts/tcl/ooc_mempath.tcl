# The SHIP memory path: kaxi_xbar5 + N per-home kaxi_l3 caches, fused. Ship = mixture
# (SASD write / SAMD read). Also full-SAMD, and the bare xbar alone, so xbar+cache vs
# xbar shows what the cache adds. Shapes 4x4 / 8x8. 512b, 2MB/home URAM, 300 MHz.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set files [list [file join $root src kohakuaccel common kohaku_sdpram.v] \
                [file join $root src kohakuaxi kaxi_wr.v] \
                [file join $root src kohakuaxi kaxi_rd.v] \
                [file join $root src kohakuaxi kaxi_xbar5.v] \
                [file join $root src kohakuaxi kaxi_l3.v] \
                [file join $root src kohakuaxi kaxi_mempath.v]]
proc syn {files part M N wr rd sets setw tag} {
    catch {close_design}
    read_verilog $files
    synth_design -top kaxi_mempath -part $part -mode out_of_context \
        -generic M=$M -generic N_HOME=$N -generic DATA_W=512 \
        -generic WR_MODE=$wr -generic RD_MODE=$rd \
        -generic SETS=$sets -generic SET_W=$setw
    create_clock -name clk -period 3.333 [get_ports clk]
    ooc_record $tag "M=$M N=$N wr=$wr rd=$rd sets=$sets" 2000 1
}
# ship mixture, full-SAMD, at 4x4 and 8x8, 2MB/home.
syn $files $part 4 4 0 1 32768 15 mempath-4x4-mix
syn $files $part 4 4 1 1 32768 15 mempath-4x4-full
syn $files $part 8 8 0 1 32768 15 mempath-8x8-mix
puts "MEMPATH_DONE"

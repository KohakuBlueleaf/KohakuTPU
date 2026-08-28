# DEV diagnosis: mixture (SASD-write / SAMD-read = write-through-ML ship config, the
# axi-ic-perf class) vs full SAMD, 4x4 512b. Where does the shippable config sit?
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set files [list [file join $root src kohakuaxi kaxi_wr.v] \
                [file join $root src kohakuaxi kaxi_rd.v] \
                [file join $root src kohakuaxi kaxi_xbar5.v]]
proc syn {files part wr rd tag} {
    catch {close_design}
    read_verilog $files
    synth_design -top kaxi_xbar5 -part $part -mode out_of_context \
        -generic M=4 -generic N_HOME=4 -generic DATA_W=512 \
        -generic WR_MODE=$wr -generic RD_MODE=$rd -flatten_hierarchy rebuilt
    create_clock -name clk -period 2.0 [get_ports clk]
    ooc_record "$tag" "wr=$wr rd=$rd" 2000 1
}
syn $files $part 0 1 x5-4x4-mix
syn $files $part 0 0 x5-4x4-SASD
puts "X5MIX_DONE"

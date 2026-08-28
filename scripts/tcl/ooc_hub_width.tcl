# Hub (per-station flit crossbar) cost vs payload width PW. The links are O(1) LUT;
# this shows the hub is the O(FW) term, and whether registering the datapath helps.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set files [list [file join $root src kohakuaccel common sb_skid.v] \
                [file join $root src kohakuaccel axi station sb_hub.v]]
proc syn {files part pw iskid} {
    catch {close_design}
    read_verilog $files
    synth_design -top sb_hub -part $part -mode out_of_context \
        -generic NSRC=4 -generic NDST=4 -generic PW=$pw -generic ISKID=$iskid
    create_clock -name c -period 2.0 [get_ports clk]
    ooc_count "hub-4x4-pw${pw}-isk$iskid"
}
foreach pw {128 256 512 1024} { syn $files $part $pw 0 }
syn $files $part 512 1
puts "HUBW_DONE"

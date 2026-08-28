# Output-engine fusion: N independent sb_nsu vs one shared core + de-concentrate.
# Independent(N) = N * nsu1; fused(N) = sb_station_out. 512b, 300 MHz.
set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e
set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]
set files [list \
  [file join $root src kohakuaccel common sync_fifo.v] \
  [file join $root src kohakuaccel common async_fifo.v] \
  [file join $root src kohakuaccel common sb_skid.v] \
  [file join $root src kohakuaccel axi station sb_nsu.v] \
  [file join $root src kohakuaccel axi station sb_axi_deconcentrate.v] \
  [file join $root src kohakuaccel axi station sb_station_out.v]]
proc syn_top {files part top gen clks tag} {
    catch {close_design}
    read_verilog $files
    eval synth_design -top $top -part $part -mode out_of_context $gen
    foreach c $clks { create_clock -name $c -period 3.333 [get_ports $c] }
    ooc_record $tag "$top $gen" 2000 1
}
syn_top $files $part sb_nsu {-generic SDW=512 -generic FW=512 -generic SIDW=4} {bus_clk m_aclk} nsu1-block
syn_top $files $part sb_station_out {-generic N=4 -generic DW=512 -generic FW=512} {clk} sout-n4-block
syn_top $files $part sb_station_out {-generic N=8 -generic DW=512 -generic FW=512} {clk} sout-n8-block
syn_top $files $part sb_axi_deconcentrate {-generic N=4 -generic DW=512} {clk} dc-n4
syn_top $files $part sb_axi_deconcentrate {-generic N=8 -generic DW=512} {clk} dc-n8
puts "SOUT_DONE"

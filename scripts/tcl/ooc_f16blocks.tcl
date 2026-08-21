# OOC synthesis of the E8M15 FMA lane BLOCK BY BLOCK, and of the DSP48
# alternatives to its two barrel shifters. SYNTH ONLY.
#   vivado -mode batch -source scripts/tcl/ooc_f16blocks.tcl -tclargs <period_ns>
#
# The lane measures 609 LUT (ooc_khd.tcl, s8-f16) and a flat total cannot say
# which stage to aim at. One Vivado session, one synth_design per top, so the
# rows cannot drift apart on part or ask.

set root [file normalize [file join [file dirname [info script]] .. ..]]
set part xcvu13p-fhgb2104-2L-e

set per [lindex $argv 0]
if {$per eq ""} { set per 3.333 }
set ::ooc_period $per

set_param general.maxThreads 4
source [file join $root scripts tcl ooc_class.tcl]

set srcs [list \
    [file join $root src kohakutpu matmul mx_fpacc.v] \
    [file join $root src kohakutpu vector vec_dsp.v] \
    [file join $root src kohakutpu vector vec_delay.v] \
    [file join $root src kohakutpu vector vec_cvt.v] \
    [file join $root src kohakuaccel pe rv32 dsp khd_f16_blocks.v]]

foreach top {khd_blk_align khd_blk_align_dsp khd_blk_norm khd_blk_norm_dsp \
             khd_blk_round khd_blk_mag khd_blk_expo khd_blk_spec \
             khd_blk_cvt_in khd_blk_cvt_out} {
    puts "@@@ ============================ $top"
    read_verilog $srcs
    read_xdc [file join $root scripts xdc ooc_khd.xdc]
    synth_design -top $top -part $part -mode out_of_context \
                 -flatten_hierarchy none -directive default
    ooc_record "blk-$top-t$per" "top=$top period=$per" 2000 2
    ooc_count $top
    ooc_classify 2000
    close_design
}

puts "@@@ ooc_f16blocks done"

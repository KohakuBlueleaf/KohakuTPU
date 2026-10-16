# Reset audit of an implementation checkpoint, read-only: every net that
# drives a flip-flop's set/reset pin (R, S, CLR, PRE), its driver, fanout,
# the blocks its loads sit in, and the driver's origin -- a reset
# synchroniser or BD reset IP (directly or through one LUT), or functional
# logic that synthesis mapped onto the reset pin.
#   vivado -mode batch -source scripts/tcl/audit/reset.tcl ?-tclargs dcp <checkpoint> tag placed cfg v9t3?
set here [file dirname [file dirname [file normalize [info script]]]]
set V9_CFG v9t3
foreach {k v} $argv { if {$k eq "cfg"} { set V9_CFG $v } }
source $here/$V9_CFG/00_config.tcl
foreach {k v} $argv {
    switch -- $k { dcp { set V8_DCP $v } tag { set V8_TAG $v } }
}
if {![info exists V8_DCP]} {
    set V8_DCP $proj_dir/${design_name}.runs/impl_1/${design_name}_wrapper_physopt.dcp
}
if {![info exists V8_TAG]} { set V8_TAG placed }
open_checkpoint $V8_DCP
set top ${design_name}_i
set tag $root/build/${design_name}_${V8_TAG}_reset
proc ra_t {} { return [clock format [clock seconds] -format %H:%M:%S] }

proc ra_src {c} {
    set n [get_property NAME $c]
    if {[regexp {u_rs_\w+|kh_rst_sync|rst_sys\d|rst_bus\d|rst_ddr4|proc_sys_reset|peripheral_aresetn|rst_ctrl|bus_rst_inv|lock_all|rst_tx_q|rstn|resetn|rst_q|rst_sync} $n]} {
        return reset
    }
    return logic
}

set ffs [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE && REF_NAME =~ FD*}]
set rp [get_pins -quiet -of_objects $ffs -filter {REF_PIN_NAME == R || REF_PIN_NAME == S || REF_PIN_NAME == CLR || REF_PIN_NAME == PRE}]
set nets [get_nets -quiet -of_objects $rp -filter {TYPE != GROUND && TYPE != POWER}]
puts "@@@ [llength $ffs] flip-flops, [llength $nets] set/reset nets  [ra_t]"

set fh [open ${tag}_nets.tsv w]
puts $fh [join {NET DRIVER DRV_REF ORIGIN VIA FANOUT_FF PINS LOAD_BLOCKS} \t]
foreach n $nets {
    set lp [get_pins -quiet -leaf -of_objects $n]
    set drv [get_pins -quiet $lp -filter {DIRECTION == OUT}]
    if {[llength $drv] != 1} { continue }
    set dc [get_cells -quiet -of_objects $drv]
    set ref [get_property REF_NAME $dc]
    set org [ra_src $dc]
    set via -
    # A LUT on the reset pin: the reset combined with logic. Name the inputs
    # that come from a reset source.
    if {[string match LUT* $ref]} {
        set via lut
        foreach ip [get_pins -quiet -of_objects $dc -filter {DIRECTION == IN}] {
            set inet [get_nets -quiet -of_objects $ip]
            set id [get_pins -quiet -leaf -of_objects $inet -filter {DIRECTION == OUT}]
            if {[llength $id] == 1 && [ra_src [get_cells -quiet -of_objects $id]] eq "reset"} {
                set org reset+logic
            }
        }
    }
    set rpins [get_pins -quiet $lp -filter {DIRECTION == IN && (REF_PIN_NAME == R || REF_PIN_NAME == S || REF_PIN_NAME == CLR || REF_PIN_NAME == PRE)}]
    array unset B
    foreach c [get_property NAME [get_cells -quiet -of_objects $rpins]] {
        set p [lrange [split $c /] 1 end]
        set o {}
        foreach s $p { if {$s ne "inst"} { lappend o $s } ; if {[llength $o] == 3} { break } }
        incr B([join $o /])
    }
    set bl {}
    foreach k [lsort [array names B]] { lappend bl "$k:$B($k)" }
    puts $fh [join [list [get_property NAME $n] [get_property NAME $dc] $ref $org $via \
        [llength $rpins] [get_property PIN_TYPE $drv] [join $bl " "]] \t]
}
close $fh
puts "@@@ reset nets in ${tag}_nets.tsv  [ra_t]"

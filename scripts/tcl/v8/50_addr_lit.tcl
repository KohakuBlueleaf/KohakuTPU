# The station bus's segment table and literal encoding, sourced by 50_addr.tcl
# and check_map.tcl alike. sb_nmu: MASK 1 = compare, emitted = (a & ~MASK) | XLT.
#
# Segment k = station*NQ + port. The host's view: mesh m's memory window at
# (m+1)<<40 reaches THAT node's S_AXI_MEM -- and through its DRAM master and
# the Xache, the same flat 16 GB every node sees; the window picks the node,
# never a channel. Control 0x800000 + m*0x10000, DDR ctrl m*0x100000, the
# clock wizard 0x900000 + m*0x10000, all below 4 GB for XDMA's AXI-Lite.

set MESH_WIN  [expr {1 << 40}]
set CTRL_BASE 0x800000
set WIZ_BASE  0x900000
set DDR_WIN   0x100000
set FULL      [expr {(1 << $AW) - 1}]

set seg_base {} ; set seg_mask {} ; set seg_xlt {}
set seg_dst  {} ; set seg_dprt {}

foreach {mid mod} $MESHES {
    lappend seg_base [expr {($mid + 1) * $MESH_WIN}]
    lappend seg_mask [expr {7 * $MESH_WIN}]
    lappend seg_xlt  0
    lappend seg_dst  $mid ; lappend seg_dprt 0

    set b [expr {$CTRL_BASE + $mid * 0x10000}]
    lappend seg_base $b ; lappend seg_mask [expr {$FULL ^ 0xFFFF}]
    lappend seg_xlt  $b ; lappend seg_dst $mid ; lappend seg_dprt 1

    set b [expr {$mid * $DDR_WIN}]
    lappend seg_base $b ; lappend seg_mask [expr {$FULL ^ 0xFFFFF}]
    lappend seg_xlt  $b ; lappend seg_dst $mid ; lappend seg_dprt 2

    set b [expr {$WIZ_BASE + $mid * 0x10000}]
    lappend seg_base $b ; lappend seg_mask [expr {$FULL ^ 0xFFFF}]
    lappend seg_xlt  $b ; lappend seg_dst $mid ; lappend seg_dprt 3
}

proc v8_bits {v w} {
    set s ""
    for {set i [expr {$w - 1}]} {$i >= 0} {incr i -1} {
        append s [expr {($v >> $i) & 1}]
    }
    return $s
}

# ONE literal: IPI rejects `{43'h..., 43'h...}` with IP_Flow 19-3450. Binary,
# because AW=43 is not a multiple of 4. Segment k is at [k*W +: W], so k=0 is
# the LSB and the string runs most-significant segment first.
proc v8_cat {vals width} {
    set s ""
    foreach v [lreverse $vals] { append s [v8_bits $v $width] }
    return "0b$s"
}

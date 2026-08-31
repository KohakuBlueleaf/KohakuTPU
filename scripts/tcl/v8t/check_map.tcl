# tclsh scripts/tcl/v8t/check_map.tcl -- the intended address map, printed from
# the SAME lists 50_addr.tcl pushes into the BD, plus the memory path the Xache
# adds. Run before the BD; 75_verify_bd reads the built BD back against it.

set here [file dirname [file normalize [info script]]]
source $here/00_config.tcl
source $here/50_addr_lit.tcl
set names {MEM CTRL DDR_CTL CLK_WIZ}

puts "=== host view: the station bus ==="
puts [format "%-4s %-8s %-14s %-14s %-14s %-9s %s" \
      stn port base mask xlt window lite]
set bad 0
set nseg [llength $seg_base]
for {set k 0} {$k < $nseg} {incr k} {
    set b [lindex $seg_base $k] ; set m [lindex $seg_mask $k]
    set p [lindex $seg_dprt $k]
    set win [expr {(~$m & $FULL) + 1}]
    set top [expr {$b + $win}]
    set lite [expr {$p == 0 ? "excluded" : ($top <= (1 << 32) ? "ok" : "ABOVE 4G")}]
    if {$lite eq "ABOVE 4G"} { incr bad }
    puts [format "%-4d %-8s 0x%011llX 0x%011llX 0x%011llX %-9s %s" \
          [lindex $seg_dst $k] [lindex $names $p] $b $m [lindex $seg_xlt $k] \
          [expr {$win >= (1 << 30) ? "[expr {$win / (1 << 30)}]G"
                                   : "[expr {$win / 1024}]K"}] $lite]
}
if {$bad} { error "$bad control window(s) unreachable by XDMA AXI-Lite" }

# Literal length and bit placement, decoded back.
foreach {nm vals w} [list base $seg_base $AW mask $seg_mask $AW xlt $seg_xlt $AW \
                          dst $seg_dst 2 dport $seg_dprt 2] {
    set lit [v8_cat $vals $w]
    set want [expr {$nseg * $w}]
    set got [expr {[string length $lit] - 2}]
    if {![string match {0b[01]*} $lit]} { error "SEG_$nm is not a 0b literal" }
    if {$got != $want} { error "SEG_$nm is $got bits, want $want" }
}
foreach {nm vals w} [list base $seg_base $AW dst $seg_dst 2 dport $seg_dprt 2] {
    set bits [string range [v8_cat $vals $w] 2 end]
    for {set k 0} {$k < $nseg} {incr k} {
        set off [expr {($nseg - 1 - $k) * $w}]
        set got 0
        foreach c [split [string range $bits $off [expr {$off + $w - 1}]] ""] {
            set got [expr {($got << 1) | $c}]
        }
        if {$got != [lindex $vals $k]} {
            error "SEG_$nm segment $k decodes to $got, want [lindex $vals $k]"
        }
    }
}
puts "@@@ station map ok: $nseg segments, literals well-formed and bit-placed"

puts "\n=== memory view: node -> Xache -> channel ==="
puts "  every node's DRAM master: one flat [expr {$DRAM_GB}] GB at \[33:0\], mesh bits \[37:36\] = 0"
puts "  home = addr\[[expr {$KX_HOME_LSB + 1}]:$KX_HOME_LSB\] AFTER the rotation; channel h = the MIG in SLR h"
puts "  rotation: $KX_NSWAP pairs, (a, b) = (i, i+2) for i = $KX_ILV_LG..[expr {$KX_HOME_LSB - 1}]"
if {[llength $KX_SWAP_A] != $KX_NSWAP || [llength $KX_SWAP_B] != $KX_NSWAP} {
    error "swap lists are not NSWAP long"
}
foreach a $KX_SWAP_A b $KX_SWAP_B {
    if {$a < $KX_ILV_LG || $a >= $KX_HOME_LSB || $b != $a + 2} { error "bad pair ($a, $b)" }
    if {$a < 12} { error "pair ($a, $b) below the 4 KB AXI bound" }
}
proc v8_perm {addr} {
    global KX_SWAP_A KX_SWAP_B
    foreach a $KX_SWAP_A b $KX_SWAP_B {
        set ba [expr {($addr >> $a) & 1}] ; set bb [expr {($addr >> $b) & 1}]
        set addr [expr {$addr & ~((1 << $a) | (1 << $b))}]
        set addr [expr {$addr | ($ba << $b) | ($bb << $a)}]
    }
    return $addr
}
puts [format "  %-6s %-14s %-4s %-14s" page addr home "in-channel"]
for {set p 0} {$p < 16} {incr p} {
    set addr [expr {$p << $KX_ILV_LG}]
    set q [v8_perm $addr]
    set home [expr {($q >> $KX_HOME_LSB) & 3}]
    set inch [expr {$q & ((1 << $KX_HOME_LSB) - 1)}]
    puts [format "  %-6d 0x%010llX %-4d 0x%010llX" $p $addr $home $inch]
    if {$home != ($p % 4)} { error "page $p lands on home $home, want [expr {$p % 4}]" }
    if {$inch != (($p / 4) << $KX_ILV_LG)} { error "page $p in-channel address is not p/4" }
}
set last [expr {($DRAM_GB << 30) - (1 << $KX_ILV_LG)}]
set q [v8_perm $last]
if {(($q & ((1 << $KX_HOME_LSB) - 1)) >> 30) >= 4} { error "the flat space overruns a channel" }
puts "@@@ memory map ok: [expr {$DRAM_GB}] GB over 4 channels at [expr {1 << ($KX_ILV_LG - 10)}] KB granularity"

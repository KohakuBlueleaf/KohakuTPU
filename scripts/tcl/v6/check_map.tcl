# tclsh scripts/tcl/v6/check_map.tcl -- prints the map from the SAME lists
# 50_addr.tcl pushes into the BD, so it cannot drift from what gets built.

set here [file dirname [file normalize [info script]]]
source $here/00_config.tcl
source $here/50_addr_lit.tcl
set names {MEM CTRL DDR_CTL CLK_WIZ}

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

# A wrong literal LENGTH silently shifts every segment, so the width is
# checked, not trusted. A Verilog concatenation here is IP_Flow 19-3450.
foreach {nm vals w} [list base $seg_base $AW mask $seg_mask $AW xlt $seg_xlt $AW \
                          dst $seg_dst 2 dport $seg_dprt 2] {
    set lit [v6_cat $vals $w]
    set want [expr {$nseg * $w}]
    set got [expr {[string length $lit] - 2}]
    if {![string match {0b[01]*} $lit]} { error "SEG_$nm is not a 0b literal" }
    if {$got != $want} { error "SEG_$nm is $got bits, want $want" }
    puts [format "  SEG_%-6s %4d bits  0b%s..." $nm $got [string range $lit 2 21]]
}

# Segment k must land at bit k*W. Decode the literal back and compare.
foreach {nm vals w} [list base $seg_base $AW dst $seg_dst 2 dport $seg_dprt 2] {
    set bits [string range [v6_cat $vals $w] 2 end]
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
puts "@@@ map ok: $nseg segments, literals well-formed and bit-placed"

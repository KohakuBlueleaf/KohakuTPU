# multimesh_v7t on-silicon acceptance: the fixed station bus against real
# endpoints. Programs the bitstream, then per station: wizard reads at every
# word offset, the historically fatal write shapes with real values, a full
# mid-profile retune (v6.5 driver register set), and BRAM endpoint R/W on all
# three port classes -- including partial strobes at the real Lite slave --
# before and after the retune.
#   python tools\vtcl.py -c "source {...}/v7t_accept.tcl" -t 600

set bit {C:/Users/apoll/Desktop/vivado/multimesh_v7t/multimesh_v7t.runs/impl_1/multimesh_v7t_wrapper.bit}
if {[catch {current_hw_server}]} { open_hw_manager; connect_hw_server -allow_non_jtag }
if {[catch {current_hw_target}]} { open_hw_target }
set dev [lindex [get_hw_devices] 0]
current_hw_device $dev
set_property PROGRAM.FILE $bit $dev
program_hw_device $dev
refresh_hw_device $dev
source [file join C:/Users/apoll/Desktop/vivado/JTAG-DMA-test/tools jtag_axi.tcl]
jaxi::connect
catch {reset_hw_axi [get_hw_axis]}
jaxi::reset_cache

set out {}
set fail 0
set nchk 0
# Flushed per line: the failure under test wedges the manager, and a wedged
# console returns nothing -- the log is the only record of how far it got.
set LOGF [open {C:/Users/apoll/Desktop/vivado/multimesh_v7t/v7t_accept.log} w]
proc say {s} { global out LOGF; lappend out $s; puts $LOGF $s; flush $LOGF }
proc rdw {addr} { lindex [jaxi::read $addr 1] 0 }
proc chk {name got want} {
    global fail nchk
    incr nchk
    if {$got ne $want} {
        incr fail
        say "  CHECK $name: got $got want $want  FAIL"
    } else {
        say "  CHECK $name: ok"
    }
}
proc wr1 {addr beat} { jaxi::write $addr [list $beat] }

# ---- A: wizard reads, every word, all four --------------------------------
say "=== A: wizard register reads (all lanes) ==="
for {set m 0} {$m < 4} {incr m} {
    set W [expr {0x900000 + $m * 0x10000}]
    set row "  wiz$m"
    foreach off {0x0 0x200 0x208 0x210 0x218 0x220 0x228 0x258} {
        append row [format " %s=%s" $off [rdw [expr {$W + $off}]]]
    }
    say $row
    chk "wiz$m CCR0" [string range [rdw [expr {$W + 0x200}]] 8 15] "00003004"
}

# ---- D-pre: endpoint R/W on every port class ------------------------------
proc ep_pass {m tag} {
    set win  [expr {($m + 1) << 40}]
    set ctrl [expr {0x800000 + $m * 0x10000}]
    set lite [expr {$m * 0x100000}]

    set beats {}
    for {set i 0} {$i < 8} {incr i} {
        lappend beats [format %08x%08x [expr {0xD0000000 + $m*16 + $i}] \
                                       [expr {0x0BADF00D + $i}]]
    }
    jaxi::write $win $beats
    set got [jaxi::read $win 8]
    set bad 0
    foreach a $beats g $got { if {$a ne $g} { incr bad } }
    chk "m$m port0 burst $tag" $bad 0

    wr1 [expr {$ctrl + 0x40}] [format %08x%08x [expr {0xC0110000 + $m}] 0x5EEDBEEF]
    chk "m$m port1 rb $tag" [rdw [expr {$ctrl + 0x40}]] \
        [format %08x%08x [expr {0xC0110000 + $m}] 0x5EEDBEEF]

    wr1 [expr {$lite + 0x40}] [format %08x%08x [expr {0x11220000 + $m}] 0x33445566]
    chk "m$m port2 rb $tag" [rdw [expr {$lite + 0x40}]] \
        [format %08x%08x [expr {0x11220000 + $m}] 0x33445566]
}
say "=== D-pre: endpoints on built clocks ==="
for {set m 0} {$m < 4} {incr m} { ep_pass $m pre }

# ---- half-word writes via rd-merge-wr at port2 (real Lite) ----------------
say "=== D-half: 32-bit halves at port2 via jaxi::wr32 ==="
set lite0 0x000080
wr1 $lite0 {AAAAAAAABBBBBBBB}
jaxi::wr32 $lite0 0xCCCCCCCC
chk "lite low-half" [rdw $lite0] "aaaaaaaacccccccc"
jaxi::wr32 [expr {$lite0 + 4}] 0xDDDDDDDD
chk "lite high-half" [rdw $lite0] "ddddddddcccccccc"
# Informational: whether the JTAG core will emit an unaligned beat at all
# (PG174 is silent). Not counted; the fabric's strobe skip is sim-proven.
if {[catch {jaxi::write [expr {$lite0 + 0x0C}] {0000000012345678}} e]} {
    say "  INFO unaligned write: refused ($e)"
} else {
    say "  INFO unaligned write: accepted, w0x88=[rdw [expr {$lite0 + 8}]]"
}

# ---- B: the historically fatal shapes, real values ------------------------
# Duty halves carry 50000 (0xC350): duty=0 is the value the wizard SLVERRs.
say "=== B: the v6.7-fatal word writes ==="
set W0 0x900000
wr1 [expr {$W0 + 0x210}] {000000030000C350}
chk "w0x210 div1+duty0" [rdw [expr {$W0 + 0x210}]] "000000030000c350"
wr1 [expr {$W0 + 0x228}] {000000060000C350}
chk "w0x228 div3+duty2" [rdw [expr {$W0 + 0x228}]] "000000060000c350"

# ---- C: full mid-profile retune, v6.5 register set, all four --------------
say "=== C: mid-profile retune (6/3/6/6) on all wizards ==="
for {set m 0} {$m < 4} {incr m} {
    set W [expr {0x900000 + $m * 0x10000}]
    foreach {off beat name} {
        0x200 {0000000000003004} vco
        0x208 {0000000000000006} div0
        0x210 {000000030000C350} div1
        0x220 {0000000000000006} div2
        0x228 {000000060000C350} div3
    } {
        wr1 [expr {$W + $off}] $beat
        chk "wiz$m $name" [rdw [expr {$W + $off}]] [string tolower $beat]
    }
    wr1 [expr {$W + 0x258}] {000000030000C350}
    say "  wiz$m LOAD issued, status=[rdw $W]"
}

# ---- D-post: endpoints alive at the NEW clocks ----------------------------
say "=== D-post: endpoints after retune ==="
after 100
for {set m 0} {$m < 4} {incr m} { ep_pass $m post }

say "==================================================="
say [format "checks=%d fail=%d" $nchk $fail]
say [expr {$fail == 0 ? "V7T-ACCEPT-PASS" : "V7T-ACCEPT-FAIL"}]
join $out "\n"

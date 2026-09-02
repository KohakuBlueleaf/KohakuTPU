# The memory-primitive tier table for the Xache: ONE launch, every tier once,
# with the boundary chain LIVE. kx_pxache's MP/HP default to "everything on
# partition 0", so an OOC run without them carries no traffic across a
# boundary: the trunks are then dead logic, kept only where xpm's CDC cells
# are, and deleted outright on the lean ring (build/ooc/kxtier_* and
# build/ooc/kxlean_* measured exactly that). MP:228 / HP:228 is partition i =
# master i = home i, the wrapper's map. The station bus (build/ooc/sbtier_*,
# sblean_lpb120) is a full sb_line4 and needed no such map.
#
#   scripts\ps1\mem_tier_sweep.ps1
$root = "C:/Users/apoll/Desktop/code/Project/KohakuTPU"
$viv  = "D:/Xilinx/Vivado/2024.2/bin/vivado.bat"

function Run-Ooc([string]$out, [string]$top, [string]$gen, [string[]]$files) {
  # A configuration is measured once: a result already on disk is reported,
  # never re-run.
  if (Test-Path "$out/result.txt") {
    Write-Output "=== $(Split-Path $out -Leaf) (on disk)"
    Write-Output ("  " + (Get-Content "$out/result.txt" -TotalCount 1))
    return
  }
  Write-Output "=== $(Split-Path $out -Leaf)"
  & $viv -mode batch -nolog -nojournal -notrace `
      -source "$root/scripts/tcl/ooc_mod.tcl" `
      -tclargs $out $top 3.333 "-" $gen @files 2>&1 |
    Select-String -Pattern '^(SYNTH FAILED|ERROR)'
  if (Test-Path "$out/result.txt") {
    Write-Output ("  " + (Get-Content "$out/result.txt" -TotalCount 1))
  } else { Write-Output "  NO RESULT" }
}

# ---- Kohaku Xache ------------------------------------------------------------
$kx = @(
  "src/kohakuaccel/common/kohaku_sdpram.v",
  "src/kohakuaccel/common/async_fifo.v",
  "src/kohakuaccel/common/sync_fifo.v",
  "src/kohakuaxi/xache/edge/kx_scdc.v",
  "src/kohakuaxi/xache/edge/kx_link.v",
  "src/kohakuaxi/xache/edge/kx_perm.v",
  "src/kohakuaccel/common/kohaku_sdpram_be.v",
  "src/kohakuaxi/xache/array/kx_carray.v",
  "src/kohakuaxi/xache/engine/kx_rd_pipe.v",
  "src/kohakuaxi/xache/engine/kx_wr_engine.v",
  "src/kohakuaccel/common/kohaku_aring.v",
  "src/kohakuaxi/pxache/lane/kx_lram.v",
  "src/kohakuaxi/pxache/lane/kx_hop.v",
  "src/kohakuaxi/pxache/lane/kx_lane.v",
  "src/kohakuaccel/common/kohaku_mux.v",
  "src/kohakuaxi/pxache/lane/kx_trunk.v",
  "src/kohakutransmit/prim/kts_fifo.v",
  "src/kohakutransmit/link/kts_tx.v",
  "src/kohakutransmit/link/kts_rx.v",
  "src/kohakutransmit/packet/kts_switch.v",
  "src/kohakuaxi/pxache/lane/kx_kedge.v",
  "src/kohakuaxi/pxache/kx_pxache.v"
) | ForEach-Object { "$root/$_" }
$kxbase = "P:4+M:4+N_HOME:4+MP:228+HP:228+SETS:16384+SET_W:14+K:2+BANKS:4+BND_TRUNK:1+BND_SCODE:1+PCLK:1+HCDC:15+RAM_STYLE:ultra"
# T0 is v8t4 as shipped. T1 moves the trunk rings, T2 the reorder buffer (with
# the 16-beat read slot that makes it 64 deep), T3 the DRAM read CDC, T4 the
# DRAM write CDC. t0_rbb16 is T0's primitives at the 16-beat slot: what the
# slot alone changes.
$kxcfg = [ordered]@{
  "t0"       = "RB_BEATS:0+MEM_TRUNK:block+MEM_RB:block+MEM_HRD:block+MEM_HWR:block"
  "t0_rbb16" = "RB_BEATS:16+MEM_TRUNK:block+MEM_RB:block+MEM_HRD:block+MEM_HWR:block"
  "t1"       = "RB_BEATS:0+MEM_TRUNK:distributed+MEM_RB:block+MEM_HRD:block+MEM_HWR:block"
  "t2"       = "RB_BEATS:16+MEM_TRUNK:distributed+MEM_RB:distributed+MEM_HRD:block+MEM_HWR:block"
  "t3"       = "RB_BEATS:16+MEM_TRUNK:distributed+MEM_RB:distributed+MEM_HRD:distributed+MEM_HWR:block"
  "t4"       = "RB_BEATS:16+MEM_TRUNK:distributed+MEM_RB:distributed+MEM_HRD:distributed+MEM_HWR:distributed"
  # T4 after the logic pass: kept slot mux, registered slot-free and credit
  # flags. Same generics; a new name because the RTL is not the same design.
  "t4x"      = "RB_BEATS:16+MEM_TRUNK:distributed+MEM_RB:distributed+MEM_HRD:distributed+MEM_HWR:distributed"
  # t4y: t4x without the kept trunk mux (kohaku_mux KEEP 0 again): the
  # registered flags alone.
  "t4y"      = "RB_BEATS:16+MEM_TRUNK:distributed+MEM_RB:distributed+MEM_HRD:distributed+MEM_HWR:distributed"
  # t4z: t4y with the request inject flit's W data riding every flit type
  # (no per-bit zeroing on AR / AW / WX).
  "t4z"      = "RB_BEATS:16+MEM_TRUNK:distributed+MEM_RB:distributed+MEM_HRD:distributed+MEM_HWR:distributed"
  # t4h: t4z plus the three Fmax registers (master inject offer, chain
  # head, write engine grant); 358 MHz at the 2.857 ask (kxlive_t4h_350).
  "t4h"      = "RB_BEATS:16+MEM_TRUNK:distributed+MEM_RB:distributed+MEM_HRD:distributed+MEM_HWR:distributed"
}
foreach ($t in $kxcfg.Keys) {
  Run-Ooc "$root/build/ooc/kxlive_$t" kx_pxache "$kxbase+$($kxcfg[$t])" $kx
}

# ---- station bus: the tiers past S1 ----------------------------------------
# S0 (sbtier_lpb0) and S1 (sblean_lpb120) are on disk. S1 leaves 19 tiles, all
# station 1's two burst managers: xdma request 647 x 256 (9 tiles), xdma
# response 264 x 256 (4), jtag 143 x 256 (2) and 264 x 256 (4). The NMU
# charges width x ceil(depth/32) a queue against its tiles: xdma 64 / 128 is
# 144 / 264 a tile, 128 / 128 is 288 / 264, 128 / 256 is 288 / 528; jtag at
# its 256 floor is 572 / 528. S2 converts the xdma pair at a depth pair with
# station 1's threshold just above it; S3 (580) converts jtag's too.
$sb = @(
  "src/kohakuaccel/common/kohaku_aring.v",
  "src/kohakuaccel/common/kohaku_mux.v",
  "src/kohakuaccel/common/sync_fifo.v",
  "src/kohakuaccel/common/async_fifo.v",
  "src/kohakuaccel/common/sb_skid.v",
  "src/kohakuaccel/axi/station/sb_hub.v",
  "src/kohakuaccel/axi/station/sb_station.v",
  "src/kohakuaccel/axi/station/sb_nmu.v",
  "src/kohakuaccel/axi/station/sb_nsu.v",
  "src/kohakuaccel/axi/link/sb_link.v",
  "src/kohakuaccel/axi/link/sb_link_cdc.v",
  "src/kohakuaccel/axi/link/sb_link_kts.v",
  "src/kohakutransmit/prim/kts_fifo.v",
  "src/kohakutransmit/link/kts_tx.v",
  "src/kohakutransmit/link/kts_rx.v",
  "src/kohakutransmit/carrier/kts_pipe.v",
  "src/kohakuaccel/axi/topo/sb_stn_line.v",
  "src/kohakuaccel/axi/topo/sb_line4.v"
) | ForEach-Object { "$root/$_" }
$sbbase = "FW:256+AW:43+NQ:4+PORTW:2+LINK_FULL:0+OST:4+STORE_FWD:1+LINK_CDC:1+MGR0_DOM:1+LUT_PER_BRAM:120"
$sbcfg = [ordered]@{
  # S1 again, as the witness that the line is byte-for-byte what sblean_lpb120
  # measured (26,487 / 41,484 / 19) after its MGR0_DOM knob was restored.
  "s1_witness" = "LPB1:120"
  "s2_64_128"  = "LPB1:270+MREQ1:64+MRSP1:128"
  "s2_128_128" = "LPB1:290+MREQ1:128+MRSP1:128"
  "s3_128_256" = "LPB1:580+MREQ1:128+MRSP1:256"
  "s3_64_128"  = "LPB1:580+MREQ1:64+MRSP1:128"
  # S2 after the logic pass: kept hub payload select on 4-source hubs,
  # registered credit reclaim in the managers.
  "s2x_64_128" = "LPB1:270+MREQ1:64+MRSP1:128"
}
foreach ($t in $sbcfg.Keys) {
  Run-Ooc "$root/build/ooc/sblean_$t" sb_line4 "$sbbase+$($sbcfg[$t])" $sb
}
Write-Output "MEM_TIER_SWEEP_DONE"

# run_synth_check.ps1 -- does the NoC RTL close its target frequency?
#
#   .\tests\run_synth_check.ps1                 # router, CU base, orchestrator @ 300 MHz
#   .\tests\run_synth_check.ps1 -Freq 400       # a different target
#   .\tests\run_synth_check.ps1 -Only noc_router
#
# Out-of-context, one module at a time. That answers "is the logic depth sane?",
# not "will the full mesh close" -- 196 routers plus interconnect will not hold the
# same slack as one router alone. Use it to catch a design that cannot possibly
# make the target, and to keep the LUT arithmetic in .plan/decisions.md honest.
#
# Each run writes its full Vivado log next to the results, because a synthesis
# failure is usually explained halfway up the log rather than at the end.

param(
    [double]$Freq = 300,
    # the carrier board's actual device (JTAG-DMA-test.xpr), not the faster -2-e
    [string]$Part = "xcvu13p-fhgb2104-2L-e",
    [string[]]$Only,
    # NAME:VALUE pairs joined by '+', e.g. "FIFO_DEPTH:8+MEMORY_TYPE:block".
    # Not '=' (Vivado's .bat wrappers split on it) and not ',' (powershell -File
    # splits a string argument on it). Both were learned the hard way.
    [string]$Generics = "-",
    [string]$VivadoBin = "D:\Xilinx\Vivado\2024.2\bin",
    # Logs and results land here. Two concurrent invocations share it by
    # default and overwrite each other's <Top>.log; give one its own to compare
    # a variant against a baseline, or to run beside someone else.
    [string]$Work = "",
    [switch]$KeepWork
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$work = if ($Work) { $Work } else { Join-Path $env:TEMP "kohakutpu-synthcheck" }
$period = [math]::Round(1000.0 / $Freq, 4)

$common = @("src\common\sync_fifo.v", "src\common\kohaku_sdpram.v")

# Module names, not file names -- the router and its ports predate the snake_case
# convention the newer modules use.
$targets = @(
    @{ Top = 'NoCRouter'
       Src = @("src\kohakunoc\noc_inport.v", "src\kohakunoc\noc_outport.v",
               "src\kohakunoc\noc_router.v") },
    @{ Top = 'noc_cu_base'
       Src = @("src\kohakunoc\noc_cu_base.v") },
    # N masters onto one DRAM across two clocks, replacing a SmartConnect that
    # measured 20,104 LUT at 4S/1M. Override N to price a configuration.
    @{ Top = 'axi_n1'
       Src = @("src\common\async_fifo.v", "src\kohakuaxi\axi_n1.v") },
    # The BD-facing wrapper. Pure wiring, so its area must match `axi_n1` at the
    # same N -- a difference means something was left unconnected.
    @{ Top = 'axi_n1_wrap_4'
       Src = @("src\common\async_fifo.v", "src\kohakuaxi\axi_n1.v",
               "src\synth_top\axi_n1_wrap_4.v") },
    # N=5 is 3 MAG ports + upload + mover, the 6-cluster shape. REGISTERED
    # RATHER THAN DELETED: it shipped generated but unreferenced, and a
    # generated file no target reads is unverified however green the tier is.
    @{ Top = 'axi_n1_wrap_5'
       Src = @("src\common\async_fifo.v", "src\kohakuaxi\axi_n1.v",
               "src\synth_top\axi_n1_wrap_5.v") },
    @{ Top = 'noc_orchestrator'
       Src = @("src\kohakunoc\noc_orchestrator.v") },
    # Two routers wired east-west. A single router cannot measure the link between
    # routers, and the busy path genuinely crosses it -- see src\synth_top\noc_pair.v.
    @{ Top = 'noc_pair_synth'
       Src = @("src\kohakunoc\noc_inport.v", "src\kohakunoc\noc_outport.v",
               "src\kohakunoc\noc_router.v", "src\synth_top\noc_pair.v") },
    # The NoC tax, isolated. noc_cu_null has no arithmetic, so the difference
    # between it and noc_cu_base is the flit-fold that keeps synthesis from
    # pruning, and the cluster divided by 12 is what one endpoint really costs.
    @{ Top = 'noc_cu_null'
       Src = @("src\kohakunoc\noc_cu_base.v", "src\kohakunoc\noc_cu_null.v") },
    @{ Top = 'noc_cluster_2x2'
       Src = @("src\kohakunoc\noc_inport.v", "src\kohakunoc\noc_outport.v",
               "src\kohakunoc\noc_router.v", "src\kohakunoc\noc_cu_base.v",
               "src\kohakunoc\noc_cu_null.v", "src\synth_top\noc_cluster_2x2.v") },
    # Same parts at a different router:endpoint ratio (1:5 vs 4:12), so the split
    # between router cost and endpoint cost is solvable rather than assumed.
    @{ Top = 'noc_tile_1r'
       Src = @("src\kohakunoc\noc_inport.v", "src\kohakunoc\noc_outport.v",
               "src\kohakunoc\noc_router.v", "src\kohakunoc\noc_cu_base.v",
               "src\kohakunoc\noc_cu_null.v", "src\synth_top\noc_tile_1r.v") },
    # MX matmul datapath. mx_mac defaults to MODEL=0, the real DSP48E2, so these
    # measure the packed cascade rather than inferred multipliers.
    @{ Top = 'mx_tcu'
       Src = @("src\kohakutpu\matmul\mx_mac.v", "src\kohakutpu\matmul\mx_tcu.v") },
    @{ Top = 'mx_cluster'
       Src = @("src\kohakutpu\matmul\mx_mac.v", "src\kohakutpu\matmul\mx_tcu.v",
               "src\kohakutpu\matmul\mx_acu.v", "src\kohakutpu\matmul\mx_cluster_core.v",
               "src\kohakutpu\matmul\mx_cluster.v") },
    # Explicit memory, so the primitive count for a given shape is measured
    # rather than argued from the datasheet. PRIM 0/1/2 = distributed/block/ultra.
    @{ Top = 'sdpram_probe'
       Src = @("src\synth_top\sdpram_probe.v") },
    # The production accumulator and the full NoC-attached compute unit.
    @{ Top = 'mx_acu_fp'
       Src = @("src\kohakutpu\matmul\mx_fpacc.v", "src\kohakutpu\matmul\mx_acu_fp.v") },
    # The real architecture: a cluster as a one-port NoC endpoint.
    @{ Top = 'mx_cluster_cu'
       Src = @("src\kohakunoc\noc_cu_base.v",
               "src\kohakutpu\matmul\mx_mac.v", "src\kohakutpu\matmul\mx_tcu.v",
               "src\kohakutpu\matmul\mx_fpacc.v", "src\kohakutpu\matmul\mx_acu_fp.v",
               "src\kohakutpu\matmul\mx_cluster_core.v",
               "src\kohakutpu\matmul\mx_cluster_mgr.v",
               "src\kohakutpu\matmul\mx_cluster_node.v",
               "src\kohakutpu\matmul\mx_cluster_cu.v") },
    # The coefficient ROMs alone. Synthesised separately because the estimate in
    # vector-core.md s9 has to be attributable: the tables are PER LANE (every
    # lane looks up a different segment on the same cycle), so if they dominate
    # a lane then they dominate the whole vector core, and that is an
    # architectural fact rather than a coding detail.
    @{ Top = 'vec_tab_probe'
       Src = @("src\kohakutpu\vector\vec_tables.v",
               "src\synth_top\vec_tab_probe.v") },
    # One vector ALU lane: three DSP48E2 and the fabric around them. mx_fpacc.v
    # is here for mx_lead1 alone -- the leading-one search is shared with the
    # accumulator rather than duplicated, and synthesis prunes the rest.
    @{ Top = 'vec_alu'
       Src = @("src\kohakutpu\matmul\mx_fpacc.v",
               "src\kohakutpu\vector\vec_dsp.v",
               "src\kohakutpu\vector\vec_delay.v",
               "src\kohakutpu\vector\vec_tables.v",
               "src\kohakutpu\vector\vec_alu.v") },
    # Philox-4x32-10 alone. One round per cycle, so the path to measure is the
    # two 32x32 multiplies and the XOR tree around them.
    @{ Top = 'mm_prng'
       Src = @("src\kohakumas\mm_prng.v") },
    # The memory mover: two mx_tdesc walkers, the index buffer, the PRNG and an
    # AXI master. mx_tdesc carries no multipliers by construction, so if this
    # misses the target the cause is the descriptor mux or the gather address.
    @{ Top = 'mm_mover'
       Src = @("src\kohakutpu\matmul\mx_tdesc.v",
               "src\kohakumas\mm_prng.v",
               "src\kohakumas\mm_mover.v") },
    # The 16-ALU array, and then the whole vector core as a NoC endpoint.
    # vec_alu was measured alone at 324.8 MHz; these say what the core around it
    # costs, which nothing measured until the mesh was assembled.
    @{ Top = 'vec_lanes'
       Src = @("src\kohakutpu\matmul\mx_fpacc.v",
               "src\kohakutpu\vector\vec_dsp.v", "src\kohakutpu\vector\vec_delay.v",
               "src\kohakutpu\vector\vec_tables.v", "src\kohakutpu\vector\vec_alu.v",
               "src\kohakutpu\vector\vec_regfile.v",
               "src\kohakutpu\vector\vec_lanes.v") },
    @{ Top = 'vec_cu'
       Src = @("src\kohakunoc\noc_cu_base.v",
               "src\kohakutpu\matmul\mx_fpacc.v",
               "src\kohakutpu\vector\vec_dsp.v", "src\kohakutpu\vector\vec_delay.v",
               "src\kohakutpu\vector\vec_tables.v", "src\kohakutpu\vector\vec_alu.v",
               "src\kohakutpu\vector\vec_cvt.v", "src\kohakutpu\vector\vec_regfile.v",
               "src\kohakutpu\vector\vec_lanes.v", "src\kohakutpu\vector\vec_agu.v",
               "src\kohakutpu\vector\vec_core.v", "src\kohakutpu\vector\vec_cu.v") },
    # The minimal machine: MAG (agent, ports, mover), one cluster, one vector
    # core, two routers. One-module-at-a-time synthesis cannot say whether the
    # parts are compatible at the assembly level; this can.
    @{ Top = 'mm_mesh'
       Src = @("src\kohakunoc\noc_inport.v", "src\kohakunoc\noc_outport.v",
               "src\kohakunoc\noc_router.v", "src\kohakunoc\noc_cu_base.v",
               "src\kohakunoc\noc_orchestrator.v",
               "src\kohakutpu\matmul\mx_mac.v", "src\kohakutpu\matmul\mx_tcu.v",
               "src\kohakutpu\matmul\mx_fpacc.v", "src\kohakutpu\matmul\mx_acu_fp.v",
               "src\kohakutpu\matmul\mx_cluster_core.v",
               "src\kohakutpu\matmul\mx_cluster_mgr.v",
               "src\kohakutpu\matmul\mx_cluster_node.v",
               "src\kohakutpu\matmul\mx_cluster_cu.v",
               "src\kohakutpu\matmul\mx_tdesc.v",
               "src\kohakutpu\vector\vec_dsp.v", "src\kohakutpu\vector\vec_delay.v",
               "src\kohakutpu\vector\vec_tables.v", "src\kohakutpu\vector\vec_alu.v",
               "src\kohakutpu\vector\vec_cvt.v", "src\kohakutpu\vector\vec_regfile.v",
               "src\kohakutpu\vector\vec_lanes.v", "src\kohakutpu\vector\vec_agu.v",
               "src\kohakutpu\vector\vec_core.v", "src\kohakutpu\vector\vec_cu.v",
               "src\kohakumas\mx_quant.v", "src\kohakumas\mag_mem_port.v",
               "src\kohakumas\mm_prng.v", "src\kohakumas\mm_mover.v",
               "src\kohakumas\mag.v", "src\synth_top\mm_mesh.v") },
    # THE SHIP CONFIGURATION. One SLR, 2x2 routers, 4 clusters, 4 vector cores,
    # 2 MAG ports -- src\synth_top\maps\mesh_2x2_4cu4vec.txt. This is the only
    # target that answers "does the machine we intend to build close timing",
    # and it is LARGE: expect tens of minutes, so run it deliberately rather
    # than as part of a sweep.
    @{ Top = 'ktpu_ship_2x2'
       Src = @("src\kohakunoc\noc_inport.v", "src\kohakunoc\noc_outport.v",
               "src\kohakunoc\noc_router.v", "src\kohakunoc\noc_cu_base.v",
               "src\kohakunoc\noc_orchestrator.v",
               "src\kohakutpu\matmul\mx_mac.v", "src\kohakutpu\matmul\mx_tcu.v",
               "src\kohakutpu\matmul\mx_fpacc.v", "src\kohakutpu\matmul\mx_acu_fp.v",
               "src\kohakutpu\matmul\mx_cluster_core.v",
               "src\kohakutpu\matmul\mx_cluster_mgr.v",
               "src\kohakutpu\matmul\mx_cluster_node.v",
               "src\kohakutpu\matmul\mx_cluster_cu.v",
               "src\kohakutpu\matmul\mx_tdesc.v",
               "src\kohakutpu\vector\vec_dsp.v", "src\kohakutpu\vector\vec_delay.v",
               "src\kohakutpu\vector\vec_tables.v", "src\kohakutpu\vector\vec_alu.v",
               "src\kohakutpu\vector\vec_cvt.v", "src\kohakutpu\vector\vec_regfile.v",
               "src\kohakutpu\vector\vec_lanes.v", "src\kohakutpu\vector\vec_agu.v",
               "src\kohakutpu\vector\vec_core.v", "src\kohakutpu\vector\vec_cu.v",
               "src\kohakumas\mx_quant.v", "src\kohakumas\mag_mem_port.v",
               "src\kohakumas\mm_prng.v", "src\kohakumas\mm_mover.v",
               "src\kohakumas\mag.v", "src\synth_top\ktpu_ship_2x2.v") },
    # 6+4 on 3 rows x 2 cols of routers -- src\synth_top\maps\mesh_3x2_6+4.txt.
    # Three MAG ports, so five AXI masters: the axi_n1_wrap_5 width.
    @{ Top = 'ktpu_ship_3x2'
       Src = @("src\kohakunoc\noc_inport.v", "src\kohakunoc\noc_outport.v",
               "src\kohakunoc\noc_router.v", "src\kohakunoc\noc_cu_base.v",
               "src\kohakunoc\noc_orchestrator.v",
               "src\kohakutpu\matmul\mx_mac.v", "src\kohakutpu\matmul\mx_tcu.v",
               "src\kohakutpu\matmul\mx_fpacc.v", "src\kohakutpu\matmul\mx_acu_fp.v",
               "src\kohakutpu\matmul\mx_cluster_core.v",
               "src\kohakutpu\matmul\mx_cluster_mgr.v",
               "src\kohakutpu\matmul\mx_cluster_node.v",
               "src\kohakutpu\matmul\mx_cluster_cu.v",
               "src\kohakutpu\matmul\mx_tdesc.v",
               "src\kohakutpu\vector\vec_dsp.v", "src\kohakutpu\vector\vec_delay.v",
               "src\kohakutpu\vector\vec_tables.v", "src\kohakutpu\vector\vec_alu.v",
               "src\kohakutpu\vector\vec_cvt.v", "src\kohakutpu\vector\vec_regfile.v",
               "src\kohakutpu\vector\vec_lanes.v", "src\kohakutpu\vector\vec_agu.v",
               "src\kohakutpu\vector\vec_core.v", "src\kohakutpu\vector\vec_cu.v",
               "src\kohakumas\mx_quant.v", "src\kohakumas\mag_mem_port.v",
               "src\kohakumas\mm_prng.v", "src\kohakumas\mm_mover.v",
               "src\kohakumas\mag.v", "src\synth_top\ktpu_ship_3x2.v") },
    # One MAG port. Measured by hand twice and registered only after the second
    # time -- a target no script names is a target nobody re-measures.
    @{ Top = 'mag_mem_port'
       Src = @("src\kohakumas\mx_quant.v", "src\kohakumas\mag_mem_port.v") },
    @{ Top = 'mx_matmul_cu'
       Src = @("src\kohakunoc\noc_cu_base.v",
               "src\kohakutpu\matmul\mx_mac.v", "src\kohakutpu\matmul\mx_tcu.v",
               "src\kohakutpu\matmul\mx_fpacc.v", "src\kohakutpu\matmul\mx_acu_fp.v",
               "src\kohakutpu\matmul\mx_cluster_core.v",
               "src\kohakutpu\matmul\mx_matmul_cu.v") }
)

if ($Only) { $targets = $targets | Where-Object { $Only -contains $_.Top } }
if (-not $targets) { throw "no matching targets" }

# Not wiped between runs: a parameter sweep is several invocations, and each one
# wiping the directory would delete the logs the previous one just produced.
New-Item -ItemType Directory -Force $work | Out-Null
Push-Location $work
$results = @()
try {
    $env:PATH = "$VivadoBin;$env:PATH"
    Write-Host "target $Freq MHz ($period ns) on $Part" -ForegroundColor Cyan

    foreach ($t in $targets) {
        $files = ($common + $t.Src) | ForEach-Object { Join-Path $root $_ }
        foreach ($f in $files) { if (-not (Test-Path $f)) { throw "missing source: $f" } }

        Write-Host ""
        Write-Host "=============== $($t.Top) ===============" -ForegroundColor Cyan
        $tag = if ($Generics -eq "-") { $t.Top } else { "$($t.Top)_$($Generics -replace '[:,+]','_')" }
        $log = Join-Path $work "$tag.log"

        & vivado.bat -mode batch -nojournal -notrace `
            -log $log `
            -source (Join-Path $root "scripts\synth_check.tcl") `
            -tclargs $t.Top $period $Part $Generics @files 2>&1 |
            Where-Object { $_ -match '^RESULT|^VERDICT|^  UTIL|^  PATH|^SYNTH FAILED|ERROR:' } |
            ForEach-Object {
                if ($_ -match '^VERDICT.*MISSES') { Write-Host $_ -ForegroundColor Red }
                elseif ($_ -match '^VERDICT')     { Write-Host $_ -ForegroundColor Green }
                else                              { Write-Host $_ }
                $script:results += $_
            }

        if ($LASTEXITCODE -ne 0) {
            Write-Host "  synthesis failed -- see $log" -ForegroundColor Red
            $results += "VERDICT $($t.Top): SYNTHESIS FAILED"
        }
    }

    Write-Host ""
    Write-Host "--- summary ---"
    $results | Where-Object { $_ -match '^VERDICT' } | ForEach-Object { Write-Host $_ }
    if ($results -match 'MISSES|FAILED') { exit 1 }
    exit 0
}
finally {
    Pop-Location
    if (-not $KeepWork) {
        Write-Host "logs kept in $work"
    }
}

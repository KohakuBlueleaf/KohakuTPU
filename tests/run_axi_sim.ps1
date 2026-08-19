# run_axi_sim.ps1 -- simulate the AXI4 slave with the hostile testbench.
#
#   .\tests\run_axi_sim.ps1                    # test src/kohakuaccel/verif/axi4_ram.v
#   .\tests\run_axi_sim.ps1 -Dut path\to.v     # test a different slave (same ports)
#
# Uses Vivado's xsim, so there's nothing extra to install.
# Exit code 0 = pass.

param(
    # Override to test your own slave against the hostile bench. Leave unset to
    # run the full AXI suite: reference slave, then master-against-slave.
    [string]$Dut,
    [string]$Tb,
    [string]$Top,
    [string]$VivadoBin = "D:\Xilinx\Vivado\2024.2\bin",
    [ValidateSet('auto','xsim','iverilog')]
    [string]$Sim      = 'auto',
    [switch]$KeepWork
)

# name, sources, top
$suite = @(
    @{ Name = 'slave (hostile master)'
       Src  = @("src\kohakuaxi\axi4_ram.v", "tests\axi\axi4_ram_tb.v")
       Top  = 'axi4_ram_tb' },
    @{ Name = 'master (burst legality + data)'
       Src  = @("src\kohakuaxi\axi4_ram.v", "src\kohakuaxi\axi4_master.v",
                "tests\axi\axi4_master_tb.v")
       Top  = 'axi4_master_tb' }
)
if ($Dut) {
    if (-not $Tb)  { $Tb  = "tests\axi\axi4_ram_tb.v" }
    if (-not $Top) { $Top = "axi4_ram_tb" }
    $suite = @(@{ Name = "custom: $Dut"; Src = @($Dut, $Tb); Top = $Top })
}

# Run under BOTH simulators when you can. iverilog is permissive about things
# Vivado rejects -- most notably use-before-declaration, which is how a forward
# reference to an undeclared identifier can quietly become an implicit 1-bit net
# in one tool and a hard error in another. Passing iverilog is not evidence that
# Vivado will even compile the file, let alone synthesise it the same way.
if ($Sim -eq 'auto') {
    $Sim = if (Get-Command iverilog -ErrorAction SilentlyContinue) { 'iverilog' } else { 'xsim' }
}

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$work = Join-Path $env:TEMP "kohakutpu-axisim"

if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Force $work | Out-Null
Push-Location $work
try {
    Write-Host "simulator: $Sim"
    $failed = @()
    $n = 0

    foreach ($t in $suite) {
        $n++
        $paths = $t.Src | ForEach-Object { Join-Path $root $_ }
        foreach ($p in $paths) { if (-not (Test-Path $p)) { throw "not found: $p" } }

        Write-Host ""
        Write-Host "=============== $($t.Name) ===============" -ForegroundColor Cyan

        if ($Sim -eq 'iverilog') {
            & iverilog -g2005-sv -o "tb$n.vvp" -s $t.Top $paths 2>&1 |
                ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) { $failed += $t.Name; continue }
            $out = & vvp "tb$n.vvp" 2>&1
        }
        else {
            $env:PATH = "$VivadoBin;$env:PATH"
            & xvlog.bat -work "w$n" $paths 2>&1 |
                Where-Object { $_ -match 'ERROR|CRITICAL' } | ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) { $failed += "$($t.Name) (compile)"; continue }

            # RTL has no `timescale (correct for synthesis) -- supply one here
            & xelab.bat -debug typical -timescale 1ns/1ps "w$n.$($t.Top)" -s "tb$n" 2>&1 |
                Where-Object { $_ -match 'ERROR|CRITICAL' } | ForEach-Object { Write-Host $_ }
            if ($LASTEXITCODE -ne 0) { $failed += "$($t.Name) (elaboration)"; continue }

            $out = & xsim.bat "tb$n" -runall 2>&1
        }

        $out | Where-Object { $_ -match 'PASS|FAIL|ERROR|^---|^  |====' } |
            ForEach-Object { Write-Host $_ }

        if ($out -match 'WATCHDOG TIMEOUT') {
            Write-Host "`nDeadlock. Almost always one of:" -ForegroundColor Red
            Write-Host "  * VALID computed from READY (e.g. assign RVALID = RREADY && ...)"
            Write-Host "  * a burst that never emits its B response or its RLAST beat"
            Write-Host "  * a state register too narrow to hold all its states"
            $failed += "$($t.Name) (watchdog)"
        }
        elseif ($out -match 'FAIL')        { $failed += $t.Name }
        elseif (-not ($out -match 'PASS')) { $failed += "$($t.Name) (no verdict)" }
    }

    Write-Host ""
    if ($failed.Count -gt 0) {
        Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red
        exit 1
    }
    Write-Host "all AXI tests passed" -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
    if (-not $KeepWork -and (Test-Path $work)) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
}

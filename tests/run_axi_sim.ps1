# run_axi_sim.ps1 -- simulate the AXI4 slave with the hostile testbench.
#
#   .\tests\run_axi_sim.ps1                    # test src/hakuaxi/axi4_ram.v
#   .\tests\run_axi_sim.ps1 -Dut path\to.v     # test a different slave (same ports)
#
# Uses Vivado's xsim, so there's nothing extra to install.
# Exit code 0 = pass.

param(
    [string]$Dut      = "src\hakuaxi\axi4_ram.v",
    [string]$Tb       = "tests\axi\axi4_ram_tb.v",
    [string]$Top      = "axi4_ram_tb",
    [string]$VivadoBin = "D:\Xilinx\Vivado\2024.2\bin",
    [ValidateSet('auto','xsim','iverilog')]
    [string]$Sim      = 'auto',
    [switch]$KeepWork
)

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
$work = Join-Path $env:TEMP "hakutpu-axisim"

if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Force $work | Out-Null
Push-Location $work
try {
    $dutPath = Join-Path $root $Dut
    $tbPath  = Join-Path $root $Tb
    if (-not (Test-Path $dutPath)) { throw "DUT not found: $dutPath" }
    if (-not (Test-Path $tbPath))  { throw "TB not found: $tbPath" }

    Write-Host "simulator: $Sim"
    Write-Host "compiling $Dut + $Tb ..."

    if ($Sim -eq 'iverilog') {
        & iverilog -g2005-sv -o tb.vvp -s $Top $dutPath $tbPath 2>&1 |
            ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { throw "iverilog failed" }
        $out = & vvp tb.vvp 2>&1
    }
    else {
        $env:PATH = "$VivadoBin;$env:PATH"
        & xvlog.bat $dutPath $tbPath 2>&1 | Where-Object { $_ -match 'ERROR|CRITICAL' } | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { throw "xvlog failed" }

        # RTL has no `timescale (correct for synthesis) -- supply one to the elaborator
        & xelab.bat -debug typical -timescale 1ns/1ps $Top -s tb 2>&1 |
            Where-Object { $_ -match 'ERROR|CRITICAL' } | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) { throw "xelab failed" }

        $out = & xsim.bat tb -runall 2>&1
    }
    $out | Where-Object { $_ -match 'PASS|FAIL|ERROR|---|====' } | ForEach-Object { Write-Host $_ }

    if ($out -match 'WATCHDOG TIMEOUT') {
        Write-Host "`nDeadlock. Almost always one of:" -ForegroundColor Red
        Write-Host "  * VALID computed from READY (e.g. assign RVALID = RREADY && ...)"
        Write-Host "  * a burst that never emits its B response or its RLAST beat"
        Write-Host "  * a state register too narrow to hold all its states"
        exit 1
    }
    if ($out -match 'FAIL') { exit 1 }
    if ($out -match 'PASS') { exit 0 }
    Write-Host "no verdict found in simulator output" -ForegroundColor Yellow
    exit 1
}
finally {
    Pop-Location
    if (-not $KeepWork -and (Test-Path $work)) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
}

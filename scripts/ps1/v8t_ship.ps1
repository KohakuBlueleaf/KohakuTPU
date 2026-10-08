# One multimesh image from start to bitstream: block design (rebuild), verify
# (stops on a FAIL line), synthesis, impl through write_bitstream and the report.
# -Jobs is synth_1's; OOC IP runs use the config's OOC_JOBS. Launch detached:
#   Invoke-CimMethod Win32_Process Create -Arguments @{CurrentDirectory=<repo>;
#     CommandLine='pwsh -NoProfile -File scripts/ps1/v8t_ship.ps1 -Ver v8t8 -Jobs 4'}
param(
    [Parameter(Mandatory = $true)][string]$Ver,
    [int]$Jobs = 4
)
$root = "C:/Users/apoll/Desktop/code/Project/KohakuTPU"
$viv  = "D:/Xilinx/Vivado/2024.2/bin/vivado.bat"
$L    = "C:/Users/apoll/Desktop/vivado"
$stage = "$L/multimesh_${Ver}_ship.log"
Set-Location $root

function Mark([string]$s) { Add-Content -Path $stage -Value "$(Get-Date -Format 'HH:mm:ss')  $s" -Encoding utf8 }

Mark "bd: rebuild"
& $viv -mode batch -log "$L/multimesh_${Ver}_bd.log" -nojournal -notrace `
    -source "scripts/tcl/multimesh_${Ver}_bd.tcl" -tclargs rebuild jobs $Jobs | Out-Null
if ($LASTEXITCODE -ne 0) { Mark "BD FAILED ($LASTEXITCODE): $L/multimesh_${Ver}_bd.log"; exit 1 }

Mark "verify: 75_verify_bd"
$v = & $viv -mode batch -log "$L/multimesh_${Ver}_verify.log" -nojournal -notrace `
    -source "scripts/tcl/${Ver}_verify.tcl" 2>&1
$fails = @($v | Where-Object { $_ -match '@@@ FAIL' })
if ($fails.Count -gt 0 -or $LASTEXITCODE -ne 0) {
    Mark "VERIFY FAILED: $($fails.Count) FAIL line(s), exit $LASTEXITCODE -- $L/multimesh_${Ver}_verify.log"
    exit 1
}
Mark "verify: clean"

Mark "synth: OOC module runs, synth_1"
& $viv -mode batch -log "$L/multimesh_${Ver}_synth.log" -nojournal -notrace `
    -source "scripts/tcl/multimesh_${Ver}_bd.tcl" -tclargs synth jobs $Jobs | Out-Null
if ($LASTEXITCODE -ne 0) { Mark "SYNTH FAILED ($LASTEXITCODE): $L/multimesh_${Ver}_synth.log"; exit 1 }

# Impl, retried once when runme.log reports Mig 66-119 (a %TEMP% transient).
$runme = "$L/multimesh_${Ver}/multimesh_${Ver}.runs/impl_1/runme.log"
for ($try = 1; $try -le 2; $try++) {
    Mark "impl: through write_bitstream and the report (try $try)"
    & $viv -mode batch -log "$L/multimesh_${Ver}_impl.log" -nojournal -notrace `
        -source "scripts/tcl/${Ver}_impl.tcl" | Out-Null
    if ($LASTEXITCODE -eq 0) { Mark "SHIP DONE"; exit 0 }
    $mig = (Test-Path $runme) -and (Select-String -Quiet -Path $runme -Pattern 'Mig 66-119')
    if (-not $mig) { break }
    Mark "impl: Mig 66-119 transient"
}
Mark "IMPL FAILED ($LASTEXITCODE): $L/multimesh_${Ver}_impl.log"
exit 1

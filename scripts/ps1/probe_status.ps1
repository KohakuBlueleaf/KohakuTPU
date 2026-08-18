<#
.SYNOPSIS
    Show how far each single-mesh placement probe has got.

.DESCRIPTION
    Reads each probe's run.log and its synth_1/impl_1 runme.log and prints one
    line per probe: pipeline stage, current sub-step, elapsed time, how long
    the log has been silent, and WNS/TNS/WHS/THS once a stage reports them.

    The idle column is the one to watch. A probe whose log has not moved for
    many minutes is stalled, not slow -- Vivado writes progress continuously.

    TNS is the column that separates "one bad path" from "a bad design": two
    probes at the same WNS have differed by 10x on TNS in this study. Hold
    (whs/ths) is printed only by the router, so a pre-route probe has none --
    blank means not measured yet, not zero. Mid-route hold is routinely deep
    negative and cleaned up by the router's own hold-fix phase; every completed
    run here finished at THS 0.

.PARAMETER Root
    Directory holding one subdirectory per probe. Default C:\Users\apoll\Desktop\vivado\probe.

.PARAMETER Watch
    Redraw until Ctrl+C instead of printing once.

.PARAMETER Every
    Seconds between redraws in -Watch mode. Default 20.

.PARAMETER Errors
    Also print the last error line of any failed probe.

.PARAMETER Filter
    Which probes to list: all, running, done (running + finished), or alive
    (everything except dead and failed). In -Watch mode the a/r/d/h keys switch
    it live and q quits. The slot count always counts every probe, filtered or
    not -- the six-run ceiling is a property of the machine, not of the view.

.EXAMPLE
    .\probe_status.ps1 -Watch

.EXAMPLE
    .\probe_status.ps1 -Errors
#>
param(
    [string]$Root = 'C:\Users\apoll\Desktop\vivado\probe',
    [switch]$Watch,
    [int]$Every = 20,
    [switch]$Errors,
    [ValidateSet('all', 'running', 'done', 'alive')]
    [string]$Filter = 'all'
)

$script:Mode = $Filter

# key -> mode, shown in the footer legend so the bindings are never guessed
$Keys = @{ 'a' = 'all'; 'r' = 'running'; 'd' = 'done'; 'h' = 'alive' }

function Test-Row {
    param($r)
    switch ($script:Mode) {
        'running' { $r.state -in @('running', 'retry') }
        'done'    { $r.state -in @('running', 'retry', 'done') }
        'alive'   { $r.state -notin @('dead', 'failed') }
        default   { $true }
    }
}

$Steps = @('bd', 'queued', 'ooc', 'synth', 'impl', 'opt', 'place', 'phys_opt',
           'route', 'done')

# Hard ceiling. Each impl grows to ~20 GB; a 7th run starves runs hours deep,
# and opening a routed checkpoint to analyse one costs a slot just the same.
$SLOTS = 6

$Fail = @(
    'Design is not routable',
    'IO Placement failed',
    'failed due to earlier errors',
    'Error in initialization of Rule object',
    'not the BD wrapper'
)

function Get-ProbeStatus {
    param([System.IO.DirectoryInfo]$Dir)

    $tag = $Dir.Name
    $runLog = Join-Path $Dir.FullName 'run.log'
    $st = [ordered]@{
        probe = $tag; stage = 'pending'; step = ''; elapsed = ''
        idle = ''; idleMin = 0.0; wns = ''; tns = ''; whs = ''; ths = ''; cong = ''
        state = 'pending'; err = ''; startedAt = [datetime]::MinValue
    }
    if (-not (Test-Path $runLog)) { return [pscustomobject]$st }

    $runs = Join-Path $Dir.FullName "mp_$tag.runs"
    $synthLog = Join-Path $runs 'synth_1\runme.log'
    # A probe can carry several implementation runs (impl_1, impl_strat,
    # impl_knobs...). Report on whichever ran most recently.
    $implRun = Get-ChildItem (Join-Path $runs 'impl*\runme.log') -ErrorAction SilentlyContinue |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $implLog = if ($implRun) { $implRun.FullName } else { Join-Path $runs 'impl_1\runme.log' }
    # run.log is the BD build; impl.log is the separate synth+impl driver.
    $topLog = Join-Path $Dir.FullName 'impl.log'

    # Newest OOC runme.log too: during OOC synthesis nothing writes impl.log
    # for many minutes, which reads as a stall when the run is perfectly busy.
    $ooc = Get-ChildItem (Join-Path $runs '*\runme.log') -ErrorAction SilentlyContinue |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1
    # An impl.log OLDER than run.log is left over from a previous attempt: the
    # BD was rebuilt after it, so this probe is queued, not running or dead.
    $stale = (Test-Path $topLog) -and
             ((Get-Item $topLog).LastWriteTime -lt (Get-Item $runLog).LastWriteTime)

    $active = @($ooc.FullName, $implLog, $synthLog, $topLog, $runLog) |
              Where-Object { $_ -and (Test-Path $_) } |
              Sort-Object { (Get-Item $_).LastWriteTime } -Descending |
              Select-Object -First 1
    # Vivado's own header, not CreationTime: a recreated log inherits the old
    # file's timestamp and reports minutes of elapsed time that never happened.
    $start = (Get-Item $runLog).CreationTime
    $hdr = Select-String -LiteralPath $runLog -Pattern '^# Start of session at: (.+)$' |
           Select-Object -First 1
    if ($hdr) {
        $parsed = [datetime]::MinValue
        # Single-digit days pad with two spaces: "Fri Aug  7 ...".
        $stamp = ($hdr.Matches[0].Groups[1].Value.Trim() -replace '\s+', ' ')
        if ([datetime]::TryParseExact($stamp,
                'ddd MMM d HH:mm:ss yyyy', [cultureinfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::None, [ref]$parsed)) {
            $start = $parsed
        }
    }
    $st.startedAt = $start
    $st.elapsed = '{0:h\hmm\m}' -f (New-TimeSpan -Start $start -End (Get-Date))
    $idle = New-TimeSpan -Start (Get-Item $active).LastWriteTime -End (Get-Date)
    $st.idleMin = $idle.TotalMinutes
    # Not mm:ss -- a multi-hour stall would wrap to 00:00 and read as healthy.
    $st.idle = if ($idle.TotalSeconds -lt 60) { '{0:n0}s' -f $idle.TotalSeconds }
               elseif ($idle.TotalMinutes -lt 60) { '{0:n0}m' -f $idle.TotalMinutes }
               else { '{0:n0}h{1:n0}m' -f [math]::Floor($idle.TotalHours), $idle.Minutes }

    # Stage. Each marker is written once, so the newest present one wins.
    $st.stage = 'bd'
    $st.state = 'running'
    if (Select-String -LiteralPath $runLog -SimpleMatch "@@@ probe ${tag}:" -Quiet) {
        # Decide from the newest run log: synth_1/runme.log survives long after
        # synthesis ends, so its mere presence is not "currently synthesising".
        # A single-process run (build+impl in one Vivado) has no impl.log, so
        # only call it queued when no run has started either.
        if ($stale -or (-not $ooc -and -not (Test-Path $topLog))) {
            $st.stage = 'queued'; $st.state = 'pending'
        }
        elseif (-not $ooc) { $st.stage = 'ooc' }
        elseif ($ooc.Directory.Name -eq 'synth_1') { $st.stage = 'synth' }
        elseif ($ooc.Directory.Name -eq 'impl_1') { $st.stage = 'impl' }
        else { $st.stage = 'ooc'; $st.step = "per-IP: $($ooc.Directory.Name)" }
    }
    if (Test-Path $implLog) {
        # A resumed run APPENDS to the old runme.log: without this anchor the
        # stage came from the new attempt and the phase from the killed one.
        $anchor = 0
        $o = Select-String -LiteralPath $implLog -Pattern '^Command: opt_design' |
             Select-Object -Last 1
        if ($o) { $anchor = $o.LineNumber }

        $cmd = Select-String -LiteralPath $implLog -Pattern '^Command: (\w+)' |
               Where-Object { $_.LineNumber -ge $anchor } | Select-Object -Last 1
        if ($cmd) {
            $st.stage = switch ($cmd.Matches[0].Groups[1].Value) {
                'opt_design'      { 'opt' }
                'place_design'    { 'place' }
                'phys_opt_design' { 'phys_opt' }
                'route_design'    { 'route' }
                default           { 'impl' }
            }
        }
        # Phase START lines, not the "| Checksum" completions, which lag a phase.
        $ph = Select-String -LiteralPath $implLog -Pattern '^(Phase [\d.]+ [^|]+?)\s*$' |
              Where-Object { $_.LineNumber -gt $anchor } | Select-Object -Last 1
        if ($ph) { $st.step = $ph.Matches[0].Groups[1].Value }
    }
    elseif ($st.stage -eq 'synth' -and (Test-Path $synthLog)) {
        $ph = Select-String -LiteralPath $synthLog -Pattern '^(Starting .+?) :' |
              Select-Object -Last 1
        if ($ph) { $st.step = $ph.Matches[0].Groups[1].Value }
    }

    # The LAST WNS in the log, whoever printed it. Reading only the placement
    # one freezes the column for the hours that routing takes.
    if (Test-Path $implLog) {
        # Anchored so a killed run's numbers are not shown as the live ones.
        # TNS off the SAME line, else a fresh WNS pairs with a superseded TNS.
        $w = Select-String -LiteralPath $implLog -Pattern 'WNS=\s*(-?\d+\.\d+)' |
             Where-Object { $_.LineNumber -gt $anchor } | Select-Object -Last 1
        if ($w) {
            $st.wns = $w.Matches[0].Groups[1].Value
            if ($w.Line -match 'TNS=\s*(-?\d+\.\d+)') {
                $st.tns = '{0:n0}' -f [double]$Matches[1]
            }
        }
        # Hold is router-only and reads N/A for whole phases, so it takes the
        # last line that carries a number, with that line's own THS.
        $h = Select-String -LiteralPath $implLog -Pattern 'WHS=\s*(-?\d+\.\d+)' |
             Where-Object { $_.LineNumber -gt $anchor } | Select-Object -Last 1
        if ($h) {
            $st.whs = $h.Matches[0].Groups[1].Value
            if ($h.Line -match 'THS=\s*(-?\d+\.\d+)') {
                $st.ths = '{0:n0}' -f [double]$Matches[1]
            }
        }
        # v5 died at level 7; 5 and above is where Vivado says timing suffers.
        $c = Select-String -LiteralPath $implLog `
             -Pattern 'congestion is level (\d+) \((\d+x\d+)\)' |
             Where-Object { $_.LineNumber -gt $anchor } | Select-Object -Last 1
        if ($c) {
            $st.cong = 'L{0} {1}' -f $c.Matches[0].Groups[1].Value,
                                     $c.Matches[0].Groups[2].Value
        }
    }

    # Single-process runs (mesh_probe_bd.tcl with "impl") end in run.log; the
    # two-phase driver ends in impl.log. Either marker means finished.
    $doneIn = @($topLog, $runLog) | Where-Object {
        (Test-Path $_) -and
        (Select-String -LiteralPath $_ -SimpleMatch "@@@ probe $tag done" -Quiet)
    }
    # A done marker older than the newest run is stale: the probe was restarted
    # under a different strategy and is running again, not finished.
    if ($doneIn -and $implRun) {
        $newest = ($doneIn | ForEach-Object { (Get-Item $_).LastWriteTime } |
                   Measure-Object -Maximum).Maximum
        if ($implRun.LastWriteTime -gt $newest) { $doneIn = @() }
    }
    if ($doneIn) {
        $st.stage = 'done'; $st.state = 'done'; $st.step = ''
    }
    else {
        $hit = Select-String -LiteralPath $active -SimpleMatch $Fail | Select-Object -Last 1
        if ($hit) {
            # A retrying probe rewrites its log every attempt, so a FRESH error
            # is the driver working through a retry, not a probe needing help.
            $st.state = if ($st.idleMin -lt 5) { 'retry' } else { 'failed' }
            $st.err = $hit.Line.Trim()
            if ($st.err.Length -gt 110) { $st.err = $st.err.Substring(0, 110) + '...' }
        }
    }
    [pscustomobject]$st
}

function Show-Status {
    $probes = Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue |
              Sort-Object Name
    if (-not $probes) { Write-Host "no probes under $Root" -ForegroundColor DarkGray; return }

    # Log timestamps cannot tell a finished probe from a killed one -- a probe
    # the jobguard shot leaves a recent log and no process. Ask the OS.
    $cmds = @(Get-CimInstance Win32_Process -Filter "Name='vivado.exe'" |
              Select-Object -ExpandProperty CommandLine)

    $rows = foreach ($p in $probes) {
        $r = Get-ProbeStatus -Dir $p
        $r | Add-Member -NotePropertyName live `
             -NotePropertyValue @($cmds | Where-Object { $_ -like "*$($p.Name)*" }).Count
        # A queued probe has no process BY DESIGN: its BD is built and the
        # driver holds it at the phase barrier until every BD is done.
        if ($r.state -ne 'done' -and $r.live -eq 0 -and
            $r.stage -notin @('pending', 'queued')) {
            $r.state = if ($r.idleMin -ge 5) { 'dead' } else { 'retry' }
        }
        $r
    }

    # Live work first, newest first; finished and dead sink to the bottom. Name
    # order interleaves the current wave with runs from hours ago.
    $rank = @{ running = 0; retry = 1; pending = 2; done = 3; dead = 4; failed = 4 }
    $rows = $rows | Sort-Object `
        @{ Expression = { $rank[[string]$_.state] } }, `
        @{ Expression = { $_.startedAt }; Descending = $true }

    # Slots count the UNFILTERED set: the ceiling belongs to the machine.
    $allRows = @($rows)
    $rows = @($rows | Where-Object { Test-Row $_ })

    # Buffered, not printed: gathering takes seconds over multi-MB logs, and
    # clearing the screen first leaves the user staring at a blank terminal.
    $out = [System.Collections.Generic.List[object]]::new()
    $seg = { param($t, $c) @{ t = $t; c = $c } }
    $out.Add(@((& $seg ('{0,-9} {1,-10} {2,-11} {3,-7} {4,-6} {5,-8} {6,-10} {7,-7} {8,-10} {9,-10} {10}' -f `
        'probe', 'stage', 'progress', 'elapsed', 'idle', 'wns', 'tns', 'whs',
        'ths', 'congest', 'step') 'DarkGray')))
    foreach ($r in $rows) {
        $i = [array]::IndexOf($Steps, $r.stage)
        $bar = if ($i -ge 0) { ('#' * ($i + 1)).PadRight($Steps.Count, '.') } else { '.......' }
        $color = switch ($r.state) {
            'done'    { 'Green' }
            'failed'  { 'Red' }
            'dead'    { 'Red' }
            'retry'   { 'Magenta' }
            'running' { 'Yellow' }
            default   { 'DarkGray' }
        }
        if ($r.state -eq 'dead') { $r.step = "NO PROCESS - $($r.step)" }
        # Silence is the stall signal, so make a stale log impossible to miss.
        $idleColor = if ($r.state -eq 'running' -and $r.idleMin -ge 10) { 'Red' } else { $color }

        # Level 5+ is where Vivado says congestion starts costing timing.
        $cc = if ($r.cong -match '^L([5-9])') { 'Red' } else { $color }
        # The router's hold-fix phase clears mid-route negatives; only a
        # FINISHED run with negative hold is a finding.
        $holdColor = if ($r.stage -eq 'done' -and $r.whs -and
                         [double]$r.whs -lt 0) { 'Red' } else { $color }
        $out.Add(@(
            (& $seg ('{0,-9} ' -f $r.probe) $color),
            (& $seg ('{0,-10} {1,-11} {2,-7} ' -f $r.stage, $bar, $r.elapsed) $color),
            (& $seg ('{0,-6} ' -f $r.idle) $idleColor),
            (& $seg ('{0,-8} {1,-10} ' -f $r.wns, $r.tns) $color),
            (& $seg ('{0,-7} {1,-10} ' -f $r.whs, $r.ths) $holdColor),
            (& $seg ('{0,-10} ' -f $r.cong) $cc),
            (& $seg ('{0}' -f $r.step) $color)))
        if ($Errors -and $r.err) {
            $out.Add(@((& $seg ("          {0}" -f $r.err) 'DarkRed')))
        }
    }

    $vs = @(Get-Process vivado -ErrorAction SilentlyContinue)
    $gb = [math]::Round((($vs | Measure-Object WorkingSet64 -Sum).Sum) / 1GB, 1)
    $free = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB, 0)
    # Six is the hard ceiling; a 7th run starves five that are already hours in.
    # From state, not from the "NO PROCESS" step text: that prefix is stamped
    # during rendering, so under a filter the hidden rows never receive it.
    $live = @($allRows | Where-Object { $_.state -in @('running', 'retry') }).Count
    $slotc = if ($live -ge $SLOTS) { 'Red' } elseif ($live -ge $SLOTS - 1) { 'Yellow' } else { 'Green' }
    $out.Add(@((& $seg '' 'DarkGray')))
    $shown = if ($rows.Count -eq $allRows.Count) { '' }
             else { "  [{0}: {1} of {2}]" -f $script:Mode, $rows.Count, $allRows.Count }
    $out.Add(@(
        (& $seg ("slots {0}/{1}" -f $live, $SLOTS) $slotc),
        (& $seg $shown 'Cyan'),
        (& $seg ("   {0} vivado process(es), {1} GB resident, {2} GB free   {3:HH:mm:ss}" -f `
            $vs.Count, $gb, $free, (Get-Date)) 'DarkGray')))
    return $out
}

function Write-Lines($lines) {
    foreach ($ln in $lines) {
        for ($i = 0; $i -lt $ln.Count; $i++) {
            Write-Host $ln[$i].t -ForegroundColor $ln[$i].c `
                       -NoNewline:($i -lt $ln.Count - 1)
        }
    }
}

# Returns 'quit', 'redraw' (a key changed the filter) or 'tick'. Polls rather
# than blocking so a keypress lands immediately instead of after the refresh.
function Wait-Key {
    param([int]$Seconds)
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        if (-not [Console]::IsInputRedirected -and [Console]::KeyAvailable) {
            $k = [string][Console]::ReadKey($true).KeyChar
            if ($k -eq 'q') { return 'quit' }
            if ($Keys.ContainsKey($k)) { $script:Mode = $Keys[$k]; return 'redraw' }
        }
        Start-Sleep -Milliseconds 120
    }
    return 'tick'
}

if ($Watch) {
    while ($true) {
        # Gather first, THEN clear and paint, so the previous view stays up
        # while the logs are being read instead of blanking for seconds.
        $lines = Show-Status
        Clear-Host
        Write-Lines $lines
        Write-Host ("a all  r running  d running+done  h hide dead  q quit" +
                    "   [{0}]  refreshing every {1}s" -f $script:Mode, $Every) `
                   -ForegroundColor DarkGray
        if ((Wait-Key $Every) -eq 'quit') { break }
    }
}
else { Write-Lines (Show-Status) }

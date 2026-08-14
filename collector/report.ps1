# MADISON collector — Windows 베타 (IMPLEMENTATION.md §8.5) ※ 실기기 미검증
# 원칙은 report.sh와 동일: 어떤 경우에도 세션을 방해하지 않는다(항상 exit 0).
param([string]$EventName = "")

$ErrorActionPreference = "SilentlyContinue"
$MadDir = Join-Path $env:USERPROFILE ".claude\madison"
$EnvFile = Join-Path $MadDir "env"
$Spool = Join-Path $MadDir "spool.jsonl"
$ThrottleDir = Join-Path $MadDir "throttle"

function Bye { exit 0 }
if (-not $EventName) { Bye }
if (-not (Test-Path $EnvFile)) { Bye }

# env 파싱 (KEY=VALUE)
$cfg = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim('"') }
}
if (-not $cfg.MADISON_URL -or -not $cfg.MADISON_TOKEN) { Bye }
$Headers = @{ Authorization = "Bearer $($cfg.MADISON_TOKEN)"; "Content-Type" = "application/json" }
New-Item -ItemType Directory -Force -Path $ThrottleDir | Out-Null

$mutex = New-Object System.Threading.Mutex($false, "MADISON_SPOOL")

function Post-Body([string]$json, [int]$timeout) {
    try {
        Invoke-RestMethod -Uri "$($cfg.MADISON_URL)/api/events" -Method Post -Headers $Headers `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec $timeout | Out-Null
        return $true
    } catch { return $false }
}

function Flush-Spool {
    while ((Test-Path $Spool) -and (Get-Item $Spool).Length -gt 0) {
        $lines = Get-Content $Spool -TotalCount 100
        $arr = "[" + (($lines | Where-Object { $_ }) -join ",") + "]"
        if (-not (Post-Body $arr 5)) { return }
        $all = Get-Content $Spool
        if ($all.Count -le $lines.Count) { Clear-Content $Spool }
        else { $all | Select-Object -Skip $lines.Count | Set-Content $Spool }
    }
}

if ($EventName -eq "__flush") {
    if ($mutex.WaitOne(2000)) { try { Flush-Spool } finally { $mutex.ReleaseMutex() } }
    Bye
}

$stdin = [Console]::In.ReadToEnd()
try { $hook = $stdin | ConvertFrom-Json } catch { Bye }
if ($cfg.MADISON_DEBUG -eq "1") {
    New-Item -ItemType Directory -Force -Path (Join-Path $MadDir "samples") | Out-Null
    $stdin | Set-Content (Join-Path $MadDir "samples\$EventName.json")
}

$sid = if ($hook.session_id) { $hook.session_id } else { "unknown" }
$cwd = if ($hook.cwd) { $hook.cwd } else { "" }
$timeoutSec = if ($EventName -eq "session_start") { 1 } else { 2 }

# 스로틀 (세션당 60초)
if ($EventName -in @("heartbeat", "tool_start")) {
    $stamp = Join-Path $ThrottleDir $sid
    if ((Test-Path $stamp) -and ((Get-Date) - (Get-Item $stamp).LastWriteTime).TotalSeconds -lt 60) { Bye }
    New-Item -ItemType File -Force -Path $stamp | Out-Null
}
if ($EventName -eq "permission_request") { Remove-Item (Join-Path $ThrottleDir $sid) -Force }

# 프로젝트/브랜치
$project = ""; $branch = ""; $origin = ""
if ($cwd -and (Test-Path $cwd)) {
    $top = git -C $cwd rev-parse --show-toplevel 2>$null
    if ($top) {
        $project = Split-Path $top -Leaf
        $branch = git -C $cwd branch --show-current 2>$null
        $origin = git -C $cwd remote get-url origin 2>$null
    } else { $project = Split-Path $cwd -Leaf }
}

$detail = @{}
switch ($EventName) {
    "session_start" { $detail = @{ source = "$($hook.source)" } }
    "prompt" { $p = "$($hook.prompt)"; $detail = @{ prompt = $p.Substring(0, [Math]::Min(200, $p.Length)) } }
    "tool_start" { $detail = @{ tool = "$($hook.tool_name)" } }
    "permission_request" { $m = "$($hook.message)"; $detail = @{ message = $m.Substring(0, [Math]::Min(200, $m.Length)) } }
    "idle" { $m = "$($hook.message)"; $detail = @{ message = $m.Substring(0, [Math]::Min(200, $m.Length)) } }
    "turn_done" { $s = "$($hook.last_assistant_message)"; $detail = @{ summary = $s.Substring(0, [Math]::Min(200, $s.Length)) } }
    "session_end" { $detail = @{ reason = "$($hook.reason)" } }
}

$ev = @{
    agent = "claude-code"; session_id = $sid; event = $EventName
    ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    event_id = [guid]::NewGuid().ToString()
    project = $project; branch = "$branch"; detail = $detail
} | ConvertTo-Json -Compress -Depth 4

# 전송 (순서 보존: 스풀이 있으면 뒤에 붙여 함께 플러시)
if ((Test-Path $Spool) -and (Get-Item $Spool).Length -gt 0) {
    if ($mutex.WaitOne(1000)) {
        try { Add-Content $Spool $ev; Flush-Spool } finally { $mutex.ReleaseMutex() }
    } else { Add-Content $Spool $ev }
} elseif (-not (Post-Body $ev $timeoutSec)) {
    if ($mutex.WaitOne(1000)) { try { Add-Content $Spool $ev } finally { $mutex.ReleaseMutex() } }
    else { Add-Content $Spool $ev }
}

# SessionStart: 핸드오프 주입 (source startup|resume, 리포 매칭)
if ($EventName -eq "session_start" -and "$($hook.source)" -in @("startup", "resume")) {
    try {
        $q = "mine=1&repo=$([uri]::EscapeDataString($project))&origin=$([uri]::EscapeDataString("$origin"))"
        $hs = Invoke-RestMethod -Uri "$($cfg.MADISON_URL)/api/handoffs?$q" -Headers $Headers -TimeoutSec 1
        foreach ($h in $hs) {
            Write-Output "[MADISON] $($h.from_name)발 핸드오프 $($h.hf) 도착: $($h.summary). 브랜치 $($h.branch)를 체크아웃하고 $($h.doc_path)를 읽은 뒤, 이어서 진행할지 사용자에게 확인하세요."
            Invoke-RestMethod -Uri "$($cfg.MADISON_URL)/api/handoffs/$($h.id)" -Method Patch -Headers $Headers `
                -Body '{"status":"delivered"}' -TimeoutSec 1 | Out-Null
        }
    } catch {}
}

Bye

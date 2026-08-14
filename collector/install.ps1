# MADISON collector 설치 — Windows 베타 (IMPLEMENTATION.md §8.5) ※ 실기기 미검증
# 사용: powershell에서
#   irm https://madison-api.example.com/install.ps1 -OutFile install.ps1
#   .\install.ps1 -Name office-pc -Secret <등록암호>
# WSL에서 Claude Code를 쓰는 경우 이 스크립트 대신 리눅스용 install.sh를 사용(정식 경로).
param(
    [string]$Name = "",
    [string]$Secret = $env:MADISON_ENROLL_SECRET,
    [string]$Hub = "https://madison-api.example.com",
    [switch]$NoCodex
)
$ErrorActionPreference = "Stop"
$MadDir = Join-Path $env:USERPROFILE ".claude\madison"
$EnvFile = Join-Path $MadDir "env"
$Settings = Join-Path $env:USERPROFILE ".claude\settings.json"

# 재설치/갱신: -Hub 미지정이면 이미 등록된 env의 MADISON_URL을 기본값으로 쓴다
if ($Hub -eq "https://madison-api.example.com" -and (Test-Path $EnvFile)) {
    $savedUrl = Get-Content $EnvFile | Where-Object { $_ -match '^MADISON_URL=(.+)$' } |
        ForEach-Object { $Matches[1].Trim('"') } | Select-Object -First 1
    if ($savedUrl) { $Hub = $savedUrl }
}

New-Item -ItemType Directory -Force -Path $MadDir, (Join-Path $MadDir "throttle") | Out-Null

# 1) 파일 배치
Invoke-WebRequest -Uri "$Hub/collector/report.ps1" -OutFile (Join-Path $MadDir "report.ps1") -TimeoutSec 15
Write-Host "[MADISON] report.ps1 배치 완료"

# 2) 등록 (env에 토큰 있으면 생략 — 멱등)
$enrolled = (Test-Path $EnvFile) -and ((Get-Content $EnvFile -Raw) -match "MADISON_TOKEN=.+")
if (-not $enrolled) {
    if (-not $Name -or -not $Secret) { throw "-Name 과 -Secret 이 필요합니다" }
    $resp = Invoke-RestMethod -Uri "$Hub/api/enroll" -Method Post -ContentType "application/json" `
        -Body (@{ name = $Name; secret = $Secret } | ConvertTo-Json) -TimeoutSec 15
    @(
        "MADISON_URL=$Hub"
        "MADISON_TOKEN=$($resp.token)"
        "MADISON_DEVICE=$Name"
        "MADISON_DEBUG=0"
    ) | Set-Content $EnvFile
    Write-Host "[MADISON] 기기 '$Name' 등록 완료"
} else {
    Write-Host "[MADISON] 이미 등록된 기기 — enroll 생략"
}

# 3) 전역 훅 병합 (Windows는 절대경로 명령 — 기기별로 달라도 무방)
$reportPath = Join-Path $MadDir "report.ps1"
$cmdFor = { param($ev) "powershell -NoProfile -ExecutionPolicy Bypass -File `"$reportPath`" $ev" }
$events = @{
    SessionStart = "session_start"; UserPromptSubmit = "prompt"; PreToolUse = "tool_start"
    PostToolUse = "heartbeat"; PermissionRequest = "permission_request"
    Stop = "turn_done"; SessionEnd = "session_end"
}
$settings = if (Test-Path $Settings) { Get-Content $Settings -Raw | ConvertFrom-Json -AsHashtable } else { @{} }
if (-not $settings.hooks) { $settings.hooks = @{} }
$changed = $false
foreach ($hookName in $events.Keys) {
    $cmd = & $cmdFor $events[$hookName]
    $entry = @{ hooks = @(@{ type = "command"; command = $cmd }) }
    if (-not $settings.hooks[$hookName]) { $settings.hooks[$hookName] = @() }
    $present = $settings.hooks[$hookName] | Where-Object { $_.hooks.command -contains $cmd }
    if (-not $present) { $settings.hooks[$hookName] += $entry; $changed = $true }
}
# Notification은 matcher 필수 (idle_prompt만 — 오탐 방지)
$idleCmd = & $cmdFor "idle"
if (-not ($settings.hooks.Notification | Where-Object { $_.hooks.command -contains $idleCmd })) {
    if (-not $settings.hooks.Notification) { $settings.hooks.Notification = @() }
    $settings.hooks.Notification += @{ matcher = "idle_prompt"; hooks = @(@{ type = "command"; command = $idleCmd }) }
    $changed = $true
}
if ($changed) {
    if (Test-Path $Settings) { Copy-Item $Settings "$Settings.bak-madison-$(Get-Date -Format yyyyMMddHHmmss)" }
    $settings | ConvertTo-Json -Depth 10 | Set-Content $Settings
    Write-Host "[MADISON] 전역 훅 설치됨 (백업 생성)"
} else { Write-Host "[MADISON] 전역 훅 이미 최신" }

# 4) Codex lifecycle hooks — 기존 hooks.json과 병합
if (-not $NoCodex) {
    $codexDir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
    $codexHooksPath = Join-Path $codexDir "hooks.json"
    New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
    $codexDoc = if (Test-Path $codexHooksPath) { Get-Content $codexHooksPath -Raw | ConvertFrom-Json -AsHashtable } else { @{} }
    if (-not $codexDoc.hooks) { $codexDoc.hooks = @{} }
    $codexChanged = $false
    foreach ($hookName in $events.Keys) {
        $eventName = $events[$hookName]
        $cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$reportPath`" $eventName codex-cli"
        $hookTimeout = if ($hookName -in @("SessionStart", "SessionEnd")) { 3 } else { 5 }
        # async 필드는 Codex가 아직 미지원("skipping async hook" 경고와 함께 훅 자체가 스킵됨) — 동기 + 짧은 timeout만 쓴다.
        $handler = @{ type = "command"; command = $cmd; timeout = $hookTimeout }
        $entry = @{ hooks = @($handler) }
        if (-not $codexDoc.hooks[$hookName]) { $codexDoc.hooks[$hookName] = @() }
        $found = $null
        foreach ($group in @($codexDoc.hooks[$hookName])) {
            foreach ($h in @($group.hooks)) {
                if ("$($h.command)" -eq $cmd) { $found = $h; break }
            }
            if ($found) { break }
        }
        if (-not $found) { $codexDoc.hooks[$hookName] += $entry; $codexChanged = $true }
        elseif ("$($found.type)" -ne "command" -or $found.timeout -ne $hookTimeout -or [bool]$found.async) {
            # 같은 command가 이미 있으면 내용만 제자리 갱신 (구버전이 남긴 async 제거 포함) — install.sh와 동일
            $found.type = "command"; $found.timeout = $hookTimeout
            if ($found.Contains("async")) { $found.Remove("async") }
            $codexChanged = $true
        }
    }
    # Codex에는 Claude의 Notification/idle_prompt 훅이 없으므로 MADISON은 추가하지 않는다.
    if ($codexChanged) {
        if (Test-Path $codexHooksPath) { Copy-Item $codexHooksPath "$codexHooksPath.bak-madison-$(Get-Date -Format yyyyMMddHHmmss)" }
        $codexDoc | ConvertTo-Json -Depth 12 | Set-Content $codexHooksPath
        Write-Host "[MADISON] Codex lifecycle hooks 설치됨 — Codex에서 /hooks 신뢰 필요"
    } else { Write-Host "[MADISON] Codex lifecycle hooks 이미 최신" }
}

# 5) 스풀 플러셔 — Task Scheduler 5분 주기 (이름 있는 태스크: MADISON\madison-flush)
schtasks /Create /F /SC MINUTE /MO 5 /TN "MADISON\madison-flush" `
    /TR "powershell -NoProfile -ExecutionPolicy Bypass -File `"$reportPath`" __flush" | Out-Null
Write-Host "[MADISON] 스풀 플러셔 등록 (Task Scheduler, 5분 주기)"

Write-Host "[MADISON] 설치 완료 — 열려 있는 Claude Code/Codex 세션은 재시작해야 훅이 적용됩니다"
if (-not $NoCodex) { Write-Host "[MADISON] Codex에서 /hooks를 열어 MADISON 훅을 검토·신뢰하세요" }
Write-Host "[MADISON] 대시보드: https://madison.example.com"

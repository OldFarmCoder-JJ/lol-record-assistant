$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskProfile = Join-Path $env:TEMP ('Seraphine-source-smoke-' + (Get-Date -Format 'yyyyMMddHHmmss'))
$taskData = Join-Path $taskProfile 'Seraphine'
New-Item -ItemType Directory -Path $taskData -Force | Out-Null
$taskOriginalData = $env:APPDATA
$taskExisting = Join-Path $taskOriginalData 'Seraphine/config.json'
$taskSettings = if (Test-Path -LiteralPath $taskExisting) {
    Get-Content -LiteralPath $taskExisting -Raw | ConvertFrom-Json
} else { [pscustomobject]@{} }
if (-not $taskSettings.General) { $taskSettings | Add-Member -NotePropertyName General -NotePropertyValue ([pscustomobject]@{}) -Force }
foreach ($taskSetting in @('EnableCheckUpdate', 'EnableCloseToTray')) {
    $taskSettings.General | Add-Member -NotePropertyName $taskSetting -NotePropertyValue $false -Force
}
$taskSettings.General | Add-Member -NotePropertyName EnableSilent -NotePropertyValue $true -Force
if ($taskSettings.Functions) {
    foreach ($taskSetting in $taskSettings.Functions.PSObject.Properties) {
        if ($taskSetting.Name -match '^EnableAuto|^AutoAccept') { $taskSetting.Value = $false }
    }
}
$taskSettings | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath (Join-Path $taskData 'config.json') -Encoding utf8
$taskProc = $null
try {
    $env:APPDATA = $taskProfile
    $taskMain = Join-Path $taskRoot 'Seraphine/main.py'
    $taskProc = Start-Process -FilePath (Join-Path $taskRoot 'packaging-env/Scripts/pythonw.exe') -ArgumentList ('"' + $taskMain + '"') -WorkingDirectory (Join-Path $taskRoot 'Seraphine') -PassThru -WindowStyle Hidden
    $taskReady = $false
    for ($taskAttempt = 0; $taskAttempt -lt 12; $taskAttempt++) {
        Start-Sleep -Seconds 2
        $taskProc.Refresh()
        if ($taskProc.HasExited) { throw 'Source GUI exited unexpectedly' }
        $taskLogPath = Join-Path $taskData 'log'
        if (Test-Path -LiteralPath $taskLogPath) {
            $taskLogText = (Get-ChildItem -LiteralPath $taskLogPath -Filter '*.log' | ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw }) -join "`n"
            if ($taskLogText -match 'Traceback|Abnormal exit') { throw 'Source GUI startup error' }
            if ($taskLogText -match 'Seraphine initialized') { $taskReady = $true; break }
        }
    }
    if (-not $taskReady) { throw 'Source initialization marker missing' }
    Write-Output 'PASS: full source GUI initialized, alive, no traceback; no packaging.'
} finally {
    if ($taskProc -and -not $taskProc.HasExited) { Stop-Process -Id $taskProc.Id }
    $env:APPDATA = $taskOriginalData
}

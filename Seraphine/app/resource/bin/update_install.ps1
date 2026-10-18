param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][string]$StageRoot,
    [Parameter(Mandatory=$true)][int]$AppPid,
    [switch]$NoRestart
)
$ErrorActionPreference = 'Stop'
$InstallRoot = (Resolve-Path -LiteralPath $InstallRoot).Path
$StageRoot = (Resolve-Path -LiteralPath $StageRoot).Path
function Get-Digest([string]$File) {
    $taskStream = [IO.File]::OpenRead($File)
    $taskHasher = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($taskHasher.ComputeHash($taskStream)).Replace('-', '').ToLowerInvariant() }
    finally { $taskStream.Dispose(); $taskHasher.Dispose() }
}
function Resolve-Child([string]$Root, [string]$Relative) {
    if ([IO.Path]::IsPathRooted($Relative) -or $Relative.Contains(':') -or (($Relative -split '[\\/]') -contains '..')) {
        throw 'Invalid release path'
    }
    $taskPrefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $taskTarget = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
    if (-not $taskTarget.StartsWith($taskPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Path escapes root' }
    $taskCursor = $taskTarget
    while ($taskCursor.Length -gt $Root.Length) {
        if (Test-Path -LiteralPath $taskCursor) {
            if ((Get-Item -LiteralPath $taskCursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked release path' }
        }
        $taskCursor = Split-Path -Parent $taskCursor
    }
    return $taskTarget
}
$taskBackup = Resolve-Child (Split-Path -Parent $StageRoot) 'backup'
$taskWritten = [Collections.Generic.List[object]]::new()
try {
    if (Get-Process -Id $AppPid -ErrorAction SilentlyContinue) {
        Wait-Process -Id $AppPid -Timeout 60 -ErrorAction Stop
    }
    $taskManifest = Get-Content -LiteralPath (Join-Path $StageRoot 'release-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskFiles = @(Get-Content -LiteralPath (Join-Path $StageRoot 'filelist.txt') -Encoding UTF8)
    $taskExpected = @($taskManifest.files.PSObject.Properties.Name) + 'release-manifest.json'
    if (@(Compare-Object ($taskFiles | Sort-Object) ($taskExpected | Sort-Object)).Count -ne 0) { throw 'Manifest mismatch' }
    foreach ($taskName in $taskFiles) {
        $taskSource = Resolve-Child $StageRoot $taskName
        $null = Resolve-Child $InstallRoot $taskName
        if (-not (Test-Path -LiteralPath $taskSource -PathType Leaf)) { throw 'Missing release file' }
        if ($taskName -ne 'release-manifest.json') {
            $taskHash = $taskManifest.files.PSObject.Properties[$taskName].Value
            if ((Get-Digest $taskSource) -ne $taskHash) { throw 'Release hash mismatch' }
        }
    }
    foreach ($taskName in $taskFiles) {
        $taskSource = Resolve-Child $StageRoot $taskName
        $taskTarget = Resolve-Child $InstallRoot $taskName
        $taskSaved = Resolve-Child $taskBackup $taskName
        $taskExisted = Test-Path -LiteralPath $taskTarget -PathType Leaf
        if ($taskExisted) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $taskSaved) -Force | Out-Null
            Copy-Item -LiteralPath $taskTarget -Destination $taskSaved -Force
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $taskTarget) -Force | Out-Null
        $taskWritten.Add([pscustomobject]@{Target=$taskTarget; Saved=$taskSaved; Existed=$taskExisted})
        Copy-Item -LiteralPath $taskSource -Destination $taskTarget -Force
    }
    Write-Output 'UPDATE_OK'
} catch {
    $taskFailure = $_
    for ($taskIndex = $taskWritten.Count - 1; $taskIndex -ge 0; $taskIndex--) {
        $taskEntry = $taskWritten[$taskIndex]
        try {
            if ($taskEntry.Existed) {
                Copy-Item -LiteralPath $taskEntry.Saved -Destination $taskEntry.Target -Force
            } elseif (Test-Path -LiteralPath $taskEntry.Target -PathType Leaf) {
                Remove-Item -LiteralPath $taskEntry.Target -Force
            }
        } catch { Write-Output ('ROLLBACK_ERROR: ' + $_.Exception.Message) }
    }
    Write-Output ('UPDATE_FAILED: ' + $taskFailure.Exception.Message)
    if (-not $NoRestart) {
        Start-Process -FilePath (Join-Path $InstallRoot 'Seraphine.exe') -WorkingDirectory $InstallRoot -WindowStyle Hidden
    }
    exit 1
}
if (-not $NoRestart) {
    Start-Process -FilePath (Join-Path $InstallRoot 'Seraphine.exe') -WorkingDirectory $InstallRoot -WindowStyle Hidden
}

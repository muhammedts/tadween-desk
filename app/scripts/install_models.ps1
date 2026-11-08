<#
.SYNOPSIS
  Install the speech models Tadween Desk needs, verified against the pinned manifest.

.DESCRIPTION
  Reads app\models\manifest.json (every file's source URL, pinned revision, byte size and
  SHA-256) and app\models\acquisition.json (the llama.cpp release zip the runtime files come
  from). Installs, into app\models:
    - Qwen3-ASR 0.6B Q8_0 and its audio projector   (role stt, ~1.02 GB)
    - llama.cpp b11960 llama-server for Windows x64  (role stt-runtime, from a ~24 MB zip)
    - Silero VAD                                      (role vad, ~0.6 MB)

  Files that are already present with the right size and SHA-256 are skipped. Missing files
  are taken from -From (a folder you filled by hand) when a matching copy is there, and are
  otherwise downloaded after showing the size and destination and asking for confirmation.
  Every downloaded or copied file is checked before it is put in place; a file that does not
  match is deleted and reported. Nothing outside app\models is changed. When Windows has no
  Microsoft C++ runtime, its DLLs are copied next to llama-server from the app's own PySide6
  package. After setup the app runs offline.

.PARAMETER From
  Folder holding files you downloaded yourself: either the files directly, the same layout as
  app\models (stt\..., runtime\..., vad\...), or the llama.cpp release zip.

.PARAMETER DryRun
  Only check what is installed and show what would be copied, recovered or downloaded.
  Nothing is written, moved or deleted.

.PARAMETER Yes
  Download without asking.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File app\scripts\install_models.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File app\scripts\install_models.ps1 -From D:\Downloads\tadween-models
#>
[CmdletBinding()]
param(
    [string]$From = '',
    [switch]$DryRun,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with its progress bar
$app = Split-Path -Parent $PSScriptRoot
$modelsDir = Join-Path $app 'models'
$manifest = Get-Content -LiteralPath (Join-Path $modelsDir 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$acquisition = Get-Content -LiteralPath (Join-Path $modelsDir 'acquisition.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$downloadDir = Join-Path $modelsDir '.downloads'
$msvcRuntime = @('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')

# Decimal units, as download sites show them (1 GB = 1,000,000,000 bytes).
function Format-Size([long]$Bytes) {
    $c = [Globalization.CultureInfo]::InvariantCulture
    if ($Bytes -ge 1e9) { return [string]::Format($c, '{0:N2} GB', $Bytes / 1e9) }
    if ($Bytes -ge 1e6) { return [string]::Format($c, '{0:N1} MB', $Bytes / 1e6) }
    return [string]::Format($c, '{0:N0} KB', [math]::Ceiling($Bytes / 1e3))
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# $true when $Path holds exactly the expected bytes (size first: hashing 1 GB takes seconds).
function Test-Expected([string]$Path, [long]$Bytes, [string]$Sha256) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ((Get-Item -LiteralPath $Path).Length -ne $Bytes) { return $false }
    return (Get-Sha256 $Path) -eq $Sha256
}

# Copy (or move) $Source to $Destination through a temporary name, so a half-written file is
# never left under the final name.
function Install-File([string]$Source, [string]$Destination, [switch]$Move) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    $part = "$Destination.part"
    if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part -Force }
    if ($Move) { Move-Item -LiteralPath $Source -Destination $part } else { Copy-Item -LiteralPath $Source -Destination $part }
    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
    Move-Item -LiteralPath $part -Destination $Destination
}

function Invoke-Download([string]$Url, [string]$OutFile) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutFile) | Out-Null
    $curl = Join-Path $env:WINDIR 'System32\curl.exe'
    if (Test-Path -LiteralPath $curl) {
        # -C - resumes a partial file left by an interrupted run.
        & $curl --fail --location --retry 3 --retry-delay 2 --continue-at - --output $OutFile $Url
        if ($LASTEXITCODE -ne 0) { throw "download failed (curl exit $LASTEXITCODE): $Url" }
    } else {
        # No curl.exe (older Windows): no resume, and .NET may need TLS 1.2 switched on.
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    }
}

# --- What should be installed ------------------------------------------------------------
$roles = @($acquisition.default_roles)
$entries = @($manifest.models | Where-Object { $roles -contains $_.role })
$items = @()
foreach ($entry in $entries) {
    $folder = Join-Path $modelsDir ($entry.install_subdir -replace '/', '\')
    $archive = @($acquisition.archives | Where-Object { $_.entry -eq $entry.id }) | Select-Object -First 1
    foreach ($file in $entry.files) {
        $items += [pscustomobject]@{
            Entry = $entry.id; Role = $entry.role; Name = $file.basename; Url = $file.source_url
            Bytes = [long]$file.bytes; Sha256 = $file.sha256; SubDir = $entry.install_subdir
            Destination = Join-Path $folder $file.basename; Archive = $archive; State = 'missing'
        }
    }
}

Write-Host "Tadween Desk speech models -> $modelsDir"
Write-Host "Checking installed files (size and SHA-256)..."
foreach ($item in $items) {
    # A run interrupted between removing an old file and renaming its verified replacement
    # leaves "<name>.part"; finish that rename instead of fetching the file again.
    $part = "$($item.Destination).part"
    if (-not (Test-Path -LiteralPath $item.Destination) -and (Test-Expected $part $item.Bytes $item.Sha256)) {
        if ($DryRun) {
            Write-Host ("  recover   {0} (verified {0}.part left by an interrupted run)" -f $item.Name)
            $item.State = 'recover'
            continue
        }
        Move-Item -LiteralPath $part -Destination $item.Destination
    }
    if (Test-Path -LiteralPath $item.Destination) {
        if (Test-Expected $item.Destination $item.Bytes $item.Sha256) { $item.State = 'ok' } else { $item.State = 'mismatch' }
    }
}
$needed = @($items | Where-Object { $_.State -ne 'ok' -and $_.State -ne 'recover' })
foreach ($item in ($items | Where-Object { $_.State -eq 'ok' })) { Write-Host ("  ok        {0}" -f $item.Name) }
foreach ($item in ($items | Where-Object { $_.State -eq 'mismatch' })) {
    Write-Host ("  replace   {0} (present, but its size or SHA-256 does not match the manifest)" -f $item.Name)
}

# --- Copies the user placed by hand ------------------------------------------------------
if ($From -and $needed.Count -gt 0) {
    if (-not (Test-Path -LiteralPath $From -PathType Container)) { throw "-From folder not found: $From" }
    $fromRoot = (Resolve-Path -LiteralPath $From).Path
    foreach ($item in $needed) {
        $candidates = @(
            (Join-Path $fromRoot $item.Name),
            (Join-Path (Join-Path $fromRoot ($item.SubDir -replace '/', '\')) $item.Name),
            (Join-Path (Join-Path (Join-Path $fromRoot 'models') ($item.SubDir -replace '/', '\')) $item.Name)
        )
        foreach ($candidate in $candidates) {
            if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
            if (-not (Test-Expected $candidate $item.Bytes $item.Sha256)) {
                Write-Host ("  skip      {0}: found in -From, but its size or SHA-256 does not match the manifest" -f $candidate)
                continue
            }
            if ($DryRun) { Write-Host ("  copy      {0} from {1}" -f $item.Name, $candidate) }
            else {
                Install-File $candidate $item.Destination
                Write-Host ("  copied    {0} from {1}" -f $item.Name, $candidate)
            }
            $item.State = 'copied'
            break
        }
    }
    # The runtime release zip, if the user downloaded it.
    foreach ($archive in @($acquisition.archives)) {
        $zip = Join-Path $fromRoot $archive.basename
        if ((Test-Path -LiteralPath $zip) -and -not (Test-Expected $zip ([long]$archive.bytes) $archive.sha256)) {
            Write-Host ("  skip      {0}: its size or SHA-256 does not match the pinned release" -f $zip)
        }
        $wanted = @($needed | Where-Object { $_.State -ne 'copied' -and $_.Archive -and $_.Archive.basename -eq $archive.basename })
        if ($wanted.Count -gt 0 -and (Test-Expected $zip ([long]$archive.bytes) $archive.sha256)) {
            foreach ($item in $wanted) { $item.State = 'from-zip:' + $zip }
            Write-Host ("  extract   {0} file(s) from {1}" -f $wanted.Count, $zip)
        }
    }
    $needed = @($needed | Where-Object { $_.State -ne 'copied' })
}

# --- Downloads ---------------------------------------------------------------------------
$toDownload = @($needed | Where-Object { -not ($_.State -like 'from-zip:*') })
$plan = @()
foreach ($archive in @($acquisition.archives)) {
    $fromArchive = @($toDownload | Where-Object { $_.Archive -and $_.Archive.basename -eq $archive.basename })
    if ($fromArchive.Count -gt 0) {
        $plan += [pscustomobject]@{ Name = $archive.basename; Url = $archive.url; Bytes = [long]$archive.bytes
            Sha256 = $archive.sha256; Into = (Split-Path -Parent $fromArchive[0].Destination); Items = $fromArchive; Zip = $true }
    }
}
foreach ($item in ($toDownload | Where-Object { -not $_.Archive })) {
    $plan += [pscustomobject]@{ Name = $item.Name; Url = $item.Url; Bytes = $item.Bytes; Sha256 = $item.Sha256
        Into = (Split-Path -Parent $item.Destination); Items = @($item); Zip = $false }
}

if ($plan.Count -gt 0) {
    $total = ($plan | Measure-Object -Property Bytes -Sum).Sum
    Write-Host ''
    Write-Host 'To download (pinned revisions; each file is checked against its SHA-256):'
    foreach ($p in $plan) {
        Write-Host ("  {0,-34} {1,10}  -> {2}" -f $p.Name, (Format-Size $p.Bytes), $p.Into)
        Write-Host ("      {0}" -f $p.Url)
    }
    Write-Host ("  Total download: {0}" -f (Format-Size $total))
    $drive = [IO.Path]::GetPathRoot($modelsDir)
    $free = (New-Object IO.DriveInfo $drive).AvailableFreeSpace
    Write-Host ("  Free space on {0}: {1}" -f $drive, (Format-Size $free))
    # Downloads are moved into place on the same drive; a zip also needs room for its files.
    $extracted = ($plan | Where-Object { $_.Zip } | ForEach-Object { $_.Items } | Measure-Object -Property Bytes -Sum).Sum
    $required = $total + [long]$extracted + 100MB
    if ($free -lt $required) {
        $message = "not enough free disk space on $drive (about $(Format-Size $required) needed)"
        if ($DryRun) { Write-Warning $message } else { throw $message }
    }
    if ($DryRun) {
        Write-Host ''
        Write-Host 'Dry run: nothing was downloaded.'
    } else {
        if (-not $Yes) {
            $answer = Read-Host ("Download {0} now? [y/N]" -f (Format-Size $total))
            if ($answer -notmatch '^(y|yes)$') { Write-Host 'Nothing downloaded.'; exit 2 }
        }
        foreach ($p in $plan) {
            $tmp = Join-Path $downloadDir $p.Name
            if (Test-Expected $tmp $p.Bytes $p.Sha256) {
                Write-Host ("Reusing {0}, already downloaded by an earlier run" -f $p.Name)
            } else {
                Write-Host ("Downloading {0} ({1})..." -f $p.Name, (Format-Size $p.Bytes))
                Invoke-Download $p.Url $tmp
            }
            if (-not (Test-Expected $tmp $p.Bytes $p.Sha256)) {
                Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
                throw "$($p.Name): the download does not match the expected size and SHA-256; it was deleted"
            }
            if ($p.Zip) { foreach ($item in $p.Items) { $item.State = 'from-zip:' + $tmp } }
            else {
                Install-File $tmp $p.Items[0].Destination -Move
                Write-Host ("  installed {0}" -f $p.Items[0].Name)
            }
        }
    }
}

# --- Files taken out of a release zip ----------------------------------------------------
$zipItems = @($items | Where-Object { $_.State -like 'from-zip:*' })
if ($zipItems.Count -gt 0 -and -not $DryRun) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    foreach ($group in ($zipItems | Group-Object { $_.State })) {
        $zipPath = $group.Name.Substring('from-zip:'.Length)
        $zip = [IO.Compression.ZipFile]::OpenRead($zipPath)
        try {
            foreach ($item in $group.Group) {
                # The release keeps its files at the top level; take the shallowest match.
                $entry = @($zip.Entries | Where-Object { $_.Name -eq $item.Name } |
                    Sort-Object { ($_.FullName -split '/').Count }) | Select-Object -First 1
                if (-not $entry) { throw "$($item.Name) is not in $zipPath" }
                $tmp = Join-Path $downloadDir ("extract-" + $item.Name)
                New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $tmp, $true)
                if (-not (Test-Expected $tmp $item.Bytes $item.Sha256)) {
                    Remove-Item -LiteralPath $tmp -Force
                    throw "$($item.Name) in the zip does not match the manifest"
                }
                Install-File $tmp $item.Destination -Move
                Write-Host ("  installed {0}" -f $item.Name)
            }
        } finally { $zip.Dispose() }
        if ([IO.Path]::GetFullPath((Split-Path -Parent $zipPath)) -eq [IO.Path]::GetFullPath($downloadDir)) {
            Remove-Item -LiteralPath $zipPath -Force
        }
    }
}
if (-not $DryRun -and (Test-Path -LiteralPath $downloadDir) -and -not (Get-ChildItem -LiteralPath $downloadDir -Force)) {
    Remove-Item -LiteralPath $downloadDir -Force
}

# --- Microsoft C++ runtime for llama-server ----------------------------------------------
$runtime = @($manifest.models | Where-Object { $_.role -eq 'stt-runtime' })[0]
$runtimeDir = Join-Path $modelsDir ($runtime.install_subdir -replace '/', '\')
$system32 = Join-Path $env:WINDIR 'System32'
$missingSystem = @($msvcRuntime | Where-Object { -not (Test-Path -LiteralPath (Join-Path $system32 $_)) })
$runtimeReady = $true
if ($missingSystem.Count -gt 0) {
    $local = @($msvcRuntime | Where-Object { Test-Path -LiteralPath (Join-Path $runtimeDir $_) })
    $pyside = Join-Path $app '.venv\Lib\site-packages\PySide6'
    $copies = @($msvcRuntime | ForEach-Object { Join-Path $pyside $_ })
    $canCopy = @($copies | Where-Object { -not (Test-Path -LiteralPath $_) }).Count -eq 0
    if ($local.Count -eq $msvcRuntime.Count) {
        Write-Host '  ok        Microsoft C++ runtime DLLs next to llama-server'
    } elseif ($canCopy -and $DryRun) {
        Write-Host '  copy      the Microsoft C++ runtime DLLs from the PySide6 package next to llama-server'
    } elseif ($canCopy) {
        New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
        foreach ($dll in $copies) { Copy-Item -LiteralPath $dll -Destination $runtimeDir -Force }
        Write-Host '  copied    the Microsoft C++ runtime DLLs from the PySide6 package next to llama-server'
    } else {
        $runtimeReady = $false
        Write-Warning ('Windows has no Microsoft Visual C++ runtime ({0}), so llama-server cannot start. Run app\scripts\bootstrap.ps1 first and run this again, or install the "Microsoft Visual C++ Redistributable for Visual Studio 2015-2022 (x64)".' -f ($missingSystem -join ', '))
    }
}

# --- Final check -------------------------------------------------------------------------
if ($DryRun) { exit 0 }
$bad = @($items | Where-Object { -not (Test-Expected $_.Destination $_.Bytes $_.Sha256) })
Write-Host ''
if ($bad.Count -gt 0) {
    foreach ($item in $bad) { Write-Host ("  NOT READY {0} -> {1}" -f $item.Name, $item.Destination) }
    Write-Host 'Some files are missing or do not match the manifest. Run this again, or place them by hand (see README).'
    exit 1
}
if (-not $runtimeReady) {
    Write-Host 'The model files are verified, but the Microsoft C++ runtime is missing (see the warning above).'
    exit 1
}
Write-Host ("All {0} files verified (size and SHA-256). Tadween Desk can now transcribe offline." -f $items.Count)
exit 0

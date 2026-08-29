param(
    [int]$ChunkSize = 25,
    [int]$PauseSeconds = 60,
    [int]$StartOffset = 0,
    [string]$ExcludeEntityId = "cafe_dn_062",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
if ($ChunkSize -lt 1) {
    throw "ChunkSize must be positive."
}
if ($PauseSeconds -lt 0) {
    throw "PauseSeconds cannot be negative."
}
if ($StartOffset -lt 0) {
    throw "StartOffset cannot be negative."
}

$ManifestPath = "config/google-maps-batch-manifest.json"
$Manifest = Get-Content -Raw -Encoding utf8 $ManifestPath | ConvertFrom-Json
$RegistryPath = Join-Path "config" $Manifest.registry_file
$Registry = Get-Content -Raw -Encoding utf8 $RegistryPath | ConvertFrom-Json
$MenuTypes = @("cafe", "restaurant", "nightlife")
$Targets = @(
    $Registry.mappings | Where-Object {
        $_.entity_type -in $MenuTypes -and
        ([string]::IsNullOrWhiteSpace($ExcludeEntityId) -or $_.entity_id -ne $ExcludeEntityId)
    }
)

Write-Host "Menu-source discovery targets: $($Targets.Count)"
Write-Host "Place discovery starts at offset: $StartOffset"
$FailedPlaceChunks = @()
for ($Offset = $StartOffset; $Offset -lt $Targets.Count; $Offset += $ChunkSize) {
    Write-Host "Running place discovery chunk offset=$Offset size=$ChunkSize"
    $Arguments = @(
        "-m", "nextrip_pipeline.cli", "batch-google-maps",
        "--manifest", $ManifestPath,
        "--mode", "place",
        "--entity-type", "cafe",
        "--entity-type", "restaurant",
        "--entity-type", "nightlife",
        "--max-requests", $ChunkSize,
        "--offset", $Offset
    )
    if (-not [string]::IsNullOrWhiteSpace($ExcludeEntityId)) {
        $Arguments += @("--exclude-entity-id", $ExcludeEntityId)
    }
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        $FailedPlaceChunks += $Offset
        Write-Warning "Place discovery chunk failed at offset $Offset. See batch summary."
    }
    if ($Offset + $ChunkSize -lt $Targets.Count -and $PauseSeconds -gt 0) {
        Start-Sleep -Seconds $PauseSeconds
    }
}

$SourceDirectory = "data/current/google_maps_menu_sources"
$DiscoveredIds = @{}
if (Test-Path $SourceDirectory) {
    Get-ChildItem $SourceDirectory -Filter *.json | ForEach-Object {
        $Entry = Get-Content -Raw -Encoding utf8 $_.FullName | ConvertFrom-Json
        $DiscoveredIds[$Entry.place_id] = $true
    }
}
$MenuTargets = @($Targets | Where-Object { $DiscoveredIds.ContainsKey($_.entity_id) })
Write-Host "Discovered menu sources eligible for OCR: $($MenuTargets.Count)"

$FailedMenuChunks = @()
for ($Offset = 0; $Offset -lt $MenuTargets.Count; $Offset += $ChunkSize) {
    Write-Host "Running menu OCR chunk offset=$Offset size=$ChunkSize"
    $Arguments = @(
        "-m", "nextrip_pipeline.cli", "batch-google-maps",
        "--manifest", $ManifestPath,
        "--mode", "menu",
        "--max-requests", $ChunkSize,
        "--offset", $Offset
    )
    if (-not [string]::IsNullOrWhiteSpace($ExcludeEntityId)) {
        $Arguments += @("--exclude-entity-id", $ExcludeEntityId)
    }
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        $FailedMenuChunks += $Offset
        Write-Warning "Menu OCR chunk failed at offset $Offset. See batch summary."
    }
    if ($Offset + $ChunkSize -lt $MenuTargets.Count -and $PauseSeconds -gt 0) {
        Start-Sleep -Seconds $PauseSeconds
    }
}

Write-Host "Place chunks with failures: $($FailedPlaceChunks -join ',')"
Write-Host "Menu chunks with failures: $($FailedMenuChunks -join ',')"
if ($FailedPlaceChunks.Count -gt 0 -or $FailedMenuChunks.Count -gt 0) {
    exit 1
}

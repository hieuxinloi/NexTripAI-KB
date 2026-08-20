[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PbfPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$')]
    [string]$DatasetVersion,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$Sha256,

    [string]$DataRoot = (Join-Path $PSScriptRoot 'valhalla-data')
)

$ErrorActionPreference = 'Stop'

$source = (Resolve-Path -LiteralPath $PbfPath).Path
if ([System.IO.Path]::GetExtension($source) -ne '.pbf') {
    throw 'PbfPath must point to an .osm.pbf or .pbf file.'
}

$actualHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
$expectedHash = $Sha256.ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "SHA-256 mismatch. Expected $expectedHash but found $actualHash."
}

$root = [System.IO.Path]::GetFullPath($DataRoot)
$targetDirectory = [System.IO.Path]::GetFullPath(
    (Join-Path $root $DatasetVersion)
)
$rootPrefix = $root.TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
) + [System.IO.Path]::DirectorySeparatorChar
if (-not $targetDirectory.StartsWith(
    $rootPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw 'Resolved dataset directory is outside DataRoot.'
}

New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
$targetPbf = Join-Path $targetDirectory 'vietnam.osm.pbf'
if (Test-Path -LiteralPath $targetPbf) {
    $existingHash = (
        Get-FileHash -LiteralPath $targetPbf -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($existingHash -ne $expectedHash) {
        throw "Dataset $DatasetVersion already exists with a different hash."
    }
} else {
    Copy-Item -LiteralPath $source -Destination $targetPbf
}

$manifest = [ordered]@{
    dataset_version = $DatasetVersion
    source_file = [System.IO.Path]::GetFileName($source)
    sha256 = $expectedHash
    prepared_at_utc = [DateTime]::UtcNow.ToString('o')
    valhalla_version = '3.8.3'
    valhalla_image = 'ghcr.io/valhalla/valhalla-scripted:3.8.3'
}
$manifestPath = Join-Path $targetDirectory 'dataset-manifest.json'
$manifest | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding utf8

Write-Output "Prepared $targetPbf"
Write-Output "Set VALHALLA_DATASET_VERSION=$DatasetVersion before starting Compose."

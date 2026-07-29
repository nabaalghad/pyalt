# pyalt installer: adds this package's bin\ folder to your user PATH.
# Run from the unzipped folder:  right-click -> Run with PowerShell
$bin = Join-Path $PSScriptRoot "bin"
if (-not (Test-Path (Join-Path $bin "pyalt.exe"))) {
    Write-Host "pyalt.exe not found next to this script - run it from the unzipped folder." -ForegroundColor Red
    exit 1
}
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -split ";" | Where-Object { $_ -eq $bin }) {
    Write-Host "Already installed: $bin is on your PATH."
} else {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
    Write-Host "Added to PATH: $bin"
}
Write-Host "Open a NEW terminal and try:  pyalt --version"

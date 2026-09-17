<#
    SARSense: create or remove Start menu shortcuts for the current user.
    Called by install.bat. Works with Windows PowerShell 5.1 and PowerShell 7.

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\windows_shortcuts.ps1 -Root <project folder> [-Desktop] [-Remove]
#>
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [switch]$Desktop,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
$programs = [Environment]::GetFolderPath('Programs')
$menu = Join-Path $programs 'SARSense'
$desktopDir = [Environment]::GetFolderPath('Desktop')
$desktopLink = Join-Path $desktopDir 'SARSense Hub.lnk'
$icon = Join-Path $Root 'sarsense\static\icon.ico'

if ($Remove) {
    if (Test-Path -LiteralPath $menu) { Remove-Item -LiteralPath $menu -Recurse -Force }
    if (Test-Path -LiteralPath $desktopLink) { Remove-Item -LiteralPath $desktopLink -Force }
    Write-Host 'Removed the SARSense Start menu shortcuts.'
    exit 0
}

New-Item -ItemType Directory -Force -Path $menu | Out-Null
$shell = New-Object -ComObject WScript.Shell

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$Description) {
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $Target
    $lnk.Arguments = $Arguments
    $lnk.WorkingDirectory = $Root
    $lnk.Description = $Description
    if (Test-Path -LiteralPath $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Save()
}

New-Shortcut (Join-Path $menu 'SARSense Hub.lnk') (Join-Path $Root 'run_hub.bat') '' 'Start the SARSense search and rescue hub'
New-Shortcut (Join-Path $menu 'SARSense Demo.lnk') (Join-Path $Root 'run_demo.bat') '' 'Try SARSense with simulated sensors'
New-Shortcut (Join-Path $menu 'SARSense Folder.lnk') (Join-Path $env:WINDIR 'explorer.exe') ('"' + $Root + '"') 'Open the SARSense folder'
New-Shortcut (Join-Path $menu 'Remove SARSense shortcuts.lnk') (Join-Path $Root 'install.bat') '--remove-shortcuts' 'Remove these Start menu entries (the SARSense folder is kept)'

# Internet shortcut for the web app
$url = @(
    '[InternetShortcut]'
    'URL=http://localhost:8080/'
    "IconFile=$icon"
    'IconIndex=0'
) -join "`r`n"
[System.IO.File]::WriteAllText((Join-Path $menu 'Open SARSense.url'), $url + "`r`n")

if ($Desktop) {
    New-Shortcut $desktopLink (Join-Path $Root 'run_hub.bat') '' 'Start the SARSense search and rescue hub'
}

Write-Host "Start menu entries created in: $menu"

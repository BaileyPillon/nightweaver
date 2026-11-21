[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Runtime,
    [string]$Python = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source,
    [string]$TaskName = 'Nightweaver Dispatcher',
    [string]$ToolPath,
    [switch]$RecoverGuards,
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $sourceRoot

$Python = (Resolve-Path -LiteralPath $Python).Path
$homeArgs = @()
if ($Runtime) { $homeArgs = @('--home', $Runtime) }
$diagnostic = & $Python -m nightweaver @homeArgs doctor
if ($LASTEXITCODE -ne 0) { throw 'Nightweaver could not resolve its runtime directory' }
$diagnostic = $diagnostic | ConvertFrom-Json
$Runtime = $diagnostic.data_directory
if (!$ToolPath) {
    $toolDirectories = $diagnostic.executables.PSObject.Properties.Value |
        Where-Object { $_ } | ForEach-Object { Split-Path -Parent $_ } | Select-Object -Unique
    $ToolPath = $toolDirectories -join ';'
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-m nightweaver --home "{0}" dispatch --watch' -f $Runtime.TrimEnd('\')
if ($RecoverGuards) { $arguments += ' --recover-guards' }
if ($ToolPath) { $arguments += ' --tool-path "{0}"' -f $ToolPath.TrimEnd('\') }
$windowless = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (Test-Path -LiteralPath $windowless) { $Python = $windowless }
if ($Preview) {
    [pscustomobject]@{ TaskName=$TaskName; User=$user; Execute=$Python; Arguments=$arguments;
        WorkingDirectory=$sourceRoot; Trigger='AtLogOn'; RecoverGuards=[bool]$RecoverGuards }
    return
}
if (-not $PSCmdlet.ShouldProcess($TaskName, 'Register the Nightweaver sign-in dispatcher')) { return }
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $sourceRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings

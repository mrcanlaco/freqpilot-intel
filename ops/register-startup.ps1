param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$TaskName = 'BTI-Desktop-Public-Runtime'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\pythonw.exe'
$Supervisor = Join-Path $PSScriptRoot 'windows_supervisor.py'
if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output 'Project startup task removed; existing runtime processes are not terminated.'
    exit 0
}
if (!(Test-Path -LiteralPath $Python)) { throw 'Python 3.13 pythonw.exe is missing.' }
if (!(Test-Path -LiteralPath $Supervisor)) { throw 'Project supervisor is missing.' }
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute $Python -Argument ('"' + $Supervisor + '"') -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Project-scoped BTI dashboard and dedicated tunnel; optional local scanner is enabled only by runtime/worker-enabled.json. Starts after this user signs in; no credentials stored.' -Force | Out-Null
$Task = Get-ScheduledTask -TaskName $TaskName
[pscustomobject]@{ TaskName=$Task.TaskName; State=$Task.State.ToString(); Execute=$Task.Actions.Execute; Arguments=$Task.Actions.Arguments; Trigger='AtLogOn'; User=$Task.Principal.UserId; LogonType=$Task.Principal.LogonType.ToString(); RunLevel=$Task.Principal.RunLevel.ToString() } | ConvertTo-Json

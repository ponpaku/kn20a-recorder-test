#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$taskName = "KN20A Recorder"
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$statusPath = Join-Path $appDir "data\boot_task_status.json"
New-Item -ItemType Directory -Path (Split-Path -Parent $statusPath) -Force | Out-Null
$pythonw = (& py -3 -c "import pathlib, sys; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))").Trim()
$supervisor = Join-Path $appDir "supervisor.py"
$recorder = Join-Path $appDir "recorder.py"
$legacyStartup = Join-Path ([Environment]::GetFolderPath("ApplicationData")) "Microsoft\Windows\Start Menu\Programs\Startup\KN20A Recorder.vbs"

[ordered]@{
    installed_at = (Get-Date).ToString("o")
    task_name = $taskName
    task_state = "Starting"
} | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8

trap {
    [ordered]@{
        installed_at = (Get-Date).ToString("o")
        task_name = $taskName
        task_state = "Failed"
        error = $_.Exception.Message
        script_line = $_.InvocationInfo.ScriptLineNumber
    } | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
    exit 1
}

if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "pythonw.exe not found: $pythonw"
}
if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
    throw "supervisor.py not found: $supervisor"
}

# A legacy sign-in launcher can win the single-instance lock before the SYSTEM
# task starts. In that state the apparent service belongs to the interactive
# session and can disappear with a tool/session shutdown. Remove that launch
# path and stop instances from this exact application directory before the
# authoritative SYSTEM task is registered.
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

$escapedSupervisor = [regex]::Escape($supervisor)
$escapedRecorder = [regex]::Escape($recorder)
Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -and
        ($_.CommandLine -match $escapedSupervisor -or $_.CommandLine -match $escapedRecorder)
    } |
    ForEach-Object {
        Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null
    }

if (Test-Path -LiteralPath $legacyStartup -PathType Leaf) {
    Remove-Item -LiteralPath $legacyStartup -Force
    Write-Output "Removed legacy sign-in launcher: $legacyStartup"
}

$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument ('"' + $supervisor + '"') `
    -WorkingDirectory $appDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Start and supervise the KN20A scheduled recorder before user sign-in."

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 5

$registered = Get-ScheduledTask -TaskName $taskName
$taskInfo = Get-ScheduledTaskInfo -TaskName $taskName
$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1

Write-Output "Registered: $taskName"
Write-Output "Task state: $($registered.State)"
Write-Output "Last task result: $($taskInfo.LastTaskResult)"
if ($listener) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
    $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner
    [ordered]@{
        installed_at = (Get-Date).ToString("o")
        task_name = $taskName
        task_state = [string]$registered.State
        last_task_result = $taskInfo.LastTaskResult
        listener_pid = $listener.OwningProcess
        listener_owner = "$($owner.Domain)\$($owner.User)"
        legacy_startup_removed = -not (Test-Path -LiteralPath $legacyStartup)
    } | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
    Write-Output "Recorder listener PID: $($listener.OwningProcess)"
    Write-Output "Recorder listener owner: $($owner.Domain)\$($owner.User)"
} else {
    throw "The task was registered, but the recorder did not open 127.0.0.1:8765."
}

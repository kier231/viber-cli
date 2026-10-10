$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'SajtologViberWorker' -ErrorAction SilentlyContinue
$info = Get-ScheduledTaskInfo -TaskName 'SajtologViberWorker' -ErrorAction SilentlyContinue
$processes = @(Get-CimInstance Win32_Process -Filter "Name='Viber.exe' OR Name='python.exe' OR Name='pythonw.exe'" |
    ForEach-Object { @{name=$_.Name;pid=$_.ProcessId;parent=$_.ParentProcessId;created=$_.CreationDate.ToString('o');executable=$_.ExecutablePath;agent=($_.CommandLine -match 'app\.vm_agent');reader=($_.CommandLine -match 'app\.viber_source_worker')} })
$listener = @(Get-NetTCPConnection -State Listen -LocalPort 4011 -ErrorAction SilentlyContinue)
$marker = Join-Path $env:LOCALAPPDATA 'viber-cli\instance-reset-complete.json'
@{task_state=[string]$task.State;task_result=$info.LastTaskResult;processes=$processes;
  listening=($listener.Count -gt 0);listener_pids=@($listener.OwningProcess);profile_prepared=(Test-Path -LiteralPath $marker)} | ConvertTo-Json -Depth 4
foreach ($name in @('viber_database.py','viber_source_worker.py','viber_watcher.py','viber_background.py')) {
    $sourcePath = Join-Path 'C:\viber-cli\app' $name
    @{file=$name;modified=(Get-Item -LiteralPath $sourcePath).LastWriteTimeUtc.ToString('o');hash=(Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash;
      query_lines=@(Select-String -LiteralPath $sourcePath -Pattern 'WHERE e.ChatID|after_event_id|TimeStamp>=|def read_snapshot|last_scan_at|force_refresh|unchanged' | ForEach-Object { @{line=$_.LineNumber;text=$_.Line.Trim()} })} | ConvertTo-Json -Depth 4
}
if ((Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).id -eq 'instance-1') {
    @'
import json,sys
sys.path.insert(0,'C:/viber-cli')
import pythoncom
pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
from app.viber_background import BackgroundViberClient
c=BackgroundViberClient(allow_foreground=True).connect()
for p in c._nodes():
 if c.ui._auto_id(p).endswith('ProfilePopup'):
  print(json.dumps({'popup_children':[{'type':c.ui._type(n),'id':c.ui._auto_id(n)} for n in p.children()],
                   'popup_descendants':[{'type':c.ui._type(n),'id':c.ui._auto_id(n)} for n in p.descendants()]},ensure_ascii=True))
'@ | & 'C:\viber-cli\.venv\Scripts\python.exe' -
    if ($LASTEXITCODE -ne 0) { throw 'Read-only popup inspection failed.' }
}

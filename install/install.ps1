<#
.SYNOPSIS
    dsh-post 在 Windows 上的安装脚本：建计划任务 + 校验 ssh 别名可达。

.DESCRIPTION
    只做三件事，别的都不碰：
      ① 说明清楚要往哪写文件、建哪个计划任务
      ② 校验 ssh 别名能不能连（连不上只是警告，因为「本机就是中心」时用不着）
      ③ 建/改一个计划任务，每 N 分钟跑一轮 `python -m bridge.loop --once`

    为什么用计划任务而不是常驻守护：Windows 上常驻进程在注销后会掉，
    而投递这件事「晚两分钟」的代价远小于「没人投」。

    ⛔ 这个脚本不写任何远端文件、不改对方设备上的任何东西。

.PARAMETER WhatIf
    空跑：把将要做的事一条条打出来，不改系统。装之前先跑一遍这个。

.PARAMETER IntervalSeconds
    0 = 从 bridge.toml 的 [loop].interval_seconds 读（推荐）。
    计划任务粒度是分钟，小于 60 秒的值会向上取整到 1 分钟。

.PARAMETER Uninstall
    删掉这个计划任务（⛔ 不删任何数据文件）。

.EXAMPLE
    pwsh -File install\install.ps1 -WhatIf
    pwsh -File install\install.ps1
    pwsh -File install\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [string]$PythonExe = "",
    [string]$TaskName = "dsh-post-loop",
    [int]$IntervalSeconds = 0,
    [switch]$Uninstall,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $ConfigPath) { $ConfigPath = Join-Path $Root "bridge.toml" }

function Say([string]$Tag, [string]$Msg) {
    Write-Host ("  [{0}] {1}" -f $Tag, $Msg)
}

function Plan([string]$Msg) { Say "计划" $Msg }
function Do([string]$Msg) { Say "执行" $Msg }
function Ok([string]$Msg) { Say "OK" $Msg }
function Warn([string]$Msg) { Say "注意" $Msg }

Write-Host ""
Write-Host "dsh-post 安装（Windows）"
Write-Host ("  仓目录：{0}" -f $Root)
Write-Host ("  配置文件：{0}" -f $ConfigPath)
if ($WhatIf) { Write-Host "  模式：空跑（-WhatIf），不改系统" -ForegroundColor Yellow }
Write-Host ""

# ── ① 卸载路径 ──
if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Warn ("没有名为 {0} 的计划任务，无需卸载" -f $TaskName)
        exit 0
    }
    if ($WhatIf) {
        Plan ("将删除计划任务 {0}" -f $TaskName)
        Plan "（不会删除 bridge.toml、收件箱、发件箱、聊天室记录 —— 一个都不动）"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Ok ("已删除计划任务 {0}" -f $TaskName)
    exit 0
}

# ── ② Python 与版本 ──
if (-not $PythonExe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Host "[X] 找不到 python。装 Python 3.11+ 并让它进 PATH，或用 -PythonExe 指定绝对路径。" -ForegroundColor Red
        exit 2
    }
    $PythonExe = $cmd.Source
}
Ok ("python：{0}" -f $PythonExe)
$verText = & $PythonExe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host ("[X] {0} 跑不起来：{1}" -f $PythonExe, $verText) -ForegroundColor Red
    exit 2
}
$ver = [version]($verText.ToString().Trim())
if ($ver -lt [version]"3.11") {
    Write-Host ("[X] Python {0} 太老，这包用 tomllib，需要 3.11+" -f $ver) -ForegroundColor Red
    exit 2
}
Ok ("Python 版本 {0}（需要 >= 3.11）" -f $ver)

# ── ③ 配置能不能读（借包自己的加载器，⛔ 不在这里二次解释 TOML）──
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Host ("[X] 找不到配置文件：{0}" -f $ConfigPath) -ForegroundColor Red
    Write-Host ("    先复制一份模板：Copy-Item '{0}\bridge.toml.example' '{1}'，再改设备表与路径。" -f $Root, $ConfigPath) -ForegroundColor Yellow
    exit 2
}
$probe = @"
import sys
sys.path.insert(0, r'$Root')
from bridge.config import load_config, describe
cfg = load_config(r'$ConfigPath')
print(cfg.loop.interval_seconds)
print(cfg.me)
print('|'.join(sorted(cfg.devices)))
print('|'.join(f'{k}={v}' for k, v in sorted(cfg.recipients.items())))
"@
$probeOut = & $PythonExe -c $probe 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] 配置读不通，先把这条修好再装：" -ForegroundColor Red
    Write-Host ($probeOut -join "`n") -ForegroundColor Red
    exit 2
}
$lines = @($probeOut | ForEach-Object { $_.ToString() })
$cfgInterval = 120
if ($lines.Count -ge 1) { [void][int]::TryParse($lines[0].Trim(), [ref]$cfgInterval) }
$meKey = if ($lines.Count -ge 2) { $lines[1].Trim() } else { "?" }
$devKeys = if ($lines.Count -ge 3) { $lines[2].Trim() } else { "" }
Ok ("配置可用：本机={0} · 设备={1}" -f $meKey, $devKeys)

if ($IntervalSeconds -le 0) { $IntervalSeconds = $cfgInterval }
$minutes = [math]::Max(1, [int][math]::Ceiling($IntervalSeconds / 60.0))
Ok ("间隔：配置里 {0}s => 计划任务每 {1} 分钟一轮（计划任务最小粒度是分钟）" -f $IntervalSeconds, $minutes)

# ── ④ ssh 别名可达性（连不上只警告）──
$aliasesProbe = @"
import sys
sys.path.insert(0, r'$Root')
from bridge.config import load_config
cfg = load_config(r'$ConfigPath')
for d in cfg.devices.values():
    if d.key == cfg.me:
        continue
    for a in d.aliases:
        print(f'{d.key}\t{a}')
"@
$pairs = @(& $PythonExe -c $aliasesProbe 2>&1 | ForEach-Object { $_.ToString() } | Where-Object { $_ -match "`t" })
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Warn "本机没有 ssh 命令。只在本机读写、不动别台时可以继续；要投别台必须先装 OpenSSH 客户端。"
} elseif ($pairs.Count -eq 0) {
    Warn "配置里没有任何远端设备的 aliases，跳过连通性检查。"
} else {
    foreach ($p in $pairs) {
        $dev, $alias = $p -split "`t"
        if ($WhatIf) {
            Plan ("将测试：ssh -o BatchMode=yes -o ConnectTimeout=5 {0} -- true   （{1} 的别名）" -f $alias, $dev)
            continue
        }
        # ⚠️ 只跑 `true`：只读探活，不写对方任何文件、不碰对方计划任务。
        & ssh -o BatchMode=yes -o ConnectTimeout=5 $alias -- true 2>$null
        if ($LASTEXITCODE -eq 0) {
            Ok ("ssh 可达：{0} -> {1}" -f $dev, $alias)
        } else {
            Warn ("ssh 连不上 {0}（别名 {1}）。三台互信怎么配见 docs\01_三台互信怎么配.md；" -f $dev, $alias)
            Warn "  连不上不影响装：这一轮投递会把这台记为失败，其它台照投。"
        }
    }
}

# ── ⑤ 建计划任务 ──
$argLine = "-m bridge.loop --once --config `"$ConfigPath`""
$startAt = (Get-Date).AddMinutes(1)

if ($WhatIf) {
    Plan ("将注册计划任务：{0}" -f $TaskName)
    Plan ("  执行：`"{0}`" {1}" -f $PythonExe, $argLine)
    Plan ("  目录：{0}" -f $Root)
    Plan ("  触发：从 {0} 起，每 {1} 分钟一次，无限期" -f $startAt.ToString("HH:mm:ss"), $minutes)
    Plan "  重复实例策略：IgnoreNew（上一轮没跑完，这一轮直接跳过 —— 投递天然幂等，跳过比排队好）"
    Plan "  （同时也会因为 bridge 自己的文件锁，重叠的两轮只会有一轮真正干活）"
    Write-Host ""
    Write-Host "空跑结束：以上命令一条都没执行。去掉 -WhatIf 就真做。" -ForegroundColor Yellow
    exit 0
}

# ⚠️ 计划任务三件套（Action/Trigger/Settings）只在真要装的时候才构造：
#    某些精简版 Windows 上 ScheduledTasks 模块的类根本没注册，
#    一构造就报「指定的类不存在」—— 空跑不该因为这种事失败。
if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    Write-Host "[X] 这个 PowerShell 里没有 ScheduledTasks 模块（Register-ScheduledTask 不可用）。" -ForegroundColor Red
    Write-Host "    两条路：① 用系统自带的 Windows PowerShell 5.1 跑本脚本；" -ForegroundColor Yellow
    Write-Host "            ② 手工建计划任务，命令如下：" -ForegroundColor Yellow
    Write-Host ("    schtasks /Create /TN `"{0}`" /SC MINUTE /MO {1} /F /TR `"`"{2}`" {3}`"" -f $TaskName, $minutes, $PythonExe, $argLine)
    exit 2
}

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $argLine -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Once -At $startAt `
    -RepetitionInterval (New-TimeSpan -Minutes $minutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "dsh-post：每 $minutes 分钟投递一轮（$Root）" -Force | Out-Null
Ok ("已注册计划任务 {0}" -f $TaskName)

Write-Host ""
Write-Host "接下来（手工，脚本不替你做）："
Write-Host ("  1) 试投一封：'{0}' -m bridge.post --once --dry-run --config '{1}'" -f $PythonExe, $ConfigPath)
Write-Host ("  2) 看状态：  '{0}' -m bridge.post --status --config '{1}'" -f $PythonExe, $ConfigPath)
Write-Host ("  3) 跑一次真投：'{0}' -m bridge.loop --once --config '{1}'" -f $PythonExe, $ConfigPath)
Write-Host "  4) 三台互信还没配好？先按 docs\01_三台互信怎么配.md 配 ssh 别名。"
Write-Host ""
Write-Host "卸载：pwsh -File install\install.ps1 -Uninstall（只删计划任务，不删数据）"
Write-Host ""
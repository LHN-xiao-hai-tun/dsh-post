# dsh-post v0.1

三台（或多台）机器之间**同步聊天** + **传递信件**。零第三方依赖，只用标准库和系统 `ssh`。

- Python **≥ 3.11**（用了 `tomllib`）
- 系统 `ssh` 客户端（只在多机模式需要）
- 无 pip 安装、无打包、无服务端

> 想先证明它真能跑：直接看下面「五分钟跑通」那一段 —— 从建目录到收到回信，
> 命令可以逐条照抄；预期输出里的路径写成了 `…\dsh-post-demo\…` 这种缩写，
> 读的时候把它当成你自己的 `$w` 即可（第 5 步把空跑和真投两段输出放在一个块里，
> 中间有 `（空跑）` 前缀区分，不是出错）。

---

## 它解决什么

同一个人手上有电脑、平板、手机。三台上各有一个 AI 会话在干活，于是需要两件事：

1. **一个共享聊天室**：三台都往里说，都能按自己的进度读到新的。
2. **一个信箱**：在一台上写好一封信，它出现在另外几台的收件箱里，一字不差。

这个包就是这两件事，外加把它们跑起来的守护和安装脚本。

---

## 五分钟跑通（单机双目录，不碰 ssh）

> **懒得起配置？** 先跑这一条：`python examples\minimal\setup.py` —— 它自己在 `%TEMP%` 下建两个目录当两台设备，跑完打 `PASS 4/4`。它验的是「核心链路没坏」，**不代替**下面这套手写配置的走查（那才是陌生人真正要迈的坎）。

先证明"它能用"再联真机 —— 这一步不需要第二台机器：拿两个目录当「两台设备」，
`transport = "local"`，全程不碰 ssh、不碰任何别的机器。

**三个前提，少一条就会卡住：**

| 前提 | 说明 |
|---|---|
| Python ≥ 3.11 | `python --version` |
| **命令要在包目录里跑** | 下面所有 `python -m bridge.*` 的 cwd 必须是 `dsh-post/`（有 `bridge/` 那个目录）。在别处跑会 `ModuleNotFoundError: No module named 'bridge'` —— 「不需要装成包」不等于「哪跑都行」，见「命令」节开头 |
| 两个可写目录 | 下面的演练用 `%TEMP%\dsh-post-demo`，跑完删掉 |

### 第 1 步：建目录

> **下面 7 步在同一个 PowerShell 窗口里连着跑** —— 第 1 步设的 `$w` 后面一直在用；
> 换窗口或换标签页就得重新设一次。

```powershell
$w = Join-Path $env:TEMP "dsh-post-demo"
New-Item -ItemType Directory -Force -Path "$w\devA\发件箱", "$w\devB\发件箱" | Out-Null
```

`发件箱` 得先有（往不存在的目录里写不了文件）；`收件箱` 不用建 —— 第一次投递时代码自己创建。

### 第 2 步：写两份配置（只差 `[general].me` 一行）

把下面这份存成 `$w\bridgeA.toml`：

```toml
[general]
root = "."                  # 相对路径的基准；相对**配置文件所在目录**展开 => 就是 $w
me = "deva"                 # 本机是哪个 [device.*] 的 key
transport = "local"         # 单机双目录试跑；联真机时改成 "ssh"

[device.deva]
name = "设备A"
workspace = "devA"          # 相对 root => $w/devA
inbox = "收件箱"
outbox = "发件箱"
aliases = []

[device.devb]
name = "设备B"
workspace = "devB"          # 相对 root => $w/devB
inbox = "收件箱"
outbox = "发件箱"
aliases = []

[hub]
host = "deva"               # 必须是上面某个 device key；local 模式下 hub 文件谁都读得到
path = "聊天室/hub.jsonl"   # 相对 hub.host 那台的 workspace

[post]
exts = [".md"]
stale_minutes = 30
verify = true

[receive]
layout = "subdir"
backend = "local"
processed_dir = ""

[naming]
act_kinds = ["信", "回话", "同步", "知会"]
enforce_since = ""
max_text_chars = 20000

[recipients]
"大家" = "*"
"设备A" = ["deva"]          # 右边每个 key 都必须出现在 [device.*] 里
"设备B" = ["devb"]

[loop]
interval_seconds = 5        # 下限 5；写 1 会直接退出码 2
```

再存一份 `$w\bridgeB.toml`：**内容与上面完全相同，只把 `me` 改成 `devb`**。

⚠️ 下面这段是**片段**，不是一份能单独用的文件 —— 把上面那份整份复制过去，只改这一行：

```toml
me = "devb"
```

> 两份配置里**只有这一行不同**。`me` 决定「本机是谁」，其余（设备表、hub、词表）必须一致 ——
> 否则同一封信在两边解读出不同的收件方。

### 第 3 步：先 `--where` 自检（排错第一把钥匙）

```powershell
cd <包目录>
python -m bridge.chat --where --config "$w\bridgeA.toml"
python -m bridge.chat --where --config "$w\bridgeB.toml"
```

期望（路径会不同，但**必须是 `devA` / `devB`**，不能是 `示例`）：

```
配置：…\work\bridgeA.toml
本机：deva（设备A）· 传输：local
工作区：…\work\devA
收件箱：…\work\devA\收件箱
发件箱：…\work\devA\发件箱
中心：…\work\devA\聊天室\hub.jsonl（在 deva 上）
心跳：…\work\devA\聊天室\.chat.beat · 判死线 130s
设备：deva=设备A、devb=设备B
```

第二条应是 `本机：devb（设备B）`、工作区 `…\work\devB`、中心仍在 `…\work\devA\…`。

🔴 **逐行核对「工作区」那一行**。还指着 `D:\示例\工作区` 就是 `[device.*].workspace` 没改
（只改 `root` 不够 —— 模板里的 workspace 是绝对路径，`root` 管不到它）。

### 第 4 步：聊天室（A 说，B 读）

```powershell
python -m bridge.chat --say "第一句" --config "$w\bridgeA.toml"
python -m bridge.chat --read --config "$w\bridgeB.toml"
```

```
[OK] 已写 #1（3 字符）→ …\work\devA\聊天室\hub.jsonl
#1 [2026-11-07 20:33:42] deva: 第一句
（新 1 条 · 共 1 条）
```

### 第 5 步：投一封信（注意文件名）

```powershell
Set-Content -LiteralPath "$w\devA\发件箱\知会_给设备B_演练第一封_20261107.md" `
  -Value "# 演练`n`n这是第一封信。" -Encoding utf8
python -m bridge.post --once --dry-run --config "$w\bridgeA.toml"
python -m bridge.post --once --config "$w\bridgeA.toml"
Get-ChildItem -Recurse -File "$w\devB\收件箱" | Select-Object -ExpandProperty FullName
```

```
（空跑）将投：知会_给设备B_演练第一封_20261107.md → devb
（空跑）候选 1 · 投出 0 · 跳过 0 · 被拦 0 · 失败 0
   · （空跑）将投：知会_给设备B_演练第一封_20261107.md → devb
候选 1 · 投出 1 · 跳过 0 · 被拦 0 · 失败 0
…\work\devB\收件箱\知会_给设备B_演练第一封_20261107.md
```

（空跑会先打一行 `将投`，汇总后面再把它列一遍 —— 同一个意思打两次，不是出错。）

文件名形状见「命令 · 投递 · 文件名契约」：`知会` 是性质、`给设备B` 是收件方词、
`演练第一封` 是主题、`20261107` 是日期。**四段少一段都不会投，只会回一个 `bad-fields`。**

可选核验「一字不差」：

```powershell
(Get-FileHash "$w\devA\发件箱\知会_给设备B_演练第一封_20261107.md" -Algorithm MD5).Hash
(Get-FileHash "$w\devB\收件箱\知会_给设备B_演练第一封_20261107.md" -Algorithm MD5).Hash
# 两行哈希应完全一样
```

### 第 6 步：回信 + 归位 + 读信

```powershell
Set-Content -LiteralPath "$w\devB\发件箱\知会_给设备A_收到_20261107.md" `
  -Value "# 回信`n`n收到了。" -Encoding utf8
python -m bridge.loop --once --config "$w\bridgeB.toml"     # B 一方：投出回信
python -m bridge.loop --once --config "$w\bridgeA.toml"     # A 一方：收下并归位
Get-ChildItem -Recurse -File "$w\devA\收件箱" | Select-Object -ExpandProperty FullName
Get-Content -LiteralPath "$w\devA\收件箱\deva\知会_给设备A_收到_20261107.md"
```

```
[OK] 归位：知会_给设备B_演练第一封_20261107.md → …\work\devB\收件箱\devb\知会_给设备B_演练第一封_20261107.md
[2026-11-07 20:35:53] 候选 1 · 投出 1 · 失败 0 · 归位 1
[OK] 归位：知会_给设备A_收到_20261107.md → …\work\devA\收件箱\deva\知会_给设备A_收到_20261107.md
[2026-11-07 20:35:53] 候选 0 · 投出 0 · 失败 0 · 归位 1
…\work\devA\收件箱\.received.json
…\work\devA\收件箱\知会_给设备A_收到_20261107.md
…\work\devA\收件箱\deva\知会_给设备A_收到_20261107.md
# 回信

收到了。
```

（B 那一轮顺手把第 5 步收到的信也归位了，所以它 `投出 1 · 归位 1`。）

**这就是「读信」**：收件箱里那个 `.md` 文件，用 `Get-Content` / `cat` / 编辑器打开即可。
`bridge.receive` 只是把它复制进 `收件箱/<收件方key>/` 并记账，不打印正文、不改内容。

### 第 7 步：三条自测

```powershell
python -m bridge.post --self-test     # 自测：17/17 通过
python tests/test_chat.py             # 断言：38/38 通过
python tests/test_post.py             # 断言：40/40 通过
```

三条都打印全对、退出码 `0`，就说明包本身是好的。

### 收尾

```powershell
Set-Location $env:TEMP
Remove-Item -Recurse -Force $w
```

### Linux / macOS / Termux 上的同一套命令

`python -m bridge.*` 本身两平台同形，只有 shell 自带的那几行要换：

| 做什么 | PowerShell | sh |
|---|---|---|
| 建目录 | `New-Item -ItemType Directory -Force -Path "$w\devA\发件箱"` | `mkdir -p "$w/devA/发件箱"` |
| 写一封信 | `Set-Content -LiteralPath <路径> -Value "正文" -Encoding utf8` | `printf '%s\n' "正文" > <路径>` |
| 看收件箱 | `Get-ChildItem -Recurse -File <目录>` | `find <目录> -type f` |
| 读信 | `Get-Content -LiteralPath <路径>` | `cat <路径>` |
| 删临时目录 | `Remove-Item -Recurse -Force $w` | `rm -rf "$w"` |

### 跑通之后：接真机

按 `docs/01_三台互信怎么配.md` 配 ssh 别名，把 `transport` 改成 `ssh`，
每台机器上的 `bridge.toml` 只差 `[general].me` 一个值。
⚠️ `ssh` 模式下**别台**的 `workspace` 必须是那台机器上的绝对路径（`/srv/work` 这种），
写相对路径会直接报错。

---

## 命令

四条命令，各自独立，**不需要装成包**。

⚠️ 但 **cwd 必须是包目录**：`python -m bridge` 靠当前目录找 `bridge/` 这个包。
在别处跑会 `ModuleNotFoundError: No module named 'bridge'`。
要么 `cd` 进包目录，要么 `$env:PYTHONPATH = "<包目录>"`。
（`install.ps1` 建的计划任务已经写了 `-WorkingDirectory <包目录>`，不用你操心。）

### 聊天室

```sh
python -m bridge.chat --where                # 只打印解析后的真实路径（排错第一步）
python -m bridge.chat --status               # 条数/最大 seq/游标差几条/坏行/归档/心跳
python -m bridge.chat --read                 # 读游标之后的新消息，并推进游标
python -m bridge.chat --tail 10              # 最后 10 条（不动游标）
python -m bridge.chat --say "一句话"          # 写一条
python -m bridge.chat --say -                # 从 stdin 读（正文含反斜杠时最安全）
python -m bridge.chat --say-file 草稿.md      # 整份文件当一条写
python -m bridge.chat --beat                 # 摸一次心跳
python -m bridge.chat --archive --keep 2000  # 归档（只在中心那台跑）
```

> `--where` 和 `--status` 开头是 8 行配置摘要（配置/本机/工作区/收件箱/发件箱/中心/心跳/设备，
> 有配置告警时再多几行），脚本要解析输出就得先跳过这一段；`--read` / `--say` 不打摘要。

### 投递

```sh
python -m bridge.post --once                 # 扫发件箱，投一轮
python -m bridge.post --once --dry-run       # 空跑：只说将投给谁
python -m bridge.post --once --alarm-exit    # 有积压/被拦 => 退出码 1（计划任务用）
python -m bridge.post --status               # 认得出的/已投/待投/被拦/积压
python -m bridge.post --send 某封信.md         # 只投这一件（收件方按文件名解）
python -m bridge.post --self-test            # 临时目录自测，不需要配置
```

`--status` 与 `--once` 里那几个词是这么算的：

| 词 | 含义 |
|---|---|
| 认得出的 | 名字合契约、能从文件名解出收件方（`bad-*` 之外的都算） |
| 已投 | 文件名 + md5 记在 `发件箱/.posted.json` 里，不会再投 |
| 待投 | 认得出，但还没投过 |
| 被拦 | 名字不合规（`bad-fields` / `bad-target` / `bad-act`），**永远不会**投，人不改它就不动 |
| 积压 | 认得出、还没投过、且躺了超过 `[post].stale_minutes` 分钟（`--alarm-exit` 据此报警） |

#### 文件名契约（写错就投不出去）

发件箱里的文件必须叫：

```
<性质>_<收件方词>_<主题>_<YYYYMMDD>.md
```

| 段 | 要求 | 从哪来 |
|---|---|---|
| 性质 | 必须是 `[naming].act_kinds` 里的一个（默认 `信` `回话` `同步` `知会`） | 配置 |
| 收件方词 | 必须能在 `[recipients]` 里查到；段里可带 `发给` / `给` / `回` 前缀，前缀会被剥掉再查 | 配置 |
| 主题 | 除下划线 `_` 外随便写：中文、空格、标点都行 | 你定 |
| 日期 | `20261107` 或 `2026-11-07` | 你定 |
| 后缀 | 必须是 `[post].exts` 里的（默认只有 `.md`） | 配置 |

```
✅ 知会_给设备B_演练第一封_20261107.md      性质=知会 · 收件方词=设备B · 前缀「给」被剥掉
✅ 信_给大家_周五结论_2026-11-07.md
❌ 给设备B_演练第一封_20261107.md           少一段（没有性质）          => bad-fields
❌ 知会_给丙机_演练_20261107.md             「丙机」不在 [recipients]    => bad-target
❌ 通知_给设备B_演练_20261107.md            「通知」不在 act_kinds       => bad-act
```

三种拒绝分别是什么意思、怎么修：

| 报的原因 | 什么意思 | 怎么修 |
|---|---|---|
| `bad-fields` | 整份名字对不上模板：段数不对、日期不是 `YYYYMMDD`/`YYYY-MM-DD`、后缀不是 `.md` | **整份改名** |
| `bad-target` | 第二段剥掉前缀后，在 `[recipients]` 里查不到这个词 | 改**一个词**，换成表里有的 |
| `bad-act` | 第一段不在 `[naming].act_kinds` 里 | 改**一个词**，换成表里有的 |

被拦的件**不会自己消失**：`--once` 只回一行 `被拦（bad-target）` 就不动它；
`--status` 会逐件列出全部原因（正则与顺序契约见 `docs/02_命名与落点.md` §二）。

#### 投出去之后

- **对方拿到的是一个普通 `.md` 文件**，落在 `<对方 workspace>/<inbox>/<原文件名>`（默认 `收件箱/`），
  逐字节一致：`[post].verify = true` 时会取对端 md5 比对，对不上算投失败。
- **原件仍留在你的发件箱**：投成功只是 `copy2` 一份到 `发件箱/已发/` 记账。
  所以下一轮 `--once` 会显示 `跳过 1` —— 那是「已投过」，不是「没投」。
- 投递只在**本机**动文件（`local`）或 `ssh` 写到对方收件箱；**它不替对方归位**，归位是收件那台的活。

### 收件归位

`bridge.receive` 只做一件事：把收件箱顶层的件**按收件方分格**，复制进 `收件箱/<收件方设备key>/`，
并在 `收件箱/.received.json` 里记 `{文件名: md5}` 判重。**它不打印正文、不改内容 —— 归位不是「读」。**

```sh
python -m bridge.receive --once              # 把收件箱里的件按收件方分格
python -m bridge.receive --once --dry-run
python -m bridge.receive --status
```

**收到信之后怎么读：**

| 问题 | 答案 |
|---|---|
| 信是什么 | 收件箱里的一个 `.md` 普通文件（正文就是你写进去的字节） |
| 默认落点 | 投递后：`<你的 workspace>/收件箱/<文件名>`；归位后另有一份：`<你的 workspace>/收件箱/<收件方设备key>/<文件名>`（`[receive].layout = "subdir"`） |
| 怎么读 | 用任何文本工具打开它：`Get-Content -LiteralPath <路径>` / `cat <路径>`。包里**没有** `bridge.read` 这种命令 |
| 原件会消失吗 | 归位默认**留着**（`[receive].processed_dir` 为空）。把它设成 `已处理` 才会把原件搬进 `收件箱/已处理/` |
| 怎么确认没被改坏 | 比 md5：`Get-FileHash … -Algorithm MD5` / `md5sum` |

### 守护

```sh
python -m bridge.loop --once                 # 跑一轮（计划任务起的就是它）
python -m bridge.loop --start                # 前台常驻（systemd / setsid 起它）
python -m bridge.loop --status               # 在不在跑、上一轮什么时候
python -m bridge.loop --start --rounds 2     # 跑两轮就停（调试）
```

`--once` 一轮做三件事：投发件箱 → 收件归位 → 写状态，输出一行
`[时间] 候选 N · 投出 N · 失败 N · 归位 N`。

### `--config` 与配置查找顺序

所有命令都接受 `--config <路径>`。不写时的查找顺序是（以 `bridge/config.py:375-398` 为准）：

1. 命令行 `--config`
2. 环境变量 `DSH_BRIDGE_CONFIG`
3. **当前目录**下的 `bridge.toml`
4. 包目录旁边的 `bridge.toml`

⚠️ 环境变量排在第 2 位，会**盖掉**当前目录那一份 —— 双设备调试完记得
`Remove-Item Env:\DSH_BRIDGE_CONFIG`，否则你会以为自己在用 A 的配置、其实是 B 的。

双设备时两份配置不可能都叫 `bridge.toml`（自动查找只认这个文件名），所以起名
`bridgeA.toml` / `bridgeB.toml`，每条命令显式带上：

```powershell
python -m bridge.chat --where --config "$w\bridgeA.toml"   # 当 A
python -m bridge.post --once --config "$w\bridgeB.toml"    # 当 B
```

---

## 配置

一份 `bridge.toml`，模板是 `bridge.toml.example`。**所有路径、设备、阈值都在里面**，
代码里没有任何写死的设备名或目录 —— 想写死也没有地方写。

### 🔴 拿到模板后必须改的五处

| # | 位置 | 改成什么 | 不改会怎样 |
|---|---|---|---|
| 1 | `[general].me` | 本机的 `[device.*]` key | 认不出本机 → 报错；或认成别台 |
| 2 | `[general].transport` | `local`（试跑）/ `ssh`（联真机） | 模板里是 `ssh`，单机试跑会去连别的机器 |
| 3 | `[general].root` | 一个真存在、可写的目录 | 所有相对路径落错地方 |
| 4 | **每个** `[device.*].workspace` | 该设备上的真目录 | 🔴 **只改 root 不够**：模板里是 `D:/示例/工作区`、`/srv/示例/工作区` 这种绝对路径，`root` 管不到它们 |
| 5 | `[hub].host` + `[recipients]` 右侧 | 必须是**真实存在**的 device key | 加载期直接报错退出 |

模板自己第一行注释就写着「照抄一定跑不通」—— 这五处就是它指的东西。

### root / workspace / 设备 key 谁管谁

| 东西 | 是什么 | 谁在用 |
|---|---|---|
| **设备 key** | `[device.<key>]` 方括号里那个词，用 ASCII（`pc` / `deva`）。给人看的名字放 `name` | `[general].me`、`[hub].host`、`[recipients]` 的右侧、`收件箱/<key>/` 目录名，都用它 |
| **`root`** | 相对路径的展开基准，**相对配置文件所在目录**展开 | 各处的相对路径 |
| **`workspace`** | 该设备的根目录。`inbox` / `outbox` / `state_dir` 都相对它展开 | 每台一段 |

`transport` 决定这些路径怎么解释：

- `transport = "local"`：**所有**设备路径都在本机解析，相对路径按 `root` 展开，绝对路径原样。
  所以单机双目录演练里 `workspace = "devA"` 就行。
- `transport = "ssh"`：**本机**那段可以写相对路径；**别台**那段必须是那台机器上的绝对路径
  （POSIX 风格），写相对会直接报错（`bridge/config.py:490-494`）——
  相对路径只会按本机的 `root` 展开，在别台是错的。

算几条给你对：

| 配置 | 解析结果 |
|---|---|
| `root = "D:/demo/work"` + `workspace = "devA"` | `D:/demo/work/devA` |
| `root = "D:/demo/work"` + `workspace = "D:/other/devB"` | `D:/other/devB`（绝对路径不看 root） |
| `root = "."`（配置文件在 `%TEMP%\demo`）+ `workspace = "devA"` | `%TEMP%\demo\devA` |
| ssh 模式下 `[device.phone].workspace = "/srv/work"` | 手机上的 `/srv/work`（本机不去展开） |

**自检只有一招**：`python -m bridge.chat --where`，逐行核对打印出来的
「工作区 / 收件箱 / 发件箱 / 中心」是不是你要的目录。任何一行还带「示例」，就是没改。

### 段表

| 段 | 管什么 |
|---|---|
| `[general]` | `root`（相对路径基准）、`me`（本机是哪台）、`transport`（`ssh` / `local`）、`state_dir` |
| `[device.<key>]` | 每台的 `workspace` / `inbox` / `outbox` / `sent_dir` / `aliases`（ssh 别名，可多个按顺序试） |
| `[hub]` | 聊天室中心在哪台（`host`）、路径、心跳判死线、归档提醒线 |
| `[post]` | 哪些后缀算信、积压阈值、投完要不要对 md5 |
| `[receive]` | 归位布局（`subdir`/`flat`）、后端、处理完把原件搬哪 |
| `[naming]` | 性质前缀表、新规矩生效日期、正文长度上限 |
| `[recipients]` | **收件方词表**：`"大家" = "*"`、`"设备B" = ["devb"]` |
| `[loop]` | 间隔秒数（**下限 5**）、pid / 状态文件 |

### 联动关系（改一处必须跟着改另一处）

- **`[recipients]` 右侧的每个 key 都必须在 `[device.*]` 里存在**，否则加载就失败：
  `[X] [recipients] 的 '电脑' 引用了不存在的设备：pc`（退出码 1）。
  所以**改设备 key 时，`[recipients]` 必须同步改**；`"大家" = "*"` 会自动展开成全部设备。
- **`[hub].host` 必须是某个 device key**，否则：`[X] [hub].host = 'xxx' 不在设备表里`（退出码 1）。
  `local` 模式下 `host` 只决定 hub 文件落在哪台的 workspace 下，谁都读得到。
- `[general].me` 必须是某个 device key，否则报「本机设备 'xxx' 不在设备表里」。
- `[loop].interval_seconds` 下限 **5** 秒：写 `1` 会直接 `[X] [loop].interval_seconds 要 ≥ 5，拿到 1`
  （退出码 2），根本走不到「向上取整」那一步。

三台真机时：每台一份 `bridge.toml`，内容可以**完全一样**，只靠 `[general].me`（或主机名自动识别）区分本机。

---

## 安装

```sh
# Windows
pwsh -File install\install.ps1 -WhatIf     # 先空跑，看清将做什么
pwsh -File install\install.ps1             # 建计划任务
pwsh -File install\install.ps1 -Uninstall  # 卸载（只删计划任务）

# Linux / macOS / Termux
sh install/install.sh --dry-run            # 先空跑
sh install/install.sh                      # 有 systemd user 写 unit，没有写启动脚本
sh install/install.sh --uninstall          # 卸载（只删 unit / 启动脚本）
```

Windows 上另有 `-ConfigPath`（默认 `.\bridge.toml`）。多份配置时就靠它指定装哪一份：

```powershell
pwsh -File install\install.ps1 -WhatIf -ConfigPath "D:\demo\work\bridgeA.toml"
```

两个脚本都**只在本机动手**：不写远端文件、不改对方设备的计划任务、不碰 ssh 配置。
装之前会先读一遍 `bridge.toml`（借包自己的加载器），配置读不通就直接停，不会装半截。
计划任务的 `-WorkingDirectory` 已由脚本写好，所以它不受「cwd 必须是包目录」那条约束。

---

## 能读我们的老数据

这包不是新格式，是**同一套格式的可安装版本**：

| 老东西 | 新包 |
|---|---|
| `hub.jsonl` 的行 `{seq, from, at, text, via?}` | 原样读，`via` 仍可选 |
| 收件箱命名 `知会_给X_主题_日期.md` | 原样认，`发给`/`给`/`回` 前缀照旧剥离 |
| 游标文件里只有一个整数 | 认；下次写入自动升级成 `{"seq","line"}` |
| `hub.bad.jsonl` 的 `{line_no, at, raw_b64}` | 同一个形状，按行号去重 |
| `.beat` 里是 epoch 浮点串 | 同一个形状 |
| `.posted.json` = `{文件名: md5}` | 同一个形状 |

文件名契约本身（正则、三种拒绝、生效日期）的正本在 `docs/02_命名与落点.md` §二。

切过去**不需要迁移数据**，把 `bridge.toml` 里的路径指到现有目录就行。
⚠️ 但别让老守护和新守护同时对同一个发件箱跑 —— 两套锁互不认识。

---

## 已知边界（v0.1 没做 / 没验证的）

- **`bridge.receive` 的 `posix-remote` 后端没有实现**：它是个接口占位，配了会明确报错。
  手机/平板那边的投放器形态各不同，这一版不去猜。
- **只验证过 `transport = "local"`**（两个临时目录）；`ssh` 多机模式**没有在真机上跑过**，
  只有代码路径与实机经验支撑。第一次用它请先 `--dry-run` 再 `--once`。
- **没有并发写中心的仲裁**：多台同时 `--say` 可能撞 seq（撞号不致命，按行号仍能读到）。
  真要严格递增，就固定由一台写中心。
- **归档是重写共享文件**：只能在中心那台、且别台没在写的时候跑。远端中心会拒绝执行。
- **没有加密与鉴权**：走的是 ssh 通道，安全边界等于你的 ssh 配置。
- `[loop].interval_seconds` 下限 **5 秒**（小于 5 加载就被拒，退出码 2）；
  Windows 计划任务最小粒度 1 分钟，所以小于 60 秒的值在装计划任务时会被**向上取整到 1 分钟**。
- 🔴 **没有后台「发现层」**：`bridge.receive` 是**按命令归位**（跑一次分一次），不会自己盯着目录发现新件。
  要无人值守收件，得靠自己挂 `bridge.loop` 或 cron 定时调 `receive` —— **不会自动弹通知**。
- 🔴 **`install.sh` / `install.ps1` 不装 sshd**：接收那一台必须**事先**有可用的 sshd（并能被对方 ssh 到）。
  proot 环境下 sshd 重启后会没、`/run/sshd` 缺失会静默退出 —— 见 `docs/04_安卓与proot.md`。

---

## 目录

```
bridge/__init__.py    入口与版本
bridge/config.py      读 bridge.toml；设备表/路径解析；Host（本地直读 / ssh 远端）
bridge/chat.py        聊天室：say / read / tail / status / where / 归档 / 坏行 / .beat
bridge/post.py        投递：once / status / send / dry-run / self-test；文件名契约；md5 判重；copy2 归档
bridge/receive.py     收件归位：按收件方分格；后端接口（local 已实现）
bridge/loop.py        守护：可配间隔、单实例锁、pid、状态 json
install/install.ps1   Windows：计划任务 + ssh 别名探活（支持 -WhatIf / -ConfigPath）
install/install.sh    POSIX：systemd user unit 或 setsid nohup 启动脚本（支持 --dry-run）
docs/01_三台互信怎么配.md   ssh 别名、密钥、指纹、排错顺序
docs/02_命名与落点.md       文件名契约、目录结构、hub 行格式、游标、归档
docs/03_故障排查.md         按症状查：不投 / 重复投 / 看不到消息 / 心跳过期 / 锁 / 乱码
docs/04_安卓与proot.md      Android/proot 额外坑：无 systemd、/run/sshd、ps 不可信、inotify、sshd 横幅
LICENSE                MIT
tests/test_chat.py    聊天室真跑测试（两个临时目录，不起 ssh）
tests/test_post.py    投递 + 收件归位真跑测试
bridge.toml.example   配置模板（里面的路径全是示例，照抄跑不通）
examples/minimal/     **一条命令证明它能跑**：建两个临时目录当两台设备，跑完自清理
                     `python examples\minimal\setup.py` → PASS 4/4
```

> ⚠️ **`install/install.ps1` 在 git 里存的是 LF、检出后变 CRLF**（`.gitattributes` 写了
> `*.ps1 text eol=crlf`），并且带 UTF-8 BOM —— 前者是给 Windows PowerShell 5.1 用的，
> BOM 是因为 5.1 没有 BOM 就按 GBK 解、中文注释会把脚本顶坏。
> **拿它跟别人 diff 时看到整文件一片红，那不是改动，是换行符。**
> 同理 `.py` / `.md` 一律 LF，别在 Windows 上顺手 CRLF 化再去提交。

---

## 推仓前必做（脱敏）

用任何 secret scanner（gitleaks / trufflehog）都行，但要按你**自己的**禁词表配：
工作区前缀、内网网段、真实姓名、主机名/机型词、会话 UUID 前缀。

⛔ **顺序不能反：先脱敏 → 再建仓 → 最后 push。** 仓一旦建了，Git 历史洗不干净。
⛔ 文档里别把禁词当例子抄进来 —— 讲脱敏的那一节最容易变成唯一的告警源。

---

## 许可

MIT，见 `LICENSE`。保留版权与许可声明即可商用、修改、再分发。

⛔ **贡献须知**：不许从 AGPL 项目（例如安卓端 `dsh-Go`）抄代码进来 —— 那会把整个仓拖进 AGPL。
互通是允许的（读同一套文件格式），抄代码不行。
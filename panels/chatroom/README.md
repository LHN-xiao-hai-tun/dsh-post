# 聊天室面板 · local-chatroom（0.2.2）

给 **dsh-go App** 用的原生面板插件：像群聊一样看最近 24 条消息，并能在 App 里直接发一句。

它和本仓的命令行工具（`python -m bridge.chat`）**读写同一份 hub.jsonl** ——
不是第二套数据。它**不会替你改任何设备设置**，只在 App 里显示与发送。

```
lib/index.js   宿主半边：面板（view/data/actions）+ 输入框胶囊（send-draft）
lib/parse.js   纯函数：parseHub / dedupeRows（去重键 seq|from|at，⛔ 不是 seq 单列）
test/          dedupe.test.mjs（33 条断言）+ fixtures/hub.sample.jsonl（公开样本）
cordis.patch.yml  组合插入行
```

## 面板里有什么

- **看**：最近 24 条（先去重 → 再切片），新的在上；列表最上面一行说「还有 N 条更早的」；
  底部说「共 N 条 / 从哪台拉的 / 坏行数」。每 10 秒自动刷一次（面板关掉就停）。
- **说**：面板顶部输入框 + 「发送」；输入框上方的胶囊「发到聊天室」把已经打好的字直接发走（不发给 AI）。
- **不推进已读游标**：读消息不改任何状态。

## 安装（dsh-go 原生插件）

这不是 Cordis bundle，装法是 dsh-go 那一套：

```sh
P="${DSH_HOME:-$HOME/.dsh}/dsh-go/plugins/local-chatroom"
mkdir -p "$(dirname "$P")" && cp -r . "$P"
dsh plugin --profile web add "$P"
```

- 装的是**指向那个目录的链接**，装完别删目录。
- 改 `package.json` 会自动重读；改 `lib/` 里的代码要在 App 的「引擎」页**重启引擎**才生效。
- 卸载：`dsh plugin --profile web remove local-chatroom`，再删目录。

## 配置

优先级：**环境变量 > 组合里插件那一行的 `config:` > 默认值**。

| 字段 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `hub` | `DSH_CHATROOM_HUB` | 空 —— **必须配** | 电脑上 hub.jsonl 的路径（面板走 `ssh … cat '<路径>'` 去读）。没配时面板明说「未配置」，不会假装没有消息。 |
| `speaker` | `DSH_CHATROOM_SPEAKER` | `me` | 面板/胶囊发出去的消息记在这个名下。⛔ 不回退成设备名：回退会把「谁说的」悄悄改掉。 |
| `deviceNames` | —（只走 config） | `{}` → 显示设备名本身 | 设备名 → 显示名，例如 `{ "phone": "手机", "pc": "电脑", "tablet": "平板" }`。⛔ 默认值里没有任何具体人名。 |
| `workspace` | `DSH_CHATROOM_WORKSPACE` | 进程当前目录 | 本机工作区：`工具/聊天室.py` 和 `.dsh/state/我是谁.json` 都在这儿。 |
| `sshAliases` | `DSH_CHATROOM_SSH`（逗号分隔） | `["pc"]` | 电脑的 ssh 别名，按「出门优先」排；多个**并发**试，第一个拿到内容的赢（串行会撞面板 1 秒的首帧窗口）。 |

组合里配一遍（`config:` 段就是 `apply(ctx, config)` 的第二个参数）：

```yaml
- insert:
    - id: local-chatroom
      name: local-chatroom
      config:
        speaker: me
        hub: /path/on/the/pc/hub.jsonl
        sshAliases: [pc6, pc]
        deviceNames:
          phone: 手机
          pc: 电脑
          tablet: 平板
```

前置条件（两条，缺一个面板就打不开消息或发不出去）：

1. 面板所在那台机器能**免密** `ssh <别名>` 到电脑（代码里带 `BatchMode=yes`，
   不会弹密码提示）；否则面板只会显示「读不到聊天室」。
2. `<workspace>/工具/聊天室.py` 存在，并且支持 `--say-file <文件> --from <名字>`。
   这个脚本**不随本插件发布** —— 它就是本仓命令行工具那一份（`python -m bridge.chat`
   同样支持 `--say-file` / `--from`，见仓根 README「命令 → 聊天室」）。
   布局不一样就改 `lib/index.js` 里 `runChat()` 那一个相对路径，或者把 `workspace` 指过去。

## 测试

```sh
node test/dedupe.test.mjs
```

- 默认读仓库自带的样本 `test/fixtures/hub.sample.jsonl`（零第三方依赖，只读，副本用完就删）。
  样本里故意放了两组「两台设备撞同一个 seq」，用来守「去重键不能只用 seq」这条判据。
- 要扫真数据：`DSH_CHATROOM_HUB=/path/to/hub.jsonl node test/dedupe.test.mjs`。
- 第 5 段要 dsh-go-plugin 技能套件里的 `view-check`（和 App 同源的视图解析器）；
  没装时它报「未验证」并**算一条失败** —— 没验过的事不许当成验过。

## 已知边界

- **这份公开版没有在真机上装过**：0.2.2 的功能在设备上验证过；脱敏改动（说话人、显示名、
  路径改成配置项，默认值中性化）只在本机用 `node` 跑过上面那套测试，没有装进 App 实测。
- ssh 别名、hub 路径、工作区路径全是**你机器上的事**，仓库里只给占位示例。
- 面板只显示最近 24 条是**预算决定**的：面板 120 个组件上限，24 条气泡约 100 个，
  再多会被 App 静默截断。
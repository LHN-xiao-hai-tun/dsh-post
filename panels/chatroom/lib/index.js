// 聊天室（dsh-go 原生面板）—— 像群聊一样看各台设备的对话，**并且能在面板里直接发言**。
//
// 为什么要有「发言」：只能看不能说的面板，最后还是要切到电脑上去打字；
// 能在 App 里直接回一句，这个面板才算真的可用。
//
// 数据源：电脑上那个只追加的 hub.jsonl（路径由 `config.hub` 给，走 ssh 过去 cat）。
// 本机不维护第二份数据。
//
// ── 写这一半（0.2.0 新增）──────────────────────────────────────────────
// 抄的是电脑 Cordis 插件 `dsh-hub-status` 的**逻辑**，不是代码：
//   电脑：浏览器 + 右侧栏 + 同源 fetch 打 /dsh-hub-status/chat
//   平板：dsh-go 原生 App，没有右侧栏，也没有 fetch 那条路
// ⇒ 起子进程调 `工具/聊天室.py --say-file --from <说话人>`。
//   写 hub 的所有规矩（ssh stdin、schema 校验、回读比对、游标）都已经在那份正本里，
//   这里⛔ 不重写一遍 —— 抄一份就等于多一个会走样的地方。
//
// ⛔ 三条不抄错的（都是别人踩过的坑）：
//   ① 不用 `--say`，用 `--say-file` —— `--say` 会被 shell 先吃一层反斜杠/反引号/TAB，
//      正本现在直接拒收这种正文（hub 里已有 6 行因此整行报废、永久读不出来）
//   ② `from` 显式写说话人名（`config.speaker`），⛔ 不回退设备名。真栽过：不回退时
//      面板里人说的话被记成 `pc`，和机器自己代发的混在一起，「谁说的」这件事永久丢了。
//   ③ 读完不推进游标（⛔ 别替我把消息标成已读）。`--tail` 本来就不碰游标。
//
// ── 0.2.2 新增（只读侧两处，别的一律没动）──────────────────────────────
//   ① 行级去重：同一行被读两次（同 `seq` + 同 `from` + 同 `at`）折叠成一条。
//      key 是**三元组**，⛔ 不是 seq —— 真 hub 里有 5 组 seq 撞号（两台设备各发
//      一条、撞在同一个 seq），只按 seq 去重会**吞掉 5 条真消息**。
//      实现在 `./parse.js` 的 `dedupeRows()`，调用点在 `buildPanelData()`。
//   ② 截断提示：面板只显示最近 24 条 ⇒ 列表**最上面**加一行
//      「还有 N 条更早的（终端里 聊天室.py --read 看）」，N = `all.length - 24`，
//      当场算、⛔ 不写死；`hidden === 0` 时**不加**（正好 24 条加一句「还有 0 条」
//      是噪音，且用户会开始怀疑这个数字）。
//   两处都是在 `buildPanelData()` 里做的：**先去重 → 再切片 → 再加提示**。

// 五条只读侧的设计判据（都是 11-05/11-06 踩出来的）：
//   1. ⛔ 坏行不许静默跳过。跳过等于让那几条消息永久消失且无人知道 ——
//      所以面板底部必须写「有 N 行读不出来」。
//   2. ⛔ 不串行拉。`pc6`/`pc` 两个别名**并发**试，不是串行等第一个超时。
//   3. ⛔ 读不到和「没有消息」必须分开说。`--status` 那个假 0 的教训：
//      一个「读不到」的值不能和「读到了 0」用同一个输出格式。
//   4. ⛔ 不用 mtime 判「谁在线」。聊天室没有在线概念，只有「谁最后说话」。
//   5. ⛔ 不在 apply 里拉数据、不装全局定时器。只有面板打开时才拉 ——
//      平板和手机都是电池设备，后台白耗电最贵。
//
// 刷新：面板的 data() 每次被调用时自己排一个 10 秒的 setInterval，
// 返回的取消函数里清掉它。App 关闭面板就会调取消函数。
// （协议上限 dataHz = 4 次/秒，10 秒一次远低于它。）

import { execFile } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// hub 的解析抽到 `./parse.js` —— 曾经考虑过「面板写状态文件、闸去读它」那个方案，
// 被否掉的理由是：`data()` 是 App 调才跑的 ⇒ 没人开面板文件就不更新，只能回答
// 「上次打开时对不对」。抽成模块后，**面板和闸 import 同一份**，口径由代码保证。
// 判据：**一个判据如果依赖「有人在看」，它就不是判据，是快照。**
import { dedupeRows, parseHub } from './parse.js'

export const name = 'local-chatroom'

/**
 * 运行期设置：`apply(ctx, config)` 时合并一次（插件 config + 环境变量），
 * 之后 `say` / `fetchHub` / `myDevice` / `buildPanelData` 都读这份。
 *
 * 为什么可配置而不是常量：这个面板会被装到别人的机器上。
 * 写死一个人的名字、一台电脑的路径，装到别处要么跑不起来，要么把别人说的话
 * 记到别人头上 —— 而记录本身看起来完全正常，事后分不开。
 */
const DEFAULT_SETTINGS = {
  /** 面板/胶囊发出去的消息记在谁名下。⛔ 默认中性，绝不默认成某个人的名字。 */
  speaker: 'me',
  /** 设备名 → 显示名；没配就显示设备名本身。⛔ 默认不带人名、不带括号。 */
  deviceNames: {},
  /** 本机工作区目录：`工具/聊天室.py` 与 `.dsh/state/我是谁.json` 都在它下面。 */
  workspace: '',
  /** 中心 hub.jsonl 在电脑上的路径（POSIX 路径，走 ssh 过去 cat）。 */
  hub: '',
  /** 电脑的 ssh 别名，按「出门优先」排。多个**并发**试，第一个拿到内容的赢。 */
  sshAliases: ['pc'],
}

let settings = resolveSettings(null)

/**
 * 合并配置。优先级：环境变量 > 插件 config > 默认值。
 *
 * 环境变量是给「不方便改组合」的场合留的，也方便测试临时换一个人名：
 *   `DSH_CHATROOM_SPEAKER` · `DSH_CHATROOM_WORKSPACE` · `DSH_CHATROOM_HUB` · `DSH_CHATROOM_SSH`
 * （`DSH_CHATROOM_SSH` 用逗号分隔多个别名，例如 `pc6,pc`。）
 *
 * ⚠️ 测试要另起一个人名时，⛔ 别借真名去测。实测过这样的代价：借真名跑面板测试，
 * 落盘几条 `from=<真名>`、正文却是脚本写的 —— **「这个人说过这句话」被弄成假的了，
 * 事后分不开**。测试数据混进真实发言，代价不是「多几条噪音」，是「以后查历史时
 * 那几条不可信」。⇒ 测试一律用一眼看得出是测试的名字。
 */
function resolveSettings(config) {
  const cfg = config && typeof config === 'object' ? config : {}
  const env = process.env

  /** 设备名映射只收非空字符串；⛔ 别把 `undefined` 塞进去，那会显示成字符串 "undefined"。 */
  const deviceNames = {}
  if (cfg.deviceNames && typeof cfg.deviceNames === 'object') {
    for (const [k, v] of Object.entries(cfg.deviceNames)) {
      const name = String(v ?? '').trim()
      if (name !== '') deviceNames[String(k)] = name
    }
  }

  const envAliases = String(env.DSH_CHATROOM_SSH ?? '').split(',').map((s) => s.trim()).filter(Boolean)
  const cfgAliases = Array.isArray(cfg.sshAliases)
    ? cfg.sshAliases.map((s) => String(s ?? '').trim()).filter(Boolean)
    : []
  const aliases = envAliases.length > 0 ? envAliases : cfgAliases

  return {
    speaker: String(env.DSH_CHATROOM_SPEAKER || cfg.speaker || DEFAULT_SETTINGS.speaker).trim()
      || DEFAULT_SETTINGS.speaker,
    deviceNames,
    // 工作区默认取进程当前目录 —— dsh-go 引擎就是在工作区里起的；要别的目录就配一个。
    workspace: String(env.DSH_CHATROOM_WORKSPACE || cfg.workspace || process.cwd()).trim(),
    hub: String(env.DSH_CHATROOM_HUB || cfg.hub || '').trim(),
    sshAliases: aliases.length > 0 ? aliases : [...DEFAULT_SETTINGS.sshAliases],
  }
}

/**
 * 面板里显示最近多少条。
 *
 * ⚠️ 不是 50 —— 气泡比 list 项贵：每条是 `row > stack > text×2` 共 4 个节点，
 *    而面板预算 `nodes: 120` / `children: 32`（view-check.js 的 BUDGETS.panel）。
 *    24 条 = 4 + 24×4 = 100 个节点，加上输入框和按钮约 106，离 120 还有余量；
 *    再多会被 App 截断，截断是静默的（只加一条 note），
 *    用户会以为「聊天室只有这么点」。
 */
const SHOW_LAST = 24

/** 自动刷新间隔（毫秒）。10 秒一次 —— 自动刷太快，电池设备上最贵的是后台唤醒。 */
const REFRESH_MS = 10_000

/** 单次 ssh 的超时（毫秒）。面板数据 3 秒内要出，所以给 2.5 秒。 */
const SSH_TIMEOUT_MS = 2_500

/**
 * 面板/胶囊发出去的消息都记在 `settings.speaker` 名下 —— ⛔ 不回退成设备名。
 *
 * 为什么不用 `myDevice()`：这个入口是**人**用的，不是给 AI 用的。
 * AI 说话走的是 `工具/聊天室.py`，`from` 是设备名，两条路本来就分开。
 * 回退成设备名 = 悄悄换掉说话人，而消息本身看起来完全正常。
 * （默认值与取值顺序见上面 `DEFAULT_SETTINGS` / `resolveSettings`。）
 *
 * 显示名同理：`settings.deviceNames[设备名]`，没配就显示设备名本身。
 * ⛔ 默认值里不要放任何具体人名 —— 默认值会被装到别人机器上。

// ── 「发两遍」的两道闸 ─────────────────────────────────────────────────
//
// 现象：在面板里按了「发到聊天室」，以为没发出去，又按了一次 ⇒
//       hub 里两条正文一模一样、相隔 5 秒。
//
// 第一道：**清空要和「按下」同一瞬间发生**。原来 `patch:{draft:''}` 写在
//   延后结果里，实测 `--say-file` 一次要 2.6 秒（真跑 `real 0m2.648s`）——
//   这 2.6 秒里字还在输入框里，人凭「框空了没有」判断发没发出去。
//   判据：**把清空放在响应之后，等于把网络延迟变成「看起来没发」。**
//
// 第二道：**同一正文 60 秒内不重发**。前一道防不住「第一次已经成功返回了，
//   用户还是又点了一下」，而手抖点两下 + 第一次已返回，正好落在这个缝里。
//   代价：真想连发两条一样的要等 60 秒 —— 几乎为零。

/** 去重窗口（毫秒）。 */
const DEDUPE_MS = 60_000

/** 上一次**成功**发出去的正文。⛔ 失败不记 —— 失败后马上重试是正常操作。 */
let lastSent = { text: '', at: 0, seq: '' }

/**
 * 同一正文还在冷却窗口内 → `{left, seq}`；否则 null。
 *
 * ⚠️ `now` 参数只为了**能测**（`test/dedupe.test.mjs` 不用真等 60 秒）。
 *    生产调用点一个都不传 ⇒ 行为与本参数引入前完全一样。
 * ⚠️ 窗口仍是 **60 秒**（`DEDUPE_MS`）—— 0.2.2 没动它，⛔ 别顺手改。
 */
export function dedupeHit(text, now = Date.now()) {
  if (lastSent.text !== text) return null
  const left = DEDUPE_MS - (now - lastSent.at)
  if (left <= 0) return null
  return { left: Math.ceil(left / 1000), seq: lastSent.seq }
}

/**
 * ⚠️ 只给 `test/dedupe.test.mjs` 用：把 `lastSent` 摆到「N 毫秒前发过」。
 *
 * 为什么需要它：`dedupeHit` 读的是模块级 `lastSent`，而它只由**真发出去**才会写。
 * 测试里为了验「30 秒内拦、超时放」去真跑一次 `--say-file` 是不可能的
 * （会往真 hub 写一条测试消息 —— 0.2.1 的注释里记着这个代价：测试数据混进
 * 真实发言，事后分不开谁说的）。所以开一个**只改内存状态**的口子。
 *
 * ⛔ 生产代码不 import 它；它是测试夹具，不是 API。
 */
export function _setLastSentForTest(v) {
  lastSent = { text: String(v?.text ?? ''), at: Number(v?.at ?? 0), seq: String(v?.seq ?? '') }
}

// 面板打开时才有值。App 关闭面板会调 data() 返回的取消函数，那里清掉。
let panelEmit = null
let panelTick = null
let panelSnapshot = null

/**
 * 插件**自己最后一次**写进输入框的值。
 *
 * ⚠️ 不是用户正在打的字 —— 插件看不到那个。它唯一的用处是：
 *    让每次 emit 的 `draft` 在插件没主动改它时保持同一个值，
 *    这样桥接算出的差量里就没有 `draft`（`mergeDiff` 只报变化），
 *    10 秒一次的刷新才不会把用户正在打的字冲掉。
 *    ⛔ 别在这里写 `draft: ''` 了事：失败回填之后那一帧会让下一个 tick
 *    把回填的字又清掉。
 */
let emittedDraft = ''

/** 往面板推一小块（不重拉 ssh）。面板没开就什么都不做。 */
function pushPatch(patch) {
  if (panelEmit === null || panelSnapshot === null) return
  panelSnapshot = { ...panelSnapshot, ...patch }
  panelEmit(panelSnapshot)
}

/**
 * 真去发，并自己把结果推回面板。
 *
 * 为什么不在 handler 里 await：handler 必须**同步**返回 `patch:{draft:''}`
 * 才能立刻清空，而 `pending` 旁边写别的键会被丢弃（`splitPending` 的
 * `ignored next to pending`）⇒ 「立刻清空」和「延后拿结果」不能同时用。
 * 所以清空走同步返回，结果走这条后台路径 + `data()` 的 emit。
 */
async function deliver(text, zh) {
  pushPatch({ head: zh ? '正在发到聊天室…' : 'Sending…' })
  const r = await say(text)
  if (!r.ok) {
    // ⛔ 失败时把正文放回输入框 —— 清空了正文就没了，
    //    而「没发出去」正是最需要原文还在的时候。
    //    冷却也要撤掉：失败之后马上重试是正常操作，不该被自己的去重挡住。
    if (lastSent.text === text) lastSent = { text: '', at: 0, seq: '' }
    emittedDraft = text
    pushPatch({
      head: (zh ? '没发出去：' : 'Failed: ') + String(r.why ?? '').slice(0, 40),
      draft: emittedDraft,
    })
    return
  }
  lastSent = { text, at: Date.now(), seq: String(r.seq) }
  pushPatch({ head: zh ? `已发到聊天室 #${r.seq}` : `Sent #${r.seq}` })
  // 发完立刻重拉一次，让新消息当场出现（而不是等 10 秒）
  if (panelTick !== null) void panelTick()
}

/** 跑一次 聊天室.py，返回 `{ok, out, err}`。⛔ 不抛 —— 失败也要有人话说出来。 */
function runChat(args, timeoutMs = 25_000) {
  return new Promise((resolve) => {
    execFile(
      'python3',
      [join(settings.workspace, '工具', '聊天室.py'), ...args],
      { cwd: settings.workspace, timeout: timeoutMs, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 },
      (error, stdout, stderr) => {
        if (error) {
          return resolve({ ok: false, out: String(stdout ?? ''), err: String(stderr ?? error.message) })
        }
        resolve({ ok: true, out: String(stdout ?? ''), err: String(stderr ?? '') })
      },
    )
  })
}

/** 取最后一行非空输出 —— 正本失败时错误在最后一行，前面是回溯/预警。 */
function lastLine(s) {
  const lines = String(s ?? '').split('\n').map((l) => l.trim()).filter(Boolean)
  return lines.length > 0 ? lines[lines.length - 1] : ''
}

/**
 * 发一条。⛔ 正文不经 shell、不经 argv —— 先落临时文件再 `--say-file`。
 *
 * 临时文件名带 pid：两个面板同时发不会互相覆盖（正本自己也有 `.tmp` + rename）。
 */
async function say(text) {
  const body = String(text ?? '')
  if (body.trim() === '') return { ok: false, why: '正文是空的' }
  const tmp = join(tmpdir(), `dsh-chatroom-say-${process.pid}.txt`)
  try {
    writeFileSync(tmp, body, 'utf8')
  } catch (e) {
    return { ok: false, why: `写临时文件失败：${e.message}` }
  }
  // 🔴 `--from <说话人>` 必须给。正本 `cmd_say(text, src, original, sender)` 里
  //    `dev = my_device()`（游标用）和 `who = sender or dev`（落盘 from 用）
  //    是**两个变量** —— 给了 `--from` 只改 from，不改游标。
  const r = await runChat(['--say-file', tmp, '--from', settings.speaker])
  if (!r.ok) return { ok: false, why: lastLine(r.err || r.out) || '没有输出' }
  const m = /#(\d+)/.exec(r.out)
  return {
    ok: true,
    seq: m ? m[1] : '?',
    verified: r.out.includes('回读一致'),
    detail: r.out.split('\n').filter((l) => l.includes('回读')).join(' ').trim(),
  }
}

/**
 * 拉一次 hub 全文。返回 `{ ok, text, alias, err }`。
 *
 * ⚠️ 两个别名**并发**试：`Promise.any` 语义（第一个成功的赢）。
 *    串行试的话，`pc6` 不通时要先等满 2.5 秒才轮到 `pc`，
 *    而面板的首次数据只有 1 秒窗口（`firstDataWaitMs: 1000`）——
 *    串行必然超时，等于「在家 WiFi 下这个面板永远显示不出来」。
 */
function fetchHub() {
  // ⛔ 没配 hub 路径时说人话，别让 `cat ''` 的错误原样糊到脸上。
  if (settings.hub === '') {
    return Promise.resolve({
      ok: false,
      errs: ['未配置 hub 路径（config.hub 或环境变量 DSH_CHATROOM_HUB）'],
    })
  }
  const tries = settings.sshAliases.map((alias) => new Promise((resolve) => {
    execFile(
      'ssh',
      ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=2', alias, `cat '${settings.hub}'`],
      { timeout: SSH_TIMEOUT_MS, maxBuffer: 8 * 1024 * 1024, encoding: 'utf8' },
      (err, stdout) => {
        if (err) return resolve({ ok: false, alias, err: String(err.message ?? err) })
        if (!stdout || stdout.trim() === '') {
          return resolve({ ok: false, alias, err: '电脑上那个文件是空的' })
        }
        resolve({ ok: true, alias, text: stdout })
      },
    )
  }))
  return new Promise((resolve) => {
    let pending = tries.length
    const errs = []
    let settled = false
    tries.forEach((p) => p.then((r) => {
      if (settled) return
      if (r.ok) { settled = true; resolve(r); return }
      errs.push(`${r.alias}: ${r.err}`)
      pending -= 1
      if (pending === 0) { settled = true; resolve({ ok: false, errs }) }
    }))
  })
}

/**
 * 本机是哪台 —— 用来决定气泡靠左还是靠右。
 *
 * 来源是 `<workspace>/.dsh/state/我是谁.json` 的 `device`（这是本机唯一可信的自述：
 * `hostname` 三台设备全都叫 localhost、工作区路径三台也一样，都不能用来区分）。
 * 读不到就返回空串 —— ⛔ 不猜、⛔ 不默认成 phone：猜错的话
 * 「别人的话」会被画到自己那一边，而那个错误在界面上看起来完全正常。
 */
function myDevice() {
  try {
    const raw = readFileSync(join(settings.workspace, '.dsh', 'state', '我是谁.json'), 'utf8')
    return String(JSON.parse(raw).device ?? '').trim()
  } catch {
    return ''
  }
}

/**
 * 一行消息压成一个气泡。
 *
 * 形状（`row > stack > text×2`）：
 *   row.style.align = 'start' | 'end'   ← 别人靠左，自己靠右
 *   stack.style.card = 'tinted' + background = 'blue' | 'green'
 *   第一行 text：`size: 'caption'` + `tone: 'muted'` 放「名字 · 时间」
 *   第二行 text：正文
 *
 * ⚠️ 正文只截前 240 字。面板预算 `text: 2000`，但一条 2000 字的消息
 *    会把整个面板顶掉 —— 聊天室是「扫一眼」的地方，要看全文去 `--tail`。
 */
function toBubble(r, me, names = {}) {
  // ⚠️ `me` 是设备名（'phone'/'pc'/'tablet'），**不是布尔** ——
  //    第一版直接写 `mine ? 'end' : 'start'`，而 `myDevice()` 返回非空字符串
  //    恒为真 ⇒ 所有气泡都靠右，界面上看起来完全正常（只是每句都是「我说的」）。
  //    判据：一个变量叫 `mine` 但它装的是「我是谁」，不是「这是不是我的」。
  const mine = me !== '' && r.from === me
  // 显示名：配了映射就用映射，没配就显示设备名本身（⛔ 不内置任何具体人名）。
  const who = names[r.from] ?? r.from
  const body = r.text.replace(/\s+/g, ' ').trim()
  const shown = body.length > 240 ? `${body.slice(0, 240)}…` : body
  return {
    type: 'row',
    style: { align: mine ? 'end' : 'start' },
    children: [
      {
        type: 'stack',
        style: { card: 'tinted', padding: 'S', background: mine ? 'green' : 'blue' },
        children: [
          {
            type: 'text',
            text: `${who} · ${r.at.slice(11, 16)}`,
            tone: 'muted',
            style: { size: 'caption' },
          },
          { type: 'text', text: shown === '' ? '（空消息）' : shown },
        ],
      },
    ],
  }
}

/**
 * 列表被截断时**必须说出来**，⛔ 不许静默。
 *
 * 0.2.1 只说「共 800 条 · 显示最近 24 条」就完了：更早那 790 条既看不到、
 * 也不知道去哪看。这一行就是那个出口（`--read` 是终端里的读法）。
 *
 * ⚠️ `hidden` 必须**当场算**（`all.length - shown.length`），⛔ 不写死、⛔ 不写「约」：
 *    写成固定数字之后，hub 一长就会对不上，而对不上时它看起来完全正常。
 * ⚠️ 只有 `hidden > 0` 才加 —— 正好 24 条时加一句「还有 0 条」纯属噪音。
 *
 * 预算：24 气泡 = 4(容器) + 24×4 = 100 个节点，这一行只 +1（一个 text），
 * 合计 101 ≤ `nodes: 120`。⇒ `SHOW_LAST` 保持 24 不动 —— ⛔ 别为了塞提示去改它，
 * 改它才是真会踩到静默截断的那条线。
 */
function truncationNote(hidden, zh) {
  return {
    type: 'text',
    tone: 'muted',
    style: { size: 'caption' },
    text: zh
      ? `还有 ${hidden} 条更早的（终端里 聊天室.py --read 看）`
      : `${hidden} older message(s) not shown — run 聊天室.py --read in a terminal`,
  }
}

/**
 * 把解析出来的行拼成面板的 data（head / bubbles / foot）。
 *
 * 抽成纯函数是为了能拿真数据直接跑（`test/dedupe.test.mjs` 调它）——
 * 里面没有 ssh、没有定时器、没有可变状态。
 *
 * 顺序是 **先去重 → 再切片 → 再 reverse**（新的在上），⛔ 不许调换：
 * 去重放在切片后面，重复行会把 24 条的窗口整段占满 —— 看着 24 条，其实只有一半内容。
 */
export function buildPanelData({ rows, bad, zh, me, alias, spent, names = {} }) {
  const all = dedupeRows(rows)
  const shown = all.slice(-SHOW_LAST)
  const hidden = all.length - shown.length
  const lastAt = all.length > 0 ? all[all.length - 1].at : ''
  const badNote = bad.length > 0
    ? (zh
      ? ` · ⚠️ ${bad.length} 行读不出来（第 ${bad.slice(0, 3).map((b) => b.line).join('/')}${bad.length > 3 ? '…' : ''} 行）`
      : ` · ⚠️ ${bad.length} unreadable line(s)`)
    : ''

  const bubbles = shown.map((r) => toBubble(r, me, names)).reverse() // 新的在上
  // 提示行放在 bubbles 第 0 位 = 面板最上面（列表开头）。气泡区是「新的在上」，
  // 所以它说的「更早的」正好是列表末尾之下那一段 —— 位置和语义对得上。
  if (hidden > 0) bubbles.unshift(truncationNote(hidden, zh))

  return {
    head: zh
      ? `共 ${all.length} 条 · 显示最近 ${shown.length} 条${lastAt ? ` · 最后 ${lastAt.slice(11, 16)}` : ''}`
      : `${all.length} message(s) · showing last ${shown.length}`,
    bubbles,
    foot: zh
      ? `从 ${alias} 拉的 · ${spent} 秒${badNote}`
      : `via ${alias} · ${spent}s${badNote}`,
  }
}

/**
 * 从表单提交里把正文捞出来。
 *
 * ⚠️ 我**没有**在真机 App 上验过 form submit 的 `event.value` 到底长什么样，
 *    所以这里接受三种形状，取到就用：
 *      ① 对象且带 `draft`（bind 的路径是 `data.draft` ⇒ 最可能是这个）
 *      ② 对象且带 `text`（有人把 bind 写成 data.text）
 *      ③ 直接是字符串
 *    取不到就返回空串 —— 调用方会提示「正文是空的」，⛔ 不会静默发一条空消息。
 *    判据：**对没验过的输入形状，宁可多认几种，也别赌一种。**
 */
function pickText(value) {
  if (typeof value === 'string') return value
  if (value && typeof value === 'object') {
    for (const k of ['draft', 'text', 'body', 'content']) {
      if (typeof value[k] === 'string') return value[k]
    }
  }
  return ''
}

/**
 * 插件入口。
 *
 * `config` 是组合里这个插件那一行的 `config:` 段（Cordis 惯例把配置作为 apply 的第二参）。
 * 全给默认值也能跑：说话人 'me'、设备名就是设备名、hub 路径要自己配。
 */
export function apply(ctx, config) {
  settings = resolveSettings(config)
  ctx.inject(['dshGo'], (ctx) => {
    // ① 胶囊：把输入框里已经打好的字发到聊天室（⛔ 不发给 AI）。
    //    这条路和下面面板里的输入框**同时存在**是有意的 ——
    //    面板里打字要先把面板打开；正在对话时胶囊少一步。
    ctx.dshGo.on('send-draft', (scope, event) => {
      const zh = !String(event.locale).startsWith('en')
      const text = String(scope.draft ?? '')
      if (text.trim() === '') {
        return { toast: zh ? '输入框是空的 —— 先打字，再点这个' : 'Type something first' }
      }
      const dup = dedupeHit(text)
      if (dup !== null) {
        return {
          toast: zh
            ? `这条刚发过（#${dup.seq}，${dup.left} 秒内不重复发）`
            : `Already sent (#${dup.seq}), ${dup.left}s cooldown`,
        }
      }
      // ⚠️ 这条路**清不掉输入框**，是平台的硬限制，不是没做：
      //    效果里只有 `insert`（往输入框塞字，`''` 会被校验拒成
      //    `insert.text must be 1-2000 characters`）和 `patch`（只对本插件
      //    自己的面板有效）。胶囊拿不到「清空会话输入框」这个动作。
      //    ⇒ 唯一的办法是**别让它看起来没发**：按下就挂 pending、结果用 toast，
      //      外加 60 秒去重。⛔ 别假装这里修好了。
      lastSent = { text, at: Date.now(), seq: '发送中' }
      return {
        pending: { label: zh ? '正在发到聊天室' : 'Sending', timeoutMs: 60_000 },
        result: async () => {
          const r = await say(text)
          if (!r.ok) {
            if (lastSent.text === text) lastSent = { text: '', at: 0, seq: '' }
            return { toast: (zh ? '没发出去：' : 'Failed: ') + String(r.why ?? '').slice(0, 30) }
          }
          lastSent = { text, at: Date.now(), seq: String(r.seq) }
          // ⚠️ 延后结果里只能用 insert / copy / toast / patch ——
          //    写 `refresh: true` 会被检查器当场报
          //    「refresh is not allowed in a deferred result」。
          return {
            toast: (zh ? `已发到聊天室 #${r.seq}` : `Sent #${r.seq}`)
              + (r.verified ? '' : (zh ? '（未回读校验）' : ' (unverified)')),
          }
        },
      }
    })

    // ② 面板：输入框 + 最近 N 条
    ctx.dshGo.panel('chatroom', {
      view: (scope, caps) => {
        const zh = !String(caps?.locale ?? 'zh').startsWith('en')
        return {
          v: 2,
          type: 'stack',
          title: '聊天室',
          children: [
            { type: 'text', text: { $: 'data.head' }, tone: 'muted' },
            // ⚠️ 输入框放在**最上面**：面板是竖着滚的，放底部等于每次都要先滑到底。
            {
              type: 'form',
              submit: 'say',
              submitLabel: zh ? '发送' : 'Send',
              children: [
                {
                  type: 'textField',
                  label: zh ? '说点什么' : 'Say something',
                  bind: 'data.draft',
                  multiline: true,
                  maxLines: 3,
                  maxLength: 2000,
                  placeholder: zh ? '发到三台聊天室，不发给 AI' : 'Send to the 3-machine hub, not to the AI',
                },
              ],
            },
            { type: 'stack', style: { gap: 'S' }, children: { $: 'data.bubbles' } },
            { type: 'text', text: { $: 'data.foot' }, tone: 'muted' },
            { type: 'actions', items: [{ label: zh ? '刷新' : 'Refresh', action: 'reload', icon: 'refresh' }] },
          ],
        }
      },

      data: (scope, emit, caps) => {
        const zh = !String(caps?.locale ?? 'zh').startsWith('en')
        // 自己是谁：决定气泡靠左/靠右。读不到就是空串 ⇒ 全部靠左（⛔ 不猜）
        const me = myDevice()
        let stopped = false
        let timer = null
        // ⚠️ 第一帧就把 draft 发出去：`bind` 指向的字段不在数据里时，
        //    输入框可能显示成「—」。发一个空串是最便宜的保护。
        //
        // ⚠️ 之后每一帧都发**同一个** `emittedDraft`，⛔ 别写死 `draft: ''`：
        //    桥接是「推完整对象、算差量」，前后两帧 draft 一样 ⇒ 差量里没有 draft
        //    ⇒ 用户正在打的字不会被冲掉；写死 '' 的话，失败回填那一帧之后的
        //    下一个 tick 会把回填的字又清掉（实测 `mergeDiff` 会给出 `{"draft":""}`）。
        panelEmit = emit
        panelSnapshot = { head: zh ? '读取中…' : 'Loading…', bubbles: [], foot: '', draft: emittedDraft }
        emit(panelSnapshot)

        const tick = async () => {
          if (stopped) return
          const t0 = Date.now()
          const got = await fetchHub()
          if (stopped) return

          if (!got.ok) {
            // ⛔ 「读不到」必须和「没有消息」分开说 —— 这是 --status 那个假 0 的教训
            panelSnapshot = {
              head: zh ? '⚠️ 读不到聊天室' : '⚠️ Cannot read the chatroom',
              bubbles: [],
              draft: emittedDraft,
              foot: zh
                ? `不是「没有消息」，是拉不到：${String(got.errs?.join(' · ') ?? got.err).slice(0, 120)}`
                : `Not empty — unreachable: ${String(got.errs?.join(' · ') ?? got.err).slice(0, 120)}`,
            }
            emit(panelSnapshot)
            return
          }

          const { rows, bad } = parseHub(got.text)
          const spent = ((Date.now() - t0) / 1000).toFixed(1)
          // 去重 / 切片 / 截断提示都在 buildPanelData 里 —— 那边有注释说明
          // key 为什么是 (seq, from, at)、提示行为什么只在 hidden > 0 时加。
          // ⚠️ draft 每帧都要带上（同一个 `emittedDraft`），否则桥接的差量里
          //    会出现 `{"draft":""}` 把用户正在打的字冲掉。
          panelSnapshot = {
            ...buildPanelData({ rows, bad, zh, me, alias: got.alias, spent, names: settings.deviceNames }),
            draft: emittedDraft,
          }
          emit(panelSnapshot)
        }

        panelTick = tick
        tick()
        timer = setInterval(tick, REFRESH_MS)

        // App 关闭面板时会调这个 —— 面板不可见就不刷，不需要插件自己判可见性
        return () => {
          stopped = true
          if (timer !== null) clearInterval(timer)
          // ⚠️ 面板关了就把引用清掉：留着的话 `pushPatch` 会往一个已经没人看的面板
          //    推数据（`emit` 内部会丢弃，但 `panelSnapshot` 会被写脏）。
          if (panelTick === tick) panelTick = null
          panelEmit = null
          panelSnapshot = null
        }
      },

      actions: {
        // 面板里的「刷新」。⛔ 用 refresh 而不是重新拉一次 ——
        //    重新拉是 data() 的事，action 里再拉一遍等于同一秒发两次 ssh。
        reload: () => ({ refresh: true }),

        // 面板里按「发送」。
        //
        // 🔴 三条时序（起因是「在面板里发了两遍」）：
        //   ① 输入框要在**这一帧**清空 ⇒ handler **同步**返回 `patch:{draft:''}`
        //   ② 结果（成功 / 失败回填）走后台 + `data()` 的 emit，⛔ 不用 pending
        //      （`pending` 旁边写别的键会被丢弃，见 `splitPending` 的
        //        `ignored next to pending`）
        //   ③ 同一正文 60 秒内再点 ⇒ 只提示、不发
        say: (scope, event) => {
          const zh = !String(event.locale).startsWith('en')
          const text = pickText(event.value)
          if (text.trim() === '') {
            return { toast: zh ? '先写点什么再发' : 'Type something first' }
          }
          const dup = dedupeHit(text)
          if (dup !== null) {
            return {
              toast: zh
                ? `这条刚发过（#${dup.seq}，${dup.left} 秒内不重复发）`
                : `Already sent (#${dup.seq}), ${dup.left}s cooldown`,
            }
          }
          // 先记下来再发：`deliver` 里成功才更新 `lastSent`，但这里要
          // 防止「同一帧连点两次」两个 handler 都通过检查（两次之间没有 await）。
          lastSent = { text, at: Date.now(), seq: '发送中' }
          emittedDraft = ''
          void deliver(text, zh)
          // 同步返回：清空输入框 + 一行提示。⛔ 不返回 pending。
          return {
            patch: { draft: '' },
            toast: zh ? '正在发…' : 'Sending…',
          }
        },
      },
    })
  })
}

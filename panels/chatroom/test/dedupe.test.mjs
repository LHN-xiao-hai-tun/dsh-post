// 聊天室面板 v0.2.2 的判据测试 —— **零第三方依赖**（只用 node 内置模块）。
//
// 跑法（在本目录）：node test/dedupe.test.mjs
// 退出码：全过 0，有一条不过 1。
//
// 数据从哪来：默认读仓库自带的样本 `test/fixtures/hub.sample.jsonl`；
//   设了 `DSH_CHATROOM_HUB` 就改读那份真数据（本机调试时用这个，⛔ 仓库里不写死任何人的路径）。
//   样本里**故意放了两组「两台设备撞同一个 seq」**，否则「key 不能只用 seq」这条判据
//   在公开仓里就没人守着了。
//
// ⛔ 只读：hub 只 copyFileSync 到系统临时目录再改，原文件一个字不动；
//    临时目录在 finally 里删掉。
//
// 测什么（对应任务书判据 2 的五条）：
//   1. hub：可解析 N 行 → 三元组去重后 N'（打印两个数字）；
//      顺带量「按 seq 去重会吞掉几条」，那是 key 选错的代价。
//   2. 人为复制末行 ⇒ 去重丢 1；同 seq 换 from ⇒ 两条都留。
//   3. 40 条 → 24 + 提示「还有 16 条更早的」；24 条整 → 24、无提示；10 条 → 10、无提示。
//   4. `dedupeHit` 的冷却窗口没被破坏（30 秒内拦；超过窗口放行）。
//   5. （额外）真视图模板过一遍 dsh-go-plugin 的 view-check（和 App 同源的解析器）；
//      找不到那个 kit 就明说「未验证」，⛔ 不假装跑过。

import { appendFileSync, copyFileSync, existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

// 真 hub 由环境变量给；没给就读仓库里的样本。⛔ 不把某台机器的路径写进公开仓。
const HUB = process.env.DSH_CHATROOM_HUB
  || fileURLToPath(new URL('./fixtures/hub.sample.jsonl', import.meta.url))
const SHOW_LAST = 24
let failures = 0
let passes = 0

function ok(name, cond, detail = '') {
  if (cond) {
    passes += 1
    console.log(`  ✔ ${name}${detail ? `  （${detail}）` : ''}`)
  } else {
    failures += 1
    console.log(`  ✘ ${name}${detail ? `  （${detail}）` : ''}`)
  }
}

function eq(name, actual, expected) {
  const same = actual === expected
  ok(name, same, same ? `=${JSON.stringify(actual)}` : `实际 ${JSON.stringify(actual)} ≠ 期望 ${JSON.stringify(expected)}`)
}

/** 把一个视图子树里所有 text 字符串抠出来（气泡正文在 children[0].children[1]）。 */
function texts(node, out = []) {
  if (Array.isArray(node)) { node.forEach((x) => texts(x, out)); return out }
  if (node && typeof node === 'object') {
    if (typeof node.text === 'string') out.push(node.text)
    for (const v of Object.values(node)) if (v && typeof v === 'object') texts(v, out)
  }
  return out
}

const { parseHub, dedupeRows } = await import(new URL('../lib/parse.js', import.meta.url))
const { apply, buildPanelData, dedupeHit, _setLastSentForTest } = await import(new URL('../lib/index.js', import.meta.url))

/** 造 n 条干净的行（seq 1..n，两个来源交替）。 */
function makeRows(n) {
  const out = []
  for (let i = 1; i <= n; i += 1) {
    out.push({
      seq: i,
      from: i % 2 === 0 ? 'phone' : 'pc',
      at: `2026-11-07 10:${String(i % 60).padStart(2, '0')}:00`,
      text: `测试消息 ${i}`,
    })
  }
  return out
}

const tmp = mkdtempSync(join(tmpdir(), 'chatroom-panel-v022-'))
try {
  // ── 1. hub（只读） ───────────────────────────────────────────────────
  console.log('\n[1] hub（只读）')
  console.log(`    ${HUB}`)
  const text = readFileSync(HUB, 'utf8')
  const { rows, bad } = parseHub(text)
  const deduped = dedupeRows(rows)
  console.log(`    可解析 N = ${rows.length} 行 · 三元组去重后 N' = ${deduped.length} 行 · 丢 ${rows.length - deduped.length} · 坏行 ${bad.length}`)
  if (bad.length > 0) console.log(`    坏行物理行号 ${bad.map((b) => b.line).join('/')}`)
  eq('hub：N\' = N − 0（没有完全重复的三元组）', deduped.length, rows.length)

  // 反证：key 只用 seq 会丢几条
  const seqSeen = new Set()
  const group = new Map()
  let seqDropped = 0
  for (const r of rows) {
    if (seqSeen.has(r.seq)) seqDropped += 1
    else seqSeen.add(r.seq)
    group.set(r.seq, (group.get(r.seq) ?? 0) + 1)
  }
  const collided = [...group.entries()].filter(([, n]) => n > 1)
  console.log(`    seq 撞号 ${collided.length} 组：${collided.map(([s, n]) => `${s}×${n}`).join(' ')}`)
  console.log(`    若按 seq 去重：${rows.length} → ${rows.length - seqDropped}，会吞掉 ${seqDropped} 条真消息`)
  ok('hub：按 seq 去重确实会丢消息（证明 key 不能只用 seq）', seqDropped > 0, `丢 ${seqDropped}`)
  eq('撞号组数 = 按 seq 去重会丢的条数', seqDropped, collided.length)

  // ── 2. 故意构造（临时副本） ───────────────────────────────────────────
  // ⚠️ 真 hub 是活文件（行数一直在涨），所以基准一律用
  //    「副本自己解析出来的行数」，⛔ 不拿开头读到的行数当基准。
  console.log('\n[2] 故意构造的副本（copyFileSync，原文件不动）')

  const copy1 = join(tmp, 'hub-dup.jsonl')
  copyFileSync(HUB, copy1)
  const base1 = parseHub(readFileSync(copy1, 'utf8'))
  const lines = readFileSync(copy1, 'utf8').split('\n')
  let lastLine = ''
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const ln = lines[i].trim()
    if (ln === '') continue
    try {
      const o = JSON.parse(ln)
      if (o !== null && typeof o === 'object') { lastLine = ln; break }
    } catch { /* 坏行，往上继续找 */ }
  }
  ok('找到最后一条可解析行用来复制', lastLine !== '')

  appendFileSync(copy1, `${lastLine}\n`, 'utf8')
  const p1 = parseHub(readFileSync(copy1, 'utf8'))
  eq('复制末行：解析条数 +1', p1.rows.length, base1.rows.length + 1)
  eq('复制末行：三元组去重丢 1（回到基准）', dedupeRows(p1.rows).length, base1.rows.length)

  const copy2 = join(tmp, 'hub-sameseq.jsonl')
  copyFileSync(HUB, copy2)
  const base2 = parseHub(readFileSync(copy2, 'utf8'))
  const changed = JSON.parse(lastLine)
  const origFrom = String(changed.from)
  changed.from = `${origFrom}-other`
  appendFileSync(copy2, `${JSON.stringify(changed)}\n`, 'utf8')
  const p2 = parseHub(readFileSync(copy2, 'utf8'))
  eq('同 seq 不同 from：两条都保留（撞号不许被吞）', dedupeRows(p2.rows).length, base2.rows.length + 1)

  // 纯函数层：先出现的留、顺序不变
  const fake = [
    { seq: 1, from: 'pc', at: '2026-11-07 10:00:00', text: '先出现的' },
    { seq: 1, from: 'pc', at: '2026-11-07 10:00:00', text: '后出现的重复' },
    { seq: 2, from: 'phone', at: '2026-11-07 10:01:00', text: '第三条' },
  ]
  const df = dedupeRows(fake)
  eq('去重保留「先出现」的那条', df[0].text, '先出现的')
  eq('去重后顺序不变', df.map((r) => r.seq).join(','), '1,2')

  // ── 3. 截断提示 ───────────────────────────────────────────────────────
  console.log('\n[3] 截断提示（buildPanelData）')
  const data = (n) => buildPanelData({
    rows: makeRows(n), bad: [], zh: true, me: 'pc', alias: 'pc', spent: '0.1',
  })

  const d40 = data(40)
  console.log(`    40 条 → 气泡 ${d40.bubbles.length} 项 · head「${d40.head}」`)
  eq('40 条：气泡数 = 25（24 条消息 + 1 行提示）', d40.bubbles.length, SHOW_LAST + 1)
  eq('40 条：提示行在列表最上面（bubbles[0]）', texts(d40.bubbles[0])[0],
    '还有 16 条更早的（终端里 聊天室.py --read 看）')
  ok('40 条：第二条是最后一条消息（新的在上）',
    texts(d40.bubbles[1]).some((t) => t === '测试消息 40'), texts(d40.bubbles[1]).join(' | '))
  ok('40 条：head 写的是去重后的总数', d40.head.includes('共 40 条') && d40.head.includes('显示最近 24 条'), d40.head)

  const d40en = buildPanelData({ rows: makeRows(40), bad: [], zh: false, me: 'pc', alias: 'pc', spent: '0.1' })
  ok('40 条（英文）：提示行也是英文', texts(d40en.bubbles[0])[0].includes('16 older message(s)'),
    texts(d40en.bubbles[0])[0])

  const d24 = data(24)
  console.log(`    24 条整 → 气泡 ${d24.bubbles.length} 项`)
  eq('24 条整：气泡数 = 24，不加提示', d24.bubbles.length, SHOW_LAST)
  ok('24 条整：不出现「还有 N 条」', !texts(d24.bubbles).some((t) => /还有 \d+ 条/.test(t)))

  const d10 = data(10)
  console.log(`    10 条 → 气泡 ${d10.bubbles.length} 项`)
  eq('10 条：气泡数 = 10，不加提示', d10.bubbles.length, 10)
  ok('10 条：不出现「还有 N 条」', !texts(d10.bubbles).some((t) => /还有 \d+ 条/.test(t)))

  // 顺序约束：**先去重再切片** —— 重复行不许占满窗口
  const dupRows = [...makeRows(30), { ...makeRows(30)[29] }]
  const dDup = buildPanelData({ rows: dupRows, bad: [], zh: true, me: 'pc', alias: 'pc', spent: '0.1' })
  eq('30 条 + 末行重复：去重后仍显示 24 条消息', dDup.bubbles.length, SHOW_LAST + 1)
  eq('30 条 + 末行重复：提示说还有 6 条（30−24，不是 31−24）', texts(dDup.bubbles[0])[0],
    '还有 6 条更早的（终端里 聊天室.py --read 看）')

  // ── 4. dedupeHit 冷却窗口 ─────────────────────────────────────────────
  console.log('\n[4] dedupeHit（同文冷却）')
  const T = 1_800_000_000_000
  _setLastSentForTest({ text: '同一句话', at: T - 30_000, seq: '#100' })
  const hit30 = dedupeHit('同一句话', T)
  ok('同文 30 秒前发过 → 拦截', hit30 !== null, hit30 ? `left=${hit30.left}s seq=${hit30.seq}` : 'null')
  eq('30 秒时剩余秒数 = 30（窗口 60 秒）', hit30?.left, 30)

  _setLastSentForTest({ text: '同一句话', at: T - 59_999, seq: '#100' })
  ok('窗口边上（59.999 秒）→ 仍拦截', dedupeHit('同一句话', T) !== null)

  _setLastSentForTest({ text: '同一句话', at: T - 60_000, seq: '#100' })
  eq('正好 60 秒 → 放行（left = 0 ⇒ null）', dedupeHit('同一句话', T), null)

  _setLastSentForTest({ text: '同一句话', at: T - 61_000, seq: '#100' })
  eq('61 秒前 → 放行', dedupeHit('同一句话', T), null)

  _setLastSentForTest({ text: '另一句', at: T - 1_000, seq: '#101' })
  eq('不同正文 → 不拦', dedupeHit('同一句话', T), null)

  _setLastSentForTest({ text: '', at: 0, seq: '' })
  eq('从没发过 → 不拦', dedupeHit('同一句话', T), null)

  // 真调用点：胶囊（send-draft）里确实走这个检查
  const reg = { panel: null, sendDraft: null }
  apply({
    inject: (_deps, fn) => fn({
      dshGo: {
        panel: (id, def) => { reg.panel = { id, def } },
        on: (ev, fn) => { if (ev === 'send-draft') reg.sendDraft = fn },
      },
    }),
  })
  ok('拿到 send-draft 处理器与面板定义（mock ctx）', reg.sendDraft !== null && reg.panel !== null)
  _setLastSentForTest({ text: '同一句话', at: Date.now() - 5_000, seq: '#102' })
  const blocked = reg.sendDraft({ draft: '同一句话' }, { locale: 'zh' })
  ok('胶囊：冷却中再点 → 只提示、不挂 pending', typeof blocked.toast === 'string'
    && blocked.toast.includes('这条刚发过') && blocked.pending === undefined, blocked.toast)
  _setLastSentForTest({ text: '同一句话', at: Date.now() - 61_000, seq: '#102' })
  const allowed = reg.sendDraft({ draft: '同一句话' }, { locale: 'zh' })
  ok('胶囊：超时后再点 → 正常进入发送（pending）', allowed.pending !== undefined, JSON.stringify(allowed).slice(0, 60))

  // ── 5. 真解析器（dsh-go-plugin kit 的 view-check） ────────────────────
  console.log('\n[5] 视图结构校验（view-check，BUDGETS.panel）')
  // 校验器来自 dsh-go-plugin 技能套件；路径按当前用户家目录推，⛔ 不写死某台机器的用户名。
  // 没装那个技能时本段报「未验证」（故意算失败 —— 没验过的事不许当成验过）。
  const KIT = process.env.DSH_GO_KIT
    || join(homedir(), '.dsh', 'skills', 'dsh-go-plugin', 'kit', 'lib', 'view-check.js')
  if (existsSync(KIT) && reg.panel !== null) {
    const { checkView } = await import(pathToFileURL(KIT).href)
    const view = reg.panel.def.view({}, { locale: 'zh' })
    const res = checkView(view, { budget: 'panel', data: { ...d40, draft: '' }, lang: 'zh' })
    console.log(`    ok=${res.ok} components=${res.components} placeholders=${res.placeholders} empty=${res.empty} truncated=${res.truncated} bytes=${res.bytes}`)
    if (res.issues.length > 0) console.log(`    issues：${JSON.stringify(res.issues)}`)
    ok('view-check：面板视图通过', res.ok === true)
    eq('view-check：没有东西被静默截断', res.truncated, 0)
    ok('view-check：节点数 components+placeholders ≤ 120',
      res.components + res.placeholders <= 120, String(res.components + res.placeholders))
  } else {
    console.log(`    （view-check 未找到：${KIT} —— 这一段「未验证」）`)
    ok('view-check 可用（否则本段未验证）', false, '找不到 kit')
  }
} finally {
  rmSync(tmp, { recursive: true, force: true })
}

console.log(`\n${failures === 0 ? 'PASS' : 'FAIL'}：${passes} 通过 / ${failures} 失败`)
process.exitCode = failures === 0 ? 0 : 1
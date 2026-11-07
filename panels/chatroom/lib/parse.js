// hub.jsonl 解析 —— 抽成独立模块，让**面板和外部校验脚本 import 同一份**。
//
// 为什么不做「面板顺手写一份状态文件、别处去读它」：
//
//   那个想法是：面板每轮把 `{total, shown, bad}` 写进一个状态文件，
//   外部脚本去读那个文件判「hub 此刻能不能读」。
//
//   但它站不住：`data()` 是 App 调才跑的 ⇒ **没有人打开面板 = 文件不更新**。
//   那个文件只能回答「上次有人打开面板时能不能读」，回答不了
//   「此刻能不能读」。这和「`--status` 报 0 条 = 没有消息」是同型的错误：
//   **一个值在它没被观测时，看起来和「正常」一样。**
//
//   ⇒ 无论每次都写还是只在变化时写，都救不了。根治办法是让
//     「怎么算一行能读」只存在一份，外部脚本自己拉 hub 自己解析，不需要面板在场。
//
// 判据：**一个判据如果依赖「有人在看」，它就不是判据，是快照。**
//
// ⚠️ 本文件只导纯函数（`parseHub` / `dedupeRows`），⛔ 不含任何 IO / ssh / 时间逻辑。
//    面板负责「从哪拉」，外部脚本负责「什么时候拉」，两边共用「怎么解析」。

/**
 * 逐行解析 hub。**坏行要数出来、也要点名**，⛔ 不许 `try { } catch { continue }`。
 *
 * 返回 `{ rows, bad }`：
 *   rows — `[{ seq, from, at, text }]`，按文件顺序（= seq 递增）
 *   bad  — `[{ line, why }]`，line 从 1 起（和 `grep -n` 对齐）
 */
export function parseHub(text) {
  const rows = []
  const bad = []
  const lines = String(text ?? '').split('\n')
  for (let i = 0; i < lines.length; i += 1) {
    const ln = lines[i]
    if (ln.trim() === '') continue
    try {
      const r = JSON.parse(ln)
      if (typeof r !== 'object' || r === null) throw new Error('不是对象')
      rows.push({
        seq: Number(r.seq) || 0,
        from: String(r.from ?? '?'),
        at: String(r.at ?? '?'),
        text: String(r.text ?? ''),
      })
    } catch (e) {
      // ⚠️ 这里**不能** continue 完事 —— 记下来，后面数给用户看
      bad.push({ line: i + 1, why: String(e.message ?? e).slice(0, 60) })
    }
  }
  return { rows, bad }
}

/**
 * 按 `(seq, from, at)` 三元组去重 —— ⛔ **不许按 `seq` 单列去重**。
 *
 * 2026-11-07 23:14 拿本机真 hub 量过（只读，`test/dedupe.test.mjs` 每次跑都重量）：
 *   854 个物理行 → 847 行可解析、6 行坏行；
 *   其中 **5 组 seq 撞号**（598 / 614 / 679 / 713 / 731），每组两条来自**不同设备**。
 *   它们是「两台设备各自发信、撞在同一个 seq」，**两条都是真消息**。
 *   - 按三元组去重：847 条 → 847 条，**丢 0**
 *   - 按 seq 去重：  847 条 → 842 条，**吞掉 5 条真消息**
 * ⇒ 按 seq 去重等于替人删消息，而且删掉的偏偏是撞号那一台说的整句话。
 *   （hub 是活文件，上面这些数字是快照 —— 判据是「丢 0」，不是这几个具体数字。）
 *
 * 另外两个约束：
 *   1. 保留**先出现**的那条（文件顺序 = 时间顺序），后面的才算重复；
 *   2. **必须在切片之前去重** —— 先切 24 条再去重的话，重复行会把整个窗口
 *      占满，用户看到 24 条其实只有 12 条内容。
 */
export function dedupeRows(rows) {
  const seen = new Set()
  const out = []
  for (const r of rows) {
    const key = `${r.seq}|${r.from}|${r.at}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(r)
  }
  return out
}

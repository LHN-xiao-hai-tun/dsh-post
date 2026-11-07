"""聊天室的真跑测试：两个临时目录模拟两台，⛔ 不起 ssh。

只覆盖聊天这一层：
  写一条 -> 按游标读回 / 坏行跳过不崩 / seq 撞号时行号去重 / say-file 上限 /
  心跳判死 / 归档后行号连续。

跑法（在仓根）：
    python tests/test_chat.py
退出码 0 = 全部通过；非 0 = 有断言挂了，且会列出是哪条。
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 测试在 Windows 控制台（代码页 936）里跑时，前半段 print 会抛 UnicodeEncodeError
# 或输出乱码 —— 与四个 CLI 入口保持一致，进程一起来就把 stdio 切到 UTF-8。
from bridge.config import enable_utf8_stdio  # noqa: E402

enable_utf8_stdio()

from bridge import chat  # noqa: E402
from bridge.config import ConfigError, load_config  # noqa: E402

PASSED = 0
TOTAL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, TOTAL
    TOTAL += 1
    if cond:
        PASSED += 1
        print(f"[OK] {name}")
    else:
        FAILS.append(f"{name}：{detail}" if detail else name)
        print(f"[X ] {name}{('：' + detail) if detail else ''}")


TOML = """\
[general]
root = "."
me = "a"
transport = "local"

[device.a]
name = "甲机"
workspace = "{a}"
inbox = "收件箱"
outbox = "发件箱"

[device.b]
name = "乙机"
workspace = "{b}"
inbox = "收件箱"
outbox = "发件箱"

[hub]
path = "聊天室/hub.jsonl"
host = ""
beat_deadline_seconds = 130

[post]
exts = [".md"]
verify = true

[recipients]
"大家" = "*"
"乙机" = ["b"]

[naming]
act_kinds = ["信", "回话", "同步", "知会"]
max_text_chars = 300
"""


def make_cfg(base: Path):
    a = base / "甲"
    b = base / "乙"
    for d in (a, b):
        (d / "收件箱").mkdir(parents=True)
        (d / "发件箱").mkdir(parents=True)
    p = base / "bridge.toml"
    p.write_text(TOML.format(a=a.as_posix(), b=b.as_posix()), encoding="utf-8")
    return load_config(p)


def run() -> int:
    with tempfile.TemporaryDirectory(prefix="dsh-post-chat-") as tmp:
        cfg = make_cfg(Path(tmp))
        hub = Path(str(cfg.hub.path))

        # ── ① 写一条 -> 按游标读回 ──
        p1 = chat.append_message(cfg, "第一条")
        p2 = chat.append_message(cfg, "第二条", via="笔记本")
        p3 = chat.append_message(cfg, "第三条")
        check("seq 从 1 起、逐条加一", [p1["seq"], p2["seq"], p3["seq"]] == [1, 2, 3],
              f"{[p1['seq'], p2['seq'], p3['seq']]}")
        check("落盘是 3 行", len(hub.read_text(encoding="utf-8").splitlines()) == 3)
        check("via 只在和 from 不同时写", ("via" in p1) is False and p2.get("via") == "笔记本")
        check("at 字段是统一口径", len(p1["at"]) == 19 and p1["at"][4] == "-", p1["at"])

        rows, bad, meta, err = chat.read_all(cfg)
        check("全量读回 3 条", len(rows) == 3 and not bad and not err, f"rows={len(rows)} bad={bad} err={err}")
        check("物理行号从 1 起", [r["_line"] for r in rows] == [1, 2, 3], str([r["_line"] for r in rows]))

        cur = chat.read_cursor(cfg)
        check("没有游标文件时读成 0", cur == {"seq": 0, "line": None}, str(cur))
        fresh = chat.new_rows(rows, cur)
        check("游标 0 时 3 条都是新的", len(fresh) == 3)

        nc = chat.cursor_after(fresh, cur)
        chat.write_cursor(cfg, nc["seq"], nc["line"])
        check("写下的游标带 seq 和行号", nc == {"seq": 3, "line": 3}, str(nc))
        check("再读一次没有新消息", chat.new_rows(rows, chat.read_cursor(cfg)) == [])

        # 老格式游标（纯整数）必须还能读
        chat.cursor_path(cfg, "a").write_text("2", encoding="utf-8")
        legacy = chat.read_cursor(cfg)
        check("老游标（纯整数）仍可读，line=None", legacy == {"seq": 2, "line": None}, str(legacy))
        check("老游标退回按 seq 比，只读到第 3 条",
              [r["seq"] for r in chat.new_rows(rows, legacy)] == [3])

        # ── ② 坏行跳过不崩 ──
        with hub.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write('{"seq": 99, "from": "b", "at": "2026-11-07 20:00:00"}\n')  # 行 4：能 json 解，但缺 text
            fh.write("{这不是 json\n")                                  # 行 5：解不出
            fh.write("\n")                                              # 行 6：空行，占号不算坏
        rows2, bad2, _m, err2 = chat.read_all(cfg)
        check("坏行不崩，正常行照读", len(rows2) == 3 and err2 == "", f"rows={len(rows2)} err={err2}")
        check("两条坏行被识别（行号 4、5）", [b[0] for b in bad2] == [4, 5], str(bad2))
        check("空行不算坏行", len(bad2) == 2, str(len(bad2)))

        added = chat.record_bad(cfg, bad2)
        bp = Path(str(chat.bad_path(cfg)))
        check("坏行原文被记下来", added == 2 and bp.is_file(), f"added={added}")
        saved = [json.loads(x) for x in bp.read_text(encoding="utf-8").splitlines()]
        check("坏行记录按行号去重（第二次不重复记）", chat.record_bad(cfg, bad2) == 0)
        check("坏行原文 base64 能解回原样",
              base64.b64decode(saved[1]["raw_b64"]).decode("utf-8") == "{这不是 json",
              base64.b64decode(saved[1]["raw_b64"]).decode("utf-8"))

        # ── ③ 坏行之后继续追加，序号接着走 ──
        p4 = chat.append_message(cfg, "坏行之后的第四条")
        check("坏行不影响 seq 递增", p4["seq"] == 4, f"seq={p4['seq']}")
        rows3, _b, _m, _e = chat.read_all(cfg)
        check("新消息落在第 7 行", rows3[-1]["_line"] == 7, str(rows3[-1]["_line"]))
        cursor_at_3 = {"seq": 3, "line": 3}
        check("按行号游标只取到第 7 行那条（不重复读 4/5/6 行的垃圾）",
              [r["seq"] for r in chat.new_rows(rows3, cursor_at_3)] == [4],
              str([r["seq"] for r in chat.new_rows(rows3, cursor_at_3)]))

        # ── ④ seq 撞号：行号仍能分开两条（这正是为什么要两个字段）──
        with hub.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"seq": 4, "from": "b", "at": "2026-11-07 20:00:00",
                                 "text": "撞号的另一条"}, ensure_ascii=False) + "\n")
        rows4, _b, _m, _e = chat.read_all(cfg)
        check("撞号后一共有 5 条消息", len(rows4) == 5, str(len(rows4)))
        after_line7 = {"seq": 4, "line": 7}
        got = chat.new_rows(rows4, after_line7)
        check("按行号能读到撞号的那一条（按 seq 比就会漏）",
              len(got) == 1 and got[0]["_line"] == 8, str([(r["seq"], r["_line"]) for r in got]))
        check("按 seq 比确实会漏（对照组）",
              [r for r in rows4 if r["seq"] > 4] == [])

        # ── ⑤ say-file 与长度上限 ──
        f = Path(tmp) / "长文.md"
        f.write_text("一整份文件的内容\n第二行\n", encoding="utf-8")
        ps = chat.say_file(cfg, f)
        check("say-file 把整份文件写成一条", ps["text"] == "一整份文件的内容\n第二行",
              repr(ps["text"]))
        try:
            chat.append_message(cfg, "x" * 301)
        except ConfigError as e:
            check("超过 max_text_chars 会被拒", "超过 300" in str(e), str(e))
        else:
            check("超过 max_text_chars 会被拒", False, "没报错")

        # ── ⑥ 心跳判死 ──
        check("没摸过心跳时读不出年龄", chat.beat_age(cfg)[0] is None)
        chat.touch_beat(cfg)
        age, berr = chat.beat_age(cfg)
        check("摸过之后年龄很小", (age is not None and age < 5 and not berr), f"age={age} err={berr}")
        chat.touch_beat(cfg, time.time() - 1000)
        age2, _ = chat.beat_age(cfg)
        check("把心跳拨到 1000 秒前 => 超过判死线 130 秒",
              age2 is not None and age2 > cfg.hub.beat_deadline_seconds, f"age={age2}")

        # ── ⑦ 归档：老行挪走，行号保持连续 ──
        rows_before = chat.read_all(cfg)[0]
        before = len(rows_before)
        keep_lines = [r["_line"] for r in rows_before[-2:]]
        info = chat.archive(cfg, keep=2)
        rows5, _b, meta5, err5 = chat.read_all(cfg)
        check("归档只留 keep 条", len(rows5) == 2, f"{len(rows5)} / 原来 {before}")
        check("归档挪走的条数对得上", info["moved_rows"] == before - 2, str(info))
        check("归档后那条消息的**绝对行号**没变（不跳号、不重号）",
              [r["_line"] for r in rows5] == keep_lines,
              f"{[r['_line'] for r in rows5]} vs {keep_lines}")
        check("meta 的 first_seq 指向保留下来的第一条",
              meta5.get("first_seq") == rows5[0]["seq"], f"{meta5} / {rows5[0]['seq']}")
        check("归档元数据读完没报错", err5 == "", err5)
        arc = Path(str(chat.archive_path(cfg)))
        check("历史册里确实有被挪走的行", arc.is_file()
              and len(arc.read_text(encoding="utf-8").splitlines()) == info["moved_lines"],
              str(arc))
        check("再归档一次不再动（已经只剩 2 条）", chat.archive(cfg, keep=2)["moved_rows"] == 0)

        # ── ⑧ CLI 走一遍（真跑，不是 mock）──
        rc = chat.main(["--config", str(cfg.path), "--tail", "1"])
        check("CLI --tail 退出码 0", rc == 0, str(rc))
        rc = chat.main(["--config", str(cfg.path), "--status"])
        check("CLI --status 退出码 0", rc == 0, str(rc))

    print(f"\n断言：{PASSED}/{TOTAL} 通过")
    if FAILS:
        print("未通过：")
        for x in FAILS:
            print(f"  · {x}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
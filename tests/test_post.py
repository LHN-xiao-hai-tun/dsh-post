"""投递 + 收件归位的真跑测试：两个临时目录模拟两台，⛔ 不起 ssh。

覆盖：
  名字合规（三种原因分类）/ 投一轮 + 字节一致 / 同名同内容不重投 /
  同内容换名判重复但覆盖不了收件方就算新件 / 内容变了要重投 /
  名字不合规被拦 / 锁单实例 / 空跑不落盘 / CLI 退出码 / 收件归位判重。

跑法（在仓根）：
    python tests/test_post.py
退出码 0 = 全部通过；非 0 = 有断言挂了，且会列出是哪条。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 测试在 Windows 控制台（代码页 936）里跑时，前半段 print 会抛 UnicodeEncodeError
# 或输出乱码 —— 与四个 CLI 入口保持一致，进程一起来就把 stdio 切到 UTF-8。
from bridge.config import enable_utf8_stdio  # noqa: E402

enable_utf8_stdio()

from bridge import post as post_mod  # noqa: E402
from bridge import receive as receive_mod  # noqa: E402
from bridge.config import load_config  # noqa: E402

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

[post]
exts = [".md"]
stale_minutes = 30
verify = true

[receive]
layout = "subdir"
backend = "local"
processed_dir = ""

[recipients]
"大家" = "*"
"乙机" = ["b"]
"甲机" = ["a"]

[naming]
act_kinds = ["信", "回话", "同步", "知会"]
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
    with tempfile.TemporaryDirectory(prefix="dsh-post-post-") as tmp:
        cfg = make_cfg(Path(tmp))
        outbox = Path(str(cfg.device().outbox))
        inbox_b = Path(str(cfg.devices["b"].inbox))

        # ── ① 名字合规：三种原因要分得开 ──
        ok, why = post_mod.validate_name(cfg, "知会_给乙机_主题_20261107.md")
        check("合规名字放行", ok and why == "ok", why)
        ok, why = post_mod.validate_name(cfg, "知会_发给乙机_主题_2026-11-07.md")
        check("带『发给』前缀的老格式也认（日期可带横线）", ok, why)
        ok, why = post_mod.validate_name(cfg, "通知_给乙机_主题_20261107.md")
        check("性质不在表里 => bad-act", (not ok) and why == "bad-act", why)
        ok, why = post_mod.validate_name(cfg, "知会_给丙机_主题_20261107.md")
        check("收件方查不到 => bad-target", (not ok) and why == "bad-target", why)
        ok, why = post_mod.validate_name(cfg, "知会_乙机_20261107.md")
        check("段数不对 => bad-fields", (not ok) and why == "bad-fields", why)
        ok, reasons = post_mod.validate_name_all(cfg, "通知_给丙机_主题_20261107.md")
        check("两个错要都报出来，且顺序是 bad-target 先于 bad-act",
              reasons == ["bad-target", "bad-act"], str(reasons))
        check("解不出收件方时 parse_targets 返回 None",
              post_mod.parse_targets(cfg, "知会_给丙机_主题_20261107.md") is None)
        check("『大家』展开成全部设备",
              post_mod.parse_targets(cfg, "知会_给大家_主题_20261107.md") == ["a", "b"],
              str(post_mod.parse_targets(cfg, "知会_给大家_主题_20261107.md")))

        # ── ② 投一轮 ──
        f1 = outbox / "知会_给乙机_第一封_20261107.md"
        f1.write_text("第一封正文\n第二行\n", encoding="utf-8")
        s1 = post_mod.post_once(cfg)
        check("候选 1、投出 1", s1["candidates"] == 1 and s1["delivered"] == 1, str(s1))
        got = inbox_b / f1.name
        check("对端收件箱里有这个文件", got.is_file())
        check("字节完全一致（中文没被改写）", got.is_file() and got.read_bytes() == f1.read_bytes())
        check("台账里记了 md5", post_mod.load_posted(cfg).get(f1.name) == post_mod.md5_file(f1))
        check("已发/ 里有副本", (Path(str(cfg.device().sent_dir)) / f1.name).is_file())
        check("原件还在发件箱（归档是 copy2 不是 mv）", f1.is_file())
        check("写了投递日志", (outbox / "post.log").is_file())

        # ── ③ 第二轮不重投 ──
        s2 = post_mod.post_once(cfg)
        check("第二轮投出 0、跳过 1", s2["delivered"] == 0 and s2["skipped"] == 1, str(s2))
        check("对端仍只有 1 份", len(list(inbox_b.iterdir())) == 1, str(list(inbox_b.iterdir())))

        # ── ④ 同内容换名：判重复，但只在收件方被覆盖时 ──
        posted = post_mod.load_posted(cfg)
        same = outbox / "知会_给乙机_换个名字_20261107.md"
        same.write_text("第一封正文\n第二行\n", encoding="utf-8")
        hit, why = post_mod.already_posted(cfg, posted, same)
        check("同内容换名 => 判成重复", hit, f"hit={hit}")
        check("理由标明是 same-content 而不是 same-name", why == "same-content", why)
        wider = outbox / "知会_给大家_换个名字_20261107.md"
        wider.write_text("第一封正文\n第二行\n", encoding="utf-8")
        hit2, _ = post_mod.already_posted(cfg, posted, wider)
        check("收件方没被覆盖 => 不算重复（同内容分投更多人是正当的）", not hit2)
        same.unlink()
        wider.unlink()

        # ── ⑤ 内容变了要重投（同名不同内容）──
        f1.write_text("第一封正文改过了\n", encoding="utf-8")
        s3 = post_mod.post_once(cfg)
        check("同名但内容变了 => 重投", s3["delivered"] == 1, str(s3))
        check("对端内容跟着更新",
              (inbox_b / f1.name).read_text(encoding="utf-8") == "第一封正文改过了\n")

        # ── ⑥ 名字不合规被拦 ──
        bad = outbox / "通知_给乙机_漏网之鱼_20261107.md"
        bad.write_text("这封不该被投\n", encoding="utf-8")
        s4 = post_mod.post_once(cfg)
        check("不合规的被拦下（不计入候选）",
              s4["blocked"] == 1 and s4["candidates"] == 0, str(s4))
        check("不合规的没被投出去", not (inbox_b / bad.name).exists())
        bl = post_mod.blocked_files(cfg)
        check("--status 能看到被拦的件", [p.name for p, _r in bl] == [bad.name],
              str([p.name for p, _r in bl]))
        check("被拦件的两类原因都报得出来",
              [r for _p, rs in bl for r in rs] == ["bad-act"], str([[r for r in rs] for _p, rs in bl]))
        bad.unlink()

        # ── ⑦ 锁单实例 ──
        lp = post_mod.lock_path(cfg)
        first = post_mod.PostLock(lp)
        first.__enter__()
        blocked_lock = False
        try:
            second = post_mod.PostLock(lp)
            try:
                second.__enter__()
            except post_mod.AlreadyRunning:
                blocked_lock = True
            else:
                second.__exit__(None, None, None)
        finally:
            first.__exit__(None, None, None)
        check("同一个锁第二次拿不到（单实例）", blocked_lock)
        again = False
        try:
            third = post_mod.PostLock(lp)
            third.__enter__()
            again = True
            third.__exit__(None, None, None)
        finally:
            pass
        check("释放后能再拿到锁", again)

        # ── ⑧ 空跑不落盘 ──
        f5 = outbox / "知会_给乙机_空跑测试_20261107.md"
        f5.write_text("空跑正文\n", encoding="utf-8")
        before = set(p.name for p in inbox_b.iterdir())
        s5 = post_mod.post_once(cfg, dry=True)
        after = set(p.name for p in inbox_b.iterdir())
        check("空跑：候选算得出，但不真投", s5["candidates"] == 1 and before == after, str(s5))
        check("空跑不写台账", f5.name not in post_mod.load_posted(cfg))

        # ── ⑨ CLI 走一遍（真跑）──
        rc = post_mod.main(["--config", str(cfg.path), "--once"])
        check("CLI --once 退出码 0", rc == 0, str(rc))
        rc = post_mod.main(["--config", str(cfg.path), "--status"])
        check("CLI --status 退出码 0", rc == 0, str(rc))
        rc = post_mod.main(["--config", str(cfg.path), "--once", "--dry-run"])
        check("CLI --dry-run 退出码 0", rc == 0, str(rc))

        # ── ⑩ 收件归位 ──
        drop = Path(str(cfg.device().inbox))
        letter = drop / "知会_给大家_共享投放点来的件_20261107.md"
        letter.write_text("这份件同时给两台\n", encoding="utf-8")
        r1 = receive_mod.receive_once(cfg)
        check("收件：扫到 1 件、归位 1 件", r1["seen"] == 1 and r1["placed"] == 1, str(r1))
        check("按收件方切进两台各自的格子",
              (drop / "a" / letter.name).is_file() and (drop / "b" / letter.name).is_file(),
              str(sorted(p.name for p in drop.iterdir())))
        check("格子里的字节和原件一致",
              (drop / "b" / letter.name).read_bytes() == letter.read_bytes())
        check("原件默认保留（删是不可逆动作，要显式配 processed_dir 才搬）", letter.is_file())
        r2 = receive_mod.receive_once(cfg)
        check("再扫一遍：判成重复，不重复归位", r2["dup"] == 1 and r2["placed"] == 0, str(r2))
        bad2 = drop / "通知_给乙机_不合规_20261107.md"
        bad2.write_text("x", encoding="utf-8")
        r3 = receive_mod.receive_once(cfg)
        check("收件侧也拦不合规名字", r3["bad"] == 1 and r3["placed"] == 0, str(r3))
        bad2.unlink()
        rc = receive_mod.main(["--config", str(cfg.path), "--status"])
        check("receive --status 退出码 0", rc == 0, str(rc))

    print(f"\n断言：{PASSED}/{TOTAL} 通过")
    if FAILS:
        print("未通过：")
        for x in FAILS:
            print(f"  · {x}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
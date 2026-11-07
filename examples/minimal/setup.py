#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小样例：一条命令证明 dsh-post 能用。

跑法（在包根目录）：
    python examples/minimal/setup.py            # 跑完自动清理
    python examples/minimal/setup.py --keep     # 保留临时目录，方便自己翻

它做四件事，每件都有判据，❌ 不看「没报错」：
  1. 建两个临时目录当两台设备（transport=local，不起 ssh）
  2. 甲机写一条聊天室消息 → 乙机读出来（正文必须逐字一致）
  3. 甲机发件箱放一封合规命名的信 → 投一轮 → 乙机收件箱出现，**md5 必须一致**
  4. 打完 PASS/FAIL，退出码 0/1
"""
import argparse, hashlib, os, re, shutil, subprocess, sys, tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent.parent

# 与包里四个 CLI 入口一致：Windows 控制台代码页常是 936，
# 不切 UTF-8 的话下面这些中文 PASS/FAIL 会变成乱码或直接抛 UnicodeEncodeError。
sys.path.insert(0, str(PKG))
from bridge.config import enable_utf8_stdio  # noqa: E402

enable_utf8_stdio()


def render(path: Path, subs: dict) -> str:
    t = Path(path).read_text(encoding="utf-8")
    for k, v in subs.items():
        t = t.replace(k, v)
    return t


def run(args, cfg, *, expect_ok=True):
    cmd = [sys.executable, "-m"] + args + ["--config", str(cfg)]
    p = subprocess.run(cmd, cwd=str(PKG), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if expect_ok and p.returncode != 0:
        print("---- 命令失败 ----")
        print(" ".join(cmd))
        print(p.stdout)
        print(p.stderr)
        raise SystemExit(2)
    return p


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    a = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="dsh_post_min_"))
    ta, tb = root / "devA", root / "devB"
    for d in (ta, tb):
        (d / "收件箱").mkdir(parents=True)
        (d / "发件箱").mkdir(parents=True)
    subs = {"{{TMP_A}}": ta.as_posix(), "{{TMP_B}}": tb.as_posix()}
    cfg_a = root / "deva.toml"
    cfg_b = root / "devb.toml"
    cfg_a.write_text(render(HERE / "deva.toml.example", subs), encoding="utf-8")
    cfg_b.write_text(render(HERE / "devb.toml.example", subs), encoding="utf-8")

    fails = []

    def check(name, ok, detail=""):
        print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + detail) if detail else ""))
        if not ok:
            fails.append(name)

    print("临时根目录:", root)
    print("[0/4] 渲染结果自检（占位符不留、路径不出临时根）")
    leak = []
    for label, cfg_path in (("deva", cfg_a), ("devb", cfg_b)):
        txt = cfg_path.read_text(encoding="utf-8")
        if "{{" in txt:
            leak.append(label + ": 还有未替换的占位符")
        for m in re.finditer(r'^\s*(?:root|workspace)\s*=\s*"([^"]+)"', txt, re.M):
            v = Path(m.group(1))
            try:
                v.resolve().relative_to(root.resolve())
            except ValueError:
                leak.append(label + ": 路径跑出临时根 -> " + m.group(1))
    check("两份配置全部落在临时目录内", not leak, "; ".join(leak))

    print("[1/4] 配置解析")
    p = run(["bridge.chat", "--where"], cfg_a)
    check("甲机配置能解析", "deva" in p.stdout)
    p = run(["bridge.chat", "--where"], cfg_b)
    check("乙机配置能解析", "devb" in p.stdout)

    print("[2/4] 聊天室：甲写 → 乙读")
    text = "最小样例自检 " + date.today().isoformat()
    run(["bridge.chat", "--say", text, "--from", "deva"], cfg_a)
    p = run(["bridge.chat", "--read"], cfg_b)
    check("乙机读到甲的正文（逐字一致）", text in p.stdout, text)

    print("[3/4] 投递：甲发件箱 → 乙收件箱")
    name = "知会_给乙机_最小样例_%s.md" % date.today().strftime("%Y%m%d")
    body = "这是一封最小样例的信。\n如果你看到它，说明投递链路通了。\n"
    mail = ta / "发件箱" / name
    mail.write_text(body, encoding="utf-8")
    src_md5 = md5(mail)
    run(["bridge.post", "--once"], cfg_a)
    found = list(tb.rglob(name))
    check("乙机收到同名文件", len(found) == 1, str(found[0]) if found else "没找到")
    if found:
        check("收到内容 md5 与原件一致", md5(found[0]) == src_md5, src_md5)

    print("[4/4] 收件归位")
    p = run(["bridge.receive", "--once"], cfg_b)
    check("归位命令退出 0", p.returncode == 0)

    print()
    if fails:
        print("FAIL:", len(fails), "项 ——", "; ".join(fails))
        print("临时目录保留在", root, "便于排查")
        return 1
    print("PASS: 4/4 全过 —— 这套包在单机上能跑通（投递 + 聊天 + 归位）")
    if not a.keep:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print("临时目录保留在", root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

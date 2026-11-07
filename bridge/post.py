"""投递：发件箱 → 各设备收件箱。

五条从实机上换来的纪律（每条都对应一次真事故，不是风格偏好）：

① **判重是「读-判-写」，不是原子的** => 必须有单实例锁。
   实测：计划任务每 2 分钟起的 `--once` 遇上手工再跑一次，4 秒内同一批信各投一遍。
   判重清单两次「读」都发生在对方「写」之前，于是都看到「没投过」。

② **判重不能只看文件名**：同一份内容换个名字就是「全新一封」。
   实测 22 秒内同一份内容被投了两遍（改了名 → 查不到 → 重投）。
   所以第二道判据是「md5 相同 **且** 收件方被覆盖」。

③ **归档用 copy2，⛔ 不用 mv**：原件被搬走 = 别的文档里那些按路径的引用
   当场变死链（我们真被文档体检闸红过）。

④ **名字不合规要拦，而且要分开说原因**：
   `bad-fields`（段数/日期都不对）要整份改名，`bad-act`（性质前缀）改一个字，
   `bad-target`（收件方）改一个字 —— 修法不同，混成一种就报不出是谁的错。

⑤ **退出码是「你忘了怎么办」的答案**：`--alarm-exit` 有积压就非 0，
   ⛔ 不指望谁天天去看日志。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from .config import (
    Config,
    ConfigError,
    HostError,
    describe,
    enable_utf8_stdio,
    load_config,
    md5_bytes,
    md5_file,
    now_stamp,
)

SENT_DIRNAME = "已发"
LOG_NAME = "post.log"
POSTED_NAME = ".posted.json"
LOCK_NAME = ".post.lock"

# `知会_给平板_主题_20261107.md`；早期还有 `…_2026-11-02.md` 这种带横线的日期。
# [!] 这是**格式**，不是路径，所以留在代码里；收件方词表与性质前缀都在 bridge.toml。
FNAME_RE = re.compile(
    r"^(?P<act>[^_]+)_(?P<to>[^_]+)_(?P<subject>.+)_(?P<date>\d{8}|\d{4}-\d{2}-\d{2})\.md$"
)

TO_PREFIXES = ("发给", "给", "回")

WHY_ACTION = {
    "bad-fields": "整份改名：要 {性质}_{发给谁}_{主题}_{YYYYMMDD}.md",
    "bad-act": "改一个词：性质只能是 ",
    "bad-target": "改一个词：收件方要能在 [recipients] 里查到",
}


# ─────────────────────────────────────────────────────────────
# 命名
# ─────────────────────────────────────────────────────────────


def name_date(fname: str) -> str:
    """文件名里的日期，统一成 8 位数字。解不出 => 空串。"""
    m = FNAME_RE.match(fname)
    if not m:
        return ""
    d = m.group("date").replace("-", "")
    return d if len(d) == 8 and d.isdigit() else ""


def is_new_name(cfg: Config, fname: str) -> bool:
    """这封是不是「新命名规矩生效之后」的件。

    [!] 判据是**文件名里的日期**，⛔ 不用 mtime：
       `cp -p` / 归档搬运 / 解包都会保留旧时间戳，拿 mtime 判新旧
       等于拿一个会被搬运改变的东西去判「它有没有变过」。
    """
    since = cfg.naming.enforce_since
    if not since:
        return True
    d = name_date(fname)
    return (not d) or d >= since


def strip_to_prefix(to: str) -> str:
    for pre in TO_PREFIXES:
        if to.startswith(pre):
            return to[len(pre):]
    return to


def validate_name_all(cfg: Config, fname: str) -> tuple[bool, list[str]]:
    """校验文件名，**收集全部原因**。顺序固定 bad-fields → bad-target → bad-act。

    [!] 这个顺序是契约：`cmd_status`/`cmd_once` 靠「第一个原因」分类汇报，
       顺序一反，双错的新件会落进 pending，报「待发 1 个」而下一轮必被拦。
    """
    reasons: list[str] = []
    m = FNAME_RE.match(fname)
    if not m:
        reasons.append("bad-fields")
        return (False, reasons)
    to = strip_to_prefix(m.group("to"))
    if to not in cfg.recipients:
        reasons.append("bad-target")
    if m.group("act") not in cfg.naming.act_kinds:
        reasons.append("bad-act")
    return (len(reasons) == 0, reasons)


def validate_name(cfg: Config, fname: str) -> tuple[bool, str]:
    ok, reasons = validate_name_all(cfg, fname)
    return (ok, reasons[0] if reasons else "ok")


def why_text(cfg: Config, why: str) -> str:
    base = WHY_ACTION.get(why, "看上面的原因")
    if why == "bad-act":
        base = base + "、".join(cfg.naming.act_kinds)
    return base


def parse_targets(cfg: Config, fname: str) -> list[str] | None:
    """从文件名解出要投给哪些设备。解不出 => None（留在原地不动）。"""
    m = FNAME_RE.match(fname)
    if not m:
        return None
    to = strip_to_prefix(m.group("to"))
    targets = cfg.recipients.get(to)
    return list(targets) if targets else None


# ─────────────────────────────────────────────────────────────
# 锁
# ─────────────────────────────────────────────────────────────


class AlreadyRunning(Exception):
    """另一个 `--once` 正在跑。不是错误，安静退出。"""


class PostLock:
    """单实例互斥锁。用内核文件锁，⛔ 不靠「写 pid 再判活」——
    那条路栽过三次：pid 会复用、`$!` 拿到的是 setsid 的号、进程退了锁没放。
    文件锁由内核在进程退出时自动释放，不需要清理。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def __enter__(self):
        # [!] mkdir 也要在 try 里：只读盘 / 路径被占同样抛 OSError。
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = self.path.open("a+b")
            if self.fh.seek(0, os.SEEK_END) == 0:
                self.fh.write(b"\0")
                self.fh.flush()
            self.fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            # 🔴 「锁被占」和「环境坏了」必须分开：前者安静退出，后者要喊。
            #    区分办法：拿得到句柄但锁不上 = 真有人跑；连句柄都没拿到 = 环境问题。
            if self.fh is None:
                raise RuntimeError(
                    f"锁文件打不开（{self.path}）：{type(e).__name__}: {e}\n"
                    f"    这不是「已有一个在跑」，是环境问题（权限/只读盘/路径被占）。"
                ) from e
            self.fh.close()
            self.fh = None
            raise AlreadyRunning() from e
        return self

    def __exit__(self, *exc):
        if self.fh is None:
            return False
        try:
            if os.name == "nt":
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.fh.close()
            self.fh = None
        return False


def lock_path(cfg: Config) -> Path:
    return Path(str(cfg.device().outbox)) / LOCK_NAME


def posted_path(cfg: Config) -> Path:
    return Path(str(cfg.device().outbox)) / POSTED_NAME


def log_path(cfg: Config) -> Path:
    return Path(str(cfg.device().outbox)) / LOG_NAME


# ─────────────────────────────────────────────────────────────
# 已投台账
# ─────────────────────────────────────────────────────────────


def load_posted(cfg: Config) -> dict:
    try:
        j = json.loads(posted_path(cfg).read_text(encoding="utf-8"))
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def save_posted(cfg: Config, posted: dict) -> bool:
    """写台账，**返回是否真写成功**。⛔ 别把异常静默吞掉：
    吞掉之后日志说「已记录」、盘上是空的，下一轮就重投一遍。"""
    p = posted_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(posted, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return True
    except OSError:
        return False


def already_posted(cfg: Config, posted: dict, f: Path) -> tuple[bool, str]:
    """这封信投过没有？→ (投过没有, 为什么这么判)。

    两种「投过」要分开说，⛔ 不要都叫「投过」：
      same-name     同名同内容（最正常；改了内容 md5 会变 => 重投）
      same-content  换了名字的同一份内容，且收件方被覆盖（本包新堵的洞）
    [!] 但「md5 相同就跳」是错的：同一份内容分投给不同收件方是正当的，
       所以第二条还要判「上次那批收件方覆盖了这次要投的」。
    """
    try:
        digest = md5_file(f)
    except OSError:
        return False, ""
    if posted.get(f.name) == digest:
        return True, "same-name"
    tg = set(parse_targets(cfg, f.name) or [])
    if not tg:
        return False, ""
    for old_name, old_md5 in posted.items():
        if old_md5 != digest or old_name == f.name:
            continue
        old_tg = set(parse_targets(cfg, old_name) or [])
        if tg <= old_tg:
            return True, "same-content"
    return False, ""


# ─────────────────────────────────────────────────────────────
# 投递
# ─────────────────────────────────────────────────────────────


def list_outbox(cfg: Config) -> list[Path]:
    """发件箱里**该被投递的候选**（按后缀过滤，按名字排序）。

    ⛔ 不递归：`已发/` 是发件箱的子目录，递归会把归档件当待发重投一遍。
    """
    box = Path(str(cfg.device().outbox))
    if not box.is_dir():
        return []
    exts = tuple(cfg.post.exts)
    return sorted(
        (p for p in box.iterdir() if p.is_file() and p.name.endswith(exts)),
        key=lambda p: p.name,
    )


def deliver(cfg: Config, dev_key: str, local: Path) -> dict:
    """把一件投到 `dev_key` 的收件箱。校验 md5，对不上不算成功。"""
    dev = cfg.device(dev_key)
    host = cfg.host(dev_key)
    try:
        data = local.read_bytes()
    except OSError as e:
        return {"ok": False, "why": f"读不了原件：{e}", "md5": "", "verified": False}
    digest = md5_bytes(data)
    dest = dev.inbox / local.name
    try:
        host.mkdir(dev.inbox)
        host.write_bytes(dest, data)
    except (HostError, OSError) as e:
        return {"ok": False, "why": f"写入失败：{e}", "md5": digest, "verified": False}
    if not cfg.post.verify:
        return {"ok": True, "why": "OK（未校验）", "md5": digest, "verified": False}
    got = host.digest(dest)
    if not got:
        return {"ok": True, "why": "写入成功，但取不到对端 md5（未验成）",
                "md5": digest, "verified": False}
    if got != digest:
        return {"ok": False, "why": f"md5 不一致（要 {digest}，对端 {got}）",
                "md5": digest, "verified": False}
    return {"ok": True, "why": "OK", "md5": digest, "verified": True}


def archive(cfg: Config, local: Path, digest: str = "") -> bool:
    """投成功后**复制**一份到 已发/ 做记录，原件留在发件箱。"""
    sent = Path(str(cfg.device().sent_dir))
    try:
        sent.mkdir(parents=True, exist_ok=True)
        dest = sent / local.name
        if not dest.exists():
            shutil.copy2(local, dest)
    except OSError:
        return False
    posted = load_posted(cfg)
    posted[local.name] = digest or md5_file(local)
    return save_posted(cfg, posted)


def log_line(cfg: Config, msg: str) -> None:
    p = log_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"[{now_stamp()}] {msg}\n")
    except OSError:
        pass


def backlog(cfg: Config) -> list[tuple[Path, str]]:
    """还没投过、且躺了超过 `[post].stale_minutes` 的件。"""
    posted = load_posted(cfg)
    out: list[tuple[Path, str]] = []
    now = time.time()
    for f in list_outbox(cfg):
        if parse_targets(cfg, f.name) is None:
            continue
        if already_posted(cfg, posted, f)[0]:
            continue
        try:
            age_min = (now - f.stat().st_mtime) / 60
        except OSError:
            continue
        if age_min > cfg.post.stale_minutes:
            out.append((f, f"{age_min:.0f} 分钟未投出"))
    return out


def blocked_files(cfg: Config) -> list[tuple[Path, list[str]]]:
    """名字不合规、不会被投的件。⛔ 它们不会自己消失，要让 `--status` 说出来。"""
    out = []
    for f in list_outbox(cfg):
        ok, reasons = validate_name_all(cfg, f.name)
        if not ok and is_new_name(cfg, f.name):
            out.append((f, reasons))
    return out


def post_once(cfg: Config, dry: bool = False) -> dict:
    """投一轮。返回统计（也供 `--self-test` 断言）。"""
    stats = {"candidates": 0, "delivered": 0, "skipped": 0, "blocked": 0,
             "failed": 0, "targets": {}, "errors": []}
    posted = load_posted(cfg)
    for f in list_outbox(cfg):
        ok, reasons = validate_name_all(cfg, f.name)
        if not ok:
            if is_new_name(cfg, f.name):
                stats["blocked"] += 1
                stats["errors"].append(f"{f.name}：被拦（{'、'.join(reasons)}）")
                continue
        targets = parse_targets(cfg, f.name)
        if targets is None:
            stats["skipped"] += 1
            continue
        hit, why = already_posted(cfg, posted, f)
        if hit:
            stats["skipped"] += 1
            continue
        stats["candidates"] += 1
        if dry:
            line = f"（空跑）将投：{f.name} → {'、'.join(targets)}"
            print(line)
            stats["errors"].append(line)
            continue
        all_ok = True
        for t in targets:
            r = deliver(cfg, t, f)
            stats["targets"][t] = stats["targets"].get(t, 0) + (1 if r["ok"] else 0)
            if r["ok"]:
                log_line(cfg, f"投出 → {t}  {f.name}"
                              + ("" if r["verified"] else f"（{r['why']}）"))
            else:
                all_ok = False
                stats["failed"] += 1
                log_line(cfg, f"失败 → {t}  {f.name}：{r['why']}")
                stats["errors"].append(f"{f.name} → {t}：{r['why']}")
        if all_ok:
            stats["delivered"] += 1
            if not archive(cfg, f, md5_file(f)):
                # 记不上账 = 下一轮重投；这比「少一份留档」更贵，所以要喊出来。
                log_line(cfg, f"[!] 已投但记账失败：{f.name}")
                stats["errors"].append(f"{f.name}：已投但 .posted.json 写不进去（下轮会重投）")
    return stats


# ─────────────────────────────────────────────────────────────
# 状态
# ─────────────────────────────────────────────────────────────


def cmd_status(cfg: Config) -> int:
    print(describe(cfg))
    box = list_outbox(cfg)
    posted = load_posted(cfg)
    blocked = blocked_files(cfg)
    pend: list[Path] = []
    done = 0
    for f in box:
        if parse_targets(cfg, f.name) is None:
            continue
        if already_posted(cfg, posted, f)[0]:
            done += 1
        else:
            pend.append(f)
    print(f"发件箱：{len(box)} 件（认得出的 {done + len(pend)} 件 · 已投 {done} · 待投 {len(pend)}）")
    if blocked:
        print(f"⛔ 名字不合规 {len(blocked)} 件（这些**不会**被投）：")
        for f, reasons in blocked[:20]:
            print(f"   · {f.name}")
            for why in reasons:
                print(f"     - {why}：{why_text(cfg, why)}")
        if len(blocked) > 20:
            print(f"   ……还有 {len(blocked) - 20} 件")
    stale = backlog(cfg)
    if stale:
        print(f"[!] 积压 {len(stale)} 件（超过 {cfg.post.stale_minutes} 分钟没投出去）：")
        for f, why in stale[:10]:
            print(f"   · {f.name}（{why}）")
    lp = log_path(cfg)
    if lp.is_file():
        try:
            tail = lp.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        except OSError:
            tail = []
        if tail:
            print("最近投递日志：")
            for line in tail:
                print(f"   {line}")
    return 0


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bridge.post",
        description="投递器：把发件箱里合规矩的件投到各设备收件箱。",
    )
    p.add_argument("--config", default=None, help="bridge.toml 路径（默认自动找）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="扫一遍发件箱，投一轮")
    g.add_argument("--status", action="store_true", help="状态：待投/已投/被拦/积压")
    g.add_argument("--send", metavar="FILE", help="只投这一件（收件方按文件名解）")
    g.add_argument("--self-test", action="store_true",
                   help="用两个临时目录自测（不起 ssh，不需要真配置）")
    p.add_argument("--to", action="append", default=None, metavar="DEVICE",
                   help="配合 --send：强制投给这台（可多次）")
    p.add_argument("--dry-run", action="store_true", help="空跑：只打印将做什么")
    p.add_argument("--alarm-exit", action="store_true",
                   help="有积压或被拦 => 退出码 1（给计划任务用）")
    return p


def send_one(cfg: Config, f: Path, explicit: list[str] | None, dry: bool) -> int:
    if not f.is_file():
        print(f"[X] 文件不存在：{f}", file=sys.stderr)
        return 2
    ok, reasons = validate_name_all(cfg, f.name)
    if not ok and is_new_name(cfg, f.name):
        print(f"[X] 名字不合规，不投：{f.name}", file=sys.stderr)
        for why in reasons:
            print(f"   - {why}：{why_text(cfg, why)}", file=sys.stderr)
        return 2
    targets = explicit or parse_targets(cfg, f.name)
    if not targets:
        print(f"[X] 解不出收件方（文件名要写成 {{性质}}_{{发给谁}}_{{主题}}_{{日期}}.md）",
              file=sys.stderr)
        return 2
    bad = [t for t in targets if t not in cfg.devices]
    if bad:
        print(f"[X] 不认识的设备：{'、'.join(bad)}", file=sys.stderr)
        return 2
    for t in targets:
        if dry:
            print(f"（空跑）将投：{f.name} → {t}")
            continue
        r = deliver(cfg, t, f)
        print(("[OK] " if r["ok"] else "[X] ") + f"{f.name} → {t}：{r['why']}")
        if not r["ok"]:
            return 1
        log_line(cfg, f"手工投出 → {t}  {f.name}")
    if not dry and not archive(cfg, f):
        print("[!] 已投但 .posted.json 写不进去（下轮会重投）", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"[X] {e}", file=sys.stderr)
        return 2

    if args.status:
        return cmd_status(cfg)
    if args.send:
        return send_one(cfg, Path(args.send), args.to, args.dry_run)
    if not args.once:
        build_parser().print_help()
        return 1

    try:
        with PostLock(lock_path(cfg)):
            stats = post_once(cfg, dry=args.dry_run)
    except AlreadyRunning:
        print("（已有一个 --once 在跑，这轮跳过）")
        return 0
    except RuntimeError as e:
        print(f"[X] {e}", file=sys.stderr)
        return 2

    tag = "（空跑）" if args.dry_run else ""
    print(f"{tag}候选 {stats['candidates']} · 投出 {stats['delivered']} · "
          f"跳过 {stats['skipped']} · 被拦 {stats['blocked']} · 失败 {stats['failed']}")
    for e in stats["errors"][:20]:
        print(f"   · {e}")

    if args.alarm_exit:
        stale = backlog(cfg)
        blocked = blocked_files(cfg)
        if stats["failed"] or stale or blocked:
            print(f"[ALARM] 报警退出：失败 {stats['failed']} · 积压 {len(stale)} · 被拦 {len(blocked)}",
                  file=sys.stderr)
            return 1
    return 0 if stats["failed"] == 0 else 1


# ─────────────────────────────────────────────────────────────
# 自测：两个临时目录，不起 ssh
# ─────────────────────────────────────────────────────────────


SELFTEST_TOML = """\
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

[recipients]
"大家" = "*"
"乙机" = ["b"]

[naming]
act_kinds = ["信", "回话", "同步", "知会"]
"""


def self_test() -> int:
    passed = 0
    total = 0
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
            print(f"[OK] {name}")
        else:
            fails.append(f"{name}{('：' + detail) if detail else ''}")
            print(f"[X] {name}{('：' + detail) if detail else ''}")

    with tempfile.TemporaryDirectory(prefix="dsh-post-selftest-") as tmp:
        base = Path(tmp)
        a = base / "甲"
        b = base / "乙"
        for d in (a, b):
            (d / "收件箱").mkdir(parents=True)
            (d / "发件箱").mkdir(parents=True)
        cfg_path = base / "bridge.toml"
        cfg_path.write_text(SELFTEST_TOML.format(a=a.as_posix(), b=b.as_posix()),
                            encoding="utf-8")
        cfg = load_config(cfg_path)

        # ① 命名校验
        ok, why = validate_name(cfg, "知会_给乙机_主题_20261107.md")
        check("合规模板的名字放行", ok, why)
        ok, why = validate_name(cfg, "通知_给乙机_主题_20261107.md")
        check("性质前缀不在表里 => bad-act", (not ok) and why == "bad-act", why)
        ok, why = validate_name(cfg, "知会_给丙机_主题_20261107.md")
        check("收件方查不到 => bad-target", (not ok) and why == "bad-target", why)
        ok, why = validate_name(cfg, "知会_乙机_20261107.md")
        check("段数/日期不对 => bad-fields", (not ok) and why == "bad-fields", why)

        # ② 投一轮 + md5 判重
        f1 = Path(str(cfg.device().outbox)) / "知会_给乙机_第一封_20261107.md"
        f1.write_text("第一封正文\n", encoding="utf-8")
        s1 = post_once(cfg)
        check("投出一件", s1["delivered"] == 1, str(s1))
        got = Path(str(cfg.devices["b"].inbox)) / f1.name
        check("对端收件箱里有这个文件", got.is_file())
        check("字节一致", got.is_file() and got.read_bytes() == f1.read_bytes())
        s2 = post_once(cfg)
        check("第二轮不重投（同名同内容）", s2["delivered"] == 0 and s2["skipped"] == 1, str(s2))

        # ③ 同一内容换个名字：只有收件方被覆盖才算重复
        posted = load_posted(cfg)
        f2 = Path(str(cfg.device().outbox)) / "知会_给乙机_第一封改个名_20261107.md"
        f2.write_text("第一封正文\n", encoding="utf-8")
        hit, why = already_posted(cfg, posted, f2)
        check("同内容换名 => 判成重复", hit, f"hit={hit} why={why}")
        check("判定理由说清了是哪种重复", why == "same-content", why)
        # 收件方没被覆盖就不该判重复（同内容分投给更多人，是正当的）
        f2b = Path(str(cfg.device().outbox)) / "知会_给大家_第一封改个名_20261107.md"
        f2b.write_text("第一封正文\n", encoding="utf-8")
        hit2, why2 = already_posted(cfg, posted, f2b)
        check("收件方没被覆盖 => 不判重复", not hit2, f"hit={hit2} why={why2}")
        f2b.unlink()

        # ④ 归档是 copy2，原件不动
        check("归档后原件仍在发件箱", f1.is_file())
        sent = Path(str(cfg.device().sent_dir)) / f1.name
        check("已发/ 里有副本", sent.is_file())

        # ⑤ 锁：单实例
        lp = lock_path(cfg)
        first = PostLock(lp)
        first.__enter__()
        try:
            second = PostLock(lp)
            blocked = False
            try:
                second.__enter__()
            except AlreadyRunning:
                blocked = True
            else:
                second.__exit__(None, None, None)
            check("同一个锁第二次拿不到（单实例）", blocked)
        finally:
            first.__exit__(None, None, None)
        third = PostLock(lp)
        reacquired = False
        try:
            third.__enter__()
            reacquired = True
        finally:
            third.__exit__(None, None, None)
        check("释放后能再拿到锁", reacquired)

        # ⑥ 空跑不落盘
        f3 = Path(str(cfg.device().outbox)) / "知会_给乙机_空跑测试_20261107.md"
        f3.write_text("空跑正文\n", encoding="utf-8")
        before = set(Path(str(cfg.devices["b"].inbox)).iterdir())
        post_once(cfg, dry=True)
        after = set(Path(str(cfg.devices["b"].inbox)).iterdir())
        check("空跑没有真投出去", before == after)
        check("空跑不写台账", f3.name not in load_posted(cfg))

    print(f"\n自测：{passed}/{total} 通过")
    if fails:
        print("失败项：")
        for x in fails:
            print(f"  · {x}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
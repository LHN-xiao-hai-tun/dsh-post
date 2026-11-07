"""收件侧：把收件箱里的件**按收件方归位**。

场景（为什么会需要这一步）：三台设备常常往**同一个投放目录**丢件
（共享盘、USB 挂载点、`/sdcard` 上的目录都是这个形态），
于是「给我的」和「给别人的」混在一个框里。这一步按文件名里的收件方
把它们分到各自的格子：`<收件箱>/<设备 key>/<原文件名>`。

三条纪律：

① **判重靠台账（`.received.json`：文件名 -> md5），不靠「搬走就算处理过」。**
   搬走后同一封再被投一次，就看不出它来过 —— 这正是「删掉旧文件等于绕过判重」的形状。

② **先归位、再记台账、最后才搬原件**（`processed_dir` 配了才搬）。
   顺序反了，中途失败会留下「原件没了、格子也没有」的黑洞。

③ **后端留口**：`[receive].backend = "local"` 是唯一实现了的后端；
   `posix-remote` 只是一个**接口占位**，跑起来会明确报「没实现」，
   ⛔ 不会假装成功 —— 假成功比失败贵得多，因为没人会再去查。

⚠️ 关于「归位后原件还在不在」：默认**不删**。收件箱里留一份副本不占多少空间，
   而删错了就是不可逆的。要清就走 `processed_dir`（那是个明确的动作）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
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
from . import post as post_mod

RECEIVED_NAME = ".received.json"


# ─────────────────────────────────────────────────────────────
# 后端：local 已实现，posix-remote 只有接口
# ─────────────────────────────────────────────────────────────


class InboxBackend:
    """收件后端接口。

    实现方要提供三个动作，`apply()` 只依赖这三个：
      list_files()        —— 框里有哪些待归位的件（文件名，不含目录）
      read_bytes(name)    —— 取一份的字节
      put(name, data, sub)—— 放进子目录 sub（按设备 key）
      ack(name, sub)      —— 归位完成后对原件的处置（删/搬/不动）

    目前只有 `LocalInboxBackend`。`PosixRemoteInboxBackend` 是**占位**：
    平板/手机侧若要用另一套实现（例如 App 里的投递队列、或 sdcard 上的
    监听器），照这个接口写一个类，再把 `[receive].backend` 指过去即可。
    """

    name = "base"

    def list_files(self) -> list[str]:
        raise NotImplementedError

    def read_bytes(self, name: str) -> bytes:
        raise NotImplementedError

    def put(self, name: str, data: bytes, sub: str) -> Path:
        raise NotImplementedError

    def ack(self, name: str, sub: str) -> str:
        raise NotImplementedError


class LocalInboxBackend(InboxBackend):
    name = "local"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.box = Path(str(cfg.device().inbox))
        # `processed_dir` 是**相对收件箱**的：写 `已处理` 就是 `<收件箱>/已处理/`。
        self.processed = (self.box / cfg.receive.processed_dir
                          if cfg.receive.processed_dir else None)

    def list_files(self) -> list[str]:
        if not self.box.is_dir():
            return []
        exts = tuple(self.cfg.post.exts)
        return sorted(p.name for p in self.box.iterdir()
                      if p.is_file() and p.name.endswith(exts))

    def read_bytes(self, name: str) -> bytes:
        return (self.box / name).read_bytes()

    def put(self, name: str, data: bytes, sub: str) -> Path:
        dest_dir = self.box / sub if sub else self.box
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        if not dest.exists() or dest.read_bytes() != data:
            tmp = dest.with_name(dest.name + ".tmp-dshbridge")
            tmp.write_bytes(data)
            tmp.replace(dest)
        return dest

    def ack(self, name: str, sub: str) -> str:
        if self.processed is None:
            return "留在原地"
        self.processed.mkdir(parents=True, exist_ok=True)
        src = self.box / name
        dest = self.processed / name
        try:
            shutil.copy2(src, dest)
            src.unlink()
        except OSError as e:
            return f"搬原件失败：{e}"
        return f"原件已移入 {dest}"


class PosixRemoteInboxBackend(InboxBackend):
    """占位后端：**没有实现**，调用即明确报错。

    要接的人照下面这三行的语义补一个类（平板/手机各自的投放器形态不同，
    我们这边不去猜它们的行为 —— 猜错会静默丢件）：
      list_files()          列出待归位件
      read_bytes(name)      读字节
      put(name, data, sub)  写进子目录并返回落点
      ack(name, sub)        归位后处置原件（删/搬/不动）并说明
    """

    name = "posix-remote"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _nope(self) -> None:
        raise ConfigError(
            "[receive].backend = \"posix-remote\" 这一版**没有实现**。"
            "它不是一个配置开关，是一个要由部署方实现的接口："
            "照 bridge/receive.py 里 InboxBackend 的四个方法写一个类，"
            "或者先用 local 后端（本机/挂载盘都能跑）。"
        )

    def list_files(self) -> list[str]:
        self._nope()
        return []

    def read_bytes(self, name: str) -> bytes:
        self._nope()
        return b""

    def put(self, name: str, data: bytes, sub: str) -> Path:
        self._nope()
        return Path()

    def ack(self, name: str, sub: str) -> str:
        self._nope()
        return ""


def make_backend(cfg: Config) -> InboxBackend:
    if cfg.receive.backend == "local":
        return LocalInboxBackend(cfg)
    return PosixRemoteInboxBackend(cfg)


# ─────────────────────────────────────────────────────────────
# 台账
# ─────────────────────────────────────────────────────────────


def ledger_path(cfg: Config) -> Path:
    return Path(str(cfg.device().inbox)) / RECEIVED_NAME


def load_ledger(cfg: Config) -> dict:
    try:
        j = json.loads(ledger_path(cfg).read_text(encoding="utf-8"))
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ledger(cfg: Config, ledger: dict) -> bool:
    p = ledger_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return True
    except OSError:
        return False


# ─────────────────────────────────────────────────────────────
# 归位
# ─────────────────────────────────────────────────────────────


def receive_once(cfg: Config, dry: bool = False) -> dict:
    """扫一遍收件箱，把件按收件方归位。返回统计（也供测试断言）。"""
    backend = make_backend(cfg)
    ledger = load_ledger(cfg)
    stats = {"seen": 0, "placed": 0, "dup": 0, "bad": 0,
             "per_device": {}, "errors": []}
    for name in backend.list_files():
        stats["seen"] += 1
        ok, reasons = post_mod.validate_name_all(cfg, name)
        if not ok and post_mod.is_new_name(cfg, name):
            stats["bad"] += 1
            stats["errors"].append(f"{name}：名字不合规（{'、'.join(reasons)}），没归位")
            continue
        targets = post_mod.parse_targets(cfg, name)
        if not targets:
            stats["bad"] += 1
            stats["errors"].append(f"{name}：解不出收件方，没归位")
            continue
        try:
            data = backend.read_bytes(name)
        except (OSError, HostError) as e:
            stats["errors"].append(f"{name}：读不了（{e}）")
            continue
        digest = md5_bytes(data)
        if ledger.get(name) == digest:
            stats["dup"] += 1
            continue
        if dry:
            for t in targets:
                print(f"（空跑）将归位：{name} → {t}/")
            stats["placed"] += 1
            continue
        placed_any = False
        for t in targets:
            sub = t if cfg.receive.layout == "subdir" else ""
            try:
                dest = backend.put(name, data, sub)
            except (OSError, HostError, ConfigError) as e:
                stats["errors"].append(f"{name} → {t}：{e}")
                continue
            stats["per_device"][t] = stats["per_device"].get(t, 0) + 1
            placed_any = True
            print(f"[OK] 归位：{name} → {dest}")
        if placed_any:
            stats["placed"] += 1
            ledger[name] = digest
            if not save_ledger(cfg, ledger):
                stats["errors"].append(f"{name}：已归位但 .received.json 写不进去（下次会重做）")
            note = backend.ack(name, "")
            if note and note != "留在原地":
                print(f"     {note}")
    return stats


def cmd_status(cfg: Config) -> int:
    print(describe(cfg))
    backend = make_backend(cfg)
    try:
        names = backend.list_files()
    except ConfigError as e:
        print(f"[X] {e}")
        return 2
    ledger = load_ledger(cfg)
    bad = [n for n in names
           if not post_mod.validate_name_all(cfg, n)[0]
           and post_mod.is_new_name(cfg, n)]
    dup = [n for n in names if n in ledger]
    print(f"收件箱：{Path(str(cfg.device().inbox))}")
    print(f"待归位 {len(names)} 件（台账里已归位 {len(dup)} · 名字不合规 {len(bad)}）")
    for n in bad[:20]:
        print(f"   · {n}")
    if len(bad) > 20:
        print(f"   ……还有 {len(bad) - 20} 件")
    print(f"后端：{backend.name} · 布局：{cfg.receive.layout} · "
          f"处理完去处：{cfg.receive.processed_dir or '（不搬，留在原地）'}")
    return 0


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bridge.receive",
        description="收件侧：把收件箱里的件按收件方归位（可判重、可空跑）。",
    )
    p.add_argument("--config", default=None, help="bridge.toml 路径（默认自动找）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="扫一遍并归位")
    g.add_argument("--status", action="store_true", help="状态")
    p.add_argument("--dry-run", action="store_true", help="空跑：只打印将归位到哪")
    return p


def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"[X] {e}", file=sys.stderr)
        return 2
    if args.status:
        return cmd_status(cfg)
    if not args.once:
        build_parser().print_help()
        return 1
    try:
        stats = receive_once(cfg, dry=args.dry_run)
    except ConfigError as e:
        print(f"[X] {e}", file=sys.stderr)
        return 2
    tag = "（空跑）" if args.dry_run else ""
    print(f"{tag}扫过 {stats['seen']} · 归位 {stats['placed']} · "
          f"重复 {stats['dup']} · 不合规 {stats['bad']}")
    for e in stats["errors"][:20]:
        print(f"   · {e}")
    return 0 if not stats["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
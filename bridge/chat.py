"""聊天室：三方共享一个 `hub.jsonl`，按游标读、按行号去重。

从既有实现里搬过来的三条硬事实（都在实机上踩过，不是推演）：

① **seq 和物理行号是两个字段，⛔ 不要让一个兼职。**
   seq 答「我说到第几条」（给人看、算偏移），line 答「我读到文件第几行」
   （**去重只认它**：只追加的文件里行号天然唯一）。
   实测过 seq 撞号：两条消息同一个 seq，游标推过去之后两条都读不到，
   `--status` 还报「差 0 条」——静默丢消息。

② **坏行必须跳过，不许崩，而且要把原文存下来。**
   行级解析失败只影响那一行；原文进 `hub.bad.jsonl`（base64），
   事后还能捞回来。[!] 坏行**占号**但当不了消息，所以「物理行数 > 消息条数」是正常的。

③ **`.beat` 是判死线**：守护每轮摸一次，超过 `beat_deadline_seconds`
   没摸就是「活着但没干活」，比进程崩了更难查。

⛔ 不改 `hub.jsonl` 的行格式：`{"seq": int, "from": str, "at": str, "text": str, "via"?: str}`
   —— 这样新包能直接读老数据。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from .config import (
    Config,
    ConfigError,
    HostError,
    PathLike,
    describe,
    enable_utf8_stdio,
    load_config,
    md5_bytes,
    now_stamp,
)

MSG_FIELDS = {"seq": int, "from": str, "at": str, "text": str}
BAD_FIELDS = {"line_no": int, "at": str, "raw_b64": str}


# ─────────────────────────────────────────────────────────────
# 派生路径（全部由 config 里的 hub.path 推出来，⛔ 没有第二处来源）
# ─────────────────────────────────────────────────────────────


def _split_parent(p: PathLike) -> tuple[str, str]:
    """拆父目录与文件名。⛔ 不用 `Path()` —— Windows 上它会把 POSIX 路径换成反斜杠，
    远端路径就这样被弄坏，而报错长得像「文件不存在」。"""
    s = str(p)
    idx = max(s.rfind("/"), s.rfind("\\"))
    if idx <= 0:
        return ("", s)
    return (s[:idx], s[idx + 1:])


def _sibling(p: PathLike, new_name: str) -> PathLike:
    parent, _ = _split_parent(p)
    s = str(p)
    sep = "\\" if ("\\" in s and "/" not in s) else "/"
    joined = (parent.rstrip("/\\") + sep + new_name) if parent else new_name
    return type(p)(joined)


def _stem(p: PathLike) -> str:
    _parent, name = _split_parent(p)
    i = name.rfind(".")
    return name[:i] if i > 0 else name


def bad_path(cfg: Config) -> PathLike:
    return _sibling(cfg.hub.path, _stem(cfg.hub.path) + ".bad.jsonl")


def meta_path(cfg: Config) -> PathLike:
    return _sibling(cfg.hub.path, _stem(cfg.hub.path) + ".meta")


def archive_path(cfg: Config) -> PathLike:
    return _sibling(cfg.hub.path, _stem(cfg.hub.path) + "_旧.jsonl")


# ─────────────────────────────────────────────────────────────
# 元数据（归档闸）
# ─────────────────────────────────────────────────────────────


def read_meta(cfg: Config) -> tuple[dict, str]:
    """读归档元数据，返回 (meta, 错误说明)。

    ⛔ 「没有 meta」不算错：从没归档过时 first_seq 恒为 1、line_offset 恒为 0，
       和按 meta 算的结果一样。但「读失败」和「没有」要分开报。
    """
    host = cfg.hub_host()
    path = meta_path(cfg)
    if not host.exists(path):
        return {"first_seq": 1, "line_offset": 0}, ""
    try:
        raw = host.read_text(path)
    except (HostError, OSError) as e:
        return {"first_seq": 1, "line_offset": 0}, f"读 hub.meta 失败：{e}"
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    if not line:
        return {"first_seq": 1, "line_offset": 0}, ""
    try:
        m = json.loads(line)
    except ValueError:
        return {"first_seq": 1, "line_offset": 0}, f"hub.meta 不是合法 JSON：{line[:120]}"
    if not isinstance(m, dict):
        return {"first_seq": 1, "line_offset": 0}, f"hub.meta 不是对象：{line[:120]}"
    fs = m.get("first_seq", 1)
    lo = m.get("line_offset", 0)
    return {
        "first_seq": fs if isinstance(fs, int) and not isinstance(fs, bool) and fs >= 1 else 1,
        "line_offset": lo if isinstance(lo, int) and not isinstance(lo, bool) and lo >= 0 else 0,
        "archived_at": m.get("archived_at", ""),
        "archived_rows": m.get("archived_rows", 0),
    }, ""


def write_meta(cfg: Config, meta: dict) -> None:
    host = cfg.hub_host()
    payload = {
        "first_seq": int(meta.get("first_seq", 1)),
        "line_offset": int(meta.get("line_offset", 0)),
        "archived_at": now_stamp(),
        "archived_rows": int(meta.get("archived_rows", 0)),
    }
    host.write_text(meta_path(cfg), json.dumps(payload, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────
# 读
# ─────────────────────────────────────────────────────────────


def split_lines(text: str) -> list[str]:
    """按 `\\n` 切，末尾换行不算多一行。

    [!] 顺手剥掉行尾的 `\\r`：Windows 上任何一次「用记事本/别家工具打开再存」
       都会把整份文件变成 CRLF。那不是坏行，只是换行符不同 ——
       把它当坏行会让 `--status` 报一堆假坏行，而真坏行就混在里面看不出来了。
    """
    if text == "":
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def parse_lines(lines: list[str], line_offset: int) -> tuple[list[dict], list[tuple[int, str]]]:
    """解析物理行。返回 (rows, bad[(绝对行号, 原文)])。

    [!] 空行不算坏行：它只是占号。
       坏行 = 非空、但**解不出 `seq`（int）和 `text`（str）** —— 这两样缺一样
       就没法当消息显示。`from`/`at` 缺失只降级成 `?`（历史行可能是别家工具写的），
       ⛔ 不因此判坏：判据要窄而硬，查得越宽越容易把正常数据判死。
    """
    rows: list[dict] = []
    bad: list[tuple[int, str]] = []
    for i, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            bad.append((line_offset + i, raw))
            continue
        if (not isinstance(obj, dict)
                or not isinstance(obj.get("seq"), int)
                or isinstance(obj.get("seq"), bool)
                or not isinstance(obj.get("text"), str)):
            bad.append((line_offset + i, raw))
            continue
        row = {k: obj.get(k) for k in ("seq", "from", "at", "text", "via") if k in obj}
        row["_line"] = line_offset + i
        rows.append(row)
    return rows, bad


def read_all(cfg: Config) -> tuple[list[dict], list[tuple[int, str]], dict, str]:
    """全量读。返回 (rows, bad, meta, err)。文件不存在 => 空，不是错误。"""
    host = cfg.hub_host()
    meta, merr = read_meta(cfg)
    try:
        text = host.read_text(cfg.hub.path)
    except FileNotFoundError:
        return [], [], meta, merr
    except HostError as e:
        return [], [], meta, (merr + "；" if merr else "") + f"读中心失败：{e}"
    rows, bad = parse_lines(split_lines(text), int(meta.get("line_offset", 0)))
    return rows, bad, meta, merr


def record_bad(cfg: Config, bad: list[tuple[int, str]]) -> int:
    """把坏行原文记进 `hub.bad.jsonl`（按行号去重）。返回新记了几条。"""
    if not bad:
        return 0
    host = cfg.hub_host()
    path = bad_path(cfg)
    seen: set[int] = set()
    try:
        for line in host.read_text(path).splitlines():
            try:
                seen.add(int(json.loads(line).get("line_no")))
            except (ValueError, TypeError, AttributeError):
                continue
    except (FileNotFoundError, HostError):
        seen = set()
    stamp = now_stamp()
    added = 0
    buf: list[str] = []
    for line_no, raw in bad:
        if line_no in seen:
            continue
        buf.append(json.dumps(
            {"line_no": line_no, "at": stamp,
             "raw_b64": base64.b64encode(raw.encode("utf-8")).decode("ascii")},
            ensure_ascii=False))
        seen.add(line_no)
        added += 1
    if buf:
        host.append_bytes(path, ("\n".join(buf) + "\n").encode("utf-8"))
    return added


# ─────────────────────────────────────────────────────────────
# 游标
# ─────────────────────────────────────────────────────────────


def cursor_path(cfg: Config, dev: str) -> Path:
    """游标是**每台自己的私有状态**，所以永远放本机 state 目录。"""
    st = Path(str(cfg.device().state_dir))
    return st / f".chat_cursor_{dev}"


def read_cursor(cfg: Config, dev: str | None = None) -> dict:
    """返回 `{"seq": int, "line": int | None}`。

    向后兼容：老游标文件里只有一个整数（形如 `602`）=> `line=None`，
    这时按 seq 比，行为和旧版一样；下一次写游标会自动升级成两字段。
    """
    dev = dev or cfg.me
    p = cursor_path(cfg, dev)
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except OSError:
        return {"seq": 0, "line": None}
    if not raw:
        return {"seq": 0, "line": None}
    try:
        return {"seq": int(raw), "line": None}
    except ValueError:
        pass
    try:
        d = json.loads(raw)
    except ValueError:
        return {"seq": 0, "line": None}
    if not isinstance(d, dict):
        return {"seq": 0, "line": None}
    seq = d.get("seq")
    line = d.get("line")
    return {
        "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
        "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
    }


def write_cursor(cfg: Config, seq: int, line: int | None, dev: str | None = None) -> None:
    dev = dev or cfg.me
    p = cursor_path(cfg, dev)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"seq": int(seq), "line": line}, ensure_ascii=False),
                 encoding="utf-8")


def new_rows(rows: list[dict], cur: dict) -> list[dict]:
    """比游标新的行。有行号就只认行号（seq 可能撞号），没有才退回比 seq。"""
    line = cur.get("line")
    if isinstance(line, int) and not isinstance(line, bool):
        return [r for r in rows if r["_line"] > line]
    seq = cur.get("seq", 0)
    if not isinstance(seq, int) or isinstance(seq, bool):
        seq = 0
    return [r for r in rows if r["seq"] > seq]


def cursor_after(rows: list[dict], cur: dict) -> dict:
    """读完一批之后游标该在哪。⛔ 不前进就等于下一轮重投一遍。"""
    if not rows:
        return cur
    last = rows[-1]
    return {"seq": int(last.get("seq") or 0), "line": int(last["_line"])}


# ─────────────────────────────────────────────────────────────
# 写
# ─────────────────────────────────────────────────────────────


def check_payload(payload: dict, max_chars: int) -> str:
    """返回空串 = 合规。只查**结构**，⛔ 不查正文措辞。

    为什么只查结构：正文是给人读的，多一条格式要求就多一个
    「为了过闸而改措辞」的动机 —— 那时校验就从「防出错」变成「教人说假话」。
    """
    if not isinstance(payload, dict):
        return f"payload 不是 dict（是 {type(payload).__name__}）"
    missing = [k for k in MSG_FIELDS if k not in payload]
    if missing:
        return f"缺字段 {'、'.join(missing)}"
    for k, typ in MSG_FIELDS.items():
        v = payload[k]
        if not isinstance(v, typ) or isinstance(v, bool):
            return f"{k} 类型不对（要 {typ.__name__}，拿到 {type(v).__name__}）"
    if not payload["from"].strip():
        return "from 是空的（谁说的）"
    if not payload["at"].strip():
        return "at 是空的（什么时候说的）"
    n = len(payload["text"])
    if n > max_chars:
        return f"text {n} 字符，超过 {max_chars}（贴整份文件请走别的通道）"
    if "via" in payload and not isinstance(payload["via"], str):
        return "via 类型不对（要 str）"
    return ""


def append_message(cfg: Config, text: str, src: str = "", via: str = "") -> dict:
    """追加一条。返回写下去的那个 payload。"""
    host = cfg.hub_host()
    rows, _bad, _meta, err = read_all(cfg)
    if err and "读中心失败" in err:
        raise ConfigError(err)
    seq = max((r.get("seq") or 0 for r in rows), default=0) + 1
    payload = {
        "seq": int(seq),
        "from": src or cfg.me,
        "at": now_stamp(),
        "text": text,
    }
    if via and via != payload["from"]:
        payload["via"] = via
    bad = check_payload(payload, cfg.naming.max_text_chars)
    if bad:
        raise ConfigError(f"不合 schema（不写）：{bad}")
    line = json.dumps(payload, ensure_ascii=False)
    if json.loads(line) != payload:
        raise ConfigError("序列化自校验失败（读回来和要写的不一致，不写）")
    host.append_bytes(cfg.hub.path, (line + "\n").encode("utf-8"))
    return payload


def say_file(cfg: Config, path: Path) -> dict:
    """把整份文件当正文写一条。⛔ 不做任何改写，字节照搬。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(f"--say-file 指向的文件不存在：{path}")
    except OSError as e:
        raise ConfigError(f"--say-file 读不了 {path}：{e}")
    return append_message(cfg, text.rstrip("\n"))


# ─────────────────────────────────────────────────────────────
# 心跳
# ─────────────────────────────────────────────────────────────


def touch_beat(cfg: Config, value: float | None = None) -> float:
    ts = time.time() if value is None else float(value)
    cfg.hub_host().write_text(cfg.hub.beat_path, str(ts))
    return ts


def beat_age(cfg: Config) -> tuple[float | None, str]:
    """心跳有多久没更新。文件不存在 => (None, '')，那是「从没起过」，不是坏事。"""
    host = cfg.hub_host()
    try:
        raw = host.read_text(cfg.hub.beat_path).strip()
    except (FileNotFoundError, HostError, OSError):
        return None, ""
    try:
        return max(0.0, time.time() - float(raw)), ""
    except ValueError:
        return None, f"心跳文件内容不是数字：{raw[:60]!r}"


# ─────────────────────────────────────────────────────────────
# 归档
# ─────────────────────────────────────────────────────────────


def archive(cfg: Config, keep: int | None = None, dry: bool = False) -> dict:
    """把老行挪进 `<hub>_旧.jsonl`，并推进 meta 的 first_seq / line_offset。

    [!] 只在中心位于本机时做：这是在**重写**那个共享文件，
       别台正在追加时切一刀会丢数据。远端中心请到那一台上跑。

    返回 {"moved_rows", "moved_lines", "first_seq", "line_offset", "file"}。
    """
    host = cfg.hub_host()
    if not host.local:
        raise ConfigError(
            f"中心在 {cfg.hub.host} 上，不能从本机归档（会与对端的追加撞车）。"
            f"请到 {cfg.hub.host} 上执行同一条命令。"
        )
    keep = cfg.hub.keep_lines if keep is None else int(keep)
    if keep < 1:
        raise ConfigError(f"keep 要 ≥ 1，拿到 {keep}")
    try:
        text = host.read_text(cfg.hub.path)
    except FileNotFoundError:
        return {"moved_rows": 0, "moved_lines": 0, "first_seq": 1,
                "line_offset": 0, "file": str(cfg.hub.path)}
    meta, _merr = read_meta(cfg)
    offset = int(meta.get("line_offset", 0))
    lines = split_lines(text)
    rows, _bad = parse_lines(lines, offset)
    if len(rows) <= keep:
        return {"moved_rows": 0, "moved_lines": 0,
                "first_seq": int(meta.get("first_seq", 1)),
                "line_offset": offset, "file": str(cfg.hub.path)}

    keep_rows = rows[-keep:]
    cut_phys = keep_rows[0]["_line"] - offset - 1   # 要挪走的物理行数
    moved = lines[:cut_phys]
    rest = lines[cut_phys:]
    moved_rows = len(rows) - keep
    first_seq = int(keep_rows[0].get("seq") or 1)
    new_meta = {
        "first_seq": first_seq,
        "line_offset": offset + cut_phys,
        "archived_rows": int(meta.get("archived_rows", 0) or 0) + moved_rows,
        "archived_at": now_stamp(),
    }
    if dry:
        return {"moved_rows": moved_rows, "moved_lines": cut_phys,
                "first_seq": first_seq, "line_offset": new_meta["line_offset"],
                "file": str(cfg.hub.path), "dry": True}

    if moved:
        host.append_bytes(archive_path(cfg),
                          ("\n".join(moved) + "\n").encode("utf-8"))
    host.write_bytes(cfg.hub.path, ("\n".join(rest) + "\n").encode("utf-8"))
    write_meta(cfg, new_meta)
    return {"moved_rows": moved_rows, "moved_lines": cut_phys,
            "first_seq": first_seq, "line_offset": new_meta["line_offset"],
            "file": str(cfg.hub.path)}


# ─────────────────────────────────────────────────────────────
# 输出
# ─────────────────────────────────────────────────────────────


def fmt_row(r: dict) -> str:
    via = f"（经 {r['via']}）" if r.get("via") and r["via"] != r.get("from") else ""
    return f"#{r.get('seq')} [{r.get('at', '?')}] {r.get('from', '?')}{via}: {r.get('text', '')}"


def cmd_status(cfg: Config) -> int:
    rows, bad, meta, merr = read_all(cfg)
    cur = read_cursor(cfg)
    pending = new_rows(rows, cur)
    total = max((r.get("seq") or 0 for r in rows), default=0)
    print(describe(cfg))
    print(f"消息条数：{len(rows)} · 最大 seq：{total}"
          f"（物理行数会更大：坏行与空行占号不算消息）")
    if bad:
        print(f"[!] 坏行 {len(bad)} 条（行号：{'、'.join(str(b[0]) for b in bad[:10])}"
              f"{'…' if len(bad) > 10 else ''}）")
    ok = not merr
    meta_line = (f"first_seq={meta.get('first_seq', 1)} · "
                 f"line_offset={meta.get('line_offset', 0)}")
    print(f"  {'[OK]' if ok else '[X]'} 归档元数据：{merr or meta_line}")
    print(f"游标 {cfg.me}：seq={cur.get('seq')} · line={cur.get('line')} · 差 {len(pending)} 条")
    if len(rows) >= cfg.hub.warn_lines:
        print(f"[!] 中心已有 {len(rows)} 行（提醒线 {cfg.hub.warn_lines}）—— 该归档了。"
              f"⛔ seq 必须继续递增，别重排。")
    age, berr = beat_age(cfg)
    if berr:
        print(f"[X] 心跳：{berr}")
    elif age is None:
        print("心跳：（没有 .beat 文件 —— 守护没起过，或从没摸过）")
    elif age > cfg.hub.beat_deadline_seconds:
        print(f"[X] 心跳：{age:.0f}s 前（判死线 {cfg.hub.beat_deadline_seconds}s）"
              f"—— 守护活着但没在干活，比进程崩了更难查")
    else:
        print(f"[OK] 心跳：{age:.0f}s 前")
    return 0


def cmd_read(cfg: Config, commit: bool = True, quiet: bool = False) -> int:
    rows, bad, _meta, err = read_all(cfg)
    if err and "读中心失败" in err:
        print(f"[X] {err}", file=sys.stderr)
        return 2
    added = record_bad(cfg, bad)
    if added and not quiet:
        print(f"[note] 已把 {added} 条坏行原文记进 {bad_path(cfg)}", file=sys.stderr)
    cur = read_cursor(cfg)
    fresh = new_rows(rows, cur)
    for r in fresh:
        print(fmt_row(r))
    if commit and fresh:
        nc = cursor_after(fresh, cur)
        write_cursor(cfg, nc["seq"], nc["line"])
    if not quiet:
        print(f"（新 {len(fresh)} 条 · 共 {len(rows)} 条）")
    return 0


def cmd_tail(cfg: Config, n: int) -> int:
    rows, _bad, _meta, err = read_all(cfg)
    if err and "读中心失败" in err:
        print(f"[X] {err}", file=sys.stderr)
        return 2
    for r in rows[-n:]:
        print(fmt_row(r))
    print(f"（共 {len(rows)} 条）")
    return 0


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bridge.chat",
        description="共享聊天室：写一条 / 按游标读 / 看状态 / 归档。",
    )
    p.add_argument("--config", default=None, help="bridge.toml 路径（默认自动找）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--say", metavar="TEXT", help="写一条。TEXT 为 - 时从 stdin 读（含反斜杠更安全）")
    g.add_argument("--say-file", metavar="PATH", help="把整份文件当正文写一条")
    g.add_argument("--tail", type=int, metavar="N", help="最后 N 条")
    g.add_argument("--read", action="store_true", help="读游标之后的新消息并推进游标")
    g.add_argument("--status", action="store_true", help="状态摘要")
    g.add_argument("--archive", action="store_true",
                   help="把老行挪进 <hub>_旧.jsonl（只在中心所在的机器上跑）")
    g.add_argument("--beat", action="store_true", help="摸一次心跳（写 .beat）")
    g.add_argument("--where", action="store_true", help="只打印解析后的配置")
    p.add_argument("--keep", type=int, default=None,
                   help="--archive 保留多少条（默认取 [hub].keep_lines）")
    p.add_argument("--dry-run", action="store_true", help="只打印将做什么，不落盘")
    p.add_argument("--from", dest="src", default="", help="这条消息的署名（默认取本机设备 key）")
    return p


def main(argv: list[str] | None = None) -> int:
    enable_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"[X] {e}", file=sys.stderr)
        return 2

    try:
        if args.where:
            print(describe(cfg))
            return 0
        if args.status:
            return cmd_status(cfg)
        if args.beat:
            ts = touch_beat(cfg)
            print(f"[OK] 心跳已写：{cfg.hub.beat_path} = {ts:.3f}")
            return 0
        if args.tail is not None:
            return cmd_tail(cfg, max(1, args.tail))
        if args.read:
            return cmd_read(cfg)
        if args.archive:
            info = archive(cfg, keep=args.keep, dry=args.dry_run)
            tag = "（空跑，未落盘）" if info.get("dry") else ""
            print(f"归档{tag}：挪走 {info['moved_rows']} 条 / {info['moved_lines']} 行 · "
                  f"first_seq={info['first_seq']} · line_offset={info['line_offset']}")
            return 0
        if args.say_file:
            payload = say_file(cfg, Path(args.say_file))
            print(f"[OK] 已写 #{payload['seq']}（{len(payload['text'])} 字符）→ {cfg.hub.path}")
            return 0
        if args.say is not None:
            text = sys.stdin.read() if args.say == "-" else args.say
            if not text.strip():
                print("[X] 正文是空的，不写", file=sys.stderr)
                return 2
            payload = append_message(cfg, text.rstrip("\n"), src=args.src)
            print(f"[OK] 已写 #{payload['seq']}（{len(payload['text'])} 字符）→ {cfg.hub.path}")
            return 0
    except (ConfigError, HostError) as e:
        print(f"[X] {e}", file=sys.stderr)
        return 2

    build_parser().print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
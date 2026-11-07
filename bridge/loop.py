"""守护：按固定间隔跑一轮「投递 + 收件归位」，并把状态写成 json。

为什么要有它（对应实机上换来的三条）：

① **间隔要可配**（`[loop].interval_seconds`）：电脑侧靠计划任务每 120 秒起一次，
   手机/平板靠 `setsid nohup` 常驻。同一份代码两种活法，间隔只能来自配置。

② **单实例**：两轮重叠会抢同一批信（判重是「读-判-写」，不原子）。
   这里用内核文件锁，⛔ 不靠 pid 文件判活 —— pid 会复用，
   「活着但没干活」和「压根没起」在 pid 文件里长得一模一样。

③ **状态写 json**：`--status` 要能回答「上一轮是什么时候、结果如何」。
   只写 pid 等于只知道「它起过」，不知道「它现在还在不在干活」。

⚠️ 锁复用：本进程每轮都拿一次锁，所以锁对象在每轮结束必须释放。
   `fcntl`/`msvcrt` 的锁绑的是「打开的文件描述」而不是进程 ——
   同一个进程开两次、拿两次，会**排斥自己**。
   那种「日志正常、进程在跑、一个件都不投」的死法比崩溃难查十倍。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from .config import (
    Config,
    ConfigError,
    HostError,
    describe,
    enable_utf8_stdio,
    load_config,
    now_stamp,
)
from . import chat as chat_mod
from . import post as post_mod
from . import receive as receive_mod

LOCK_NAME = "dsh_bridge_loop.lock"


def lock_path(cfg: Config) -> Path:
    return Path(str(cfg.device().state_dir)) / LOCK_NAME


def status_path(cfg: Config) -> Path:
    return Path(cfg.loop.status_file)


def pid_path(cfg: Config) -> Path:
    return Path(cfg.loop.pid_file)


def write_status(cfg: Config, data: dict) -> bool:
    p = status_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return True
    except OSError:
        return False


def read_status(cfg: Config) -> dict:
    try:
        j = json.loads(status_path(cfg).read_text(encoding="utf-8"))
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


def pid_alive(pid: int) -> bool:
    """pid 还活着没有。

    ⚠️ 只回答「这个号上有没有进程」，⛔ 不回答「那是我们的守护」——
       pid 会复用。判断是不是本守护要靠**锁**（拿得到锁 = 它没在跑），
       两个判据各答一个问题，别让一个兼职。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def run_cycle(cfg: Config, do_receive: bool = True, beat: bool = False) -> dict:
    """跑一轮。⛔ 不吞异常：把失败写进结果，让下一轮的 --status 看得见。"""
    started = now_stamp()
    result: dict = {"at": started, "post": None, "receive": None, "beat": None, "error": ""}
    t0 = time.time()
    try:
        if beat:
            chat_mod.touch_beat(cfg)
            result["beat"] = now_stamp()
        result["post"] = post_mod.post_once(cfg, dry=False)
        if do_receive:
            result["receive"] = receive_mod.receive_once(cfg, dry=False)
    except (ConfigError, HostError) as e:
        result["error"] = f"{type(e).__name__}: {e}"
    except Exception as e:  # noqa: BLE001 - 守护不能因为一轮意外就死掉
        result["error"] = f"{type(e).__name__}: {e}"
    result["rc"] = 0 if not result["error"] else 1
    result["seconds"] = round(time.time() - t0, 2)
    # 一轮跑得比 `once_timeout_seconds` 还久 => 下一轮会压在上一轮屁股上。
    # ⛔ 不因此杀掉它：杀掉只会让「慢」变成「失败」，而慢通常是链路的事。
    if result["seconds"] > cfg.loop.once_timeout_seconds:
        result["error"] = (result["error"] + "；" if result["error"] else "") + \
            f"这一轮用了 {result['seconds']}s，超过 once_timeout_seconds={cfg.loop.once_timeout_seconds}"
        result["rc"] = 1
    return result


def loop_forever(cfg: Config, do_receive: bool, beat: bool, max_rounds: int = 0) -> int:
    lk = post_mod.PostLock(lock_path(cfg))
    try:
        lk.__enter__()
    except post_mod.AlreadyRunning:
        print("[!] 已有一个守护在跑（锁被占），这次不起第二份")
        return 0
    except RuntimeError as e:
        print(f"[X] {e}", file=sys.stderr)
        return 2

    stop = {"flag": False}

    def _on_signal(signum, _frame):
        stop["flag"] = True
        print(f"[!] 收到信号 {signum}，这一轮跑完就停")

    for sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig):
            try:
                signal.signal(getattr(signal, sig), _on_signal)
            except (ValueError, OSError):
                pass

    pid_p = pid_path(cfg)
    pid_p.parent.mkdir(parents=True, exist_ok=True)
    pid_p.write_text(str(os.getpid()), encoding="utf-8")

    started_at = now_stamp()
    runs = 0
    errors = 0
    print(f"守护启动：pid={os.getpid()} · 间隔 {cfg.loop.interval_seconds}s · "
          f"配置 {cfg.path}")
    try:
        while not stop["flag"]:
            t0 = time.time()
            res = run_cycle(cfg, do_receive=do_receive, beat=beat)
            runs += 1
            if res["rc"]:
                errors += 1
                print(f"[{res['at']}] [!] 本轮有事：{res['error'] or '见投递输出'}")
            else:
                p = res.get("post") or {}
                print(f"[{res['at']}] 候选 {p.get('candidates', 0)} · "
                      f"投出 {p.get('delivered', 0)} · 失败 {p.get('failed', 0)} · "
                      f"第 {runs} 轮")
            write_status(cfg, {
                "pid": os.getpid(),
                "started_at": started_at,
                "last_run_at": res["at"],
                "last_rc": res["rc"],
                "runs": runs,
                "errors": errors,
                "interval_seconds": cfg.loop.interval_seconds,
                "next_run_in_seconds": cfg.loop.interval_seconds,
                "last": res,
            })
            if max_rounds and runs >= max_rounds:
                break
            slept = 0.0
            while slept < cfg.loop.interval_seconds and not stop["flag"]:
                time.sleep(min(1.0, cfg.loop.interval_seconds - slept))
                slept += 1.0
    finally:
        try:
            pid_p.unlink()
        except OSError:
            pass
        write_status(cfg, {
            "pid": 0,
            "started_at": started_at,
            "last_run_at": now_stamp(),
            "last_rc": 0,
            "runs": runs,
            "errors": errors,
            "stopped_at": now_stamp(),
        })
        lk.__exit__(None, None, None)
        print(f"守护停止：跑了 {runs} 轮（失败 {errors} 轮）")
    return 0


def cmd_status(cfg: Config) -> int:
    print(describe(cfg))
    st = read_status(cfg)
    pid = 0
    try:
        pid = int(pid_path(cfg).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        pid = int(st.get("pid") or 0)
    alive = pid_alive(pid)
    # 锁能拿到 => 没有守护在跑（比 pid 可靠）
    running = False
    lk = post_mod.PostLock(lock_path(cfg))
    try:
        lk.__enter__()
        lk.__exit__(None, None, None)
    except post_mod.AlreadyRunning:
        running = True
    except RuntimeError:
        running = None
    print(f"守护：{'在跑' if running else ('没在跑' if running is False else '判不了（锁文件打不开）')}"
          f" · pid={pid or '（无）'}{'（这个号上确实有进程）' if alive else ''}")
    if st:
        print(f"  起于 {st.get('started_at', '?')} · 上一轮 {st.get('last_run_at', '?')} · "
              f"共 {st.get('runs', 0)} 轮 · 失败 {st.get('errors', 0)} 轮")
        last = st.get("last") or {}
        if last.get("error"):
            print(f"  [!] 上一轮有错：{last['error']}")
    else:
        print("  （没有状态文件：从没跑过，或状态文件被删了）")
    age, berr = chat_mod.beat_age(cfg)
    if berr:
        print(f"[X] 心跳：{berr}")
    elif age is None:
        print("心跳：（没有 .beat 文件）")
    elif age > cfg.hub.beat_deadline_seconds:
        print(f"[X] 心跳：{age:.0f}s 前（判死线 {cfg.hub.beat_deadline_seconds}s）")
    else:
        print(f"[OK] 心跳：{age:.0f}s 前")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bridge.loop",
        description="守护：每轮投递 + 收件归位，单实例锁 + pid + 状态 json。",
    )
    p.add_argument("--config", default=None, help="bridge.toml 路径（默认自动找）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--start", action="store_true", help="前台常驻（计划任务/setsid 都起这个）")
    g.add_argument("--once", action="store_true", help="只跑一轮就退出（给计划任务用）")
    g.add_argument("--status", action="store_true", help="状态")
    p.add_argument("--no-receive", action="store_true", help="这一轮不做收件归位")
    p.add_argument("--beat", action="store_true", help="每轮顺手摸一次聊天室心跳")
    p.add_argument("--rounds", type=int, default=0, help="跑几轮就停（0 = 不停，调试用）")
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
    if args.once:
        try:
            with post_mod.PostLock(lock_path(cfg)):
                res = run_cycle(cfg, do_receive=not args.no_receive, beat=args.beat)
        except post_mod.AlreadyRunning:
            print("（已有一个守护在跑，这一轮跳过）")
            return 0
        except RuntimeError as e:
            print(f"[X] {e}", file=sys.stderr)
            return 2
        p = res.get("post") or {}
        r = res.get("receive") or {}
        print(f"[{res['at']}] 候选 {p.get('candidates', 0)} · 投出 {p.get('delivered', 0)} · "
              f"失败 {p.get('failed', 0)} · 归位 {r.get('placed', 0)}"
              + (f" · [!] {res['error']}" if res["error"] else ""))
        return res["rc"]
    if args.start:
        return loop_forever(cfg, do_receive=not args.no_receive, beat=args.beat,
                            max_rounds=args.rounds)
    build_parser().print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
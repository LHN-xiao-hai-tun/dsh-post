"""配置加载与主机访问。

**这一层是整包唯一知道「路径长什么样」的地方。**
`bridge.toml` 里的每个路径都在这里解析，其它模块只拿路径对象 / `Host`，
因此代码里没有第二处可以写死路径 —— 想写死也没有地方写。

三种访问方式（`[general].transport`）：

  ssh    多机部署（默认）。本机直接读写磁盘，别台走系统 `ssh`。
  local  单机双目录试跑。两台「设备」都在同一个文件系统上，不起 ssh。
         `tests/` 用的就是这一档，这也是「不依赖 ssh 也能自测」的实现方式。

[!] 本机（`me`）的路径用 `pathlib.Path`（Windows 上就是 WindowsPath）；
   远端设备的路径用 `PurePosixPath`。**这不是洁癖**：
   Windows 的 `Path("/a/b")` 一 `str()` 就变成 `\a\b`，
   把它塞进 `ssh ... cat` 里会把远端路径改坏，而且报错看起来像是「文件不存在」。

零第三方依赖：只用标准库 + 系统 `ssh`。Python ≥ 3.11（`tomllib`）。
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shlex
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Union

PathLike = Union[Path, PurePosixPath]

SCHEMA_VERSION = 1

CONFIG_ENV = "DSH_BRIDGE_CONFIG"
DEVICE_ENV = "DSH_BRIDGE_DEVICE"
DEFAULT_CONFIG_NAMES = ("bridge.toml",)

# Windows 上 `ssh` 是控制台子系统程序：不加这个标志，每次调用都会弹一个黑窗口。
# 这是我们自己在实机上踩过的坑（计划任务每轮弹一次，屏幕上会闪个不停）。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class ConfigError(Exception):
    """配置有毛病。消息要说清「哪个键、哪个文件」，⛔ 不要只说『配置错误』。"""


class HostError(RuntimeError):
    """连不上 / 读不到。消息里要带别名和原始输出，⛔ 别只说『失败』。"""


def now_stamp() -> str:
    """统一的时间戳口径：`YYYY-MM-DD HH:MM:SS`，本地时间。

    与既有 `hub.jsonl` 里 `at` 字段同一口径 => 新包能直接读老数据。
    """
    return time.strftime("%Y-%m-%d %H:%M:%S")


def enable_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8。

    [!] 这不是洁癖：Windows 控制台默认代码页常常是 936/GBK，
       `print` 一个 GBK 编不出的字符（箭头、勾叉）会直接抛
       `UnicodeEncodeError` 把命令打死 —— 一个只负责打印的字符
       不该能让投递失败。`errors="replace"` 保证最坏情况是少个字符。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_absolute_like(s: str) -> bool:
    """跨平台的「像绝对路径」判断。

    [!] 不用 `Path(s).is_absolute()`：Windows 上它把一个以斜杠开头的 POSIX 路径判成 False，
       于是一份正常的 Linux 设备配置会被误报成「相对路径」。
    """
    s = s.strip()
    return s.startswith("/") or s.startswith("\\\\") or bool(_DRIVE_RE.match(s))


def _local_resolve(base: Path, value: str) -> Path:
    p = Path(value.strip())
    if not p.is_absolute():
        p = base / p
    return Path(os.path.normpath(str(p)))


def _posix_resolve(base: str, value: str) -> PurePosixPath:
    v = value.strip()
    if not is_absolute_like(v):
        v = posixpath.join(base.rstrip("/") or "/", v)
    return PurePosixPath(posixpath.normpath(v))


# ─────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────


@dataclass
class Device:
    """一台设备。路径已解析：本机是 `Path`，远端是 `PurePosixPath`。"""

    key: str
    name: str
    workspace: PathLike
    aliases: list[str]
    inbox: PathLike
    outbox: PathLike
    sent_dir: PathLike
    state_dir: PathLike
    home: str = ""


@dataclass
class Hub:
    """聊天室中心文件（各台共享的那一个 jsonl）。"""

    host: str                     # "" = 就是 [general].me 那台；否则是设备 key
    path: PathLike
    beat_path: PathLike
    beat_deadline_seconds: int
    warn_lines: int
    keep_lines: int


@dataclass
class LoopSettings:
    interval_seconds: int
    pid_file: Path
    status_file: Path
    once_timeout_seconds: int


@dataclass
class ReceiveSettings:
    layout: str                   # subdir | flat
    backend: str                  # local | posix-remote
    processed_dir: str            # "" = 不搬原件


@dataclass
class Naming:
    act_kinds: list[str]
    enforce_since: str            # "" = 不按日期宽限；否则形如 20261105
    max_text_chars: int


@dataclass
class PostSettings:
    """投递策略。阈值也外置 —— 「30 分钟算积压」是判断，不是事实。"""

    stale_minutes: int
    exts: list[str]
    verify: bool


@dataclass
class Config:
    path: Path
    root: Path
    me: str
    transport: str
    ssh_timeout_seconds: int
    devices: dict[str, Device]
    hub: Hub
    loop: LoopSettings
    receive: ReceiveSettings
    naming: Naming
    post: PostSettings
    recipients: dict[str, list[str]]
    warnings: list[str] = field(default_factory=list)

    # ── 查询 ──
    def device(self, key: str | None = None) -> Device:
        k = key or self.me
        if k not in self.devices:
            raise ConfigError(
                f"设备 {k!r} 不在设备表里（有：{'、'.join(sorted(self.devices))}）"
            )
        return self.devices[k]

    def host(self, key: str | None = None) -> "Host":
        return Host(self.device(key), self)

    def hub_host(self) -> "Host":
        return Host(self.device(self.hub.host or self.me), self)

    def device_for_alias(self, alias: str) -> Device | None:
        for d in self.devices.values():
            if alias == d.key or alias in d.aliases:
                return d
        return None


# ─────────────────────────────────────────────────────────────
# 主机访问：local 直连磁盘 / ssh 走系统客户端
# ─────────────────────────────────────────────────────────────


class Host:
    r"""一台设备上的文件访问。

    [!] 正文**一律走 stdin**，⛔ 不拼进 ssh 的 argv：
        Windows 的 argv 会把 `\\` 折成 `\`，而 `json.dumps` 恰好把字面反斜杠
        写成 `\\` —— 一条含反斜杠的消息会被静默改成真换行，而且看不出来。
        （这是原实现里实测过的坑，新包沿用同一条纪律。）
    """

    def __init__(self, device: Device, config: Config):
        self.device = device
        self.config = config

    # ── 判定 ──
    @property
    def local(self) -> bool:
        return self.config.transport == "local" or self.device.key == self.config.me

    @property
    def label(self) -> str:
        if self.local:
            return "本机"
        first = self.device.aliases[0] if self.device.aliases else "无别名"
        return f"{self.device.key}({first})"

    # ── 底层 ──
    def _ssh(self, alias: str, cmd: str, stdin: bytes | None = None,
             timeout: int | None = None) -> tuple[int, bytes, bytes]:
        t = timeout or self.config.ssh_timeout_seconds
        argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={t}", alias, "--", cmd]
        try:
            p = subprocess.run(
                argv,
                input=stdin if stdin is not None else b"",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=t + 10,
                creationflags=_NO_WINDOW,
            )
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError as e:
            raise HostError(f"本机没有 ssh 命令，连不上 {alias}：{e}") from e
        except subprocess.TimeoutExpired:
            return 124, b"", f"ssh {alias} 超时（{t}s）".encode()

    def run(self, cmd: str, stdin: bytes | None = None,
            timeout: int | None = None) -> tuple[int, bytes, bytes]:
        """在设备上执行一条命令。按 `aliases` 顺序试，第一个成功的算数。"""
        if self.local:
            raise HostError("run() 只对远端设备有意义；本机请直接用 pathlib")
        if not self.device.aliases:
            raise HostError(f"设备 {self.device.key} 没配 aliases，无法 ssh 过去")
        last: tuple[int, bytes, bytes] = (255, b"", b"")
        for alias in self.device.aliases:
            rc, out, err = self._ssh(alias, cmd, stdin=stdin, timeout=timeout)
            if rc == 0:
                return rc, out, err
            last = (rc, out, err)
        raise HostError(
            f"ssh 到 {self.device.key} 全部别名都失败"
            f"（试过 {'、'.join(self.device.aliases)}）："
            f"{last[2].decode('utf-8', 'replace')[:300]}"
        )

    # ── 文件操作 ──
    def read_bytes(self, path: PathLike) -> bytes:
        if self.local:
            return Path(path).read_bytes()
        rc, out, err = self.run(f"cat {shlex.quote(str(path))}")
        return out

    def read_text(self, path: PathLike) -> str:
        return self.read_bytes(path).decode("utf-8", "replace")

    def exists(self, path: PathLike) -> bool:
        if self.local:
            return Path(path).exists()
        rc, out, err = self.run(f"test -e {shlex.quote(str(path))} && echo YES || echo NO")
        return b"YES" in out

    def append_bytes(self, path: PathLike, data: bytes) -> None:
        if self.local:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("ab") as fh:
                fh.write(data)
            return
        self.run(f"cat >> {shlex.quote(str(path))}", stdin=data)

    def write_bytes(self, path: PathLike, data: bytes) -> None:
        if self.local:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp-dshbridge")
            tmp.write_bytes(data)
            tmp.replace(p)
            return
        self.run(f"cat > {shlex.quote(str(path))}", stdin=data)

    def write_text(self, path: PathLike, text: str) -> None:
        self.write_bytes(path, text.encode("utf-8"))

    def mkdir(self, path: PathLike) -> None:
        if self.local:
            Path(path).mkdir(parents=True, exist_ok=True)
            return
        self.run(f"mkdir -p {shlex.quote(str(path))}")

    def list_files(self, path: PathLike) -> list[str]:
        """列目录里的**普通文件名**（不含子目录）。目录不存在 => 空列表。"""
        if self.local:
            p = Path(path)
            if not p.is_dir():
                return []
            return sorted(x.name for x in p.iterdir() if x.is_file())
        rc, out, err = self.run(
            f"if [ -d {shlex.quote(str(path))} ]; then ls -1A {shlex.quote(str(path))}; fi"
        )
        return [x for x in out.decode("utf-8", "replace").splitlines() if x.strip()]

    def digest(self, path: PathLike) -> str:
        """文件 md5。远端优先 `md5sum`，退回 `certutil`（Windows 远端的兜底），
        再退回读回来自己算。三者都失败就返回空串 —— ⛔ 不返回一个假的哈希。"""
        if self.local:
            try:
                return md5_file(Path(path))
            except OSError:
                return ""
        q = shlex.quote(str(path))
        for cmd in (
            f"md5sum {q} 2>/dev/null | cut -d' ' -f1",
            f"certutil -hashfile {q} MD5 2>/dev/null | sed -n 2p | tr -d ' \\r'",
        ):
            try:
                rc, out, _ = self.run(cmd)
            except HostError:
                continue
            s = out.decode("utf-8", "replace").strip()
            if rc == 0 and len(s) == 32 and all(c in "0123456789abcdef" for c in s.lower()):
                return s.lower()
        try:
            return md5_bytes(self.read_bytes(path))
        except (HostError, FileNotFoundError, OSError):
            return ""


# ─────────────────────────────────────────────────────────────
# 加载
# ─────────────────────────────────────────────────────────────


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """找配置：命令行 > 环境变量 > 当前目录 > 本包旁边。都找不到就报错。"""
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ConfigError(f"--config 指向的文件不存在：{p}")
        return p.resolve()
    env = os.environ.get(CONFIG_ENV)
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise ConfigError(f"环境变量 {CONFIG_ENV} 指向的文件不存在：{p}")
        return p.resolve()
    here = Path(__file__).resolve().parent
    for base in (Path.cwd(), here.parent):
        for name in DEFAULT_CONFIG_NAMES:
            cand = base / name
            if cand.is_file():
                return cand.resolve()
    raise ConfigError(
        "找不到 bridge.toml。三选一："
        "① 在它所在目录运行；② 用 --config 指定；"
        f"③ 设环境变量 {CONFIG_ENV}。模板见仓里的 bridge.toml.example。"
    )


def _need_str(section: dict, key: str, where: str, allow_empty: bool = False) -> str:
    v = section.get(key, "")
    if not isinstance(v, str):
        raise ConfigError(f"{where}.{key} 要字符串，拿到 {v!r}")
    if not allow_empty and not v.strip():
        raise ConfigError(f"{where}.{key} 不能为空")
    return v.strip()


def _str(section: dict, key: str, default: str, where: str) -> str:
    v = section.get(key, default)
    if not isinstance(v, str):
        raise ConfigError(f"{where}.{key} 要字符串，拿到 {v!r}")
    return v


def _int(section: dict, key: str, default: int, where: str, minimum: int = 0) -> int:
    v = section.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{where}.{key} 要整数，拿到 {v!r}")
    if v < minimum:
        raise ConfigError(f"{where}.{key} 要 ≥ {minimum}，拿到 {v}")
    return v


def _str_list(section: dict, key: str, default: list[str], where: str) -> list[str]:
    v = section.get(key, default)
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise ConfigError(f"{where}.{key} 要非空字符串列表，拿到 {v!r}")
    return [x.strip() for x in v]


def load_config(explicit: str | Path | None = None) -> Config:
    path = resolve_config_path(explicit)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise ConfigError(f"读不了 {path}：{e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} 不是合法 TOML：{e}") from e

    warnings: list[str] = []
    gen = raw.get("general", {})
    if not isinstance(gen, dict):
        raise ConfigError("[general] 要是一张表")

    root = _local_resolve(path.parent, _str(gen, "root", ".", "[general]") or ".")
    transport = _str(gen, "transport", "ssh", "[general]").strip().lower()
    if transport not in ("ssh", "local"):
        raise ConfigError(f"[general].transport 只能是 ssh 或 local，拿到 {transport!r}")
    ssh_timeout = _int(gen, "ssh_timeout_seconds", 15, "[general]", minimum=1)
    state_dir_raw = _str(gen, "state_dir", ".dsh/state", "[general]").strip() or ".dsh/state"

    devs_raw = raw.get("device", {})
    if not isinstance(devs_raw, dict) or not devs_raw:
        raise ConfigError("至少要配一个 [device.<key>]")
    for key, d in devs_raw.items():
        if not isinstance(d, dict):
            raise ConfigError(f"[device.{key}] 要是一张表")

    # ── 先认本机，再决定路径怎么解析 ──
    env_me = os.environ.get(DEVICE_ENV, "").strip()
    me = env_me or _str(gen, "me", "", "[general]").strip()
    if me:
        if me not in devs_raw:
            raise ConfigError(
                f"本机设备 {me!r} 不在设备表里（有：{'、'.join(sorted(devs_raw))}）"
            )
    else:
        me = _detect_me(devs_raw, root, transport)

    devices: dict[str, Device] = {}
    for key, d in devs_raw.items():
        where = f"[device.{key}]"
        local_side = (transport == "local") or (key == me)
        ws_raw = _need_str(d, "workspace", where)
        if local_side:
            workspace = _local_resolve(root, ws_raw)
            if not Path(str(workspace)).is_absolute():
                raise ConfigError(f"{where}.workspace 解析后不是绝对路径：{workspace}")
            inbox_raw = _need_str(d, "inbox", where)
            outbox_raw = _need_str(d, "outbox", where)
            inbox = _local_resolve(Path(workspace), inbox_raw)
            outbox = _local_resolve(Path(workspace), outbox_raw)
            sent_raw = _str(d, "sent_dir", "", where).strip()
            sent_dir = (_local_resolve(Path(workspace), sent_raw) if sent_raw
                        else Path(outbox) / "已发")
            state_dir = _local_resolve(Path(workspace), state_dir_raw)
        else:
            if not is_absolute_like(ws_raw):
                raise ConfigError(
                    f"{where}.workspace 是给**别台**用的路径，必须写绝对路径"
                    f"（拿到 {ws_raw!r}）—— 相对路径只会按本机的 root 展开，那在这里是错的"
                )
            workspace_p = PurePosixPath(posixpath.normpath(ws_raw))
            inbox = _posix_resolve(str(workspace_p), _need_str(d, "inbox", where))
            outbox = _posix_resolve(str(workspace_p), _need_str(d, "outbox", where))
            sent_raw = _str(d, "sent_dir", "", where).strip()
            sent_dir = (_posix_resolve(str(workspace_p), sent_raw) if sent_raw
                        else outbox / "已发")
            state_dir = _posix_resolve(str(workspace_p), state_dir_raw)
            workspace = workspace_p

        devices[key] = Device(
            key=key,
            name=_str(d, "name", key, where) or key,
            workspace=workspace,
            aliases=_str_list(d, "aliases", [], where),
            inbox=inbox,
            outbox=outbox,
            sent_dir=sent_dir,
            state_dir=state_dir,
            home=_str(d, "home", "", where),
        )

    me_dev = devices[me]
    if transport == "ssh" and not me_dev.aliases:
        warnings.append(
            f"本机 {me} 没配 aliases —— 别台要连你就需要它（ssh config 里的别名）。"
        )

    # ── 中心 ──
    hub_raw = raw.get("hub", {})
    if not isinstance(hub_raw, dict):
        raise ConfigError("[hub] 要是一张表")
    hub_host = _str(hub_raw, "host", "", "[hub]").strip()
    if hub_host and hub_host not in devices:
        raise ConfigError(f"[hub].host = {hub_host!r} 不在设备表里")
    hub_dev = devices[hub_host or me]
    hub_local = (transport == "local") or (hub_dev.key == me)
    hub_path_raw = _need_str(hub_raw, "path", "[hub]")
    if hub_local:
        hub_path = _local_resolve(Path(str(hub_dev.workspace)), hub_path_raw)
        beat_raw = _str(hub_raw, "beat_path", "", "[hub]").strip()
        beat_path = (_local_resolve(Path(str(hub_path)).parent, beat_raw) if beat_raw
                     else Path(str(hub_path)).parent / ".chat.beat")
    else:
        hub_path = _posix_resolve(str(hub_dev.workspace), hub_path_raw)
        beat_raw = _str(hub_raw, "beat_path", "", "[hub]").strip()
        beat_path = (_posix_resolve(str(PurePosixPath(str(hub_path)).parent), beat_raw)
                     if beat_raw else PurePosixPath(str(hub_path)).parent / ".chat.beat")

    hub = Hub(
        host=hub_host,
        path=hub_path,
        beat_path=beat_path,
        beat_deadline_seconds=_int(hub_raw, "beat_deadline_seconds", 130, "[hub]", minimum=1),
        warn_lines=_int(hub_raw, "warn_lines", 5000, "[hub]", minimum=1),
        keep_lines=_int(hub_raw, "keep_lines", 5000, "[hub]", minimum=1),
    )

    # ── 守护 ──
    loop_raw = raw.get("loop", {})
    if not isinstance(loop_raw, dict):
        raise ConfigError("[loop] 要是一张表")
    pid_raw = _str(loop_raw, "pid_file", "", "[loop]").strip()
    status_raw = _str(loop_raw, "status_file", "", "[loop]").strip()
    loop = LoopSettings(
        interval_seconds=_int(loop_raw, "interval_seconds", 120, "[loop]", minimum=5),
        pid_file=(_local_resolve(Path(str(me_dev.state_dir)), pid_raw) if pid_raw
                  else Path(str(me_dev.state_dir)) / "dsh_bridge_loop.pid"),
        status_file=(_local_resolve(Path(str(me_dev.state_dir)), status_raw) if status_raw
                     else Path(str(me_dev.state_dir)) / "dsh_bridge_loop.json"),
        once_timeout_seconds=_int(loop_raw, "once_timeout_seconds", 90, "[loop]", minimum=5),
    )

    # ── 收件 ──
    recv_raw = raw.get("receive", {})
    if not isinstance(recv_raw, dict):
        raise ConfigError("[receive] 要是一张表")
    layout = _str(recv_raw, "layout", "subdir", "[receive]").strip().lower()
    if layout not in ("subdir", "flat"):
        raise ConfigError(f"[receive].layout 只能是 subdir 或 flat，拿到 {layout!r}")
    backend = _str(recv_raw, "backend", "local", "[receive]").strip().lower()
    if backend not in ("local", "posix-remote"):
        raise ConfigError(f"[receive].backend 只能是 local 或 posix-remote，拿到 {backend!r}")
    receive = ReceiveSettings(
        layout=layout,
        backend=backend,
        processed_dir=_str(recv_raw, "processed_dir", "", "[receive]").strip(),
    )

    # ── 命名 ──
    naming_raw = raw.get("naming", {})
    if not isinstance(naming_raw, dict):
        raise ConfigError("[naming] 要是一张表")
    naming = Naming(
        act_kinds=_str_list(naming_raw, "act_kinds",
                            ["信", "回话", "同步", "知会"], "[naming]"),
        enforce_since=_str(naming_raw, "enforce_since", "", "[naming]").strip(),
        max_text_chars=_int(naming_raw, "max_text_chars", 20000, "[naming]", minimum=1),
    )

    # ── 投递策略 ──
    post_raw = raw.get("post", {})
    if not isinstance(post_raw, dict):
        raise ConfigError("[post] 要是一张表")
    exts = _str_list(post_raw, "exts", [".md"], "[post]")
    for e in exts:
        if not e.startswith("."):
            raise ConfigError(f"[post].exts 的每一项都要以点开头（拿到 {e!r}）")
    verify_raw = post_raw.get("verify", True)
    if not isinstance(verify_raw, bool):
        raise ConfigError(f"[post].verify 要 true/false，拿到 {verify_raw!r}")
    post = PostSettings(
        stale_minutes=_int(post_raw, "stale_minutes", 30, "[post]", minimum=1),
        exts=exts,
        verify=verify_raw,
    )

    # ── 收件方 ──
    recipients: dict[str, list[str]] = {}
    rec_raw = raw.get("recipients", {})
    if not isinstance(rec_raw, dict) or not rec_raw:
        raise ConfigError('至少要配一条 [recipients]（例：\"大家\" = \"*\"）')
    for word, targets in rec_raw.items():
        if targets == "*":
            recipients[word] = sorted(devices)
            continue
        if not isinstance(targets, list) or not targets:
            raise ConfigError(f'[recipients] 的 {word!r} 要设备列表或 "*"，拿到 {targets!r}')
        bad = [t for t in targets if t not in devices]
        if bad:
            raise ConfigError(
                f"[recipients] 的 {word!r} 引用了不存在的设备：{'、'.join(bad)}"
            )
        recipients[word] = list(targets)

    return Config(
        path=path,
        root=root,
        me=me,
        transport=transport,
        ssh_timeout_seconds=ssh_timeout,
        devices=devices,
        hub=hub,
        loop=loop,
        receive=receive,
        naming=naming,
        post=post,
        recipients=recipients,
        warnings=warnings,
    )


def _detect_me(devs_raw: dict, root: Path, transport: str) -> str:
    """认本机是哪台：① 主机名命中别名 ② workspace 存在 ③ 唯一候选。"""
    host = socket.gethostname().lower()
    by_name = [
        key for key, d in devs_raw.items()
        if isinstance(d, dict)
        and host
        and host in (a.lower() for a in d.get("aliases", []) if isinstance(a, str))
    ]
    if len(by_name) == 1:
        return by_name[0]
    by_ws = []
    if transport == "local":
        by_ws = [k for k, d in devs_raw.items()
                 if isinstance(d, dict) and _local_resolve(root, str(d.get("workspace", ""))).is_dir()]
    if len(by_ws) == 1:
        return by_ws[0]
    raise ConfigError(
        "认不出本机是哪台设备。请在 bridge.toml 的 [general] 里写 me = \"<设备 key>\"，"
        f"或设环境变量 {DEVICE_ENV}。"
        f"（设备：{'、'.join(sorted(devs_raw))}；主机名：{socket.gethostname()}）"
    )


def describe(cfg: Config) -> str:
    """人读的一段摘要，`--status` 与排查时用。"""
    d = cfg.device()
    hub_where = f"（在 {cfg.hub.host} 上）" if cfg.hub.host else "（本机）"
    lines = [
        f"配置：{cfg.path}",
        f"本机：{cfg.me}（{d.name}）· 传输：{cfg.transport}",
        f"工作区：{d.workspace}",
        f"收件箱：{d.inbox}",
        f"发件箱：{d.outbox}",
        f"中心：{cfg.hub.path}{hub_where}",
        f"心跳：{cfg.hub.beat_path} · 判死线 {cfg.hub.beat_deadline_seconds}s",
        "设备：" + "、".join(f"{k}={v.name}" for k, v in sorted(cfg.devices.items())),
    ]
    for w in cfg.warnings:
        lines.append(f"[!] {w}")
    return "\n".join(lines)
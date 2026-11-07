#!/bin/sh
# dsh-post 安装（POSIX：Linux / macOS / Termux 之类）
#
# 只做三件事：
#   ① 校验 python3 >= 3.11、能读懂 bridge.toml
#   ② 有 systemd user 就写一个 user unit；没有就写一个启动脚本（setsid nohup）
#   ③ 打印接下来要手工做的事
#
# ⛔ 不写任何远端文件、不改任何设备的计划任务、不碰 ssh 配置。
#
# 用法：
#   sh install/install.sh --dry-run        # 空跑，只打印将做什么
#   sh install/install.sh                  # 真装
#   sh install/install.sh --uninstall      # 卸掉 unit / 启动脚本（不删数据）
#
# 可选：
#   --config PATH     指定 bridge.toml（默认仓根）
#   --python PATH     指定解释器（默认 $PYTHON 或 python3）
#   --prefix DIR      启动脚本落点（默认 $HOME/.local）
#   --no-systemd      即使有 systemd 也走 setsid nohup
#   --no-enable       只写 unit，不 enable / start

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONFIG=""
PYTHON_BIN="${PYTHON:-}"
PREFIX="${HOME}/.local"
DRY_RUN=0
USE_SYSTEMD=1
DO_ENABLE=1
UNINSTALL=0
UNIT_NAME="dsh-post-loop.service"

say()  { printf '  [%s] %s\n' "$1" "$2"; }
plan() { say "计划" "$1"; }
todo() { say "执行" "$1"; }
ok()   { say "OK"   "$1"; }
warn() { say "注意" "$1"; }
die()  { printf '[X] %s\n' "$1" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)    DRY_RUN=1 ;;
        --config)     shift; CONFIG="${1:-}"; [ -n "$CONFIG" ] || die "--config 后面要给路径" ;;
        --python)     shift; PYTHON_BIN="${1:-}"; [ -n "$PYTHON_BIN" ] || die "--python 后面要给路径" ;;
        --prefix)     shift; PREFIX="${1:-}"; [ -n "$PREFIX" ] || die "--prefix 后面要给目录" ;;
        --no-systemd) USE_SYSTEMD=0 ;;
        --no-enable)  DO_ENABLE=0 ;;
        --uninstall)  UNINSTALL=1 ;;
        -h|--help)    sed -n '2,26p' "$0"; exit 0 ;;
        *)            die "不认识的参数：$1（--help 看用法）" ;;
    esac
    shift
done

[ -n "$CONFIG" ] || CONFIG="$ROOT/bridge.toml"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"
SCRIPT_PATH="$PREFIX/bin/dsh-post-loop"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/dsh-post"
LOG_PATH="$STATE_DIR/loop.log"

printf '\ndsh-post 安装（POSIX）\n'
printf '  仓目录：%s\n' "$ROOT"
printf '  配置文件：%s\n' "$CONFIG"
[ "$DRY_RUN" -eq 1 ] && printf '  模式：空跑（--dry-run），不改系统\n'
printf '\n'

# ── ① 卸载 ──
if [ "$UNINSTALL" -eq 1 ]; then
    if [ -f "$UNIT_PATH" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            plan "将 systemctl --user disable --now $UNIT_NAME"
            plan "将删除 $UNIT_PATH"
        else
            systemctl --user disable --now "$UNIT_NAME" || true
            rm -f "$UNIT_PATH"
            systemctl --user daemon-reload || true
            ok "已停用并删除 $UNIT_PATH"
        fi
    else
        warn "没有 $UNIT_PATH，跳过 systemd 部分"
    fi
    if [ -f "$SCRIPT_PATH" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            plan "将删除启动脚本 $SCRIPT_PATH"
        else
            rm -f "$SCRIPT_PATH"
            ok "已删除启动脚本 $SCRIPT_PATH"
        fi
    fi
    say "注意" "数据（收件箱/发件箱/聊天室/状态）一个都没动，要清自己清"
    exit 0
fi

# ── ② 解释器 ──
if [ -z "$PYTHON_BIN" ]; then
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then PYTHON_BIN=$(command -v "$c"); break; fi
    done
fi
[ -n "$PYTHON_BIN" ] || die "找不到 python3。装 Python 3.11+ 或用 --python 指定绝对路径。"
ok "python：$PYTHON_BIN"
PYVER=$("$PYTHON_BIN" -c 'import sys;print("%d.%d" % sys.version_info[:2])') || die "$PYTHON_BIN 跑不起来"
case "$PYVER" in
    3.11|3.12|3.13|3.14|3.15|3.16|3.17|3.18|3.19|4.*) ok "Python 版本 $PYVER（需要 >= 3.11）" ;;
    *) die "Python $PYVER 太老：这包用 tomllib，需要 3.11+" ;;
esac

command -v ssh >/dev/null 2>&1 || warn "没有 ssh 命令：只在本机读写时没问题，要投别台必须先装。"

# ── ③ 配置 ──
[ -f "$CONFIG" ] || die "找不到配置文件 $CONFIG（先复制 bridge.toml.example 成 bridge.toml 再改）"
PROBE=$("$PYTHON_BIN" - "$ROOT" "$CONFIG" <<'PY' 2>&1
import sys
sys.path.insert(0, sys.argv[1])
from bridge.config import load_config
cfg = load_config(sys.argv[2])
print(cfg.loop.interval_seconds)
print(cfg.me)
print("|".join(sorted(cfg.devices)))
PY
) || {
    printf '[X] 配置读不通，先把这条修好再装：\n%s\n' "$PROBE" >&2
    exit 2
}
INTERVAL=$(printf '%s\n' "$PROBE" | sed -n 1p)
ME=$(printf '%s\n' "$PROBE" | sed -n 2p)
DEVS=$(printf '%s\n' "$PROBE" | sed -n 3p)
[ -n "${INTERVAL:-}" ] || die "从配置里读不出 [loop].interval_seconds"
ok "配置可用：本机=$ME · 设备=$DEVS · 间隔 ${INTERVAL}s"

# ── ④ systemd user unit 还是 setsid nohup ──
HAVE_SYSTEMD=0
if [ "$USE_SYSTEMD" -eq 1 ] && command -v systemctl >/dev/null 2>&1; then
    if systemctl --user show-environment >/dev/null 2>&1; then
        HAVE_SYSTEMD=1
    else
        warn "systemctl 在，但 user 实例连不上（没有 user session / 没开 lingering）⇒ 走 setsid nohup"
    fi
fi

if [ "$HAVE_SYSTEMD" -eq 1 ]; then
    UNIT_CONTENT="[Unit]
Description=dsh-post 投递守护（每 ${INTERVAL}s 一轮）
Documentation=file://$ROOT/README.md

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$PYTHON_BIN -m bridge.loop --start --config $CONFIG
Restart=always
RestartSec=30
# ⛔ 不要设置 PrivateTmp / ProtectHome：本包要读用户主目录下的工作区
Environment=PYTHONIOENCODING=utf-8

[Install]
WantedBy=default.target
"
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "将写入 $UNIT_PATH，内容："
        printf '%s\n' "$UNIT_CONTENT" | sed 's/^/        /'
        plan "将 systemctl --user daemon-reload"
        [ "$DO_ENABLE" -eq 1 ] && plan "将 systemctl --user enable --now $UNIT_NAME"
        plan "日志：journalctl --user -u $UNIT_NAME -f"
    else
        mkdir -p "$UNIT_DIR"
        printf '%s' "$UNIT_CONTENT" > "$UNIT_PATH"
        ok "已写入 $UNIT_PATH"
        systemctl --user daemon-reload
        if [ "$DO_ENABLE" -eq 1 ]; then
            systemctl --user enable --now "$UNIT_NAME"
            ok "已 enable --now $UNIT_NAME"
        else
            warn "没 enable（--no-enable）：手工 systemctl --user enable --now $UNIT_NAME"
        fi
    fi
else
    BOOT_CONTENT="#!/bin/sh
# 由 install/install.sh 生成；改配置不用重装，改 bridge.toml 就行。
cd \"$ROOT\" || exit 1
exec \"$PYTHON_BIN\" -m bridge.loop --start --config \"$CONFIG\"
"
    RUN_CMD="setsid nohup \"$SCRIPT_PATH\" >> \"$LOG_PATH\" 2>&1 &"
    if [ "$DRY_RUN" -eq 1 ]; then
        plan "将写入启动脚本 $SCRIPT_PATH，内容："
        printf '%s\n' "$BOOT_CONTENT" | sed 's/^/        /'
        plan "mkdir -p $STATE_DIR（日志目录）"
        plan "启动方式（脚本不替你起，免得你以为它常驻了）："
        plan "        $RUN_CMD"
        plan "自启（脚本不替你做，要的话自己加进 crontab/rc）："
        plan "        @reboot $RUN_CMD"
    else
        mkdir -p "$PREFIX/bin" "$STATE_DIR"
        printf '%s' "$BOOT_CONTENT" > "$SCRIPT_PATH"
        chmod +x "$SCRIPT_PATH"
        ok "已写入启动脚本 $SCRIPT_PATH"
        warn "没有 systemd user 实例：这次只写了脚本，**没有**替你起进程。要常驻就跑："
        printf '        %s\n' "$RUN_CMD"
    fi
fi

printf '\n接下来（手工，脚本不替你做）：\n'
printf '  1) 试投一封：%s -m bridge.post --once --dry-run --config %s\n' "$PYTHON_BIN" "$CONFIG"
printf '  2) 看状态：  %s -m bridge.post --status --config %s\n' "$PYTHON_BIN" "$CONFIG"
printf '  3) 跑一次：  %s -m bridge.loop --once --config %s\n' "$PYTHON_BIN" "$CONFIG"
printf '  4) 三台互信没配好？先看 docs/01_三台互信怎么配.md\n'
printf '\n卸载：sh install/install.sh --uninstall（只删 unit / 启动脚本，不删数据）\n\n'
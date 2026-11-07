"""dsh-post —— 三台（或多台）共享聊天室 + 传递信件。

零第三方依赖：只用标准库 + 系统 `ssh`。Python ≥ 3.11。

四个入口（各自是独立命令，⛔ 不需要装成包）：

    python -m bridge.chat    --status | --read | --say "..." | --say-file f.md | --tail 5
    python -m bridge.post    --once [--alarm-exit] | --status | --send f.md | --self-test
    python -m bridge.receive --once [--dry-run] | --status
    python -m bridge.loop    --start | --once | --status

配置全在 `bridge.toml` 里（模板见仓根 `bridge.toml.example`）。
代码里没有任何写死的设备名或路径 —— 想写死也没有地方写。
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
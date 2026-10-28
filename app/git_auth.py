"""Git HTTPS 凭据通过 askpass 环境提供，避免写入 remote URL 或命令参数。"""
import os
from . import config


def auth_env(token: str | None) -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    if token:
        script = config.DATA_DIR / "git-askpass.sh"
        script.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "x-access-token";; *) printf "%s" "$HUB_GIT_TOKEN";; esac\n', encoding="utf-8")
        script.chmod(0o700)
        env["GIT_ASKPASS"] = str(script)
        env["HUB_GIT_TOKEN"] = token
    return env

"""Git HTTPS 凭据通过 askpass 环境提供，避免写入 remote URL 或命令参数。"""
import os
import tempfile
from . import config

ASKPASS_BODY = ('#!/bin/sh\ncase "$1" in *Username*) printf "x-access-token";; '
                '*) printf "%s" "$HUB_GIT_TOKEN";; esac\n')


def _askpass_script():
    script = config.DATA_DIR / "git-askpass.sh"
    # 多个项目可同时准备 Git 认证。原子替换避免另一个 Git 进程读到半截脚本。
    fd, temp_path = tempfile.mkstemp(prefix="git-askpass-", dir=config.DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(ASKPASS_BODY)
        os.chmod(temp_path, 0o700)
        os.replace(temp_path, script)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return script


def auth_env(token: str | None) -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    if token:
        env["GIT_ASKPASS"] = str(_askpass_script())
        env["HUB_GIT_TOKEN"] = token
    return env

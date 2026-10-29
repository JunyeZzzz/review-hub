"""本机命令生命周期：每次独立进程组，超时只清理该命令及普通子进程。"""
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

MAX_CAPTURE_BYTES = 16_000_000


class OutputLimitExceeded(RuntimeError):
    pass


class BackgroundProcessLeftover(RuntimeError):
    pass


def _stop_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS 上组长已退出时可能报告 EPERM；至少收掉仍在运行的直接子进程。
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=1)


def run_grouped(cmd: list[str], *, cwd: str | Path, env: dict,
                timeout: float, stdin=None,
                max_output_bytes: int = MAX_CAPTURE_BYTES) -> subprocess.CompletedProcess:
    """输出落到独立临时文件；过量或超时即清理本进程组，绝不截断后判通过。"""
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=stdin,
                                stdout=stdout_file, stderr=stderr_file,
                                start_new_session=True)
        deadline = time.monotonic() + timeout
        while True:
            output_size = os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size
            if output_size > max_output_bytes:
                _stop_group(proc)
                raise OutputLimitExceeded(
                    f"命令输出超过 {max_output_bytes} 字节上限；已终止该进程组，不能截断后判定通过")
            if proc.poll() is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_group(proc)
                # Agent 的命令参数可能含完整源码提示词，异常不可回显它们。
                raise subprocess.TimeoutExpired(cmd[:1], timeout)
            time.sleep(min(0.05, remaining))
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            pass
        else:
            _stop_group(proc)
            raise BackgroundProcessLeftover("命令已退出但仍有后台子进程；已清理该进程组，不能判定门禁通过")
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

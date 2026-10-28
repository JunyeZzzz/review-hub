"""机械门禁：密钥扫描 / 测试 / 构建 / semgrep 安全扫描。

隔离原则（docs/GAP-ANALYSIS.md「我们的风格」）：
- 门禁在人审之前执行未经审查的代码——主机模式只传白名单环境变量，token 一律不进子进程；
- Docker 沙盒模式：一次性容器 + 断网（依赖安装期可单独放网）+ 资源上限 + 按项目的依赖缓存卷；
- 显式选择了 Docker / 工具而环境不满足 -> fail loud 红灯，不静默跳过。
"""
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

SECRET_PATTERNS = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("OpenAI Key", re.compile(r"sk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}")),
    ("私钥文件", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("通用高熵赋值", re.compile(r"(?i)(password|passwd|secret|api_key)\s*[:=]\s*['\"][A-Za-z0-9+/_-]{24,}['\"]")),
]

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__"}

# pytest 及其依赖缺失时解释器会报 No module named XXX——这是环境问题，不是测试失败
_MISSING_MODULE_RX = re.compile(r"No module named ['\"]?(pytest|pluggy|iniconfig|hamcrest)['\"]?")

# 主机模式环境白名单：只给运行工具所需的最小集。被审代码拿不到任何凭证。
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
             "TERM", "SHELL", "USER", "LOGNAME")

DOCKER_LIMITS = ["--cpus", "2", "--memory", "2g", "--pids-limit", "512", "--cap-drop", "ALL"]

_NET_LABEL = {"off": "断网", "install": "仅安装期放网", "on": "全程放网"}


def _gate_env() -> dict:
    return {k: v for k, v in os.environ.items() if k in _ENV_KEEP}


def scan_secrets(workdir: Path, files: list) -> dict:
    """只扫本次变更的文件。返回 {ok, findings:[{rule,file,line,preview}]}"""
    findings = []
    for rel in files:
        p = workdir / rel
        if not p.is_file() or p.stat().st_size > 1_000_000:
            continue
        if set(rel.split("/")) & SKIP_DIRS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rule, rx in SECRET_PATTERNS:
            m = rx.search(text)
            if m:
                line_no = text[: m.start()].count("\n") + 1
                findings.append({
                    "rule": rule, "file": rel, "line": line_no,
                    "preview": m.group(0)[:12] + "…",
                })
    return {"ok": not findings, "findings": findings}


# ---------- 主机模式：白名单环境 + ok/fail/skip 三态 ----------

def _exec(cmd: list, workdir: Path, timeout: int) -> dict:
    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                           timeout=timeout, env=_gate_env())
    except subprocess.TimeoutExpired:
        return {"status": "fail", "cmd": " ".join(cmd), "log": f"超时（>{timeout}s）——按门禁失败处理"}
    except FileNotFoundError:
        return {"status": "skip", "cmd": "",
                "log": f"命令 {cmd[0]} 不存在（部署机未安装），门禁跳过"}
    log = (r.stdout + "\n" + r.stderr).strip()
    if r.returncode == 127 or "command not found" in log[:500]:
        return {"status": "skip", "cmd": "",
                "log": f"{cmd[0]} 不可用（部署机未安装），门禁跳过\n{log[-1500:]}"}
    m = _MISSING_MODULE_RX.search(log)
    if m:
        return {"status": "skip", "cmd": " ".join(cmd),
                "log": f"部署机缺少 {m.group(1)}（这是环境问题，不是代码失败），门禁跳过——"
                       f"安装后门禁自动生效\n{log[-1500:]}"}
    return {"status": "ok" if r.returncode == 0 else "fail",
            "cmd": " ".join(cmd), "log": log[-8000:] or "(无输出)"}


# ---------- Docker 沙盒 ----------

def docker_available() -> bool:
    return bool(shutil.which("docker"))


def _auto_image(workdir: Path) -> str:
    if (workdir / "package.json").exists():
        return "node:22-bookworm"
    if (workdir / "go.mod").exists():
        return "golang:1.23-bookworm"
    return "python:3.12-bookworm"   # 自带 make/gcc，Makefile 型项目也能跑


def _cache_volumes(workdir: Path, cache_id) -> list:
    """按项目持久化的依赖缓存卷：并发审查互不影响，重跑不用重新装依赖。"""
    if not cache_id:
        return []
    v = []
    if (workdir / "package.json").exists():
        v += ["-v", f"hubcache-npm-{cache_id}:/w/node_modules"]
    if (workdir / "go.mod").exists():
        v += ["-v", f"hubcache-go-{cache_id}:/go/pkg/mod"]
    v += ["-v", f"hubcache-pip-{cache_id}:/root/.cache/pip"]
    return v


def _docker_phase(cmd: list, workdir: Path, image: str, net_on: bool,
                  timeout: int, cache_id, purpose: str) -> dict:
    """跑一个一次性容器。超时强制移除容器，不留僵尸。"""
    name = f"hub-gate-{uuid.uuid4().hex[:10]}"
    w = str(workdir.resolve())
    argv = ["docker", "run", "--rm", "--name", name,
            "--network", "bridge" if net_on else "none", *DOCKER_LIMITS,
            "-v", f"{w}:/w", "-w", "/w", *_cache_volumes(workdir, cache_id), image, *cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        return {"status": "fail", "cmd": purpose,
                "log": f"沙盒内 {purpose} 超时（>{timeout}s），容器已强制移除"}
    log = (r.stdout + "\n" + r.stderr).strip()
    low = log.lower()
    if r.returncode != 0 and ("docker daemon" in low or "cannot connect" in low
                              or "is the docker daemon" in low):
        return {"status": "fail", "cmd": purpose,
                "log": f"Docker 不可用：{log[-500:]}——沙盒门禁显式启用却无法执行，fail loud"}
    return {"status": "ok" if r.returncode == 0 else "fail",
            "cmd": purpose, "log": log[-8000:] or "(无输出)",
            "returncode": r.returncode, "stdout": r.stdout[-200000:], "stderr": r.stderr[-5000:]}


def _norm_cfg(cfg: dict | None) -> dict:
    cfg = cfg or {}
    sandbox = "docker" if cfg.get("sandbox") == "docker" else "off"
    network = cfg.get("network") if cfg.get("network") in ("off", "install", "on") else "install"
    return {"sandbox": sandbox, "network": network,
            "image": str(cfg.get("image") or "").strip()}


def _install_cmd(workdir: Path) -> list | None:
    if (workdir / "package.json").exists():
        return ["sh", "-c", "npm ci --no-audit --no-fund || npm install --no-audit --no-fund"]
    if (workdir / "requirements.txt").exists():
        return ["sh", "-c", "pip install -q -r requirements.txt"]
    if (workdir / "pyproject.toml").exists():
        return ["sh", "-c", "pip install -q ."]
    return None


def _run_sandboxed(cmd: list, workdir: Path, timeout: int, cfg: dict | None,
                   cache_id, purpose: str, install_aware: bool = True) -> dict:
    """主机白名单执行 或 Docker 沙盒执行。install_aware：测试类门禁先装依赖（安装期放网）。"""
    c = _norm_cfg(cfg)
    if c["sandbox"] != "docker":
        return _exec(cmd, workdir, timeout)
    if not docker_available():
        return {"status": "fail", "cmd": purpose,
                "log": "沙盒门禁已选择 Docker，但本机没有 docker 命令——fail loud；"
                       "安装 Docker，或在配置中心把该门禁的隔离切回「本机」"}
    image = c["image"] or _auto_image(workdir)
    network = c["network"]
    notes = ""
    if image.startswith("golang") and network == "install":
        network = "on"   # go test 要在测试期拉模块
        notes = "（go 需在测试期拉取模块，已自动改为全程放网）"
    head = f"Docker 沙盒 · 镜像 {image} · 网络：{_NET_LABEL[network]}{notes}"
    if install_aware and network != "off":
        ic = _install_cmd(workdir)
        if ic:
            ir = _docker_phase(ic, workdir, image, True, timeout, cache_id, "依赖安装")
            if ir["status"] != "ok":
                return {"status": "fail", "cmd": purpose,
                        "log": head + "\n依赖安装失败（依赖装不上本身就是红灯信号）：\n" + ir["log"]}
    net_on = network == "on"
    r = _docker_phase(cmd, workdir, image, net_on, timeout, cache_id, purpose)
    r["log"] = head + "\n" + r["log"]
    return r


# ---------- 三类门禁 ----------

def run_tests(workdir: Path, timeout: int = 900, cfg: dict | None = None, cache_id="") -> dict:
    """测试门禁：探测 make test / npm test / pytest / go test；没有入口返回 skip。"""
    mk = workdir / "Makefile"
    if mk.exists() and re.search(r"(?m)^\s*test\s*:", mk.read_text(errors="replace")):
        cmd = ["make", "test"]
    else:
        pj = workdir / "package.json"
        if pj.exists():
            if re.search(r'"test"\s*:', pj.read_text(errors="replace")):
                cmd = ["npm", "test"]
            else:
                return {"status": "skip", "cmd": "", "log": "package.json 没有 test 脚本，跳过"}
        elif (workdir / "pyproject.toml").exists() or (workdir / "requirements.txt").exists():
            cmd = ["python3", "-m", "pytest", "-q"]
        elif (workdir / "go.mod").exists():
            cmd = ["go", "test", "./..."]
        else:
            return {"status": "skip", "cmd": "",
                    "log": "未找到测试入口（Makefile test 目标 / package.json test 脚本 / "
                           "pyproject|requirements / go.mod），跳过"}
    return _run_sandboxed(cmd, workdir, timeout, cfg, cache_id, "测试门禁")


def run_build(workdir: Path, timeout: int = 900, cfg: dict | None = None, cache_id="") -> dict:
    """构建门禁：探测 npm run build / make build / go build；没有入口返回 skip。"""
    pj = workdir / "package.json"
    if pj.exists():
        if re.search(r'"build"\s*:', pj.read_text(errors="replace")):
            cmd = ["npm", "run", "build"]
        else:
            return {"status": "skip", "cmd": "", "log": "package.json 没有 build 脚本，跳过"}
    else:
        mk = workdir / "Makefile"
        if mk.exists() and re.search(r"(?m)^\s*build\s*:", mk.read_text(errors="replace")):
            cmd = ["make", "build"]
        elif (workdir / "go.mod").exists():
            cmd = ["go", "build", "./..."]
        else:
            return {"status": "skip", "cmd": "",
                    "log": "未找到构建入口（package.json build 脚本 / Makefile build 目标 / go.mod），跳过"}
    return _run_sandboxed(cmd, workdir, timeout, cfg, cache_id, "构建门禁", install_aware=False)


def semgrep_available() -> bool:
    return bool(shutil.which("semgrep"))


def run_semgrep(workdir: Path, files: list, timeout: int = 900,
                cfg: dict | None = None, cache_id="") -> dict:
    """安全扫描门禁：semgrep p/default + p/secrets，只扫变更文件。
    ERROR 级发现 = Important（一票否决阻断）；WARNING/INFO = Nit（可见不阻断）。
    显式启用却缺工具/缺 Docker -> fail loud。Docker 模式用官方 semgrep 镜像，无需本机安装。"""
    c = _norm_cfg(cfg)
    path_args = [f for f in files if f and not set(f.split("/")) & SKIP_DIRS][:300]
    if not path_args:
        return {"status": "skip", "cmd": "", "log": "本次变更没有可扫描的文件，跳过"}

    if c["sandbox"] == "docker":
        if not docker_available():
            return {"status": "fail", "cmd": "semgrep",
                    "log": "安全门禁已选择 Docker 沙盒，但本机没有 docker 命令——fail loud"}
        # semgrep 规则集（p/default p/secrets）需联网拉取；扫描本身不执行被审代码
        base = ["semgrep", "scan", "--json", "--quiet",
                "--config", "p/default", "--config", "p/secrets", "--"]
        image = c["image"] or "semgrep/semgrep"
        head = f"Docker 沙盒 · 镜像 {image} · 网络：全程放网（拉取规则集）\nsemgrep 扫描 {len(path_args)} 个变更文件"
        r = _docker_phase(base + path_args, workdir, image, True, timeout, "", head)
        return _parse_semgrep(r, head)
    if not semgrep_available():
        return {"status": "fail", "cmd": "semgrep",
                "log": "semgrep 未安装（pip install semgrep / brew install semgrep），"
                       "安全门禁显式启用却无法执行——fail loud"}
    cmd = ["semgrep", "scan", "--json", "--quiet",
           "--config", "p/default", "--config", "p/secrets", "--"] + path_args
    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                           timeout=timeout, env=_gate_env())
    except subprocess.TimeoutExpired:
        return {"status": "fail", "cmd": " ".join(cmd), "log": f"semgrep 超时（>{timeout}s）"}
    head = f"semgrep 扫描 {len(path_args)} 个变更文件"
    return _parse_semgrep({"returncode": r.returncode, "stdout": r.stdout,
                           "stderr": r.stderr, "status": "ok" if r.returncode < 2 else "fail",
                           "log": ""}, head, raw_cmd=" ".join(cmd))


def _parse_semgrep(r: dict, head: str, raw_cmd: str = "semgrep") -> dict:
    """主机/Docker 两条路的统一结果解析：r 带 returncode + 原始 stdout。
    已经是失败判定的结果（Docker 不可用/超时）直接透传，绝不误判为干净。"""
    if r.get("status") == "fail" and "returncode" not in r:
        return r
    rc = r.get("returncode", 1)
    try:
        data = json.loads(r.get("stdout") or "{}")
    except json.JSONDecodeError:
        return {"status": "fail", "cmd": raw_cmd,
                "log": head + f"——semgrep 输出无法解析（退出码 {rc}）：\n"
                       f"{((r.get('stdout') or '') + (r.get('stderr') or r.get('log') or ''))[-3000:]}"}
    if rc >= 2:   # semgrep 约定：0=干净 1=有发现 2+=扫描本身出错
        return {"status": "fail", "cmd": raw_cmd,
                "log": head + f"——semgrep 扫描本身失败（退出码 {rc}，fail loud）：\n"
                       f"{((r.get('stderr') or '') + (r.get('stdout') or ''))[-3000:]}"}
    findings = []
    for res in data.get("results") or []:
        extra = res.get("extra") or {}
        sev = str(extra.get("severity") or "INFO").upper()
        findings.append({
            "rule": str(res.get("check_id") or "?"),
            "file": res.get("path") or "?",
            "line": (res.get("start") or {}).get("line") or 0,
            "severity": "IMPORTANT" if sev == "ERROR" else "NIT",
            "condition": str(extra.get("message") or "")[:400],
            "preview": str(extra.get("lines") or "")[:120],
        })
    imp = sum(1 for f in findings if f["severity"] == "IMPORTANT")
    log = (head + f"：Important {imp} / 共 {len(findings)} 条"
           f"（ERROR 级一票否决，WARNING/INFO 记为 Nit）\n"
           + json.dumps(findings, ensure_ascii=False, indent=1)[:6000])
    return {"status": "fail" if imp else "ok", "cmd": raw_cmd,
            "log": log, "findings": findings}

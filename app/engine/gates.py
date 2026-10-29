"""机械门禁：密钥扫描 / 测试 / 构建 / semgrep 安全扫描。

隔离原则：
- 默认本机执行；每条门禁命令使用独立 HOME、临时目录和工具缓存，工作目录按项目分开；
- 超时清理整个命令进程组；环境变量白名单只减少意外串扰，不提供文件权限隔离；
- 旧配置仍可显式选 Docker；工具或执行环境缺失时门禁失败。
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from .. import config
from ..processes import BackgroundProcessLeftover, OutputLimitExceeded, run_grouped

SECRET_PATTERNS = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub Token", re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("OpenAI Key", re.compile(r"sk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}")),
    ("私钥文件", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("通用高熵赋值", re.compile(r"(?i)(password|passwd|secret|api_key)\s*[:=]\s*['\"][A-Za-z0-9+/_-]{24,}['\"]")),
]

MAX_SECRET_FILE_BYTES = 10_000_000
SEMGREP_BATCH_SIZE = 300
MAX_SEMGREP_BATCHES = 40

# pytest 及其依赖缺失时解释器会报 No module named XXX——这是环境问题，不是测试失败
_MISSING_MODULE_RX = re.compile(r"No module named ['\"]?(pytest|pluggy|iniconfig|hamcrest)['\"]?")

# 本机命令只继承执行所需的变量；HOME/TMPDIR 和工具缓存每条命令单独创建。
_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SHELL", "USER", "LOGNAME")

DOCKER_LIMITS = ["--cpus", "2", "--memory", "2g", "--pids-limit", "512", "--cap-drop", "ALL"]

_NET_LABEL = {"off": "断网", "install": "仅安装期放网", "on": "全程放网"}


def _gate_env(runtime: Path, workdir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
    for name in ("home", "tmp", "cache", "config", "go", "npm"):
        (runtime / name).mkdir(mode=0o700, exist_ok=True)
    env.update({
        "HOME": str(runtime / "home"), "TMPDIR": str(runtime / "tmp"),
        "TMP": str(runtime / "tmp"), "TEMP": str(runtime / "tmp"),
        "XDG_CACHE_HOME": str(runtime / "cache"),
        "XDG_CONFIG_HOME": str(runtime / "config"),
        "PIP_CACHE_DIR": str(runtime / "cache" / "pip"),
        "PIP_CONFIG_FILE": os.devnull,
        "npm_config_cache": str(runtime / "npm"),
        "npm_config_userconfig": str(runtime / "config" / "npmrc"),
        "GOPATH": str(runtime / "go"),
        "GOCACHE": str(runtime / "cache" / "go-build"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "PWD": str(workdir),
    })
    return env


def _run_host(cmd: list, workdir: Path, timeout: int) -> subprocess.CompletedProcess:
    """每次独立的进程环境；超时杀掉该命令的进程组，不牵连其他审查。"""
    with tempfile.TemporaryDirectory(prefix="hub-gate-", dir=config.DATA_DIR) as tmp:
        return run_grouped(cmd, cwd=workdir, env=_gate_env(Path(tmp), workdir), timeout=timeout,
                           stdin=subprocess.DEVNULL)


def scan_secrets(workdir: Path, files: list) -> dict:
    """只扫本次变更的文件。返回 {ok, findings:[{rule,file,line,preview}]}"""
    findings = []
    for rel in files:
        p = workdir / rel
        try:
            mode = p.lstat().st_mode
        except FileNotFoundError:  # 已删除的文件没有当前内容可扫
            continue
        except OSError as exc:
            raise RuntimeError(f"密钥扫描无法访问 {rel}：{exc}") from exc
        if not stat.S_ISREG(mode):
            raise RuntimeError(f"密钥扫描不能读取非普通文件 {rel}；不能判定通过")
        if p.stat().st_size > MAX_SECRET_FILE_BYTES:
            raise RuntimeError(f"密钥扫描无法完整读取 {rel}（超过 {MAX_SECRET_FILE_BYTES} 字节）；不能判定通过")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"密钥扫描无法读取 {rel}：{exc}") from exc
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
        r = _run_host(cmd, workdir, timeout)
    except subprocess.TimeoutExpired:
        return {"status": "fail", "cmd": " ".join(cmd), "log": f"超时（>{timeout}s）——按门禁失败处理"}
    except FileNotFoundError:
        return {"status": "fail", "cmd": " ".join(cmd),
                "log": f"命令 {cmd[0]} 不存在（部署机未安装），门禁无法执行"}
    except (OutputLimitExceeded, BackgroundProcessLeftover) as exc:
        return {"status": "fail", "cmd": " ".join(cmd), "log": str(exc)}
    log = (r.stdout + "\n" + r.stderr).strip()
    if r.returncode == 127 or "command not found" in log[:500]:
        return {"status": "fail", "cmd": " ".join(cmd),
                "log": f"{cmd[0]} 不可用（部署机未安装），门禁无法执行\n{log[-1500:]}"}
    m = _MISSING_MODULE_RX.search(log)
    if m:
        return {"status": "fail", "cmd": " ".join(cmd),
                "log": f"部署机缺少 {m.group(1)}（环境问题），门禁无法执行；请安装后重审\n{log[-1500:]}"}
    return {"status": "ok" if r.returncode == 0 else "fail",
            "cmd": " ".join(cmd), "log": log or "(无输出)"}


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
            "cmd": purpose, "log": log or "(无输出)",
            "returncode": r.returncode, "stdout": r.stdout, "stderr": r.stderr[-5000:]}


def _norm_cfg(cfg: dict | None) -> dict:
    cfg = cfg or {}
    sandbox = "docker" if cfg.get("sandbox") == "docker" else "off"
    network = cfg.get("network") if cfg.get("network") in ("off", "install", "on") else "install"
    return {"sandbox": sandbox, "network": network,
            "image": str(cfg.get("image") or "").strip()}


def _install_cmd(workdir: Path) -> list | None:
    if (workdir / "package.json").exists():
        if (workdir / "package-lock.json").exists() or (workdir / "npm-shrinkwrap.json").exists():
            return ["npm", "ci", "--no-audit", "--no-fund"]
        return ["npm", "install", "--no-audit", "--no-fund"]
    if (workdir / "requirements.txt").exists():
        return ["sh", "-c", "pip install -q -r requirements.txt"]
    if (workdir / "pyproject.toml").exists():
        return ["sh", "-c", "pip install -q ."]
    return None


def _run_sandboxed(cmd: list, workdir: Path, timeout: int, cfg: dict | None,
                   cache_id, purpose: str, install_aware: bool = True) -> dict:
    """默认本机独立进程环境，旧配置可选 Docker；测试类门禁可先安装依赖。"""
    c = _norm_cfg(cfg)
    if c["sandbox"] != "docker":
        return _exec(cmd, workdir, timeout)
    if not docker_available():
        return {"status": "fail", "cmd": purpose,
                "log": "沙盒门禁已选择 Docker，但本机没有 docker 命令——fail loud；"
                       "安装 Docker，或将该门禁改为本机执行（仅用于可信仓库）"}
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
    path_args = [f for f in files if f and (workdir / f).is_file()]
    if not path_args:
        return {"status": "skip", "cmd": "", "log": "本次变更没有现存文件可扫描（可能均为删除），跳过"}
    if len(path_args) > SEMGREP_BATCH_SIZE * MAX_SEMGREP_BATCHES:
        return {"status": "fail", "cmd": "semgrep",
                "log": f"本次有 {len(path_args)} 个可扫描文件，超过 {SEMGREP_BATCH_SIZE * MAX_SEMGREP_BATCHES} 个文件的安全扫描上限；不能只扫前一部分"}
    if len(path_args) > SEMGREP_BATCH_SIZE:
        findings, notes = [], []
        for start in range(0, len(path_args), SEMGREP_BATCH_SIZE):
            batch = path_args[start:start + SEMGREP_BATCH_SIZE]
            result = run_semgrep(workdir, batch, timeout=timeout, cfg=cfg, cache_id=cache_id)
            if result["status"] == "fail" and not result.get("findings"):
                return {"status": "fail", "cmd": "semgrep",
                        "log": f"第 {start // SEMGREP_BATCH_SIZE + 1} 批扫描失败：{result['log']}"}
            findings.extend(result.get("findings") or [])
            notes.append(f"第 {start // SEMGREP_BATCH_SIZE + 1} 批：{len(result.get('findings') or [])} 条发现")
        important = sum(f["severity"] == "IMPORTANT" for f in findings)
        return {"status": "fail" if important else "ok", "cmd": "semgrep",
                "log": f"完整扫描 {len(path_args)} 个文件，Important {important} / 共 {len(findings)} 条；" +
                       "；".join(notes) + "\n" + json.dumps(findings, ensure_ascii=False),
                "findings": findings}

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
        r = _run_host(cmd, workdir, timeout)
    except subprocess.TimeoutExpired:
        return {"status": "fail", "cmd": " ".join(cmd), "log": f"semgrep 超时（>{timeout}s）"}
    except FileNotFoundError:
        return {"status": "fail", "cmd": "semgrep", "log": "semgrep 命令不可用，安全门禁未执行"}
    except (OutputLimitExceeded, BackgroundProcessLeftover) as exc:
        return {"status": "fail", "cmd": "semgrep", "log": str(exc)}
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
    if rc not in (0, 1):   # semgrep 约定：0=干净 1=有发现 2+=扫描本身出错
        return {"status": "fail", "cmd": raw_cmd,
                "log": head + f"——semgrep 扫描本身失败（退出码 {rc}，fail loud）：\n"
                       f"{((r.get('stderr') or '') + (r.get('stdout') or ''))[-3000:]}"}
    if not isinstance(data, dict) or not isinstance(data.get("results"), list) or data.get("errors"):
        return {"status": "fail", "cmd": raw_cmd,
                "log": head + "——semgrep 响应缺少完整 results 或包含扫描错误，不能判定干净"}
    findings = []
    for res in data["results"]:
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
           + json.dumps(findings, ensure_ascii=False, indent=1))
    return {"status": "fail" if imp else "ok", "cmd": raw_cmd,
            "log": log, "findings": findings}

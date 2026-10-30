"""配置：从 review-hub 根目录的 .env 读取。缺失的凭证不阻塞启动，只降级。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "hub.db"
CLONES_DIR = DATA_DIR / "clones"
POLICY_DIR = ROOT / "policy"


def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()

# GitHub
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")  # 如 https://xxx.trycloudflare.com
GITHUB_ORG = os.environ.get("GITHUB_ORG", "")  # 组织名，用于一键导入
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))

# LLM 凭证
CLAUDE_CODE_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Kimi（Moonshot）：作为 Codex 侧评审的后端模型（Responses API，OpenAI 兼容）
KIMI_API_KEY = os.environ.get("KIMI_API_KEY", "")
KIMI_BASE_URL = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.cn/v1")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "kimi-k2.7-code")
# hub 专属的 codex 配置目录（隔离于用户全局 ~/.codex）
CODEX_HOME = DATA_DIR / "codex-home"

PORT = int(os.environ.get("HUB_PORT", "7777"))

# 并发审查 worker 数：不同项目工作目录分开；本机命令仍共享系统资源和用户权限。
WORKER_CONCURRENCY = max(1, int(os.environ.get("WORKER_CONCURRENCY", "2")))
# 交叉评审组和完整分片共享的全局模型调用上限；跨项目也共用此预算。
REVIEW_CONCURRENCY = min(4, max(1, int(os.environ.get("REVIEW_CONCURRENCY", "4"))))
REVIEW_CLI_CONCURRENCY = min(REVIEW_CONCURRENCY, max(1, int(os.environ.get("REVIEW_CLI_CONCURRENCY", "2"))))
REVIEW_AGENT_TIMEOUT_SECONDS = min(3600, max(30, int(os.environ.get("REVIEW_AGENT_TIMEOUT_SECONDS", "1800"))))

# CLI 绝对路径：启动时解析一次，避免运行期 PATH 漂移导致"时有时无"
import shutil as _shutil  # noqa: E402

CLAUDE_BIN = _shutil.which("claude") or ""
CODEX_BIN = _shutil.which("codex") or ""


def cli_identity() -> list[dict]:
    """不执行 CLI 的文件身份；同版本重装、链接替换也需重新验证。"""
    identities=[]
    for provider,binary in (('claude',CLAUDE_BIN),('codex',CODEX_BIN)):
        entry={'provider':provider,'path':str(Path(binary).resolve()) if binary else ''}
        try:
            stat=Path(binary).stat() if binary else None
        except OSError:
            stat=None
        entry.update(mtime_ns=stat.st_mtime_ns if stat else None,
                     size=stat.st_size if stat else None,inode=stat.st_ino if stat else None)
        identities.append(entry)
    return identities

# 子进程显式 PATH：把 CLI 所在目录放最前面
SUBPROCESS_PATH = os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")
if CLAUDE_BIN:
    _bin_dir = str(Path(CLAUDE_BIN).parent)
    if _bin_dir not in SUBPROCESS_PATH:
        SUBPROCESS_PATH = f"{_bin_dir}:{SUBPROCESS_PATH}"

for d in (DATA_DIR, CLONES_DIR):
    d.mkdir(parents=True, exist_ok=True)


def credential_status() -> dict:
    claude_machine_login = (Path.home() / ".claude").exists()
    from . import db
    return {
        "github_token": bool(db.get("SELECT 1 FROM github_connections WHERE disabled=0 LIMIT 1")),
        "claude": bool(CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY) or claude_machine_login,
        "kimi": bool(get_kimi_api_key()),
        "webhook_secret": bool(WEBHOOK_SECRET),
        "public_base_url": PUBLIC_BASE_URL,
        "org": GITHUB_ORG,
    }


# ---------- 运行时配置：网站里改的存 DB 立即生效；.env 只是兜底 ----------

def _db_get(key: str):
    try:
        from . import db
        return db.get_setting(key)
    except Exception:
        return None


def get_github_token() -> str:
    return _db_get("github_token") or GITHUB_TOKEN


def get_kimi_api_key() -> str:
    return _db_get("kimi_api_key") or KIMI_API_KEY


def get_claude_token() -> str:
    return _db_get("claude_token") or CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY


def get_kimi_base_url() -> str:
    return _db_get("kimi_base_url") or KIMI_BASE_URL


def get_kimi_model() -> str:
    return _db_get("kimi_model") or KIMI_MODEL


def get_claude_model() -> str:
    return _db_get("claude_model") or ""

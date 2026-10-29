"""模型 API 节点：凭证按 ID 绑定，密钥只在调用边界读取。"""
import ipaddress
import json
from urllib.parse import urlparse

import httpx

from .. import connections, db


PROVIDERS = {
    "openai": ("OpenAI", "https://api.openai.com/v1", "chat"),
    "anthropic": ("Anthropic", "https://api.anthropic.com/v1", "anthropic"),
    "gemini": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "chat"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com", "chat"),
    "moonshot": ("Moonshot / Kimi", "https://api.moonshot.cn/v1", "chat"),
    "qwen": ("阿里云百炼 / Qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1", "chat"),
    "zhipu": ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "chat"),
    "siliconflow": ("SiliconFlow", "https://api.siliconflow.cn/v1", "chat"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "chat"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "chat"),
    "mistral": ("Mistral", "https://api.mistral.ai/v1", "chat"),
    "azure": ("Azure OpenAI / Foundry", "", "chat"),
    "bedrock": ("Amazon Bedrock (API Key)", "", "chat"),
    "custom": ("自定义 OpenAI 兼容", "", "chat"),
}


def validate_base_url(provider: str, base_url: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError("未知模型服务商")
    if not base_url:
        base_url = PROVIDERS[provider][1]
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL 必须是无账号和查询参数的 HTTPS 地址")
    try:
        addr = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        addr = None
    if addr is not None and not addr.is_global:
        raise ValueError("Base URL 不能指向内网地址")
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("Base URL 不能指向本机")
    return base_url.rstrip("/")


def list_public() -> list[dict]:
    return [dict(row) for row in db.query(
        "SELECT id,label,provider,base_url,disabled,created_at,updated_at "
        "FROM model_credentials ORDER BY id")]


def create(label: str, provider: str, key: str, base_url: str = "") -> int:
    if not label.strip() or not key.strip():
        raise ValueError("名称和 API Key 不能为空")
    url = validate_base_url(provider, base_url)
    ref = connections._write_secret(key)
    try:
        return db.execute(
            "INSERT INTO model_credentials(label,provider,base_url,secret_ref,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)", (label.strip(), provider, url, ref, db.now(), db.now()))
    except Exception:
        connections._secret_path(ref).unlink(missing_ok=True)
        raise


def rotate(cid: int, key: str) -> None:
    row = db.get("SELECT secret_ref FROM model_credentials WHERE id=?", (cid,))
    if not row or not key.strip():
        raise ValueError("凭证不存在或 API Key 为空")
    ref = connections._write_secret(key)
    db.execute("UPDATE model_credentials SET secret_ref=?,updated_at=? WHERE id=?", (ref, db.now(), cid))
    connections._secret_path(row["secret_ref"]).unlink(missing_ok=True)


def delete(cid: int) -> None:
    row = db.get("SELECT secret_ref FROM model_credentials WHERE id=?", (cid,))
    if not row:
        raise ValueError("凭证不存在")
    db.execute("DELETE FROM model_credentials WHERE id=?", (cid,))
    connections._secret_path(row["secret_ref"]).unlink(missing_ok=True)


def get_active(cid: int) -> dict:
    row = db.get("SELECT * FROM model_credentials WHERE id=?", (cid,))
    if not row or row["disabled"]:
        raise ValueError(f"模型凭证 #{cid} 不可用")
    return dict(row)


def run(step: dict, prompt: str, *, timeout: int = 180, max_tokens: int = 4096) -> str:
    credential = get_active(int(step["credential_id"]))
    key = connections._secret_path(credential["secret_ref"]).read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError("模型凭证内容为空")
    model = (step.get("model") or "").strip()
    if not model:
        raise ValueError("API 节点必须指定模型 ID")
    base = validate_base_url(credential["provider"], credential["base_url"])
    if credential["provider"] == "anthropic":
        url = base + "/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        payload = {"model": model, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": prompt}]}
    else:
        url = base + "/chat/completions"
        headers = {"api-key": key} if credential["provider"] == "azure" else {"Authorization": "Bearer " + key}
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if max_tokens != 4096:  # 手动检测显式限制输出；普通评审保持现有兼容请求
            payload["max_tokens"] = max_tokens
        if credential["provider"] == "gemini":
            headers["x-goog-api-client"] = "review-hub/1.0"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2_000_000:
                        raise RuntimeError("模型响应超过 2 MB 上限")
                if response.status_code >= 300:
                    raise RuntimeError(f"模型 API 返回 HTTP {response.status_code}：{bytes(raw[:300]).decode('utf-8', 'replace').replace(key, '[REDACTED]')}")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"模型 API 请求失败：{str(exc).replace(key, '[REDACTED]')[:300]}") from exc
    try:
        data = json.loads(raw)
        if credential["provider"] == "anthropic":
            text = "\n".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text")
        else:
            content = data["choices"][0]["message"]["content"]
            text = content if isinstance(content, str) else "\n".join(
                x.get("text", "") for x in content if isinstance(x, dict))
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型 API 响应缺少可读取的文本") from exc
    if not text.strip():
        raise RuntimeError("模型 API 返回空文本")
    return text

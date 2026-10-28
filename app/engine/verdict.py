"""裁决：合并两个模型的发现。与 quality-gates/scripts/verdict.py 同逻辑。"""
import json
import re
from difflib import SequenceMatcher


def extract_findings(text: str):
    blocks = re.findall(r"```json\s*(.*?)```", text or "", flags=re.S)
    for raw in reversed(blocks):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            findings = data["findings"]
            if all(isinstance(f, dict) and isinstance(f.get("title"), str) and f["title"].strip()
                   for f in findings):
                return findings
    return None


def _title_key(f: dict) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(f.get("title", "")).lower())


def similar(a: dict, b: dict) -> bool:
    """两条发现是否指向同一问题：同文件 ±15 行且标题相似。裁决合并与评测命中共用同一把尺子。"""
    same_area = a.get("file") == b.get("file") and abs(int(a.get("line") or 0) - int(b.get("line") or 0)) <= 15
    return same_area and SequenceMatcher(None, _title_key(a), _title_key(b)).ratio() >= 0.55


def _norm(f: dict, src: str) -> dict:
    sev = str(f.get("severity", "Nit")).upper()
    if sev not in {"IMPORTANT", "NIT", "PRE-EXISTING"}:
        sev = "NIT"
    try:
        line = int(f.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    return {
        "severity": sev,
        "file": str(f.get("file", "")).lstrip("./"),
        "line": line,
        "title": str(f.get("title", "")).strip(),
        "condition": str(f.get("condition", "")).strip(),
        "fix": str(f.get("fix", "")).strip(),
        "sources": [src],
    }


def merge(claude_text: str, codex_text: str) -> dict:
    """兼容旧签名：两个评审者。"""
    return merge_multi([("claude", claude_text), ("codex", codex_text)])


def merge_multi(reviews: list) -> dict:
    """reviews: [(来源名, 输出文本), ...]，数量任意。
    合并规则：≥2 个来源报了相似发现 → Important（交叉确认）；单来源 → Nit（未交叉验证）。"""
    claude_ok = codex_ok = None
    findings = []
    for src, text in reviews:
        raw = extract_findings(text)
        ok = raw is not None
        if src == "claude":
            claude_ok = ok
        if src == "codex":
            codex_ok = ok
        for f in raw or []:
            if isinstance(f, dict):
                findings.append(_norm(f, src))

    # 组内摆放只控制执行先后。固定合并遍历顺序，避免相似度非传递时
    # 先完成的评审器改变最终聚类和严重度。
    findings.sort(key=lambda f: (f["file"], int(f["line"] or 0), _title_key(f), f["sources"][0]))
    merged: list = []
    for f in findings:
        hit = None
        for m in merged:
            if similar(m, f):
                hit = m
                break
        if hit is not None:
            for s in f["sources"]:
                if s not in hit["sources"]:
                    hit["sources"].append(s)
        else:
            merged.append(f)

    # 交叉确认：被 ≥2 个来源报告的 → Important；只有 1 个来源 → Nit。
    # 但若流水线只有一个有效评审者，无法交叉验证，保持原始严重度（fail loud 不放水）。
    valid_sources = {s for s, text in reviews if extract_findings(text) is not None}
    cross_confirm = len(valid_sources) >= 2
    for m in merged:
        if cross_confirm:
            if len(m["sources"]) >= 2:
                m["severity"] = "IMPORTANT"
            elif m["severity"] == "IMPORTANT":
                m["severity"] = "NIT"

    order = {"IMPORTANT": 0, "NIT": 1, "PRE-EXISTING": 2}
    merged.sort(key=lambda f: order.get(f["severity"], 1))
    imp = sum(1 for f in merged if f["severity"] == "IMPORTANT")
    parts = []
    for src, text in sorted(reviews, key=lambda item: item[0]):
        if extract_findings(text) is None:
            parts.append(f"{src} 无有效输出")
    parts.append(f"Important {imp} / 共 {len(merged)} 条")
    return {
        "findings": merged,
        "claude_ok": claude_ok,
        "codex_ok": codex_ok,
        "important_count": imp,
        "summary": "；".join(parts),
    }

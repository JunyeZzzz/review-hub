"""风险路由：与 quality-gates/policy/risk-rules.yml 同一套规则。"""
import re
from pathlib import Path

import yaml

from .. import config

_cache: dict = {}


def _rules() -> dict:
    if "rules" not in _cache:
        path = config.POLICY_DIR / "risk-rules.yml"
        _cache["rules"] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _cache["rules"]


def glob_to_re(pat: str) -> re.Pattern:
    r = pat.replace("**/", "\x01").replace("**", "\x02").replace("*", "\x03").replace("?", "\x04")
    r = re.escape(r)
    r = r.replace("\x01", "(?:.*/)?").replace("\x02", ".*").replace("\x03", "[^/]*").replace("\x04", "[^/]")
    return re.compile("^" + r + "$")


def classify(files: list, changed_lines: int) -> dict:
    rules = _rules()
    tiers = rules.get("tiers", {})
    high_pats = [glob_to_re(p) for p in tiers.get("high", {}).get("paths", [])]
    trivial_pats = [glob_to_re(p) for p in tiers.get("trivial", {}).get("paths", [])]
    defaults = rules.get("defaults", {})
    med_min = int(defaults.get("medium_min_changed_lines", 400))

    hit_high = any(rx.match(f) for f in files for rx in high_pats)
    all_trivial = bool(files) and all(any(rx.match(f) for rx in trivial_pats) for f in files)
    if hit_high:
        tier = "high"
    elif all_trivial:
        tier = "trivial"
    elif changed_lines >= med_min:
        tier = "medium"
    else:
        tier = defaults.get("base_tier", "low")

    ai = rules.get("ai_review", {})
    return {
        "tier": tier,
        "run_ai": tier not in ai.get("skip_for", ["trivial"]),
        "block_on_important": bool(ai.get("block_on_important", {}).get(tier, False)),
    }

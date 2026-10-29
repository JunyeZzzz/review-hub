"""评审 prompt 构建：初始提示词可在配置中心编辑（存 settings 表）。"""
import subprocess
from pathlib import Path

from .. import config, db

MAX_REVIEW_PART_LINES = 4000
MAX_REVIEW_PART_CHARS = 160_000
MAX_REVIEW_PARTS = 40

DEFAULT_TEMPLATE = """你是交叉评审组中的独立代码评审员。只根据上游项目上下文和你自己核验的证据判断，不参考其他评审员的结论；以怀疑态度专查真实缺陷，不负责夸奖。

## 输入

- 仓库根目录：`{repo_root}`
- 本节点待审 diff（必读，unified patch）：`{DIFF_FILE}`。大变更会拆片，系统只在全部片段审完后汇总。
- 评审规则（必读，严格执行）：`{REVIEW_MD}`
{profile_section}{intent_section}
## 工作方式

1. 先通读 diff，列出你怀疑的点
2. 用 Read / Grep / Glob 打开相关源码验证：确认调用方、数据流、边界条件是否真的如你所想
3. 按评审规则过滤：说不出触发条件的不报；风格问题不报；diff 之前已存在的问题归入 Pre-existing 且只给一句话
4. 同一坏模式多处出现：只报一条，title 写明"这一类"，在 condition 里列出所有位置
5. 不要修改任何文件，你是只读的评审员

## 输出格式（严格执行）

先写不超过 5 行的总评（其中必须有一句明确回答：本次变更达成了它的声明目的吗？），然后必须以一个 ```json 代码块结尾：

```json
{{"findings": [{{"severity": "Important", "file": "src/x.ts", "line": 42, "title": "一句话问题", "condition": "什么输入/场景触发", "fix": "建议修法"}}]}}
```

- severity 只能取：Important / Nit / Pre-existing
- 没有发现时输出 `{{"findings": []}}`
- JSON 代码块之后不要再输出任何内容
""".replace("{DIFF_FILE}", "{diff_file}").replace("{REVIEW_MD}", "{review_md}")


def get_template() -> str:
    """初始提示词：DB 里存了用 DB 的（用户在配置中心改的），否则默认。"""
    saved = db.get_setting("prompt_template")
    return saved if saved else DEFAULT_TEMPLATE


INIT_PROMPT = """根据下面的仓库信息，用**不超过 3 句中文**概括这个项目是做什么的：面向谁、解决什么问题、主要技术栈。

要求：
- 只输出概括本身，不要标题、不要开场白、不要列举文件
- 信息不足就概括你能确定的部分，不要编造功能

## 仓库信息
- 名称：{name}
- 描述：{description}
- 主语言：{language}
- README（可能截断）：
{readme}
"""

def build(workdir: Path, intent: str = "", profile: str = "", diff_spec: str = "") -> dict:
    """生成 diff 与 prompt 文件；提供 diff_spec 时必须严格使用该审查区间。"""
    base = f"origin/{_default_branch(workdir)}"
    diff_result = subprocess.run(
        ["git", "diff", diff_spec or f"{base}...HEAD"], cwd=workdir,
        capture_output=True, text=True,
    )
    if diff_spec and diff_result.returncode:
        raise RuntimeError(f"无法取得审查区间 {diff_spec}：{diff_result.stderr[-400:]}")
    diff = diff_result.stdout if diff_result.returncode == 0 else ""
    if not diff_spec and not diff.strip():
        # 本地仓库可能没有 origin：退回比较上一次提交
        fallback = subprocess.run(
            ["git", "diff", "HEAD~1...HEAD"], cwd=workdir,
            capture_output=True, text=True,
        )
        if fallback.returncode:
            raise RuntimeError(f"无法取得审查 diff：{(fallback.stderr or diff_result.stderr)[-400:]}")
        diff = fallback.stdout
    if not diff.strip():
        raise RuntimeError("审查 diff 为空，不能判定通过")
    if any(line.startswith("Binary files ") or
           line.startswith(("+Subproject commit ", "-Subproject commit ")) or
           line.startswith(("new file mode 120000", "deleted file mode 120000")) or
           (line.startswith("index ") and line.endswith(" 120000"))
           for line in diff.splitlines()):
        raise RuntimeError("变更包含二进制文件、符号链接或子模块指针，文本审查无法完整核验；需要人工核验，不能自动判定通过")
    lines = diff.splitlines(keepends=True)
    parts = _review_parts(lines)

    diff_file = workdir / ".hub-diff.patch"
    prompt_file = workdir / ".hub-prompt.txt"
    diff_file.write_text(diff, encoding="utf-8")
    part_files = []
    if len(parts) > 1:
        for index, part in enumerate(parts, 1):
            path = workdir / f".hub-diff-part-{index}.patch"
            path.write_text(part, encoding="utf-8")
            part_files.append(path)
    profile_section = ""
    if profile.strip():
        profile_section = (
            "\n## 项目背景（这个项目是干什么的，人工确认过）\n\n"
            f"{profile.strip()}\n"
        )
    intent_section = ""
    if intent.strip():
        intent_section = (
            "\n## 本次变更的声明目的（commit message / PR 描述）\n\n"
            f"{intent.strip()}\n\n"
            "评审第一步：判断这个 diff 是否达成了声明目的。未达成、只部分达成、"
            "或夹带了与目的无关的改动 → 作为 Important 报告；声明目的本身有误也要指出。\n"
        )
    prompt_file.write_text(get_template().format(
        repo_root=str(workdir),
        diff_file=str(diff_file),
        truncated="no（完整变更；大变更会逐片审查）",
        review_md=str(config.POLICY_DIR / "REVIEW.md"),
        intent_section=intent_section,
        profile_section=profile_section,
    ), encoding="utf-8")
    return {"prompt_file": prompt_file, "diff_file": diff_file,
            "diff_lines": len(lines), "truncated": False,
            "review_parts": part_files,
            "intent": intent, "profile": profile}


def _review_parts(lines: list[str]) -> list[str]:
    """把完整 patch 分成有界输入；每一行恰好进入一个审查片段。"""
    parts, current, size = [], [], 0
    last_file, last_hunk = "", ""
    for number, line in enumerate(lines, 1):
        if len(line) > MAX_REVIEW_PART_CHARS - 1000:
            raise RuntimeError("diff 存在超长单行，无法完整审查；请拆分变更")
        if current and (len(current) >= MAX_REVIEW_PART_LINES or size + len(line) > MAX_REVIEW_PART_CHARS - 500):
            parts.append("".join(current))
            prefix = (f"# Review Hub 续片：原始 diff 第 {number} 行；文件 {last_file[:200]}；"
                      f"所在块 {last_hunk[:200]}。此行以下是该块的后续内容，请按文件核实真实行号。\n")
            current, size = [prefix], len(prefix)
        current.append(line)
        size += len(line)
        if line.startswith("diff --git "):
            last_file, last_hunk = line.strip(), ""
        elif line.startswith("@@ "):
            last_hunk = line.strip()
    if current:
        parts.append("".join(current))
    if len(parts) > MAX_REVIEW_PARTS:
        raise RuntimeError(f"变更需拆成 {len(parts)} 个审查片段，超过上限 {MAX_REVIEW_PARTS}；请拆分提交，不能截断后判通过")
    return parts


def _default_branch(workdir: Path) -> str:
    r = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
        cwd=workdir, capture_output=True, text=True,
    )
    if r.returncode == 0 and "/" in r.stdout.strip():
        return r.stdout.strip().split("/", 1)[1]
    return "main"

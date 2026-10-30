"""SQLite 持久层。线程内各自建连接（check_same_thread=False + 每次新连接）。"""
import sqlite3
import threading
import json
from datetime import datetime, timezone

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                -- github | local
  full_name TEXT NOT NULL UNIQUE,    -- owner/repo 或本地绝对路径
  name TEXT NOT NULL,
  default_branch TEXT NOT NULL DEFAULT 'main',
  enabled INTEGER NOT NULL DEFAULT 1,
  provider TEXT NOT NULL DEFAULT 'auto',
  auto_revert INTEGER NOT NULL DEFAULT 0,
  webhook_attached INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_run_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  trigger TEXT NOT NULL,             -- manual | push | pr | merged
  ref TEXT, base_ref TEXT, head_sha TEXT,
  pr_number INTEGER,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued|running|passed|blocked|failed
  tier TEXT, summary TEXT,
  important_count INTEGER DEFAULT 0,
  findings_json TEXT,
  diff_complete INTEGER,              -- NULL=旧报告未知；1=本次保存了完整 diff
  error TEXT,
  files_changed INTEGER DEFAULT 0,
  duration_ms INTEGER,
  revert_pr_url TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- running|ok|warn|fail|skip
  log TEXT DEFAULT '',
  trace TEXT,                              -- 思考过程轨迹（JSON）：thinking/tool/text 事件流
  prompt TEXT,                             -- 评审步骤实际收到的完整输入提示词
  duration_ms INTEGER,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS github_connections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL UNIQUE,
  token_ref TEXT NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0,
  account_login TEXT,
  last_checked_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL UNIQUE,
  provider TEXT NOT NULL,
  base_url TEXT NOT NULL,
  secret_ref TEXT NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connectivity_checks (
  target TEXT PRIMARY KEY,            -- api:<id> | agent:claude | agent:codex
  status TEXT NOT NULL,                -- ok | failed
  detail TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL,
  checked_at TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS finding_marks (
  run_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,                  -- findings_json 数组下标（run 结束后不再变化）
  disposition TEXT NOT NULL,             -- confirmed | false_positive
  note TEXT DEFAULT '',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, idx)
);
CREATE TABLE IF NOT EXISTS run_failure_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('handled','reopen')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_failure_events_run ON run_failure_events(run_id,id);
CREATE TABLE IF NOT EXISTS eval_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  run_id INTEGER,
  idx INTEGER,
  polarity TEXT NOT NULL DEFAULT 'positive',  -- positive=应报出 | negative=不应再报出
  head_sha TEXT NOT NULL,
  intent TEXT DEFAULT '',                -- 原 run 的声明目的，跑分时保持评审条件一致
  expected TEXT NOT NULL,                -- 期望发现 JSON（单条）
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(run_id, idx)
);
CREATE TABLE IF NOT EXISTS config_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL,                -- prompt_template | rules | pipeline | project_pipeline:{pid}
  value TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_feedback (
 id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
 run_id INTEGER NOT NULL, idx INTEGER NOT NULL, disposition TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
 finding TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(run_id,idx)
);
CREATE TABLE IF NOT EXISTS review_improvements (
 id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
 title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'check',
 scope TEXT NOT NULL DEFAULT '*', rationale TEXT NOT NULL DEFAULT '',
 instruction TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, feedback_id INTEGER NOT NULL,
 snapshot TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS improvement_sources (
 improvement_id INTEGER NOT NULL, feedback_id INTEGER NOT NULL,
 PRIMARY KEY(improvement_id,feedback_id)
);
CREATE TABLE IF NOT EXISTS improvement_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, improvement_id INTEGER NOT NULL,
 action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS improvement_validations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, improvement_id INTEGER NOT NULL,
 revision INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'running',
 fingerprint TEXT NOT NULL, snapshot TEXT NOT NULL,
 results TEXT NOT NULL DEFAULT '[]', progress TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '', eligible INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS improvement_uses (
 run_id INTEGER NOT NULL, improvement_id INTEGER NOT NULL, revision INTEGER NOT NULL,
 snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,improvement_id)
);
CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL DEFAULT 'running',     -- running|done|failed
  cases_total INTEGER DEFAULT 0,
  hit INTEGER DEFAULT 0, miss INTEGER DEFAULT 0,
  fp INTEGER DEFAULT 0, error INTEGER DEFAULT 0,
  config_snapshot TEXT,
  results TEXT,
  error_msg TEXT,
  current_case_id INTEGER,
  current_stage TEXT,
  current_detail TEXT,
  current_trace TEXT,
  created_at TEXT NOT NULL, finished_at TEXT
);
"""

RUN_MIGRATE_COLS = ("branch", "commit_msg", "commit_author", "commit_date", "intent",
                    "additions", "deletions", "approval_json", "approval_at",
                    "pipeline_snapshot", "diff_text", "diff_complete")
PROJECT_MIGRATE = (("profile", "TEXT"), ("init_status", "TEXT DEFAULT 'done'"), ("last_sha", "TEXT"))
STEP_MIGRATE_COLS = ("trace", "prompt")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


def init() -> None:
    c = conn()
    c.executescript(SCHEMA)
    eval_cols = {r[1] for r in c.execute("PRAGMA table_info(eval_runs)")}
    validation_cols = {r[1] for r in c.execute('PRAGMA table_info(improvement_validations)')}
    if 'review_note' not in validation_cols:
        c.execute("ALTER TABLE improvement_validations ADD COLUMN review_note TEXT NOT NULL DEFAULT ''")
    improvement_cols = {r[1] for r in c.execute('PRAGMA table_info(review_improvements)')}
    if 'activation_signature' not in improvement_cols:
        c.execute("ALTER TABLE review_improvements ADD COLUMN activation_signature TEXT NOT NULL DEFAULT ''")
    for col, definition in (("current_case_id", "INTEGER"), ("current_stage", "TEXT"),
                            ("current_detail", "TEXT"), ("current_trace", "TEXT")):
        if col not in eval_cols:
            c.execute(f"ALTER TABLE eval_runs ADD COLUMN {col} {definition}")
    # 轻量迁移：补列（存在则跳过）
    existing = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    for col in ("branch", "commit_msg", "commit_author", "commit_date", "intent",
                "additions", "deletions", "approval_json", "approval_at",
                "pipeline_snapshot", "diff_text"):
        if col not in existing:
            c.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
    if "diff_complete" not in existing:
        c.execute("ALTER TABLE runs ADD COLUMN diff_complete INTEGER")
    pcols = {r[1] for r in c.execute("PRAGMA table_info(projects)")}
    for col, ddl in (("profile", "TEXT"), ("init_status", "TEXT DEFAULT 'done'"),
                     ("last_sha", "TEXT"), ("pipeline_json", "TEXT"),
                     ("github_connection_id", "INTEGER")):
        if col not in pcols:
            c.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
    scols = {r[1] for r in c.execute("PRAGMA table_info(steps)")}
    for col in ("trace", "prompt"):
        if col not in scols:
            c.execute(f"ALTER TABLE steps ADD COLUMN {col} TEXT")
    ccols = {r[1] for r in c.execute("PRAGMA table_info(github_connections)")}
    for col in ("account_login", "last_checked_at", "last_error"):
        if col not in ccols:
            c.execute(f"ALTER TABLE github_connections ADD COLUMN {col} TEXT")
    c.commit()
    if get_setting('feedback_samples_migration_v1') != 'done':
        # 历史有效判定补齐样本；只迁移一次，之后删除样本不会被启动恢复。
        c.execute("INSERT OR IGNORE INTO eval_cases(project_id,run_id,idx,polarity,head_sha,intent,expected,created_at) "
                  "SELECT r.project_id,m.run_id,m.idx,CASE WHEN m.disposition='confirmed' THEN 'positive' ELSE 'negative' END,"
                  "r.head_sha,COALESCE(r.intent,''),j.value,m.updated_at "
                  "FROM finding_marks m JOIN runs r ON r.id=m.run_id JOIN projects p ON p.id=r.project_id "
                  "JOIN json_each(COALESCE(r.findings_json,'[]')) j ON CAST(j.key AS INTEGER)=m.idx "
                  "WHERE p.kind='github' AND COALESCE(r.head_sha,'')<>'' "
                  "AND NOT EXISTS (SELECT 1 FROM json_each(json_extract(j.value,'$.sources')) s WHERE s.value LIKE 'gate:%' OR s.value='gitleaks-like')")
        c.commit()
        set_setting('feedback_samples_migration_v1', 'done')
    # 旧判定进入反馈收件箱；不推断原因，不自动采用任何策略。
    c.execute("INSERT OR IGNORE INTO review_feedback(project_id,run_id,idx,disposition,reason,finding,created_at,updated_at) "
              "SELECT r.project_id,m.run_id,m.idx,m.disposition,COALESCE(m.note,''),j.value,m.updated_at,m.updated_at "
              "FROM finding_marks m JOIN runs r ON r.id=m.run_id "
              "JOIN json_each(COALESCE(r.findings_json,'[]')) j ON CAST(j.key AS INTEGER)=m.idx")
    c.commit()
    if get_setting("pipeline_node_migration_v1") != "done":
        # 旧执行器曾在读取时隐式补测试门禁。迁移一次使真实执行顺序可见；
        # 新配置删除门禁后不会再次自动补入。
        old_global = get_setting("pipeline")
        if old_global:
            try:
                steps = json.loads(old_global)
                if isinstance(steps, list) and steps and not any(s.get("kind") == "gate" for s in steps):
                    steps.insert(0, {"id": "gate-test", "kind": "gate", "name": "测试门禁",
                                     "gate": "tests", "enabled": True})
                    set_setting("pipeline", json.dumps(steps, ensure_ascii=False))
            except (ValueError, TypeError, AttributeError):
                pass
        for row in query("SELECT id,pipeline_json FROM projects WHERE pipeline_json IS NOT NULL"):
            try:
                steps = json.loads(row["pipeline_json"])
                if isinstance(steps, list) and steps and not any(s.get("kind") == "gate" for s in steps):
                    steps.insert(0, {"id": "gate-test", "kind": "gate", "name": "测试门禁",
                                     "gate": "tests", "enabled": True})
                    execute("UPDATE projects SET pipeline_json=? WHERE id=?",
                            (json.dumps(steps, ensure_ascii=False), row["id"]))
            except (ValueError, TypeError, AttributeError):
                pass
        set_setting("pipeline_node_migration_v1", "done")
    from . import connections
    connections.migrate_legacy()


def get_setting(key: str):
    row = get("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    if value in (None, ""):
        execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        execute("INSERT INTO settings(key,value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def query(sql: str, args: tuple = ()) -> list:
    return conn().execute(sql, args).fetchall()


def get(sql: str, args: tuple = ()):
    return conn().execute(sql, args).fetchone()


def execute(sql: str, args: tuple = ()) -> int:
    c = conn()
    cur = c.execute(sql, args)
    c.commit()
    return cur.lastrowid

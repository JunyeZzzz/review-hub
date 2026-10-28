"""SQLite 持久层。线程内各自建连接（check_same_thread=False + 每次新连接）。"""
import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS finding_marks (
  run_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,                  -- findings_json 数组下标（run 结束后不再变化）
  disposition TEXT NOT NULL,             -- confirmed | false_positive
  note TEXT DEFAULT '',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, idx)
);
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
CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL DEFAULT 'running',     -- running|done|failed
  cases_total INTEGER DEFAULT 0,
  hit INTEGER DEFAULT 0, miss INTEGER DEFAULT 0,
  fp INTEGER DEFAULT 0, error INTEGER DEFAULT 0,
  config_snapshot TEXT,
  results TEXT,
  error_msg TEXT,
  created_at TEXT NOT NULL, finished_at TEXT
);
"""

RUN_MIGRATE_COLS = ("branch", "commit_msg", "commit_author", "commit_date", "intent",
                    "additions", "deletions", "approval_json", "approval_at",
                    "pipeline_snapshot", "diff_text")
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
    # 轻量迁移：补列（存在则跳过）
    existing = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    for col in ("branch", "commit_msg", "commit_author", "commit_date", "intent",
                "additions", "deletions", "approval_json", "approval_at",
                "pipeline_snapshot", "diff_text"):
        if col not in existing:
            c.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
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

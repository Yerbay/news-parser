"""SQLite: источники, статьи, события (склеенные статьи), отзывы, настройки."""
import json
import sqlite3
import time

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,              -- rss | html | hf_papers | hn
    weight INTEGER NOT NULL DEFAULT 2,
    category TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_ok INTEGER,
    last_error TEXT,
    fail_since INTEGER,
    warned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY,
    event TEXT NOT NULL,             -- короткое название события (для склейки)
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    llm_score INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0,
    best_article_id INTEGER,
    sent_at INTEGER,
    sent_kind TEXT                   -- digest | urgent
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    published INTEGER,
    fetched_at INTEGER NOT NULL,
    text TEXT,
    metric INTEGER,                  -- очки HN / апвоуты HF
    metric_kind TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- new | done | skip
    lang TEXT,
    llm_score INTEGER,
    excluded INTEGER,
    summary_ru TEXT,
    summary_orig TEXT,
    cluster_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_cluster ON articles(cluster_id);
CREATE INDEX IF NOT EXISTS idx_clusters_created ON clusters(created_at);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    cluster_id INTEGER NOT NULL,
    value INTEGER NOT NULL,          -- 1 полезно, -1 не интересно
    at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULTS = {
    "digest_time": "08:30",
    "digest_count": 20,
    "min_score": 6,          # минимальная оценка LLM, чтобы попасть в сводку
    "urgent_on": True,
    "urgent_sources": 3,     # срочно: столько сильных источников (вес ≥2) за 3 часа
    "bilingual": True,       # саммари ещё и на языке оригинала
    "topics_interest": "AI, большие языковые модели (LLM), агенты, инструменты разработки и программирования с ИИ, "
                       "открытые модели, исследования в машинном обучении, инференс и железо для ИИ",
    "topics_exclude": "политика, криминал, шоу-бизнес, финансовое регулирование",
    "last_digest_at": 0,
    "last_collect_at": 0,
}

conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
conn.executescript(SCHEMA)
conn.commit()


def now() -> int:
    return int(time.time())


def get(key: str):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else DEFAULTS.get(key)


def put(key: str, value) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


# ---------------------------------------------------------------- источники

def sources(enabled_only: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM sources" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY weight DESC, name"
    return conn.execute(q).fetchall()


def source(source_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()


def source_by_url(url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE url=?", (url,)).fetchone()


def add_source(name: str, url: str, kind: str, weight: int, category: str | None = None) -> int:
    """Добавляет источник; удалённый ранее с тем же адресом включается обратно."""
    conn.execute(
        "INSERT INTO sources(name, url, kind, weight, category, created_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET enabled=1, name=excluded.name, kind=excluded.kind, weight=excluded.weight, "
        "fail_since=NULL, warned=0",
        (name, url, kind, weight, category, now()))
    conn.commit()
    return source_by_url(url)["id"]


def set_source(source_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE sources SET {cols} WHERE id=?", (*fields.values(), source_id))
    conn.commit()


def source_ok(source_id: int) -> None:
    set_source(source_id, last_ok=now(), last_error=None, fail_since=None, warned=0)


def source_failed(source_id: int, error: str) -> None:
    conn.execute("UPDATE sources SET last_error=?, fail_since=COALESCE(fail_since, ?) WHERE id=?",
                 (error[:300], now(), source_id))
    conn.commit()


def has_articles(source_id: int) -> bool:
    return conn.execute("SELECT 1 FROM articles WHERE source_id=? LIMIT 1", (source_id,)).fetchone() is not None


# ---------------------------------------------------------------- статьи

def known_urls(urls: list[str]) -> set[str]:
    if not urls:
        return set()
    marks = ",".join("?" * len(urls))
    return {r["url"] for r in conn.execute(f"SELECT url FROM articles WHERE url IN ({marks})", urls)}


def save_article(source_id: int, item, status: str = "new") -> bool:
    """Сохраняет статью. Если такая ссылка уже есть — только обновляет метрику (очки растут). True — новая."""
    cur = conn.execute(
        "INSERT INTO articles(source_id, url, title, published, fetched_at, text, metric, metric_kind, status) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING",
        (source_id, item.url, item.title, item.published, now(), item.text, item.metric, item.metric_kind, status))
    if cur.rowcount == 0 and item.metric is not None:
        conn.execute("UPDATE articles SET metric=MAX(COALESCE(metric, 0), ?), metric_kind=? WHERE url=?",
                     (item.metric, item.metric_kind, item.url))
    return cur.rowcount > 0


def commit() -> None:
    conn.commit()


def cleanup() -> None:
    """Срок хранения: статьи и события старше RETENTION_DAYS удаляются."""
    border = now() - config.RETENTION_DAYS * 86400
    conn.execute("DELETE FROM articles WHERE fetched_at < ?", (border,))
    conn.execute("DELETE FROM clusters WHERE updated_at < ? AND id NOT IN "
                 "(SELECT DISTINCT cluster_id FROM articles WHERE cluster_id IS NOT NULL)", (border,))
    conn.execute("DELETE FROM feedback WHERE at < ?", (border,))
    conn.commit()
    conn.execute("VACUUM")

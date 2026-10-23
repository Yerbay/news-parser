"""Конвейер: сбор → очистка и склейка (LLM) → ранжирование → отбор для сводки и срочных."""
import asyncio
import json
import logging
from datetime import datetime

import config
import db
import fetchers
import llm

log = logging.getLogger(__name__)
lock = asyncio.Lock()  # сбор и обработка не идут параллельно (часовой обход и /now)
BATCH = 10
EVENTS_CONTEXT_HOURS = 48
URGENT_WINDOW_HOURS = 3
LLM_FACTOR = 1.5


# ---------------------------------------------------------------- сбор

def save_items(source, items: list, first_run: bool) -> int:
    """Сохраняет статьи источника. Старые (и при первом обходе HTML — без даты) сразу помечаются skip."""
    now = db.now()
    border = now - config.FRESH_HOURS * 3600
    new = 0
    for it in items:
        if it.published and it.published > now + 3600:
            it.published = now  # дата из будущего — ошибка сайта
        stale = (it.published and it.published < border) or (first_run and source["kind"] == "html" and not it.published)
        new += db.save_article(source["id"], it, "skip" if stale else "new")
    db.commit()
    return new


async def _collect_one(http, source) -> int:
    first_run = not db.has_articles(source["id"])
    try:
        items = await fetchers.fetch_source(
            http, source["kind"], source["url"],
            is_new_url=lambda urls: set(urls) - db.known_urls(urls), first_run=first_run,
            link_prefix=source["link_prefix"])
    except Exception as e:  # ошибка одного источника не останавливает остальные
        log.warning("Источник %s: %s", source["name"], e)
        db.source_failed(source["id"], str(e) or type(e).__name__)
        return 0
    db.source_ok(source["id"])
    return save_items(source, items, first_run)


async def collect(force: bool = False) -> int | None:
    """Обходит все источники. Не чаще раза в COLLECT_MINUTES; None — обход пропущен."""
    if not force and db.now() - db.get("last_collect_at") < (config.COLLECT_MINUTES - 5) * 60:
        return None
    db.put("last_collect_at", db.now())
    async with fetchers.client() as http:
        results = await fetchers.gather_limited([_collect_one(http, s) for s in db.sources()])
    new = sum(r for r in results if isinstance(r, int))
    log.info("Сбор: новых статей %s", new)
    return new


# ---------------------------------------------------------------- очистка, склейка, оценка

def _recent_events(digest: str) -> list[dict]:
    rows = db.conn.execute("SELECT id, event FROM clusters WHERE digest=? AND updated_at >= ? "
                           "ORDER BY updated_at DESC LIMIT 250",
                           (digest, db.now() - EVENTS_CONTEXT_HOURS * 3600)).fetchall()
    return [dict(r) for r in rows]


def _refresh_cluster(cluster_id: int) -> None:
    c = db.conn
    best = c.execute(
        "SELECT a.id FROM articles a JOIN sources s ON s.id = a.source_id WHERE a.cluster_id=? "
        "ORDER BY (COALESCE(a.summary_ru, '') != '') DESC, COALESCE(a.excluded, 0), s.weight DESC, "
        "a.llm_score DESC, a.id LIMIT 1", (cluster_id,)).fetchone()
    agg = c.execute("SELECT MAX(llm_score) m, MIN(COALESCE(excluded, 0)) e FROM articles WHERE cluster_id=?",
                    (cluster_id,)).fetchone()
    c.execute("UPDATE clusters SET best_article_id=?, llm_score=?, excluded=?, updated_at=? WHERE id=?",
              (best["id"] if best else None, agg["m"] or 0, agg["e"] or 0, db.now(), cluster_id))


def _apply(digest: str, batch: list, results: list[dict]) -> None:
    c = db.conn
    by_id = {r["id"]: r for r in batch}
    labels: dict[str, int] = {}
    touched = set()
    for res in results:
        try:
            aid = int(res.get("id"))
        except (TypeError, ValueError):
            continue
        if aid not in by_id:
            continue
        score = max(1, min(10, int(res.get("score") or 1)))
        excluded = 1 if res.get("excluded") else 0
        label = str(res.get("cluster") or f"_{aid}").strip()
        cluster_id = None
        if label[:1].upper() == "C" and label[1:].isdigit():
            if c.execute("SELECT 1 FROM clusters WHERE id=? AND digest=?", (int(label[1:]), digest)).fetchone():
                cluster_id = int(label[1:])
        elif label in labels:
            cluster_id = labels[label]
        if cluster_id is None:
            now = db.now()
            cluster_id = c.execute(
                "INSERT INTO clusters(event, created_at, updated_at, llm_score, excluded, digest) VALUES(?,?,?,?,?,?)",
                (str(res.get("event") or by_id[aid]["title"])[:200], now, now, score, excluded, digest)).lastrowid
            labels[label] = cluster_id
        extra = {f: res[f] for f in llm.EXTRA_FIELDS[digest] if res.get(f) not in (None, "")}
        c.execute(
            "UPDATE articles SET status='done', lang=?, llm_score=?, excluded=?, summary_ru=?, summary_orig=?, "
            "cluster_id=?, extra=? WHERE id=?",
            (str(res.get("lang") or "")[:5], score, excluded, (res.get("summary_ru") or "").strip(),
             (res.get("summary_orig") or "").strip(), cluster_id,
             json.dumps(extra, ensure_ascii=False) if extra else None, aid))
        touched.add(cluster_id)
        del by_id[aid]
    # Статьи, про которые модель промолчала, повторно не гоняем
    for aid in by_id:
        c.execute("UPDATE articles SET status='skip' WHERE id=?", (aid,))
    for cid in touched:
        _refresh_cluster(cid)
    db.commit()


async def _classify_batch(digest: str, batch: list[dict], interests: str, excludes: str) -> None:
    """Пачка через LLM; если ответ не разобрался — делим пополам и повторяем."""
    results = await llm.classify(digest, batch, _recent_events(digest), interests, excludes)
    if results is None and len(batch) > 1:
        half = len(batch) // 2
        await _classify_batch(digest, batch[:half], interests, excludes)
        await _classify_batch(digest, batch[half:], interests, excludes)
        return
    _apply(digest, batch, results or [])


async def process() -> int:
    """Новые статьи — через LLM пачками. Возвращает число обработанных."""
    c = db.conn
    border = db.now() - config.FRESH_HOURS * 3600
    c.execute("UPDATE articles SET status='skip' WHERE status='new' AND COALESCE(published, fetched_at) < ?", (border,))
    db.commit()
    done = 0
    for digest in db.DIGESTS:
        rows = c.execute(
            "SELECT a.id, a.title, a.text, s.name source FROM articles a JOIN sources s ON s.id = a.source_id "
            "WHERE a.status='new' AND s.digest=? ORDER BY COALESCE(a.published, a.fetched_at) DESC LIMIT ?",
            (digest, config.MAX_LLM_ARTICLES)).fetchall()
        interests = db.get(db.key(digest, "topics_interest"))
        excludes = db.get(db.key(digest, "topics_exclude"))
        for i in range(0, len(rows), BATCH):
            batch = [dict(r) for r in rows[i:i + BATCH]]
            for a in batch:
                a["text"] = (a["text"] or "")[:1500]
            try:
                await _classify_batch(digest, batch, interests, excludes)
            except Exception as e:  # сеть или API — остальное доделаем на следующем обходе
                log.warning("LLM: %s", e)
                break
            done += len(batch)
    log.info("Обработано LLM: %s", done)
    return done


# ---------------------------------------------------------------- ранжирование

def _external_bonus(kind: str | None, metric: int | None) -> float:
    if not metric:
        return 0
    steps = {"hn": (150, 300, 600), "hf": (50, 100, 200)}.get(kind or "")
    return float(sum(metric >= s for s in steps)) if steps else 0


def score_cluster(cluster) -> dict | None:
    arts = db.conn.execute(
        "SELECT a.*, s.name source, s.weight FROM articles a JOIN sources s ON s.id = a.source_id "
        "WHERE a.cluster_id=? ORDER BY s.weight DESC, a.id", (cluster["id"],)).fetchall()
    best = next((a for a in arts if a["id"] == cluster["best_article_id"]), arts[0] if arts else None)
    if not best:
        return None
    sources = {a["source_id"]: a for a in arts}
    weight = max(a["weight"] for a in arts)
    cross = min(4.5, 1.5 * (len(sources) - 1))  # кросс-упоминания
    external = max(_external_bonus(a["metric_kind"], a["metric"]) for a in arts)
    recent = db.now() - URGENT_WINDOW_HOURS * 3600
    strong_recent = {a["source_id"] for a in arts if a["weight"] >= 2 and a["fetched_at"] >= recent}
    return {
        "id": cluster["id"],
        # Оценка LLM — главный сигнал, остальные — поправки (вес 1–3, кросс до 4.5, метрики до 3)
        "score": LLM_FACTOR * cluster["llm_score"] + weight + cross + external,
        "llm_score": cluster["llm_score"],
        "title": best["title"],
        "url": best["url"],
        "source": best["source"],
        "summary_ru": best["summary_ru"],
        "summary_orig": best["summary_orig"],
        "lang": best["lang"],
        "extra": json.loads(best["extra"]) if best["extra"] else {},
        "digest": cluster["digest"],
        "other_sources": len(sources) - 1,
        "strong_recent": len(strong_recent),
    }


def _candidates(digest: str, since: int) -> list[dict]:
    rows = db.conn.execute(
        "SELECT * FROM clusters WHERE digest=? AND sent_at IS NULL AND excluded=0 AND llm_score >= ? "
        "AND created_at >= ?", (digest, db.get(db.key(digest, "min_score")), since)).fetchall()
    scored = [s for s in map(score_cluster, rows) if s]
    return sorted(scored, key=lambda s: s["score"], reverse=True)


def digest_items(digest: str) -> list[dict]:
    """Топ за сутки (или с прошлой сводки, если она была раньше — но не дальше 3 суток)."""
    now = db.now()
    since = max(now - 72 * 3600, min(now - 24 * 3600, db.get("last_digest_at") or now))
    limit = db.get(db.key(digest, "digest_count"))
    per_industry = db.get(db.key(digest, "max_per_industry"))
    if not per_industry:
        return _candidates(digest, since)[:limit]
    picked, seen = [], {}
    for it in _candidates(digest, since):
        industry = str(it["extra"].get("industry") or "").strip().lower()
        if industry and seen.get(industry, 0) >= per_industry:
            continue
        seen[industry] = seen.get(industry, 0) + 1
        picked.append(it)
        if len(picked) >= limit:
            break
    return picked


def urgent_items() -> list[dict]:
    """Срочное — только в ленте новостей: боли не горят."""
    if not db.get("urgent_on") or is_quiet():
        return []
    since = db.now() - URGENT_WINDOW_HOURS * 3600
    return [s for s in _candidates("news", since) if s["strong_recent"] >= db.get("urgent_sources")]


def mark_sent(cluster_id: int, kind: str) -> None:
    db.conn.execute("UPDATE clusters SET sent_at=?, sent_kind=? WHERE id=?", (db.now(), kind, cluster_id))
    db.commit()


def is_quiet() -> bool:
    hour = datetime.now(config.TZ).hour
    return hour >= config.QUIET_FROM or hour < config.QUIET_TO


def stale_sources() -> list:
    """Источники, которые не отвечают больше суток и о которых ещё не предупреждали."""
    return db.conn.execute("SELECT * FROM sources WHERE enabled=1 AND warned=0 AND fail_since IS NOT NULL "
                           "AND fail_since < ?", (db.now() - 86400,)).fetchall()

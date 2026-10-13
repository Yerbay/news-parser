"""Telegram-бот: команды, утренняя сводка, срочные уведомления, часовой обход источников."""
import asyncio
import html
import logging
import re
from datetime import datetime, time as dtime

from telegram import (BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, LinkPreviewOptions,
                      Update)
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, Defaults, MessageHandler,
                          filters)

import config
import db
import fetchers
import pipeline
import seed

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

OWNER = filters.User(user_id=config.OWNER_ID)
KIND_NAMES = {"rss": "RSS", "html": "HTML", "hf_papers": "JSON API", "hn": "HN API"}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]

HELP = """Привет! Я собираю статьи с ваших источников, отбираю значимые и каждое утро присылаю сводку.

/add &lt;ссылка&gt; — добавить сайт (можно просто прислать ссылку)
/remove — удалить сайт
/list — источники и их статус
/now — сводка прямо сейчас
/settings — время, количество, срочные уведомления
/topics — интересные и исключённые темы

Под каждой новостью есть кнопки «Полезно» / «Не интересно» — отмечайте, это учтётся в ранжировании."""


def esc(text) -> str:
    return html.escape(str(text or ""), quote=True)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def today_text() -> str:
    d = datetime.now(config.TZ)
    return f"{d.day} {MONTHS[d.month - 1]}"


# ---------------------------------------------------------------- отправка новостей

def format_item(n: int | None, it: dict) -> str:
    parts = [f"<b>{f'{n}. ' if n else ''}{esc(it['title'])}</b>"]
    if it["summary_ru"]:
        parts.append(esc(it["summary_ru"]))
    if db.get("bilingual") and it["summary_orig"] and (it["lang"] or "").lower() != "ru":
        parts.append(f"<i>{esc(it['summary_orig'])}</i>")
    src = f'<a href="{esc(it["url"])}">{esc(it["source"])}</a>'
    if it["other_sources"]:
        k = it["other_sources"]
        src += f" + ещё {k} {plural(k, 'источник', 'источника', 'источников')}"
    parts.append(f"{src} · {it['score']:.1f}")
    return "\n\n".join(parts)[:4000]


def feedback_kb(cluster_id: int) -> Kb:
    return Kb([[Btn("👍 Полезно", callback_data=f"fb:{cluster_id}:1"),
                Btn("👎 Не интересно", callback_data=f"fb:{cluster_id}:-1")]])


async def send(bot, text: str, **kw):
    for _ in range(3):
        try:
            return await bot.send_message(config.OWNER_ID, text, **kw)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)


async def send_items(bot, items: list[dict], header: str, kind: str, numbered: bool = True) -> None:
    await send(bot, header)
    for n, it in enumerate(items, 1):
        await send(bot, format_item(n if numbered else None, it), reply_markup=feedback_kb(it["id"]),
                   disable_notification=kind == "digest")
        pipeline.mark_sent(it["id"], kind)
        await asyncio.sleep(0.35)


async def refresh() -> None:
    """Обход источников (если пора) и обработка новых статей."""
    async with pipeline.lock:
        await pipeline.collect()
        await pipeline.process()


async def run_digest(bot, manual: bool = False) -> None:
    await refresh()
    items = pipeline.digest_items()
    db.put("last_digest_at", db.now())
    if not items:
        await send(bot, "За сутки значимых новостей не нашлось." if not manual else
                   "Новых значимых новостей пока нет — всё, что было, уже показано.")
        return
    count = len(items)
    title = f"☀️ Сводка на {today_text()}" if not manual else "📰 Сводка сейчас"
    await send_items(bot, items, f"<b>{title}</b>: {count} {plural(count, 'новость', 'новости', 'новостей')}",
                     "digest")


# ---------------------------------------------------------------- фоновые задачи

async def job_hourly(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await refresh()
        for s in pipeline.stale_sources():
            await send(ctx.bot, f"⚠️ Источник <b>{esc(s['name'])}</b> не отвечает больше суток.\n"
                                f"Ошибка: {esc(s['last_error'])}\n/remove — удалить, если он больше не нужен.")
            db.set_source(s["id"], warned=1)
        items = pipeline.urgent_items()
        if items:
            await send_items(ctx.bot, items, "⚡️ <b>Срочно</b>", "urgent", numbered=False)
    except Exception:
        log.exception("Часовой обход")


async def job_digest(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await run_digest(ctx.bot)
    except Exception:
        log.exception("Утренняя сводка")
        await send(ctx.bot, "Не получилось собрать сводку — попробуйте /now.")


async def job_cleanup(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    async with pipeline.lock:
        db.cleanup()


def schedule_digest(app: Application) -> None:
    for job in app.job_queue.get_jobs_by_name("digest"):
        job.schedule_removal()
    h, m = map(int, db.get("digest_time").split(":"))
    app.job_queue.run_daily(job_digest, dtime(h, m, tzinfo=config.TZ), name="digest")


# ---------------------------------------------------------------- команды

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        ctx.user_data["await"] = "add"
        await update.message.reply_text("Пришлите ссылку на сайт или его ленту.")
        return
    await add_flow(update, ctx.args[0])


def weight_kb(source_id: int, current: int) -> Kb:
    return Kb([[Btn(f"{'✓ ' if w == current else ''}Вес {w}", callback_data=f"w:{source_id}:{w}") for w in (1, 2, 3)]])


async def add_flow(update: Update, url: str) -> None:
    status = await update.message.reply_text("Проверяю сайт…")
    try:
        info = await fetchers.discover(url)
    except Exception as e:
        await status.edit_text(f"Не удалось прочитать сайт: {esc(e)}")
        return
    if not info["items"]:
        await status.edit_text("Сайт открылся, но статей на нём я не нашёл.")
        return
    existing = db.source_by_url(info["url"])
    if existing and existing["enabled"]:
        await status.edit_text(f"Этот источник уже есть: <b>{esc(existing['name'])}</b>.")
        return
    weight = existing["weight"] if existing else 2
    sid = db.add_source(info["name"][:80], info["url"], info["kind"], weight, "Добавлен")
    pipeline.save_items(db.source(sid), info["items"], first_run=not db.has_articles(sid))
    db.source_ok(sid)
    latest = sorted(info["items"], key=lambda i: i.published or 0, reverse=True)[:3]
    lines = []
    for it in latest:
        when = f" ({datetime.fromtimestamp(it.published, config.TZ):%d.%m.%Y})" if it.published else ""
        lines.append(f"• {esc(it.title[:150])}{when}")
    await status.edit_text(
        f"✅ Добавлен: <b>{esc(info['name'])}</b> ({KIND_NAMES[info['kind']]})\n{esc(info['url'])}\n\n"
        f"Последние статьи:\n" + "\n".join(lines) +
        "\n\nВес источника: 3 — первоисточник (блог компании), 2 — аналитика, 1 — агрегатор/пересказ.",
        reply_markup=weight_kb(sid, weight))


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.sources()
    if not rows:
        await update.message.reply_text("Источников нет. Добавьте: /add &lt;ссылка&gt;")
        return
    kb = [[Btn(f"✖ {s['name']}", callback_data=f"rm:{s['id']}")] for s in rows]
    await update.message.reply_text("Какой источник удалить?", reply_markup=Kb(kb))


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.sources()
    if not rows:
        await update.message.reply_text("Источников нет. Добавьте: /add &lt;ссылка&gt;")
        return
    lines = []
    for s in rows:
        if s["last_error"]:
            mark = "⚠️"
        elif s["last_ok"]:
            mark = "✅"
        else:
            mark = "⏳"
        line = f"{mark} <b>{esc(s['name'])}</b> — вес {s['weight']}, {KIND_NAMES.get(s['kind'], s['kind'])}"
        if s["last_error"]:
            line += f"\n      ошибка: {esc(s['last_error'][:120])}"
        lines.append(line)
    text = f"Источников: {len(rows)}\n\n" + "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def cmd_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Собираю сводку…")
    try:
        await run_digest(ctx.bot, manual=True)
    except Exception as e:
        log.exception("/now")
        await update.message.reply_text(f"Ошибка: {esc(e)}")


def settings_view() -> tuple[str, Kb]:
    on = db.get("urgent_on")
    urgent = f" (от {db.get('urgent_sources')} сильных источников за 3 часа)" if on else ""
    text = (f"<b>Настройки</b>\n\n"
            f"Время сводки: <b>{db.get('digest_time')}</b> (Алматы)\n"
            f"Новостей в сводке: <b>{db.get('digest_count')}</b>\n"
            f"Минимальная оценка LLM для сводки: <b>{db.get('min_score')}</b> из 10\n"
            f"Срочные уведомления: <b>{'вкл' if on else 'выкл'}</b>{urgent}\n"
            f"Саммари ещё и на языке оригинала: <b>{'да' if db.get('bilingual') else 'нет'}</b>\n\n"
            f"С 23:00 до 8:00 срочное не присылается — оно попадёт в утреннюю сводку.")
    kb = Kb([
        [Btn("🕗 Время", callback_data="set:digest_time"), Btn("🔢 Количество", callback_data="set:digest_count")],
        [Btn("⭐ Мин. оценка", callback_data="set:min_score"),
         Btn("⚡ Порог срочных", callback_data="set:urgent_sources")],
        [Btn(f"Срочные: {'выключить' if on else 'включить'}", callback_data="tg:urgent_on")],
        [Btn(f"Язык оригинала: {'выключить' if db.get('bilingual') else 'включить'}", callback_data="tg:bilingual")],
    ])
    return text, kb


SETTING_PROMPTS = {
    "digest_time": "Во сколько присылать сводку? Формат ЧЧ:ММ, например 08:30.",
    "digest_count": "Сколько новостей в сводке? Число от 1 до 50.",
    "min_score": "Минимальная оценка LLM (1–10), ниже которой новость в сводку не попадает. Сейчас рекомендую 6.",
    "urgent_sources": "Сколько сильных источников (вес 2–3) должны написать о событии за 3 часа, "
                      "чтобы оно пришло срочно? Число от 1 до 10.",
    "topics_interest": "Пришлите новый список интересных тем (через запятую).",
    "topics_exclude": "Пришлите новый список исключённых тем (через запятую). «-» — без исключений.",
}


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = settings_view()
    await update.message.reply_text(text, reply_markup=kb)


def topics_view() -> tuple[str, Kb]:
    text = (f"<b>Интересные темы</b>\n{esc(db.get('topics_interest'))}\n\n"
            f"<b>Исключённые темы</b>\n{esc(db.get('topics_exclude') or '—')}\n\n"
            "По ним LLM оценивает новости; исключённые в сводку не попадают.")
    kb = Kb([[Btn("✏️ Интересные", callback_data="set:topics_interest"),
              Btn("✏️ Исключённые", callback_data="set:topics_exclude")]])
    return text, kb


async def cmd_topics(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = topics_view()
    await update.message.reply_text(text, reply_markup=kb)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    key = ctx.user_data.pop("await", None)
    if key == "add":
        await add_flow(update, text)
        return
    if key in ("digest_time",):
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", text)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            ctx.user_data["await"] = key
            await update.message.reply_text("Не понял время. Формат ЧЧ:ММ, например 08:30.")
            return
        db.put(key, f"{int(m.group(1)):02d}:{m.group(2)}")
        schedule_digest(ctx.application)
    elif key in ("digest_count", "min_score", "urgent_sources"):
        limits = {"digest_count": (1, 50), "min_score": (1, 10), "urgent_sources": (1, 10)}[key]
        if not text.isdigit() or not limits[0] <= int(text) <= limits[1]:
            ctx.user_data["await"] = key
            await update.message.reply_text(f"Нужно число от {limits[0]} до {limits[1]}.")
            return
        db.put(key, int(text))
    elif key in ("topics_interest", "topics_exclude"):
        db.put(key, "" if text == "-" else text[:1000])
        t, kb = topics_view()
        await update.message.reply_text("Сохранил. Новые темы учитываются для новых статей.\n\n" + t, reply_markup=kb)
        return
    elif re.match(r"(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", text):
        await add_flow(update, text)
        return
    else:
        await update.message.reply_text(HELP)
        return
    t, kb = settings_view()
    await update.message.reply_text("Сохранил.\n\n" + t, reply_markup=kb)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q.from_user.id != config.OWNER_ID:
        await q.answer()
        return
    kind, _, arg = q.data.partition(":")
    if kind == "fb":
        cid, value = map(int, arg.split(":"))
        db.conn.execute("INSERT INTO feedback(cluster_id, value, at) VALUES(?,?,?)", (cid, value, db.now()))
        db.commit()
        label = "✓ Полезно" if value > 0 else "✓ Не интересно"
        await q.answer("Учту")
        await q.edit_message_reply_markup(Kb([[Btn(label, callback_data="noop:")]]))
    elif kind == "w":
        sid, w = map(int, arg.split(":"))
        db.set_source(sid, weight=w)
        await q.answer(f"Вес {w}")
        await q.edit_message_reply_markup(weight_kb(sid, w))
    elif kind == "rm":
        s = db.source(int(arg))
        db.set_source(int(arg), enabled=0)
        await q.answer("Удалён")
        await q.edit_message_text(f"Удалён источник <b>{esc(s['name'] if s else arg)}</b>.")
    elif kind == "set":
        ctx.user_data["await"] = arg
        await q.answer()
        await q.message.reply_text(SETTING_PROMPTS[arg])
    elif kind == "tg":
        db.put(arg, not db.get(arg))
        await q.answer("Сохранил")
        text, kb = settings_view()
        await q.edit_message_text(text, reply_markup=kb)
    else:
        await q.answer()


# ---------------------------------------------------------------- запуск

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("now", "Сводка прямо сейчас"),
        BotCommand("add", "Добавить сайт"),
        BotCommand("list", "Источники и статус"),
        BotCommand("remove", "Удалить сайт"),
        BotCommand("settings", "Время, количество, срочные"),
        BotCommand("topics", "Интересные и исключённые темы"),
        BotCommand("start", "Инструкция"),
    ])


def main() -> None:
    missing = config.check()
    if missing:
        raise SystemExit(f"Заполните в .env: {', '.join(missing)}")
    seed.ensure()
    app = (Application.builder().token(config.BOT_TOKEN)
           .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True),
                              tzinfo=config.TZ))
           .post_init(post_init).build())
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("add", cmd_add), ("remove", cmd_remove),
                     ("list", cmd_list), ("now", cmd_now), ("settings", cmd_settings), ("topics", cmd_topics)]:
        app.add_handler(CommandHandler(name, fn, filters=OWNER))
    app.add_handler(MessageHandler(OWNER & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.job_queue.run_repeating(job_hourly, interval=config.COLLECT_MINUTES * 60, first=20, name="hourly")
    app.job_queue.run_daily(job_cleanup, dtime(4, 0, tzinfo=config.TZ), name="cleanup")
    schedule_digest(app)
    log.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

"""Стартовые источники обеих лент — при запуске добавляются те, которых ещё нет в базе."""
import db

# (название, категория, вес, адрес, тип) — лента «Новости AI»
SOURCES = [
    ("OpenAI News", "Первоисточник", 3, "https://openai.com/news/rss.xml", "rss"),
    ("Google DeepMind", "Первоисточник", 3, "https://deepmind.google/blog/rss.xml", "rss"),
    ("Hugging Face Blog", "Первоисточник", 3, "https://huggingface.co/blog/feed.xml", "rss"),
    ("Anthropic News", "Первоисточник", 3, "https://www.anthropic.com/news", "html"),
    ("Mistral AI News", "Первоисточник", 3, "https://mistral.ai/news/rss", "rss"),  # у сайта нашлась RSS
    ("Meta AI Blog", "Первоисточник", 3, "https://ai.meta.com/blog/", "html"),
    ("Claude Code (релизы)", "Инструменты", 3, "https://github.com/anthropics/claude-code/releases.atom", "rss"),
    ("OpenAI Codex (релизы)", "Инструменты", 2, "https://github.com/openai/codex/releases.atom", "rss"),
    ("Ollama (релизы)", "Инструменты", 2, "https://github.com/ollama/ollama/releases.atom", "rss"),
    ("vLLM (релизы)", "Инструменты", 2, "https://github.com/vllm-project/vllm/releases.atom", "rss"),
    ("LangChain (релизы)", "Инструменты", 1, "https://github.com/langchain-ai/langchain/releases.atom", "rss"),
    ("Simon Willison", "Аналитика", 2, "https://simonwillison.net/atom/everything/", "rss"),
    ("Latent Space", "Аналитика", 2, "https://www.latent.space/feed", "rss"),
    ("Import AI", "Аналитика", 2, "https://jack-clark.net/feed/", "rss"),
    ("Interconnects", "Аналитика", 2, "https://www.interconnects.ai/feed", "rss"),
    ("Ahead of AI", "Аналитика", 2, "https://magazine.sebastianraschka.com/feed", "rss"),
    # hnrss.org часто отвечает 502/таймаутом — тот же запрос через официальный API Algolia, с очками
    ("Hacker News (LLM, 150+ очков)", "Агрегатор", 1,
     "https://hn.algolia.com/api/v1/search_by_date?query=LLM&tags=story&numericFilters=points%3E%3D150&hitsPerPage=50",
     "hn"),
    ("Reddit r/LocalLLaMA (топ дня)", "Агрегатор", 1, "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=day", "rss"),
    ("Хабр: Искусственный интеллект", "Агрегатор", 1, "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/", "rss"),
    ("Hugging Face Daily Papers", "Исследования", 2, "https://huggingface.co/api/daily_papers", "hf_papers"),
]


HN_API = "https://hn.algolia.com/api/v1/search_by_date?tags={tag}&numericFilters=points%3E%3D{points}&hitsPerPage=50"

R = "https://www.reddit.com"

# (название, категория, вес, адрес, тип, путь статей для HTML) — лента «Боли»: где люди сами описывают проблемы.
# Профессиональные сообщества — главный источник; кейсы (боль, которую уже закрыли продуктом) — с малым весом.
PAIN_SOURCES = [
    ("Reddit: офисные профессии", "Профессии", 3,
     R + "/r/accounting+Bookkeeping+LawFirm+humanresources+Insurance+realtors+msp+PropertyManagement"
         "/top/.rss?t=day&limit=50", "rss", None),
    ("Reddit: бизнес «в поле»", "Профессии", 3,
     R + "/r/restaurateur+ecommerce+Construction+HVAC+Dentistry+logistics+freelance+photography"
         "/top/.rss?t=day&limit=50", "rss", None),
    ("Reddit: Entrepreneur, smallbusiness", "Предприниматели", 2,
     R + "/r/Entrepreneur+smallbusiness/top/.rss?t=day&limit=50", "rss", None),
    ("Ask HN (30+ очков)", "Предприниматели", 2, HN_API.format(tag="ask_hn", points=30), "hn", None),
    ("Reddit: «is there an app / I wish there was»", "Запросы", 1,
     R + "/search.rss?q=%22is+there+a+tool%22+OR+%22is+there+an+app%22+OR+%22I+wish+there+was%22"
         "+OR+%22how+do+you+deal+with%22&sort=top&t=day&limit=50", "rss", None),
    ("Reddit: SomebodyMakeThis, AppIdeas, Lightbulb", "Запросы", 1,
     R + "/r/SomebodyMakeThis+AppIdeas+Lightbulb/top/.rss?t=day&limit=50", "rss", None),
    ("vc.ru", "Русскоязычные", 1, "https://vc.ru/rss/all", "rss", None),
    ("Starter Story", "Кейсы", 1, "https://www.starterstory.com/", "html", "/stories/"),
    ("Reddit: SideProject, SaaS, microsaas, indiehackers", "Кейсы", 1,
     R + "/r/SideProject+SaaS+microsaas+indiehackers/top/.rss?t=day&limit=50", "rss", None),
]


def ensure() -> None:
    """Добавляет стартовые источники, которых ещё нет в базе. Удалённые пользователем (enabled=0) не возвращает."""
    for name, category, weight, url, kind in SOURCES:
        if not db.source_by_url(url):
            db.add_source(name, url, kind, weight, category)
    # Бывшая лента «Бизнес-идеи» (до 2026-10-23): нужные источники переходят в «Боли», остальные выключаются
    pains = {url: (name, category, weight) for name, category, weight, url, _, _ in PAIN_SOURCES}
    for s in db.conn.execute("SELECT * FROM sources WHERE digest='ideas'").fetchall():
        if s["url"] in pains:
            name, category, weight = pains[s["url"]]
            db.set_source(s["id"], digest="pains", name=name, category=category, weight=weight)
        else:
            db.set_source(s["id"], digest="pains", enabled=0)
    for name, category, weight, url, kind, prefix in PAIN_SOURCES:
        if not db.source_by_url(url):
            db.add_source(name, url, kind, weight, category, digest="pains", link_prefix=prefix)

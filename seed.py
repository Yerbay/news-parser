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

# (название, категория, вес, адрес, тип, путь статей для HTML) — лента «Бизнес-идеи»
# Не подошли: There's An AI For That и Indie Hackers (403 для ботов), YC Launches и Astana Hub (лента рисуется JS),
# Futurepedia (каталог без новостей).
IDEA_SOURCES = [
    ("Product Hunt", "Витрина", 2, "https://www.producthunt.com/feed", "rss", None),
    ("Show HN (50+ очков)", "Витрина", 2, HN_API.format(tag="show_hn", points=50), "hn", None),
    ("Starter Story", "Кейсы", 3, "https://www.starterstory.com/", "html", "/stories/"),
    ("Reddit: SideProject, SaaS, microsaas, indiehackers", "Кейсы", 2,
     "https://www.reddit.com/r/SideProject+SaaS+microsaas+indiehackers/top/.rss?t=day&limit=50", "rss", None),
    ("vc.ru", "Кейсы", 1, "https://vc.ru/rss/all", "rss", None),
    ("Ideabrowser (идея дня)", "Идеи", 3, "https://www.ideabrowser.com/idea-of-the-day", "html", "/idea/"),
    ("Exploding Topics", "Идеи", 1, "https://explodingtopics.com/blog", "html", None),
    ("Ben's Bites", "Идеи", 2, "https://www.bensbites.com/feed", "rss", None),
    ("Lenny's Newsletter", "Идеи", 2, "https://www.lennysnewsletter.com/feed", "rss", None),
    ("Reddit: Entrepreneur, smallbusiness", "Спрос", 2,
     "https://www.reddit.com/r/Entrepreneur+smallbusiness/top/.rss?t=day&limit=50", "rss", None),
    ("Ask HN (30+ очков)", "Спрос", 2, HN_API.format(tag="ask_hn", points=30), "hn", None),
    ("digitalbusiness.kz", "Казахстан", 1, "https://digitalbusiness.kz/feed/", "rss", None),
    ("profit.kz", "Казахстан", 1, "https://profit.kz/rss/", "rss", None),
]


def ensure() -> None:
    """Добавляет стартовые источники, которых ещё нет в базе. Удалённые пользователем (enabled=0) не возвращает."""
    for name, category, weight, url, kind in SOURCES:
        if not db.source_by_url(url):
            db.add_source(name, url, kind, weight, category)
    for name, category, weight, url, kind, prefix in IDEA_SOURCES:
        if not db.source_by_url(url):
            db.add_source(name, url, kind, weight, category, digest="ideas", link_prefix=prefix)

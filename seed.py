"""Стартовый набор источников — добавляется при первом запуске, если база пуста."""
import db

# (название, категория, вес, адрес, тип)
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


def ensure() -> None:
    if db.sources(enabled_only=False):
        return
    for name, category, weight, url, kind in SOURCES:
        db.add_source(name, url, kind, weight, category)

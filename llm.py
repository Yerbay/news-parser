"""DeepSeek: фильтр тем, склейка в события, оценка значимости и саммари — одним запросом на пачку статей."""
import json
import logging

from openai import AsyncOpenAI

import config

log = logging.getLogger(__name__)
_client: AsyncOpenAI | None = None

SYSTEM = """Ты — редактор персональной ежедневной сводки новостей.
Интересы читателя: {interests}.
Исключённые темы: {excludes}.

Тебе дают недавние события (уже известные) и пачку новых статей. Для КАЖДОЙ статьи верни:
- "id": id статьи.
- "excluded": true, если статья в основном про исключённые темы или вообще не связана с интересами читателя.
- "score": значимость для этого читателя от 1 до 10.
  9–10: крупный релиз или прорыв ведущей лаборатории (новая флагманская модель, большой продукт), то, что обсуждает вся индустрия.
  7–8: заметный релиз модели или инструмента, важное обновление, сильное исследование, глубокий разбор с новыми фактами.
  5–6: полезно, но рядовое: обычное обновление, интересная статья без больших новостей.
  1–4: мелочь: патч-релиз с багфиксами, маркетинг, кейсы клиентов, найм, мнение без новой информации.
  Научные статьи обычно 4–6; 7+ — только если результат заметно меняет практику или это работа ведущей лаборатории.
  Личные эксперименты и вопросы пользователей форумов обычно 3–5.
- "cluster": если статья про то же событие, что одно из недавних событий, — его id (например "C12").
  Иначе новая метка вида "n1"; статьи этой пачки про одно и то же новое событие получают одинаковую метку.
  Разные релизы одного проекта или разные статьи на одну тему — разные события. Одно событие — это одна новость.
- "event": короткое нейтральное название события на английском (до 12 слов).
- "lang": язык статьи (код: "en", "ru", ...).
- "summary_ru": 2–3 предложения на русском: что произошло и почему это важно. Конкретно, без воды и оценочных слов.
- "summary_orig": то же саммари на языке оригинала статьи (2–3 предложения); пустая строка, если статья на русском.
Если "excluded": true, саммари можно оставить пустыми.

Ответ — только JSON: {{"items": [ ... ]}}"""


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)
    return _client


async def classify(articles: list[dict], events: list[dict], interests: str, excludes: str) -> list[dict]:
    """articles: [{id, source, title, text}], events: [{id, event}]. Возвращает список оценок по статьям."""
    events_text = "\n".join(f"C{e['id']}: {e['event']}" for e in events) or "(нет)"
    articles_text = "\n\n".join(
        f"id={a['id']} | источник: {a['source']}\nЗаголовок: {a['title']}\nТекст: {a['text'] or '(только заголовок)'}"
        for a in articles)
    user = f"НЕДАВНИЕ СОБЫТИЯ:\n{events_text}\n\nНОВЫЕ СТАТЬИ:\n{articles_text}"
    resp = await _get_client().chat.completions.create(
        model=config.LLM_MODEL,
        messages=[{"role": "system", "content": SYSTEM.format(interests=interests, excludes=excludes)},
                  {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        max_tokens=8000,
        temperature=0.2,
    )
    content = resp.choices[0].message.content or "{}"
    try:
        return json.loads(content).get("items", [])
    except ValueError:
        log.warning("LLM вернула не JSON: %s", content[:300])
        return []

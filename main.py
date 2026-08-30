import os
import logging
import asyncio
import requests
import feedparser
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPOSITORY = os.getenv('GITHUB_REPOSITORY')
GIGACHAT_CREDENTIALS = os.getenv('GIGACHAT_CREDENTIALS') # Авторизация в GigaChat

# RSS-источники
RSS_FEEDS = [
    {'name': 'Mongabay', 'url': 'https://news.mongabay.com/feed/', 'emoji': '🌿', 'title': 'Mongabay: Охрана природы'},
    {'name': 'NASA Science', 'url': 'https://science.nasa.gov/feed/?science_org=22414%2C19791', 'emoji': '🚀', 'title': 'NASA Science: Новости космоса и Земли'},
    {'name': 'The Guardian Environment', 'url': 'https://www.theguardian.com/environment/rss', 'emoji': '🌍', 'title': 'The Guardian: Окружающая среда'}
]

STATE_VAR_SOURCE = "LAST_RSS_SOURCE_INDEX"
STATE_VAR_LINK = "LAST_SENT_NATURE_LINK"

# ==================== УПРАВЛЕНИЕ СОСТОЯНИЕМ (GitHub Variables) ====================
def get_github_variable(var_name):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY: return ""
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/variables/{var_name}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        return response.json()["value"] if response.status_code == 200 else ""
    except Exception as e:
        logger.warning(f"⚠️ Не удалось получить {var_name}: {e}")
        return ""

def set_github_variable(var_name, value):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY: return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/variables/{var_name}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    data = {"name": var_name, "value": value}
    try:
        response = requests.patch(url, headers=headers, json=data, timeout=10)
        if response.status_code == 404: # Создаем, если не существует
            requests.post(f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/variables", headers=headers, json=data, timeout=10)
    except Exception as e:
        logger.error(f"❌ Не удалось сохранить {var_name}: {e}")

# ==================== ОБРАБОТКА ТЕКСТА ЧЕРЕЗ GIGACHAT ====================
def process_with_gigachat(title, summary):
    """
    Переводит и сжимает текст через GigaChat.
    Возвращает кортеж (ru_title, ru_summary) или (None, None) при ошибке.
    """
    if not GIGACHAT_CREDENTIALS:
        logger.error("❌ Отсутствует GIGACHAT_CREDENTIALS")
        return None, None

    prompt = f"""Ты - профессиональный редактор новостного Telegram-канала.
Твоя задача: перевести заголовок и описание статьи с английского на русский язык, а также сжать описание.
Требования:
1. Сохрани главную мысль, ключевые факты и цифры. Ничего не придумывай от себя.
2. Итоговый текст (вместе с заголовком) должен занимать СТРОГО до 750 символов (с пробелами).
3. Стиль: информационный, без воды и вступлений.
4. Ответ верни СТРОГО в следующем формате, где первая строка — заголовок, а всё остальное — основной текст:
[ЗАГОЛОВОК]
[ОПИСАНИЕ]

Оригинальный заголовок: {title}
Оригинальное описание: {summary}
"""
    try:
        # verify_ssl_certs=False часто требуется в CI/CD из-за корневых сертификатов Сбера
        with GigaChat(credentials=GIGACHAT_CREDENTIALS, scope="GIGACHAT_API_PERS", verify_ssl_certs=False) as giga:
            response = giga.chat(
                Chat(
                    messages=[Messages(role=MessagesRole.USER, content=prompt)],
                    model="GigaChat:latest"
                )
            )
            result = response.choices[0].message.content.strip()
            
            # Парсим ответ нейросети
            parts = result.split('\n', 1)
            ru_title = parts[0].strip()
            ru_summary = parts[1].strip() if len(parts) > 1 else ru_title
            
            # Жесткая страховка от превышения 800 символов суммарно
            full_text = f"{ru_title}\n\n{ru_summary}"
            if len(full_text) > 780:
                max_summary_len = 780 - len(ru_title) - 4
                if max_summary_len > 0:
                    ru_summary = ru_summary[:max_summary_len].rsplit(' ', 1)[0] + "..."
                else:
                    ru_summary = ""
                    
            return ru_title, ru_summary
            
    except Exception as e:
        logger.error(f"❌ Ошибка GigaChat: {e}")
        return None, None

def clean_html(raw_html):
    if not raw_html: return ""
    soup = BeautifulSoup(raw_html, "lxml")
    return soup.get_text(separator=' ', strip=True)

def extract_image(entry):
    if 'media_content' in entry: return entry.media_content[0].get('url')
    if 'enclosures' in entry: return entry.enclosures[0].get('url')
    return None

# ==================== ОБРАБОТКА RSS ====================
def process_rss_feed(feed_info):
    """Обрабатывает одну ленту. Возвращает готовый контент или None."""
    last_link = get_github_variable(STATE_VAR_LINK)
    logger.info(f"📡 Проверка ленты {feed_info['name']}")
    
    try:
        feed = feedparser.parse(feed_info['url'])
        if not feed.entries: return None

        for entry in feed.entries:
            link = entry.get('link', '')
            if link == last_link or not link: 
                continue # Пропускаем уже отправленные
            
            title = entry.get('title', 'Без заголовка')
            summary = clean_html(entry.get('summary', entry.get('description', '')))
            
            # Обработка через Gigachat (Перевод + Суммаризация)
            ru_title, ru_summary = process_with_gigachat(title, summary)
            
            if not ru_title or not ru_summary:
                logger.warning(f"⚠️ GigaChat не смог обработать новость '{title}'. Пробуем следующую в ленте.")
                continue # Переходим к следующей новости в этой же ленте

            # Формирование финального поста
            caption_parts = [
                f"{feed_info['emoji']} <b>{feed_info['title']}</b>",
                "",
                f"<b>{ru_title}</b>",
                "",
                ru_summary,
                "",
                f"🔗 <a href='{link}'>Читать оригинал</a>"
            ]
            caption = "\n".join(caption_parts)
            
            image_url = extract_image(entry)
            return {
                'type': 'photo' if image_url else 'text',
                'url': image_url,
                'caption': caption,
                'link': link
            }
            
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга {feed_info['name']}: {e}")
        
    return None

def send_to_telegram(content):
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        if content['type'] == 'photo' and content['url']:
            asyncio.run(bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=content['url'], caption=content['caption'], parse_mode='HTML'))
        else:
            asyncio.run(bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=content['caption'], parse_mode='HTML'))
        return True
    except TelegramError as e:
        logger.error(f"❌ Ошибка Telegram: {e}")
        return False

# ==================== ГЛАВНАЯ ЛОГИКА ====================
def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GIGACHAT_CREDENTIALS]):
        logger.error("❌ Не все переменные окружения установлены")
        return False

    # 1. Читаем последний успешный индекс
    last_index_str = get_github_variable(STATE_VAR_SOURCE)
    try:
        last_index = int(last_index_str)
    except (ValueError, TypeError):
        last_index = -1 # При первом запуске начнем с индекса 0

    content = None
    chosen_index = -1

    # 2. Жесткая ротация с перебором (Fallback)
    # Цикл пройдет по всем источникам, начиная со следующего после последнего использованного
    for i in range(len(RSS_FEEDS)):
        next_index = (last_index + 1 + i) % len(RSS_FEEDS)
        feed_info = RSS_FEEDS[next_index]
        logger.info(f"🎯 Пробуем источник: {feed_info['name']} ({next_index + 1}/{len(RSS_FEEDS)})")
        
        content = process_rss_feed(feed_info)
        if content:
            chosen_index = next_index
            break
        else:
            logger.warning(f"⚠️ Источник {feed_info['name']} не дал пригодных новостей. Переход к следующему...")

    # 3. Финальная проверка
    if not content:
        logger.error("❌ Не удалось получить и обработать новости ни из одного источника.")
        return False

    # 4. Отправка и сохранение состояния
    if send_to_telegram(content):
        logger.info("✅ Пост успешно опубликован. Сохраняем состояние...")
        set_github_variable(STATE_VAR_SOURCE, str(chosen_index))
        set_github_variable(STATE_VAR_LINK, content['link'])
        return True
        
    return False

if __name__ == "__main__":
    main()

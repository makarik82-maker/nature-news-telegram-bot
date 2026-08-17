import os
import requests
import logging
import asyncio
import feedparser
from telegram import Bot
from telegram.error import TelegramError
from deep_translator import GoogleTranslator

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPOSITORY = os.getenv('GITHUB_REPOSITORY')

# RSS-ленты (можно добавлять свои)
RSS_FEEDS = [
    "https://www.sciencedaily.com/rss/earth_environment.xml",  # ScienceDaily: Земля и окружающая среда
    "https://earthobservatory.nasa.gov/feeds/earthobservatory.rdf", # NASA Earth Observatory
    "https://www.nationalgeographic.com/environment/rss" # NatGeo Environment (если доступен)
]

STATE_VAR_NAME = "LAST_SENT_RSS_LINK"
translator = GoogleTranslator(source='auto', target='ru')

# ==================== УПРАВЛЕНИЕ СОСТОЯНИЕМ ====================
def get_last_sent_link():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return ""
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/variables/{STATE_VAR_NAME}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()["value"]
        return ""
    except Exception as e:
        logger.warning(f"⚠️ Не удалось получить последнюю ссылку: {e}")
        return ""

def set_last_sent_link(link):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/variables/{STATE_VAR_NAME}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    data = {"name": STATE_VAR_NAME, "value": link}
    try:
        response = requests.patch(url, headers=headers, json=data, timeout=10)
        if response.status_code == 404:
            create_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/variables"
            requests.post(create_url, headers=headers, json=data, timeout=10)
        logger.info(f"💾 Сохранена новая ссылка: {link}")
    except Exception as e:
        logger.error(f"❌ Не удалось сохранить ссылку: {e}")

# ==================== ОСНОВНАЯ ЛОГИКА ====================
def extract_image(entry):
    """Пытается найти изображение в записи RSS"""
    # Вариант 1: media:content
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0].get('url')
    # Вариант 2: enclosure
    if 'enclosures' in entry and len(entry.enclosures) > 0:
        return entry.enclosures[0].get('href')
    # Вариант 3: извлечение из summary (простой regex для img src)
    import re
    match = re.search(r'<img[^>]+src="([^">]+)"', entry.get('summary', ''))
    if match:
        return match.group(1)
    return None

def process_rss():
    last_link = get_last_sent_link()
    logger.info(f"🔍 Последняя отправленная ссылка: {last_link or 'Нет'}")

    for feed_url in RSS_FEEDS:
        logger.info(f"📡 Проверка ленты: {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
            if not feed.entries:
                continue

            for entry in feed.entries:
                link = entry.get('link', '')
                
                # Если мы уже отправляли эту ссылку, значит и все следующие в этой ленте тоже старые
                if link == last_link or not link:
                    logger.info("✅ Достигли уже отправленных новостей. Останавливаемся.")
                    return True

                # Обрабатываем новую новость
                title = entry.get('title', 'Без заголовка')
                summary = entry.get('summary', entry.get('description', 'Нет описания'))
                
                # Очищаем summary от HTML-тегов для перевода
                import re
                clean_summary = re.sub('<.*?>', '', summary)[:1000] # Ограничиваем длину для перевода

                logger.info(f"📰 Найдена новая новость: {title}")

                # Перевод
                title_ru = translator.translate(title) if len(title) > 5 else title
                summary_ru = translator.translate(clean_summary) if len(clean_summary) > 10 else clean_summary

                # Формируем текст
                caption = f"🌿 <b>Новости природы и науки</b>\n\n"
                caption += f"<b>{title_ru}</b>\n\n"
                caption += f"{summary_ru}\n\n"
                caption += f"🔗 <a href='{link}'>Читать оригинал</a>"

                # Обрезаем, если слишком длинно для Telegram (лимит 1024 для фото, 4096 для текста)
                if len(caption) > 1000:
                    caption = caption[:1000].rsplit(' ', 1)[0] + "...\n\n🔗 <a href='" + link + "'>Читать оригинал</a>"

                image_url = extract_image(entry)

                # Отправка в Telegram
                try:
                    bot = Bot(token=TELEGRAM_BOT_TOKEN)
                    if image_url:
                        asyncio.run(bot.send_photo(
                            chat_id=TELEGRAM_CHANNEL_ID,
                            photo=image_url,
                            caption=caption,
                            parse_mode='HTML'
                        ))
                    else:
                        asyncio.run(bot.send_message(
                            chat_id=TELEGRAM_CHANNEL_ID,
                            text=caption,
                            parse_mode='HTML'
                        ))
                    
                    logger.info("✅ Новость успешно отправлена в Telegram")
                    set_last_sent_link(link)
                    return True # Отправляем только одну новость за запуск, чтобы не спамить

                except TelegramError as e:
                    logger.error(f"❌ Ошибка Telegram: {e}")
                    return False

        except Exception as e:
            logger.error(f"❌ Ошибка при парсинге {feed_url}: {e}")
            continue

    logger.warning("⚠️ Новых новостей не найдено или все ленты недоступны.")
    return True


if __name__ == '__main__':
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID]):
        logger.error("❌ Не все переменные окружения установлены")
        exit(1)
    
    success = process_rss()
    exit(0 if success else 1)

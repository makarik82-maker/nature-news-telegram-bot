import os
import re
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

#  RSS-источники (в порядке ротации)
RSS_FEEDS = [
    {
        'name': 'WWF Stories',
        'url': 'https://www.worldwildlife.org/stories/rss',
        'emoji': '🐼',
        'title': 'WWF: Истории о дикой природе'
    },
    {
        'name': 'Mongabay',
        'url': 'https://news.mongabay.com/feed/',
        'emoji': '🌿',
        'title': 'Mongabay: Охрана природы'
    },
    {
        'name': 'ScienceDaily Environment',
        'url': 'https://www.sciencedaily.com/rss/earth_environment.xml',
        'emoji': '🔬',
        'title': 'ScienceDaily: Экология и Земля'
    },
    {
        'name': 'NASA Earth Observatory',
        'url': 'https://earthobservatory.nasa.gov/feeds/eo.rss',
        'emoji': '🌍',
        'title': 'NASA: Наблюдения за Землёй'
    },
    {
        'name': 'The Guardian Environment',
        'url': 'https://www.theguardian.com/environment/rss',
        'emoji': '',
        'title': 'The Guardian: Окружающая среда'
    }
]

STATE_VAR_SOURCE = "LAST_RSS_SOURCE_INDEX"
STATE_VAR_LINK = "LAST_SENT_NATURE_LINK"
translator = GoogleTranslator(source='auto', target='ru')


# ==================== УПРАВЛЕНИЕ СОСТОЯНИЕМ ====================
def get_github_variable(var_name):
    """Получить значение переменной из GitHub"""
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return ""
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/variables/{var_name}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()["value"]
        return ""
    except Exception as e:
        logger.warning(f"⚠️ Не удалось получить переменную {var_name}: {e}")
        return ""


def set_github_variable(var_name, value):
    """Сохранить значение переменной в GitHub"""
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/variables/{var_name}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    data = {"name": var_name, "value": value}
    try:
        response = requests.patch(url, headers=headers, json=data, timeout=10)
        if response.status_code == 404:
            create_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/variables"
            requests.post(create_url, headers=headers, json=data, timeout=10)
        logger.info(f"💾 Сохранено: {var_name} = {value}")
    except Exception as e:
        logger.error(f" Не удалось сохранить {var_name}: {e}")


def get_next_source_index():
    """Определить индекс следующего источника (ротация)"""
    last_index_str = get_github_variable(STATE_VAR_SOURCE)
    try:
        last_index = int(last_index_str)
        next_index = (last_index + 1) % len(RSS_FEEDS)
    except (ValueError, TypeError):
        next_index = 0
    return next_index


# ==================== ОБРАБОТКА RSS ====================
def extract_image(entry):
    """Пытается найти изображение в записи RSS"""
    # media:content
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0].get('url')
    # enclosure
    if 'enclosures' in entry and len(entry.enclosures) > 0:
        return entry.enclosures[0].get('href')
    # media:thumbnail
    if 'media_thumbnail' in entry and len(entry.media_thumbnail) > 0:
        return entry.media_thumbnail[0].get('url')
    # Извлечение из summary
    match = re.search(r'<img[^>]+src="([^">]+)"', entry.get('summary', ''))
    if match:
        return match.group(1)
    return None


def clean_html(text):
    """Очищает текст от HTML-тегов"""
    clean = re.sub('<.*?>', '', text)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


def process_rss_feed(feed_info):
    """Обработать одну RSS-ленту и вернуть первую новую новость"""
    last_link = get_github_variable(STATE_VAR_LINK)
    logger.info(f"🔍 Последняя отправленная ссылка: {last_link or 'Нет'}")
    
    feed_url = feed_info['url']
    feed_name = feed_info['name']
    logger.info(f"📡 Проверка ленты {feed_name}: {feed_url}")
    
    try:
        feed = feedparser.parse(feed_url)
        
        if not feed.entries:
            logger.warning(f"⚠️ Лента {feed_name} пуста или недоступна.")
            return None

        logger.info(f"📰 Найдено записей в ленте {feed_name}: {len(feed.entries)}")

        for entry in feed.entries:
            link = entry.get('link', '')
            
            # Если мы уже отправляли эту ссылку — останавливаемся
            if link == last_link or not link:
                logger.info(f"✅ Достигли уже отправленных новостей в {feed_name}.")
                return None

            # Обрабатываем новую новость
            title = entry.get('title', 'Без заголовка')
            summary = entry.get('summary', entry.get('description', 'Нет описания'))
            
            # Очищаем summary от HTML-тегов
            clean_summary = clean_html(summary)
            
            # Ограничиваем длину для перевода
            if len(clean_summary) > 4000:
                clean_summary = clean_summary[:4000]

            logger.info(f" Найдена новая новость: {title}")

            # Перевод на русский
            try:
                title_ru = translator.translate(title) if len(title) > 5 else title
            except Exception as e:
                logger.warning(f"⚠️ Ошибка перевода заголовка: {e}")
                title_ru = title

            try:
                summary_ru = translator.translate(clean_summary) if len(clean_summary) > 10 else clean_summary
            except Exception as e:
                logger.warning(f"⚠️ Ошибка перевода описания: {e}")
                summary_ru = clean_summary

            logger.info(f"✅ Перевод выполнен: {title_ru}")

            # Формируем текст поста
            caption_parts = [
                f"{feed_info['emoji']} <b>{feed_info['title']}</b>",
                "",
                f"<b>{title_ru}</b>",
                "",
                summary_ru,
                "",
                f"🔗 <a href='{link}'>Читать оригинал</a>"
            ]
            
            caption = "\n".join(caption_parts)

            # Защита от превышения лимита Telegram
            max_len = 1000
            if len(caption) > max_len:
                caption = caption[:max_len].rsplit(' ', 1)[0] + "..."
                caption += f"\n\n🔗 <a href='{link}'>Читать оригинал</a>"

            # Ищем изображение
            image_url = extract_image(entry)

            return {
                'type': 'photo' if image_url else 'text',
                'url': image_url,
                'caption': caption,
                'link': link
            }

    except Exception as e:
        logger.error(f"❌ Ошибка при парсинге ленты {feed_name}: {e}")
        return None

    return None


def send_to_telegram(content):
    """Отправить контент в Telegram"""
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        
        if content['type'] == 'photo' and content['url']:
            logger.info(f"🖼️ Найдено изображение: {content['url'][:50]}...")
            asyncio.run(bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=content['url'],
                caption=content['caption'],
                parse_mode='HTML'
            ))
        else:
            logger.info(" Изображение не найдено, отправляем текст")
            asyncio.run(bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=content['caption'],
                parse_mode='HTML'
            ))
        
        logger.info("✅ Новость успешно отправлена в Telegram")
        return True
        
    except TelegramError as e:
        logger.error(f"❌ Ошибка Telegram: {e}")
        return False


def main():
    logger.info("🚀 Запуск бота природы (ротация 5 источников)")
    
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID]):
        logger.error("❌ Не все переменные окружения установлены")
        return False
    
    # Определяем следующий источник
    source_index = get_next_source_index()
    feed_info = RSS_FEEDS[source_index]
    logger.info(f" Выбран источник: {feed_info['name']} ({source_index + 1}/{len(RSS_FEEDS)})")
    
    # Обрабатываем выбранный источник
    content = process_rss_feed(feed_info)
    
    if not content:
        logger.warning(f"⚠️ Не удалось получить новость из {feed_info['name']}. Пробуем следующий источник...")
        
        # Если не получилось, пробуем остальные источники по очереди
        for i in range(1, len(RSS_FEEDS)):
            next_index = (source_index + i) % len(RSS_FEEDS)
            next_feed = RSS_FEEDS[next_index]
            logger.info(f"🔄 Пробуем: {next_feed['name']}")
            
            content = process_rss_feed(next_feed)
            if content:
                source_index = next_index
                break
    
    if not content:
        logger.warning("⚠️ Не удалось получить новости ни из одного источника.")
        return False
    
    # Отправляем в Telegram
    success = send_to_telegram(content)
    
    if success:
        # Сохраняем информацию
        set_github_variable(STATE_VAR_SOURCE, str(source_index))
        set_github_variable(STATE_VAR_LINK, content['link'])
        return True
    
    return False


if __name__ == '__main__':
    success = main()
    if success:
        exit(0)
    else:
        logger.warning("⚠️ Завершаем работу. Завтра попробуем снова!")
        exit(0)

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

#  Единственный источник — WWF Stories
RSS_FEED_URL = "https://www.worldwildlife.org/stories/rss"

STATE_VAR_NAME = "LAST_SENT_WWF_LINK"
translator = GoogleTranslator(source='auto', target='ru')


# ==================== УПРАВЛЕНИЕ СОСТОЯНИЕМ ====================
def get_last_sent_link():
    """Получает ссылку последней отправленной новости из переменных GitHub"""
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
        logger.warning(f"️ Не удалось получить последнюю ссылку: {e}")
        return ""


def set_last_sent_link(link):
    """Сохраняет ссылку отправленной новости в переменные GitHub"""
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


# ==================== ОБРАБОТКА RSS ====================
def extract_image(entry):
    """Пытается найти изображение в записи RSS WWF"""
    # Вариант 1: media:content
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0].get('url')
    # Вариант 2: enclosure
    if 'enclosures' in entry and len(entry.enclosures) > 0:
        return entry.enclosures[0].get('href')
    # Вариант 3: извлечение из summary (тег <img>)
    match = re.search(r'<img[^>]+src="([^">]+)"', entry.get('summary', ''))
    if match:
        return match.group(1)
    # Вариант 4: WWF часто использует media:thumbnail
    if 'media_thumbnail' in entry and len(entry.media_thumbnail) > 0:
        return entry.media_thumbnail[0].get('url')
    return None


def clean_html(text):
    """Очищает текст от HTML-тегов"""
    clean = re.sub('<.*?>', '', text)
    # Убираем лишние пробелы и переносы строк
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


def process_wwf_rss():
    """Основная функция обработки RSS-ленты WWF Stories"""
    last_link = get_last_sent_link()
    logger.info(f"🔍 Последняя отправленная ссылка: {last_link or 'Нет'}")

    logger.info(f"📡 Проверка ленты WWF Stories: {RSS_FEED_URL}")
    
    try:
        feed = feedparser.parse(RSS_FEED_URL)
        
        if not feed.entries:
            logger.warning("⚠️ Лента WWF пуста или недоступна.")
            return False

        logger.info(f"📰 Найдено записей в ленте: {len(feed.entries)}")

        for entry in feed.entries:
            link = entry.get('link', '')
            
            # Если мы уже отправляли эту ссылку — останавливаемся
            if link == last_link or not link:
                logger.info("✅ Достигли уже отправленных новостей. Останавливаемся.")
                return True

            # Обрабатываем новую новость
            title = entry.get('title', 'Без заголовка')
            summary = entry.get('summary', entry.get('description', 'Нет описания'))
            
            # Очищаем summary от HTML-тегов
            clean_summary = clean_html(summary)
            
            # Ограничиваем длину для перевода (Google Translate имеет лимит ~5000 символов)
            if len(clean_summary) > 4000:
                clean_summary = clean_summary[:4000]

            logger.info(f"📰 Найдена новая новость: {title}")

            # Перевод на русский
            try:
                title_ru = translator.translate(title) if len(title) > 5 else title
            except Exception as e:
                logger.warning(f"⚠️ Ошибка перевода заголовка: {e}. Используем оригинал.")
                title_ru = title

            try:
                summary_ru = translator.translate(clean_summary) if len(clean_summary) > 10 else clean_summary
            except Exception as e:
                logger.warning(f"️ Ошибка перевода описания: {e}. Используем оригинал.")
                summary_ru = clean_summary

            logger.info(f"✅ Перевод выполнен: {title_ru}")

            # Формируем текст поста
            caption_parts = [
                "🐼 <b>WWF: Истории о дикой природе</b>",
                "",
                f"<b>{title_ru}</b>",
                "",
                summary_ru,
                "",
                f"🔗 <a href='{link}'>Читать оригинал на сайте WWF</a>"
            ]
            
            caption = "\n".join(caption_parts)

            # Защита от превышения лимита Telegram (1024 символа для фото, 4096 для текста)
            max_len = 1000  # С запасом для фото
            if len(caption) > max_len:
                # Обрезаем по последнему пробелу
                caption = caption[:max_len].rsplit(' ', 1)[0] + "..."
                caption += f"\n\n <a href='{link}'>Читать оригинал</a>"

            # Ищем изображение
            image_url = extract_image(entry)

            # Отправка в Telegram
            try:
                bot = Bot(token=TELEGRAM_BOT_TOKEN)
                
                if image_url:
                    logger.info(f"️ Найдено изображение: {image_url[:50]}...")
                    asyncio.run(bot.send_photo(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        photo=image_url,
                        caption=caption,
                        parse_mode='HTML'
                    ))
                else:
                    logger.info(" Изображение не найдено, отправляем текст")
                    asyncio.run(bot.send_message(
                        chat_id=TELEGRAM_CHANNEL_ID,
                        text=caption,
                        parse_mode='HTML'
                    ))
                
                logger.info("✅ Новость WWF успешно отправлена в Telegram")
                set_last_sent_link(link)
                return True  # Отправляем только одну новость за запуск

            except TelegramError as e:
                logger.error(f"❌ Ошибка Telegram: {e}")
                return False

    except Exception as e:
        logger.error(f"❌ Ошибка при парсинге ленты WWF: {e}")
        return False

    logger.warning("⚠️ Новых новостей не найдено.")
    return True


if __name__ == '__main__':
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID]):
        logger.error("❌ Не все переменные окружения установлены")
        exit(1)
    
    success = process_wwf_rss()
    exit(0 if success else 1)

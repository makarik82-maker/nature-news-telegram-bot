import os
import logging
import asyncio
import requests
import feedparser
import json
import subprocess
import re
import html
import traceback
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация окружения
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_REPOSITORY = os.getenv('GITHUB_REPOSITORY')
GIGACHAT_CREDENTIALS = os.getenv('GIGACHAT_CREDENTIALS')

STATE_FILE = 'state.json'
MAX_HISTORY_SIZE = 200 # Храним только последние 200 ссылок, чтобы файл не разрастался

# RSS-источники
RSS_FEEDS = [
    {'name': 'Mongabay', 'url': 'https://news.mongabay.com/feed/', 'emoji': '🌿', 'title': 'Mongabay: Охрана природы'},
    {'name': 'NASA Science', 'url': 'https://science.nasa.gov/feed/?science_org=22414%2C19791', 'emoji': '🚀', 'title': 'NASA Science: Новости космоса и Земли'},
    {'name': 'The Guardian Environment', 'url': 'https://www.theguardian.com/environment/rss', 'emoji': '🌍', 'title': 'The Guardian: Окружающая среда'}
]

# ==================== РАБОТА С ИСТОРИЕЙ (state.json) ====================
def load_history():
    """Загружает историю из файла. Если файла нет - создает пустую структуру."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Ошибка чтения {STATE_FILE}: {e}")
    return {"last_source_index": -1, "sent_links": []}

def save_history(data):
    """Сохраняет историю в файл, обрезая до MAX_HISTORY_SIZE."""
    if len(data['sent_links']) > MAX_HISTORY_SIZE:
        data['sent_links'] = data['sent_links'][-MAX_HISTORY_SIZE:]
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def commit_and_push():
    """Коммитит и пушит изменения state.json в репозиторий."""
    try:
        if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
            logger.warning("⚠️ Нет GITHUB_TOKEN для пуша в Git")
            return

        subprocess.run(['git', 'config', '--global', 'user.name', 'github-actions[bot]'], check=True)
        subprocess.run(['git', 'config', '--global', 'user.email', 'github-actions[bot]@users.noreply.github.com'], check=True)
        
        # Настраиваем URL с токеном для авторизации пуша
        remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPOSITORY}.git"
        subprocess.run(['git', 'remote', 'set-url', 'origin', remote_url], check=True, capture_output=True)
        
        subprocess.run(['git', 'add', STATE_FILE], check=True)
        
        # Проверяем, есть ли реальные изменения для коммита
        status = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True, check=True)
        if status.stdout.strip():
            subprocess.run(['git', 'commit', '-m', '🤖 Автоматическое обновление истории публикаций'], check=True)
            subprocess.run(['git', 'push', 'origin', 'HEAD'], check=True)
            logger.info("✅ История успешно сохранена в репозиторий")
        else:
            logger.info("ℹ️ Изменений в истории нет")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Ошибка Git: {e}")

# ==================== ОЧИСТКА ТЕКСТА ====================
def clean_text(text):
    """Удаляет спецсимволы, markdown, скобки и экранирует HTML."""
    if not text: return ""
    # Оставляем только буквы (в т.ч. кириллицу), цифры, пробелы и базовую пунктуацию.
    # Квадратные [], фигурные {}, угловые <>, звездочки *, решетки # и т.д. будут УДАЛЕНЫ.
    text = re.sub(r'[^\w\s\.\,\!\?\-\:\;\"\'\(\)]', '', text, flags=re.UNICODE)
    # Схлопываем множественные пробелы и переносы строк
    text = re.sub(r'\s+', ' ', text).strip()
    # Экранируем HTML-символы, чтобы исходный текст не ломал нашу верстку Telegram
    return html.escape(text)

# ==================== GIGACHAT ====================
def process_with_gigachat(title, summary):
    """Переводит и сжимает текст через GigaChat."""
    if not GIGACHAT_CREDENTIALS: return None, None
    prompt = f"""Ты - профессиональный редактор новостного Telegram-канала.
Переведи заголовок и описание на русский язык и сожми описание до 700 символов.
Требования:
1. Сохрани главную мысль и факты. Ничего не придумывай.
2. Убери все спецсимволы, квадратные скобки, звездочки, markdown-разметку.
3. Ответ верни СТРОГО в формате:
[ЗАГОЛОВОК]
[ОПИСАНИЕ]

Оригинал: {title}
Описание: {summary}
"""
    try:
        with GigaChat(credentials=GIGACHAT_CREDENTIALS, scope="GIGACHAT_API_PERS", verify_ssl_certs=False) as giga:
            response = giga.chat(Chat(messages=[Messages(role=MessagesRole.USER, content=prompt)], model="GigaChat:latest"))
            result = response.choices[0].message.content.strip()
            
            parts = result.split('\n', 1)
            # Жесткая очистка ответа нейросети
            ru_title = clean_text(parts[0].strip())
            ru_summary = clean_text(parts[1].strip()) if len(parts) > 1 else ru_title
            
            if not ru_title or not ru_summary: return None, None
            
            # Страховка от превышения 800 символов суммарно
            full_text = f"{ru_title}\n\n{ru_summary}"
            if len(full_text) > 750:
                max_summary_len = 750 - len(ru_title) - 4
                if max_summary_len > 0:
                    ru_summary = ru_summary[:max_summary_len].rsplit(' ', 1)[0] + "..."
                    
            return ru_title, ru_summary
    except Exception as e:
        logger.error(f"❌ Ошибка GigaChat: {e}\n{traceback.format_exc()}")
        return None, None

# ==================== ОБРАБОТКА RSS ====================
def clean_html(raw_html):
    if not raw_html: return ""
    return BeautifulSoup(raw_html, "lxml").get_text(separator=' ', strip=True)

def extract_image(entry):
    """Безопасное извлечение картинки из RSS-записи"""
    try:
        # Проверяем, что списки существуют и они ТОЧНО НЕ пустые
        if entry.get('media_content') and len(entry.media_content) > 0:
            return entry.media_content[0].get('url')
        if entry.get('enclosures') and len(entry.enclosures) > 0:
            return entry.enclosures[0].get('url')
    except (IndexError, KeyError, TypeError, AttributeError):
        pass
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
        logger.error(f"❌ Ошибка Telegram: {e}\n{traceback.format_exc()}")
        return False

# ==================== ГЛАВНАЯ ЛОГИКА ====================
def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, GIGACHAT_CREDENTIALS]):
        logger.error("❌ Не все переменные окружения установлены")
        return False

    history = load_history()
    last_index = history.get('last_source_index', -1)
    # Используем set для мгновенной проверки дубликатов
    sent_links = set(history.get('sent_links', [])) 
    
    content = None
    chosen_index = -1
    new_link = ""

    # Жесткая ротация с проверкой истории и авто-перебором
    for i in range(len(RSS_FEEDS)):
        next_index = (last_index + 1 + i) % len(RSS_FEEDS)
        feed_info = RSS_FEEDS[next_index]
        logger.info(f"🎯 Пробуем источник: {feed_info['name']} ({next_index + 1}/{len(RSS_FEEDS)})")
        
        try:
            feed = feedparser.parse(feed_info['url'])
            if not feed.entries: 
                logger.warning(f"⚠️ Лента {feed_info['name']} пуста.")
                continue

            for entry in feed.entries:
                link = entry.get('link', '')
                # ГЛАВНАЯ ПРОВЕРКА: если ссылка уже в истории, пропускаем её
                if not link or link in sent_links: 
                    continue 
                
                title = entry.get('title', '')
                summary = clean_html(entry.get('summary', entry.get('description', '')))
                
                ru_title, ru_summary = process_with_gigachat(title, summary)
                if not ru_title or not ru_summary: 
                    logger.warning(f"⚠️ GigaChat не смог обработать '{title}'. Переход к следующей новости.")
                    continue

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
                
                content = {
                    'type': 'photo' if image_url else 'text',
                    'url': image_url,
                    'caption': caption,
                    'link': link
                }
                new_link = link
                chosen_index = next_index
                break # Нашли статью, выходим из цикла статей
                
        except Exception as e:
            logger.error(f"❌ Ошибка парсинга {feed_info['name']}: {e}\n{traceback.format_exc()}")
            
        if content:
            break # Нашли источник, выходим из цикла источников

    if not content:
        logger.error("❌ Не удалось найти новые уникальные новости ни в одном источнике.")
        return False

    # Публикация и сохранение
    if send_to_telegram(content):
        logger.info("✅ Пост опубликован. Обновляем историю...")
        history['last_source_index'] = chosen_index
        history['sent_links'].append(new_link)
        save_history(history)
        commit_and_push()
        return True
        
    return False

if __name__ == "__main__":
    main()

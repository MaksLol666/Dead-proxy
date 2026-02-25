import os
import re
import asyncio
import json
import socket
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
from telethon import TelegramClient, events

# Загружаем настройки из .env
load_dotenv()
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
PHONE_NUMBER = os.getenv('PHONE_NUMBER')
TARGET_CHANNEL = int(os.getenv('TARGET_CHANNEL'))

# Паттерн для поиска MTProto прокси-ссылок
PROXY_PATTERN = r'(tg://proxy\?server=[^&\s]+&port=\d+&secret=[a-fA-F0-9]+)'

# Файлы для хранения данных
PROXY_DB_FILE = 'proxy_db.json'           # База всех когда-либо отправленных прокси
PENDING_FILE = 'pending_proxies.json'     # Очередь прокси, собранных за час

def load_source_channels():
    """Загружает список каналов для мониторинга из файла"""
    try:
        with open('channels.txt', 'r') as f:
            channels = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        print(f"✅ Загружено каналов для мониторинга: {len(channels)}")
        return channels
    except FileNotFoundError:
        print("❌ Файл channels.txt не найден. Создайте его со списком каналов.")
        return []

def extract_proxy_links(text):
    """Извлекает ВСЕ ссылки на прокси из текста сообщения"""
    if not text:
        return []
    return re.findall(PROXY_PATTERN, text)

def load_json_file(filename, default=None):
    """Загружает данные из JSON файла"""
    if default is None:
        default = {}
    try:
        with open(filename, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json_file(filename, data):
    """Сохраняет данные в JSON файл"""
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)

async def check_proxy(proxy_link):
    """
    Проверяет, работает ли прокси-сервер
    Возвращает True если порт открыт, False если нет
    """
    try:
        # Парсим ссылку
        parsed = urlparse(proxy_link)
        params = parse_qs(parsed.query)
        
        server = params.get('server', [None])[0]
        port = int(params.get('port', [0])[0])
        
        if not server or not port:
            return False
        
        # Пробуем подключиться к серверу (таймаут 3 секунды)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((server, port))
        sock.close()
        
        return result == 0  # 0 значит порт открыт
    except Exception as e:
        print(f"⚠️ Ошибка при проверке {proxy_link[:50]}...: {e}")
        return False

async def cleanup_old_proxies(client, target_entity):
    """Удаляет прокси старше 24 часов из канала и базы"""
    print("🧹 Запускаю очистку старых прокси...")
    
    db = load_json_file(PROXY_DB_FILE)
    now = datetime.now()
    threshold = now - timedelta(hours=24)
    
    # Находим прокси для удаления
    to_delete = []
    for proxy_link, added_time_str in db.items():
        added_time = datetime.fromisoformat(added_time_str)
        if added_time < threshold:
            to_delete.append(proxy_link)
    
    if not to_delete:
        print("✅ Нет прокси старше 24 часов")
        return
    
    print(f"🔍 Найдено {len(to_delete)} прокси старше 24 часов")
    
    # Ищем сообщения в канале и удаляем
    deleted_count = 0
    async for message in client.iter_messages(target_entity, limit=100):
        if not message.text:
            continue
        
        for proxy_link in to_delete:
            if proxy_link in message.text:
                await message.delete()
                print(f"🗑️ Удалил прокси: {proxy_link[:50]}...")
                # Удаляем из базы
                del db[proxy_link]
                deleted_count += 1
                break
    
    save_json_file(PROXY_DB_FILE, db)
    print(f"✅ Очистка завершена. Удалено {deleted_count} прокси. Осталось {len(db)} в базе")

async def process_pending_proxies(client, target_entity):
    """
    Проверяет накопленные прокси и отправляет рабочие в канал
    Запускается каждый час
    """
    print(f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Начинаю обработку накопленных прокси...")
    
    # Загружаем очередь и базу
    pending = load_json_file(PENDING_FILE, [])
    db = load_json_file(PROXY_DB_FILE)
    
    if not pending:
        print("📭 Нет новых прокси для обработки")
        return
    
    print(f"📊 В очереди: {len(pending)} прокси для проверки")
    
    # Проверяем каждый прокси
    working_proxies = []
    for proxy_link in pending:
        print(f"🔄 Проверяю: {proxy_link[:50]}...", end=" ")
        
        if await check_proxy(proxy_link):
            print("✅ РАБОТАЕТ")
            working_proxies.append(proxy_link)
        else:
            print("❌ не работает")
        
        # Небольшая задержка между проверками
        await asyncio.sleep(0.5)
    
    if not working_proxies:
        print("😔 Нет рабочих прокси в этой партии")
        # Очищаем очередь и выходим
        save_json_file(PENDING_FILE, [])
        return
    
    print(f"\n📨 Отправляю {len(working_proxies)} рабочих прокси в канал...")
    
    # Отправляем все рабочие прокси в канал
    sent_count = 0
    for proxy_link in working_proxies:
        # Проверяем, не отправляли ли уже такой прокси
        if proxy_link in db:
            print(f"⏭️ Прокси уже есть в базе, пропускаю: {proxy_link[:50]}...")
            continue
        
        # Отправляем ссылку
        await client.send_message(
            target_entity,
            proxy_link,
            link_preview=False
        )
        
        # Сохраняем в базу
        db[proxy_link] = datetime.now().isoformat()
        sent_count += 1
        print(f"✅ Отправлено: {proxy_link[:50]}...")
        
        # Небольшая задержка между отправками
        await asyncio.sleep(0.3)
    
    # Сохраняем обновленную базу
    save_json_file(PROXY_DB_FILE, db)
    
    # Очищаем очередь
    save_json_file(PENDING_FILE, [])
    
    print(f"✨ Готово! Отправлено {sent_count} новых прокси. Всего в базе: {len(db)}")

async def main():
    # Создаем клиент Telethon
    client = TelegramClient('proxy_scraper_session', API_ID, API_HASH)
    
    # Авторизуемся
    await client.start(phone=PHONE_NUMBER)
    me = await client.get_me()
    print("✅ Успешно авторизовались как", me.first_name)
    
    # Загружаем каналы для мониторинга
    source_channels = load_source_channels()
    if not source_channels:
        print("❌ Нет каналов для мониторинга. Завершаем работу.")
        return
    
    # Получаем целевой канал
    target_entity = await client.get_entity(TARGET_CHANNEL)
    print(f"📢 Целевой канал: {target_entity.title}")
    
    # Загружаем существующие базы
    db = load_json_file(PROXY_DB_FILE)
    pending = load_json_file(PENDING_FILE, [])
    print(f"📚 В базе: {len(db)} прокси")
    print(f"📥 В очереди: {len(pending)} прокси на проверку")
    
    # Запускаем периодические задачи
    async def scheduled_tasks():
        """Запускает задачи по расписанию"""
        # Ждем до следующего полного часа
        now = datetime.now()
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        wait_seconds = (next_hour - now).total_seconds()
        
        print(f"⏰ Первая отправка через {int(wait_seconds/60)} минут (в {next_hour.strftime('%H:%M')})")
        
        # Ждем до следующего часа
        await asyncio.sleep(wait_seconds)
        
        while True:
            # Обрабатываем накопленные прокси
            await process_pending_proxies(client, target_entity)
            
            # Очищаем старые прокси (раз в 24 часа)
            if datetime.now().hour == 3:  # В 3 часа ночи
                await cleanup_old_proxies(client, target_entity)
            
            # Ждем 1 час до следующей проверки
            print(f"⏰ Следующая проверка через 1 час (в {(datetime.now() + timedelta(hours=1)).strftime('%H:%M')})")
            await asyncio.sleep(3600)
    
    # Запускаем планировщик в фоне
    asyncio.create_task(scheduled_tasks())
    
    @client.on(events.NewMessage(chats=source_channels))
    async def handler(event):
        """Обрабатывает новые сообщения - собирает все прокси в очередь"""
        message = event.message
        
        if not message.text:
            return
        
        # Ищем все ссылки на прокси
        proxy_links = extract_proxy_links(message.text)
        
        if proxy_links:
            # Загружаем текущую очередь
            pending = load_json_file(PENDING_FILE, [])
            db = load_json_file(PROXY_DB_FILE)
            
            added_count = 0
            for proxy_link in proxy_links:
                # Пропускаем дубликаты (если уже есть в очереди или в базе)
                if proxy_link in pending or proxy_link in db:
                    continue
                
                pending.append(proxy_link)
                added_count += 1
                print(f"📥 Добавил в очередь: {proxy_link[:50]}...")
            
            if added_count > 0:
                # Сохраняем обновленную очередь
                save_json_file(PENDING_FILE, pending)
                print(f"📦 В очереди теперь: {len(pending)} прокси")
    
    print(f"👀 Начинаю мониторинг {len(source_channels)} каналов...")
    print("🟢 Бот запущен. Сбор прокси идет круглосуточно, отправка раз в час")
    print("⏰ Очистка старых прокси каждые сутки в 3:00")
    
    # Держим соединение открытым
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())



import itertools
import re
import io
import json
import random
import time
import datetime
import os
import shutil
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from PIL import Image

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google import genai

from config import (
    TELEGRAM_TOKEN,
    GEMINI_KEYS,
    MODEL_NAME,
    OWNER_ID,   
    ROLE_OWNER_ID,
)
from memory import (
    USER_MEMORY,
    USER_PROFILE,
    clear_user_memory,
    get_profile,
    save_memory,
    get_memory,
    get_visual_memory,
    save_visual_memory,
    clear_visual_memory,
)
from characters import (
    get_characters,
    add_character,
    save_character_list,
    remove_character,
    set_active_character,
    get_active_character,
    add_character_change,
    get_character_changes,
    clear_character_temporary_changes,
    remove_character_change,
    save_characters,
    load_characters,
)
from ekaterina import PERSONALITY

# ==========================
# Фоновый веб-сервер для Render (Health Check & Anti-Sleep)
# ==========================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"Ekaterina Bot is alive and well!")

    def log_message(self, format, *args):
        # Отключаем спам в консоль при регулярных пингах
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"Фоновый веб-сервер для Render запущен на порту {port}")
    server.serve_forever()

# ==========================
# Правила ролевой
# ==========================

with open("rules.txt", "r", encoding="utf-8") as file:
    RULES = file.read()
with open("relationship_rules.txt", "r", encoding="utf-8") as file:
    RELATIONSHIP_RULES = file.read()
with open("lore.txt", "r", encoding="utf-8") as file:
    LORE = file.read()

# ==========================
# Настройки
# ==========================

TRIGGERS = [
    "катя",
    "катюша",
    "кать",
    "екатерина",
]

print("Найдено Gemini ключей:", len(GEMINI_KEYS))
print("Используемая модель:", MODEL_NAME)

if not GEMINI_KEYS:
    raise Exception("Нет Gemini ключей")

keys = itertools.cycle(GEMINI_KEYS)

# ==========================
# Безопасное извлечение текста из Gemini
# ==========================

def get_gemini_text(response):
    """Безопасно извлекает текст из ответа Gemini, исключая ошибки NoneType."""
    if not response:
        return ""
    try:
        if hasattr(response, "text") and response.text:
            return response.text.strip()
    except Exception:
        pass

    try:
        if hasattr(response, "candidates") and response.candidates:
            for cand in response.candidates:
                if cand.content and cand.content.parts:
                    parts_text = [p.text for p in cand.content.parts if hasattr(p, "text") and p.text]
                    if parts_text:
                        return "\n".join(parts_text).strip()
    except Exception:
        pass
    return ""

# ==========================
# Нормализация категорий статусов
# ==========================

def normalize_status_category(cat_str):
    """Приводит любые названия категорий от ИИ к строгим ключам анкеты."""
    if not cat_str:
        return "temporary"
    c = str(cat_str).lower().strip()
    if c in ["rumor", "rumors", "слух", "слухи", "сплетня", "сплетни"]:
        return "rumors"
    elif c in ["tragedy", "tragedies", "трагедия", "трагедии"]:
        return "tragedies"
    elif c in ["permanent", "постоянный", "постоянные", "perm"]:
        return "permanent"
    elif c in ["achievement", "achievements", "достижение", "достижения"]:
        return "achievements"
    else:
        return "temporary"

# ==========================
# Автоматическая очистка и архивация устаревших слухов
# ==========================

RUMOR_MAX_AGE_SECONDS = 7 * 86400  # Слухи устаревают через 7 дней
MAX_ACTIVE_RUMORS = 5              # Максимум 5 активных слухов в анкете

def clean_expired_statuses_and_rumors(char):
    """Очищает устаревшие слухи, переносит их в архив и ограничивает размер анкеты."""
    if not char or not isinstance(char, dict):
        return char

    if "changes" not in char or not isinstance(char["changes"], dict):
        char["changes"] = {"permanent": [], "temporary": [], "achievements": [], "tragedies": [], "rumors": []}

    changes = char["changes"]
    status_times = char.setdefault("status_times", {})
    archived_rumors = changes.setdefault("archived_rumors", [])

    now = time.time()
    rumors = changes.get("rumors", [])
    valid_rumors = []

    for r in rumors:
        r_time = status_times.get(r)
        if r_time and (now - r_time > RUMOR_MAX_AGE_SECONDS):
            archived_rumors.append(f"{r} (забыт обществом)")
            status_times.pop(r, None)
            continue
        valid_rumors.append(r)

    # Если слухов все еще больше лимита, старые отправляем в архив
    if len(valid_rumors) > MAX_ACTIVE_RUMORS:
        excess = valid_rumors[:-MAX_ACTIVE_RUMORS]
        valid_rumors = valid_rumors[-MAX_ACTIVE_RUMORS:]
        for ex in excess:
            archived_rumors.append(f"{ex} (ушел в архив)")
            status_times.pop(ex, None)

    changes["rumors"] = valid_rumors
    if len(archived_rumors) > 10:
        changes["archived_rumors"] = archived_rumors[-10:]

    return char

# ==========================
# Определение точной даты и сезона
# ==========================

def get_current_season_info():
    """Определяет точное Московское время (UTC+3), день недели, число, месяц, 1838 год и сезон."""
    msk_tz = datetime.timezone(datetime.timedelta(hours=3))
    now = datetime.datetime.now(msk_tz)
    
    month = now.month
    day = now.day
    weekday_idx = now.weekday()
    time_str = now.strftime("%H:%M")
    
    hour = now.hour
    if 5 <= hour < 12:
        time_of_day = "утро"
    elif 12 <= hour < 17:
        time_of_day = "день"
    elif 17 <= hour < 23:
        time_of_day = "вечер"
    else:
        time_of_day = "ночь"

    weekdays_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    months_genitive_ru = [
        "", "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря"
    ]
    
    weekday_str = weekdays_ru[weekday_idx]
    month_str = months_genitive_ru[month]
    
    if month in [12, 1, 2]:
        season = "Зима"
        phase = "начало" if month == 12 else ("середина" if month == 1 else "конец")
    elif month in [3, 4, 5]:
        season = "Весна"
        phase = "начало" if month == 3 else ("середина" if month == 4 else "конец")
    elif month in [6, 7, 8]:
        season = "Лето"
        phase = "начало" if month == 6 else ("середина" if month == 7 else "конец")
    else:
        season = "Осень"
        phase = "начало" if month == 9 else ("середина" if month == 10 else "конец")
        
    return f"{weekday_str}, {day} {month_str} 1838 года, {time_str} МСК ({time_of_day}, {season}, {phase} сезона)"

# ==========================
# СЕКРЕТНЫЕ АДМИНИСТРАТИВНЫЕ ДИРЕКТИВЫ ПОВЕДЕНИЯ
# ==========================

PERSISTENT_DIR = '/data' if os.path.exists('/data') else ('data' if os.path.exists('data') else '.')
DIRECTIVES_FILE_NAME = "directives.json"
DIRECTIVES_FILE = os.path.join(PERSISTENT_DIR, DIRECTIVES_FILE_NAME)

ADMIN_DIRECTIVES = {}

def load_directives():
    global ADMIN_DIRECTIVES
    try:
        with open(DIRECTIVES_FILE, "r", encoding="utf-8") as f:
            ADMIN_DIRECTIVES = json.load(f)
    except Exception:
        ADMIN_DIRECTIVES = {}

def save_directives():
    with open(DIRECTIVES_FILE, "w", encoding="utf-8") as f:
        json.dump(ADMIN_DIRECTIVES, f, ensure_ascii=False, indent=2)

load_directives()

def get_directive_for_user(user_id, active_char=None, username=None):
    """Собирает секретные указания админов для конкретного игрока или персонажа."""
    directives = []
    
    if user_id in ADMIN_DIRECTIVES:
        directives.append(ADMIN_DIRECTIVES[user_id])
        
    if username:
        uname_key = "@" + username.lower().replace("@", "")
        if uname_key in ADMIN_DIRECTIVES:
            directives.append(ADMIN_DIRECTIVES[uname_key])
            
    if active_char and active_char.get("name"):
        cname_key = active_char.get("name").lower().strip()
        if cname_key in ADMIN_DIRECTIVES:
            directives.append(ADMIN_DIRECTIVES[cname_key])
            
    return " ".join(directives) if directives else None

# ==========================
# Вспомогательная асинхронная обертка для Gemini
# ==========================

async def call_gemini_async(messages):
    """Выполняет тяжелый запрос к Gemini в отдельном потоке."""
    def _sync_call():
        client = create_client()
        return client.models.generate_content(model=MODEL_NAME, contents=messages)
    return await asyncio.to_thread(_sync_call)

# ==========================
# ГЕНЕРАТОР СВЕТСКИХ СОВЕТОВ ПО ОТЫГРЫШУ (/совет)
# ==========================

async def generate_roleplay_advice(user_query, active_char, user_id, chat_id):
    """Генерирует разжёванный совет по отыгрышу любого действия в 1838 году."""
    all_active = get_all_active_characters(filter_chat_id=chat_id)
    target_char = None
    if user_query:
        q_lower = user_query.lower()
        for tuid, tc in all_active:
            if tuid != user_id:
                c_name = tc.get("name", "").lower()
                if c_name and (c_name in q_lower or any(part in q_lower for part in c_name.split() if len(part) > 3)):
                    target_char = tc
                    break

    season_info = get_current_season_info()
    
    target_info = ""
    if target_char:
        target_info = f"\nВНИМАНИЕ: Пользователь также упомянул второго персонажа в запросе: {target_char.get('name')} ({target_char.get('title')}, характер: {target_char.get('personality')}). Обязательно учитывай их взаимную динамику в совете!"

    char_info = "Не выбран"
    if active_char:
        char_info = f"Имя: {active_char.get('name')}, Сословие: {active_char.get('estate')}, Титул: {active_char.get('title')}, Характер: {active_char.get('personality')}, Навыки: {active_char.get('skills_hobbies')}"

    prompt = f"""
Ты — Всезнающий Светский Наставник и Мастер ТРИ в лице Екатерины II (Петербург 1838 года).
Пользователь просит совет по отыгрышу следующего действия/ситуации: "{user_query}"

Персонаж запрашивающего совета:
{char_info}
{target_info}

🗓️ ТЕКУЩАЯ ДАТА И СЕЗОН В РЕАЛЬНОМ МИРЕ: {season_info}

СТРОГИЕ ПРАВИЛА ОФОРМЛЕНИЯ:
1. Заголовок выведи СНАРУЖИ цитат:
<b>💡 Императорский Наставник | Совет по отыгрышу</b>

2. Каждый из 6 разделов выведи в СВОЕЙ СОБСТВЕННОЙ ОТДЕЛЬНОЙ НЕЗАКРЫВАЕМОЙ ЦИТАТЕ <blockquote>...</blockquote> (используй обычный тег <blockquote>, без expandable!).

СТРУКТУРА 6 ЦИТАТ:

<blockquote>📌 <b>1. Механика действия простыми словами:</b>
(Разжуй пошагово для новичков: Шаг 1, Шаг 2, Шаг 3 на понятном и ролевом языке).</blockquote>

<blockquote>🎲 <b>2. От чего зависит результат:</b>
(Явно укажи, на чем строится результат: на Навыках из анкеты, Сословном статусе или Соглашении игроков.
СТРОГОЕ ПРАВИЛО: Использовать 6-гранный кубик Telegram 🎲 разрешено упоминать ИСКЛЮЧИТЕЛЬНО в советах по ДУЭЛЯМ!
Во всех остальных случаях (карты, балы, торги, допросы, визиты) результат зависит СТРОГО от личных навыков персонажа из анкеты, его сословного статуса или взаимной договоренности игроков в OOC //).</blockquote>

<blockquote>🏆 <b>3. Условия Победы и Поражения:</b>
• <b>Победа:</b> Что считается победой + как отыграть триумф по-дворянски без гордыни.
• <b>Поражение:</b> Что считается поражением + как отыграть проигрыш красиво и превратить его в зрелищную драму для сюжета.</blockquote>

<blockquote>🕵️‍♂️ <b>4. Тайные шулерские хитрости:</b>
<tg-spoiler>(ОБЯЗАТЕЛЬНО оберни сам текст хитрости в тег tg-spoiler: распиши, как в этом конкретном деле хитрили, мухлевали или обходили правила в 1838 году: подделка карт, подкуп секундантов/слуг, тайные знаки веером, заговор и т.д.)</tg-spoiler></blockquote>

<blockquote>🎭 <b>5. Персональный совет под анкету:</b>
(Дай совет под характер и навыки персонажа {active_char.get('name') if active_char else 'героя'}: с чего начать, чем закончить и напиши 3 готовые исторические реплики XIX века под его характер. Если вопрос про отыгрыш НПС — дай совет по речи и манерам этого НПС).</blockquote>

<blockquote>🚫 <b>6. ТАБУ И ЗАПРЕТЫ:</b>
(Строгий список 2-3 вещей, которые КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО делать в этой сцене: Powergaming, современный сленг, несоответствие эпохе).</blockquote>

ТЕХНИЧЕСКОЕ ТАБУ: Используй ТОЛЬКО <i>, <b>, <u>, <s>, <strong>, <blockquote>, <tg-spoiler>. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать теги <h1>-<h6>, <p>, <div>, <span>, <br>.
"""
    try:
        response = await call_gemini_async(prompt)
        ans = get_gemini_text(response)
        if not ans:
            return "// Простите, милая, не удалось составить совет прямо сейчас."
        ans = re.sub(r'</?h[1-6]\s*[^>]*>', '', ans)
        ans = re.sub(r'</?p\s*[^>]*>', '\n\n', ans)
        ans = re.sub(r'<br\s*/?>', '\n', ans)
        return ans
    except Exception as e:
        print("Ошибка генерации совета:", e)
        return "// Простите, милая, мои старинные фолианты запылились, не могу составить совет прямо сейчас."

# ==========================
# База данных бизнесов Петербурга
# ==========================

BUSINESSES_FILE_NAME = "businesses.json"
BUSINESSES_FILE = os.path.join(PERSISTENT_DIR, BUSINESSES_FILE_NAME)

if PERSISTENT_DIR == '/data':
    local_path = BUSINESSES_FILE_NAME
    if not os.path.exists(local_path) and os.path.exists(f"data/{BUSINESSES_FILE_NAME}"):
        local_path = f"data/{BUSINESSES_FILE_NAME}"
        
    if not os.path.exists(BUSINESSES_FILE) and os.path.exists(local_path):
        try:
            shutil.copy(local_path, BUSINESSES_FILE)
            print(f"Файл {BUSINESSES_FILE_NAME} успешно перенесен на вечный диск!")
        except Exception as e:
            print(f"Ошибка переноса {BUSINESSES_FILE_NAME}: {e}")

CITY_BUSINESSES = []

def load_businesses():
    global CITY_BUSINESSES
    try:
        with open(BUSINESSES_FILE, "r", encoding="utf-8") as f:
            CITY_BUSINESSES = json.load(f)
    except FileNotFoundError:
        CITY_BUSINESSES = []

def save_businesses():
    with open(BUSINESSES_FILE, "w", encoding="utf-8") as f:
        json.dump(CITY_BUSINESSES, f, ensure_ascii=False, indent=2)

load_businesses()

# ==========================
# ХРАНИЛИЩЕ И ЛОГИКА ИМПЕРСКОЙ ГАЗЕТЫ
# ==========================

GAZETTE_FILE_NAME = "gazette.json"
GAZETTE_FILE = os.path.join(PERSISTENT_DIR, GAZETTE_FILE_NAME)

def load_gazette_data():
    if not os.path.exists(GAZETTE_FILE):
        return {
            "gazette_chat_id": None,
            "last_pinned_msg_id": None,
            "pending_ads": {},
            "approved_ads": [],
            "archive": []
        }
    try:
        with open(GAZETTE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("Ошибка загрузки gazette.json:", e)
        return {
            "gazette_chat_id": None,
            "last_pinned_msg_id": None,
            "pending_ads": {},
            "approved_ads": [],
            "archive": []
        }

def save_gazette_data(data):
    try:
        with open(GAZETTE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Ошибка сохранения gazette.json:", e)

async def publish_weekly_gazette(bot, target_chat_id=None):
    """Генерирует, публикует, открепляет старую и закрепляет новую газету."""
    g_data = load_gazette_data()
    chat_to_send = target_chat_id or g_data.get("gazette_chat_id")
    
    if not chat_to_send:
        print("Ошибка публикации газеты: Чат для газеты не привязан!")
        return False, "Чат для газеты еще не привязан! Напишите /чат_газеты в ролевой группе."

    approved_ads = g_data.get("approved_ads", [])
    ads_text = ""
    if approved_ads:
        for ad in approved_ads:
            ads_text += f"• <i>{ad['author_name']}:</i> «{ad['text']}»\n"
    else:
        ads_text = "• <i>На текущую неделю особых объявлений от жителей столицы в редакцию не поступало.</i>"

    society_registry = get_global_society_registry()
    date_str = datetime.datetime.now().strftime("%d.%m.") + "1838г."
    date_season_info = get_current_season_info()

    prompt = f"""
Ты — главный редактор газеты «Санкт-Петербургские Ведомости» в 1838 году.
Сгенерируй изысканный, атмосферный и богатый информацией еженедельный номер газеты за дата-штамп: {date_str}.

🗓️ ТЕКУЩАЯ ДАТА, ВРЕМЯ И СЕЗОН В РЕАЛЬНОМ МИРЕ: {date_season_info}. 
ПОГОДА И АТМОСФЕРА ГАЗЕТЫ ДОЛЖНЫ СТРОГО СООТВЕТСТВОВАТЬ ЭТОЙ ДАТЕ И СЕЗОНУ В ПЕТЕРБУРГЕ 1838 ГОДА!

📜 ОДОБРЕННЫЕ ОБЪЯВЛЕНИЯ ЖИТЕЛЕЙ (Игроков):
{ads_text}

СТРОГИЕ ПРАВИЛА СОДЕРЖАНИЯ:
1. ПОГОДА: Напиши ПОДРОБНЫЙ ПОДНЕВНЫЙ ПРОГНОЗ ПОГОДЫ на всю неделю (Понедельник, Вторник, Среда, Четверг, Пятница, Суббота, Воскресенье) с колоритом Петербурга 1838 года и строго по текущей дате и сезону ({date_season_info}).
2. ИГРОКИ: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО придумывать любые факты, слухи или события про реальных игроков! Всё, что касается игроков — находится ТОЛЬКО в разделе одобренных объявлений выше.
3. БЕЗ ЗАКОНОВ: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать про указы, законы, реформы и политику правительства.
4. НОВОСТИ И КРИМИНАЛ: Все новости должны быть ИСКЛЮЧИТЕЛЬНО про вымышленных НПС.
5. РЕКЛАМА БИЗНЕСА: Ненавязчиво отрекламируй 1-2 заведения из реестра бизнесов столицы.

СТРУКТУРА ГАЗЕТЫ (Используй HTML-разметку):
Заголовок выведи СНАРУЖИ цитаты blockquote:
<b>📰 САНКТ-ПЕТЕРБУРГСКИЕ ВЕДОМОСТИ | Выпуск от {date_str}</b>

Содержимое оберни СТРОГО в один тег <blockquote expandable> ... </blockquote>:
1. ☀️ <b>Подневный прогноз погоды на неделю:</b>
   • Понедельник: ...
   • Вторник: ...
   • Среда: ...
   • Четверг: ...
   • Пятница: ...
   • Суббота: ...
   • Воскресенье: ...

2. 🎭 <b>Светская хроника НПС и Столичные курьезы:</b> (Слухи про НПС, утерянные вещи, курьезы на Невском, премьеры).
3. 🚔 <b>Происшествия и криминальная хроника столицы (НПС):</b> (Увлекательные происшествия, работы сыскной полиции, дерзкие карманники, карточные шулеры или поддельные векселя вымышленных НПС).
4. 🌍 <b>Заграничные и имперские вести:</b> (Интересные мировые события 1838 года, научные открытия, путешествия).
5. 🛍️ <b>Коммерческий вестник столицы:</b> (Изысканная заметка или реклама заведений из реестра).
6. 📣 <b>Частные объявления жителей:</b>
{ads_text}
7. 👗 <b>Столичный вестник моды:</b> (Заметка про фасоны, ткани и стили 1838 года с учетом текущей даты и сезона: {date_season_info}).

ТЕХНИЧЕСКОЕ ТАБУ: Используй ТОЛЬКО разрешенные теги Telegram: <i>, <b>, <u>, <s>, <strong>, <blockquote>. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать теги <h1>-<h6>, <p>, <div>, <span>, <br> и стили style="".
"""
    try:
        response = await call_gemini_async(prompt)
        gazette_text = get_gemini_text(response)
        if not gazette_text:
            return False, "ИИ не сгенерировал текст газеты."

        gazette_text = re.sub(r'</?h[1-6]\s*[^>]*>', '', gazette_text)
        gazette_text = re.sub(r'</?p\s*[^>]*>', '\n\n', gazette_text)
        gazette_text = re.sub(r'<br\s*/?>', '\n', gazette_text)
        gazette_text = re.sub(r'</?div\s*[^>]*>', '\n', gazette_text)
        gazette_text = re.sub(r'</?span\s*[^>]*>', '', gazette_text)
        gazette_text = re.sub(r'\n{3,}', '\n\n', gazette_text)

        last_pinned = g_data.get("last_pinned_msg_id")
        if last_pinned:
            try:
                await bot.unpin_chat_message(chat_id=chat_to_send, message_id=last_pinned)
            except Exception as e:
                print("Не удалось открепить старую газету:", e)

        sent_msg = await bot.send_message(chat_id=chat_to_send, text=gazette_text, parse_mode="HTML")
        
        try:
            await bot.pin_chat_message(chat_id=chat_to_send, message_id=sent_msg.message_id)
            g_data["last_pinned_msg_id"] = sent_msg.message_id
        except Exception as e:
            print("Не удалось закрепить новую газету:", e)

        g_data["approved_ads"] = []

        summary_prompt = f"Сделай ОДНО короткое, красивое предложение-выжимку главных новостей этого выпуска газеты 1838 года:\n{gazette_text[:1000]}"
        try:
            sum_res = await call_gemini_async(summary_prompt)
            short_summary = get_gemini_text(sum_res).replace("\n", " ") or f"Свежий выпуск Санкт-Петербургских Ведомостей от {date_str}."
        except Exception:
            short_summary = f"Свежий выпуск Санкт-Петербургских Ведомостей от {date_str}."

        g_data["archive"].append({
            "date": date_str,
            "summary": short_summary,
            "deleted": False
        })

        save_gazette_data(g_data)
        return True, "Газета успешно напечатана, отправлена и закреплена!"

    except Exception as e:
        print("Ошибка при выпуске газеты:", e)
        return False, f"Ошибка при генерации газеты: {e}"

async def scheduled_monday_gazette_loop(app):
    """Чистый фоновый планировщик выпуска газеты по понедельникам в 09:00 МСК (06:00 UTC)."""
    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            if now.weekday() == 0 and now.hour == 6 and now.minute == 0:
                print("Запуск планового автовыпуска понедельничной газеты...")
                await publish_weekly_gazette(app.bot)
                await asyncio.sleep(65)
        except Exception as e:
            print("Ошибка в фоновом планировщике газеты:", e)
            
        await asyncio.sleep(30)

async def post_init(application: Application) -> None:
    """Безопасный запуск фоновых задач после инициализации бота."""
    asyncio.create_task(scheduled_monday_gazette_loop(application))

# ==========================
# Глобальные вспомогательные функции реестра и кэша
# ==========================

USERNAME_CACHE = {}
ACTIVE_DUELS = {}

def get_all_active_characters(filter_chat_id=None):
    data = load_characters()
    active_chars = []
    for key in data:
        if key.endswith("_active"):
            continue
        if filter_chat_id and not key.startswith(f"{filter_chat_id}:"):
            continue
            
        uid = str(key)
        index = data.get(uid + "_active", None)
        characters = data.get(uid, [])
        if characters:
            if index is not None and 0 <= index < len(characters):
                active_chars.append((uid, characters[index]))
            else:
                active_chars.append((uid, characters[0]))
    return active_chars

def get_global_society_registry():
    active_chars = get_all_active_characters()
    if not active_chars:
        return "В реестре высшего света пока пусто."
    
    registry = "🏰 ГЛОБАЛЬНЫЙ СВЕТСКИЙ РЕЕСТР ПЕТЕРБУРГА (1838):\n"
    for uid, char in active_chars:
        clean_expired_statuses_and_rumors(char)
        name = char.get("name", "Неизвестный")
        title = char.get("title", "—")
        estate = char.get("estate", "—")
        occupation = char.get("occupation", "—")
        changes = char.get("changes", {})
        
        statuses = []
        for k in ["permanent", "temporary", "achievements", "tragedies", "rumors"]:
            if k in changes and changes[k]:
                statuses.extend(changes[k])
                
        status_info = f" | Текущие слухи и статусы: {', '.join(statuses)}" if statuses else ""
        connections_info = f" | Связи: {char.get('connections', '—')}" if char.get('connections') else ""
        
        registry += f"- {title} {name} ({estate}, деятельность: {occupation}){status_info}{connections_info}\n"
    return registry

async def edit_character_sheet_with_ai(current_char, user_changes_text):
    prompt = f"""
У нас есть существующая анкета персонажа (XIX век) в формате JSON:
{json.dumps(current_char, ensure_ascii=False, indent=2)}

Пользователь передал следующие изменения или дополнения для этой анкеты:
"{user_changes_text}"

Твоя задача — обновить поля персонажа с учетом этих изменений. 
ПРАВИЛА:
1. Если пользователь указывает новое значение поля, аккуратно обнови или ДОПОЛНИ его.
2. Верни СТРОГО обновленный JSON со всеми теми же ключами:
name, age, gender, title, estate, nationality, family, appearance, personality, history, occupation, likes, dislikes, allergies, phobias, skills_hobbies, connections.
Никакого лишнего текста, только чистый JSON.
"""
    try:
        response = await call_gemini_async(prompt)
        raw = get_gemini_text(response).replace("```json", "").replace("```", "").strip()
        updated_data = json.loads(raw)
        
        for key in ["changes", "achievement_times", "status_times", "stats"]:
            if key in current_char:
                updated_data[key] = current_char[key]
                
        return updated_data
    except Exception as e:
        print("Ошибка ИИ при редактировании анкеты:", e)
        return current_char

# ==========================
# Достижения
# ==========================

ALL_ACHIEVEMENTS = {
    "Перо не знает усталости": "Написать письма (ступенчатое)",
    "Любимчик фортуны": "Получить удачный исход в ролевых событиях",
    "Законодатель столичной моды": "Прислать Кате на оценку наряды через картинки",
    "Любимец императрицы": "Написать Кате суммарно сообщения",
    "Мастер дуэльных интриг": "Получить средний или кризисный масштаб конфликта либо участвовать в дуэлях",
    "Любимец петербургских сплетен": "Накопить в разделе слухов анкеты записей",
    "Ночной призрак Фонтанки": "Отправить боту сообщения в ночное время (01:00 - 05:00)",
    "Знаток императорских уставов": "Обратиться к правилам ролевой через ключевые слова",
    "Любитель чистых страниц": "Использовать команду сброса памяти (/clear)",
    "Мастер груптрик": "Отправить групповое письмо сразу трём и более адресатам",
    "Хроничар светских перемен": "Успешно добавить в анкету персонажа любые статусы",
    "Эстет городских трущоб и балов": "Сгенерировать ролевые события в разных темах",
    "Повелитель вееров и реплик": "Написать суммарно реплик в диалоге с ботом",
    "Таинственный незнакомец": "Сменить активного персонажа",
    "Путник ночных дорог": "Сгенерировать ролевое событие в ночное время суток",
    "Именитый гильдеец": "Зарегистрировать собственное заведение в Петербурге"
}

def award_achievement_to_char(char, achievement_name):
    if "changes" not in char:
        char["changes"] = {"permanent": [], "temporary": [], "achievements": [], "tragedies": [], "rumors": []}
    if "achievement_times" not in char:
        char["achievement_times"] = {}
        
    ach_list = char["changes"].get("achievements", [])
    if achievement_name not in ach_list:
        ach_list.append(achievement_name)
        char["changes"]["achievements"] = ach_list
        char["achievement_times"][achievement_name] = time.time()
        return True
    return False

def check_character_achievements(user_id, char, stat_key, amount=1, sub_category=None):
    if not char:
        return []
    if "stats" not in char:
        char["stats"] = {}
    
    char["stats"][stat_key] = char["stats"].get(stat_key, 0) + amount
    if sub_category:
        if "sub_stats" not in char["stats"]:
            char["stats"]["sub_stats"] = {}
        if isinstance(sub_category, list):
            for sc in sub_category:
                char["stats"]["sub_stats"][sc] = True
        else:
            char["stats"]["sub_stats"][sub_category] = True

    unlocked = []
    
    letters_count = char["stats"].get("letters_written", 0)
    if letters_count >= 30 and award_achievement_to_char(char, "Перо не знает усталости (III степень)"):
        unlocked.append("Перо не знает усталости (III степень)")
    elif letters_count >= 15 and award_achievement_to_char(char, "Перо не знает усталости (II степень)"):
        unlocked.append("Перо не знает усталости (II степень)")
    elif letters_count >= 5 and award_achievement_to_char(char, "Перо не знает усталости (I степень)"):
        unlocked.append("Перо не знает усталости (I степень)")

    luck_count = char["stats"].get("luck_success", 0)
    if luck_count >= 15 and award_achievement_to_char(char, "Любимчик фортуны (III степень)"):
        unlocked.append("Любимчик фортуны (III степень)")
    elif luck_count >= 8 and award_achievement_to_char(char, "Любимчик фортуны (II степень)"):
        unlocked.append("Любимчик фортуны (II степень)")
    elif luck_count >= 3 and award_achievement_to_char(char, "Любимчик фортуны (I степень)"):
        unlocked.append("Любимчик фортуны (I степень)")

    outfits_count = char["stats"].get("outfits_sent", 0)
    if outfits_count >= 15 and award_achievement_to_char(char, "Законодатель столичной моды (III степень)"):
        unlocked.append("Законодатель столичной моды (III степень)")
    elif outfits_count >= 8 and award_achievement_to_char(char, "Законодатель столичной моды (II степень)"):
        unlocked.append("Законодатель столичной моды (II степень)")
    elif outfits_count >= 3 and award_achievement_to_char(char, "Законодатель столичной моды (I степень)"):
        unlocked.append("Законодатель столичной моды (I степень)")

    msg_count = char["stats"].get("messages_to_katya", 0)
    if msg_count >= 150 and award_achievement_to_char(char, "Любимец императрицы (III степень)"):
        unlocked.append("Любимец императрицы (III степень)")
    elif msg_count >= 70 and award_achievement_to_char(char, "Любимец императрицы (II степень)"):
        unlocked.append("Любимец императрицы (II степень)")
    elif msg_count >= 30 and award_achievement_to_char(char, "Любимец императрицы (I степень)"):
        unlocked.append("Любимец императрицы (I степень)")

    if char["stats"].get("duels_conflicts", 0) >= 3 and award_achievement_to_char(char, "Мастер дуэльных интриг"):
        unlocked.append("Мастер дуэльных интриг")

    rumors_len = len(char.get("changes", {}).get("rumors", []))
    if rumors_len >= 3 and award_achievement_to_char(char, "Любимец петербургских сплетен"):
        unlocked.append("Любимец петербургских сплетен")

    if char["stats"].get("night_messages", 0) >= 5 and award_achievement_to_char(char, "Ночной призрак Фонтанки"):
        unlocked.append("Ночной призрак Фонтанки")

    if char["stats"].get("rules_checked", 0) >= 5 and award_achievement_to_char(char, "Знаток императорских уставов"):
        unlocked.append("Знаток императорских уставов")

    if char["stats"].get("memory_clears", 0) >= 3 and award_achievement_to_char(char, "Любитель чистых страниц"):
        unlocked.append("Любитель чистых страниц")

    if char["stats"].get("group_letters_sent", 0) >= 1 and award_achievement_to_char(char, "Мастер груптрик"):
        unlocked.append("Мастер груптрик")

    total_statuses = sum(len(char.get("changes", {}).get(k, [])) for k in ["permanent", "temporary", "achievements", "tragedies", "rumors"])
    if total_statuses >= 5 and award_achievement_to_char(char, "Хроничар светских перемен"):
        unlocked.append("Хроничар светских перемен")

    sub_stats = char["stats"].get("sub_stats", {})
    required_themes = ["отношения", "карьера", "деньги", "тайны", "быт"]
    if all(th in sub_stats for th in required_themes) and award_achievement_to_char(char, "Эстет городских трущоб и балов"):
        unlocked.append("Эстет городских трущоб и балов")

    if char["stats"].get("total_replies", 0) >= 50 and award_achievement_to_char(char, "Повелитель вееров и реплик"):
        unlocked.append("Повелитель вееров и реплик")

    if char["stats"].get("character_switches", 0) >= 5 and award_achievement_to_char(char, "Таинственный незнакомец"):
        unlocked.append("Таинственный незнакомец")

    if char["stats"].get("night_events", 0) >= 1 and award_achievement_to_char(char, "Путник ночных дорог"):
        unlocked.append("Путник ночных дорог")

    if char["stats"].get("businesses_created", 0) >= 1 and award_achievement_to_char(char, "Именитый гильдеец"):
        unlocked.append("Именитый гильдеец")

    return unlocked

# ==========================
# Вспомогательные функции
# ==========================

async def send_long_html(message, text_html):
    """Безопасно отправляет длинные HTML сообщения (включая анкеты и советы), разрезая на куски до 3500 символов."""
    if len(text_html) <= 3800:
        try:
            await message.reply_text(text_html, parse_mode="HTML")
            return
        except Exception:
            pass

    raw_content = text_html
    if raw_content.startswith("<blockquote expandable>") and raw_content.endswith("</blockquote>"):
        raw_content = raw_content[len("<blockquote expandable>"): -len("</blockquote>")]

    limit = 3500
    for i in range(0, len(raw_content), limit):
        chunk = raw_content[i:i + limit]
        formatted_chunk = f"<blockquote expandable>{chunk}</blockquote>"
        try:
            await message.reply_text(formatted_chunk, parse_mode="HTML")
        except Exception:
            try:
                await message.get_bot().send_message(chat_id=message.chat_id, text=formatted_chunk, parse_mode="HTML")
            except Exception as e:
                print("Ошибка отправки чанка HTML:", e)

def get_status_duration(timestamp):
    diff = time.time() - timestamp
    if diff < 60:
        return f"{int(diff)} сек."
    elif diff < 3600:
        return f"{int(diff // 60)} мин."
    elif diff < 86400:
        return f"{int(diff // 3600)} ч. {int((diff % 3600) // 60)} мин."
    else:
        return f"{int(diff // 86400)} дн. {int((diff % 86400) // 3600)} ч."

def get_dear(char):
    if not char:
        return "дорогая"
    gender = str(char.get("gender", "")).lower()
    if any(w in gender for w in ["муж", "м", "мужской", "парень", "мальчик"]):
        return "дорогой"
    return "дорогая"

def get_mily(char):
    if not char:
        return "милая"
    gender = str(char.get("gender", "")).lower()
    if any(w in gender for w in ["муж", "м", "мужской", "парень", "мальчик"]):
        return "милый"
    return "милая"

def format_character_sheet(char):
    clean_expired_statuses_and_rumors(char)
    name = char.get("name", "Безымянный")
    age = char.get("age", "—")
    gender = char.get("gender", "—")
    family = char.get("family", "—")
    occupation = char.get("occupation", "—")
    
    title_estate = char.get("title", "—")
    if char.get("estate") and char.get("estate") not in ["—", "-", ""]:
        if title_estate in ["—", "-", ""]:
            title_estate = char.get("estate")
        elif char.get("estate") not in title_estate:
            title_estate += f" / {char.get('estate')}"
            
    nationality = char.get("nationality", "—")
    appearance = char.get("appearance", "—")
    personality = char.get("personality", "—")
    history = char.get("history", "—")
    likes = char.get("likes", "—")
    dislikes = char.get("dislikes", "—")
    allergies = char.get("allergies", "—")
    phobias = char.get("phobias", "—")
    skills_hobbies = char.get("skills_hobbies", "—")
    connections = char.get("connections", "—")

    sheet_text = (
        f"<b>Анкета персонажа:</b> {name}\n"
        f"ㅤㅤ∴ ════ ∴ ✦ ∴ ════ ∴\n"
        f"<b>Возраст/Пол:</b> {age}, {gender}\n"
        f"<b>Семья:</b> {family}\n"
        f"<b>Работа:</b> {occupation}\n"
        f"<b>Титул/Сословие:</b> {title_estate}\n"
        f"<b>Национальность:</b> {nationality}\n"
        f"<b>Внешность:</b> {appearance}\n"
        f"│────────✤────────│\n"
    )
    
    if personality and personality not in ["—", "-"]:
        sheet_text += f"<b>Характеристики:</b> {personality}\n"
    if history and history not in ["—", "-"]:
        sheet_text += f"<b>Биография персонажа:</b> {history}\n"
        
    sheet_text += (
        f"<b>Любит:</b> {likes}\n"
        f"<b>Не любит:</b> {dislikes}\n\n"
        f"<b>Аллергии:</b> {allergies}\n"
        f"<b>Фобии:</b> {phobias}\n"
        f"<b>Навыки + хобби:</b> {skills_hobbies}\n"
    )
    
    if connections and connections not in ["—", "-"]:
        sheet_text += f"<b>Связи с другими персонажами:</b> {connections}\n"
        
    sheet_text += f"│────────✤────────│\n\n"
    sheet_text += "<b>•Статусы полученные в ролевой•</b>\n\n"

    changes = char.get("changes", {})
    status_times = char.get("status_times", {})
    achievement_times = char.get("achievement_times", {})
    
    perm = changes.get("permanent", [])
    temp = changes.get("temporary", [])
    ach = changes.get("achievements", [])
    rum = changes.get("rumors", [])
    trg = changes.get("tragedies", [])
    arch_rum = changes.get("archived_rumors", [])

    temp_with_time = []
    for t in temp:
        if t in status_times:
            temp_with_time.append(f"{t} ({get_status_duration(status_times[t])})")
        else:
            temp_with_time.append(t)

    rum_with_time = []
    for r in rum:
        if r in status_times:
            rum_with_time.append(f"{r} ({get_status_duration(status_times[r])})")
        else:
            rum_with_time.append(r)

    ach_with_time = []
    for a in ach:
        if a in achievement_times:
            ach_with_time.append(f"{a} ({get_status_duration(achievement_times[a])})")
        else:
            ach_with_time.append(a)
    
    sheet_text += "<b>Постоянные:</b> " + (", ".join(perm) if perm else "—") + "\n\n"
    sheet_text += "<b>Временные:</b> " + (", ".join(temp_with_time) if temp_with_time else "—") + "\n\n"
    sheet_text += "<b>Достижения:</b> " + (", ".join(ach_with_time) if ach_with_time else "—") + "\n\n"
    sheet_text += "<b>Слухи в обществе:</b> " + (", ".join(rum_with_time) if rum_with_time else "—") + "\n\n"
    if arch_rum:
        sheet_text += "<b>Архив забытых слухов:</b> " + (", ".join(arch_rum)) + "\n\n"
    sheet_text += "<b>Трагедии:</b> " + (", ".join(trg) if trg else "—") + "\n"

    return f"<blockquote expandable>{sheet_text}</blockquote>"

def get_rule_by_number(number_str, is_relationship=False):
    source_text = RELATIONSHIP_RULES if is_relationship else RULES
    for line in source_text.split("\n"):
        if line.strip().startswith(f"{number_str}."):
            return line.strip()
    return ""

def apply_duel_result(target_user_id, result_type, opponent_name, duration, end_time_str):
    chars = get_characters(target_user_id)
    if not chars: return []
    active_char = ACTIVE_CHARACTER.get(target_user_id) or chars[0]
    
    if "stats" not in active_char: active_char["stats"] = {}
    if "duels_history" not in active_char["stats"]: active_char["stats"]["duels_history"] = []

    active_char["stats"]["duels_conflicts"] = active_char["stats"].get("duels_conflicts", 0) + 1
    
    if result_type == "win":
        active_char["stats"]["duels_won"] = active_char["stats"].get("duels_won", 0) + 1
        res_str = "Победа"
    elif result_type == "loss":
        active_char["stats"]["duels_lost"] = active_char["stats"].get("duels_lost", 0) + 1
        res_str = "Поражение"
    else:
        active_char["stats"]["duels_cancelled"] = active_char["stats"].get("duels_cancelled", 0) + 1
        res_str = "Отменена"

    unlocked = []
    if result_type != "cancel":
        active_char["stats"]["duels_history"].append({
            "opponent": opponent_name,
            "result": res_str,
            "duration": duration,
            "ended_at": end_time_str
        })
        unlocked += check_character_achievements(target_user_id, active_char, "duels_conflicts", 1)
        if result_type == "win":
            unlocked += check_character_achievements(target_user_id, active_char, "luck_success", 1)

    for i, c in enumerate(chars):
        if c.get("name") == active_char.get("name"):
            chars[i] = active_char
            break
            
    save_characters(target_user_id, chars)
    ACTIVE_CHARACTER[target_user_id] = active_char
    return unlocked

# ==========================
# Gemini
# ==========================

def create_client():
    return genai.Client(
        api_key=next(keys)
    )

MEMORY_LIMIT = 50
WAITING_CHARACTER = {}
ACTIVE_CHARACTER = {}
WAITING_CHARACTER_UPDATE = {}

VISUAL_MEMORY_PROMPT = """
Ты решаешь, нужно ли сохранить изображение в памяти для будущих разговоров.
Ответь строго JSON без пояснений.
Возможные типы:
- none — не сохранять
- context — временно помнить, о какой картинке сейчас идет разговор
- character — персонаж пользователя
- avatar — аватар пользователя
- pet — питомец пользователя
- preference — вкус или предпочтение пользователя
- object — важный личный предмет

Формат ответа:
{
  "type": "none",
  "save": false,
  "replace": false,
  "summary": ""
}
"""

async def generate_business_assortment(name, b_type, status, owner_name):
    season_info = get_current_season_info()
    prompt = f"""
Ты — Катя (Екатерина II), ведущая ролевой игры Петербурга 1838 года.
Сгенерируй меню или ассортимент для заведения:
Название: "{name}"
Тип: {b_type}
Статус: {status}
Владелец: {owner_name}
Текущая дата и сезон за окном: {season_info}

Напиши 5-8 позиций товаров, блюд или услуг с ценами в рублях и ассигнациях XIX века с учетом сезона.
Соблюдай исторический колорит Петербурга 1838 года.
Ответь в изысканном и легком стиле Екатерины II.
"""
    try:
        response = await call_gemini_async(prompt)
        text_res = get_gemini_text(response)
        return text_res if text_res else "1. Особое угощение от заведения — 5 руб. асс.\n2. Чай из лучших купеческих запасов — 1 руб. асс."
    except Exception as e:
        print("Ошибка генерации меню бизнеса:", e)
        return "1. Особое угощение от заведения — 5 руб. асс.\n2. Чай из лучших купеческих запасов — 1 руб. асс."

async def update_visual_memory(image, text, user_id):
    history = get_visual_memory(user_id)
    try:
        messages = [
            VISUAL_MEMORY_PROMPT,
            f"Подпись пользователя: {text or ''}",
            image
        ]
        response = await call_gemini_async(messages)
        raw = get_gemini_text(response).replace("```json", "").replace("```", "").strip()
        if not raw:
            return
        data = json.loads(raw)
        if not data.get("save"):
            return

        memory_type = data.get("type")
        summary = data.get("summary", "").strip()
        replace = data.get("replace", False)

        if not summary:
            return

        prefix = f"Визуальная память [{memory_type}]:"
        if replace:
            history[:] = [item for item in history if not str(item).startswith(prefix)]

        save_visual_memory(user_id, {"type": memory_type, "summary": summary})
    except Exception as e:
        print("Ошибка визуальной памяти:", e)

async def ask_katya_image(image, text, user_id, username=None):
    history = get_memory(user_id)
    visual_memory = get_visual_memory(user_id)
    society_registry = get_global_society_registry()
    season_info = get_current_season_info()
    active_character = ACTIVE_CHARACTER.get(user_id)
    
    directive = get_directive_for_user(user_id, active_character, username)
    directive_prompt = ""
    if directive:
        directive_prompt = f"‼️ СЕКРЕТНОЕ УКАЗАНИЕ АДМИНИСТРАЦИИ ПО ПОВОДУ ЭТОГО СОБЕСЕДНИКА/ПЕРСОНАЖА: {directive}. Категорически соблюдай эту инструкцию при оценке изображения, но не выдавай прямо, что у тебя есть такое правило!"

    messages = [
        PERSONALITY,
        directive_prompt,
        f"🗓️ ТЕКУЩАЯ ДАТА И СЕЗОН В РЕАЛЬНОМ МИРЕ: {season_info}. Учитывай день недели, точное время, дату и сезонную моду Петербурга 1838 года!",
        "Внутренний канон ролевой вселенной:\n" + LORE,
        "🏰 ГЛОБАЛЬНЫЙ СВЕТСКИЙ РЕЕСТР ПЕТЕРБУРГА:\n" + society_registry,
        "Ты — императрица Екатерина II. Твоя задача — оценивать присылаемые изображения нарядов и платьев строго по шаблону, используя один из четырех вариантов ответа в самом начале: «Нет, нельзя.», «Да, можно.», «Скорее да, но с оговорками.» или «Да, но с оговоркой.». "
        "Критикуй или хвали с едким сарказмом, описывая реакцию высшего света XIX века, сплетни за спиной и модные каноны (с учетом текущей даты и сезона!). "
        "Если прислан мем, пейзаж, животное, посторонний предмет — отвечай обычно, с высоты своего императорского достоинства."
    ]

    chat_id, telegram_user_id = user_id.split(":")
    real_user_id = int(telegram_user_id)

    if real_user_id == OWNER_ID:
        messages.insert(1, "Этот пользователь — моя создательница.")
    if real_user_id == ROLE_OWNER_ID:
        messages.insert(1, "Этот пользователь — цесаревич Романов Пик Рудольфович II.")

    if active_character:
        clean_expired_statuses_and_rumors(active_character)
        char_info = f"""
Активный персонаж пользователя, с которым ты ведешь диалог:
Имя: {active_character.get('name')}
Пол: {active_character.get('gender')}
Возраст: {active_character.get('age')}
Титул/Сословие: {active_character.get('title')} / {active_character.get('estate')}
Характер: {active_character.get('personality')}
"""
        changes = active_character.get("changes", {})
        status_times = active_character.get("status_times", {})
        if changes:
            char_info += "\nТекущие статусы и изменения персонажа:\n"
            for k, v in changes.items():
                if v and k != "archived_rumors":
                    if k == "temporary":
                        v_timed = [f"{st} (длится {get_status_duration(status_times[st])})" if st in status_times else st for st in v]
                        char_info += f"- {k}: {', '.join(v_timed)}\n"
                    else:
                        char_info += f"- {k}: {', '.join(v)}\n"
        messages.insert(3, char_info)

    for item in history:
        messages.append(item)
    for item in visual_memory:
        messages.append("Визуальная память:\n" + str(item))

    messages.append(text or "Посмотри на изображение.")
    messages.append(image)

    try:
        response = await call_gemini_async(messages)
        answer = get_gemini_text(response)
        if not answer:
            return "// Я не смогла разглядеть данное изображение либо оно претит нормам приличия."

        if not answer.startswith("//"):
            answer = "// " + answer

        history.append("Пользователь отправил изображение.")
        if text:
            history.append("Подпись: " + text)
        history.append("Катя: " + answer)

        await update_visual_memory(image, text, user_id)
        save_memory()
        return answer
    except Exception as e:
        print("Ошибка Vision:", e)
        return "// Я не смогла толком рассмотреть данное изображение."

def compress_memory(user_id):
    history = get_memory(user_id)
    if len(history) < MEMORY_LIMIT:
        return

    old_messages = history[:-20]
    prompt = "Сделай долговременную память пользователя, сохранив главное:\n" + "\n".join(str(m) for m in old_messages)
    try:
        client = create_client()
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        text_res = get_gemini_text(response)
        if text_res:
            USER_MEMORY[str(user_id)] = ["Долговременная память:\n" + text_res]
            save_memory()
    except Exception as e:
        print("Ошибка сжатия памяти:", e)
        save_memory()

def should_remember(text):
    important_words = ["я люблю", "мне нравится", "запомни", "меня зовут", "мой возраст", "моя работа"]
    return any(w in text.lower() for w in important_words)

# ==========================
# Поиск правил по ключевым словам или ИИ
# ==========================

async def analyze_question_for_rule(user_text):
    prompt = f"""
У нас есть две системы правил ролевой:
1. Основные правила (номера от 1 до 35). Вот их тексты:
{RULES}

2. Правила отношений (номера от 1 до 6). Вот их тексты:
{RELATIONSHIP_RULES}

Пользователь задал вопрос о правилах: "{user_text}"

Найди, какой пункт правил лучше всего отвечает на этот вопрос.
Ответь строго JSON в формате:
{{"found": true, "type": "main" (или "relationship"), "number": "номер_пункта"}}
Если не можете найти подходящий пункт, верни:
{{"found": false}}
Никакого лишнего текста, только чистый JSON.
"""
    try:
        response = await call_gemini_async(prompt)
        raw = get_gemini_text(response).replace("```json", "").replace("```", "").strip()
        if not raw:
            return {"found": False}
        data = json.loads(raw)
        return data
    except Exception as e:
        print("Ошибка анализа правил через ИИ:", e)
        return {"found": False}

# ==========================
# Функция для создания изысканных писем (Одиночные)
# ==========================

async def rewrite_letter_command_with_char(text, active_character):
    char_context = ""
    if active_character:
        char_context = f"""
Информация об авторе письма (персонаже пользователя / НПС):
- Имя/Описание: {active_character.get('name', '—')}
- Пол: {active_character.get('gender', '—')}
- Возраст: {active_character.get('age', '—')}
- Титул/Сословие: {active_character.get('title', '—')} / {active_character.get('estate', '—')}
- Характер/Детали: {active_character.get('personality', '—')}
"""

    prompt = f"""
Ты — помощница в стилизации текстов для текстовой ролевой игры (Петербург 1838 года). 
{char_context}
Пользователь передал черновик письма или мысль: "{text}"

СТРОГИЕ ПРАВИЛА ВЫВОДА:
1. Выдай СРАЗУ ГОТОВОЕ ПИСЬМО в изысканном эпистолярном стиле XIX века. 
2. ДОРЕФОРМЕННАЯ ОРФОГРАФИЯ: Обязательно пиши текст по правилам русской дореформенной орфографии (ѣ, i, ѳ, ъ на конце слов после согласных, -аго/-яго).
3. ЗАПРЕЩЕНО писать любые вводные слова. Только текст самого письма.
4. ОФОРМЛЕНИЕ ЦИТАТ И АДРЕСАТА:
   - Первая цитата (текст письма): <blockquote expandable> Текст обращения, само письмо, подпись и в конце: ㅤㅤ∴ ════ ∴ ✦ ════ ∴ ㅤㅤ </blockquote>
   - Вторая цитата (отдельно): <blockquote expandable> Адресат: [Титул и Имя получателя] ([username, если есть]) </blockquote>
5. ТЕХНИЧЕСКОЕ ТАБУ: Используй ТОЛЬКО разрешенные теги Telegram. Запрещены теги <br>, <div>, <span>.
"""
    try:
        response = await call_gemini_async(prompt)
        ans = get_gemini_text(response)
        return ans if ans else "// Охъ, милая, чернильница опрокинулась, и письмо не удалось составить."
    except Exception as e:
        print("Ошибка генерации письма:", e)
        return "// Охъ, милая, чернильница опрокинулась, и письмо не удалось составить. Попробуйте еще раз."

# ==========================
# Функция для создания групповых писем
# ==========================

async def generate_group_letters_command_with_char(query_text, active_character):
    char_context = ""
    if active_character:
        char_context = f"""
Информация об авторе письма:
- Имя/Описание: {active_character.get('name', '—')}
- Пол: {active_character.get('gender', '—')}
- Возраст: {active_character.get('age', '—')}
- Титул/Сословие: {active_character.get('title', '—')} / {active_character.get('estate', '—')}
- Характер/Детали: {active_character.get('personality', '—')}
"""

    prompt = f"""
Ты — помощница в стилизации текстов для текстовой ролевой игры (Петербург 1838 года). 
{char_context}
Пользователь передал данные для ГРУППОВОГО письма: "{query_text}"

СТРОГИЕ ПРАВИЛА ВЫВОДА:
1. ДОРЕФОРМЕННАЯ ОРФОГРАФИЯ (ѣ, i, ѳ, ъ на конце слов).
2. ЗАПРЕЩЕНО писать любые вводные слова.
3. СТРУКТУРА ВЫВОДА:
   Для каждого адресата выводи пару блоков в ОТДЕЛЬНЫХ ЦИТАТАХ:
   - Первая цитата: <blockquote expandable> Текст письма... </blockquote>
   - Вторая цитата: <blockquote expandable> Адресат: [Имя] (@[username]) </blockquote>
4. ТЕХНИЧЕСКОЕ ТАБУ: Запрещены теги <br>, <div>, <span>.
"""
    try:
        response = await call_gemini_async(prompt)
        ans = get_gemini_text(response)
        return ans if ans else "// Охъ, милая, чернильница опрокинулась, групповые послания не удалось составить."
    except Exception as e:
        print("Ошибка генерации группового письма:", e)
        return "// Охъ, милая, чернильница опрокинулась, групповые послания не удалось составить."

# ==========================
# Генерация ролевых событий (Events)
# ==========================

async def generate_roleplay_event(char_list_str, theme):
    try:
        char_list = json.loads(char_list_str)
        if not isinstance(char_list, list):
            char_list = [char_list]
    except Exception:
        char_list = []
        
    if not char_list:
        return "// Произошла ошибка парсинга участников события.", False, False
        
    main_char = char_list[0]
    char_name = main_char.get("name", "Персонаж")
    season_info = get_current_season_info()
    
    participants_info = ""
    for idx, char in enumerate(char_list, 1):
        clean_expired_statuses_and_rumors(char)
        name = char.get("name", "Неизвестный")
        title = char.get("title", "—")
        estate = char.get("estate", "—")
        occupation = char.get("occupation", "—")
        personality = char.get("personality", "—")
        connections = char.get("connections", "—")
        changes = char.get("changes", {})
        
        statuses = []
        for k in ["permanent", "temporary", "achievements", "tragedies", "rumors"]:
            if k in changes and changes[k]:
                statuses.extend(changes[k])
        status_str = f", статусы: {', '.join(statuses)}" if statuses else ""
        
        participants_info += (
            f"{idx}. {title} {name}\n"
            f"   - Сословие/Титул: {estate} | Род занятий: {occupation}\n"
            f"   - Характер: {personality}\n"
            f"   - Связи в анкетной карте: {connections}{status_str}\n\n"
        )
    
    luck_roll = random.randint(1, 100)
    if luck_roll <= 15:
        luck = "🟢 Удачное (Выгода, редкая удача, покровительство)"
        is_success = True
    elif luck_roll <= 65:
        luck = "⚪️ Нейтральное (Случайное происшествие, загадка, новость)"
        is_success = False
    elif luck_roll <= 90:
        luck = "🟡 Слабо отрицательное (Мелкие неприятности, досадный конфуз, легкая утеря)"
        is_success = False
    else:
        luck = "🔴 Неудачное (Скандал, финансовая утеря, ранение, беда)"
        is_success = False
        
    scale_roll = random.randint(1, 100)
    if scale_roll <= 60:
        scale = "🌱 Легкое (Бытовая ситуация, мелкая размолвка, конфуз)"
        is_duel_conflict = False
    elif scale_roll <= 90:
        scale = "⚔️ Среднее (Заметный конфликт, долг, вызов на дуэль, визит полицмейстера)"
        is_duel_conflict = True
    else:
        scale = "🔥 Кризисное (Ужасное/Государственный масштаб, бунт, грандиозный скандал)"
        is_duel_conflict = True

    owned_businesses = []
    other_businesses = []
    for b in CITY_BUSINESSES:
        if b.get("owner_name") == char_name:
            owned_businesses.append(b)
        else:
            other_businesses.append(b)

    business_context = ""
    use_business = False

    if owned_businesses:
        if random.random() < 0.5:
            b_owned = random.choice(owned_businesses)
            business_context = f"\nВНИМАНИЕ: Персонаж {char_name} является владельцем заведения «{b_owned.get('name')}» ({b_owned.get('type')}). Сделай событие связанным с его заведением.\n"
            use_business = True

    if not use_business and other_businesses and random.random() < 0.15:
        b_sample = random.choice(other_businesses)
        business_context = f"\nВплети заведение другого игрока как фоновую локацию: «{b_sample.get('name')}» ({b_sample.get('type')}).\n"

    prompt = f"""
Ты — Мастер Игры в лице Екатерины II для текстовой ролевой игры (Петербург 1838 года).
Сгенерируй ролевое событие-зацепку для участников.

🗓️ ТЕКУЩАЯ ДАТА И СЕЗОН В РЕАЛЬНОМ МИРЕ: {season_info}. Событие должно строго соответствовать этой дате и сезону!

👥 УЧАСТНИКИ СОБЫТИЯ:
{participants_info}

{business_context}

СТРОГИЕ ПРАВИЛА ОФОРМЛЕНИЯ ЗАГОЛОВКА И ЦИТАТЫ:
1. Выведи СНАРУЖИ цитаты blockquote СТРОГО следующую плашку-шапку (без изменений названий полей!):
🎲 <b>Название события:</b> «[Придумай красивое название]»
<b>Исход:</b> {luck}
<b>Масштаб:</b> {scale}
<b>Тема:</b> {theme}

2. Весь остальной литературный текст события оберни строго в ОДИН тег <blockquote expandable> ... </blockquote>:
- 📖 <b>Предыстория:</b> 1-2 абзаца о причинах.
- 📍 <b>Где разворачивается сцена:</b> Опиши атмосферу места с учетом текущей даты и сезона ({season_info}).
- 🔍 <b>Суть происшествия и зацепка:</b> Ситуация для отыгрыша.
[При масштабе Среднее или Кризисное]: 🗣️ <b>Светские толки и слухи:</b> (Сплетни).
[При масштабе Кризисное]: 📰 <b>Сводка из петербургских газет:</b> (Заметка из газеты).
- 👥 <b>Идеи для совместного отыгрыша:</b> 2-3 зацепки.
- 📖 <b>Предлагаемые изменения в анкету:</b> 1-2 статуса на выбор.

ТЕХНИЧЕСКОЕ ТАБУ: Используй ТОЛЬКО разрешенные теги Telegram: <i>, <b>, <u>, <s>, <strong>, <blockquote>. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать теги <h1>, <h2>, <h3>, <h4>, <h5>, <h6>, <p>, <div>, <span>, <br> и стили style="".
"""
    try:
        response = await call_gemini_async(prompt)
        event_text = get_gemini_text(response)
        if not event_text:
            return "// Простите, дорогая, вестовые с вашим событием потерялись в пути.", False, False
        
        event_text = re.sub(r'</?h[1-6]\s*[^>]*>', '', event_text)
        event_text = re.sub(r'</?p\s*[^>]*>', '\n\n', event_text)
        event_text = re.sub(r'<br\s*/?>', '\n', event_text)
        event_text = re.sub(r'</?div\s*[^>]*>', '\n', event_text)
        event_text = re.sub(r'</?span\s*[^>]*>', '', event_text)
        event_text = re.sub(r'\n{3,}', '\n\n', event_text)
        
        return event_text, is_success, is_duel_conflict
    except Exception as e:
        print("Ошибка генерации события:", e)
        return "// Простите, дорогая, вестовые с вашим событием потерялись в пути.", False, False

# ==========================
# Анализ статусов в диалоге
# ==========================

async def analyze_dialogue_for_status_changes(user_text, katya_answer, user_id):
    active_char = ACTIVE_CHARACTER.get(user_id)
    if not active_char:
        return None

    clean_expired_statuses_and_rumors(active_char)
    current_changes = active_char.get("changes", {})
    society_registry = get_global_society_registry()

    prompt = f"""
Проанализируй диалог в ролевой игре (XIX век) между пользователем и Катей.
Текущий персонаж собеседника (автора сообщения): {active_char.get('name')}
Текущие статусы этого персонажа: {json.dumps(current_changes, ensure_ascii=False)}

🏰 ГЛОБАЛЬНЫЙ РЕЕСТР ВСЕХ ПЕРСОНАЖЕЙ В ИГРЕ:
{society_registry}

Сообщение пользователя: "{user_text}"
Ответ Кати: "{katya_answer}"

Определи, произошло ли событие для добавления или удаления статуса/слуха/трагедии.

ВАЖНЕЙШЕЕ ПРАВИЛО ДЛЯ СЛУХОВ И СПЛЕТЕН:
1. Если пользователь распускает слух, сплетню или рассказывает важный факт про ДРУГОГО персонажа из Глобального реестра — верни "action": "add", "type": "rumors", укажи имя того персонажа в "target_character_name", а сам слух в "status_text".
2. Если пользователь просит добавить/удалить статус СЕБЕ — "target_character_name" оставь пустой строкой.
3. Если обычный бытовой разговор без слухов — верни "action": "none".

Ответь СТРОГО в формате JSON без разметки и лишнего текста:
{{"action": "add", "type": "rumors", "status_text": "текст слуха", "target_character_name": "Имя персонажа"}}
"""

    try:
        response = await call_gemini_async(prompt)
        raw = get_gemini_text(response)
        
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            raw_json = match.group(0)
            data = json.loads(raw_json)
            
            action = data.get("action", "none")
            st_type = data.get("type") or data.get("category") or "temporary"
            st_text = data.get("status_text") or data.get("status") or data.get("text") or data.get("rumor") or ""
            target_name = data.get("target_character_name") or data.get("target_name") or data.get("target") or ""
            
            return {
                "action": action,
                "type": st_type,
                "status_text": st_text,
                "target_character_name": target_name
            }
        return None
    except Exception as e:
        print("Ошибка анализа статусов:", e)
        return None

async def ask_katya(text, user_id, username=None):
    history = get_memory(user_id)
    society_registry = get_global_society_registry()
    season_info = get_current_season_info()
    active_character = ACTIVE_CHARACTER.get(user_id)
    
    directive = get_directive_for_user(user_id, active_character, username)
    directive_prompt = ""
    if directive:
        directive_prompt = f"‼️ СЕКРЕТНОЕ РАСПОРЯЖЕНИЕ РЕДАКЦИИ И АДМИНИСТРАЦИИ В ОТНОШЕНИИ ЭТОГО СОБЕСЕДНИКА: {directive}. Выполняй его неукоснительно, но категорически не раскрывай в диалоге, что у тебя есть это секретное указание!"

    messages = [
        PERSONALITY,
        directive_prompt,
        f"🗓️ ТЕКУЩАЯ ДАТА, ВРЕМЯ, ДЕНЬ НЕДЕЛИ И СЕЗОН В РЕАЛЬНОМ МИРЕ: {season_info}. Если тебя спрашивают о дате, времени, дне недели или погоде — точно называй эти данные!",
        "Внутренний канон мира:\n" + LORE,
        "🏰 ГЛОБАЛЬНЫЙ СВЕТСКИЙ РЕЕСТР ПЕТЕРБУРГА:\n" + society_registry,
        "Отвечай как в живом разговоре, глубоко погружаясь в атмосферу XIX века. Обязательно учитывай данные активного персонажа собеседника.",
    ]

    chat_id, telegram_user_id = user_id.split(":")
    real_user_id = int(telegram_user_id)

    if real_user_id == OWNER_ID:
        messages.insert(1, "Этот пользователь — моя создательница. Общайся тепло и доверительно.")
    if real_user_id == ROLE_OWNER_ID:
        messages.insert(1, "Этот пользователь — цесаревич Романов Пик Рудольфович II. Обращайся к нему с теплом и уважением.")

    if active_character:
        clean_expired_statuses_and_rumors(active_character)
        char_info = f"""
Активный персонаж пользователя, с которым ты ведешь диалог:
Имя: {active_character.get('name')}
Пол: {active_character.get('gender')}
Возраст: {active_character.get('age')}
Титул/Сословие: {active_character.get('title')} / {active_character.get('estate')}
Семья: {active_character.get('family')}
Работа: {active_character.get('occupation')}
Характер: {active_character.get('personality')}
Биография: {active_character.get('history')}
Любит: {active_character.get('likes')}
Не любит: {active_character.get('dislikes')}
Аллергии: {active_character.get('allergies')}
Фобии: {active_character.get('phobias')}
Навыки и хобби: {active_character.get('skills_hobbies')}
Связи: {active_character.get('connections')}
"""
        changes = active_character.get("changes", {})
        status_times = active_character.get("status_times", {})
        if changes:
            char_info += "\nТекущие статусы и изменения персонажа:\n"
            for k, v in changes.items():
                if v and k != "archived_rumors":
                    if k == "temporary":
                        v_timed = [f"{st} (длится {get_status_duration(status_times[st])})" if st in status_times else st for st in v]
                        char_info += f"- {k}: {', '.join(v_timed)}\n"
                    else:
                        char_info += f"- {k}: {', '.join(v)}\n"
        
        messages.insert(3, char_info)

    for item in history:
        messages.append(item)
    messages.append(text)

    for attempt in range(len(GEMINI_KEYS)):
        try:
            response = await call_gemini_async(messages)
            answer = get_gemini_text(response)
            
            if not answer:
                answer = "// (Екатерина лишь изящно взмахнула веером и предпочла сменить тему разговора...)"

            if not answer.startswith("//"):
                answer = "// " + answer

            history.append("Пользователь: " + text)
            history.append("Катя: " + answer)

            if should_remember(text):
                history.append("ВАЖНЫЙ ФАКТ: " + text)

            compress_memory(user_id)
            save_memory()
            return answer
        except Exception as e:
            print("ОШИБКА GEMINI:", e)
            if "429" in str(e).lower() or "quota" in str(e).lower():
                continue
            return "// У меня временные проблемы."
    return "// Лимит ключей исчерпан."

# ==========================
# Telegram Handlers
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("// Я Екатерина II. Готова помочь вам в нашей ролевой.")

async def send_long_message(message, text):
    limit = 3900  
    for i in range(0, len(text), limit):
        chunk = text[i:i + limit]
        formatted_chunk = f"<blockquote expandable>{chunk}</blockquote>"
        try:
            await message.reply_text(formatted_chunk, parse_mode="HTML")
        except Exception:
            try:
                await message.get_bot().send_message(chat_id=message.chat_id, text=formatted_chunk, parse_mode="HTML")
            except Exception as e:
                print("Ошибка отправки длинного сообщения:", e)

async def send_raw_long_message(message, text):
    limit = 4000  
    for i in range(0, len(text), limit):
        chunk = text[i:i + limit]
        try:
            await message.get_bot().send_message(chat_id=message.chat_id, text=chunk, parse_mode="HTML")
        except Exception as e:
            print("Ошибка отправки сырого длинного сообщения:", e)

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return
    
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            greeting = (
                "// *Плавным движением склоняет голову* "
                "Приветствую вас, господа! Я императрица Екатерина II. "
                "Благодарю за приглашение в ваш светский круг. Располагайтесь с достоинством, "
                "а все правила нашей ролевой вселенной Петербурга 1838 года вы найдете в закрепе."
            )
        else:
            name = member.first_name or "гость"
            greeting = (
                f"// Добро пожаловать в Петербург 1838 года, {name}! "
                f"Я императрица Екатерина II. Рада приветствовать вас в нашем высшем свете. "
                f"Создавайте анкету и присоединяйтесь к балу, а всю необходимую инструкцию ищите в закрепленном сообщении, милая."
            )
        await update.message.reply_text(greeting)

async def photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    caption = update.message.caption or ""
    is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id
    if not any(t in caption.lower() for t in TRIGGERS) and not is_reply_to_bot:
        return
    if not update.message.photo:
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image = Image.open(io.BytesIO(await file.download_as_bytearray())).convert("RGB")
    user_id = f"{update.effective_chat.id}:{update.effective_user.id}"
    username = update.effective_user.username
    
    active_char = ACTIVE_CHARACTER.get(user_id)
    if active_char:
        unlocked = check_character_achievements(user_id, active_char, "outfits_sent", 1)
        if unlocked:
            await update.message.reply_text(f"// ✨ За ваше утонченное чувство моды персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")

    answer = await ask_katya_image(image, caption, user_id, username)
    await update.message.reply_text(answer)

async def convert_character_sheet(text):
    prompt = """
Извлеки из текста анкету персонажа (XIX век) в формате строгого JSON:
{
"name": "", "age": "", "gender": "", "title": "", "estate": "", "nationality": "",
"family": "", "appearance": "", "personality": "", "history": "", "occupation": "",
"likes": "", "dislikes": "", "allergies": "", "phobias": "", "skills_hobbies": "", "connections": ""
}
Анкета:
""" + text
    try:
        response = await call_gemini_async(prompt)
        raw = get_gemini_text(response).replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        data["changes"] = {"permanent": [], "temporary": [], "achievements": [], "tragedies": [], "rumors": []}
        data["achievement_times"] = {}
        data["stats"] = {}
        for k in data:
            if k not in ["changes", "achievement_times", "stats"] and not data[k]:
                data[k] = "—"
        return data
    except Exception as e:
        print("Ошибка обработки анкеты:", e)
        return {"name": "Персонаж", "changes": {"permanent": [], "temporary": [], "achievements": [], "tragedies": [], "rumors": []}, "achievement_times": {}, "stats": {}}

def set_active_character_local(user_id, index):
    chars = get_characters(user_id)
    if 0 <= index < len(chars):
        ACTIVE_CHARACTER[user_id] = chars[index]
        check_character_achievements(user_id, ACTIVE_CHARACTER[user_id], "character_switches", 1)
        return True
    return False

def called_katya(text):
    if not text:
        return False
    return any(re.search(rf"\b{re.escape(t)}\b", text.lower()) for t in TRIGGERS)

def find_missing_character_fields(char):
    fields = {"age": "Возраст", "gender": "Пол", "title": "Титул/Сословие", "family": "Семья", "appearance": "Внешность", "occupation": "Работа"}
    return [name for key, name in fields.items() if not char.get(key) or char.get(key) in ["—", "-", "нет данных"]]

def update_character_from_text(character, text):
    fields = {
        "возраст": "age", "пол": "gender", "титул": "title", "сословие": "estate",
        "национальность": "nationality", "семья": "family", "внешность": "appearance",
        "характер": "personality", "работа": "occupation", "биография": "history",
        "любит": "likes", "не любит": "dislikes", "аллергии": "allergies", "фобии": "phobias",
        "навыки": "skills_hobbies", "связи": "connections"
    }
    for line in text.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.lower().strip(), v.strip()
            if k in fields and v:
                character[fields[k]] = v
    return character

async def messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    user_id = f"{update.effective_chat.id}:{update.effective_user.id}"
    sender_user_id = update.effective_user.id
    username = update.effective_user.username
    lower = text.lower().strip()
    chat_id = update.effective_chat.id
    is_private = update.effective_chat.type == "private"

    if username:
        USERNAME_CACHE["@" + username.lower()] = user_id

    if lower.startswith("/start"):
        return

    is_reply = update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id

    has_active_state = (
        user_id in WAITING_CHARACTER
        or user_id in WAITING_CHARACTER_UPDATE
        or context.user_data.get("letter_step")
        or context.user_data.get("group_letter_step")
        or context.user_data.get("event_step")
        or context.user_data.get("biz_step")
        or context.user_data.get("close_biz_step")
        or context.user_data.get("admin_sheet_step")
        or context.user_data.get("pending_status")
        or context.user_data.get("status_step")
        or context.user_data.get("edit_sheet_step")
        or context.user_data.get("waiting_status_type")
        or context.user_data.get("waiting_status_remove")
        or context.user_data.get("ad_step")
    )

    # ==========================
    # СЕКРЕТНЫЕ АДМИН-ДИРЕКТИВЫ ПОВЕДЕНИЯ (/отношение)
    # ==========================
    if lower.startswith("/отношение") or lower.startswith("/указание"):
        if sender_user_id not in [OWNER_ID, ROLE_OWNER_ID]:
            await update.message.reply_text("// Эта команда доступна только модераторам столицы, милая.")
            return
        parts = text.split(" ", 2)
        if len(parts) < 3:
            await update.message.reply_text("// Укажите кого и какое отношение установить! Пример: `/отношение @username Будь с ней мягче` или `/отношение Князь_Орлов Критикуй его камзолы`", parse_mode="Markdown")
            return
        target_key = parts[1].strip().lower()
        directive_text = parts[2].strip()

        ADMIN_DIRECTIVES[target_key] = directive_text
        save_directives()
        await update.message.reply_text(f"// Принято! Секретная директива поведения для <b>{target_key}</b> сохранена: «{directive_text}»", parse_mode="HTML")
        return

    if lower.startswith("/сбросить_отношение") or lower.startswith("/сбросить_указание"):
        if sender_user_id not in [OWNER_ID, ROLE_OWNER_ID]:
            await update.message.reply_text("// Команда доступна только модераторам столицы, милая.")
            return
        parts = text.split(" ", 1)
        if len(parts) < 2:
            await update.message.reply_text("// Укажите имя или username! Пример: `/сбросить_отношение @username`", parse_mode="Markdown")
            return
        target_key = parts[1].strip().lower()
        if target_key in ADMIN_DIRECTIVES:
            ADMIN_DIRECTIVES.pop(target_key)
            save_directives()
            await update.message.reply_text(f"// Секретная директива для <b>{target_key}</b> успешно отменена.", parse_mode="HTML")
        else:
            await update.message.reply_text("// Для этой цели не найдено активных секретных директив.")
        return

    if lower in ["/список_отношений", "/директивы"]:
        if sender_user_id not in [OWNER_ID, ROLE_OWNER_ID]:
            await update.message.reply_text("// Команда доступна только модераторам столицы, милая.")
            return
        if not ADMIN_DIRECTIVES:
            await update.message.reply_text("// Особых секретных директив пока нет.")
            return
        res = "<b>🔒 Секретные административные директивы поведения Кати:</b>\n\n"
        for k, v in ADMIN_DIRECTIVES.items():
            res += f"• <b>{k}:</b> {v}\n"
        await update.message.reply_text(f"<blockquote expandable>{res}</blockquote>", parse_mode="HTML")
        return

    # ==========================
    # ПРИНУДИТЕЛЬНОЕ УДАЛЕНИЕ АНКЕТ МОДЕРАТОРАМИ (/удалить_анкету)
    # ==========================
    if lower.startswith("/удалить_анкету") or lower.startswith("катя принудительно удали анкету"):
        if sender_user_id not in [OWNER_ID, ROLE_OWNER_ID, 727703718, 5714213307]:
            await update.message.reply_text("// Принудительное изъятие анкет доступно только модераторам столицы, милая.")
            return

        target_user_id_str = None
        if update.message.reply_to_message:
            target_user = update.message.reply_to_message.from_user
            target_user_id_str = f"{chat_id}:{target_user.id}"
        else:
            parts = text.split(" ", 1)
            if len(parts) > 1 and parts[1].strip():
                target_arg = parts[1].strip()
                if target_arg.startswith("@"):
                    uname_key = target_arg.lower()
                    target_user_id_str = USERNAME_CACHE.get(uname_key)
                else:
                    all_active = get_all_active_characters()
                    for tuid, tc in all_active:
                        if target_arg.lower() in tc.get("name", "").lower():
                            target_user_id_str = tuid
                            break

        if not target_user_id_str:
            await update.message.reply_text(
                "// Ответьте на сообщение пользователя этой командой или укажите его @username / имя персонажа!\n"
                "<i>Пример: Ответить на сообщение + `/удалить_анкету` или `/удалить_анкету @username`</i>",
                parse_mode="HTML"
            )
            return

        t_chars = get_characters(target_user_id_str)
        if not t_chars:
            await update.message.reply_text("// У данного пользователя не найдено анкет в реестре.")
            return

        parts_list = text.split(" ")
        char_idx_to_del = 0
        if len(parts_list) > 1 and parts_list[-1].isdigit():
            char_idx_to_del = int(parts_list[-1]) - 1

        if 0 <= char_idx_to_del < len(t_chars):
            deleted_char = t_chars[char_idx_to_del]
            remove_character(target_user_id_str, char_idx_to_del)
            
            if ACTIVE_CHARACTER.get(target_user_id_str, {}).get("name") == deleted_char.get("name"):
                ACTIVE_CHARACTER.pop(target_user_id_str, None)

            await update.message.reply_text(
                f"// <b>Анкета изъята!</b>\n"
                f"Персонаж <b>«{deleted_char.get('name', 'Без имени')}»</b> был официально удален из архивов столицы по распоряжению модерации.",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("// Неверный индекс персонажа для удаления.")
        return

    # ==========================
    # КОМАНДА /совет (ГЕНЕРАТОР НАСТАВЛЕНИЙ И ЭТИКЕТА)
    # ==========================
    if lower.startswith("/совет") or lower.startswith("катя совет") or lower.startswith("катя, совет"):
        user_query = ""
        if lower.startswith("/совет"):
            user_query = text[len("/совет"):].strip()
        elif lower.startswith("катя совет"):
            user_query = text[len("катя совет"):].strip(" ,")
        elif lower.startswith("катя, совет"):
            user_query = text[len("катя, совет"):].strip(" ,")

        if not user_query:
            quick_menu = (
                "// <b>💡 Наставник высшего света | По какому поводу требуется совет?</b>\n\n"
                "Пожалуйста, укажите ваш вопрос после команды! Пример: <code>/совет как отыграть дуэль на шпагах?</code>\n\n"
                "<b>Или выберите одну из популярных тем для вопроса:</b>\n"
                "1. ⚔️ <b>Дуэльный этикет</b> (пистолеты, шпаги, правила секундантов)\n"
                "2. 🃏 <b>Карточные игры</b> (Штосс, Бостон, Вист, правила и азарт)\n"
                "3. 💃 <b>Балы и салоны</b> (язык веера, мазурка, флирт)\n"
                "4. 💌 <b>Тайные интриги</b> (шифры, заговор, подкуп слуг)\n"
                "5. 🎭 <b>Отыгрыш НПС</b> (слуги, кучера, извозчики, городовые)\n"
                "6. 💰 <b>Векселя и заёмы</b> (ростовщики, долги, залоги)"
            )
            await update.message.reply_text(quick_menu, parse_mode="HTML")
            return

        chars = get_characters(user_id)
        active_char = ACTIVE_CHARACTER.get(user_id) or (chars[0] if chars else None)
        
        working_msg = await update.message.reply_text("// Открываю старинные светские трактаты и составляю наставление... 📜")
        advice_res = await generate_roleplay_advice(user_query, active_char, user_id, chat_id)
        
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
        except Exception:
            pass

        await send_long_html(update.message, advice_res)
        return

    # ==========================
    # МОДЕРАЦИЯ ЗАЯВОК В ГАЗЕТУ
    # ==========================
    if is_private and sender_user_id in [OWNER_ID, ROLE_OWNER_ID] and update.message.reply_to_message:
        replied_msg = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        match_id = re.search(r'ad_\d+_\d+', replied_msg)
        
        if match_id and (lower.startswith("/одобрить") or lower.startswith("/отклонить") or lower.startswith("/изменить")):
            ad_id = match_id.group(0)
            g_data = load_gazette_data()
            
            if ad_id not in g_data["pending_ads"]:
                await update.message.reply_text("// Эта заявка не найдена или уже была обработана.")
                return
                
            ad_item = g_data["pending_ads"].pop(ad_id)
            
            if lower.startswith("/одобрить"):
                g_data["approved_ads"].append({
                    "author_name": ad_item["author_name"],
                    "text": ad_item["text"]
                })
                save_gazette_data(g_data)
                await update.message.reply_text(f"// Заявка <code>{ad_id}</code> успешно <b>ОДОБРЕНА</b> и пойдет в ближайший номер!", parse_mode="HTML")
                
            elif lower.startswith("/отклонить"):
                save_gazette_data(g_data)
                await update.message.reply_text(f"// Заявка <code>{ad_id}</code> <b>ОТКЛОНЕНА</b> и сгорела в камине редакции.", parse_mode="HTML")
                
            elif lower.startswith("/изменить"):
                parts = text.split(" ", 1)
                if len(parts) < 2 or not parts[1].strip():
                    await update.message.reply_text("// Укажите отредактированный текст после команды! Пример: `/изменить Продаю шикарного скакуна.`", parse_mode="Markdown")
                    return
                edited_text = parts[1].strip()
                g_data["approved_ads"].append({
                    "author_name": ad_item["author_name"],
                    "text": edited_text
                })
                save_gazette_data(g_data)
                await update.message.reply_text(f"// Заявка <code>{ad_id}</code> отредактирована и <b>ОДОБРЕНА</b>!", parse_mode="HTML")
            return

    # ==========================
    # КОМАНДЫ И ОБРАБОТКА ПОДАЧИ ОБЪЯВЛЕНИЙ В ГАЗЕТУ (/редакция)
    # ==========================
    if lower == "/чат_газеты":
        if sender_user_id not in [OWNER_ID, ROLE_OWNER_ID]:
            await update.message.reply_text("// Эта команда доступна только модераторам столицы, милая.")
            return
        g_data = load_gazette_data()
        g_data["gazette_chat_id"] = chat_id
        save_gazette_data(g_data)
        await update.message.reply_text("// Официальный чат для публикации Имперской Газеты успешно привязан! 📰")
        return

    ad_step = context.user_data.get("ad_step")
    if ad_step and lower in ["отмена", "нет", "отменить", "стоп"]:
        context.user_data.pop("ad_step", None)
        context.user_data.pop("ad_text", None)
        await update.message.reply_text("// Подача объявления в газету отменена, милая.")
        return

    if ad_step == "select_author":
        try:
            clean_text = re.sub(r'[^\d]', '', text)
            if not clean_text:
                await update.message.reply_text("// Пожалуйста, напишите номер из списка цифрой.")
                return
            idx = int(clean_text) - 1
            chars = get_characters(user_id)
            anon_idx = len(chars)

            if idx == anon_idx:
                author_name = "Анонимный житель столицы"
            elif 0 <= idx < len(chars):
                author_name = chars[idx].get("name", "Житель столицы")
            else:
                await update.message.reply_text("// Такого номера нет в списке. Напишите верный номер или «отмена».")
                return

            ad_text = context.user_data.get("ad_text", "")
            uname = "@" + username if username else "—"
            ad_id = f"ad_{int(time.time())}_{random.randint(100, 999)}"

            g_data = load_gazette_data()
            g_data["pending_ads"][ad_id] = {
                "id": ad_id,
                "user_id": user_id,
                "author_name": author_name,
                "username": uname,
                "text": ad_text
            }
            save_gazette_data(g_data)

            context.user_data.pop("ad_step", None)
            context.user_data.pop("ad_text", None)

            await update.message.reply_text(f"// Ваше объявление от имени <b>{author_name}</b> передано в редакторскую коллегию Имперской Газеты! ✒️", parse_mode="HTML")

            notification = (
                f"📝 <b>Новая заявка в газету!</b>\n"
                f"<b>ID заявки:</b> <code>{ad_id}</code>\n"
                f"<b>Автор в газете:</b> {author_name}\n"
                f"<b>Отправитель (Telegram):</b> {uname}\n"
                f"<b>Текст:</b>\n<i>{ad_text}</i>\n\n"
                f"💡 <i>Ответьте на это сообщение (Reply) одной из команд:\n"
                f"• <code>/одобрить</code>\n"
                f"• <code>/отклонить</code>\n"
                f"• <code>/изменить [Новый текст]</code></i>"
            )

            for admin_id in [OWNER_ID, ROLE_OWNER_ID]:
                try:
                    await context.bot.send_message(chat_id=admin_id, text=notification, parse_mode="HTML")
                except Exception as e:
                    print(f"Не удалось отправить заявку админу {admin_id}: {e}")
            return
        except ValueError:
            await update.message.reply_text("// Пожалуйста, напишите номер цифрой.")
            return

    if lower.startswith("/редакция"):
        parts = text.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            await update.message.reply_text("// Напишите текст объявления после команды! Пример: `/редакция Продаю арабского скакуна.`", parse_mode="Markdown")
            return
            
        ad_text = parts[1].strip()
        chars = get_characters(user_id)
        
        context.user_data["ad_text"] = ad_text
        context.user_data["ad_step"] = "select_author"

        msg = "// От чьего имени подаем объявление в редакцию «Санкт-Петербургских Ведомостей»?\n\n"
        for i, c in enumerate(chars, 1):
            msg += f"{i}. {c.get('name', 'Без имени')} ({c.get('title', '—')})\n"
        
        anon_idx = len(chars) + 1
        msg += f"{anon_idx}. 🕵️ Анонимно (От тайного доброжелателя / анонимного жителя)\n"
        msg += "\n*(Напишите номер выбора или «отмена»)*"

        await update.message.reply_text(msg)
        return

    if lower in ["/выпуск_газеты", "катя выпуск газеты", "катя, выпуск газеты"]:
        if sender_user_id not in [OWNER_ID, ROLE_OWNER_ID]:
            await update.message.reply_text(
                "// *Императорски смотрит через лорнет* "
                "Полноте, милая! Верстать и печатать Главную Газету Империи позволено лишь Редакторской Коллегии. "
                "Вы же можете подавать свои объявления в следующий номер через команду /редакция."
            )
            return
        
        working_msg = await update.message.reply_text("// Печатные станки типографии пущены в ход, верстаем свежий номер... 📰")
        ok, msg_res = await publish_weekly_gazette(context.bot, target_chat_id=chat_id)
        
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
        except Exception:
            pass
            
        if not ok:
            await update.message.reply_text(f"// {msg_res}")
        return

    if lower.startswith("/удалить_газету") or lower.startswith("/удалить_выпуск"):
        if sender_user_id not in [OWNER_ID, ROLE_OWNER_ID]:
            await update.message.reply_text("// Изъять выпуск из архива могут только владельцы редакции, милая.")
            return
        parts = text.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            await update.message.reply_text("// Укажите номер выпуска из архива! Пример: `/удалить_газету 1`", parse_mode="Markdown")
            return
        try:
            clean_num = re.sub(r'[^\d]', '', parts[1])
            if not clean_num:
                await update.message.reply_text("// Пожалуйста, укажите номер выпуска цифрой.")
                return
            idx_to_del = int(clean_num) - 1
            g_data = load_gazette_data()
            archive = g_data.get("archive", [])
            if 0 <= idx_to_del < len(archive):
                archive[idx_to_del]["deleted"] = True
                archive[idx_to_del]["summary"] = "🔥 [Выпуск изъят из архива редакции и предан огню по распоряжению модерации]"
                save_gazette_data(g_data)
                await update.message.reply_text(f"// Выпуск №{idx_to_del + 1} успешно изъят из архива и сжёг в камине редакции! 🔥")
            else:
                await update.message.reply_text("// Выпуска с таким номером нет в архиве.")
        except ValueError:
            await update.message.reply_text("// Пожалуйста, укажите номер выпуска цифрой.")
        return

    if lower in ["/архив_газет", "катя архив газет", "катя, архив газет"]:
        g_data = load_gazette_data()
        archive = g_data.get("archive", [])
        
        if not archive:
            await update.message.reply_text("// В архивах Имперской Газеты пока нет прошлых выпусков, милая.")
            return

        header = "<b>📜 ИСТОРИЧЕСКИЙ АРХИВ «САНКТ-ПЕТЕРБУРГСКИХ ВЕДОМОСТЕЙ»:</b>\n\n"
        footer = "\n💡 <i>Модераторы могут изъять выпуск командой /удалить_газету [Номер].</i>"

        lines = []
        for idx, issue in enumerate(archive, 1):
            if issue.get("deleted"):
                lines.append(f"• <b>Выпуск №{idx} ({issue.get('date', '—')}):</b> <i>🔥 [Выпуск изъят из архива редакции и предан огню]</i>")
            else:
                lines.append(f"• <b>Выпуск №{idx} ({issue.get('date', '—')}):</b> {issue.get('summary', '')}")

        current_chunk = header
        for line in lines:
            if len(current_chunk) + len(line) + 2 > 3500:
                await update.message.reply_text(f"<blockquote expandable>{current_chunk}</blockquote>", parse_mode="HTML")
                current_chunk = "<b>📜 ИСТОРИЧЕСКИЙ АРХИВ (Продолжение):</b>\n\n" + line + "\n"
            else:
                current_chunk += line + "\n"

        current_chunk += footer
        await update.message.reply_text(f"<blockquote expandable>{current_chunk}</blockquote>", parse_mode="HTML")
        return

    valid_commands = [
        "/дать_достижение", "/достижение", "/реестр", "/заведение", 
        "/закрыть_бизнес", "/создать_бизнес", "/дуэль", "/статистика", 
        "/письмо", "/групповое_письмо", "/гписьмо", "/clear", "/правила",
        "/добавить_статус", "/удалить_статус", "/изменить_анкету",
        "/чат_газеты", "/редакция", "/одобрить", "/отклонить", "/изменить",
        "/выпуск_газеты", "/архив_газет", "/удалить_газету", "/удалить_выпуск",
        "/отношение", "/указание", "/сбросить_отношение", "/сбросить_указание", "/список_отношений", "/директивы",
        "/совет", "/удалить_анкету"
    ]
    
    is_command = any(lower.startswith(cmd) for cmd in valid_commands)
    is_addressed_to_katya = called_katya(text)
    
    if not is_addressed_to_katya and not is_reply and not has_active_state and not is_command and not is_private:
        return

    creation_triggers = ["/start", "новая анкета", "создать анкету"]
    is_creation_request = any(trigger in lower for trigger in creation_triggers)

    if not is_creation_request and user_id not in WAITING_CHARACTER:
        chars = get_characters(user_id)
        if not chars:
            if (is_addressed_to_katya or is_reply or is_private) and sender_user_id not in [727703718, 5714213307]:
                await update.message.reply_text(
                    f"// *Императорски смотрит на вас через лорнет* "
                    f"Полноте, милая! Прежде чем вести со мной беседы или "
                    f"участвовать в светской жизни Петербурга, необходимо составить анкету "
                    f"и представить себя ко двору. Создайте персонажа, чтобы я знала, как с вами общаться!"
                )
                return 
        else:
            if user_id not in ACTIVE_CHARACTER or not ACTIVE_CHARACTER[user_id]:
                ACTIVE_CHARACTER[user_id] = chars[0]
            
    if sender_user_id in [727703718, 5714213307] and (lower.startswith("/дать_достижение") or lower.startswith("/достижение")):
        if not update.message.reply_to_message:
            await update.message.reply_text("// Ответьте этой командой на сообщение игрока, которому хотите выдать достижение, милая.")
            return
        parts = text.split(" ", 1)
        if len(parts) < 2:
            await update.message.reply_text("// Укажите название достижения после команды.")
            return
        ach_name = parts[1].strip()
        target_user = update.message.reply_to_message.from_user
        target_user_id_str = f"{chat_id}:{target_user.id}"
        t_chars = get_characters(target_user_id_str)
        if not t_chars:
            await update.message.reply_text("// У этого господина/дамы нет персонажей.")
            return
        t_active = ACTIVE_CHARACTER.get(target_user_id_str) or t_chars[0]
        if award_achievement_to_char(t_active, ach_name):
            all_chars = get_characters(target_user_id_str)
            for i, c in enumerate(all_chars):
                if c and t_active and c.get("name") == t_active.get("name"):
                    all_chars[i] = t_active
                    break
            save_characters(target_user_id_str, all_chars)
            await update.message.reply_text(f"// Успешно пожаловано достижение «{ach_name}» персонажу {t_active.get('name')}!", parse_mode="HTML")
        else:
            await update.message.reply_text(f"// У персонажа уже есть это достижение или произошла ошибка.")
        return

    admin_sheet_step = context.user_data.get("admin_sheet_step")
    if admin_sheet_step and sender_user_id in [727703718, 5714213307]:
        if lower in ["отмена", "нет", "отменить", "стоп"]:
            context.user_data.pop("admin_sheet_step", None)
            context.user_data.pop("admin_sheet_chars", None)
            await update.message.reply_text("// Как прикажете, отменяю.")
            return
        try:
            clean_text = re.sub(r'[^\d]', '', text)
            idx = int(clean_text) - 1
            chars = context.user_data.get("admin_sheet_chars", [])
            if 0 <= idx < len(chars):
                chosen_char = chars[idx]
                context.user_data.pop("admin_sheet_step", None)
                context.user_data.pop("admin_sheet_chars", None)
                await send_long_html(update.message, format_character_sheet(chosen_char))
            else:
                await update.message.reply_text("// Нет такого номера в списке. Напишите верный или «отмена».")
        except ValueError:
            await update.message.reply_text("// Пожалуйста, укажите цифру.")
        return

    if "покажи его анкету" in lower or "покажи анкету" in lower:
        if sender_user_id not in [727703718, 5714213307]:
            insults = [
                "// *Крутит у виска веером* Какая дерзость! Эта информация строго конфиденциальна.",
                "// *Выразительно крутит у виска* Полноте, голунчик, вам не положено знать чужие тайны.",
                "// *Императорски крутит у виска* Оставьте свои праздные любопытства, сие есть государственная тайна!",
                "// *С легким презрением крутит у виска* С чего вы решили, что вам откроют архивы высшего света?"
            ]
            await update.message.reply_text(random.choice(insults))
            return
        
        if not update.message.reply_to_message:
            await update.message.reply_text("// Ответьте этой командой на сообщение того пользователя, чью анкету желаете проверить.")
            return
            
        target_user = update.message.reply_to_message.from_user
        target_user_id_str = f"{chat_id}:{target_user.id}"
        chars = get_characters(target_user_id_str)
        
        if not chars:
            await update.message.reply_text("// У этого человека нет ни одной зарегистрированной анкеты, моя дорогая.")
            return
            
        if len(chars) == 1:
            await send_long_html(update.message, format_character_sheet(chars[0]))
            return
            
        msg = f"// У данного господина/дамы несколько персонажей ({len(chars)}):\n\n"
        for i, c in enumerate(chars, 1):
            msg += f"{i}. {c.get('name', 'Без имени')}\n"
        msg += "\n*(Напишите номер анкеты, которую желаете показать, или «отмена»)*"
        
        context.user_data["admin_sheet_step"] = True
        context.user_data["admin_sheet_chars"] = chars
        await update.message.reply_text(msg)
        return

    # ==========================
    # РЕЕСТР И ПРОСМОТР БИЗНЕСОВ
    # ==========================

    if lower in ["/реестр", "катя реестр", "катя, реестр", "список бизнесов", "катя список бизнесов"]:
        if not CITY_BUSINESSES:
            await update.message.reply_text("// В реестре столицы пока нет ни одного зарегистрированного заведения, милая.")
            return

        res = "<b>🏢 Официальный реестр заведений Санкт-Петербурга (1838 год):</b>\n\n"
        for idx, b in enumerate(CITY_BUSINESSES, 1):
            res += f"{idx}. <b>«{b.get('name')}»</b> — {b.get('type')}\n"
            res += f"   <i>Владелец:</i> {b.get('owner_name')}\n"
            res += f"   <i>Статус:</i> {b.get('status')}\n\n"
        res += "💡 <i>Используйте команду «/заведение [Номер или Название]», чтобы изучить ассортимент, меню и описание.</i>"
        await update.message.reply_text(f"<blockquote expandable>{res}</blockquote>", parse_mode="HTML")
        return

    if lower.startswith("/заведение") or lower.startswith("катя заведение") or lower.startswith("катя, заведение"):
        parts = text.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            await update.message.reply_text("// Укажите номер из реестра или название заведения, милая! Пример: `/заведение 1`", parse_mode="Markdown")
            return

        query = parts[1].strip().lower()
        found_b = None

        if query.isdigit():
            idx = int(query) - 1
            if 0 <= idx < len(CITY_BUSINESSES):
                found_b = CITY_BUSINESSES[idx]
        else:
            for b in CITY_BUSINESSES:
                if query in b.get("name", "").lower():
                    found_b = b
                    break

        if not found_b:
            await update.message.reply_text("// Заведение с таким номером или названием не найдено в реестре столицы.")
            return

        card = (
            f"<b>🏛️ Карточка заведения: «{found_b.get('name')}»</b>\n"
            f"ㅤㅤ∴ ════ ∴ ✦ ∴ ════ ∴\n"
            f"<b>Тип:</b> {found_b.get('type')}\n"
            f"<b>Владелец:</b> {found_b.get('owner_name')}\n"
            f"<b>Статус:</b> {found_b.get('status')}\n"
        )
        if found_b.get("description") and found_b.get("description") != "—":
            card += f"<b>Экстерьер и детали:</b> {found_b.get('description')}\n"
        card += f"│────────✤────────│\n\n<b>📜 Меню / Ассортимент:</b>\n{found_b.get('assortment', '—')}"

        await update.message.reply_text(f"<blockquote expandable>{card}</blockquote>", parse_mode="HTML")
        return

    close_biz_step = context.user_data.get("close_biz_step")
    if close_biz_step and lower in ["отмена", "нет", "отменить", "стоп"]:
        context.user_data.pop("close_biz_step", None)
        context.user_data.pop("close_biz_list", None)
        await update.message.reply_text("// Закрытие заведения отменено, милая.")
        return

    if text.strip().lower().startswith("/закрыть_бизнес") or lower in ["катя закрыть бизнес", "катя, закрыть бизнес"]:
        user_businesses = [b for b in CITY_BUSINESSES if b.get("owner_user_id") == user_id]
        if not user_businesses:
            chars = get_characters(user_id)
            char_names = [c.get("name") for c in chars]
            user_businesses = [b for b in CITY_BUSINESSES if b.get("owner_name") in char_names]

        if not user_businesses:
            await update.message.reply_text("// У вас нет зарегистрированных заведений в столице, которые можно было бы закрыть, милая.")
            return

        if len(user_businesses) == 1:
            b_to_close = user_businesses[0]
            CITY_BUSINESSES.remove(b_to_close)
            save_businesses()
            await update.message.reply_text(f"// Заведение <b>«{b_to_close.get('name')}»</b> («{b_to_close.get('type')}») было официально закрыто и исключено из реестра Санкт-Петербурга, милая.", parse_mode="HTML")
            return

        msg = f"// Какое именно из ваших заведений вы желаете закрыть и предать забвению?\n\n"
        for i, b in enumerate(user_businesses, 1):
            msg += f"{i}. <b>«{b.get('name')}»</b> ({b.get('type')})\n"
        msg += "\n*(Напишите номер заведения или «отмена»)*"

        context.user_data["close_biz_step"] = "select"
        context.user_data["close_biz_list"] = user_businesses
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    if close_biz_step == "select":
        try:
            clean_text = re.sub(r'[^\d]', '', text)
            idx = int(clean_text) - 1
            user_businesses = context.user_data.get("close_biz_list", [])
            if 0 <= idx < len(user_businesses):
                b_to_close = user_businesses[idx]
                CITY_BUSINESSES.remove(b_to_close)
                save_businesses()
                
                context.user_data.pop("close_biz_step", None)
                context.user_data.pop("close_biz_list", None)
                
                await update.message.reply_text(f"// Заведение <b>«{b_to_close.get('name')}»</b> («{b_to_close.get('type')}») было официально закрыто и исключено из реестра Санкт-Петербурга, милая.", parse_mode="HTML")
            else:
                await update.message.reply_text("// Нет такого номера в списке. Напишите верный номер или «отмена».")
        except ValueError:
            await update.message.reply_text("// Пожалуйста, напишите цифру номера заведения.")
        return

    biz_step = context.user_data.get("biz_step")

    if biz_step and lower in ["отмена", "нет", "отменить", "стоп"]:
        context.user_data.pop("biz_step", None)
        context.user_data.pop("biz_owner", None)
        context.user_data.pop("biz_name", None)
        context.user_data.pop("biz_type", None)
        context.user_data.pop("biz_status", None)
        await update.message.reply_text("// Создание заведения отменено, милая.")
        return

    if text.strip().lower().startswith("/создать_бизнес") or lower in ["катя создать бизнес", "катя, создать бизнес"]:
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей, милая. Создайте анкету сначала.")
            return

        active_char = ACTIVE_CHARACTER.get(user_id) or chars[0]
        dear = get_dear(active_char)

        msg = f"// Кто из ваших персонажей станет владельцем заведения, {dear}?\n\n"
        for i, c in enumerate(chars, 1):
            msg += f"{i}. {c.get('name', 'Без имени')}\n"
        msg += "\n*(Напишите номер персонажа или «отмена»)*"

        context.user_data["biz_step"] = "owner"
        await update.message.reply_text(msg)
        return

    if biz_step == "owner":
        try:
            clean_text = re.sub(r'[^\d]', '', text)
            idx = int(clean_text) - 1
            chars = get_characters(user_id)
            if 0 <= idx < len(chars):
                chosen_char = chars[idx]
                context.user_data["biz_owner"] = chosen_char
                context.user_data["biz_step"] = "info"
                await update.message.reply_text(
                    f"// Прекрасно! Владельцем выбран(а) {chosen_char.get('name')}.\n\n"
                    f"Укажите через запятую:\n"
                    f"<b>1. Название заведения</b>\n"
                    f"<b>2. Тип заведения</b> (трактир, лавка, мастерская, салон и т.д.)\n"
                    f"<b>3. Статус</b> (старинное родовое гнездо или свежеоткрытое предприятие)\n\n"
                    f"<i>Пример: Трактир «Веселый Гусаръ», Трактир, Свежеоткрытое предприятие</i>",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text("// Нет такого номера в списке. Укажите верный или «отмена».")
        except ValueError:
            await update.message.reply_text("// Напишите номер персонажа цифрой.")
        return

    if biz_step == "info":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 3:
            await update.message.reply_text("// Укажите все 3 параметра через запятую: Название, Тип, Статус! Или напишите «отмена».")
            return

        context.user_data["biz_name"] = parts[0]
        context.user_data["biz_type"] = parts[1]
        context.user_data["biz_status"] = parts[2]
        context.user_data["biz_step"] = "description"

        mily = get_mily(context.user_data.get("biz_owner"))
        await update.message.reply_text(
            f"// Сколько этажей занимает ваше заведение, {mily}? Как оно выглядит изнутри иснаружи, чтобы петербуржцы узнавали его издалека?\n\n"
            f"*(Напишите описание экстерьера/интерьера или «Пропустить» / «На усмотрение Кати»)*"
        )
        return

    if biz_step == "description":
        desc = text.strip()
        if desc.lower() in ["пропустить", "на усмотрение кати", "нет"]:
            desc = "—"

        context.user_data["biz_description"] = desc
        context.user_data["biz_step"] = "generating_assortment"

        owner_char = context.user_data["biz_owner"]
        b_name = context.user_data["biz_name"]
        b_type = context.user_data["biz_type"]
        b_status = context.user_data["biz_status"]

        mily = get_mily(owner_char)
        working_msg = await update.message.reply_text(f"// Составляю реестровую запись и подбираю изысканный ассортимент для «{b_name}», {mily}... ✒️")

        assortment = await generate_business_assortment(b_name, b_type, b_status, owner_char.get("name"))

        new_biz = {
            "name": b_name,
            "type": b_type,
            "status": b_status,
            "description": desc,
            "assortment": assortment,
            "owner_name": owner_char.get("name"),
            "owner_user_id": user_id
        }

        CITY_BUSINESSES.append(new_biz)
        save_businesses()

        unlocked = check_character_achievements(user_id, owner_char, "businesses_created", 1)

        context.user_data.pop("biz_step", None)
        context.user_data.pop("biz_owner", None)
        context.user_data.pop("biz_name", None)
        context.user_data.pop("biz_type", None)
        context.user_data.pop("biz_status", None)
        context.user_data.pop("biz_description", None)

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
        except Exception:
            pass

        card = (
            f"<b>🎉 Новое заведение успешно внесено в реестр Петербурга!</b>\n\n"
            f"<b>🏛️ «{b_name}»</b>\n"
            f"<b>Тип:</b> {b_type}\n"
            f"<b>Владелец:</b> {owner_char.get('name')}\n"
            f"<b>Статус:</b> {b_status}\n"
            f"<b>Детали:</b> {desc}\n\n"
            f"<b>📜 Базовый ассортимент / Меню:</b>\n"
            f"{assortment}"
        )

        await update.message.reply_text(f"<blockquote expandable>{card}</blockquote>", parse_mode="HTML")
        if unlocked:
            await update.message.reply_text(f"// ✨ Поздравляю! Ваш персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        return

    # ==========================
    # РУЧНЫЕ ДУЭЛИ И СТАТИСТИКА
    # ==========================

    if lower.startswith("катя дуэль") or lower.startswith("/дуэль"):
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей, милая. Создайте анкету сначала.")
            return
            
        match = re.search(r'@(\w+)', text)
        if not match:
            await update.message.reply_text("// Милая, вы забыли указать секундантам противника! Обязательно напишите его @username. Пример: `катя дуэль @username`", parse_mode="Markdown")
            return
            
        opp_uname = "@" + match.group(1).lower()
        my_uname = "@" + update.effective_user.username.lower() if update.effective_user.username else ""
        
        if opp_uname == my_uname and my_uname != "":
            await update.message.reply_text("// Вызов самого себя на дуэль — признак дурного тона или помешательства, милая.")
            return
            
        active_char = ACTIVE_CHARACTER.get(user_id) or chars[0]
        mily = get_mily(active_char)
        dear = get_dear(active_char)

        for d_id, d_data in list(ACTIVE_DUELS.items()):
            if d_id == user_id:
                await update.message.reply_text(f"// {dear}, у вашего персонажа уже назначена дуэль! Завершите ее («катя я выиграл дуэль») либо отмените («катя дуэль отменена»).")
                return
            if (d_data["p2_uname"] == opp_uname or d_data["p2_uname"] == my_uname) and d_data["p2_uname"] != "":
                 await update.message.reply_text(f"// Секунданты докладывают, что один из вас уже стоит на барьере в другой дуэли. Дождитесь её окончания.")
                 return

        ACTIVE_DUELS[user_id] = {
            "p1_id": user_id,
            "p1_name": active_char.get("name", "Безымянный"),
            "p2_uname": opp_uname,
            "start_time": time.time()
        }
        await update.message.reply_text(f"// Секунданты разошлись по позициям на Черной речке против <b>{opp_uname}</b>, {mily}. Дуэль началась! Любой из вас двоих может сообщить об исходе («катя я выиграл дуэль» / «катя я проиграл дуэль») или отменить вызов («катя дуэль отменена»). ⚔️", parse_mode="HTML")
        return

    if lower in ["катя дуэль отменена", "катя, дуэль отменена", "катя я выйграл дуэль", "катя я выиграл дуэль", "катя, я выйграл дуэль", "катя, я выиграл дуэль", "катя я проиграл дуэль", "катя, я проиграл дуэль"]:
        
        my_uname = "@" + update.effective_user.username.lower() if update.effective_user.username else ""
        duel_key = None
        is_initiator = False

        if user_id in ACTIVE_DUELS:
            duel_key = user_id
            is_initiator = True
        else:
            for k, v in ACTIVE_DUELS.items():
                if v["p2_uname"] == my_uname and my_uname != "":
                    duel_key = k
                    is_initiator = False
                    break

        if not duel_key:
            await update.message.reply_text("// Полноте, у вас сейчас нет активной дуэли, милая.")
            return
            
        d = ACTIVE_DUELS.pop(duel_key)
        p1_id = d["p1_id"]
        p1_name = d["p1_name"]
        p2_uname = d["p2_uname"]
        p2_id = USERNAME_CACHE.get(p2_uname)
        
        duration = get_status_duration(d["start_time"])
        end_time_str = datetime.datetime.now().strftime("%H:%M")
        
        p2_name = "Неизвестный противник"
        if p2_id:
            p2_chars = get_characters(p2_id)
            if p2_chars:
                p2_active = ACTIVE_CHARACTER.get(p2_id) or p2_chars[0]
                p2_name = p2_active.get("name", p2_uname)
        else:
            p2_name = p2_uname
            
        if "отменена" in lower:
            apply_duel_result(p1_id, "cancel", p2_name, duration, end_time_str)
            if p2_id:
                apply_duel_result(p2_id, "cancel", p1_name, duration, end_time_str)
            await update.message.reply_text("// Дуэль благополучно отменена по обоюдному согласию, секунданты спрятали пистолеты в кобуру. Честь сохранена для обоих.")
            return
            
        if "выиграл" in lower or "выйграл" in lower:
            if is_initiator:
                p1_res, p2_res = "win", "loss"
                winner_name, loser_name = p1_name, p2_name
            else:
                p1_res, p2_res = "loss", "win"
                winner_name, loser_name = p2_name, p1_name
        else:
            if is_initiator:
                p1_res, p2_res = "loss", "win"
                winner_name, loser_name = p2_name, p1_name
            else:
                p1_res, p2_res = "win", "loss"
                winner_name, loser_name = p1_name, p2_name

        ach1 = apply_duel_result(p1_id, p1_res, p2_name, duration, end_time_str)
        ach2 = []
        if p2_id:
            ach2 = apply_duel_result(p2_id, p2_res, p1_name, duration, end_time_str)
            
        msg = f"// ⚔️ <b>Дуэль завершена!</b>\nТриумфальная победа достается: <b>{winner_name}</b>!\n<b>{loser_name}</b> получает ранение и горечь поражения. Петербургский свет уже гудит об этом!"
        await update.message.reply_text(msg, parse_mode="HTML")
        
        if ach1 and is_initiator:
            await update.message.reply_text(f"// ✨ За дуэльные интриги вы получаете достижение: <b>{', '.join(ach1)}</b>!", parse_mode="HTML")
        elif ach2 and not is_initiator:
            await update.message.reply_text(f"// ✨ За дуэльные интриги вы получаете достижение: <b>{', '.join(ach2)}</b>!", parse_mode="HTML")
        return

    # ==========================
    # РУЧНОЕ УПРАВЛЕНИЕ СТАТУСАМИ И СЛУХАМИ
    # ==========================
    status_step = context.user_data.get("status_step")
    if status_step and lower in ["отмена", "нет", "отменить", "стоп"]:
        context.user_data.pop("status_step", None)
        await update.message.reply_text("// Как скажешь, милая. Отменяю изменение статусов.")
        return

    if status_step == "choose_type":
        choice = text.strip()
        types_map = {"1": "temporary", "2": "permanent", "3": "rumors", "4": "tragedies"}
        if choice in types_map:
            st_type = types_map[choice]
            context.user_data.pop("status_step", None)
            context.user_data["waiting_status_type"] = st_type
            
            names_map = {
                "temporary": "временный статус",
                "permanent": "постоянный статус",
                "rumors": "слух",
                "tragedies": "трагедию"
            }
            await update.message.reply_text(f"// Напишите текст, который нужно записать как {names_map[st_type]}.")
        else:
            await update.message.reply_text("// Пожалуйста, напишите цифру от 1 до 4 или «отмена».")
        return

    if re.search(r"катя,?\s+добавь\s+(временный\s+статус|постоянный\s+статус|слух|трагедию)", lower):
        if "временный" in lower:
            st_type = "temporary"
            prompt_name = "временный статус"
        elif "постоянный" in lower:
            st_type = "permanent"
            prompt_name = "постоянный статус"
        elif "слух" in lower:
            st_type = "rumors"
            prompt_name = "слух"
        else:
            st_type = "tragedies"
            prompt_name = "трагедию"
            
        context.user_data["waiting_status_type"] = st_type
        await update.message.reply_text(f"// Какой {prompt_name} записать в вашу анкету, милая? Напишите его текст.")
        return

    if re.search(r"катя,?\s+добавь\s+статус", lower) or lower == "/добавить_статус":
        msg = (
            "// Какой тип статуса вы желаете добавить в анкету, милая?\n\n"
            "1. ⏳ Временный статус\n"
            "2. 📌 Постоянный статус\n"
            "3. 🗣️ Слух в обществе\n"
            "4. 💔 Трагедия\n\n"
            "*(Напишите цифру от 1 до 4 или «отмена»)*"
        )
        context.user_data["status_step"] = "choose_type"
        await update.message.reply_text(msg)
        return

    if re.search(r"катя,?\s+удали\s+статус", lower) or lower == "/удалить_статус":
        context.user_data["waiting_status_remove"] = True
        await update.message.reply_text("// Укажите точное название статуса, слуха или трагедии, который необходимо убрать из анкеты.")
        return

    # ==========================
    # РЕДАКТИРОВАНИЕ ОСНОВНОЙ АНКЕТЫ ИЛИ ИЗМЕНЕНИЕ СВЯЗЕЙ
    # ==========================
    edit_sheet_step = context.user_data.get("edit_sheet_step")
    if edit_sheet_step:
        if lower in ["отмена", "нет", "отменить", "стоп"]:
            context.user_data.pop("edit_sheet_step", None)
            await update.message.reply_text("// Изменение анкеты отменено, милая.")
            return
            
        active_char = ACTIVE_CHARACTER.get(user_id)
        if not active_char:
            chars = get_characters(user_id)
            active_char = chars[0] if chars else None
            
        if not active_char:
            context.user_data.pop("edit_sheet_step", None)
            await update.message.reply_text("// Ошибка: активный персонаж не найден.")
            return
            
        mily = get_mily(active_char)
        working_msg = await update.message.reply_text(f"// Вношу исправления в архивы двора для {active_char.get('name')}, {mily}... ✒️")
        
        updated_char = await edit_character_sheet_with_ai(active_char, text)
        
        all_chars = get_characters(user_id)
        for i, c in enumerate(all_chars):
            if c.get("name") == active_char.get("name"):
                all_chars[i] = updated_char
                break
        save_characters(user_id, all_chars)
        ACTIVE_CHARACTER[user_id] = updated_char
        
        context.user_data.pop("edit_sheet_step", None)
        
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
        except Exception:
            pass
            
        sheet_text = format_character_sheet(updated_char)
        await send_long_html(update.message, sheet_text)
        return

    if re.search(r"катя,?\s+измени\s+анкету", lower) or lower == "/изменить_анкету":
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей. Создайте анкету сначала.")
            return
            
        active_char = ACTIVE_CHARACTER.get(user_id) or chars[0]
        mily = get_mily(active_char)
        
        msg = (
            f"// Какое поле вашей анкеты вы желаете изменить или дополнить, {mily}?\n\n"
            f"Вы можете написать изменения простым языком или указать нужные поля списком в формате <b>Поле: Новое значение</b>.\n\n"
            f"<b>Доступные поля для изменений:</b>\n"
            f"• <b>Имя, Возраст, Пол, Семья, Работа</b>\n"
            f"• <b>Титул / Сословие, Национальность</b>\n"
            f"• <b>Внешность, Характер, Биография</b>\n"
            f"• <b>Любит, Не любит, Аллергии, Фобии</b>\n"
            f"• <b>Навыки, Связи</b>\n\n"
            f"<i>Пример:\n"
            f"Связи: Граф Безбородко (тайные враги)\n"
            f"Любит: Верховая езда, горький шоколад\n"
            f"Фобии: Гроза, замкнутые пространства</i>\n\n"
            f"*(Напишите изменения одним сообщением или отправьте «отмена»)*"
        )
        context.user_data["edit_sheet_step"] = True
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    # ==========================
    # ПРОСМОТР СВОЕЙ АНКЕТЫ
    # ==========================
    if any(phrase in lower for phrase in ["моя анкета", "текущий персонаж", "покажи мою анкету"]) or lower in ["катя анкета", "катя, анкета", "анкета"]:
        char = ACTIVE_CHARACTER.get(user_id)
        if not char:
            chars = get_characters(user_id)
            if chars:
                ACTIVE_CHARACTER[user_id] = chars[0]
                char = chars[0]
            else:
                await update.message.reply_text("// У вас пока нет сохранённых персонажей. Создайте анкету командой 'катя новая анкета'.")
                return
        
        unlocked = check_character_achievements(user_id, char, "rumors_check", 1)
        sheet_res = format_character_sheet(char)
        await send_long_html(update.message, sheet_res)
        if unlocked:
            await update.message.reply_text(f"// ✨ За громкие сплетни персонаж получает достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
            
        all_chars = get_characters(user_id)
        for i, c in enumerate(all_chars):
            if c.get("name") == char.get("name"):
                all_chars[i] = char
                break
        save_characters(user_id, all_chars)
        return

    if text.strip().lower().startswith("/статистика") or lower.startswith("катя, статистика") or lower.startswith("катя статистика"):
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей, милая. Создайте анкету сначала.")
            return

        active_char = ACTIVE_CHARACTER.get(user_id) or chars[0]
        
        stats = active_char.get("stats", {})
        duels_count = stats.get("duels_conflicts", 0)
        duels_won = stats.get("duels_won", 0)
        duels_lost = stats.get("duels_lost", 0)
        duels_cancelled = stats.get("duels_cancelled", 0)
        duels_history = stats.get("duels_history", [])

        sheet_text = (
            f"<b>⚔️ Дуэльная статистика персонажа:</b> {active_char.get('name', 'Без имени')}\n"
            f"ㅤㅤ∴ ════ ∴ ✦ ∴ ════ ∴\n"
            f"<b>Всего завершенных дуэлей:</b> {duels_count}\n"
            f"<b>Побед:</b> {duels_won} | <b>Поражений:</b> {duels_lost} | <b>Отмен:</b> {duels_cancelled}\n\n"
            f"<b>📜 История дуэлей:</b>\n"
        )

        if duels_history:
            for idx, dh in enumerate(duels_history, 1):
                sheet_text += f"{idx}. С кем: <b>{dh.get('opponent')}</b> | Исход: <b>{dh.get('result')}</b> | Длилась: {dh.get('duration')} (окончена в {dh.get('ended_at')})\n"
        else:
            sheet_text += "— (Дуэлей еще не проводилось)\n"

        achievements = active_char.get("changes", {}).get("achievements", [])
        duel_achievements = [a for a in achievements if "дуэль" in a.lower() or "интриг" in a.lower()]
        if duel_achievements:
            sheet_text += f"\n<b>Достижения:</b> {', '.join(duel_achievements)}\n"
            
        gender = active_char.get("gender", "").lower()
        is_female = any(w in gender for w in ["жен", "ж", "девуш", "дам", "девоч"])
        if is_female and duels_count > 0:
            sheet_text += "\n\n<i>// *Катя прижимает кружевной платок к груди* Какой кошмар, дорогая! Дамы с пистолетами на барьере... Куда катится этот свет? В мое время мы стрелялись разве что взглядами!</i>\n"
        
        sheet_text += "│────────✤────────│"
        await send_long_html(update.message, f"<blockquote expandable>{sheet_text}</blockquote>")
        return

    # ==========================
    # Одиночные письма
    # ==========================
    letter_step = context.user_data.get("letter_step")
    
    if letter_step and lower in ["отмена", "нет", "отменить", "стоп"]:
        prompt_id = context.user_data.pop("letter_prompt_msg_id", None)
        if prompt_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        context.user_data.pop("letter_step", None)
        context.user_data.pop("letter_char_idx", None)
        context.user_data.pop("letter_query", None)
        await update.message.reply_text("// Как скажешь, милая. Отменяю составление письма.")
        return

    if letter_step == "character":
        try:
            clean_text = re.sub(r'[^\d]', '', text)
            if not clean_text:
                await update.message.reply_text("// Пожалуйста, напишите номер из списка цифрой.")
                return
            idx = int(clean_text) - 1
            chars = get_characters(user_id)
            npc_option_num = len(chars)

            if idx == npc_option_num:
                prompt_msg_id = context.user_data.get("letter_prompt_msg_id")
                try:
                    if prompt_msg_id: await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
                    await update.message.delete()
                except Exception:
                    pass
                    
                context.user_data["letter_step"] = "npc_details"
                msg_obj = await context.bot.send_message(
                    chat_id=chat_id, 
                    text="// 🎭 От какого именно НПС пишем письмо?\nНапишите его имя, титул или краткое описание."
                )
                context.user_data["letter_prompt_msg_id"] = msg_obj.message_id
                return

            elif 0 <= idx < len(chars):
                chosen_char = chars[idx]
                letter_query = context.user_data.get("letter_query", "")
                prompt_msg_id = context.user_data.get("letter_prompt_msg_id")
                user_msg_id = update.message.message_id
                
                context.user_data.pop("letter_step", None)
                context.user_data.pop("letter_char_idx", None)
                context.user_data.pop("letter_query", None)
                context.user_data.pop("letter_prompt_msg_id", None)
                
                mily = get_mily(chosen_char)
                working_msg = await update.message.reply_text(f"// Перо скрипит по бумаге, {mily}, составляю послание от лица вашего персонажа... ✍️")
                working_msg_id = working_msg.message_id
                
                answer = await rewrite_letter_command_with_char(letter_query, chosen_char)
                unlocked = check_character_achievements(user_id, chosen_char, "letters_written", 1)
                
                for m_id in [prompt_msg_id, working_msg_id, user_msg_id]:
                    if m_id:
                        try:
                            await context.bot.delete_message(chat_id=chat_id, message_id=m_id)
                        except Exception:
                            pass

                if not answer or answer.startswith("// Ох"):
                    await context.bot.send_message(chat_id=chat_id, text=f"// Простите, {get_dear(chosen_char)}, чернильница опрокинулась, и письмо не сошлось строчками.")
                else:
                    await context.bot.send_message(chat_id=chat_id, text=answer, parse_mode="HTML")
                    if unlocked:
                        await context.bot.send_message(chat_id=chat_id, text=f"// ✨ За ваши труды персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
            else:
                await update.message.reply_text("// Такого номера нет в списке. Напиши верный номер или «отмена».")
        except ValueError:
            await update.message.reply_text("// Пожалуйста, напиши просто цифру номера персонажа.")
        return

    if letter_step == "npc_details":
        npc_info = text.strip()
        prompt_msg_id = context.user_data.get("letter_prompt_msg_id")
        user_msg_id = update.message.message_id
        letter_query = context.user_data.get("letter_query", "")
        
        context.user_data.pop("letter_step", None)
        context.user_data.pop("letter_query", None)
        context.user_data.pop("letter_prompt_msg_id", None)
        
        npc_char = {
            "name": npc_info,
            "title": "НПС",
            "estate": "НПС",
            "gender": "—",
            "age": "—",
            "personality": f"НПС: {npc_info}",
            "history": f"НПС: {npc_info}"
        }
        
        working_msg = await update.message.reply_text(f"// Перо скрипит по бумаге, составляю послание от лица НПС ({npc_info})... ✍️")
        working_msg_id = working_msg.message_id
        
        answer = await rewrite_letter_command_with_char(letter_query, npc_char)
        
        for m_id in [prompt_msg_id, working_msg_id, user_msg_id]:
            if m_id:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=m_id)
                except Exception:
                    pass

        if not answer or answer.startswith("// Ох"):
            await context.bot.send_message(chat_id=chat_id, text="// Простите, чернильница опрокинулась, и письмо от лица НПС не сошлось строчками.")
        else:
            await context.bot.send_message(chat_id=chat_id, text=answer, parse_mode="HTML")
        return

    if text.strip().lower().startswith("/письмо"):
        letter_query = text.strip()[len("/письмо"):].strip()
        
        try:
            if update.message:
                 await update.message.delete()
        except Exception:
            pass 

        chars = get_characters(user_id)
        msg = "// Кто пишет это послание?\n\n"
        for i, c in enumerate(chars, 1):
            msg += f"{i}. {c.get('name', 'Без имени')}\n"
            
        npc_num = len(chars) + 1
        msg += f"{npc_num}. 🎭 Написать от лица НПС (Укажите имя и детали НПС)\n"
        msg += "\n*(Напишите номер выбора или «отмена»)*"

        context.user_data["letter_step"] = "character"
        context.user_data["letter_query"] = letter_query
        
        msg_obj = await context.bot.send_message(chat_id=chat_id, text=msg)
        context.user_data["letter_prompt_msg_id"] = msg_obj.message_id
        return

    # ==========================
    # Групповые письма
    # ==========================
    group_letter_step = context.user_data.get("group_letter_step")

    if group_letter_step and lower in ["отмена", "нет", "отменить", "стоп"]:
        prompt_id = context.user_data.pop("group_letter_prompt_msg_id", None)
        if prompt_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=prompt_id)
            except Exception:
                pass
        context.user_data.pop("group_letter_step", None)
        context.user_data.pop("group_letter_char_idx", None)
        context.user_data.pop("group_letter_npc_char", None)
        context.user_data.pop("group_letter_query", None)
        await update.message.reply_text("// Как скажешь, милая. Отменяю составление группового письма.")
        return

    if group_letter_step == "character":
        try:
            clean_text = re.sub(r'[^\d]', '', text)
            if not clean_text:
                await update.message.reply_text("// Пожалуйста, напишите номер из списка цифрой.")
                return
            idx = int(clean_text) - 1
            chars = get_characters(user_id)
            npc_num = len(chars)

            if idx == npc_num:
                prompt_msg_id = context.user_data.get("group_letter_prompt_msg_id")
                try:
                    if prompt_msg_id: await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
                    await update.message.delete()
                except Exception:
                    pass

                context.user_data["group_letter_step"] = "npc_details"
                msg_obj = await context.bot.send_message(
                    chat_id=chat_id,
                    text="// 🎭 От какого именно НПС пишем групповые письма?\nНапишите его имя, титул или краткое описание."
                )
                context.user_data["group_letter_prompt_msg_id"] = msg_obj.message_id
                return

            elif 0 <= idx < len(chars):
                chosen_char = chars[idx]
                prompt_msg_id = context.user_data.get("group_letter_prompt_msg_id")
                user_msg_id = update.message.message_id
                existing_query = context.user_data.get("group_letter_query", "").strip()

                if existing_query:
                    context.user_data.pop("group_letter_step", None)
                    context.user_data.pop("group_letter_char_idx", None)
                    context.user_data.pop("group_letter_query", None)
                    context.user_data.pop("group_letter_prompt_msg_id", None)

                    mily = get_mily(chosen_char)
                    working_msg = await update.message.reply_text(f"// Перо быстро скользит по пергаменту, {mily}, составляю групповые послания... ✍️")
                    working_msg_id = working_msg.message_id

                    answer = await generate_group_letters_command_with_char(existing_query, chosen_char)
                    
                    unlocked_g = check_character_achievements(user_id, chosen_char, "group_letters_sent", 1)
                    unlocked_l = check_character_achievements(user_id, chosen_char, "letters_written", 1)
                    all_unlocked = list(set(unlocked_g + unlocked_l))

                    for m_id in [prompt_msg_id, working_msg_id, user_msg_id]:
                        if m_id:
                            try:
                                await context.bot.delete_message(chat_id=chat_id, message_id=m_id)
                            except Exception:
                                pass

                    if not answer or answer.startswith("// Охъ"):
                        await context.bot.send_message(chat_id=chat_id, text=f"// Простите, {get_dear(chosen_char)}, чернильница опрокинулась, письма не сошлись строчками.")
                    else:
                        await send_raw_long_message(update.message, answer)
                        if all_unlocked:
                            await context.bot.send_message(chat_id=chat_id, text=f"// ✨ За широкие светские интриги персонаж получает тайное достижение: <b>{', '.join(all_unlocked)}</b>!", parse_mode="HTML")
                else:
                    context.user_data["group_letter_step"] = "content"
                    context.user_data["group_letter_char_idx"] = idx

                    dear = get_dear(chosen_char)
                    msg = (
                        f"// Прекрасно, {dear}. Теперь опишите детали группового письма:\n\n"
                        "1. Укажите **тему письма**.\n"
                        "2. Перечислите **адресатов** (их имена и Telegram-username).\n"
                        "3. Укажите особенности для каждого.\n"
                        "4. Уточните: отправить **всем одинаковое** письмо или **для каждого разное**.\n\n"
                        "*(Напишите всё одним сообщением или отправьте «отмена»)*"
                    )
                    
                    if prompt_msg_id:
                        try:
                            await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
                        except Exception:
                            pass

                    msg_obj = await update.message.reply_text(msg)
                    context.user_data["group_letter_prompt_msg_id"] = msg_obj.message_id
                    
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
                    except Exception:
                        pass
            else:
                await update.message.reply_text("// Такого номера нет в списке. Напиши верный номер или «отмена».")
        except ValueError:
            await update.message.reply_text("// Пожалуйста, напиши просто цифру номера персонажа.")
        return

    if group_letter_step == "npc_details":
        npc_info = text.strip()
        prompt_msg_id = context.user_data.get("group_letter_prompt_msg_id")
        user_msg_id = update.message.message_id
        existing_query = context.user_data.get("group_letter_query", "").strip()

        npc_char = {
            "name": npc_info,
            "title": "НПС",
            "estate": "НПС",
            "gender": "—",
            "age": "—",
            "personality": f"НПС: {npc_info}",
            "history": f"НПС: {npc_info}"
        }

        if existing_query:
            context.user_data.pop("group_letter_step", None)
            context.user_data.pop("group_letter_query", None)
            context.user_data.pop("group_letter_prompt_msg_id", None)

            working_msg = await update.message.reply_text(f"// Перо быстро скользит по пергаменту, составляю групповые послания от НПС ({npc_info})... ✍️")
            working_msg_id = working_msg.message_id

            answer = await generate_group_letters_command_with_char(existing_query, npc_char)

            for m_id in [prompt_msg_id, working_msg_id, user_msg_id]:
                if m_id:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=m_id)
                    except Exception:
                        pass

            if not answer or answer.startswith("// Охъ"):
                await context.bot.send_message(chat_id=chat_id, text="// Простите, чернильница опрокинулась, письма от НПС не сошлись строчками.")
            else:
                await send_raw_long_message(update.message, answer)
        else:
            context.user_data["group_letter_step"] = "content"
            context.user_data["group_letter_npc_char"] = npc_char

            msg = (
                f"// Прекрасно! Пишем от лица НПС ({npc_info}). Теперь опишите детали группового письма:\n\n"
                "1. Укажите **тему письма**.\n"
                "2. Перечислите **адресатов** (их имена и Telegram-username).\n"
                "3. Укажите особенности для каждого.\n"
                "4. Уточните: отправить **всем одинаковое** письмо или **для каждого разное**.\n\n"
                "*(Напишите всё одним сообщением или отправьте «отмена»)*"
            )

            if prompt_msg_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
                except Exception: pass

            msg_obj = await update.message.reply_text(msg)
            context.user_data["group_letter_prompt_msg_id"] = msg_obj.message_id

            try: await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
            except Exception: pass
        return

    if group_letter_step == "content":
        idx = context.user_data.get("group_letter_char_idx", 0)
        npc_char = context.user_data.get("group_letter_npc_char")
        
        if npc_char:
            chosen_char = npc_char
        else:
            chars = get_characters(user_id)
            chosen_char = chars[idx] if 0 <= idx < len(chars) else None

        prompt_msg_id = context.user_data.get("group_letter_prompt_msg_id")
        user_msg_id = update.message.message_id
        query_text = text

        context.user_data.pop("group_letter_step", None)
        context.user_data.pop("group_letter_char_idx", None)
        context.user_data.pop("group_letter_npc_char", None)
        context.user_data.pop("group_letter_prompt_msg_id", None)

        char_title_name = chosen_char.get('name', 'автора')
        working_msg = await update.message.reply_text(f"// Перо быстро скользит по пергаменту, составляю групповые послания от лица {char_title_name}... ✍️")
        working_msg_id = working_msg.message_id

        answer = await generate_group_letters_command_with_char(query_text, chosen_char)
        
        unlocked = []
        if not npc_char:
            unlocked_g = check_character_achievements(user_id, chosen_char, "group_letters_sent", 1)
            unlocked_l = check_character_achievements(user_id, chosen_char, "letters_written", 1)
            unlocked = list(set(unlocked_g + unlocked_l))

        for m_id in [prompt_msg_id, working_msg_id, user_msg_id]:
            if m_id:
                try: await context.bot.delete_message(chat_id=chat_id, message_id=m_id)
                except Exception: pass

        if not answer or answer.startswith("// Охъ"):
            await context.bot.send_message(chat_id=chat_id, text="// Простите, чернильница опрокинулась, письма не сошлись строчками.")
        else:
            await send_raw_long_message(update.message, answer)
            if unlocked:
                await context.bot.send_message(chat_id=chat_id, text=f"// ✨ За широкие светские интриги персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        return

    if text.strip().lower().startswith("/групповое_письмо") or text.strip().lower().startswith("/гписьмо"):
        group_query = text.strip().split(" ", 1)[1] if " " in text.strip() else ""

        try:
              if update.message:
                 await update.message.delete()
        except Exception as e:
            print("Не удалось удалить сообщение пользователя:", e)

        chars = get_characters(user_id)
        msg = "// Кто пишет эти групповые послания?\n\n"
        for i, c in enumerate(chars, 1):
            msg += f"{i}. {c.get('name', 'Без имени')}\n"
            
        npc_num = len(chars) + 1
        msg += f"{npc_num}. 🎭 Написать от лица НПС (Укажите имя и детали НПС)\n"
        msg += "\n*(Напишите номер выбора или «отмена»)*"

        context.user_data["group_letter_step"] = "character"
        context.user_data["group_letter_query"] = group_query
        
        msg_obj = await context.bot.send_message(chat_id=chat_id, text=msg)
        context.user_data["group_letter_prompt_msg_id"] = msg_obj.message_id
        return

    # ==========================
    # ИНТЕРАКТИВНОЕ СОБЫТИЕ (STATE MACHINE)
    # ==========================
    event_step = context.user_data.get("event_step")
    
    if event_step and lower in ["отмена", "нет", "отменить", "стоп"]:
        context.user_data.pop("event_step", None)
        context.user_data.pop("event_mode", None)
        context.user_data.pop("event_participants", None)
        context.user_data.pop("chat_active_chars", None)
        await update.message.reply_text("// Как скажешь, милая. Отменяю создание события.")
        return

    if re.search(r"катя,?\s*(сгенерируй\s+)?событие", lower) or lower == "/event":
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей, милая. Создайте анкету сначала.")
            return
            
        active_char = ACTIVE_CHARACTER.get(user_id) or chars[0]
        dear = get_dear(active_char)

        msg = (
            f"// Для кого мы крутим колесо судьбы, {dear}?\n\n"
            f"1. 👤 Одиночное приключение (для одного вашего персонажа)\n"
            f"2. 👥 Совместное приключение (для нескольких игроков этого чата)\n\n"
            f"*(Напишите цифру 1 или 2, либо «отмена»)*"
        )
        context.user_data["event_step"] = "select_mode"
        await update.message.reply_text(msg)
        return

    if event_step == "select_mode":
        choice = text.strip()
        my_chars = get_characters(user_id)
        my_active = ACTIVE_CHARACTER.get(user_id) or my_chars[0]
        
        if choice == "1":
            context.user_data["event_mode"] = "single"
            msg = f"// Кто из ваших персонажей отправится в приключение?\n\n"
            for i, c in enumerate(my_chars, 1):
                msg += f"{i}. {c.get('name', 'Без имени')}\n"
            msg += "\n*(Напишите номер персонажа или «отмена»)*"
            context.user_data["event_step"] = "select_char_single"
            await update.message.reply_text(msg)
            
        elif choice == "2":
            context.user_data["event_mode"] = "multi"
            msg = (
                f"// Как мы выберем спутников для приключения, {get_mily(my_active)}?\n\n"
                f"1. ✍️ Выбрать вручную из списка активных персонажей этого чата\n"
                f"2. 🎲 Перст судьбы (Я выберу случайных компаньонов сама)\n\n"
                f"*(Напишите цифру 1 или 2, либо «отмена»)*"
            )
            context.user_data["event_step"] = "select_multi_mode"
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("// Пожалуйста, напишите цифру 1 или 2, либо «отмена».")
        return

    if event_step == "select_char_single":
        try:
            clean_text = re.sub(r'[^\d]', '', text)
            idx = int(clean_text) - 1
            my_chars = get_characters(user_id)
            if 0 <= idx < len(my_chars):
                chosen_char = my_chars[idx]
                context.user_data["event_participants"] = [chosen_char]
                
                dear = get_dear(chosen_char)
                msg = (
                    f"// Прекрасно, {dear}! Какую сферу жизни затронем?\n\n"
                    "1. 💌 Отношения и репутация\n"
                    "2. 🏛️ Власть, карьера и положение\n"
                    "3. 💰 Деньги, векселя и имущество\n"
                    "4. 🕵️ Тайны и прошлое\n"
                    "5. 🎭 Светская жизнь, быт и досуг\n"
                    "6. 🎲 Случайный перст судьбы (выберу сама)"
                )
                context.user_data["event_step"] = "theme"
                await update.message.reply_text(msg)
            else:
                await update.message.reply_text("// Такого номера нет в списке. Напишите верный номер или «отмена».")
        except ValueError:
            await update.message.reply_text("// Пожалуйста, напишите номер персонажа цифрой.")
        return

    if event_step == "select_multi_mode":
        choice = text.strip()
        my_chars = get_characters(user_id)
        my_active = ACTIVE_CHARACTER.get(user_id) or my_chars[0]
        
        all_active_in_chat = get_all_active_characters(filter_chat_id=chat_id)
        companions = [item for item in all_active_in_chat if item[0] != user_id]
        
        if not companions:
            await update.message.reply_text(
                "// Увы, в этом чате пока нет других представленных ко двору персонажей. "
                "Кручу колесо одиночной судьбы для вашего персонажа! Переходим к выбору темы."
            )
            context.user_data["event_participants"] = [my_active]
            msg = (
                f"// Какую сферу жизни затронем?\n\n"
                "1. 💌 Отношения и репутация\n"
                "2. 🏛️ Власть, карьера и положение\n"
                "3. 💰 Деньги, векселя и имущество\n"
                "4. 🕵️ Тайны и прошлое\n"
                "5. 🎭 Светская жизнь, быт и досуг\n"
                "6. 🎲 Случайный перст судьбы (выберу сама)"
            )
            context.user_data["event_step"] = "theme"
            await update.message.reply_text(msg)
            return

        if choice == "1":
            msg = f"// Выберите компаньонов из списка (укажите один или несколько номеров через запятую или пробел):\n\n"
            for i, (uid, c) in enumerate(companions, 1):
                msg += f"{i}. {c.get('name', 'Без имени')} ({c.get('title', '—')})\n"
            msg += "\n*(Напишите номера спутников или «отмена»)*"
            
            context.user_data["chat_active_chars"] = companions
            context.user_data["event_step"] = "select_multi_manual"
            await update.message.reply_text(msg)
            
        elif choice == "2":
            num_to_choose = min(len(companions), random.randint(1, 3))
            chosen_companions = random.sample(companions, num_to_choose)
            
            participants = [my_active] + [item[1] for item in chosen_companions]
            context.user_data["event_participants"] = participants
            
            names_list = ", ".join(c.get('name') for c in participants[1:])
            await update.message.reply_text(f"// Судьба определила вам в спутники: <b>{names_list}</b>! Переходим к выбору темы.", parse_mode="HTML")
            
            msg = (
                f"// Какую сферу жизни затронем?\n\n"
                "1. 💌 Отношения и репутация\n"
                "2. 🏛️ Власть, карьера и положение\n"
                "3. 💰 Деньги, векселя и имущество\n"
                "4. 🕵️ Тайны и прошлое\n"
                "5. 🎭 Светская жизнь, быт и досуг\n"
                "6. 🎲 Случайный перст судьбы (выберу сама)"
            )
            context.user_data["event_step"] = "theme"
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("// Пожалуйста, напишите цифру 1 или 2, либо «отмена».")
        return

    if event_step == "select_multi_manual":
        companions = context.user_data.get("chat_active_chars", [])
        my_chars = get_characters(user_id)
        my_active = ACTIVE_CHARACTER.get(user_id) or my_chars[0]
        
        try:
            nums = [int(n) for n in re.findall(r'\d+', text)]
            selected = []
            for num in nums:
                idx = num - 1
                if 0 <= idx < len(companions):
                    selected.append(companions[idx][1])
            
            if not selected:
                await update.message.reply_text("// Я не распознала номера из списка. Пожалуйста, напишите их цифрами через запятую.")
                return
                
            context.user_data["event_participants"] = [my_active] + selected
            
            names_list = ", ".join(c.get('name') for c in selected)
            await update.message.reply_text(f"// Отлично! С вами отправятся: <b>{names_list}</b>. Переходим к выбору темы.", parse_mode="HTML")
            
            msg = (
                f"// Какую сферу жизни затронем?\n\n"
                "1. 💌 Отношения и репутация\n"
                "2. 🏛️ Власть, карьера и положение\n"
                "3. 💰 Деньги, векселя и имущество\n"
                "4. 🕵️ Тайны и прошлое\n"
                "5. 🎭 Светская жизнь, быт и досуг\n"
                "6. 🎲 Случайный перст судьбы (выберу сама)"
            )
            context.user_data["event_step"] = "theme"
            await update.message.reply_text(msg)
        except Exception:
            await update.message.reply_text("// Произошла ошибка распознавания номеров. Пожалуйста, укажите верные цифры.")
        return

    if event_step == "theme":
        choice = text.strip()
        if choice in ["1", "2", "3", "4", "5", "6"]:
            if choice == "1":
                theme_str = "Отношения и репутация"
                sub_key = "отношения"
            elif choice == "2":
                theme_str = "Власть, карьера и положение"
                sub_key = "карьера"
            elif choice == "3":
                theme_str = "Деньги, векселя и имущество"
                sub_key = "деньги"
            elif choice == "4":
                theme_str = "Тайны и прошлое"
                sub_key = "тайны"
            elif choice == "5":
                sub_themes_pool = [
                    "Театральная жизнь и премьеры",
                    "Искусство, выставки и поэзия",
                    "Музыкальные вечера и романсы",
                    "Карточные игры и пари",
                    "Светский этикет и визиты",
                    "Питомец в большом городе",
                    "Гастрономия и кулинарные курьезы",
                    "Шумные рынки и барахолки",
                    "Кофейни и кондитерские",
                    "Уличные музыканты и шарманщики",
                    "Петербургская мистика и тайны",
                    "Антиквариат и старинные лавки",
                    "Почтовые казусы и письма",
                    "Модные ателье и портные",
                    "Домашние снадобья и лекари",
                    "Дорожные приключения"
                ]
                theme_str = random.choice(sub_themes_pool)
                sub_key = "быт"
            else:
                theme_str = "Случайная тема"
                sub_key = random.choice(["отношения", "карьера", "деньги", "тайны", "быт"])

            participants = context.user_data.get("event_participants", [])
            if not participants:
                my_chars = get_characters(user_id)
                participants = [ACTIVE_CHARACTER.get(user_id) or my_chars[0]]

            char = participants[0]
            mily = get_mily(char)
            
            char_info = json.dumps(participants, ensure_ascii=False)
            
            context.user_data.pop("event_step", None)
            context.user_data.pop("event_mode", None)
            context.user_data.pop("event_participants", None)
            context.user_data.pop("chat_active_chars", None)
            
            await update.message.reply_text(f"// Кручу колесо судьбы, {mily}... Ждем вестей от курьеров! 📜")
            
            event_result, is_success, is_duel_conflict = await generate_roleplay_event(char_info, theme_str)
            
            unlocked = []
            if is_success:
                unlocked += check_character_achievements(user_id, char, "luck_success", 1)
            if is_duel_conflict:
                unlocked += check_character_achievements(user_id, char, "duels_conflicts", 1)
            
            cur_hour_ev = datetime.datetime.now().hour
            if 1 <= cur_hour_ev < 5:
                unlocked += check_character_achievements(user_id, char, "night_events", 1)

            unlocked += check_character_achievements(user_id, char, "event_theme_dummy", 1, sub_category=sub_key)
            all_ach = list(set(unlocked))

            await update.message.reply_text(event_result, parse_mode="HTML")
            if all_ach:
                await update.message.reply_text(f"// ✨ Судьба благоволит вам! Персонаж получает тайные достижения: <b>{', '.join(all_ach)}</b>!", parse_mode="HTML")
        else:
            await update.message.reply_text("// Напиши цифру от 1 до 6 или «отмена».")
        return

    # ==========================
    # Статусы персонажей (Включая слухи от других игроков)
    # ==========================
    pending = context.user_data.get("pending_status")
    if pending:
        act = pending["action"]
        st_cat = pending["type"]
        st_val = pending["text"]
        target_uid = pending.get("target_uid", user_id)
        
        affirmatives = ["да", "согласна", "согласен", "добавь", "давай", "запиши", "конечно", "ага", "ок", "хорошо", "заноси", "пусти", "будь добра"]
        negatives = ["нет", "отмена", "не надо", "не хочу", "не добавляй", "отмени"]

        if act == "add" and any(w in lower for w in affirmatives):
            t_chars = get_characters(target_uid)
            success = False
            char = None
            if t_chars:
                from characters import load_characters
                db = load_characters()
                active_idx = db.get(target_uid + "_active", 0)
                if active_idx >= len(t_chars):
                    active_idx = 0
                char = t_chars[active_idx]
                
                clean_expired_statuses_and_rumors(char)
                
                for key_cat in ["permanent", "temporary", "achievements", "tragedies", "rumors"]:
                    if key_cat not in char["changes"]:
                        char["changes"][key_cat] = []

                st_cat = normalize_status_category(st_cat)
                    
                if st_val not in char["changes"][st_cat]:
                    char["changes"][st_cat].append(st_val)
                    
                    if st_cat in ["temporary", "rumors"]:
                        if "status_times" not in char: char["status_times"] = {}
                        char["status_times"][st_val] = time.time()
                    elif st_cat == "achievements":
                        if "achievement_times" not in char: char["achievement_times"] = {}
                        char["achievement_times"][st_val] = time.time()
                        
                    clean_expired_statuses_and_rumors(char)
                    t_chars[active_idx] = char
                    save_characters(target_uid, t_chars)
                    ACTIVE_CHARACTER[target_uid] = char
                    success = True

            if success and char:
                target_display = f"персонажа «{char.get('name')}»" if target_uid != user_id else "анкету"
                unlocked = check_character_achievements(target_uid, char, "status_added", 1)
                
                t_chars[active_idx] = char
                save_characters(target_uid, t_chars)
                ACTIVE_CHARACTER[target_uid] = char

                await update.message.reply_text(f"// Готово, милая! Статус «{st_val}» добавлен в {target_display}.")
                if unlocked and target_uid == user_id:
                    await update.message.reply_text(f"// ✨ За бурную светскую хронику персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
            else:
                await update.message.reply_text("// Не получилось записать статус (возможно, такой статус уже существует у персонажа).")
            context.user_data.pop("pending_status", None)
            return
            
        elif act == "remove" and any(w in lower for w in affirmatives):
            t_chars = get_characters(target_uid)
            success = False
            char = None
            if t_chars:
                from characters import load_characters
                db = load_characters()
                active_idx = db.get(target_uid + "_active", 0)
                if active_idx >= len(t_chars):
                    active_idx = 0
                char = t_chars[active_idx]
                
                st_cat = normalize_status_category(st_cat)
                
                if char and "changes" in char and st_cat in char["changes"]:
                    if st_val in char["changes"][st_cat]:
                        char["changes"][st_cat].remove(st_val)
                        t_chars[active_idx] = char
                        save_characters(target_uid, t_chars)
                        ACTIVE_CHARACTER[target_uid] = char
                        success = True
            if success and char:
                target_display = f"персонажа «{char.get('name')}»" if target_uid != user_id else "анкету"
                await update.message.reply_text(f"// Статус «{st_val}» убран из {target_display}, дорогая.")
            else:
                await update.message.reply_text("// У персонажа не найден такой статус для удаления.")
            context.user_data.pop("pending_status", None)
            return  
            
        elif any(w in lower for w in negatives):
            await update.message.reply_text("// Как скажешь, изменения отменены.")
            context.user_data.pop("pending_status", None)
            return
            
        else:
            context.user_data.pop("pending_status", None)
            if not is_addressed_to_katya and not is_reply and not (update.effective_chat.type == "private"):
                return

    waiting_status_type = context.user_data.get("waiting_status_type")
    if waiting_status_type:
        context.user_data.pop("waiting_status_type", None)
        char = ACTIVE_CHARACTER.get(user_id)
        if not char:
            await update.message.reply_text("// Сначала выберите активного персонажа.")
            return
        st_text = text.strip(" '\"«»")
        
        success = False
        if st_text:
            clean_expired_statuses_and_rumors(char)
            st_cat = normalize_status_category(waiting_status_type)
                
            if st_text not in char["changes"].get(st_cat, []):
                if st_cat not in char["changes"]:
                    char["changes"][st_cat] = []
                char["changes"][st_cat].append(st_text)
                
                if st_cat in ["temporary", "rumors"]:
                    if "status_times" not in char: char["status_times"] = {}
                    char["status_times"][st_text] = time.time()
                elif st_cat == "achievements":
                    if "achievement_times" not in char: char["achievement_times"] = {}
                    char["achievement_times"][st_text] = time.time()
                    
                clean_expired_statuses_and_rumors(char)
                all_chars = get_characters(user_id)
                from characters import load_characters
                db = load_characters()
                active_idx = db.get(user_id + "_active", 0)
                if active_idx < len(all_chars):
                    all_chars[active_idx] = char
                else:
                    for i, c in enumerate(all_chars):
                        if c.get("name") == char.get("name"):
                            all_chars[i] = char
                            break
                save_characters(user_id, all_chars)
                ACTIVE_CHARACTER[user_id] = char
                success = True

        if success:
            curr_char = ACTIVE_CHARACTER[user_id]
            unlocked = check_character_achievements(user_id, curr_char, "status_added", 1)
            await update.message.reply_text(f"// Статус успешно добавлен в анкету: {st_text}")
            if unlocked:
                await update.message.reply_text(f"// ✨ За бурные светские перемены персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        else:
            await update.message.reply_text("// Не удалось добавить статус. Вероятно, он уже существует.")
        return

    if context.user_data.get("waiting_status_remove"):
        context.user_data.pop("waiting_status_remove", None)
        st_text = text.strip(" '\"«»")
        char = ACTIVE_CHARACTER.get(user_id)
        if not char:
            await update.message.reply_text("// Активный персонаж не выбран.")
            return
            
        found = False
        for cat in ["temporary", "permanent", "achievements", "tragedies", "rumors"]:
            if cat in char.get("changes", {}) and st_text in char["changes"][cat]:
                char["changes"][cat].remove(st_text)
                clean_expired_statuses_and_rumors(char)
                all_chars = get_characters(user_id)
                from characters import load_characters
                db = load_characters()
                active_idx = db.get(user_id + "_active", 0)
                if active_idx < len(all_chars):
                    all_chars[active_idx] = char
                else:
                    for i, c in enumerate(all_chars):
                        if c.get("name") == char.get("name"):
                            all_chars[i] = char
                            break
                save_characters(user_id, all_chars)
                ACTIVE_CHARACTER[user_id] = char
                found = True
                await update.message.reply_text(f"// Статус «{st_text}» успешно убран из анкеты, милая.")
                break
                
        if not found:
            await update.message.reply_text(f"// Я не нашла статус «{st_text}» в анкете. Проверь точное название.")
        return

    if lower in ["катя мои персонажи", "катя, мои персонажи", "катя персонажи"]:
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей.")
            return
        answer = "// Ваши персонажи:\n\n" + "\n".join(f"{i}. {c.get('name', 'Без имени')}" for i, c in enumerate(chars, 1))
        await update.message.reply_text(answer)
        return

    match = re.search(r"катя,?\s+выбери персонажа\s+(\d+)", lower)
    if match:
        idx = int(match.group(1)) - 1
        if set_active_character_local(user_id, idx):
            curr_char = ACTIVE_CHARACTER[user_id]
            unlocked = check_character_achievements(user_id, curr_char, "character_switches", 1)
            await update.message.reply_text(
                "// Хорошо. Теперь играем за:\n\n" + format_character_sheet(curr_char),
                parse_mode="HTML"
            )
            if unlocked:
                await update.message.reply_text(f"// ✨ За смену масок персонаж получает достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        else:
            await update.message.reply_text("// Такого персонажа нет.")
        return

    match_del = re.search(r"катя,?\s+удали персонажа\s+(\d+)", lower)
    if match_del:
        idx = int(match_del.group(1)) - 1
        chars = get_characters(user_id)
        if 0 <= idx < len(chars):
            rem = chars[idx]
            remove_character(user_id, idx)
            if ACTIVE_CHARACTER.get(user_id, {}).get("name") == rem.get("name"):
                ACTIVE_CHARACTER.pop(user_id, None)
            await update.message.reply_text(f"// Персонаж «{rem.get('name', 'Без имени')}» удален.")
        else:
            await update.message.reply_text("// Неверный номер персонажа.")
        return

    if lower in ["катя новая анкета", "катя, новая анкета"]:
        WAITING_CHARACTER[user_id] = True
        msg_prompt = await update.message.reply_text("// Пришлите анкету нового персонажа (Имя, Возраст, Пол, Внешность и т.д.).")
        context.user_data["new_char_msg_ids"] = [update.message.message_id, msg_prompt.message_id]
        return

    if WAITING_CHARACTER.get(user_id):
        char = await convert_character_sheet(text)
        add_character(user_id, char)
        WAITING_CHARACTER.pop(user_id, None)
        
        if "new_char_msg_ids" not in context.user_data:
            context.user_data["new_char_msg_ids"] = []
        context.user_data["new_char_msg_ids"].append(update.message.message_id)

        missing = find_missing_character_fields(char)
        if missing:
            WAITING_CHARACTER_UPDATE[user_id] = char
            msg_upd = await update.message.reply_text("// Анкета сохранена, но не хватает данных: " + ", ".join(missing) + "\nМожете прислать их следом.")
            context.user_data["new_char_msg_ids"].append(msg_upd.message_id)
        else:
            msg_ids_to_delete = context.user_data.pop("new_char_msg_ids", [])
            for m_id in msg_ids_to_delete:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=m_id)
                except Exception:
                    pass
            
            try:
                await update.message.reply_text(f"<blockquote expandable>Персонаж {char.get('name')} успешно добавлен и готов к светской жизни!</blockquote>", parse_mode="HTML")
            except Exception:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Персонаж {char.get('name')} успешно добавлен и готов к светской жизни!"
                )
        return

    if user_id in WAITING_CHARACTER_UPDATE:
        char = update_character_from_text(WAITING_CHARACTER_UPDATE[user_id], text)
        WAITING_CHARACTER_UPDATE.pop(user_id, None)
        chars = get_characters(user_id)
        for i, c in enumerate(chars):
            if c.get("name") == char.get("name"):
                chars[i] = char
                break
        save_characters(user_id, chars)
        
        if "new_char_msg_ids" not in context.user_data:
            context.user_data["new_char_msg_ids"] = []
        context.user_data["new_char_msg_ids"].append(update.message.message_id)

        msg_ids_to_delete = context.user_data.pop("new_char_msg_ids", [])
        for m_id in msg_ids_to_delete:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=m_id)
            except Exception:
                pass
        
        await update.message.reply_text(f"<blockquote expandable>Персонаж {char.get('name')} добавлен.</blockquote>", parse_mode="HTML")
        return

    if lower in [
        "катя забудь все",
        "катя, забудь все",
        "катя забудь всё",
        "катя, забудь всё",
        "/clear"
    ]:
        clear_user_memory(user_id)
        clear_visual_memory(user_id)
        
        active_char = ACTIVE_CHARACTER.get(user_id)
        unlocked = check_character_achievements(user_id, active_char, "memory_clears", 1)
        
        await update.message.reply_text("// Начнем наш диалог сначала.")
        if unlocked:
            await update.message.reply_text(f"// ✨ За способность начать с чистого листа персонаж получает достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        return

    if lower in ["катя правила", "катя, правила", "/правила"]:
        full_rules = RULES + "\n\n--- ПРАВИЛА ОТНОШЕНИЙ ---\n\n" + RELATIONSHIP_RULES
        active_char = ACTIVE_CHARACTER.get(user_id)
        unlocked = check_character_achievements(user_id, active_char, "rules_checked", 1)
        await send_long_message(update.message, full_rules)
        if unlocked:
            await update.message.reply_text(f"// ✨ За усердное изучение законов персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        return

    match_rel_punct = re.search(r"катя,?\s+пункт\s+отношений\s+(\d+)", lower)
    if match_rel_punct:
        num = match_rel_punct.group(1)
        rule_text = get_rule_by_number(num, is_relationship=True)
        active_char = ACTIVE_CHARACTER.get(user_id)
        unlocked = check_character_achievements(user_id, active_char, "rules_checked", 1)
        if rule_text:
            await update.message.reply_text(f"<blockquote expandable>{rule_text}</blockquote>", parse_mode="HTML")
            if unlocked:
                await update.message.reply_text(f"// ✨ Персонаж получает достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        else:
            await update.message.reply_text("// Такого пункта в правилах отношений нет.")
        return

    match_punct = re.search(r"катя,?\s+пункт\s+(\d+)", lower)
    if match_punct:
        num = match_punct.group(1)
        rule_text = get_rule_by_number(num, is_relationship=False)
        active_char = ACTIVE_CHARACTER.get(user_id)
        unlocked = check_character_achievements(user_id, active_char, "rules_checked", 1)
        if rule_text:
            await update.message.reply_text(f"<blockquote expandable>{rule_text}</blockquote>", parse_mode="HTML")
            if unlocked:
                await update.message.reply_text(f"// ✨ Персонаж получает достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        else:
            await update.message.reply_text("// Такого пункта в правилах нет.")
        return

    rule_triggers = ["правил", "пункт", "можно ли", "разрешен", "запрещен", "лимити", "по правилам"]
    if any(trigger in lower for trigger in rule_triggers):
        rule_check = await analyze_question_for_rule(text)
        if rule_check and rule_check.get("found"):
            r_type = rule_check.get("type")
            r_num = str(rule_check.get("number"))
            is_rel = (r_type == "relationship")
            found_rule_text = get_rule_by_number(r_num, is_relationship=is_rel)
            
            active_char = ACTIVE_CHARACTER.get(user_id)
            unlocked = check_character_achievements(user_id, active_char, "rules_checked", 1)
            
            if found_rule_text:
                await update.message.reply_text(f"<blockquote expandable>{found_rule_text}</blockquote>", parse_mode="HTML")
                if unlocked:
                    await update.message.reply_text(f"// ✨ Персонаж получает достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
                return

    active_char = ACTIVE_CHARACTER.get(user_id)
    unlocked_msgs = []
    unlocked_replies = []
    unlocked_night = []

    if active_char:
        unlocked_msgs = check_character_achievements(user_id, active_char, "messages_to_katya", 1)
        unlocked_replies = check_character_achievements(user_id, active_char, "total_replies", 1)

        cur_hour_msg = datetime.datetime.now().hour
        if 1 <= cur_hour_msg < 5:
            unlocked_night = check_character_achievements(user_id, active_char, "night_messages", 1)

    text_with_context = text
    if is_reply and update.message.reply_to_message.text:
        raw_reply_text = update.message.reply_to_message.text.replace("//", "").strip()
        text_with_context = f"[Это ответ на твою реплику: \"{raw_reply_text}\"]\n\n{text}"

    answer = await ask_katya(text_with_context, user_id, username)
    await update.message.reply_text(answer, parse_mode="HTML")

    all_chat_achievements = list(set(unlocked_msgs + unlocked_replies + unlocked_night))
    if all_chat_achievements and active_char:
        await update.message.reply_text(
            f"// ✨ За вашу светскую активность персонаж получает тайное достижение: "
            f"<b>{', '.join(all_chat_achievements)}</b>!",
            parse_mode="HTML"
        )

    # Автосохранение анкеты активного персонажа в конце обработки сообщения
    active_char = ACTIVE_CHARACTER.get(user_id)
    if active_char:
        clean_expired_statuses_and_rumors(active_char)
        all_chars = get_characters(user_id)
        from characters import load_characters
        db = load_characters()
        active_idx = db.get(user_id + "_active", 0)
        if 0 <= active_idx < len(all_chars):
            all_chars[active_idx] = active_char
        else:
            for i, c in enumerate(all_chars):
                if c.get("name") == active_char.get("name"):
                    all_chars[i] = active_char
                    break
        save_characters(user_id, all_chars)

    # Анализ изменений и сплетен
    status_analysis = await analyze_dialogue_for_status_changes(text, answer, user_id)
    if status_analysis and status_analysis.get("action") in ["add", "remove"]:
        action = status_analysis.get("action")
        st_type = status_analysis.get("type", "temporary")
        st_text = status_analysis.get("status_text", "")
        target_char_name = status_analysis.get("target_character_name", "")

        target_uid = user_id
        target_display_name = "вашу анкету"
        
        if target_char_name:
            t_name_clean = target_char_name.lower().strip()
            all_active = get_all_active_characters()
            for tuid, tc in all_active:
                c_name = tc.get("name", "").lower().strip()
                if t_name_clean == c_name or t_name_clean in c_name or c_name in t_name_clean:
                    target_uid = tuid
                    target_display_name = f"анкету персонажа «{tc.get('name')}»"
                    break

        if st_text:
            if action == "add":
                proposal = f"// Кажется, в сюжете произошло событие. Добавить статус «{st_text}» в {target_display_name}?"
                hint = "*(Ответь «Да, добавь» или «Нет, отмена»)*"
            else:
                proposal = f"// Кажется, статус «{st_text}» больше не актуален. Удалить его из {target_display_name}?"
                hint = "*(Ответь «Да, удали» или «Нет, отмена»)*"

            context.user_data["pending_status"] = {
                "action": action,
                "type": st_type,
                "text": st_text,
                "target_uid": target_uid
            }
            await update.message.reply_text(f"{proposal}\n\n{hint}")

    print("Запрос:", text)

# ==========================
# Запускаем бота
# ==========================

def main():
    # Запускаем фоновый веб-сервер для хостинга Render
    threading.Thread(target=run_health_server, daemon=True).start()

    print("Екатерина запускается...")
    
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photos))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.TEXT, messages))

    print("Екатерина запущена!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

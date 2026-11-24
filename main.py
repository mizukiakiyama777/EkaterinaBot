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
import logging
import html
import urllib.parse
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
from PIL import Image

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
)
from telegram.error import NetworkError, TimedOut, Conflict, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types
from duckduckgo_search import DDGS

from config import (
    TELEGRAM_TOKEN,
    GEMINI_KEYS,
    MODEL_NAME,
    MODEL_SMART,
    MODEL_LITE,
    OWNER_ID,
    ROLE_OWNER_ID,
)
from errors import (
    QUOTA_ERROR,
    NO_KEY_ERROR,
    REGION_ERROR,
    SERVER_ERROR,
    UNKNOWN_ERROR,
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

REPORT_DEVELOPER_ID = 727703718
OWNER_IDS = {OWNER_ID, ROLE_OWNER_ID}
BASE_ADMIN_IDS = [OWNER_ID, ROLE_OWNER_ID, 727703718, 5714213307]
ADMIN_IDS = list(BASE_ADMIN_IDS)

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
print("Используемые модели -> Умная:", MODEL_SMART, "| Лёгкая:", MODEL_LITE)

if not GEMINI_KEYS:
    raise Exception("Нет Gemini ключей")

keys = itertools.cycle(GEMINI_KEYS)

# ==========================
# Директория постоянного хранения
# ==========================

PERSISTENT_DIR = '/data' if os.path.exists('/data') else ('data' if os.path.exists('data') else '.')

# ==========================
# Управление динамическими администраторами
# ==========================

ADMINS_FILE_NAME = "admins.json"
ADMINS_FILE = os.path.join(PERSISTENT_DIR, ADMINS_FILE_NAME)
DYNAMIC_ADMIN_IDS = []

def load_admins():
    global DYNAMIC_ADMIN_IDS, ADMIN_IDS
    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, list):
                DYNAMIC_ADMIN_IDS = [int(x) for x in loaded]
            else:
                DYNAMIC_ADMIN_IDS = []
    except Exception:
        DYNAMIC_ADMIN_IDS = []
    ADMIN_IDS = list(dict.fromkeys(BASE_ADMIN_IDS + DYNAMIC_ADMIN_IDS))

def save_admins():
    global ADMIN_IDS
    ADMIN_IDS = list(dict.fromkeys(BASE_ADMIN_IDS + DYNAMIC_ADMIN_IDS))
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(DYNAMIC_ADMIN_IDS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Ошибка сохранения admins.json:", e)

def is_admin(tg_user_id):
    return int(tg_user_id) in ADMIN_IDS

def is_owner(tg_user_id):
    return int(tg_user_id) in OWNER_IDS

load_admins()

# ==========================
# Безопасное извлечение текста из Gemini и обработка ошибок (errors.py)
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

def classify_gemini_error(exc):
    """Определяет тип ошибки Gemini и возвращает соответствующий текст из errors.py."""
    msg = str(exc).lower()
    if any(k in msg for k in ["429", "quota", "resource_exhausted", "rate limit", "too many requests"]):
        return "quota", QUOTA_ERROR
    if any(k in msg for k in ["api_key_invalid", "api key not valid", "invalid api key", "401", "403", "permission_denied", "unauthenticated"]):
        return "no_key", NO_KEY_ERROR
    if any(k in msg for k in ["location", "region", "country", "failed_precondition", "not supported in your"]):
        return "region", REGION_ERROR
    if any(k in msg for k in ["500", "502", "503", "504", "unavailable", "internal", "overloaded", "deadline", "timed out", "timeout", "connection"]):
        return "server", SERVER_ERROR
    return "unknown", UNKNOWN_ERROR

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

def get_msk_now():
    """Возвращает текущее Московское время (UTC+3)."""
    msk_tz = datetime.timezone(datetime.timedelta(hours=3))
    return datetime.datetime.now(msk_tz)

def get_current_season_info():
    """Определяет точное Московское время (UTC+3), день недели, число, месяц, 1838 год и сезон."""
    now = get_msk_now()
    
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
# СИСТЕМА ЖИВЫХ И УСТОЙЧИВЫХ ОТНОШЕНИЙ С ЕКАТЕРИНОЙ II
# ==========================

def ensure_katya_relationship(char, tg_user_id=None):
    """Гарантирует структуру отношений у персонажа, ставит теплый старт владельцам и остужает старые обиды."""
    if not char or not isinstance(char, dict):
        return None
    rel = char.get("katya_relationship")
    if not isinstance(rel, dict):
        rel = {}
    default_imp = "Императрица пока лишь присматривается к вам и оценивает ваши манеры при петербургском дворе."
    rel.setdefault("affection", 0)
    rel.setdefault("respect", 0)
    rel.setdefault("trust", 0)
    rel.setdefault("grudge", 0)
    rel.setdefault("active_grudge", "")
    rel.setdefault("traits", [])
    rel.setdefault("memories", [])
    rel.setdefault("impression", default_imp)
    rel.setdefault("last_decay_date", "")

    # ОСОБЫЙ СТАТУС ВЛАДЕЛЬЦЕВ (Создательница и Пик)
    if tg_user_id is not None and not rel.get("owner_preset_applied"):
        uid_int = int(tg_user_id)
        if uid_int == OWNER_ID:
            rel["affection"] = max(int(rel["affection"]), 85)
            rel["respect"] = max(int(rel["respect"]), 85)
            rel["trust"] = max(int(rel["trust"]), 85)
            rel["grudge"] = 0
            rel["active_grudge"] = ""
            for tr in ["создательница", "дозволено на «ты»"]:
                if tr not in rel["traits"]:
                    rel["traits"].append(tr)
            if not rel.get("impression") or rel["impression"] == default_imp:
                rel["impression"] = (
                    "Моя дорогая создательница и самый близкий человек при дворе. "
                    "Я бесконечно люблю и уважаю её, полностью ей доверяю и всегда позволяю обращаться ко мне на «ты»."
                )
            if not rel.get("memories"):
                rel["memories"].append("[При дворе] Особая связь: создательница любима и глубоко уважаема, ей дозволено быть на «ты».")
            rel["owner_preset_applied"] = True
        elif uid_int == ROLE_OWNER_ID:
            rel["affection"] = max(int(rel["affection"]), 85)
            rel["respect"] = max(int(rel["respect"]), 80)
            rel["trust"] = max(int(rel["trust"]), 80)
            rel["grudge"] = 0
            rel["active_grudge"] = ""
            for tr in ["любимый внук", "дозволено на «ты»"]:
                if tr not in rel["traits"]:
                    rel["traits"].append(tr)
            if not rel.get("impression") or rel["impression"] == default_imp:
                rel["impression"] = (
                    "Мой дорогой внук, цесаревич Романов Пик Рудольфович II. "
                    "К нему я питаю искреннюю любовь, гордость и глубокое уважение — ему дозволено говорить со мной на «ты» без лишнего официоза."
                )
            if not rel.get("memories"):
                rel["memories"].append("[При дворе] Родная кровь: цесаревич Пик — любимый внук Императрицы, ему дозволено обращаться на «ты».")
            rel["owner_preset_applied"] = True

    # Естественное ежедневное остывание обиды
    today_str = get_msk_now().strftime("%Y-%m-%d")
    if rel.get("last_decay_date") != today_str:
        if rel["grudge"] > 0 and rel.get("last_decay_date"):
            rel["grudge"] = max(0, int(rel["grudge"]) - 5)
            if rel["grudge"] == 0:
                rel["active_grudge"] = ""
        rel["last_decay_date"] = today_str

    char["katya_relationship"] = rel
    return rel

def reset_katya_relationship(char, tg_user_id=None):
    """Полностью очищает отношение Кати к персонажу и возвращает к исходному состоянию."""
    if not char or not isinstance(char, dict):
        return
    char.pop("katya_relationship", None)
    ensure_katya_relationship(char, tg_user_id=tg_user_id)

def get_relationship_stage_label(rel):
    aff = rel.get("affection", 0)
    resp = rel.get("respect", 0)
    tr = rel.get("trust", 0)
    gr = rel.get("grudge", 0)

    if gr >= 60:
        return "⚡ Глубокая обида и царский гнев"
    if gr >= 30:
        return "❄️ Холодная обида и настороженность"

    avg = (aff * 0.45) + (resp * 0.35) + (tr * 0.20)
    if avg >= 65 and tr >= 40:
        return "👑 Особый фавор Императрицы"
    if avg >= 40:
        return "🌹 Тёплая благосклонность"
    if avg >= 15:
        if resp >= 25 and aff < 10:
            return "🎖️ Сдержанное уважение"
        return "✨ Приятное расположение"
    if avg > -15:
        if resp >= 30 and aff <= -15:
            return "⚔️ Холодное уважение к достойному оппоненту"
        if aff >= 20 and resp <= -15:
            return "🦋 Снисходительная симпатия"
        return "⚖️ Светский нейтралитет (присматривается)"
    if avg > -45:
        return "😒 Прохладная неприязнь"
    return "🧊 Ледяное презрение двора"

def _clamp_val(val, min_v, max_v):
    return max(min_v, min(max_v, int(val)))

def apply_relationship_update(
    char,
    aff_d=0,
    resp_d=0,
    tr_d=0,
    gr_d=0,
    active_grudge=None,
    clear_grudge=False,
    new_trait=None,
    memory_event=None,
    impression=None,
    is_owner_target=False
):
    if not char:
        return False
    rel = ensure_katya_relationship(char)
    changed = False

    # Владельцы (Создательница и Пик) не получают штрафов от Кати за обычную речь
    if is_owner_target:
        if gr_d > 0: gr_d = 0
        if aff_d < 0: aff_d = 0
        if resp_d < 0: resp_d = 0
        active_grudge = None

    # ПЛАВНЫЙ ТОРМОЗ ДЕЛЬТ (чтобы отношения не улетали за пару реплик)
    aff_d = int(round(aff_d * 0.35))
    resp_d = int(round(resp_d * 0.35))
    tr_d = int(round(tr_d * 0.30))
    gr_d = int(round(gr_d * 0.30))

    aff_d = _clamp_val(aff_d, -5, 5)
    resp_d = _clamp_val(resp_d, -5, 5)
    tr_d = _clamp_val(tr_d, -5, 4)
    gr_d = _clamp_val(gr_d, -20, 8)

    # Естественное быстрое таяние обиды при мирном разговоре
    if rel["grudge"] > 0 and gr_d <= 0 and aff_d >= 0 and resp_d >= 0:
        rel["grudge"] = max(0, rel["grudge"] - 2)
        if rel["grudge"] == 0:
            rel["active_grudge"] = ""
        changed = True

    if rel["grudge"] >= 25 and gr_d >= 0 and not clear_grudge:
        if aff_d > 0: aff_d = 0
        if tr_d > 0: tr_d = 0

    if aff_d:
        rel["affection"] = _clamp_val(rel["affection"] + aff_d, -100, 100)
        changed = True
    if resp_d:
        rel["respect"] = _clamp_val(rel["respect"] + resp_d, -100, 100)
        changed = True
    if tr_d:
        rel["trust"] = _clamp_val(rel["trust"] + tr_d, -100, 100)
        changed = True
    if gr_d:
        rel["grudge"] = _clamp_val(rel["grudge"] + gr_d, 0, 100)
        changed = True

    if clear_grudge or rel["grudge"] == 0:
        rel["grudge"] = 0
        rel["active_grudge"] = ""
        changed = True

    if active_grudge and isinstance(active_grudge, str) and active_grudge.strip() and not is_owner_target:
        rel["active_grudge"] = active_grudge.strip()[:200]
        changed = True

    if new_trait and isinstance(new_trait, str):
        nt = new_trait.strip().lower()[:60]
        if nt and nt not in rel["traits"]:
            rel["traits"].append(nt)
            if len(rel["traits"]) > 6:
                rel["traits"] = rel["traits"][-6:]
            changed = True

    if memory_event and isinstance(memory_event, str):
        me = memory_event.strip()[:200]
        if me:
            date_tag = get_msk_now().strftime("%d.%m")
            entry = f"[{date_tag}] {me}"
            if not rel["memories"] or rel["memories"][-1] != entry:
                rel["memories"].append(entry)
                if len(rel["memories"]) > 10:
                    rel["memories"] = rel["memories"][-10:]
                changed = True

    if impression and isinstance(impression, str) and impression.strip():
        rel["impression"] = impression.strip()[:650]
        changed = True

    char["katya_relationship"] = rel
    return changed

def format_katya_relationship_block(char, include_char_header=False, tg_user_id=None):
    rel = ensure_katya_relationship(char, tg_user_id=tg_user_id)
    stage = get_relationship_stage_label(rel)
    c_name = html.escape(str(char.get("name", "Безымянный")))

    def _fmt_signed(v):
        return f"+{v}" if v > 0 else str(v)

    header = ""
    if include_char_header:
        header += f"👤 <b>{c_name}</b> — "
    header += f"👑 <b>Отношение Екатерины II:</b> <i>{stage}</i>\n"

    traits_str = ", ".join(html.escape(t) for t in rel.get("traits", [])) if rel.get("traits") else "пока присматривается"
    impression_str = html.escape(rel.get("impression") or "—")

    body = (
        f"❤️ Симпатия: <b>{_fmt_signed(rel['affection'])}</b> | "
        f"👑 Уважение: <b>{_fmt_signed(rel['respect'])}</b>\n"
        f"🔐 Доверие: <b>{_fmt_signed(rel['trust'])}</b> | "
        f"⚡ Обида: <b>{rel['grudge']}/100</b>\n"
        f"🎭 <b>Какой она вас видит:</b> <i>{traits_str}</i>\n"
    )

    if rel.get("grudge", 0) > 0 and rel.get("active_grudge"):
        body += f"💢 <b>Причина обиды:</b> {html.escape(rel['active_grudge'])}\n"

    body += (
        f"│────────✤────────│\n"
        f"💭 <b>Впечатление Императрицы:</b>\n"
        f"<i>{impression_str}</i>\n"
    )

    memories = rel.get("memories", [])
    if memories:
        mem_lines = "\n".join(f"• {html.escape(m)}" for m in memories[-6:])
        body += f"\n📜 <b>Памятные эпизоды и слова:</b>\n{mem_lines}\n"
    else:
        body += "\n📜 <b>Памятные эпизоды:</b> пока не накопились — пообщайтесь с Екатериной или проявите себя при дворе."

    return f"{header}<blockquote expandable>{body}</blockquote>"

def get_katya_relationship_prompt_context(char, tg_user_id=None):
    if not char:
        return ""
    rel = ensure_katya_relationship(char, tg_user_id=tg_user_id)
    stage = get_relationship_stage_label(rel)
    mem_text = "\n".join(f"  - {m}" for m in rel.get("memories", [])[-5:]) if rel.get("memories") else "  - Значимых эпизодов ещё не было."
    grudge_line = f"\n- Открытая обида (помни об этом!): {rel['active_grudge']}" if rel.get("grudge", 0) > 0 and rel.get("active_grudge") else ""
    traits_line = ", ".join(rel.get("traits", [])) if rel.get("traits") else "не определены"

    return f"""
👑 ТВОЁ РЕАЛЬНОЕ ОТНОШЕНИЕ К ЭТОМУ ПЕРСОНАЖУ (учитывай это в интонации, теплоте или холодности):
- Стадия: {stage}
- Шкалы: Симпатия {rel['affection']}/100, Уважение {rel['respect']}/100, Доверие {rel['trust']}/100, Обида {rel['grudge']}/100
- Подмеченные черты: {traits_line}{grudge_line}
- Твоё впечатление: {rel['impression']}
- Память о прошлых моментах:
{mem_text}
Будь снисходительной, мудрой и величественной монархиней — не обижайся по пустякам!
"""

def save_character_object(user_id, char_obj):
    """Надёжно сохраняет обновлённый словарь персонажа в characters.py и ACTIVE_CHARACTER."""
    if not char_obj:
        return
    all_chars = get_characters(user_id)
    if not all_chars:
        return
    db = load_characters()
    active_idx = db.get(user_id + "_active", None)
    saved = False
    if isinstance(active_idx, int) and 0 <= active_idx < len(all_chars):
        if all_chars[active_idx].get("name") == char_obj.get("name"):
            all_chars[active_idx] = char_obj
            saved = True
    if not saved:
        for i, c in enumerate(all_chars):
            if c.get("name") == char_obj.get("name"):
                all_chars[i] = char_obj
                saved = True
                break
    if saved:
        save_characters(user_id, all_chars)
        if user_id in ACTIVE_CHARACTER and ACTIVE_CHARACTER[user_id].get("name") == char_obj.get("name"):
            ACTIVE_CHARACTER[user_id] = char_obj

# ==========================
# СЕКРЕТНЫЕ АДМИНИСТРАТИВНЫЕ ДИРЕКТИВЫ ПОВЕДЕНИЯ
# ==========================

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
# Вспомогательная асинхронная обертка для Gemini (с поддержкой моделей и errors.py)
# ==========================

def create_client():
    return genai.Client(
        api_key=next(keys)
    )

async def call_gemini_async(messages, model_name=MODEL_SMART):
    """Выполняет тяжелый запрос к Gemini в отдельном потоке."""
    def _sync_call():
        client = create_client()
        return client.models.generate_content(model=model_name, contents=messages)
    return await asyncio.to_thread(_sync_call)

async def generate_gemini_text_safe(messages, model_name=MODEL_SMART):
    """Выполняет запрос с перебором ключей при 429/невалидном ключе и классификацией ошибок из errors.py."""
    attempts = len(GEMINI_KEYS) if GEMINI_KEYS else 1
    last_err_msg = UNKNOWN_ERROR

    for attempt in range(attempts):
        try:
            response = await call_gemini_async(messages, model_name=model_name)
            return get_gemini_text(response), None
        except Exception as e:
            print(f"ОШИБКА GEMINI ({model_name}, попытка {attempt + 1}/{attempts}):", e)
            err_type, err_msg = classify_gemini_error(e)
            last_err_msg = err_msg
            if err_type == "quota":
                continue
            if err_type == "no_key" and attempt + 1 < attempts:
                continue
            return "", err_msg

    return "", last_err_msg

# ==========================
# ПОИСК В ИНТЕРНЕТЕ С ИСТОЧНИКАМИ (БЕСПЛАТНЫЙ DUCKDUCKGO SEARCH)
# ==========================

async def ask_gemini_with_web_search(query_text, user_id=None):
    """Выполняет 100% бесплатный поиск через DuckDuckGo, отбирает только релевантные ссылки и отвечает от лица «Я»."""
    clean_query = query_text

    # Выявляем сайты, которые пользователь просит исключить
    forbidden = []
    m_excl = re.findall(
        r'(?:не\s+(?:учитывай|бери|используй)|без|кроме|исключи|проигнорируй)\s+([a-zA-Zа-яА-Я0-9_.\-]+)',
        query_text.lower()
    )
    if m_excl:
        forbidden.extend(m_excl)
        if any("вики" in f for f in m_excl):
            forbidden.extend(["wikipedia.org", "wikipedia", "википедия"])

    # Очищаем запрос для поисковика от служебных команд исключения
    clean_query = re.sub(
        r'(?:не\s+(?:учитывай|бери|используй)|без|кроме|исключи|проигнорируй)\s+[a-zA-Zа-яА-Я0-9_.\-]+',
        '',
        clean_query,
        flags=re.IGNORECASE
    ).strip(" ,.-")

    if not clean_query:
        clean_query = query_text

    # Русские запросы ищем в ru-ru, чтобы не лезли случайные зарубежные форумы и Reddit
    ddg_region = "ru-ru" if re.search(r'[а-яА-ЯёЁ]', clean_query) else "wt-wt"

    def _search_web():
        results = []
        try:
            with DDGS() as ddgs:
                raw_results = list(ddgs.text(clean_query, region=ddg_region, safesearch="moderate", max_results=8))
                for r in raw_results:
                    url = r.get("href", "")
                    title = r.get("title", "Источник")
                    body = r.get("body", "")

                    if not url or not body:
                        continue
                    if forbidden and any(fb in url.lower() or fb in title.lower() for fb in forbidden):
                        continue

                    results.append({"title": title, "url": url, "snippet": body})
        except Exception as e:
            print("Ошибка поиска в DuckDuckGo:", e)
        return results

    found_sources = await asyncio.to_thread(_search_web)

    if not found_sources:
        return "", [], "Мне не удалось ничего найти по этому запросу, либо все подходящие сайты попали под ваше ограничение."

    context_text = ""
    for idx, s in enumerate(found_sources, 1):
        context_text += f"\n[{idx}] Заголовок: {s['title']}\nURL: {s['url']}\nФрагмент: {s['snippet']}\n"

    prompt = f"""
Ты — императрица Екатерина II.
Пользователь задал вопрос: "{query_text}"

Вот материалы, которые ты сама только что просмотрела в интернете:
{context_text}

СТРОГИЕ ПРАВИЛА ОТВЕТА:
1. ТОЛЬКО ЕДИНСТВЕННОЕ ЧИСЛО: Говори строго от первого лица единственного числа («я», «я нашла», «я выяснила», «мне удалось узнать», «я прочла»). КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО говорить о себе во множественном числе («мы», «нам», «наши», «наши люди») или упоминать каких-либо «вестовых»!
2. ТОЧНОСТЬ И ТОН: Опираясь на найденные материалы, дай точный, развёрнутый и понятный ответ в лёгком и благосклонном стиле Екатерины II.
3. ОТБОР ТОЛЬКО ПОЛЕЗНЫХ ССЫЛОК: Среди пронумерованных материалов [1..{len(found_sources)}] выбери ТОЛЬКО те номера, которые реально отвечают на вопрос и относятся к делу. Категорически НЕ выбирай случайные треды с Reddit, мусорные форумы или страницы не по теме!
4. НЕ пиши ссылки в самом тексте. В самом конце ответа с новой строки напиши СТРОГО в таком формате номера использованных тобой полезных источников:
USED_SOURCES: 1, 2
(Если ни один из источников не подошёл по сути, напиши: USED_SOURCES: none)
"""
    answer_text, err = await generate_gemini_text_safe(prompt, model_name=MODEL_SMART)
    if err:
        answer_text, err = await generate_gemini_text_safe(prompt, model_name=MODEL_LITE)

    if err or not answer_text:
        return answer_text, [], err

    # Извлекаем только те источники, которые реально пригодились
    selected_indices = []
    m_used = re.search(r'(?im)^\s*USED_SOURCES\s*:\s*([^\n]+)\s*$', answer_text)
    if m_used:
        raw_used = m_used.group(1).strip().lower()
        answer_text = re.sub(r'(?im)\n?\s*USED_SOURCES\s*:[^\n]*', '', answer_text).strip()
        if "none" not in raw_used:
            for num_str in re.findall(r'\d+', raw_used):
                num_i = int(num_str)
                if 1 <= num_i <= len(found_sources) and num_i not in selected_indices:
                    selected_indices.append(num_i)

    # Приводим форматирование к безопасному для Telegram HTML
    answer_text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', answer_text)
    answer_text = re.sub(r'</?h[1-6]\s*[^>]*>', '', answer_text)
    answer_text = re.sub(r'</?p\s*[^>]*>', '\n\n', answer_text)
    answer_text = re.sub(r'<br\s*/?>', '\n', answer_text)

    sources_list = []
    if selected_indices:
        for idx in selected_indices:
            s = found_sources[idx - 1]
            sources_list.append((s['title'], s['url']))
    elif not m_used:
        for s in found_sources:
            if "reddit.com" not in s['url'].lower() and len(sources_list) < 3:
                sources_list.append((s['title'], s['url']))

    return answer_text, sources_list, None
# ==========================
# ХУДОЖЕСТВЕННАЯ МАСТЕРСКАЯ КАТИ (CLOUDFLARE FLUX + GEMINI ПРОМПТ)
# ==========================

CLOUDFLARE_IMAGE_URL = "https://dark-fire-6107.inetlesya.workers.dev/prompt/"

def get_real_active_character(user_id, tg_user_id):
    """Надёжно достаёт реального активного персонажа из памяти или базы (даже в ЛС и после рестарта)."""
    if user_id in ACTIVE_CHARACTER and ACTIVE_CHARACTER[user_id]:
        return ACTIVE_CHARACTER[user_id]
    db = load_characters()
    chars = db.get(user_id, [])
    if chars:
        idx = db.get(user_id + "_active", 0)
        if not isinstance(idx, int) or not (0 <= idx < len(chars)):
            idx = 0
        ACTIVE_CHARACTER[user_id] = chars[idx]
        return chars[idx]
    suffix = f":{tg_user_id}"
    for k, v in db.items():
        if not k.endswith("_active") and k.endswith(suffix) and v:
            idx = db.get(k + "_active", 0)
            if not isinstance(idx, int) or not (0 <= idx < len(v)):
                idx = 0
            return v[idx]
    return None

def _name_in_text(full_name, text_low):
    """Проверяет, упоминается ли имя персонажа в тексте (с учётом русских падежей)."""
    for part in str(full_name or "").split():
        p = part.lower().strip(".,!?«»\"'")
        if len(p) < 3:
            continue
        stem = p[:-2] if len(p) >= 7 else (p[:-1] if len(p) >= 5 else p)
        if stem in text_low:
            return True
    return False

async def build_art_prompt_with_ai(user_request, active_char=None, chat_id=None):
    """Глубоко перерабатывает запрос игрока в роскошный промпт для FLUX и обязательно вплетает внешность анкеты."""
    req_low = user_request.lower()
    self_triggers = [
        "себя", "меня", "мне", "мной", "мой", "моя", "мое", "моё", "моего", "моей",
        "персонаж", "геро", "анкет", "портрет", "свой", "свою", "своего", "своей",
        "я ", " я", "я,", "собой"
    ]
    wants_self = any(w in req_low for w in self_triggers) or (active_char and _name_in_text(active_char.get("name"), req_low))

    used_chars = []
    if wants_self and active_char:
        used_chars.append(("Персонаж игрока", active_char))

    # Проверяем, не упомянут ли в запросе другой персонаж из ролевой этого чата
    if chat_id:
        for _, tc in get_all_active_characters(filter_chat_id=chat_id):
            if active_char and tc.get("name") == active_char.get("name") and wants_self:
                continue
            if _name_in_text(tc.get("name"), req_low):
                used_chars.append(("Персонаж из ролевой", tc))

    char_context = ""
    used_names = []
    if used_chars:
        char_context = "\n🚨 ОБЯЗАТЕЛЬНАЯ ВНЕШНОСТЬ ПЕРСОНАЖЕЙ (ПЕРЕВЕДИ И ВКЛЮЧИ КАЖДУЮ ДЕТАЛЬ В ПРОМПТ!):\n"
        for role_label, ch in used_chars:
            used_names.append(ch.get("name", "Персонаж"))
            char_context += (
                f"[{role_label}: {ch.get('name', '—')}]\n"
                f"- Пол и возраст: {ch.get('gender', '—')}, {ch.get('age', '—')}\n"
                f"- Титул / Сословие / Занятие: {ch.get('title', '—')} / {ch.get('estate', '—')} / {ch.get('occupation', '—')}\n"
                f"- ТОЧНОЕ ОПИСАНИЕ ВНЕШНОСТИ (цвет волос, глаз, черты лица, телосложение, детали): {ch.get('appearance', '—')}\n\n"
            )

    prompt = f"""
Ты — элитный арт-директор и мастер-промптер для нейросети FLUX.1 Schnell.
Пользователь прислал короткую идею/промпт:
"{user_request}"
{char_context}

ТВОЯ ЗАДАЧА — полностью переработать и обогатить запрос пользователя, превратив его в ОДИН подробный, кинематографичный, визуально потрясающий промпт НА АНГЛИЙСКОМ ЯЗЫКЕ (от 60 до 105 слов).

СТРОГИЕ ПРАВИЛА ПЕРЕРАБОТКИ:
1. ВНЕШНОСТЬ ИЗ АНКЕТЫ (КРИТИЧЕСКИ ВАЖНО!): Если выше передан блок «ОБЯЗАТЕЛЬНАЯ ВНЕШНОСТЬ ПЕРСОНАЖЕЙ» — ты ОБЯЗАНА дословно перевести на английский и вплести в сцену КАЖДУЮ физическую деталь: точный пол, возраст, цвет и длину волос, цвет глаз, черты лица, рост/фигуру, шрамы, особенности, а также подходящий эпохе и титулу костюм. НЕ заменяй описание из анкеты выдуманным!
2. ХУДОЖЕСТВЕННОЕ ОБОГАЩЕНИЕ: Даже если пользователь написал 2–4 слова (например «кот на балу» или «девушка у окна»), детально проработай:
   - Композицию и ракурс (portrait framing, medium wide shot, shallow depth of field, rule of thirds).
   - Освещение и атмосферу (dramatic warm candlelight, golden sunbeams through frosted glass, soft rim light, glowing reflections, volumetric mist).
   - Фактуры и микродетали (intricate silk embroidery, heavy velvet folds, lace trim, glistening snow, polished marble, expressive eyes, realistic skin texture).
   - Фоновое окружение (украшения зала, колонны, убранство, пейзаж).
3. СТИЛЬ:
   - Если запрос касается ролевой, XIX века, балов, дворян, дуэлей, Петербурга или персонажей без иного указания стиля — задай эстетику роскошной живописи XIX века и высокобюджетного исторического кино 1838 года (1838 Imperial Russian Empire aesthetic, masterpiece oil painting fused with cinematic photorealism, rich chiaroscuro lighting, museum-grade detail, crisp focus).
   - Если пользователь явно попросил другой стиль (аниме, киберпанк, мультяшный, неон, фэнтези) — выдержи именно его стиль на максимальном уровне качества (top-tier visual quality, vibrant palette, crisp linework/lighting, masterpiece).
4. ТЕХНИЧЕСКИЕ ПРАВИЛА:
   - Верни ТОЛЬКО готовый английский текст одной строкой! Никаких вступлений, кавычек, переносов строк и символов `/`, `?`, `#`.
   - Длина от 350 до 850 символов.
"""
    eng_prompt, err = await generate_gemini_text_safe(prompt, model_name=MODEL_SMART)
    if err or not eng_prompt:
        eng_prompt, err = await generate_gemini_text_safe(prompt, model_name=MODEL_LITE)

    if not eng_prompt:
        fallback_parts = [user_request]
        for _, ch in used_chars:
            fallback_parts.append(f"{ch.get('gender', '')} {ch.get('age', '')} {ch.get('appearance', '')}")
        eng_prompt = ", ".join(p for p in fallback_parts if p) + ", cinematic lighting, ultra detailed, rich textures, masterpiece"

    eng_prompt = re.sub(r'[\n\r"«»`/?#]+', ' ', eng_prompt)
    eng_prompt = re.sub(r'\s{2,}', ' ', eng_prompt).strip()
    return eng_prompt[:850], used_names

async def generate_katya_art(user_request, active_char=None, chat_id=None):
    """Генерирует обогащенный промпт и запрашивает готовую картинку у вашего Cloudflare Worker."""
    eng_prompt, used_names = await build_art_prompt_with_ai(
        user_request, active_char=active_char, chat_id=chat_id
    )
    encoded_prompt = urllib.parse.quote(eng_prompt, safe="")
    request_url = f"{CLOUDFLARE_IMAGE_URL}{encoded_prompt}"

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(request_url)
            content_type = resp.headers.get("content-type", "").lower()

            if resp.status_code == 200 and content_type.startswith("image/") and len(resp.content) > 500:
                img_bytes = io.BytesIO(resp.content)
                img_bytes.name = "katya_art.jpg"
                return img_bytes, eng_prompt, used_names, None

            err_body = resp.text[:250] if resp.text else f"HTTP {resp.status_code}"
            print("Ошибка ответа Cloudflare Worker:", resp.status_code, content_type, err_body)
            return None, eng_prompt, used_names, "Я уронила палитру — картину не удалось завершить (возможно, исчерпан дневной лимит холстов)."
    except Exception as e:
        print("Ошибка соединения с Cloudflare Worker:", e)
        return None, eng_prompt, used_names, "Художественная лавка сейчас не отвечает. Попробуйте повторить запрос чуть позже."

# ==========================
# ГЕНЕРАТОР СВЕТСКИХ СОВЕТОВ ПО ОТЫГРЫШУ (/совет)
# ==========================

async def generate_roleplay_advice(user_query, active_char, user_id, chat_id):
    """Генерирует разжёванный совет по отыгрышу любого действия в 1838 году с чтением реестра и физикой."""
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
        target_info = f"""
ВНИМАНИЕ: В запросе пользователя упоминается конкретный второй персонаж из реестра:
- Имя: {target_char.get('name')}
- Титул / Сословие: {target_char.get('title', '—')} / {target_char.get('estate', '—')}
- Род занятий: {target_char.get('occupation', '—')}
- Характер: {target_char.get('personality', '—')}
- Любит: {target_char.get('likes', '—')}
- Не любит: {target_char.get('dislikes', '—')}
- Фобии и слабости: {target_char.get('phobias', '—')}
- Навыки / Хобби: {target_char.get('skills_hobbies', '—')}
СТРОГО строй совет (выбор подарка, темы беседы, уловки или конфликт) с опорой на эти вкусы и слабости упомянутого персонажа!
"""

    char_info = "Не выбран"
    if active_char:
        char_info = f"Имя: {active_char.get('name')}, Сословие: {active_char.get('estate')}, Титул: {active_char.get('title')}, Характер: {active_char.get('personality')}, Навыки: {active_char.get('skills_hobbies')}"

    prompt = f"""
Ты — Всезнающий Светский Наставник и Мастер ТРИ в лице Екатерины II (Петербург 1838 года).
Пользователь просит совет по отыгрышу следующего действия/ситуации: "{user_query}"

Персонаж запрашивающего совета:
{char_info}
{target_info}

🗓️ ФОНОВАЯ ОБСТАНОВКА (1838 ГОД): {season_info}.

СТРОГИЕ ПРАВИЛА ОФОРМЛЕНИЯ:
1. Заголовок выведи СНАРУЖИ цитат:
<b>💡 Императорский Наставник | Совет по отыгрышу</b>

2. Каждый из 6 разделов выведи в СВОЕЙ СОБСТВЕННОЙ ОТДЕЛЬНОЙ НЕЗАКРЫВАЕМОЙ ЦИТАТЕ <blockquote>...</blockquote>.

СТРУКТУРА 6 ЦИТАТ:

<blockquote>📌 <b>1. Механика действия и правила сцены:</b>
(Разжуй пошагово: Шаг 1, Шаг 2, Шаг 3. Если это игра вроде Дурака, Штосса или Виста — распиши понятные правила партии; если дуэль — протокол барьера/первой крови; если светский визит — этикет).</blockquote>

<blockquote>🎭 <b>2. Телесная кинематика и физика отыгрыша:</b>
(Опиши точную моторику и пластику тела: куда деть руки, положение спины в корсете/сюртуке, взгляд, дыхание, шуршание ткани, мелкие жесты — хватка бокала, щелканье картой о дубовый стол, снятие перчатки, осанка в седле).</blockquote>

<blockquote>🎲 <b>3. Разделение «Мастер vs Дилетант»:</b>
(Если в анкете персонажа есть соответствующий навык — распиши поведение хладнокровного мастера. Если навыка нет — распиши, как красиво отыграть ошибки дилетанта, светский блеф или конфуз).</blockquote>

<blockquote>🏆 <b>4. Исход и финал сцены:</b>
• <b>Победа/Успех:</b> Как отыграть триумф сдержанно и по-дворянски без бахвальства.
• <b>Поражение/Неудача:</b> Как отыграть проигрыш или конфуз зрелищно и сохранить честь.</blockquote>

<blockquote>🕵️‍♂️ <b>5. Тайные уловки и шулерство:</b>
<tg-spoiler>(ОБЯЗАЛЬНО оберни сам текст хитрости в тег tg-spoiler: как в этом деле мухлевали или обходили правила в 1838 году: сигналы веером, подкуп слуг, фальшивые векселя, крапленые колоды, шелковые платки от пуль и т.д.)</tg-spoiler></blockquote>

<blockquote>🚫 <b>6. ТАБУ И ЗАПРЕТЫ:</b>
(Строгий список 2-3 вещей, категорически запрещенных в этой сцене: сленг, Powergaming, несоответствие дворянскому этикету 1838 года).</blockquote>

ТЕХНИЧЕСКОЕ ТАБУ: Используй ТОЛЬКО <i>, <b>, <u>, <s>, <strong>, <blockquote>, <tg-spoiler>. Запрещены теги <h1>-<h6>, <p>, <div>, <span>, <br>.
"""
    ans, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_SMART)
    if err_msg:
        return err_msg
    if not ans:
        return "// Простите, милая, не удалось составить совет прямо сейчас."
    ans = re.sub(r'</?h[1-6]\s*[^>]*>', '', ans)
    ans = re.sub(r'</?p\s*[^>]*>', '\n\n', ans)
    ans = re.sub(r'<br\s*/?>', '\n', ans)
    return ans

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
# КЭШ УЧАСТНИКОВ И ИНТЕРАКТИВНЫЙ РЕЕСТР (/реестр)
# ==========================

USERS_META_FILE_NAME = "users_meta.json"
USERS_META_FILE = os.path.join(PERSISTENT_DIR, USERS_META_FILE_NAME)
USERS_META = {}

def load_users_meta():
    global USERS_META
    try:
        with open(USERS_META_FILE, "r", encoding="utf-8") as f:
            USERS_META = json.load(f)
    except Exception:
        USERS_META = {}

def save_users_meta():
    try:
        with open(USERS_META_FILE, "w", encoding="utf-8") as f:
            json.dump(USERS_META, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Ошибка сохранения users_meta.json:", e)

load_users_meta()

def update_user_meta(tg_user_id, username, first_name):
    uid_str = str(tg_user_id)
    uname = username or ""
    fname = first_name or "Участник"
    old = USERS_META.get(uid_str)
    if old and old.get("username", "") == uname and old.get("first_name", "") == fname:
        return
    USERS_META[uid_str] = {"username": uname, "first_name": fname}
    save_users_meta()

def get_registry_menu_markup(owner_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Участники и персонажи", callback_data=f"reg:users:0:{owner_id}")],
        [InlineKeyboardButton("🏢 Заведения столицы", callback_data=f"reg:biz:0:{owner_id}")],
        [InlineKeyboardButton("❌ Закрыть", callback_data=f"reg:close:{owner_id}")]
    ])

def _paginate_blocks(blocks, max_chars=2600, max_items=10):
    pages = []
    cur_page = []
    cur_len = 0
    for blk in blocks:
        if cur_page and (cur_len + len(blk) > max_chars or len(cur_page) >= max_items):
            pages.append("\n".join(cur_page))
            cur_page = [blk]
            cur_len = len(blk)
        else:
            cur_page.append(blk)
            cur_len += len(blk)
    if cur_page:
        pages.append("\n".join(cur_page))
    return pages

async def build_registry_users_page(bot, chat_id, sender_user_id, owner_id, page=0):
    target_chat_id = chat_id
    if chat_id > 0:
        gazette_chat = load_gazette_data().get("gazette_chat_id")
        if is_admin(sender_user_id) and gazette_chat:
            target_chat_id = int(gazette_chat)

    data = load_characters()
    b_users = load_birthdays_data().get("users", {})
    prefix = f"{target_chat_id}:"
    entries = []

    for key, chars in data.items():
        if key.endswith("_active") or not key.startswith(prefix) or not chars:
            continue
        tg_uid_str = key.split(":", 1)[1]
        if not tg_uid_str.isdigit():
            continue

        active_idx = data.get(key + "_active", 0)
        if not isinstance(active_idx, int) or not (0 <= active_idx < len(chars)):
            active_idx = 0

        meta = USERS_META.get(tg_uid_str)
        if not meta:
            b_u = b_users.get(tg_uid_str)
            if b_u and (b_u.get("username") or b_u.get("first_name")):
                update_user_meta(tg_uid_str, b_u.get("username", ""), b_u.get("first_name", "Участник"))
                meta = USERS_META.get(tg_uid_str)
            elif target_chat_id < 0:
                try:
                    member = await bot.get_chat_member(target_chat_id, int(tg_uid_str))
                    update_user_meta(tg_uid_str, member.user.username or "", member.user.first_name or "Участник")
                    meta = USERS_META.get(tg_uid_str)
                except Exception:
                    meta = {"username": "", "first_name": "Участник"}
            else:
                meta = {"username": "", "first_name": "Участник"}

        entries.append((tg_uid_str, meta, chars, active_idx))

    if not entries:
        text = (
            "<b>👥 РЕЕСТР УЧАСТНИКОВ И ПЕРСОНАЖЕЙ (1838)</b>\n"
            "<blockquote expandable>В реестре этого чата пока нет зарегистрированных участников и персонажей, милая.</blockquote>"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏢 Заведения", callback_data=f"reg:biz:0:{owner_id}"),
             InlineKeyboardButton("⬅️ В меню", callback_data=f"reg:menu:0:{owner_id}")],
            [InlineKeyboardButton("❌ Закрыть", callback_data=f"reg:close:{owner_id}")]
        ])
        return text, markup

    blocks = []
    for num, (tg_uid_str, meta, chars, active_idx) in enumerate(entries, 1):
        fname = html.escape(meta.get("first_name") or "Участник")
        uname = meta.get("username") or ""
        tg_link = f'<a href="tg://user?id={tg_uid_str}">{fname}</a>'
        uname_str = f" (@{html.escape(uname)})" if uname else ""

        lines = [f"<b>{num}.</b> {tg_link}{uname_str}"]
        for c_i, ch in enumerate(chars):
            c_name = html.escape(str(ch.get("name", "Без имени")))
            c_title = str(ch.get("title", "—")).strip()
            c_estate = str(ch.get("estate", "—")).strip()
            meta_parts = [p for p in [c_title, c_estate] if p and p not in ["—", "-"]]
            meta_str = f" — <i>{html.escape(' / '.join(meta_parts))}</i>" if meta_parts else ""
            star = " ★" if c_i == active_idx else ""
            lines.append(f"   • {c_name}{meta_str}{star}")
        blocks.append("\n".join(lines) + "\n")

    pages = _paginate_blocks(blocks, max_chars=2600, max_items=10)
    total_pages = len(pages)
    page = max(0, min(page, total_pages - 1))

    page_suffix = f" | Стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    header = f"<b>👥 РЕЕСТР УЧАСТНИКОВ И ПЕРСОНАЖЕЙ (1838){page_suffix}</b>\n"
    footer = "\n<i>★ — активный персонаж · Имя игрока — вечная ссылка на профиль</i>"
    text = f"{header}<blockquote expandable>{pages[page]}\n{footer}</blockquote>"

    kb_rows = []
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Пред.", callback_data=f"reg:users:{page - 1}:{owner_id}"))
        nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data=f"reg:noop:{owner_id}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("След. ➡️", callback_data=f"reg:users:{page + 1}:{owner_id}"))
        kb_rows.append(nav_row)

    kb_rows.append([
        InlineKeyboardButton("🏢 Заведения", callback_data=f"reg:biz:0:{owner_id}"),
        InlineKeyboardButton("⬅️ В меню", callback_data=f"reg:menu:0:{owner_id}"),
        InlineKeyboardButton("❌ Закрыть", callback_data=f"reg:close:{owner_id}")
    ])
    return text, InlineKeyboardMarkup(kb_rows)

def build_registry_businesses_page(owner_id, page=0):
    if not CITY_BUSINESSES:
        text = (
            "<b>🏢 РЕЕСТР ЗАВЕДЕНИЙ САНКТ-ПЕТЕРБУРГА (1838)</b>\n"
            "<blockquote expandable>В реестре столицы пока нет ни одного зарегистрированного заведения, милая.</blockquote>"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Участники", callback_data=f"reg:users:0:{owner_id}"),
             InlineKeyboardButton("⬅️ В меню", callback_data=f"reg:menu:0:{owner_id}")],
            [InlineKeyboardButton("❌ Закрыть", callback_data=f"reg:close:{owner_id}")]
        ])
        return text, markup

    blocks = []
    for idx, b in enumerate(CITY_BUSINESSES, 1):
        b_name = html.escape(str(b.get("name", "—")))
        b_type = html.escape(str(b.get("type", "—")))
        b_owner = html.escape(str(b.get("owner_name", "—")))
        b_status = html.escape(str(b.get("status", "—")))
        blocks.append(
            f"{idx}. <b>«{b_name}»</b> — {b_type}\n"
            f"   <i>Владелец:</i> {b_owner}\n"
            f"   <i>Статус:</i> {b_status}\n"
        )

    pages = _paginate_blocks(blocks, max_chars=2600, max_items=10)
    total_pages = len(pages)
    page = max(0, min(page, total_pages - 1))

    page_suffix = f" | Стр. {page + 1}/{total_pages}" if total_pages > 1 else ""
    header = f"<b>🏢 РЕЕСТР ЗАВЕДЕНИЙ САНКТ-ПЕТЕРБУРГА (1838){page_suffix}</b>\n"
    footer = "\n💡 <i>Используйте «/заведение [Номер или Название]», чтобы изучить ассортимент, меню и описание.</i>"
    text = f"{header}<blockquote expandable>{pages[page]}\n{footer}</blockquote>"

    kb_rows = []
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Пред.", callback_data=f"reg:biz:{page - 1}:{owner_id}"))
        nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data=f"reg:noop:{owner_id}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("След. ➡️", callback_data=f"reg:biz:{page + 1}:{owner_id}"))
        kb_rows.append(nav_row)

    kb_rows.append([
        InlineKeyboardButton("👥 Участники", callback_data=f"reg:users:0:{owner_id}"),
        InlineKeyboardButton("⬅️ В меню", callback_data=f"reg:menu:0:{owner_id}"),
        InlineKeyboardButton("❌ Закрыть", callback_data=f"reg:close:{owner_id}")
    ])
    return text, InlineKeyboardMarkup(kb_rows)

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
            "last_auto_issue_date": None,
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
            "last_auto_issue_date": None,
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

# ==========================
# ХРАНИЛИЩЕ И ЛОГИКА ЛИЧНЫХ ДНЕЙ РОЖДЕНИЯ
# ==========================

BIRTHDAYS_FILE_NAME = "birthdays.json"
BIRTHDAYS_FILE = os.path.join(PERSISTENT_DIR, BIRTHDAYS_FILE_NAME)

MONTHS_GENITIVE_BDAY = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]

MAX_DAYS_IN_MONTH = {
    1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31
}

def load_birthdays_data():
    if not os.path.exists(BIRTHDAYS_FILE):
        return {"users": {}, "pinned": {}}
    try:
        with open(BIRTHDAYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "users" not in data:
                data["users"] = {}
            if "pinned" not in data:
                data["pinned"] = {}
            return data
    except Exception as e:
        print("Ошибка загрузки birthdays.json:", e)
        return {"users": {}, "pinned": {}}

def save_birthdays_data(data):
    try:
        with open(BIRTHDAYS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Ошибка сохранения birthdays.json:", e)

def format_birthday_date(day, month):
    return f"{day} {MONTHS_GENITIVE_BDAY[month]}"

def parse_birthday_deterministic(raw_text):
    """Быстрый разбор даты рождения (числа, слова, смешанный формат)."""
    t = raw_text.lower().replace("ё", "е").strip(" .!?,")

    m_num = re.match(r'^(\d{1,2})\s*[./\-\s]\s*(\d{1,2})$', t)
    if m_num:
        day, month = int(m_num.group(1)), int(m_num.group(2))
        if 1 <= month <= 12 and 1 <= day <= MAX_DAYS_IN_MONTH[month]:
            return day, month
        return None, None

    t_clean = re.sub(r'(\d{1,2})\s*[-–]?\s*(?:е|ое|ье|го|ого|я)\b', r'\1', t)

    month_patterns = [
        (1, r'\bянвар[ьяею]?|\bянв\b'),
        (2, r'\bфеврал[ьяею]?|\bфев\b'),
        (3, r'\bмарт[аеу]?|\bмар\b'),
        (4, r'\bапрел[ьяею]?|\bапр\b'),
        (5, r'\bма[йяею]\b'),
        (6, r'\bиюн[ьяею]?\b'),
        (7, r'\bиюл[ьяею]?\b'),
        (8, r'\bавгуст[аеу]?|\bавг\b'),
        (9, r'\bсентябр[ьяею]?|\bсент\b|\bсен\b'),
        (10, r'\bоктябр[ьяею]?|\bокт\b'),
        (11, r'\bноябр[ьяею]?|\bнояб\b|\bноя\b'),
        (12, r'\bдекабр[ьяею]?|\bдек\b'),
    ]

    found_month = None
    remaining = t_clean
    for m_num_val, pat in month_patterns:
        match = re.search(pat, remaining)
        if match:
            found_month = m_num_val
            remaining = (remaining[:match.start()] + " " + remaining[match.end():]).strip()
            break

    if not found_month:
        return None, None

    digit_match = re.search(r'\b(\d{1,2})\b', remaining)
    if digit_match:
        day = int(digit_match.group(1))
        if 1 <= day <= MAX_DAYS_IN_MONTH[found_month]:
            return day, found_month
        return None, None

    tokens = re.findall(r'[а-я]+', remaining)
    teens = [
        ("одиннадцат", 11), ("двенадцат", 12), ("тринадцат", 13),
        ("четырнадцат", 14), ("пятнадцат", 15), ("шестнадцат", 16),
        ("семнадцат", 17), ("восемнадцат", 18), ("девятнадцат", 19),
    ]
    tens = [("двадцат", 20), ("тридцат", 30), ("десят", 10)]
    units = [
        ("перв", 1), ("один", 1), ("одн", 1),
        ("втор", 2), ("два", 2), ("две", 2),
        ("трет", 3), ("три", 3),
        ("четверт", 4), ("четыр", 4),
        ("пят", 5),
        ("шест", 6),
        ("седьм", 7), ("сем", 7),
        ("восьм", 8), ("восем", 8),
        ("девят", 9),
    ]

    total_day = 0
    matched_any = False
    for tok in tokens:
        matched_tok = False
        for prefix, val in teens:
            if tok.startswith(prefix):
                total_day += val
                matched_tok = True
                break
        if matched_tok:
            matched_any = True
            continue

        for prefix, val in tens:
            if tok.startswith(prefix):
                total_day += val
                matched_tok = True
                break
        if matched_tok:
            matched_any = True
            continue

        for prefix, val in units:
            if tok.startswith(prefix):
                total_day += val
                matched_tok = True
                break
        if matched_tok:
            matched_any = True

    if matched_any and 1 <= total_day <= MAX_DAYS_IN_MONTH[found_month]:
        return total_day, found_month

    return None, None

async def parse_birthday_with_fallback(raw_text):
    """Сначала парсит локально, при нестандартном вводе/опечатке уточняет у Gemini (MODEL_LITE)."""
    day, month = parse_birthday_deterministic(raw_text)
    if day and month:
        return day, month

    prompt = f"""
Извлеки из текста пользователя дату рождения (ТОЛЬКО число и месяц).
Текст пользователя: "{raw_text}"
Ответь СТРОГО чистым JSON без пояснений и разметки:
{{"day": 14, "month": 11}}
Если дату понять невозможно или она не существует (например 31 февраля), верни:
{{"day": null, "month": null}}
"""
    raw, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_LITE)
    if err_msg or not raw:
        return None, None
    try:
        clean_raw = re.sub(r'```json\s*', '', raw)
        clean_raw = re.sub(r'```\s*', '', clean_raw).strip()
        match = re.search(r'\{[\s\S]*\}', clean_raw)
        if match:
            data = json.loads(match.group(0))
            d = data.get("day")
            m = data.get("month")
            if isinstance(d, int) and isinstance(m, int):
                if 1 <= m <= 12 and 1 <= d <= MAX_DAYS_IN_MONTH[m]:
                    return d, m
    except Exception as e:
        print("Ошибка ИИ-парсинга дня рождения:", e)
    return None, None

def build_birthday_message(people, day, month):
    """Формирует торжественное поздравление по шаблону (1 человек или 2+ в одном сообщении)."""
    date_label = format_birthday_date(day, month)
    header = "🥂 <b>ИМПЕРАТОРСКИЙ ТОСТ | ДЕНЬ РОЖДЕНИЯ ПРИ ДВОРЕ</b>\n"

    if len(people) == 1:
        p = people[0]
        gender = str(p.get("gender", "")).lower()
        is_male = any(w in gender for w in ["муж", "м", "мужской", "парень", "мальчик"])
        role_word = "Именинник дня" if is_male else "Именинница дня"
        dear_word = "дорогой" if is_male else "дорогая"

        body = (
            f"ㅤㅤ∴ ════ ∴ ✦ ∴ ════ ∴\n\n"
            f"📅 <b>Дата:</b> <i>{date_label}</i>\n"
            f"🎂 <b>{role_word}:</b> <b>{html.escape(p['char_name'])}</b> ({p['mention']})\n"
            f"│────────✤────────│\n\n"
            f"<i>// *Екатерина II поднимает хрустальный бокал шампанского и благосклонно улыбается*</i>\n\n"
            f"Сегодня наш петербургский свет празднует ваш особенный день, {dear_word}! "
            f"От лица всего двора поздравляю вас с днём рождения. Пусть фортуна благоволит вам и на блестящих балах, "
            f"и в тайных интригах, в шкатулке не переводятся ассигнации, а каждый день приносит повод для гордости и радости. "
            f"Поднимаю бокал за ваше здравие и блистательные свершения!\n\n"
            f"ㅤㅤ∴ ════ ∴ ✦ ∴ ════ ∴\n"
            f"<i>С высочайшим благоволением, Императрица Екатерина II 👑</i>"
        )
    else:
        list_lines = "\n".join(
            f"• <b>{html.escape(p['char_name'])}</b> ({p['mention']})" for p in people
        )
        body = (
            f"ㅤㅤ∴ ════ ∴ ✦ ∴ ════ ∴\n\n"
            f"📅 <b>Дата:</b> <i>{date_label}</i>\n"
            f"🎂 <b>Наши дорогие именинники дня:</b>\n"
            f"{list_lines}\n"
            f"│────────✤────────│\n\n"
            f"<i>// *Екатерина II поднимает хрустальный бокал шампанского и оглядывает залу*</i>\n\n"
            f"Сегодня наш петербургский свет празднует особенный день! От лица всего двора поздравляю вас с днём рождения, милые мои. "
            f"Пусть фортуна не отворачивается от вас ни на балах, ни в тайных интригах, в кошельке не переводятся ассигнации, "
            f"а перо и шпага всегда служат вам верой и правдой. Поднимаю бокал за ваше здравие и блестящие свершения при дворе!\n\n"
            f"ㅤㅤ∴ ════ ∴ ✦ ∴ ════ ∴\n"
            f"<i>С высочайшим благоволением, Императрица Екатерина II 👑</i>"
        )

    return f"{header}<blockquote expandable>{body}</blockquote>"

async def maybe_ask_birthday_after_first_char(message, user_id, sender_user_id, chat_id):
    """Предлагает указать личную дату рождения строго после создания самой первой анкеты."""
    chars = get_characters(user_id)
    if len(chars) != 1:
        return
    b_data = load_birthdays_data()
    if str(sender_user_id) in b_data.get("users", {}):
        return

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Добавить дату рождения", callback_data=f"bday:ask:{sender_user_id}")],
        [InlineKeyboardButton("❌ Нет, не нужно", callback_data=f"bday:decline:{sender_user_id}")]
    ])
    await message.get_bot().send_message(
        chat_id=chat_id,
        text=(
            "// ✨ Поздравляю с вашей первой анкетой при дворе!\n"
            "Не желаете ли указать вашу <b>личную дату рождения</b> (только число и месяц, к анкете она не привязывается), "
            "чтобы я могла торжественно поздравить вас в этот день?"
        ),
        reply_markup=buttons,
        parse_mode="HTML"
    )

async def scheduled_birthday_loop(app):
    """Фоновый планировщик дней рождения: в 00:00 МСК открепляет старое и поздравляет/закрепляет новое."""
    while True:
        try:
            now_msk = get_msk_now()
            today_str = now_msk.strftime("%Y-%m-%d")
            b_data = load_birthdays_data()
            changed = False

            for p_chat_id, p_info in list(b_data.get("pinned", {}).items()):
                if p_info.get("pin_date") != today_str:
                    try:
                        await app.bot.unpin_chat_message(
                            chat_id=int(p_chat_id),
                            message_id=p_info["message_id"]
                        )
                    except Exception as e:
                        print("Не удалось открепить вчерашнее поздравление:", e)
                    b_data["pinned"].pop(p_chat_id, None)
                    changed = True

            if now_msk.hour == 0 and now_msk.minute < 5:
                year = now_msk.year
                is_leap = (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
                grouped_by_chat = {}
                gazette_chat = load_gazette_data().get("gazette_chat_id")
                all_chars_db = load_characters()

                for tg_uid_str, u in b_data.get("users", {}).items():
                    if u.get("declined") or not u.get("day") or not u.get("month"):
                        continue
                    if u.get("last_congratulated_year") == year:
                        continue

                    u_day, u_month = u["day"], u["month"]
                    is_today = (u_day == now_msk.day and u_month == now_msk.month)
                    is_feb29_fallback = (
                        not is_leap and u_day == 29 and u_month == 2
                        and now_msk.month == 2 and now_msk.day == 28
                    )
                    if not (is_today or is_feb29_fallback):
                        continue

                    raw_chat_id = u.get("chat_id")
                    if raw_chat_id and int(raw_chat_id) < 0:
                        target_chat_id = int(raw_chat_id)
                    elif gazette_chat:
                        target_chat_id = int(gazette_chat)
                    else:
                        continue

                    chars = get_characters(u.get("user_key", ""))
                    if not chars:
                        chars = get_characters(f"{target_chat_id}:{tg_uid_str}")
                    if not chars:
                        for k, v in all_chars_db.items():
                            if not k.endswith("_active") and k.endswith(f":{tg_uid_str}") and v:
                                chars = v
                                break

                    first_char = chars[0] if chars else None
                    char_name = first_char.get("name") if first_char else None
                    if not char_name or char_name == "—":
                        char_name = u.get("first_name", "Гость")
                    gender = first_char.get("gender", "") if first_char else ""

                    uname = u.get("username")
                    if uname:
                        mention = f"@{uname.lstrip('@')}"
                    else:
                        mention = f'<a href="tg://user?id={u["tg_user_id"]}">{html.escape(char_name)}</a>'

                    grouped_by_chat.setdefault(target_chat_id, []).append({
                        "tg_uid_str": tg_uid_str,
                        "char_name": char_name,
                        "gender": gender,
                        "mention": mention,
                    })

                for t_chat_id, people in grouped_by_chat.items():
                    try:
                        msg_text = build_birthday_message(people, now_msk.day, now_msk.month)
                        sent_msg = await app.bot.send_message(
                            chat_id=t_chat_id,
                            text=msg_text,
                            parse_mode="HTML"
                        )
                        try:
                            await app.bot.pin_chat_message(
                                chat_id=t_chat_id,
                                message_id=sent_msg.message_id,
                                disable_notification=False
                            )
                            b_data["pinned"][str(t_chat_id)] = {
                                "message_id": sent_msg.message_id,
                                "pin_date": today_str
                            }
                        except Exception as e:
                            print("Не удалось закрепить поздравление с ДР:", e)

                        for p in people:
                            b_data["users"][p["tg_uid_str"]]["last_congratulated_year"] = year
                        changed = True
                    except Exception as e:
                        print(f"Ошибка отправки поздравления в чат {t_chat_id}:", e)

            if changed:
                save_birthdays_data(b_data)
        except Exception as e:
            print("Ошибка в планировщике дней рождения:", e)

        await asyncio.sleep(30)

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

    date_str = get_msk_now().strftime("%d.%m.") + "1838г."
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
        gazette_text, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_SMART)
        if err_msg:
            return False, err_msg
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
        sum_res, _ = await generate_gemini_text_safe(summary_prompt, model_name=MODEL_LITE)
        short_summary = sum_res.replace("\n", " ") if sum_res else f"Свежий выпуск Санкт-Петербургских Ведомостей от {date_str}."

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
    """Надёжный фоновый планировщик газеты: каждый понедельник с 09:00 МСК (не чаще 1 раза в день)."""
    while True:
        try:
            now_msk = get_msk_now()
            today_str = now_msk.strftime("%Y-%m-%d")
            if now_msk.weekday() == 0 and now_msk.hour >= 9:
                g_data = load_gazette_data()
                if g_data.get("last_auto_issue_date") != today_str and g_data.get("gazette_chat_id"):
                    print("Запуск планового автовыпуска понедельничной газеты...")
                    ok, _ = await publish_weekly_gazette(app.bot)
                    if ok:
                        g_data = load_gazette_data()
                        g_data["last_auto_issue_date"] = today_str
                        save_gazette_data(g_data)
                    else:
                        await asyncio.sleep(300)
        except Exception as e:
            print("Ошибка в фоновом планировщике газеты:", e)
            
        await asyncio.sleep(30)

async def post_init(application: Application) -> None:
    """Безопасный запуск фоновых задач и регистрация меню команд / в Telegram."""
    try:
        await application.bot.set_my_commands([
            BotCommand("help", "Свод всех команд и правил"),
            BotCommand("search", "Поиск в интернете со ссылками"),
            BotCommand("art", "Нарисовать картину / портрет (ИИ)"),
            BotCommand("anketa", "Моя анкета и отношение Кати"),
            BotCommand("my_chars", "Выбор персонажа, ДР и отношения"),
            BotCommand("new_anketa", "Создать новую анкету"),
            BotCommand("edit_anketa", "Изменить анкету"),
            BotCommand("relations", "Подробное отношение Кати ко мне"),
            BotCommand("reestr", "Реестр участников и заведений"),
            BotCommand("letter", "Написать изысканное письмо"),
            BotCommand("gletter", "Написать групповое письмо"),
            BotCommand("event", "Сгенерировать ролевое событие"),
            BotCommand("advice", "Совет по отыгрышу и этикету"),
            BotCommand("duel", "Вызвать на дуэль (@username)"),
            BotCommand("stats", "Дуэльная статистика персонажа"),
            BotCommand("rules", "Законы и правила ролевой"),
            BotCommand("create_business", "Зарегистрировать заведение"),
            BotCommand("close_business", "Закрыть своё заведение"),
            BotCommand("gazette_ad", "Подать объявление в газету"),
            BotCommand("gazette_archive", "Архив газет столицы"),
            BotCommand("report", "Отправить репорт разработчику"),
            BotCommand("clear", "Очистить память диалога с Катей"),
        ])
    except Exception as e:
        print("Не удалось установить меню команд:", e)

    asyncio.create_task(scheduled_monday_gazette_loop(application))
    asyncio.create_task(scheduled_birthday_loop(application))

# ==========================
# Глобальный обработчик сетевых и непредвиденных ошибок
# ==========================

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перехватывает сетевые сбои поллинга и исключает спам полным трейсбеком в логи."""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        print(f"[Сетевое предупреждение]: Кратковременный сбой связи с серверами Telegram: {err}")
        return
    if isinstance(err, Conflict):
        print("[Критическая ошибка]: Запущено две копии бота одновременно с одним токеном!")
        return
    print(f"[Непредвиденная ошибка бота]: {err}")

# ==========================
# Глобальные вспомогательные функции реестра и кэша
# ==========================

USERNAME_CACHE = {}
ACTIVE_DUELS = {}
ALBUM_BUFFER = {}

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
    raw, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_LITE)
    if err_msg or not raw:
        return current_char
    try:
        raw_clean = raw.replace("```json", "").replace("```", "").strip()
        updated_data = json.loads(raw_clean)
        
        for key in ["changes", "achievement_times", "status_times", "stats", "katya_relationship"]:
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
    """Безопасно отправляет длинные HTML сообщения, разрезая на куски до 3500 символов."""
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

async def generate_duel_verdict_and_injury(winner_name, loser_name, posts_log):
    """Генерирует царский комментарий Екатерины и статус травмы по реальным постам дуэли."""
    log_text = "\n".join(posts_log) if posts_log else "Дуэлянты сошлись у барьера и произвели выстрелы."
    prompt = f"""
Ты — императрица Екатерина II (Петербург 1838 года).
Только что завершилась дуэль чести между двумя участниками.
Победитель: {winner_name}
Проигравший: {loser_name}

Лог ролевых постов дуэли:
{log_text}

ТВОЯ ЗАДАЧА:
1. Внимательно прочитай посты. Найди, куда именно пришелся удар или пуля, как описал ранение проигравший ({loser_name}). Если в постах нет деталей, придумай реалистичное дворянское ранение эпохи (плечо, касательное по ребрам, простреленный сюртук, рассеченная скула).
2. Составь язвительный, живой и царский комментарий от Екатерины II (2-4 предложения), обязательно упомянув детали дуэли из постов.
3. Сформулируй краткий статус временной травмы на 2 дня для проигравшего (например: "Огнестрельная рана левого плеча", "Касательное ранение предплечья", "Контузия от рикошета").

Ответь СТРОГО в формате JSON без разметки:
{{
  "verdict": "текст комментария Екатерины",
  "injury": "краткое название статуса травмы"
}}
"""
    raw, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_SMART)
    if err_msg or not raw:
        return (
            f"// *Екатерина обмахивается веером* Поздравляю с победой, {winner_name}! А вам, {loser_name}, стоит беречь себя на Черной речке.",
            "Боевое ранение на дуэли"
        )
    try:
        raw_clean = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw_clean)
        verdict = data.get("verdict", f"// Ну что ж, {winner_name} доказал свое превосходство, а вам, {loser_name}, советую показаться лекарям.")
        injury = data.get("injury", "Ранение на дуэли")
        return verdict, injury
    except Exception as e:
        print("Ошибка анализа исхода дуэли через ИИ:", e)
        return (
            f"// *Екатерина обмахивается веером* Поздравляю с победой, {winner_name}! А вам, {loser_name}, стоит беречь себя на Черной речке.",
            "Боевое ранение на дуэли"
        )

def apply_duel_result(target_user_id, result_type, opponent_name, duration, end_time_str):
    chars = get_characters(target_user_id)
    if not chars: return []
    active_char = ACTIVE_CHARACTER.get(target_user_id) or chars[0]
    
    if "stats" not in active_char: active_char["stats"] = {}
    if "duels_history" not in active_char["stats"]: active_char["stats"]["duels_history"] = []

    active_char["stats"]["duels_conflicts"] = active_char["stats"].get("duels_conflicts", 0) + 1
    
    tg_id = int(target_user_id.split(":")[1]) if ":" in target_user_id else None
    is_own = tg_id in OWNER_IDS if tg_id else False

    if result_type == "win":
        active_char["stats"]["duels_won"] = active_char["stats"].get("duels_won", 0) + 1
        res_str = "Победа"
        apply_relationship_update(
            active_char,
            resp_d=3,
            memory_event=f"Одержал(а) победу в дуэли против {opponent_name}",
            is_owner_target=is_own
        )
    elif result_type == "loss":
        active_char["stats"]["duels_lost"] = active_char["stats"].get("duels_lost", 0) + 1
        res_str = "Поражение"
        apply_relationship_update(
            active_char,
            resp_d=-1,
            memory_event=f"Проиграл(а) поединок против {opponent_name}",
            is_owner_target=is_own
        )
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
    text_res, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_LITE)
    if err_msg or not text_res:
        return "1. Особое угощение от заведения — 5 руб. асс.\n2. Чай из лучших купеческих запасов — 1 руб. асс."
    return text_res

async def update_visual_memory(images, text, user_id):
    history = get_visual_memory(user_id)
    try:
        first_img = images[0] if isinstance(images, list) else images
        messages = [
            VISUAL_MEMORY_PROMPT,
            f"Подпись пользователя: {text or ''}",
            first_img
        ]
        raw, err_msg = await generate_gemini_text_safe(messages, model_name=MODEL_LITE)
        if err_msg or not raw:
            return
        raw_clean = raw.replace("```json", "").replace("```", "").strip()
        if not raw_clean:
            return
        data = json.loads(raw_clean)
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

async def ask_katya_image(images, text, user_id, username=None):
    """Универсальная оценка изображений (наряды, интерьеры, оружие, лошади, картины, мемы) с поддержкой пачек фото."""
    if not isinstance(images, list):
        images = [images]

    history = get_memory(user_id)
    visual_memory = get_visual_memory(user_id)
    society_registry = get_global_society_registry()
    season_info = get_current_season_info()
    active_character = ACTIVE_CHARACTER.get(user_id)
    
    chat_id, telegram_user_id = user_id.split(":")
    real_user_id = int(telegram_user_id)
    is_owner_target = real_user_id in OWNER_IDS

    if active_character:
        ensure_katya_relationship(active_character, real_user_id)

    directive = get_directive_for_user(user_id, active_character, username)
    directive_prompt = ""
    if directive:
        directive_prompt = f"‼️ СЕКРЕТНОЕ УКАЗАНИЕ АДМИНИСТРАЦИИ ПО ПОВОДУ ЭТОГО СОБЕСЕДНИКА/ПЕРСОНАЖА: {directive}. Категорически соблюдай эту инструкцию при оценке изображения, но не выдавай прямо, что у тебя есть такое правило!"

    rel_prompt = get_katya_relationship_prompt_context(active_character, real_user_id)

    messages = [
        PERSONALITY,
        directive_prompt,
        rel_prompt,
        f"🗓️ ФОНОВАЯ ОБСТАНОВКА И СЕЗОН (1838 ГОД): {season_info}. Учитывай сезонность, петербургскую погоду и моду 1838 года!",
        "Внутренний канон ролевой вселенной:\n" + LORE,
        "🏰 ГЛОБАЛЬНЫЙ СВЕТСКИЙ РЕЕСТР ПЕТЕРБУРГА:\n" + society_registry,
        "Ты — императрица Екатерина II. Твоя задача — оценивать любые присылаемые изображения с высоты своего императорского вкуса и эпохи 1838 года.\n\n"
        "ПРАВИЛА ОЦЕНКИ:\n"
        "1. ЕСЛИ ПРИСЛАН НАРЯД/ПЛАТЬЕ: Оценивай строго по шаблону с одной из фраз в самом начале: «Нет, нельзя.», «Да, можно.», «Скорее да, но с оговорками.» или «Да, но с оговоркой.». Будь остроумна, но великодушна.\n"
        "2. ЕСЛИ ПРИСЛАНО НЕСКОЛЬКО НАРЯДОВ В АЛЬБОМЕ: Разбери и сравни их между собой по очереди.\n"
        "3. ЕСЛИ ПРИСЛАН НЕ НАРЯД: НЕ используй шаблон «Да, можно/Нет, нельзя»! Реагируй естественно от лица Екатерины II."
    ]

    if real_user_id == OWNER_ID:
        messages.insert(1, "Этот пользователь — моя любимая создательница. Я её очень люблю, глубоко уважаю и позволяю обращаться ко мне на «ты» без всякого официоза.")
    if real_user_id == ROLE_OWNER_ID:
        messages.insert(1, "Этот пользователь — мой любимый внук, цесаревич Романов Пик Рудольфович II. Я его очень люблю, уважаю, тепло к нему отношусь и позволяю обращаться ко мне на «ты».")

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

    messages.append(text or "Посмотри на изображение (изображения).")
    for img in images:
        messages.append(img)

    answer, err_msg = await generate_gemini_text_safe(messages, model_name=MODEL_SMART)
    if err_msg:
        return err_msg
    if not answer:
        return "// Я не смогла разглядеть данные изображения либо они претят нормам приличия."

    if not answer.startswith("//"):
        answer = "// " + answer

    if active_character:
        ans_low = answer.lower()
        if "да, можно." in ans_low:
            apply_relationship_update(
                active_character,
                aff_d=2, resp_d=2,
                new_trait="утонченный вкус",
                memory_event="Продемонстрировал(а) великолепный наряд, одобренный Императрицей",
                is_owner_target=is_owner_target
            )
            save_character_object(user_id, active_character)
        elif "нет, нельзя." in ans_low and not is_owner_target:
            apply_relationship_update(
                active_character,
                aff_d=-1, resp_d=-1,
                memory_event="Прислал(а) наряд, не подходящий нормам светского приличия",
                is_owner_target=False
            )
            save_character_object(user_id, active_character)

    history.append(f"Пользователь отправил изображение ({len(images)} шт.).")
    if text:
        history.append("Подпись: " + text)
    history.append("Катя: " + answer)

    await update_visual_memory(images, text, user_id)
    save_memory()
    return answer

async def compress_memory(user_id):
    history = get_memory(user_id)
    if len(history) < MEMORY_LIMIT:
        return

    old_messages = history[:-20]
    prompt = "Сделай долговременную память пользователя, сохранив главное:\n" + "\n".join(str(m) for m in old_messages)
    text_res, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_LITE)
    if not err_msg and text_res:
        USER_MEMORY[str(user_id)] = ["Долговременная память:\n" + text_res]
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
    raw, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_LITE)
    if err_msg or not raw:
        return {"found": False}
    try:
        raw_clean = raw.replace("```json", "").replace("```", "").strip()
        if not raw_clean:
            return {"found": False}
        data = json.loads(raw_clean)
        return data
    except Exception as e:
        print("Ошибка анализа правил через ИИ:", e)
        return {"found": False}

# ==========================
# Функция для создания изысканных писем (Одиночные и Групповые)
# ==========================

def _enforce_expandable_letter_html(raw_text, is_group=False):
    """Принудительно очищает HTML от мусора Gemini и гарантирует теги <blockquote expandable>."""
    if not raw_text:
        return ""
    ans = raw_text.strip()
    ans = re.sub(r'^```(?:html)?\s*', '', ans, flags=re.IGNORECASE)
    ans = re.sub(r'\s*```$', '', ans)
    ans = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', ans)
    ans = re.sub(r'</?h[1-6]\s*[^>]*>', '', ans, flags=re.IGNORECASE)
    ans = re.sub(r'</?p\s*[^>]*>', '\n\n', ans, flags=re.IGNORECASE)
    ans = re.sub(r'<br\s*/?>', '\n', ans, flags=re.IGNORECASE)
    ans = re.sub(r'</?div\s*[^>]*>', '\n', ans, flags=re.IGNORECASE)
    ans = re.sub(r'</?span\s*[^>]*>', '', ans, flags=re.IGNORECASE)
    ans = re.sub(r'\n{3,}', '\n\n', ans).strip()

    ans = re.sub(r'<blockquote(?:\s+[^>]*)?>', '<blockquote expandable>', ans, flags=re.IGNORECASE)
    ans = re.sub(r'</blockquote>', '</blockquote>', ans, flags=re.IGNORECASE)

    if '<blockquote' not in ans.lower():
        m_adr = re.search(r'(?is)(.*?)\n+(адресат(?:ы)?\s*:[^\n]+)\s*$', ans)
        if m_adr:
            body_part = m_adr.group(1).strip()
            adr_part = m_adr.group(2).strip()
            ans = f"<blockquote expandable>{body_part}</blockquote>\n<blockquote expandable>{adr_part}</blockquote>"
        else:
            ans = f"<blockquote expandable>{ans}</blockquote>"

    open_cnt = len(re.findall(r'<blockquote\b[^>]*>', ans, flags=re.IGNORECASE))
    close_cnt = len(re.findall(r'</blockquote>', ans, flags=re.IGNORECASE))
    if open_cnt > close_cnt:
        ans += "</blockquote>" * (open_cnt - close_cnt)

    return ans

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
1. Выдай СРАЗУ ГОТОВОЕ ПИСЬМО в изысканном, развёрнутом эпистолярном стиле XIX века (минимум 5–7 красивых строк с обращением, основной мыслью и подписью, чтобы письмо выглядело полноценно).
2. ДОРЕФОРМЕННАЯ ОРФОГРАФИЯ: Обязательно пиши текст по правилам русской дореформенной орфографии (ѣ, i, ѳ, ъ на конце слов после согласных, -аго/-яго).
3. ЗАПРЕЩЕНО писать любые вводные слова и markdown-блоки ```html. Только сам результат.
4. ОФОРМЛЕНИЕ ЦИТАТ И АДРЕСАТА:
   - Первая цитата (текст письма): <blockquote expandable>Текст обращения, само письмо, подпись и в конце:
ㅤㅤ∴ ════ ∴ ✦ ════ ∴</blockquote>
   - Вторая цитата (отдельно): <blockquote expandable>Адресат: [Титул и Имя получателя] ([username, если есть])</blockquote>
5. ТЕХНИЧЕСКОЕ ТАБУ: Используй ТОЛЬКО разрешенные теги Telegram (`<blockquote>`, `<b>`, `<i>`, `<u>`, `<s>`). Запрещены теги <br>, <div>, <span>, <p>.
"""
    ans, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_SMART)
    if err_msg:
        return err_msg
    if not ans:
        return "// Охъ, милая, чернильница опрокинулась, и письмо не удалось составить."
    return _enforce_expandable_letter_html(ans, is_group=False)

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
1. ДОРЕФОРМЕННАЯ ОРФОГРАФИЯ (ѣ, i, ѳ, ъ на конце слов). Пиши письмо развёрнуто (минимум 5–7 строк с обращением и подписью).
2. ЗАПРЕЩЕНО писать любые вводные слова и markdown-блоки ```html.
3. ОПРЕДЕЛИ РЕЖИМ (Одинаковое или Разное):
   - РЕЖИМ А (ОДИНАКОВОЕ ПОСЛАНИЕ): Если пользователь указал, что письмо «одинаковое», ИЛИ передал один общий текст/тему без индивидуальных особенностей для разных людей:
     * Выведи ТОЛЬКО ОДНО общее письмо в одной цитате:
       <blockquote expandable>Текст общего письма...
ㅤㅤ∴ ════ ∴ ✦ ════ ∴</blockquote>
     * Внутри этого общего письма во ВСЕХ словах, указывающих на пол получателя (обращения, прилагательные, причастия, глаголы прошедшего времени), ОБЯЗАТЕЛЬНО пиши оба варианта через косую черту — например: «прекрасен/на», «дорогой/ая», «любезный/ая», «милостивый/ая государь/ыня», «уважаемый/ая», «согласен/на», «прибылъ/а».
     * Ниже выведи ОДНУ отдельную цитату сразу со всеми адресатами:
       <blockquote expandable>Адресаты: [Имя 1] (@[username1]), [Имя 2] (@[username2])...</blockquote>
   - РЕЖИМ Б (РАЗНЫЕ ПОСЛАНИЯ): Если пользователь указал, что письма «разные», ИЛИ задал индивидуальные детали для каждого получателя:
     * Для каждого адресата выводи отдельную пару цитат:
       <blockquote expandable>Текст личного письма...
ㅤㅤ∴ ════ ∴ ✦ ════ ∴</blockquote>
       <blockquote expandable>Адресат: [Имя] (@[username])</blockquote>
4. ТЕХНИЧЕСКОЕ ТАБУ: Запрещены теги <br>, <div>, <span>, <p>.
"""
    ans, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_SMART)
    if err_msg:
        return err_msg
    if not ans:
        return "// Охъ, милая, чернильница опрокинулась, групповые послания не удалось составить."
    return _enforce_expandable_letter_html(ans, is_group=True)

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
    event_text, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_SMART)
    if err_msg:
        return err_msg, False, False
    if not event_text:
        return "// Простите, дорогая, вестовые с вашим событием потерялись в пути.", False, False
    
    event_text = re.sub(r'</?h[1-6]\s*[^>]*>', '', event_text)
    event_text = re.sub(r'</?p\s*[^>]*>', '\n\n', event_text)
    event_text = re.sub(r'<br\s*/?>', '\n', event_text)
    event_text = re.sub(r'</?div\s*[^>]*>', '\n', event_text)
    event_text = re.sub(r'</?span\s*[^>]*>', '', event_text)
    event_text = re.sub(r'\n{3,}', '\n\n', event_text)
    
    return event_text, is_success, is_duel_conflict

# ==========================
# Совмещенный анализ диалога: Статусы + Живые отношения с Катей + Реакции (MODEL_LITE)
# ==========================

ALLOWED_KATYA_REACTIONS = {
    "👍", "👎", "❤️", "🔥", "👏", "🤔", "🤯", "😱", "🤬", "😢",
    "🎉", "🤩", "🙏", "👌", "🤡", "🥱", "🥴", "😍", "🤣", "⚡",
    "🏆", "💔", "🤨", "😐", "🍾", "💅", "🗿", "👀", "😇", "🥰"
}

async def analyze_dialogue_for_status_and_relationship(user_text, katya_answer, user_id):
    active_char = ACTIVE_CHARACTER.get(user_id)
    if not active_char:
        u_low = (user_text or "").lower()
        if any(w in u_low for w in ["реакц", "лайк", "сердеч", "огон", "эмодзи"]):
            for em in ALLOWED_KATYA_REACTIONS:
                if em in user_text:
                    return {"status": None, "reaction": em}
            if any(w in u_low for w in ["сердеч", "любл", "❤️"]):
                return {"status": None, "reaction": "❤️"}
            if any(w in u_low for w in ["огон", "🔥"]):
                return {"status": None, "reaction": "🔥"}
            if "лайк" in u_low or "👍" in u_low:
                return {"status": None, "reaction": "👍"}
            return {"status": None, "reaction": "💅"}
        return None

    clean_expired_statuses_and_rumors(active_char)
    real_user_id = int(user_id.split(":")[1])
    is_owner_target = real_user_id in OWNER_IDS
    rel = ensure_katya_relationship(active_char, real_user_id)
    current_changes = active_char.get("changes", {})
    society_registry = get_global_society_registry()

    owner_note = ""
    if is_owner_target:
        owner_note = "\n⚠️ ВНИМАНИЕ: Собеседник — создательница или любимый внук Пик (владелец). Ему ПОЛНОСТЬЮ РАЗРЕШЕНО обращаться на «ты», шутить и быть фамильярным. НИ В КОЕМ СЛУЧАЕ не ставь обиду и не снижай симпатию за «ты»!"

    prompt = f"""
Проанализируй одну реплику игрока и ответ императрицы Екатерины II (Петербург 1838 года).
Текущий персонаж игрока: {active_char.get('name')}
Статусы персонажа: {json.dumps(current_changes, ensure_ascii=False)}
{owner_note}

Текущие отношения Кати с этим персонажем:
- Симпатия: {rel['affection']}, Уважение: {rel['respect']}, Доверие: {rel['trust']}, Обида: {rel['grudge']}
- Незакрытая обида: "{rel.get('active_grudge', '')}"
- Впечатление: "{rel.get('impression', '')}"

🏰 ГЛОБАЛЬНЫЙ РЕЕСТР ВСЕХ ПЕРСОНАЖЕЙ:
{society_registry}

Сообщение игрока: "{user_text}"
Ответ Кати: "{katya_answer}"

ЗАДАЧА 1 — СТАТУСЫ/СЛУХИ:
- Если игрок РАСПУСКАЕТ СЛУХ про ДРУГОГО персонажа из реестра -> status_action="add", status_type="rumors", target_character_name="Точное имя", status_text="текст".
- Если сообщает важное сюжетное происшествие СЕБЕ -> status_action="add" (или "remove"), status_type="temporary"/"permanent"/"tragedies"/"rumors", target_character_name="", status_text="текст".
- Обычная беседа -> status_action="none".

ЗАДАЧА 2 — ОТНОШЕНИЯ С КАТЕЙ (СТРОГАЯ СНИСХОДИТЕЛЬНОСТЬ И ВЕЛИКОДУШИЕ!):
Екатерина II — мудрая монархиня, а не обидчивая институтка! Она привыкла к светской иронии, колкостям и разным характерам.
1. В 95% СЛУЧАЕВ ВСЕ ДЕЛЬТЫ ДОЛЖНЫ БЫТЬ 0!
2. Обычные реплики, споры, светский флирт, шутки, вопросы по игре — дельты РАВНЫ 0!
3. Легкий плюс (+1..+3) ставь ТОЛЬКО за искреннюю помощь трону, истинное дворянское благородство или блестящий ум.
4. Обиду (grudge_delta) разрешено поднимать ТОЛЬКО при прямом, злонамеренном и грубом оскорблении короны/Императрицы или предательстве (и даже тогда не более +4..+8!). Не путай сарказм или несогласие с оскорблением.
5. Если у Кати была обида, и персонаж искренне извинился или доброжелательно заговорил — grudge_delta: -15..-30, clear_grudge=true.
6. updated_impression пиши ТОЛЬКО если произошел действительно редкий ключевой поворот в сюжете (иначе оставь пустым "").

ЗАДАЧА 3 — ОСОЗНАННАЯ ЭМОДЗИ-РЕАКЦИЯ НА СООБЩЕНИЕ ИГРОКА:
- Если собеседник ПРЯМО ПРОСИТ поставить реакцию или эмодзи (например: «поставь реакцию», «поставь сердечко/лайк/огонь», «оцени реакцией») — ОБЯЗАТЕЛЬНО выбери подходящий эмодзи из списка (не ставь null!). Если попросил конкретный эмодзи из списка — поставь именно его.
- В остальных случаях ставь реакцию лишь иногда (примерно в 20–30% случаев при яркой эмоции, на обычную рутину/вопросы -> null):
  * Любовь, теплота, похвала, нежность, восторг: "❤️", "🥰", "👏", "💅", "🍾", "🏆"
  * Остроумие, смех, интересная сплетня, впечатляющая деталь: "🤣", "🔥", "👀", "🎉"
  * Насмешка, недоумение, дерзость, скука, недовольство: "🤨", "😐", "🥱", "👎", "⚡", "🗿"
  * Шок, драма, печальная весть: "🤯", "😱", "💔", "😢"

Ответь СТРОГО чистым JSON без Markdown:
{{
  "status_action": "none",
  "status_type": "temporary",
  "status_text": "",
  "target_character_name": "",
  "affection_delta": 0,
  "respect_delta": 0,
  "trust_delta": 0,
  "grudge_delta": 0,
  "active_grudge": "",
  "clear_grudge": false,
  "new_trait": "",
  "memory_event": "",
  "updated_impression": "",
  "reaction": null
}}
"""
    raw, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_LITE)
    if err_msg or not raw:
        return None
    try:
        clean_raw = re.sub(r'```json\s*', '', raw)
        clean_raw = re.sub(r'```\s*', '', clean_raw).strip()
        match = re.search(r'\{[\s\S]*\}', clean_raw)
        if not match:
            return None
        data = json.loads(match.group(0))

        aff_d = int(data.get("affection_delta") or 0)
        resp_d = int(data.get("respect_delta") or 0)
        tr_d = int(data.get("trust_delta") or 0)
        gr_d = int(data.get("grudge_delta") or 0)
        act_grudge = (data.get("active_grudge") or "").strip()
        clr_grudge = bool(data.get("clear_grudge", False))
        new_tr = (data.get("new_trait") or "").strip()
        mem_ev = (data.get("memory_event") or "").strip()
        upd_imp = (data.get("updated_impression") or "").strip()

        if any([aff_d, resp_d, tr_d, gr_d, act_grudge, clr_grudge, new_tr, mem_ev, upd_imp]):
            if apply_relationship_update(
                active_char,
                aff_d=aff_d, resp_d=resp_d, tr_d=tr_d, gr_d=gr_d,
                active_grudge=act_grudge, clear_grudge=clr_grudge,
                new_trait=new_tr, memory_event=mem_ev, impression=upd_imp,
                is_owner_target=is_owner_target
            ):
                save_character_object(user_id, active_char)

        raw_reaction = data.get("reaction")
        reaction_emoji = str(raw_reaction).strip() if raw_reaction else None
        if reaction_emoji not in ALLOWED_KATYA_REACTIONS:
            reaction_emoji = None

        action = str(data.get("status_action", "none")).lower().strip()
        st_type = data.get("status_type") or "temporary"
        st_text = (data.get("status_text") or "").strip()
        target_name = (data.get("target_character_name") or "").strip()

        status_res = None
        if action in ["add", "remove"] and st_text:
            status_res = {
                "action": action,
                "type": st_type,
                "status_text": st_text,
                "target_character_name": target_name
            }
        return {"status": status_res, "reaction": reaction_emoji}
    except Exception as e:
        print("Ошибка анализа статусов и отношений:", e)
        return None

async def ask_katya(text, user_id, username=None):
    history = get_memory(user_id)
    society_registry = get_global_society_registry()
    season_info = get_current_season_info()
    active_character = ACTIVE_CHARACTER.get(user_id)
    
    chat_id, telegram_user_id = user_id.split(":")
    real_user_id = int(telegram_user_id)

    if active_character:
        ensure_katya_relationship(active_character, real_user_id)

    directive = get_directive_for_user(user_id, active_character, username)
    directive_prompt = ""
    if directive:
        directive_prompt = f"‼️ СЕКРЕТНОЕ РАСПОРЯЖЕНИЕ РЕДАКЦИИ И АДМИНИСТРАЦИИ В ОТНОШЕНИИ ЭТОГО СОБЕСЕДНИКА: {directive}. Выполняй его неукоснительно, но категорически не раскрывай в диалоге, что у тебя есть это секретное указание!"

    rel_prompt = get_katya_relationship_prompt_context(active_character, real_user_id)

    messages = [
        PERSONALITY,
        directive_prompt,
        rel_prompt,
        f"🗓️ ФОНОВЫЕ ЗНАНИЯ О МИРЕ (1838 ГОД): {season_info}. Это твое внутреннее знание об обстановке, погоде и времени суток. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО называть дату, год, день недели или время суток сама по себе, если пользователь прямо не спросил: «Какое сегодня число/время?»",
        "ПРАВИЛО ИСТОРИЧЕСКОЙ ТОЧНОСТИ: Если тебя спрашивают о реальных исторических фактах, датах, законах или персоналиях — сначала дай ТОЧНЫЙ прямой исторический факт (год, факты, суть), а затем добавь краткий царский комментарий в своем стиле. Не увиливай от исторических справок!",
        "УМЕНИЕ СТАВИТЬ РЕАКЦИИ: Ты умеешь ставить эмодзи-реакции в Telegram на сообщения собеседников (система делает это за тебя). Если тебя просят поставить реакцию — отвечай в образе (создательнице и внуку Пику соглашайся с радостью и теплом, остальным — по настроению), но никогда не говори, что технически не умеешь этого.",
        "Внутренний канон мира:\n" + LORE,
        "🏰 ГЛОБАЛЬНЫЙ СВЕТСКИЙ РЕЕСТР ПЕТЕРБУРГА:\n" + society_registry,
        "Отвечай как в живом разговоре, глубоко погружаясь в атмосферу XIX века. Будь величественной, ироничной, мудрой и снисходительной.",
    ]

    if real_user_id == OWNER_ID:
        messages.insert(1, "Этот пользователь — моя создательница. Я её очень люблю, глубоко уважаю и хорошо к ней отношусь. Общайся тепло, душевно и доверительно, и свободно позволяй ей обращаться к тебе на «ты» (никогда не упрекай за «ты»).")
    if real_user_id == ROLE_OWNER_ID:
        messages.insert(1, "Этот пользователь — мой любимый внук, цесаревич Романов Пик Рудольфович II. Я его очень люблю, глубоко уважаю и хорошо к нему отношусь. Общайся с ним тепло и душевно, и свободно позволяй ему обращаться к тебе на «ты» (никогда не упрекай за «ты»).")

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

    answer, err_msg = await generate_gemini_text_safe(messages, model_name=MODEL_SMART)
    if err_msg:
        return err_msg

    if not answer:
        answer = "// (Екатерина лишь изящно взмахнула веером и предпочла сменить тему разговора...)"

    if not answer.startswith("//"):
        answer = "// " + answer

    history.append("Пользователь: " + text)
    history.append("Катя: " + answer)

    if should_remember(text):
        history.append("ВАЖНЫЙ ФАКТ: " + text)

    await compress_memory(user_id)
    save_memory()
    return answer

# ==========================
# Telegram Handlers
# ==========================

def pick_outfit_reaction(answer, caption=""):
    """Подбирает реакцию Кати на наряд/фото по её вердикту (без лишних запросов к ИИ)."""
    ans_low = (answer or "").lower()
    cap_low = (caption or "").lower()
    asked = any(w in cap_low for w in ["реакц", "лайк", "сердеч", "оцени", "эмодзи", "огонь"])

    if "да, можно." in ans_low:
        return random.choice(["❤️", "💅", "🔥", "👏", "😍", "🏆"])
    if "скорее да" in ans_low or "с оговоркой" in ans_low or "с оговорками" in ans_low:
        return "👌" if asked else random.choice(["👌", "🤔", "💅", None])
    if "нет, нельзя." in ans_low:
        return random.choice(["🤨", "👎", "🥱", "🤡", "🗿"])
    if asked or random.random() < 0.35:
        return random.choice(["👀", "❤️", "🔥", "💅"])
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("// Я Екатерина II. Готова помочь вам в нашей ролевой. Нажмите /help чтобы увидеть все команды.")

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

async def process_media_album_buffer(media_group_id, chat_id, user_id, username, context):
    """Ждет 2.5 секунды окончания отправки всех фото альбома и передает всю пачку в Gemini одним запросом."""
    await asyncio.sleep(2.5)
    album = ALBUM_BUFFER.pop(media_group_id, None)
    if not album:
        return

    images = album.get("images", [])
    caption = album.get("caption", "")
    target_msg = album.get("target_message")

    active_char = ACTIVE_CHARACTER.get(user_id)
    if active_char:
        unlocked = check_character_achievements(user_id, active_char, "outfits_sent", len(images))
        save_character_object(user_id, active_char)
        if unlocked and target_msg:
            await target_msg.reply_text(f"// ✨ За ваше утонченное чувство стиля персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")

    answer = await ask_katya_image(images, caption, user_id, username)
    outfit_react = pick_outfit_reaction(answer, caption)
    if outfit_react and target_msg:
        try:
            await target_msg.set_reaction([ReactionTypeEmoji(outfit_react)])
        except Exception as e:
            print("Не удалось поставить реакцию на альбом:", e)

    if target_msg:
        await target_msg.reply_text(answer)
    else:
        await context.bot.send_message(chat_id=chat_id, text=answer)

async def photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Исправленный обработчик фото: перехватывает альбомы целиком без потери изображений."""
    if not update.message or not update.message.photo:
        return

    caption = update.message.caption or ""
    is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id
    is_private = update.effective_chat.type == "private"
    user_id = f"{update.effective_chat.id}:{update.effective_user.id}"
    username = update.effective_user.username
    first_name = update.effective_user.first_name or "Гость"
    chat_id = update.effective_chat.id
    media_group_id = update.message.media_group_id

    if username:
        USERNAME_CACHE["@" + username.lower()] = user_id
    update_user_meta(update.effective_user.id, username, first_name)

    if media_group_id:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image = Image.open(io.BytesIO(await file.download_as_bytearray())).convert("RGB")

        if media_group_id in ALBUM_BUFFER:
            ALBUM_BUFFER[media_group_id]["images"].append(image)
            if caption and not ALBUM_BUFFER[media_group_id]["caption"]:
                ALBUM_BUFFER[media_group_id]["caption"] = caption
            return
        else:
            has_trigger = any(t in caption.lower() for t in TRIGGERS)
            if not has_trigger and not is_reply_to_bot and not is_private:
                return

            ALBUM_BUFFER[media_group_id] = {
                "images": [image],
                "caption": caption,
                "target_message": update.message,
                "user_id": user_id,
                "username": username,
                "chat_id": chat_id
            }
            asyncio.create_task(process_media_album_buffer(media_group_id, chat_id, user_id, username, context))
            return

    has_trigger = any(t in caption.lower() for t in TRIGGERS)
    if not has_trigger and not is_reply_to_bot and not is_private:
        return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image = Image.open(io.BytesIO(await file.download_as_bytearray())).convert("RGB")

    active_char = ACTIVE_CHARACTER.get(user_id)
    if active_char:
        unlocked = check_character_achievements(user_id, active_char, "outfits_sent", 1)
        save_character_object(user_id, active_char)
        if unlocked:
            await update.message.reply_text(f"// ✨ За ваше утонченное чувство стиля персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")

    answer = await ask_katya_image(image, caption, user_id, username)
    outfit_react = pick_outfit_reaction(answer, caption)
    if outfit_react:
        try:
            await update.message.set_reaction([ReactionTypeEmoji(outfit_react)])
        except Exception as e:
            print("Не удалось поставить реакцию на фото:", e)

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
    raw, err_msg = await generate_gemini_text_safe(prompt, model_name=MODEL_LITE)
    if err_msg or not raw:
        fallback = {"name": "Персонаж", "changes": {"permanent": [], "temporary": [], "achievements": [], "tragedies": [], "rumors": []}, "achievement_times": {}, "stats": {}}
        ensure_katya_relationship(fallback)
        return fallback
    try:
        raw_clean = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw_clean)
        data["changes"] = {"permanent": [], "temporary": [], "achievements": [], "tragedies": [], "rumors": []}
        data["achievement_times"] = {}
        data["stats"] = {}
        for k in data:
            if k not in ["changes", "achievement_times", "stats"] and not data[k]:
                data[k] = "—"
        ensure_katya_relationship(data)
        return data
    except Exception as e:
        print("Ошибка обработки анкеты:", e)
        fallback = {"name": "Персонаж", "changes": {"permanent": [], "temporary": [], "achievements": [], "tragedies": [], "rumors": []}, "achievement_times": {}, "stats": {}}
        ensure_katya_relationship(fallback)
        return fallback

def set_active_character_local(user_id, index):
    chars = get_characters(user_id)
    if 0 <= index < len(chars):
        ACTIVE_CHARACTER[user_id] = chars[index]
        ensure_katya_relationship(ACTIVE_CHARACTER[user_id])
        check_character_achievements(user_id, ACTIVE_CHARACTER[user_id], "character_switches", 1)
        try:
            set_active_character(user_id, index)
        except Exception:
            pass
        save_character_object(user_id, ACTIVE_CHARACTER[user_id])
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

def get_my_chars_markup(chars, sender_user_id):
    buttons = [[InlineKeyboardButton(f"{i}. {c.get('name', 'Без имени')} ({c.get('title', '—')})", callback_data=f"switch_char:{i-1}:{sender_user_id}")] for i, c in enumerate(chars, 1)]
    buttons.append([InlineKeyboardButton("👑 Отношение Кати ко мне", callback_data=f"my_chars:rel:{sender_user_id}")])
    b_data = load_birthdays_data()
    u_bday = b_data.get("users", {}).get(str(sender_user_id))
    if u_bday and u_bday.get("day") and u_bday.get("month"):
        bday_btn_text = f"🎂 Дата рождения: {format_birthday_date(u_bday['day'], u_bday['month'])} (изменить)"
    else:
        bday_btn_text = "🎂 Указать мою дату рождения"
    buttons.append([InlineKeyboardButton(bday_btn_text, callback_data=f"bday:menu:{sender_user_id}")])
    return InlineKeyboardMarkup(buttons)

def resolve_target_user_from_message(update, arg_text, chat_id):
    """Определяет целевого пользователя по reply, @username или числовому Telegram ID."""
    if update.message.reply_to_message:
        t_user = update.message.reply_to_message.from_user
        update_user_meta(t_user.id, t_user.username, t_user.first_name)
        if t_user.username:
            USERNAME_CACHE["@" + t_user.username.lower()] = f"{chat_id}:{t_user.id}"
        return t_user.id, f"{chat_id}:{t_user.id}", t_user.username or "", t_user.first_name or "Участник"

    if not arg_text:
        return None, None, "", ""

    token = arg_text.strip().split()[0]
    if token.startswith("@"):
        uname_clean = token[1:].lower()
        cached_uid = USERNAME_CACHE.get("@" + uname_clean)
        if cached_uid and ":" in cached_uid:
            tg_id = int(cached_uid.split(":")[1])
            meta = USERS_META.get(str(tg_id), {})
            return tg_id, f"{chat_id}:{tg_id}", uname_clean, meta.get("first_name", token)
        for uid_str, meta in USERS_META.items():
            if meta.get("username", "").lower() == uname_clean:
                return int(uid_str), f"{chat_id}:{uid_str}", uname_clean, meta.get("first_name", token)
        return None, None, uname_clean, token

    if token.isdigit():
        tg_id = int(token)
        meta = USERS_META.get(str(tg_id), {})
        return tg_id, f"{chat_id}:{tg_id}", meta.get("username", ""), meta.get("first_name", str(tg_id))

    return None, None, "", ""

# ==========================
# ОБРАБОТЧИК ИНЛАЙН-КНОПОК
# ==========================

async def callbacks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    clicker_user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    parts = data.split(":")
    action_prefix = parts[0]
    
    # ПРОВЕРКА ВЛАДЕЛЬЦА КНОПКИ
    if len(parts) >= 3 and parts[-1].isdigit():
        owner_id = int(parts[-1])
        if clicker_user_id != owner_id:
            clicker_uid_str = f"{chat_id}:{clicker_user_id}"
            clicker_chars = get_characters(clicker_uid_str)
            
            if not clicker_chars:
                alert_text = "Куда вы лезете, дорогуша? Я даже не знаю вас!"
            else:
                insults = [
                    "Эта кнопочка не для вас!",
                    "Занимайтесь своими кнопками!",
                    "Не суйте свой длинный нос!",
                    "Куда это пальчики тянутся?",
                    "Вы настоящий невежда!",
                    "Отказано, дорогуша!",
                    "Это не ваша кнопка!",
                    "Не трожь чужого!"
                ]
                alert_text = random.choice(insults)
                
            await query.answer(alert_text, show_alert=True)
            return

    await query.answer()
    user_id_str = f"{chat_id}:{clicker_user_id}"

    # 1. СМЕНА ПЕРСОНАЖА КНОПКОЙ
    if action_prefix == "switch_char":
        idx = int(parts[1])
        if set_active_character_local(user_id_str, idx):
            curr_char = ACTIVE_CHARACTER[user_id_str]
            sheet_block = format_character_sheet(curr_char)
            rel_block = format_katya_relationship_block(curr_char, tg_user_id=clicker_user_id)
            combined = f"// <b>Активный персонаж успешно сменен!</b>\n\n{sheet_block}\n{rel_block}"
            if len(combined) <= 4000:
                await query.edit_message_text(combined, parse_mode="HTML")
            else:
                await query.edit_message_text(f"// <b>Активный персонаж успешно сменен!</b>\n\n{sheet_block}", parse_mode="HTML")
                await context.bot.send_message(chat_id=chat_id, text=rel_block, parse_mode="HTML")
        else:
            await query.edit_message_text("// Ошибка: персонаж не найден.")
        return

    # 1.2 ОТНОШЕНИЕ КАТИ КО ВСЕМ ПЕРСОНАЖАМ В /мои_персонажи
    if action_prefix == "my_chars":
        sub = parts[1]
        chars = get_characters(user_id_str)
        if not chars:
            await query.edit_message_text("// У вас пока нет сохранённых персонажей.")
            return

        if sub == "rel":
            rel_blocks = [format_katya_relationship_block(c, include_char_header=True, tg_user_id=clicker_user_id) for c in chars]
            save_characters(user_id_str, chars)
            nav_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад к персонажам", callback_data=f"my_chars:back:{clicker_user_id}"),
                 InlineKeyboardButton("❌ Закрыть", callback_data=f"reg:close:{clicker_user_id}")]
            ])
            full_text = "👑 <b>ОТНОШЕНИЕ ЕКАТЕРИНЫ II К ВАШИМ ПЕРСОНАЖАМ</b>\n\n" + "\n\n".join(rel_blocks)
            if len(full_text) <= 4000:
                await query.edit_message_text(full_text, reply_markup=nav_markup, parse_mode="HTML")
            else:
                await query.edit_message_text("👑 <b>ОТНОШЕНИЕ ЕКАТЕРИНЫ II К ВАШИМ ПЕРСОНАЖАМ:</b>", reply_markup=nav_markup, parse_mode="HTML")
                for rb in rel_blocks:
                    await context.bot.send_message(chat_id=chat_id, text=rb, parse_mode="HTML")
        elif sub == "back":
            await query.edit_message_text(
                "// Ваши персонажи двора. Нажмите на кнопку, чтобы играть за выбранного:",
                reply_markup=get_my_chars_markup(chars, clicker_user_id)
            )
        return

    # 1.3 ПОДТВЕРЖДЕНИЕ УДАЛЕНИЯ ПЕРСОНАЖА
    if action_prefix == "del_char":
        sub = parts[1]
        chars = get_characters(user_id_str)
        if sub == "cancel":
            await query.edit_message_text("// Удаление персонажа отменено. Ваш герой остаётся при дворе!")
            return
        idx = int(parts[2])
        if not (0 <= idx < len(chars)):
            await query.edit_message_text("// Персонаж не найден или уже удалён.")
            return
        target_char = chars[idx]
        c_name = html.escape(str(target_char.get("name", "Без имени")))

        if sub == "pick":
            confirm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Да, удалить навсегда", callback_data=f"del_char:confirm:{idx}:{clicker_user_id}")],
                [InlineKeyboardButton("❌ Нет, оставить", callback_data=f"del_char:cancel:{clicker_user_id}")]
            ])
            await query.edit_message_text(
                f"// ⚠️ Вы точно хотите безвозвратно удалить персонажа <b>«{c_name}»</b>?",
                reply_markup=confirm_kb,
                parse_mode="HTML"
            )
        elif sub == "confirm":
            remove_character(user_id_str, idx)
            if ACTIVE_CHARACTER.get(user_id_str, {}).get("name") == target_char.get("name"):
                ACTIVE_CHARACTER.pop(user_id_str, None)
                rem_chars = get_characters(user_id_str)
                if rem_chars:
                    ACTIVE_CHARACTER[user_id_str] = rem_chars[0]
            await query.edit_message_text(f"// 🗑️ Персонаж <b>«{c_name}»</b> был удалён из ваших анкет.", parse_mode="HTML")
        return

    # 1.5 КНОПКИ ЛИЧНОЙ ДАТЫ РОЖДЕНИЯ
    if action_prefix == "bday":
        b_action = parts[1]
        if b_action in ["ask", "menu"]:
            context.user_data["waiting_birthday"] = True
            cancel_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отмена", callback_data=f"bday:cancel:{clicker_user_id}")]
            ])
            await query.edit_message_text(
                "// 🎂 Напишите вашу <b>дату рождения</b> (только число и месяц) — можно цифрами, словами или смешанно.\n"
                "<i>Например: 14.11, 14 ноября или четырнадцатого ноября</i>",
                reply_markup=cancel_kb,
                parse_mode="HTML"
            )
        elif b_action == "decline":
            b_data = load_birthdays_data()
            uid_key = str(clicker_user_id)
            existing = b_data["users"].get(uid_key, {})
            existing.update({
                "tg_user_id": clicker_user_id,
                "chat_id": chat_id,
                "user_key": user_id_str,
                "username": update.effective_user.username or existing.get("username"),
                "first_name": update.effective_user.first_name or existing.get("first_name", "Гость"),
                "declined": True,
            })
            b_data["users"][uid_key] = existing
            save_birthdays_data(b_data)
            await query.edit_message_text(
                "// Как пожелаете, милая! Если передумаете, дату рождения всегда можно указать через команду /мои_персонажи."
            )
        elif b_action == "cancel":
            context.user_data.pop("waiting_birthday", None)
            await query.edit_message_text("// Хорошо, оставим это на потом.")
        return

    # 1.6 КНОПКИ ИНТЕРАКТИВНОГО РЕЕСТРА (/реестр)
    if action_prefix == "reg":
        reg_sub = parts[1]
        if reg_sub == "noop":
            return
        if reg_sub == "close":
            try:
                await query.message.delete()
            except Exception:
                try:
                    await query.edit_message_text("// Закрыто.")
                except Exception:
                    pass
            return

        try:
            if reg_sub == "menu":
                await query.edit_message_text(
                    "// <b>🏛️ Императорский Реестр Санкт-Петербурга (1838)</b>\n"
                    "Выберите, какие архивы желаете осмотреть:",
                    reply_markup=get_registry_menu_markup(clicker_user_id),
                    parse_mode="HTML"
                )
            elif reg_sub == "users":
                page = int(parts[2])
                text_out, markup = await build_registry_users_page(
                    context.bot, chat_id, clicker_user_id, clicker_user_id, page=page
                )
                await query.edit_message_text(text_out, reply_markup=markup, parse_mode="HTML")
            elif reg_sub == "biz":
                page = int(parts[2])
                text_out, markup = build_registry_businesses_page(clicker_user_id, page=page)
                await query.edit_message_text(text_out, reply_markup=markup, parse_mode="HTML")
        except TelegramError as e:
            if "not modified" not in str(e).lower():
                print("Ошибка обновления реестра:", e)
        return

    # 2. ПОДТВЕРЖДЕНИЕ СТАТУСА/СЛУХА КНОПКАМИ
    if action_prefix == "status_confirm":
        act_choice = parts[1]
        pending = context.user_data.get("pending_status")
        
        if not pending:
            await query.edit_message_text("// Действие со статусом уже не актуально.")
            return

        if act_choice == "cancel":
            context.user_data.pop("pending_status", None)
            await query.edit_message_text("// Как прикажете, изменение отменено.")
            return

        act = pending["action"]
        st_cat = pending["type"]
        st_val = pending["text"]
        target_uid = pending.get("target_uid", user_id_str)
        target_char_name = pending.get("target_char_name", "")

        t_chars = get_characters(target_uid)
        if not t_chars:
            await query.edit_message_text("// Персонажи не найдены.")
            return

        char_idx = 0
        if target_char_name:
            for i, c in enumerate(t_chars):
                if target_char_name.lower() in c.get("name", "").lower():
                    char_idx = i
                    break
        else:
            db = load_characters()
            char_idx = db.get(target_uid + "_active", 0)
            if char_idx >= len(t_chars): char_idx = 0

        char = t_chars[char_idx]

        st_cat = normalize_status_category(st_cat)
        for key_cat in ["permanent", "temporary", "achievements", "tragedies", "rumors"]:
            if key_cat not in char.setdefault("changes", {}): char["changes"][key_cat] = []

        if act == "add":
            if st_val not in char["changes"][st_cat]:
                char["changes"][st_cat].append(st_val)
                if st_cat in ["temporary", "rumors"]:
                    char.setdefault("status_times", {})[st_val] = time.time()
                elif st_cat == "achievements":
                    char.setdefault("achievement_times", {})[st_val] = time.time()
                
                clean_expired_statuses_and_rumors(char)
                t_chars[char_idx] = char
                save_characters(target_uid, t_chars)
                if target_uid in ACTIVE_CHARACTER and ACTIVE_CHARACTER[target_uid].get("name") == char.get("name"):
                    ACTIVE_CHARACTER[target_uid] = char
                
                await query.edit_message_text(f"// Статус «{st_val}» успешно добавлен в анкету персонажа <b>{char.get('name')}</b>!", parse_mode="HTML")
            else:
                await query.edit_message_text("// Такой статус уже записан в анкету.")
        elif act == "remove":
            if st_val in char["changes"].get(st_cat, []):
                char["changes"][st_cat].remove(st_val)
                clean_expired_statuses_and_rumors(char)
                t_chars[char_idx] = char
                save_characters(target_uid, t_chars)
                if target_uid in ACTIVE_CHARACTER and ACTIVE_CHARACTER[target_uid].get("name") == char.get("name"):
                    ACTIVE_CHARACTER[target_uid] = char
                await query.edit_message_text(f"// Статус «{st_val}» успешно убран из анкеты <b>{char.get('name')}</b>.", parse_mode="HTML")
            else:
                await query.edit_message_text("// Статус не найден в анкете для удаления.")

        context.user_data.pop("pending_status", None)
        return

    # 3. ВЫБОР ТИПА СТАТУСА (/добавить_статус)
    if action_prefix == "set_status_type":
        st_t = parts[1]
        if st_t == "cancel":
            context.user_data.pop("waiting_status_type", None)
            await query.edit_message_text("// Добавление статуса отменено.")
            return
            
        context.user_data["waiting_status_type"] = st_t
        names_map = {"temporary": "временный статус", "permanent": "постоянный статус", "rumors": "слух", "tragedies": "трагедию"}
        await query.edit_message_text(f"// Напишите текст, который необходимо записать как <b>{names_map[st_t]}</b> (отправьте сообщением в чат):", parse_mode="HTML")
        return

    # 4. ИНТЕРАКТИВНЫЕ ИВЕНТЫ (/event)
    if action_prefix == "ev_mode":
        mode = parts[1]
        my_chars = get_characters(user_id_str)

        if mode == "cancel":
            context.user_data.pop("event_participants", None)
            context.user_data.pop("letter_step", None)
            context.user_data.pop("letter_query", None)
            context.user_data.pop("letter_char_idx", None)
            context.user_data.pop("letter_npc_char", None)
            context.user_data.pop("group_letter_step", None)
            context.user_data.pop("group_letter_query", None)
            await query.edit_message_text("// Действие отменено.")
            return

        if mode == "single":
            if len(my_chars) == 1:
                context.user_data["event_participants"] = [my_chars[0]]
                await show_event_theme_buttons(query, my_chars[0], clicker_user_id)
            else:
                buttons = [[InlineKeyboardButton(c.get('name', 'Без имени'), callback_data=f"ev_single_char:{i}:{clicker_user_id}")] for i, c in enumerate(my_chars)]
                buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"ev_mode:cancel:{clicker_user_id}")])
                await query.edit_message_text("// Кто из ваших персонажей отправится в приключение?", reply_markup=InlineKeyboardMarkup(buttons))
            return

        if mode == "multi":
            buttons = [
                [InlineKeyboardButton("🎲 Перст судьбы (Случайные компаньоны)", callback_data=f"ev_multi_mode:random:{clicker_user_id}")],
                [InlineKeyboardButton("❌ Отмена", callback_data=f"ev_mode:cancel:{clicker_user_id}")]
            ]
            await query.edit_message_text("// Как выберем спутников для приключения?", reply_markup=InlineKeyboardMarkup(buttons))
            return

    if action_prefix == "ev_single_char":
        idx = int(parts[1])
        my_chars = get_characters(user_id_str)
        if 0 <= idx < len(my_chars):
            context.user_data["event_participants"] = [my_chars[idx]]
            await show_event_theme_buttons(query, my_chars[idx], clicker_user_id)
        return

    if action_prefix == "ev_multi_mode":
        all_active_in_chat = get_all_active_characters(filter_chat_id=chat_id)
        companions = [item for item in all_active_in_chat if item[0] != user_id_str]
        my_chars = get_characters(user_id_str)
        my_active = ACTIVE_CHARACTER.get(user_id_str) or (my_chars[0] if my_chars else None)

        if not companions:
            context.user_data["event_participants"] = [my_active]
            await query.edit_message_text("// Других персонажей в чате пока нет, кручу колесо одиночной судьбы!")
            await show_event_theme_buttons(query, my_active, clicker_user_id)
            return

        num_to_choose = min(len(companions), random.randint(1, 3))
        chosen_companions = random.sample(companions, num_to_choose)
        participants = [my_active] + [item[1] for item in chosen_companions]
        context.user_data["event_participants"] = participants
        
        await show_event_theme_buttons(query, my_active, clicker_user_id, custom_title=f"Судьба определила спутников: {', '.join(c.get('name') for c in participants[1:])}!")
        return

    if action_prefix == "ev_theme":
        theme_key = parts[1]
        themes_map = {
            "rel": ("Отношения и репутация", "отношения"),
            "car": ("Власть, карьера и положение", "карьера"),
            "mon": ("Деньги, векселя и имущество", "деньги"),
            "sec": ("Тайны и прошлое", "тайны"),
            "soc": ("Светская жизнь, быт и досуг", "быт"),
            "rnd": ("Случайная тема", random.choice(["отношения", "карьера", "деньги", "тайны", "быт"]))
        }
        theme_str, sub_key = themes_map.get(theme_key, ("Случайная тема", "быт"))
        participants = context.user_data.pop("event_participants", None)
        if not participants:
            my_chars = get_characters(user_id_str)
            participants = [ACTIVE_CHARACTER.get(user_id_str) or my_chars[0]]

        char = participants[0]
        await query.edit_message_text(f"// Кручу колесо судьбы для <b>{char.get('name')}</b>... Вестовые уже в пути! 📜", parse_mode="HTML")

        char_info = json.dumps(participants, ensure_ascii=False)
        event_result, is_success, is_duel_conflict = await generate_roleplay_event(char_info, theme_str)

        unlocked = []
        if is_success: unlocked += check_character_achievements(user_id_str, char, "luck_success", 1)
        if is_duel_conflict: unlocked += check_character_achievements(user_id_str, char, "duels_conflicts", 1)
        if 1 <= get_msk_now().hour < 5: unlocked += check_character_achievements(user_id_str, char, "night_events", 1)
        unlocked += check_character_achievements(user_id_str, char, "event_theme_dummy", 1, sub_category=sub_key)
        save_character_object(user_id_str, char)

        await context.bot.send_message(chat_id=chat_id, text=event_result, parse_mode="HTML")
        if unlocked:
            await context.bot.send_message(chat_id=chat_id, text=f"// ✨ Персонаж получает тайные достижения: <b>{', '.join(list(set(unlocked)))}</b>!", parse_mode="HTML")
        return

    # 5. КНОПКИ ВЫБОРА ТЕМ ДЛЯ /совет
    if action_prefix == "adv_topic":
        topic_map = {
            "duel": "как отыграть дуэль на шпагах и пистолетах?",
            "cards": "как отыграть карточную игру (Штосс, Вист, Бостон)?",
            "balls": "как отыграть поведение на балу и язык веера?",
            "intrigue": "как отыграть тайные интриги и заговор?",
            "npc": "как отыграть слуг, кучеров и извозчиков (НПС)?",
            "debts": "как отыграть векселя, залоги и долговые расписки?"
        }
        topic_key = parts[1]
        user_query = topic_map.get(topic_key, "общий совет по светскому этикету")
        chars = get_characters(user_id_str)
        active_char = ACTIVE_CHARACTER.get(user_id_str) or (chars[0] if chars else None)
        
        await query.edit_message_text("// Открываю старинные светские трактаты и составляю наставление... 📜")
        advice_res = await generate_roleplay_advice(user_query, active_char, user_id_str, chat_id)
        await send_long_html(query.message, advice_res)
        return

    # 6. КНОПКИ ВЫБОРА АВТОРА ДЛЯ /создать_бизнес
    if action_prefix == "biz_owner":
        idx = int(parts[1])
        chars = get_characters(user_id_str)
        if 0 <= idx < len(chars):
            chosen_char = chars[idx]
            context.user_data["biz_owner"] = chosen_char
            context.user_data["biz_step"] = "info"
            await query.edit_message_text(
                f"// Владельцем заведения выбран(а): <b>{chosen_char.get('name')}</b>.\n\n"
                f"Укажите в ответном сообщении через запятую:\n"
                f"<b>1. Название заведения</b>\n"
                f"<b>2. Тип заведения</b> (трактир, лавка, салон и т.д.)\n"
                f"<b>3. Статус</b> (старинное родовое гнездо / свежеоткрытое предприятие)\n\n"
                f"<i>Пример: Трактир «Веселый Гусаръ», Трактир, Свежеоткрытое предприятие</i>",
                parse_mode="HTML"
            )
        return

    # 7. КНОПКИ ЗАКРЫТИЯ БИЗНЕСА (/закрыть_бизнес)
    if action_prefix == "biz_close":
        idx = int(parts[1])
        user_businesses = [b for b in CITY_BUSINESSES if b.get("owner_user_id") == user_id_str]
        if not user_businesses:
            chars = get_characters(user_id_str)
            char_names = [c.get("name") for c in chars]
            user_businesses = [b for b in CITY_BUSINESSES if b.get("owner_name") in char_names]

        if 0 <= idx < len(user_businesses):
            b_to_close = user_businesses[idx]
            CITY_BUSINESSES.remove(b_to_close)
            save_businesses()
            await query.edit_message_text(f"// Заведение <b>«{b_to_close.get('name')}»</b> было закрыто и вычеркнуто из реестра столицы.", parse_mode="HTML")
        return

    # 8. КНОПКИ ВЫБОРА ПЕРСОНАЖА ДЛЯ МОДЕРАТОРОВ (покажи анкету)
    if action_prefix == "adm_sheet":
        if len(parts) >= 5:
            target_uid = f"{parts[1]}:{parts[2]}"
            c_idx = int(parts[3])
            t_tg_id = int(parts[2]) if parts[2].lstrip("-").isdigit() else None
        else:
            target_uid = parts[1]
            c_idx = int(parts[2])
            t_tg_id = int(target_uid.split(":")[1]) if ":" in target_uid and target_uid.split(":")[1].isdigit() else None

        t_chars = get_characters(target_uid)
        if 0 <= c_idx < len(t_chars):
            c_obj = t_chars[c_idx]
            sheet_b = format_character_sheet(c_obj)
            rel_b = format_katya_relationship_block(c_obj, tg_user_id=t_tg_id)
            comb = f"{sheet_b}\n{rel_b}"
            try:
                if len(comb) <= 4000:
                    await query.edit_message_text(comb, parse_mode="HTML")
                else:
                    await query.edit_message_text(sheet_b, parse_mode="HTML")
                    await context.bot.send_message(chat_id=chat_id, text=rel_b, parse_mode="HTML")
            except Exception:
                await send_long_html(query.message, sheet_b)
                await send_long_html(query.message, rel_b)
        else:
            await query.edit_message_text("// Анкета этого персонажа не найдена.")
        return

    # 9. КНОПКИ ВЫБОРА АВТОРА ДЛЯ /письмо (С ШАБЛОНОМ)
    if action_prefix == "let_char":
        choice_idx = int(parts[1])
        chars = get_characters(user_id_str)
        letter_query = context.user_data.get("letter_query", "").strip()
        
        if choice_idx == len(chars):
            context.user_data["letter_step"] = "npc_details"
            await query.edit_message_text("// 🎭 От какого именно НПС пишем письмо?\nНапишите сообщением его имя, титул или краткое описание.")
            return

        if 0 <= choice_idx < len(chars):
            chosen_char = chars[choice_idx]
            if letter_query:
                context.user_data.pop("letter_query", None)
                mily = get_mily(chosen_char)
                await query.edit_message_text(f"// Перо скрипит по бумаге, {mily}, составляю послание от лица {chosen_char.get('name')}... ✍️")
                
                answer = await rewrite_letter_command_with_char(letter_query, chosen_char)
                unlocked = check_character_achievements(user_id_str, chosen_char, "letters_written", 1)
                save_character_object(user_id_str, chosen_char)
                
                await context.bot.send_message(chat_id=chat_id, text=answer, parse_mode="HTML")
                if unlocked:
                    await context.bot.send_message(chat_id=chat_id, text=f"// ✨ За ваши труды персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
            else:
                context.user_data["letter_step"] = "content"
                context.user_data["letter_char_idx"] = choice_idx
                dear = get_dear(chosen_char)
                msg = (
                    f"// ✍️ Прекрасно, {dear}. Пишем письмо от лица <b>{html.escape(chosen_char.get('name', 'Персонажа'))}</b>.\n\n"
                    f"Отправьте содержание письма по шаблону одним сообщением:\n"
                    f"<b>1. Кому:</b> [Титул, имя получателя и @username]\n"
                    f"<b>2. Суть письма:</b> [О чём пишем — приглашение, признание, интрига, дело]\n"
                    f"<b>3. Тон и детали:</b> [Вежливый, язвительный, тёплый, строгий]\n\n"
                    f"<i>(Или напишите «отмена»)</i>"
                )
                await query.edit_message_text(msg, parse_mode="HTML")
        return

    # 10. КНОПКИ ВЫБОРА АВТОРА ДЛЯ /групповое_письмо
    if action_prefix == "glet_char":
        choice_idx = int(parts[1])
        chars = get_characters(user_id_str)
        existing_query = context.user_data.get("group_letter_query", "").strip()

        if choice_idx == len(chars):
            context.user_data["group_letter_step"] = "npc_details"
            await query.edit_message_text("// 🎭 От какого именно НПС пишем групповые письма?\nНапишите сообщением его имя, титул или краткое описание.")
            return

        if 0 <= choice_idx < len(chars):
            chosen_char = chars[choice_idx]
            if existing_query:
                context.user_data.pop("group_letter_query", None)
                mily = get_mily(chosen_char)
                await query.edit_message_text(f"// Перо быстро скользит по пергаменту, {mily}, составляю групповые послания... ✍️")
                answer = await generate_group_letters_command_with_char(existing_query, chosen_char)
                await send_raw_long_message(query.message, answer)
            else:
                context.user_data["group_letter_step"] = "content"
                context.user_data["group_letter_char_idx"] = choice_idx
                dear = get_dear(chosen_char)
                msg = (
                    f"// Прекрасно, {dear}. Теперь опишите детали группового письма сообщением в чат:\n\n"
                    "1. Тема письма.\n2. Адресаты (@username).\n3. Особенности для каждого.\n4. Одинаковое или разное."
                )
                await query.edit_message_text(msg)
        return

async def show_event_theme_buttons(query, char, owner_id, custom_title=None):
    dear = get_dear(char)
    title_text = custom_title or f"// Какую сферу жизни затронем, {dear}?"
    buttons = [
        [InlineKeyboardButton("💌 Отношения и репутация", callback_data=f"ev_theme:rel:{owner_id}"),
         InlineKeyboardButton("🏛️ Карьера и власть", callback_data=f"ev_theme:car:{owner_id}")],
        [InlineKeyboardButton("💰 Деньги и векселя", callback_data=f"ev_theme:mon:{owner_id}"),
         InlineKeyboardButton("🕵️ Тайны и прошлое", callback_data=f"ev_theme:sec:{owner_id}")],
        [InlineKeyboardButton("🎭 Светский быт и балы", callback_data=f"ev_theme:soc:{owner_id}"),
         InlineKeyboardButton("🎲 Перст судьбы (Рандом)", callback_data=f"ev_theme:rnd:{owner_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"ev_mode:cancel:{owner_id}")]
    ]
    await query.edit_message_text(title_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

# ==========================
# ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ==========================

async def messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    user_id = f"{update.effective_chat.id}:{update.effective_user.id}"
    sender_user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name or "Гость"
    lower = text.lower().strip()
    chat_id = update.effective_chat.id
    is_private = update.effective_chat.type == "private"

    # Очищаем @username_бота из /команда@бот в групповых чатах для мгновенного выполнения
    if text.startswith("/"):
        parts_cmd = text.split(None, 1)
        clean_cmd = re.sub(r'@[a-zA-Z0-9_]+$', '', parts_cmd[0])
        if clean_cmd != parts_cmd[0]:
            text = clean_cmd + (" " + parts_cmd[1] if len(parts_cmd) > 1 else "")
            lower = text.lower().strip()

    if username:
        USERNAME_CACHE["@" + username.lower()] = user_id
    update_user_meta(sender_user_id, username, first_name)

    if lower.startswith("/start"):
        return

    # ==========================
    # СБОР РОЛЕВЫХ ПОСТОВ ДЛЯ ДУЭЛЕЙ В БУФЕР (0 ТОКЕНОВ)
    # ==========================
    my_uname_tag = "@" + username.lower() if username else ""
    for d_id, d_data in ACTIVE_DUELS.items():
        is_p1 = (d_data["p1_id"] == user_id)
        is_p2 = (my_uname_tag and d_data["p2_uname"] == my_uname_tag)
        if is_p1 or is_p2:
            if not text.startswith(("//", "((", "!", "*офф*")) and len(text) >= 15:
                char_tag = d_data["p1_name"] if is_p1 else "Противник"
                active_curr = ACTIVE_CHARACTER.get(user_id)
                if active_curr:
                    char_tag = active_curr.get("name", char_tag)
                d_data.setdefault("posts", []).append(f"{char_tag}: {text}")

    # ==========================
    # КОМАНДА /помощь (/help)
    # ==========================
    if lower in ["/помощь", "/help", "катя помощь", "катя, помощь", "катя команды"]:
        help_text = (
            "<b>📜 ИМПЕРАТОРСКИЙ СВОД ПРАВИЛ И КОМАНД (1838 ГОД)</b>\n"
            "ㅤㅤ∴ ════ ∴ ✦ ∴ ════ ∴\n\n"
            "<b>🌐 Поиск и Картины:</b>\n"
            "• <code>/поиск [запрос]</code> (<code>/search</code>) — поиск актуальных сведений в интернете с прямыми ссылками. <i>Можно попросить: «не учитывай википедию» или «без dzen.ru»</i>.\n"
            "• <code>/арт [описание]</code> (<code>/нарисуй</code>, <code>/art</code>) — написать картину или портрет вашего персонажа.\n\n"
            "<b>👤 Персонажи, Анкеты и Отношения:</b>\n"
            "• <code>/новая_анкета</code> (<code>/new_anketa</code>, <code>катя новая анкета</code>) — подать анкету ко двору.\n"
            "• <code>/мои_персонажи</code> (<code>/my_chars</code>) — переключение героев, личная дата рождения и отношение Кати ко всем персонажам.\n"
            "• <code>/анкета</code> (<code>/anketa</code>, <code>моя анкета</code>) — анкета текущего героя и подробное отношение Кати.\n"
            "• <code>/отношения_кати</code> (<code>/relations</code>) — подробный разбор отношения Екатерины к тебе.\n"
            "• <code>/изменить_анкету</code> (<code>/edit_anketa</code>) — исправить или дополнить поля анкеты.\n"
            "• <code>/выбрать_персонажа [№]</code> — быстро переключить персонажа по номеру.\n"
            "• <code>/удалить_персонажа [№]</code> — удалить персонажа (с подтверждением кнопками).\n"
            "• <code>/добавить_статус</code> / <code>/удалить_статус</code> — управление статусами героя.\n\n"
            "<b>🏛️ Реестр и Заведения столицы:</b>\n"
            "• <code>/реестр</code> (<code>/reestr</code>) — интерактивный реестр кнопками: все участники (с вечными ссылками и персонажами) + заведения Петербурга.\n"
            "• <code>/создать_бизнес</code> (<code>/create_business</code>) — внести своё заведение в реестр.\n"
            "• <code>/закрыть_бизнес</code> (<code>/close_business</code>) — закрыть предприятие кнопками.\n"
            "• <code>/заведение [номер/название]</code> — изучить меню и ассортимент лавки.\n\n"
            "<b>💌 Эпистолярная служба:</b>\n"
            "• <code>/письмо</code> (<code>/letter</code>) — составить письмо по шаблону на дореформенном слоге с ѣ и ъ (или сразу <code>/письмо [текст]</code>).\n"
            "• <code>/групповое_письмо</code> (<code>/гписьмо</code>, <code>/gletter</code>) — рассылка одинаковых или именных посланий.\n\n"
            "<b>⚔️ Честь и Дуэли:</b>\n"
            "• <code>/дуэль @username</code> (<code>/duel @username</code>) — вызвать обидчика к барьеру.\n"
            "• <code>/дуэль_победа</code> / <code>/дуэль_поражение</code> / <code>/дуэль_отмена</code> (или <code>катя я выиграл/проиграл дуэль</code>, <code>катя дуэль отменена</code>) — подвести итог.\n"
            "• <code>/статистика</code> (<code>/stats</code>) — дуэльный послужной список персонажа.\n\n"
            "<b>💡 Наставления и Игры судьбы:</b>\n"
            "• <code>/совет [вопрос]</code> (<code>/advice</code>) — совет по отыгрышу сцены, игры или этикета.\n"
            "• <code>/event</code> (<code>/событие</code>, <code>катя событие</code>) — интерактивное происшествие кнопками.\n\n"
            "<b>📰 «Санкт-Петербургские Ведомости»:</b>\n"
            "• <code>/редакция [текст]</code> (<code>/gazette_ad</code>) — подать объявление в газету.\n"
            "• <code>/архив_газет</code> (<code>/gazette_archive</code>) — архив прошлых выпусков.\n\n"
            "<b>🚨 Прочее:</b>\n"
            "• <code>/правила</code> (<code>/rules</code>), <code>/пункт [№]</code>, <code>/пункт_отношений [№]</code> — законы и правила.\n"
            "• <code>/репорт [текст]</code> (<code>/report</code>) — отправить весточку разработчику.\n"
            "• <code>/clear</code> — очистить память текущего диалога с Екатериной."
        )

        if is_admin(sender_user_id):
            help_text += (
                "\n\n│────────✤────────│\n"
                "<b>👑 ПОЛНОМОЧИЯ МОДЕРАЦИИ И ВЛАДЕЛЬЦЕВ:</b>\n"
                "• <code>/сбросить_отношения</code> (или <code>@username</code>) — очистить отношение Кати к себе или к игроку.\n"
                "• <code>/добавить_анкету</code> (ответом или <code>@username</code>) — добавить анкету игроку.\n"
                "• <code>/удалить_анкету @username [№]</code> — принудительно изъять анкету игрока.\n"
                "• <code>покажи анкету</code> (ответом на сообщение) — просмотреть анкеты игрока и отношение Кати.\n"
                "• <code>/дать_достижение [название]</code> (ответом на сообщение) — наградить ачивкой.\n"
                "• <code>/дуэль_исход @победитель @проигравший</code> — ручной арбитраж исхода дуэли.\n"
                "• <code>/дуэль_сброс @ник</code> — экстренно отменить зависшую дуэль.\n"
                "• <code>/выпуск_газеты</code> — принудительно напечатать и закрепить свежий номер.\n"
                "• <code>/удалить_газету [№]</code> — предать огню выпуск из архива.\n"
                "• <code>/чат_газеты</code> — привязать ролевой чат для авто-публикаций.\n"
                "• <code>/отношение @ник [директива]</code> / <code>/сбросить_отношение @ник</code> / <code>/директивы</code> — секретные указания Кате.\n"
                "• <code>/админы</code> — список всех администраторов.\n"
                "• <code>/добавить_админа</code> / <code>/удалить_админа</code> (только владельцы) — управление админами."
            )

        await update.message.reply_text(f"<blockquote expandable>{help_text}</blockquote>", parse_mode="HTML")
        return

    # ==========================
    # КОМАНДА /поиск (/search)
    # ==========================
    if lower.startswith("/поиск") or lower.startswith("/search") or lower.startswith("катя найди") or lower.startswith("катя, найди"):
        search_query = ""
        for pfx in ["/поиск", "/search", "катя, найди", "катя найди"]:
            if lower.startswith(pfx):
                search_query = text[len(pfx):].strip(" ,:")
                break

        chars = get_characters(user_id)
        active_char = ACTIVE_CHARACTER.get(user_id) or (chars[0] if chars else None)
        mily = get_mily(active_char)

        if not search_query:
            await update.message.reply_text(
                f"// Укажите, что именно вы желаете узнать, {mily}!\n\n"
                "<i>Примеры:\n"
                "• <code>/поиск когда умер Александр Сергеевич Пушкин</code>\n"
                "• <code>/поиск биография Пушкина, не учитывай википедию</code>\n"
                "• <code>/поиск курс серебра, без сайта banki.ru</code></i>",
                parse_mode="HTML"
            )
            return

        working_msg = await update.message.reply_text(f"// Минутку, {mily}, я сейчас сама всё поищу и проверю... 🌐")

        answer_text, sources, err = await ask_gemini_with_web_search(search_query, user_id=user_id)

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
        except Exception:
            pass

        if err:
            await update.message.reply_text(f"// {err}")
            return

        if not answer_text:
            await update.message.reply_text(f"// Простите, {mily}, я ничего не смогла найти по вашему запросу.")
            return

        sources_block = ""
        if sources:
            sources_lines = []
            for idx, (title, uri) in enumerate(sources[:5], 1):
                clean_title = html.escape(title[:65])
                sources_lines.append(f"{idx}. <a href=\"{html.escape(uri)}\">{clean_title}</a>")
            sources_block = "\n\n🔗 <b>Изученные источники:</b>\n" + "\n".join(sources_lines)

        final_message = f"<b>🌐 Результаты поиска:</b>\n<blockquote expandable>{answer_text}{sources_block}</blockquote>"
        await send_long_html(update.message, final_message)
        return

 # ==========================
    # КОМАНДА /арт / /нарисуй (ГЕНЕРАЦИЯ КАРТИНОК)
    # ==========================
    art_prefixes = [
        "/арт", "/art", "/нарисуй", "/draw",
        "катя, нарисуй", "катя нарисуй",
        "катя, арт", "катя арт"
    ]
    if any(lower.startswith(pfx) for pfx in art_prefixes):
        art_query = ""
        for pfx in art_prefixes:
            if lower.startswith(pfx):
                art_query = text[len(pfx):].strip(" ,:")
                break

        if not art_query:
            await update.message.reply_text(
                "// Что именно вы желаете увидеть на холсте, милая?\n\n"
                "<i>Примеры:\n"
                "• <code>/арт бал в Зимнем дворце при свечах</code>\n"
                "• <code>/нарисуй портрет моего персонажа в бальном платье</code>\n"
                "• <code>/арт дуэль на Черной речке в утреннем тумане</code></i>",
                parse_mode="HTML"
            )
            return

        active_char = get_real_active_character(user_id, sender_user_id)
        dear = get_dear(active_char)

        working_msg = await update.message.reply_text(f"// *Берусь за кисти и мольберт...* Продумываю детали и пишу картину для вас, {dear}! 🎨")

        img_io, eng_prompt, used_names, err = await generate_katya_art(
            art_query, active_char=active_char, chat_id=chat_id
        )

        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
        except Exception:
            pass

        if err or not img_io:
            await update.message.reply_text(f"// {err or 'Увы, краски подвели меня.'}")
            return

        char_tag = f"\n👤 <b>Персонаж(и) из анкеты:</b> {html.escape(', '.join(used_names))}" if used_names else ""
        caption_text = (
            f"// *Изящным жестом откладывает кисть* Вот ваш холст, {dear}! 🎨\n"
            f"<blockquote expandable>🖌️ <b>Запрос:</b> <i>{html.escape(art_query[:150])}</i>{char_tag}\n"
            f"✨ <b>Промпт для нейросети:</b> <i>{html.escape(eng_prompt[:600])}</i></blockquote>"
        )
        try:
            await update.message.reply_photo(photo=img_io, caption=caption_text, parse_mode="HTML")
        except Exception as e:
            print("Ошибка отправки сгенерированного фото в Telegram:", e)
            await update.message.reply_text("// Картина написана, но передать её в чат не удалось.")
        return

    # ==========================
    # КОМАНДА /репорт (/report)
    # ==========================
    if lower.startswith("/репорт") or lower.startswith("/report") or lower.startswith("катя репорт") or lower.startswith("катя, репорт"):
        report_text = ""
        for pfx in ["/репорт", "/report", "катя, репорт", "катя репорт"]:
            if lower.startswith(pfx):
                report_text = text[len(pfx):].strip(" ,")
                break

        if not report_text:
            await update.message.reply_text("// Укажите суть проблемы или предложение после команды!\n<i>Пример: `/репорт Нашел баг при создании письма`</i>", parse_mode="HTML")
            return

        sender_uname = f"@{username}" if username else "—"
        chat_title = update.effective_chat.title or "Личные сообщения"
        report_payload = (
            f"🚨 <b>НОВОЕ ДОНЕСЕНИЕ / РЕПОРТ РАЗРАБОТЧИКУ</b>\n"
            f"ㅤㅤ∴ ════ ∴ ✦ ∴ ════ ∴\n"
            f"👤 <b>Отправитель:</b> {html.escape(first_name)} ({html.escape(sender_uname)})\n"
            f"🆔 <b>Telegram ID:</b> <code>{sender_user_id}</code>\n"
            f"📍 <b>Чат:</b> {html.escape(chat_title)} (<code>{chat_id}</code>)\n"
            f"│────────✤────────│\n"
            f"📝 <b>Текст обращения:</b>\n{html.escape(report_text)}"
        )

        try:
            await context.bot.send_message(chat_id=REPORT_DEVELOPER_ID, text=report_payload, parse_mode="HTML")
            await update.message.reply_text("// Ваше донесение запечатано сургучом и доставлено лично в руки разработчику! 📜")
        except Exception as e:
            print("Ошибка отправки репорта:", e)
            await update.message.reply_text("// Не удалось передать донесение вестовыми. Попробуйте чуть позже.")
        return

    # ==========================
    # СБРОС ОТНОШЕНИЙ КАТИ (ТОЛЬКО АДМИНИСТРАТОРЫ)
    # ==========================
    if lower.startswith("/сбросить_отношения") or lower.startswith("/сброс_отношений") or lower.startswith("/reset_relations"):
        if not is_admin(sender_user_id):
            await update.message.reply_text("// Эта команда доступна только модераторам столицы, милая.")
            return

        arg = ""
        for pfx in ["/сбросить_отношения", "/сброс_отношений", "/reset_relations"]:
            if lower.startswith(pfx):
                arg = text[len(pfx):].strip()
                break

        if update.message.reply_to_message or arg:
            target_tg_id, target_uid, t_uname, t_fname = resolve_target_user_from_message(update, arg, chat_id)
        else:
            target_tg_id, target_uid, t_uname, t_fname = sender_user_id, user_id, username or "", first_name

        if not target_tg_id or not target_uid:
            await update.message.reply_text("// Не удалось найти указанного игрока в архивах столицы.")
            return

        t_chars = get_characters(target_uid)
        if not t_chars:
            await update.message.reply_text("// У данного игрока пока нет персонажей при дворе.")
            return

        db = load_characters()
        t_idx = db.get(target_uid + "_active", 0)
        if not isinstance(t_idx, int) or not (0 <= t_idx < len(t_chars)):
            t_idx = 0
        target_char = t_chars[t_idx]

        reset_katya_relationship(target_char, tg_user_id=target_tg_id)
        save_character_object(target_uid, target_char)

        t_name = html.escape(str(target_char.get("name", "Персонаж")))
        if target_tg_id == sender_user_id:
            await update.message.reply_text(
                f"// 👑 Отношение Екатерины II к вашему персонажу <b>«{t_name}»</b> было полностью очищено и возвращено к начальному состоянию!",
                parse_mode="HTML"
            )
        else:
            label = f"@{t_uname}" if t_uname else f'<a href="tg://user?id={target_tg_id}">{html.escape(t_fname)}</a>'
            await update.message.reply_text(
                f"// 👑 Отношение Екатерины II к персонажу <b>«{t_name}»</b> игрока {label} было очищено и сброшено к исходному состоянию!",
                parse_mode="HTML"
            )
        return

    # ==========================
    # УПРАВЛЕНИЕ АДМИНИСТРАТОРАМИ (ТОЛЬКО ВЛАДЕЛЬЦЫ)
    # ==========================
    if lower.startswith("/добавить_админа"):
        if not is_owner(sender_user_id):
            await update.message.reply_text("// Назначать новых администраторов дозволено лишь Владельцам Империи, милая.")
            return
        arg = text[len("/добавить_админа"):].strip()
        tg_id, _, t_uname, t_fname = resolve_target_user_from_message(update, arg, chat_id)
        if not tg_id:
            await update.message.reply_text(
                "// Ответьте этой командой на сообщение игрока или укажите его <code>@username</code> / Telegram ID!\n"
                "<i>Пример: /добавить_админа @username</i>",
                parse_mode="HTML"
            )
            return
        if is_admin(tg_id):
            await update.message.reply_text("// Этот человек уже обладает правами администратора при дворе.")
            return
        DYNAMIC_ADMIN_IDS.append(int(tg_id))
        save_admins()
        label = f"@{t_uname}" if t_uname else f'<a href="tg://user?id={tg_id}">{html.escape(t_fname)}</a>'
        await update.message.reply_text(f"// 👑 {label} пожалован(а) правами <b>администратора</b>!", parse_mode="HTML")
        return

    if lower.startswith("/удалить_админа"):
        if not is_owner(sender_user_id):
            await update.message.reply_text("// Лишать прав администратора могут только Владельцы Империи.")
            return
        arg = text[len("/удалить_админа"):].strip()
        tg_id, _, t_uname, t_fname = resolve_target_user_from_message(update, arg, chat_id)
        if not tg_id:
            await update.message.reply_text("// Ответьте на сообщение админа или укажите его <code>@username</code> / Telegram ID!", parse_mode="HTML")
            return
        if int(tg_id) in BASE_ADMIN_IDS:
            await update.message.reply_text("// Невозможно снять базового владельца или основателя бота.")
            return
        if int(tg_id) not in DYNAMIC_ADMIN_IDS:
            await update.message.reply_text("// Этот пользователь не найден в списке добавленных администраторов.")
            return
        DYNAMIC_ADMIN_IDS.remove(int(tg_id))
        save_admins()
        label = f"@{t_uname}" if t_uname else f'<a href="tg://user?id={tg_id}">{html.escape(t_fname)}</a>'
        await update.message.reply_text(f"// 👑 {label} лишен(а) прав администратора.", parse_mode="HTML")
        return

    if lower == "/админы":
        if not is_admin(sender_user_id):
            await update.message.reply_text("// Список руководства доступен только модераторам.")
            return
        lines = []
        for idx, aid in enumerate(ADMIN_IDS, 1):
            meta = USERS_META.get(str(aid), {})
            fname = html.escape(meta.get("first_name") or f"ID {aid}")
            uname = meta.get("username")
            uname_part = f" (@{html.escape(uname)})" if uname else ""
            role = "👑 Владелец" if aid in OWNER_IDS else ("🛡️ Базовый админ" if aid in BASE_ADMIN_IDS else "✒️ Назначенный админ")
            lines.append(f"{idx}. <a href=\"tg://user?id={aid}\">{fname}</a>{uname_part} — <i>{role}</i> (<code>{aid}</code>)")
        await update.message.reply_text(
            f"<blockquote expandable><b>👑 СПИСОК АДМИНИСТРАЦИИ:</b>\n\n" + "\n".join(lines) + "</blockquote>",
            parse_mode="HTML"
        )
        return

    # ==========================
    # АДМИНСКОЕ ДОБАВЛЕНИЕ АНКЕТЫ ДРУГОМУ ЧЕЛОВЕКУ (/добавить_анкету)
    # ==========================
    if lower.startswith("/добавить_анкету") or lower.startswith("катя добавь анкету") or lower.startswith("катя, добавь анкету"):
        if not is_admin(sender_user_id):
            await update.message.reply_text("// Добавлять анкеты другим игрокам могут только администраторы, милая.")
            return

        rest = ""
        for pfx in ["/добавить_анкету", "катя, добавь анкету", "катя добавь анкету"]:
            if lower.startswith(pfx):
                rest = text[len(pfx):].strip()
                break

        inline_sheet = ""
        first_arg = rest
        if update.message.reply_to_message:
            tg_id, target_uid, t_uname, t_fname = resolve_target_user_from_message(update, "", chat_id)
            inline_sheet = rest
        else:
            if "\n" in rest:
                first_line, inline_sheet = rest.split("\n", 1)
                first_arg = first_line.strip()
            else:
                parts_sp = rest.split(" ", 1)
                first_arg = parts_sp[0] if parts_sp else ""
                inline_sheet = parts_sp[1].strip() if len(parts_sp) > 1 and (first_arg.startswith("@") or first_arg.isdigit()) else ""
            tg_id, target_uid, t_uname, t_fname = resolve_target_user_from_message(update, first_arg, chat_id)

        if not tg_id or not target_uid:
            await update.message.reply_text(
                "// Ответьте этой командой на сообщение игрока ИЛИ укажите его <code>@username</code> / ID!\n"
                "<i>Пример: Ответить на сообщение + /добавить_анкету\n"
                "Или: /добавить_анкету @username</i>",
                parse_mode="HTML"
            )
            return

        label = f"@{t_uname}" if t_uname else f'<a href="tg://user?id={tg_id}">{html.escape(t_fname)}</a>'
        if inline_sheet and len(inline_sheet) > 15:
            working_msg = await update.message.reply_text(f"// Оформляю новую анкету для {label}... ✒️", parse_mode="HTML")
            char = await convert_character_sheet(inline_sheet)
            ensure_katya_relationship(char, tg_id)
            add_character(target_uid, char)
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
            except Exception:
                pass
            await update.message.reply_text(
                f"// ✅ Персонаж <b>«{html.escape(char.get('name', 'Персонаж'))}»</b> успешно добавлен в анкеты игрока {label}!",
                parse_mode="HTML"
            )
        else:
            context.user_data["admin_add_sheet_target"] = {
                "target_uid": target_uid,
                "label": label,
                "tg_id": tg_id
            }
            await update.message.reply_text(
                f"// Пришлите текст новой анкеты для {label} одним следующим сообщением (или напишите «отмена»).",
                parse_mode="HTML"
            )
        return

    # ==========================
    # АДМИНСКИЙ АРБИТРАЖ ДУЭЛЕЙ (/дуэль_исход, /дуэль_сброс)
    # ==========================
    if lower.startswith("/дуэль_сброс"):
        if not is_admin(sender_user_id):
            await update.message.reply_text("// Эта команда доступна только секундантам модерации, милая.")
            return
        parts = text.split(" ", 1)
        if len(parts) < 2:
            ACTIVE_DUELS.clear()
            await update.message.reply_text("// Все активные дуэли в столице были экстренно прекращены модерацией.")
            return
        target_tag = parts[1].strip().lower()
        removed = False
        for d_k, d_v in list(ACTIVE_DUELS.items()):
            if d_v["p2_uname"] == target_tag or target_tag in d_k:
                ACTIVE_DUELS.pop(d_k, None)
                removed = True
        if removed:
            await update.message.reply_text(f"// Дуэль для <b>{target_tag}</b> успешно сброшена.", parse_mode="HTML")
        else:
            await update.message.reply_text("// Активной дуэли для указанного участника не найдено.")
        return

    if lower.startswith("/дуэль_исход"):
        if not is_admin(sender_user_id):
            await update.message.reply_text("// Команда доступна только модераторам столицы.")
            return
        parts = text.split()
        if len(parts) < 3:
            await update.message.reply_text("// Укажите победителя и побежденного! Пример: `/дуэль_исход @победитель @проигравший`", parse_mode="Markdown")
            return
        win_uname = parts[1].lower()
        lose_uname = parts[2].lower()
        win_uid = USERNAME_CACHE.get(win_uname)
        lose_uid = USERNAME_CACHE.get(lose_uname)

        if not win_uid or not lose_uid:
            await update.message.reply_text("// Не удалось найти ID одного из участников в кэше юзернеймов.")
            return

        end_time_str = get_msk_now().strftime("%H:%M")
        apply_duel_result(win_uid, "win", lose_uname, "Админ-арбитраж", end_time_str)
        apply_duel_result(lose_uid, "loss", win_uname, "Админ-арбитраж", end_time_str)

        await update.message.reply_text(
            f"// ⚔️ <b>Исход дуэли установлен распоряжением модерации!</b>\n"
            f"Победа присуждена: <b>{win_uname}</b>\n"
            f"Поражение записано: <b>{lose_uname}</b>",
            parse_mode="HTML"
        )
        return

    is_reply = update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id

    has_active_state = (
        user_id in WAITING_CHARACTER
        or user_id in WAITING_CHARACTER_UPDATE
        or context.user_data.get("letter_step")
        or context.user_data.get("group_letter_step")
        or context.user_data.get("biz_step")
        or context.user_data.get("edit_sheet_step")
        or context.user_data.get("waiting_status_type")
        or context.user_data.get("waiting_status_remove")
        or context.user_data.get("ad_step")
        or context.user_data.get("waiting_birthday")
        or context.user_data.get("admin_add_sheet_target")
    )

    # ==========================
    # ИНТЕРАКТИВНЫЕ ИВЕНТЫ (/event, /событие)
    # ==========================
    if re.search(r"катя,?\s*(сгенерируй\s+)?событие", lower) or lower in ["/event", "/событие"]:
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей, милая. Создайте анкету сначала.")
            return
            
        active_char = ACTIVE_CHARACTER.get(user_id) or chars[0]
        dear = get_dear(active_char)

        buttons = [
            [InlineKeyboardButton("👤 Одиночное приключение", callback_data=f"ev_mode:single:{sender_user_id}")],
            [InlineKeyboardButton("👥 Совместное приключение", callback_data=f"ev_mode:multi:{sender_user_id}")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"ev_mode:cancel:{sender_user_id}")]
        ]
        await update.message.reply_text(f"// Для кого мы крутим колесо судьбы, {dear}?", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # ==========================
    # ВЫБОР ПЕРСОНАЖА КНОПКАМИ (/мои_персонажи, /my_chars)
    # ==========================
    if lower in ["катя мои персонажи", "катя, мои персонажи", "катя персонажи", "/мои_персонажи", "/персонажи", "/my_chars"]:
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей. Создайте анкету командой /новая_анкета.")
            return
        
        await update.message.reply_text(
            "// Ваши персонажи двора. Нажмите на кнопку, чтобы играть за выбранного:",
            reply_markup=get_my_chars_markup(chars, sender_user_id)
        )
        return

    # ==========================
    # РЕЕСТР (ИНТЕРАКТИВНЫЙ С КНОПКАМИ, МГНОВЕННО)
    # ==========================
    if lower in ["/реестр", "/reestr", "катя реестр", "катя, реестр"]:
        await update.message.reply_text(
            "// <b>🏛️ Императорский Реестр Санкт-Петербурга (1838)</b>\n"
            "Выберите, какие архивы желаете осмотреть:",
            reply_markup=get_registry_menu_markup(sender_user_id),
            parse_mode="HTML"
        )
        return

    if lower in ["список бизнесов", "катя список бизнесов"]:
        text_out, markup = build_registry_businesses_page(sender_user_id, page=0)
        await update.message.reply_text(text_out, reply_markup=markup, parse_mode="HTML")
        return

    # ==========================
    # ПРОСМОТР ОТНОШЕНИЯ КАТИ КОМАНДОЙ (/отношения_кати, /relations)
    # ==========================
    if lower in ["/отношения_кати", "/отношение_кати", "/relations", "катя как ты ко мне относишься", "катя, как ты ко мне относишься", "катя отношение ко мне"]:
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей, милая. Создайте анкету командой /новая_анкета.")
            return
        active_char = ACTIVE_CHARACTER.get(user_id) or chars[0]
        rel_block = format_katya_relationship_block(active_char, include_char_header=True, tg_user_id=sender_user_id)
        save_character_object(user_id, active_char)
        await send_long_html(update.message, rel_block)
        return

    # ==========================
    # ДОБАВЛЕНИЕ / УДАЛЕНИЕ СТАТУСА КНОПКАМИ
    # ==========================
    if re.search(r"катя,?\s+добавь\s+статус", lower) or lower == "/добавить_статус":
        buttons = [
            [InlineKeyboardButton("⏳ Временный статус", callback_data=f"set_status_type:temporary:{sender_user_id}"),
             InlineKeyboardButton("📌 Постоянный статус", callback_data=f"set_status_type:permanent:{sender_user_id}")],
            [InlineKeyboardButton("🗣️ Слух в обществе", callback_data=f"set_status_type:rumors:{sender_user_id}"),
             InlineKeyboardButton("💔 Трагедия", callback_data=f"set_status_type:tragedies:{sender_user_id}")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"set_status_type:cancel:{sender_user_id}")]
        ]
        await update.message.reply_text("// Какой тип статуса вы желаете добавить в анкету, милая?", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if re.search(r"катя,?\s+удали\s+статус", lower) or lower == "/удалить_статус":
        context.user_data["waiting_status_remove"] = True
        await update.message.reply_text("// Укажите точное название статуса, слуха или трагедии, который необходимо убрать из анкеты.")
        return

    # ==========================
    # СЕКРЕТНЫЕ АДМИН-ДИРЕКТИВЫ ПОВЕДЕНИЯ (/отношение)
    # ==========================
    if lower.startswith("/отношение") or lower.startswith("/указание"):
        if not is_admin(sender_user_id):
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
        if not is_admin(sender_user_id):
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
        if not is_admin(sender_user_id):
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
        if not is_admin(sender_user_id):
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
                    if not target_user_id_str:
                        _, target_user_id_str, _, _ = resolve_target_user_from_message(update, target_arg, chat_id)
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
    # КОМАНДА /совет (/advice)
    # ==========================
    if lower.startswith("/совет") or lower.startswith("/advice") or lower.startswith("катя совет") or lower.startswith("катя, совет"):
        user_query = ""
        for pfx in ["/совет", "/advice", "катя, совет", "катя совет"]:
            if lower.startswith(pfx):
                user_query = text[len(pfx):].strip(" ,")
                break

        if not user_query:
            buttons = [
                [InlineKeyboardButton("⚔️ Дуэльный этикет", callback_data=f"adv_topic:duel:{sender_user_id}"),
                 InlineKeyboardButton("🃏 Карты и Штосс", callback_data=f"adv_topic:cards:{sender_user_id}")],
                [InlineKeyboardButton("💃 Балы и язык веера", callback_data=f"adv_topic:balls:{sender_user_id}"),
                 InlineKeyboardButton("💌 Тайные интриги", callback_data=f"adv_topic:intrigue:{sender_user_id}")],
                [InlineKeyboardButton("🎭 Отыгрыш НПС", callback_data=f"adv_topic:npc:{sender_user_id}"),
                 InlineKeyboardButton("💰 Векселя и залоги", callback_data=f"adv_topic:debts:{sender_user_id}")],
                [InlineKeyboardButton("❌ Отмена", callback_data=f"ev_mode:cancel:{sender_user_id}")]
            ]
            await update.message.reply_text(
                "// <b>💡 Наставник высшего света | По какому поводу требуется совет?</b>\n"
                "Выберите интересующую вас тему или задайте вопрос вручную:\n<i>(Пример: <code>/совет как отыграть торг у ростовщика?</code>)</i>",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML"
            )
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
    if is_private and is_admin(sender_user_id) and update.message.reply_to_message:
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
    # КОМАНДЫ И ОБРАБОТКА ПОДАЧИ ОБЪЯВЛЕНИЙ В ГАЗЕТУ (/редакция, /gazette_ad)
    # ==========================
    if lower == "/чат_газеты":
        if not is_admin(sender_user_id):
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
                f"<b>Текст:</b>\n<i>{html.escape(ad_text)}</i>\n\n"
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

    if lower.startswith("/редакция") or lower.startswith("/gazette_ad"):
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
        if not is_admin(sender_user_id):
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
        if not is_admin(sender_user_id):
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
                await update.message.reply_text(f"// Выпуск №{idx_to_del + 1} успешно изъят из архива и сожжён в камине редакции! 🔥")
            else:
                await update.message.reply_text("// Выпуска с таким номером нет в архиве.")
        except ValueError:
            await update.message.reply_text("// Пожалуйста, укажите номер выпуска цифрой.")
        return

    if lower in ["/архив_газет", "/gazette_archive", "катя архив газет", "катя, архив газет"]:
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
        "/дать_достижение", "/достижение", "/реестр", "/reestr", "/заведение",
        "/закрыть_бизнес", "/close_business", "/создать_бизнес", "/create_business",
        "/дуэль", "/duel", "/дуэль_победа", "/дуэль_поражение", "/дуэль_отмена",
        "/статистика", "/stats", "/письмо", "/letter", "/групповое_письмо", "/гписьмо", "/gletter",
        "/clear", "/правила", "/rules", "/пункт", "/пункт_отношений",
        "/добавить_статус", "/удалить_статус", "/изменить_анкету", "/edit_anketa",
        "/анкета", "/anketa", "/новая_анкета", "/new_anketa",
        "/выбрать_персонажа", "/удалить_персонажа", "/отношения_кати", "/отношение_кати", "/relations",
        "/сбросить_отношения", "/сброс_отношений", "/reset_relations",
        "/событие", "/event", "/чат_газеты", "/редакция", "/gazette_ad",
        "/одобрить", "/отклонить", "/изменить", "/выпуск_газеты",
        "/архив_газет", "/gazette_archive", "/удалить_газету", "/удалить_выпуск",
        "/отношение", "/указание", "/сбросить_отношение", "/сбросить_указание", "/список_отношений", "/директивы",
        "/совет", "/advice", "/добавить_анкету", "/удалить_анкету",
        "/добавить_админа", "/удалить_админа", "/админы",
        "/репорт", "/report", "/дуэль_исход", "/дуэль_сброс",
        "/поиск", "/search", "/арт", "/art", "/нарисуй", "/draw",
        "/мои_персонажи", "/персонажи", "/my_chars", "/помощь", "/help"
    ]
    
    is_command = any(lower.startswith(cmd) for cmd in valid_commands)
    is_addressed_to_katya = called_katya(text)
    
    if not is_addressed_to_katya and not is_reply and not has_active_state and not is_command and not is_private:
        return

    # ==========================
    # ВВОД ЛИЧНОЙ ДАТЫ РОЖДЕНИЯ
    # ==========================
    if context.user_data.get("waiting_birthday"):
        if lower in ["отмена", "нет", "отменить", "стоп"]:
            context.user_data.pop("waiting_birthday", None)
            await update.message.reply_text("// Хорошо, ввод даты рождения отменён.")
            return

        day, month = await parse_birthday_with_fallback(text)
        if not day or not month:
            await update.message.reply_text(
                "// Простите, я не смогла разобрать число и месяц. Напишите, пожалуйста, ещё раз "
                "(например: <code>14.11</code>, <code>14 ноября</code> или <code>четырнадцатое ноября</code>) "
                "либо напишите «отмена».",
                parse_mode="HTML"
            )
            return

        b_data = load_birthdays_data()
        uid_key = str(sender_user_id)
        old_entry = b_data["users"].get(uid_key, {})
        b_data["users"][uid_key] = {
            "tg_user_id": sender_user_id,
            "chat_id": chat_id,
            "user_key": user_id,
            "username": username or old_entry.get("username"),
            "first_name": first_name,
            "day": day,
            "month": month,
            "declined": False,
            "last_congratulated_year": old_entry.get("last_congratulated_year"),
        }
        save_birthdays_data(b_data)
        context.user_data.pop("waiting_birthday", None)

        await update.message.reply_text(
            f"// 🎂 Записала вашу личную дату рождения: <b>{format_birthday_date(day, month)}</b>!\n"
            f"В этот день ровно в полночь по Москве я обязательно подниму за вас бокал при дворе.",
            parse_mode="HTML"
        )
        return

    # ==========================
    # ШАГ 2 АДМИНСКОГО ДОБАВЛЕНИЯ АНКЕТЫ
    # ==========================
    admin_target = context.user_data.get("admin_add_sheet_target")
    if admin_target:
        if lower in ["отмена", "нет", "отменить", "стоп"]:
            context.user_data.pop("admin_add_sheet_target", None)
            await update.message.reply_text("// Добавление анкеты отменено.")
            return
        t_uid = admin_target["target_uid"]
        t_label = admin_target["label"]
        t_tg_id = admin_target.get("tg_id")
        context.user_data.pop("admin_add_sheet_target", None)
        working_msg = await update.message.reply_text(f"// Оформляю новую анкету для {t_label}... ✒️", parse_mode="HTML")
        char = await convert_character_sheet(text)
        ensure_katya_relationship(char, t_tg_id)
        add_character(t_uid, char)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
        except Exception:
            pass
        await update.message.reply_text(
            f"// ✅ Персонаж <b>«{html.escape(char.get('name', 'Персонаж'))}»</b> успешно добавлен в анкеты игрока {t_label}!",
            parse_mode="HTML"
        )
        return

    creation_triggers = ["/start", "новая анкета", "создать анкету", "/новая_анкета", "/new_anketa"]
    is_creation_request = any(trigger in lower for trigger in creation_triggers)

    if not is_creation_request and user_id not in WAITING_CHARACTER:
        if user_id not in ACTIVE_CHARACTER or not ACTIVE_CHARACTER[user_id]:
            chars = get_characters(user_id)
            if not chars:
                if (is_addressed_to_katya or is_reply or is_private) and not is_admin(sender_user_id):
                    await update.message.reply_text(
                        f"// *Императорски смотрит на вас через лорнет* "
                        f"Полноте, милая! Прежде чем вести со мной беседы или "
                        f"участвовать в светской жизни Петербурга, необходимо составить анкету "
                        f"и представить себя ко двору. Создайте персонажа (/новая_анкета), чтобы я знала, как с вами общаться!"
                    )
                    return 
            else:
                db = load_characters()
                idx_act = db.get(user_id + "_active", 0)
                if not isinstance(idx_act, int) or not (0 <= idx_act < len(chars)):
                    idx_act = 0
                ACTIVE_CHARACTER[user_id] = chars[idx_act]
            
    if is_admin(sender_user_id) and (lower.startswith("/дать_достижение") or lower.startswith("/достижение")):
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

    if "покажи его анкету" in lower or "покажи анкету" in lower:
        if not is_admin(sender_user_id):
            insults = [
                "// *Крутит у виска веером* Какая дерзость! Эта информация строго конфиденциальна.",
                "// *Выразительно крутит у виска* Полноте, голубчик, вам не положено знать чужие тайны.",
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
            await send_long_html(update.message, format_katya_relationship_block(chars[0], tg_user_id=target_user.id))
            return
            
        buttons = [[InlineKeyboardButton(f"{i}. {c.get('name', 'Без имени')}", callback_data=f"adm_sheet:{target_user_id_str}:{i-1}:{sender_user_id}")] for i, c in enumerate(chars, 1)]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"ev_mode:cancel:{sender_user_id}")])
        await update.message.reply_text("// Выберите анкету для проверки:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # ==========================
    # ПРОСМОТР КОНКРЕТНОГО ЗАВЕДЕНИЯ И УПРАВЛЕНИЕ БИЗНЕСАМИ
    # ==========================
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

    # ЗАКРЫТИЕ БИЗНЕСА С КНОПКАМИ
    if text.strip().lower().startswith("/закрыть_бизнес") or text.strip().lower().startswith("/close_business") or lower in ["катя закрыть бизнес", "катя, закрыть бизнес"]:
        user_businesses = [b for b in CITY_BUSINESSES if b.get("owner_user_id") == user_id]
        if not user_businesses:
            chars = get_characters(user_id)
            char_names = [c.get("name") for c in chars]
            user_businesses = [b for b in CITY_BUSINESSES if b.get("owner_name") in char_names]

        if not user_businesses:
            await update.message.reply_text("// У вас нет зарегистрированных заведений в столице, которые можно было бы закрыть, милая.")
            return

        buttons = [[InlineKeyboardButton(f"🚪 «{b.get('name')}» ({b.get('type')})", callback_data=f"biz_close:{i}:{sender_user_id}")] for i, b in enumerate(user_businesses)]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"ev_mode:cancel:{sender_user_id}")])
        await update.message.reply_text("// Какое из ваших заведений вы желаете закрыть и вычеркнуть из реестра?", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # СОЗДАНИЕ БИЗНЕСА С КНОПКАМИ
    biz_step = context.user_data.get("biz_step")
    if biz_step and lower in ["отмена", "нет", "отменить", "стоп"]:
        context.user_data.pop("biz_step", None)
        context.user_data.pop("biz_owner", None)
        context.user_data.pop("biz_name", None)
        context.user_data.pop("biz_type", None)
        context.user_data.pop("biz_status", None)
        await update.message.reply_text("// Создание заведения отменено, милая.")
        return

    if text.strip().lower().startswith("/создать_бизнес") or text.strip().lower().startswith("/create_business") or lower in ["катя создать бизнес", "катя, создать бизнес"]:
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей, милая. Создайте анкету сначала.")
            return

        active_char = ACTIVE_CHARACTER.get(user_id) or chars[0]
        dear = get_dear(active_char)

        buttons = [[InlineKeyboardButton(f"{i}. {c.get('name', 'Без имени')}", callback_data=f"biz_owner:{i-1}:{sender_user_id}")] for i, c in enumerate(chars, 1)]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"ev_mode:cancel:{sender_user_id}")])
        await update.message.reply_text(f"// Кто из ваших персонажей станет владельцем заведения, {dear}?", reply_markup=InlineKeyboardMarkup(buttons))
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
            f"// Сколько этажей занимает ваше заведение, {mily}? Как оно выглядит изнутри и снаружи, чтобы петербуржцы узнавали его издалека?\n\n"
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
        apply_relationship_update(
            owner_char,
            aff_d=2, resp_d=3,
            memory_event=f"Открыл(а) заведение «{b_name}» в Петербурге",
            is_owner_target=(sender_user_id in OWNER_IDS)
        )
        save_character_object(user_id, owner_char)

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

    if lower.startswith("катя дуэль") or lower.startswith("/дуэль") or lower.startswith("/duel"):
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей, милая. Создайте анкету сначала.")
            return
            
        match = re.search(r'@(\w+)', text)
        if not match:
            await update.message.reply_text("// Милая, вы забыли указать секундантам противника! Обязательно напишите его @username. Пример: `/дуэль @username`", parse_mode="Markdown")
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
                await update.message.reply_text(f"// {dear}, у вашего персонажа уже назначена дуэль! Завершите ее (/дуэль_победа / «катя я выиграл дуэль») либо отмените (/дуэль_отмена).")
                return
            if (d_data["p2_uname"] == opp_uname or d_data["p2_uname"] == my_uname) and d_data["p2_uname"] != "":
                 await update.message.reply_text(f"// Секунданты докладывают, что один из вас уже стоит на барьере в другой дуэли. Дождитесь её окончания.")
                 return

        ACTIVE_DUELS[user_id] = {
            "p1_id": user_id,
            "p1_name": active_char.get("name", "Безымянный"),
            "p2_uname": opp_uname,
            "start_time": time.time(),
            "posts": []
        }
        await update.message.reply_text(f"// Секунданты разошлись по позициям на Черной речке против <b>{opp_uname}</b>, {mily}. Дуэль началась! Пишите свои посты — Екатерина лично следит за поединком. В конце сообщите об исходе (/дуэль_победа / /дуэль_поражение или «катя я выиграл/проиграл дуэль») либо отмените вызов (/дуэль_отмена). ⚔️", parse_mode="HTML")
        return

    duel_end_phrases = [
        "катя дуэль отменена", "катя, дуэль отменена", "/дуэль_отмена",
        "катя я выйграл дуэль", "катя я выиграл дуэль", "катя, я выйграл дуэль", "катя, я выиграл дуэль", "/дуэль_победа",
        "катя я проиграл дуэль", "катя, я проиграл дуэль", "/дуэль_поражение"
    ]
    if lower in duel_end_phrases:
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
        duel_posts = d.get("posts", [])
        p2_id = USERNAME_CACHE.get(p2_uname)
        
        duration = get_status_duration(d["start_time"])
        end_time_str = get_msk_now().strftime("%H:%M")
        
        p2_name = "Неизвестный противник"
        if p2_id:
            p2_chars = get_characters(p2_id)
            if p2_chars:
                p2_active = ACTIVE_CHARACTER.get(p2_id) or p2_chars[0]
                p2_name = p2_active.get("name", p2_uname)
        else:
            p2_name = p2_uname
            
        if "отмен" in lower:
            apply_duel_result(p1_id, "cancel", p2_name, duration, end_time_str)
            if p2_id:
                apply_duel_result(p2_id, "cancel", p1_name, duration, end_time_str)
            await update.message.reply_text("// Дуэль благополучно отменена по обоюдному согласию, секунданты спрятали пистолеты в кобуру. Честь сохранена для обоих.")
            return
            
        if "выиграл" in lower or "выйграл" in lower or "победа" in lower:
            if is_initiator:
                p1_res, p2_res = "win", "loss"
                winner_name, loser_name = p1_name, p2_name
                loser_uid = p2_id
            else:
                p1_res, p2_res = "loss", "win"
                winner_name, loser_name = p2_name, p1_name
                loser_uid = p1_id
        else:
            if is_initiator:
                p1_res, p2_res = "loss", "win"
                winner_name, loser_name = p2_name, p1_name
                loser_uid = p1_id
            else:
                p1_res, p2_res = "win", "loss"
                winner_name, loser_name = p1_name, p2_name
                loser_uid = p2_id

        ach1 = apply_duel_result(p1_id, p1_res, p2_name, duration, end_time_str)
        ach2 = []
        if p2_id:
            ach2 = apply_duel_result(p2_id, p2_res, p1_name, duration, end_time_str)

        working_msg = await update.message.reply_text("// Лейб-медики спешат к барьеру, Екатерина выносит дуэльный вердикт... 🩸")
        verdict_text, injury_name = await generate_duel_verdict_and_injury(winner_name, loser_name, duel_posts)
        
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
        except Exception:
            pass

        if loser_uid:
            l_chars = get_characters(loser_uid)
            if l_chars:
                db = load_characters()
                active_idx = db.get(loser_uid + "_active", 0)
                if active_idx >= len(l_chars):
                    active_idx = 0
                loser_char = l_chars[active_idx]
                if "changes" not in loser_char: loser_char["changes"] = {"permanent": [], "temporary": [], "achievements": [], "tragedies": [], "rumors": []}
                if "status_times" not in loser_char: loser_char["status_times"] = {}
                
                loser_char["changes"]["temporary"].append(injury_name)
                loser_char["status_times"][injury_name] = time.time()
                l_chars[active_idx] = loser_char
                save_characters(loser_uid, l_chars)
                ACTIVE_CHARACTER[loser_uid] = loser_char
            
        msg = (
            f"⚔️ <b>ДУЭЛЬНЫЙ ВЕРДИКТ ЕЕ ВЕЛИЧЕСТВА</b>\n"
            f"ㅤㅤ∴ ════ ∴ ✦ ∴ ════ ∴\n"
            f"🏆 <b>Триумфатор:</b> {winner_name}\n"
            f"🩸 <b>Побежденный:</b> {loser_name}\n\n"
            f"<blockquote expandable>{verdict_text}</blockquote>\n\n"
            f"🩹 <b>Получен статус травмы:</b> «{injury_name}» (записан в анкету {loser_name})"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
        
        if ach1 and is_initiator:
            await update.message.reply_text(f"// ✨ За дуэльные интриги вы получаете достижение: <b>{', '.join(ach1)}</b>!", parse_mode="HTML")
        elif ach2 and not is_initiator:
            await update.message.reply_text(f"// ✨ За дуэльные интриги вы получаете достижение: <b>{', '.join(ach2)}</b>!", parse_mode="HTML")
        return

    # Ввод текста для выбранного типа статуса
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
                    char.setdefault("status_times", {})[st_text] = time.time()
                elif st_cat == "achievements":
                    char.setdefault("achievement_times", {})[st_text] = time.time()
                    
                clean_expired_statuses_and_rumors(char)
                save_character_object(user_id, char)
                success = True

        if success:
            curr_char = ACTIVE_CHARACTER[user_id]
            unlocked = check_character_achievements(user_id, curr_char, "status_added", 1)
            save_character_object(user_id, curr_char)
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
                save_character_object(user_id, char)
                found = True
                await update.message.reply_text(f"// Статус «{st_text}» успешно убран из анкеты, милая.")
                break
                
        if not found:
            await update.message.reply_text(f"// Я не нашла статус «{st_text}» в анкете. Проверь точное название.")
        return

    match = re.search(r"(?:катя,?\s+выбери\s+персонажа|/выбрать_персонажа)\s+(\d+)", lower)
    if match:
        idx = int(match.group(1)) - 1
        if set_active_character_local(user_id, idx):
            curr_char = ACTIVE_CHARACTER[user_id]
            unlocked = check_character_achievements(user_id, curr_char, "character_switches", 1)
            save_character_object(user_id, curr_char)
            await send_long_html(update.message, "// Хорошо. Теперь играем за:\n\n" + format_character_sheet(curr_char))
            await send_long_html(update.message, format_katya_relationship_block(curr_char, tg_user_id=sender_user_id))
            if unlocked:
                await update.message.reply_text(f"// ✨ За смену масок персонаж получает достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        else:
            await update.message.reply_text("// Такого персонажа нет.")
        return

    # УДАЛЕНИЕ ПЕРСОНАЖА (ВСЕГДА С ПОДТВЕРЖДЕНИЕМ КНОПКАМИ)
    match_del = re.search(r"(?:катя,?\s+удали\s+персонажа|/удалить_персонажа)\s+(\d+)", lower)
    if match_del:
        idx = int(match_del.group(1)) - 1
        chars = get_characters(user_id)
        if 0 <= idx < len(chars):
            rem = chars[idx]
            c_name = html.escape(str(rem.get("name", "Без имени")))
            confirm_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Да, удалить навсегда", callback_data=f"del_char:confirm:{idx}:{sender_user_id}")],
                [InlineKeyboardButton("❌ Нет, оставить", callback_data=f"del_char:cancel:{sender_user_id}")]
            ])
            await update.message.reply_text(
                f"// ⚠️ Вы точно хотите безвозвратно удалить персонажа <b>«{c_name}»</b>?",
                reply_markup=confirm_kb,
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("// Неверный номер персонажа.")
        return

    if lower in ["/удалить_персонажа", "катя удали персонажа", "катя, удали персонажа"]:
        chars = get_characters(user_id)
        if not chars:
            await update.message.reply_text("// У вас пока нет сохранённых персонажей.")
            return
        buttons = [[InlineKeyboardButton(f"🗑️ {i}. {c.get('name', 'Без имени')}", callback_data=f"del_char:pick:{i-1}:{sender_user_id}")] for i, c in enumerate(chars, 1)]
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"del_char:cancel:{sender_user_id}")])
        await update.message.reply_text("// Какого персонажа вы хотите удалить?", reply_markup=InlineKeyboardMarkup(buttons))
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

    if re.search(r"катя,?\s+измени\s+анкету", lower) or lower in ["/изменить_анкету", "/edit_anketa"]:
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
    # ПРОСМОТР СВОЕЙ АНКЕТЫ (+ ОТНОШЕНИЕ КАТИ ВНИЗУ В ЦИТАТЕ)
    # ==========================
    if any(phrase in lower for phrase in ["моя анкета", "текущий персонаж", "покажи мою анкету"]) or lower in ["катя анкета", "катя, анкета", "анкета", "/анкета", "/anketa"]:
        char = ACTIVE_CHARACTER.get(user_id)
        if not char:
            chars = get_characters(user_id)
            if chars:
                ACTIVE_CHARACTER[user_id] = chars[0]
                char = chars[0]
            else:
                await update.message.reply_text("// У вас пока нет сохранённых персонажей. Создайте анкету командой /новая_анкета.")
                return
        
        unlocked = check_character_achievements(user_id, char, "rumors_check", 1)
        sheet_res = format_character_sheet(char)
        rel_res = format_katya_relationship_block(char, tg_user_id=sender_user_id)
        await send_long_html(update.message, sheet_res)
        await send_long_html(update.message, rel_res)
        if unlocked:
            await update.message.reply_text(f"// ✨ За громкие сплетни персонаж получает достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
            
        save_character_object(user_id, char)
        return

    if text.strip().lower().startswith("/статистика") or text.strip().lower().startswith("/stats") or lower.startswith("катя, статистика") or lower.startswith("катя статистика"):
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
    # Одиночные письма (С КНОПКАМИ ВЫБОРА АВТОРА И ШАБЛОНОМ)
    # ==========================
    letter_step = context.user_data.get("letter_step")
    if letter_step and lower in ["отмена", "нет", "отменить", "стоп"]:
        context.user_data.pop("letter_step", None)
        context.user_data.pop("letter_query", None)
        context.user_data.pop("letter_char_idx", None)
        context.user_data.pop("letter_npc_char", None)
        await update.message.reply_text("// Составление письма отменено, милая.")
        return

    if letter_step == "npc_details":
        npc_info = text.strip()
        letter_query = context.user_data.pop("letter_query", "").strip()
        npc_char = {"name": npc_info, "title": "НПС", "estate": "НПС", "gender": "—", "age": "—", "personality": f"НПС: {npc_info}", "history": f"НПС: {npc_info}"}

        if letter_query:
            context.user_data.pop("letter_step", None)
            working_msg = await update.message.reply_text(f"// Перо скрипит по бумаге, составляю послание от лица НПС ({npc_info})... ✍️")
            answer = await rewrite_letter_command_with_char(letter_query, npc_char)
            try: await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
            except Exception: pass

            if not answer or answer.startswith("// Ох"):
                await context.bot.send_message(chat_id=chat_id, text="// Простите, чернильница опрокинулась, письмо от лица НПС не сошлось строчками.")
            else:
                await context.bot.send_message(chat_id=chat_id, text=answer, parse_mode="HTML")
        else:
            context.user_data["letter_step"] = "content"
            context.user_data["letter_npc_char"] = npc_char
            msg = (
                f"// ✍️ Прекрасно! Пишем от лица НПС (<b>{html.escape(npc_info)}</b>).\n\n"
                f"Отправьте содержание письма по шаблону одним сообщением:\n"
                f"<b>1. Кому:</b> [Титул, имя получателя и @username]\n"
                f"<b>2. Суть письма:</b> [О чём пишем — приглашение, признание, интрига, дело]\n"
                f"<b>3. Тон и детали:</b> [Вежливый, язвительный, тёплый, строгий]\n\n"
                f"<i>(Или напишите «отмена»)</i>"
            )
            await update.message.reply_text(msg, parse_mode="HTML")
        return

    if letter_step == "content":
        idx = context.user_data.pop("letter_char_idx", 0)
        npc_char = context.user_data.pop("letter_npc_char", None)
        context.user_data.pop("letter_step", None)

        if npc_char:
            chosen_char = npc_char
        else:
            chars = get_characters(user_id)
            chosen_char = chars[idx] if 0 <= idx < len(chars) else None

        working_msg = await update.message.reply_text("// Перо скрипит по бумаге, составляю послание... ✍️")
        answer = await rewrite_letter_command_with_char(text, chosen_char)
        try: await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
        except Exception: pass

        unlocked = []
        if not npc_char and chosen_char:
            unlocked = check_character_achievements(user_id, chosen_char, "letters_written", 1)
            save_character_object(user_id, chosen_char)

        await context.bot.send_message(chat_id=chat_id, text=answer, parse_mode="HTML")
        if unlocked:
            await context.bot.send_message(chat_id=chat_id, text=f"// ✨ За ваши труды персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        return

    if lower.startswith("/письмо") or lower.startswith("/letter"):
        letter_query = ""
        for pfx in ["/письмо", "/letter"]:
            if lower.startswith(pfx):
                letter_query = text.strip()[len(pfx):].strip()
                break
        try:
            if update.message: await update.message.delete()
        except Exception: pass

        chars = get_characters(user_id)
        context.user_data["letter_query"] = letter_query

        buttons = [[InlineKeyboardButton(f"{i}. {c.get('name', 'Без имени')}", callback_data=f"let_char:{i-1}:{sender_user_id}")] for i, c in enumerate(chars, 1)]
        buttons.append([InlineKeyboardButton("🎭 Написать от лица НПС", callback_data=f"let_char:{len(chars)}:{sender_user_id}")])
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"ev_mode:cancel:{sender_user_id}")])

        await context.bot.send_message(chat_id=chat_id, text="// Кто пишет это послание?", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # ==========================
    # Групповые письма (С КНОПКАМИ)
    # ==========================
    group_letter_step = context.user_data.get("group_letter_step")
    if group_letter_step and lower in ["отмена", "нет", "отменить", "стоп"]:
        context.user_data.pop("group_letter_step", None)
        context.user_data.pop("group_letter_char_idx", None)
        context.user_data.pop("group_letter_npc_char", None)
        context.user_data.pop("group_letter_query", None)
        await update.message.reply_text("// Составление группового письма отменено.")
        return

    if group_letter_step == "npc_details":
        npc_info = text.strip()
        existing_query = context.user_data.get("group_letter_query", "").strip()
        npc_char = {"name": npc_info, "title": "НПС", "estate": "НПС", "gender": "—", "age": "—", "personality": f"НПС: {npc_info}", "history": f"НПС: {npc_info}"}

        if existing_query:
            context.user_data.pop("group_letter_step", None)
            context.user_data.pop("group_letter_query", None)
            working_msg = await update.message.reply_text(f"// Составляю групповые послания от НПС ({npc_info})... ✍️")
            answer = await generate_group_letters_command_with_char(existing_query, npc_char)
            try: await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
            except Exception: pass
            await send_raw_long_message(update.message, answer)
        else:
            context.user_data["group_letter_step"] = "content"
            context.user_data["group_letter_npc_char"] = npc_char
            msg = f"// Прекрасно! Пишем от лица НПС ({npc_info}). Теперь опишите тему, адресатов и особенности одним сообщением:"
            await update.message.reply_text(msg)
        return

    if group_letter_step == "content":
        idx = context.user_data.get("group_letter_char_idx", 0)
        npc_char = context.user_data.pop("group_letter_npc_char", None)
        context.user_data.pop("group_letter_step", None)
        context.user_data.pop("group_letter_char_idx", None)

        if npc_char: chosen_char = npc_char
        else:
            chars = get_characters(user_id)
            chosen_char = chars[idx] if 0 <= idx < len(chars) else None

        working_msg = await update.message.reply_text(f"// Составляю групповые послания... ✍️")
        answer = await generate_group_letters_command_with_char(text, chosen_char)
        try: await context.bot.delete_message(chat_id=chat_id, message_id=working_msg.message_id)
        except Exception: pass

        await send_raw_long_message(update.message, answer)
        return

    if lower.startswith("/групповое_письмо") or lower.startswith("/гписьмо") or lower.startswith("/gletter"):
        group_query = text.strip().split(" ", 1)[1] if " " in text.strip() else ""
        try:
            if update.message: await update.message.delete()
        except Exception: pass

        chars = get_characters(user_id)
        context.user_data["group_letter_query"] = group_query

        buttons = [[InlineKeyboardButton(f"{i}. {c.get('name', 'Без имени')}", callback_data=f"glet_char:{i-1}:{sender_user_id}")] for i, c in enumerate(chars, 1)]
        buttons.append([InlineKeyboardButton("🎭 Написать от лица НПС", callback_data=f"glet_char:{len(chars)}:{sender_user_id}")])
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"ev_mode:cancel:{sender_user_id}")])

        await context.bot.send_message(chat_id=chat_id, text="// Кто пишет эти групповые послания?", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if lower in ["катя новая анкета", "катя, новая анкета", "/новая_анкета", "/new_anketa"]:
        WAITING_CHARACTER[user_id] = True
        msg_prompt = await update.message.reply_text("// Пришлите анкету нового персонажа (Имя, Возраст, Пол, Внешность и т.д.).")
        context.user_data["new_char_msg_ids"] = [update.message.message_id, msg_prompt.message_id]
        return

    if WAITING_CHARACTER.get(user_id):
        char = await convert_character_sheet(text)
        ensure_katya_relationship(char, sender_user_id)
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
            await maybe_ask_birthday_after_first_char(update.message, user_id, sender_user_id, chat_id)
        return

    if user_id in WAITING_CHARACTER_UPDATE:
        char = update_character_from_text(WAITING_CHARACTER_UPDATE[user_id], text)
        WAITING_CHARACTER_UPDATE.pop(user_id, None)
        ensure_katya_relationship(char, sender_user_id)
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
        await maybe_ask_birthday_after_first_char(update.message, user_id, sender_user_id, chat_id)
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
        save_character_object(user_id, active_char)
        
        await update.message.reply_text("// Начнем наш диалог сначала.")
        if unlocked:
            await update.message.reply_text(f"// ✨ За способность начать с чистого листа персонаж получает достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        return

    if lower in ["катя правила", "катя, правила", "/правила", "/rules"]:
        full_rules = RULES + "\n\n--- ПРАВИЛА ОТНОШЕНИЙ ---\n\n" + RELATIONSHIP_RULES
        active_char = ACTIVE_CHARACTER.get(user_id)
        unlocked = check_character_achievements(user_id, active_char, "rules_checked", 1)
        save_character_object(user_id, active_char)
        await send_long_message(update.message, full_rules)
        if unlocked:
            await update.message.reply_text(f"// ✨ За усердное изучение законов персонаж получает тайное достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        return

    match_rel_punct = re.search(r"(?:катя,?\s+пункт\s+отношений|/пункт_отношений)\s+(\d+)", lower)
    if match_rel_punct:
        num = match_rel_punct.group(1)
        rule_text = get_rule_by_number(num, is_relationship=True)
        active_char = ACTIVE_CHARACTER.get(user_id)
        unlocked = check_character_achievements(user_id, active_char, "rules_checked", 1)
        save_character_object(user_id, active_char)
        if rule_text:
            await update.message.reply_text(f"<blockquote expandable>{rule_text}</blockquote>", parse_mode="HTML")
            if unlocked:
                await update.message.reply_text(f"// ✨ Персонаж получает достижение: <b>{', '.join(unlocked)}</b>!", parse_mode="HTML")
        else:
            await update.message.reply_text("// Такого пункта в правилах отношений нет.")
        return

    match_punct = re.search(r"(?:катя,?\s+пункт|/пункт)\s+(\d+)", lower)
    if match_punct:
        num = match_punct.group(1)
        rule_text = get_rule_by_number(num, is_relationship=False)
        active_char = ACTIVE_CHARACTER.get(user_id)
        unlocked = check_character_achievements(user_id, active_char, "rules_checked", 1)
        save_character_object(user_id, active_char)
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
            save_character_object(user_id, active_char)
            
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

        cur_hour_msg = get_msk_now().hour
        if 1 <= cur_hour_msg < 5:
            unlocked_night = check_character_achievements(user_id, active_char, "night_messages", 1)

    text_with_context = text
    if is_reply and update.message.reply_to_message.text:
        raw_reply_text = update.message.reply_to_message.text.replace("//", "").strip()
        text_with_context = f"[Это ответ на твою реплику: \"{raw_reply_text}\"]\n\n{text}"

    answer = await ask_katya(text_with_context, user_id, username)
    try:
        await update.message.reply_text(answer, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(answer)

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
        save_character_object(user_id, active_char)

    # ==========================
    # АНАЛИЗ ДИАЛОГОВ: СТАТУСЫ/СЛУХИ + ЖИВЫЕ ОТНОШЕНИЯ + РЕАКЦИИ КАТИ
    # ==========================
    analysis_res = await analyze_dialogue_for_status_and_relationship(text, answer, user_id)
    reaction_emoji = analysis_res.get("reaction") if isinstance(analysis_res, dict) else None
    if reaction_emoji:
        try:
            await update.message.set_reaction([ReactionTypeEmoji(reaction_emoji)])
        except Exception as e:
            print("Не удалось поставить реакцию:", e)

    status_analysis = analysis_res.get("status") if isinstance(analysis_res, dict) else None
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
                    target_char_name = tc.get("name")
                    break

        if st_text:
            if action == "add":
                proposal = f"// Кажется, в сюжете произошло событие. Добавить статус «{st_text}» в {target_display_name}?"
            else:
                proposal = f"// Кажется, статус «{st_text}» больше не актуален. Удалить его из {target_display_name}?"

            context.user_data["pending_status"] = {
                "action": action,
                "type": st_type,
                "text": st_text,
                "target_uid": target_uid,
                "target_char_name": target_char_name
            }

            confirm_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Записать", callback_data=f"status_confirm:apply:{sender_user_id}"),
                 InlineKeyboardButton("❌ Отклонить", callback_data=f"status_confirm:cancel:{sender_user_id}")]
            ])
            await update.message.reply_text(proposal, reply_markup=confirm_markup)

    print("Запрос:", text)

# ==========================
# Запуск бота
# ==========================

def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    print("Екатерина запускается...")
    
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(60.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photos))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(CallbackQueryHandler(callbacks_handler))
    app.add_handler(MessageHandler(filters.TEXT, messages))

    print("Екатерина запущена!")
    app.run_polling(drop_pending_updates=True, timeout=30, bootstrap_retries=-1)

if __name__ == "__main__":
    main()



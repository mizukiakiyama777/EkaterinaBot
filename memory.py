import os
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY

# Инициализация подключения к базе
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

USER_MEMORY = {}
USER_PROFILE = {}
VISUAL_MEMORY = {}

def make_user_key(user_id):
    return str(user_id)

def load_user_data_from_db(user_id):
    """Подгружает данные пользователя из Supabase при необходимости."""
    uid = make_user_key(user_id)
    if not supabase:
        return
    try:
        res = supabase.table("bot_memory").select("*").eq("user_id", uid).execute()
        if res.data and len(res.data) > 0:
            row = res.data[0]
            USER_MEMORY[uid] = row.get("memory", [])
            USER_PROFILE[uid] = row.get("profile", {})
            VISUAL_MEMORY[uid] = row.get("visual_memory", [])
        else:
            USER_MEMORY.setdefault(uid, [])
            USER_PROFILE.setdefault(uid, {})
            VISUAL_MEMORY.setdefault(uid, [])
    except Exception as e:
        print(f"Ошибка загрузки памяти из Supabase для {uid}:", e)

def sync_user_to_db(user_id):
    """Атомарно сохраняет память пользователя в облако Supabase."""
    uid = make_user_key(user_id)
    if not supabase:
        return
    try:
        supabase.table("bot_memory").upsert({
            "user_id": uid,
            "memory": USER_MEMORY.get(uid, []),
            "profile": USER_PROFILE.get(uid, {}),
            "visual_memory": VISUAL_MEMORY.get(uid, [])
        }).execute()
    except Exception as e:
        print(f"Ошибка сохранения памяти в Supabase для {uid}:", e)

def get_memory(user_id):
    key = make_user_key(user_id)
    if key not in USER_MEMORY:
        load_user_data_from_db(key)
    return USER_MEMORY.get(key, [])

def add_memory(user_id, message):
    key = make_user_key(user_id)
    history = get_memory(user_id)
    history.append(message)
    sync_user_to_db(key)

def save_memory():
    # Синхронизируем всех измененных пользователей в памяти
    for uid in list(USER_MEMORY.keys()):
        sync_user_to_db(uid)

def clear_user_memory(user_id):
    key = make_user_key(user_id)
    USER_MEMORY[key] = []
    sync_user_to_db(key)

def get_profile(user_id):
    key = make_user_key(user_id)
    if key not in USER_PROFILE:
        load_user_data_from_db(key)
    return USER_PROFILE.get(key, {})

def set_profile(user_id, field_key, value):
    key = make_user_key(user_id)
    if key not in USER_PROFILE:
        load_user_data_from_db(key)
    USER_PROFILE[key][field_key] = value
    sync_user_to_db(key)

def get_visual_memory(user_id):
    key = make_user_key(user_id)
    if key not in VISUAL_MEMORY:
        load_user_data_from_db(key)
    return VISUAL_MEMORY.get(key, [])

def save_visual_memory(user_id, item):
    key = make_user_key(user_id)
    memory = get_visual_memory(user_id)
    memory.append(item)
    sync_user_to_db(key)

def clear_visual_memory(user_id):
    key = make_user_key(user_id)
    VISUAL_MEMORY[key] = []
    sync_user_to_db(key)
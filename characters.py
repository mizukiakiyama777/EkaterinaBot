import time
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Кэш персонажей в оперативной памяти
CHARACTERS_CACHE = {}

def load_characters():
    """Загружает всех персонажей из Supabase в кэш."""
    global CHARACTERS_CACHE
    if not supabase:
        return CHARACTERS_CACHE
    try:
        res = supabase.table("characters").select("*").execute()
        db_data = {}
        if res.data:
            for row in res.data:
                uid = str(row["user_id"])
                db_data[uid] = row.get("characters", [])
                db_data[uid + "_active"] = row.get("active_index", 0)
        CHARACTERS_CACHE = db_data
        return CHARACTERS_CACHE
    except Exception as e:
        print("Ошибка загрузки characters из Supabase:", e)
        return CHARACTERS_CACHE

def write_characters(data):
    """Синхронизирует персонажей в Supabase."""
    global CHARACTERS_CACHE
    CHARACTERS_CACHE = data
    if not supabase:
        return
    try:
        for key, val in data.items():
            if not key.endswith("_active") and isinstance(val, list):
                uid = str(key)
                active_idx = data.get(uid + "_active", 0)
                supabase.table("characters").upsert({
                    "user_id": uid,
                    "characters": val,
                    "active_index": active_idx
                }).execute()
    except Exception as e:
        print("Ошибка записи characters в Supabase:", e)

def get_characters(user_id):
    uid = str(user_id)
    if uid not in CHARACTERS_CACHE:
        load_characters()
    return CHARACTERS_CACHE.get(uid, [])

def save_characters(user_id, characters):
    uid = str(user_id)
    CHARACTERS_CACHE[uid] = characters
    active_idx = CHARACTERS_CACHE.get(uid + "_active", 0)
    if supabase:
        try:
            supabase.table("characters").upsert({
                "user_id": uid,
                "characters": characters,
                "active_index": active_idx
            }).execute()
        except Exception as e:
            print(f"Ошибка сохранения персонажей для {uid}:", e)

def save_character_list(user_id, characters):
    save_characters(user_id, characters)

def add_character(user_id, character):
    uid = str(user_id)
    characters = get_characters(uid)

    if "changes" not in character or not isinstance(character["changes"], dict):
        character["changes"] = {
            "permanent": [],
            "temporary": [],
            "achievements": [],
            "tragedies": [],
            "rumors": []
        }

    characters.append(character)
    save_characters(uid, characters)

def remove_character(user_id, index):
    uid = str(user_id)
    characters = get_characters(uid)
    if not characters or index < 0 or index >= len(characters):
        return False
    characters.pop(index)
    save_characters(uid, characters)
    return True

def set_active_character(user_id, index):
    uid = str(user_id)
    characters = get_characters(uid)
    if index < 0 or index >= len(characters):
        return False
    CHARACTERS_CACHE[uid + "_active"] = index
    save_characters(uid, characters)
    return True

def get_active_character(user_id):
    uid = str(user_id)
    characters = get_characters(uid)
    if not characters:
        return None
    index = CHARACTERS_CACHE.get(uid + "_active", 0)
    if index is not None and 0 <= index < len(characters):
        return characters[index]
    return characters[0]

def add_character_change(user_id, change_type, text):
    uid = str(user_id)
    characters = get_characters(uid)
    if not characters:
        return False
    
    active_index = CHARACTERS_CACHE.get(uid + "_active", 0)
    if active_index >= len(characters):
        active_index = 0
        
    char = characters[active_index]
    if "changes" not in char or not isinstance(char["changes"], dict):
        char["changes"] = {"permanent": [], "temporary": [], "achievements": [], "tragedies": [], "rumors": []}
    if "status_times" not in char:
        char["status_times"] = {}
    if change_type not in char["changes"]:
        char["changes"][change_type] = []
        
    if text not in char["changes"][change_type]:
        char["changes"][change_type].append(text)
        if change_type in ["temporary", "achievements"]:
            char["status_times"][text] = time.time()
        save_characters(uid, characters)
        return True
    return False

def remove_character_change(user_id, change_type, text):
    uid = str(user_id)
    characters = get_characters(uid)
    if not characters:
        return False
    active_index = CHARACTERS_CACHE.get(uid + "_active", 0)
    if active_index >= len(characters):
        active_index = 0
    char = characters[active_index]
    if "changes" in char and change_type in char["changes"]:
        if text in char["changes"][change_type]:
            char["changes"][change_type].remove(text)
            if "status_times" in char and text in char["status_times"]:
                del char["status_times"][text]
            save_characters(uid, characters)
            return True
    return False

def get_character_changes(user_id):
    active = get_active_character(user_id)
    if not active:
        return {}
    return active.get("changes", {})

def clear_character_temporary_changes(user_id):
    uid = str(user_id)
    characters = get_characters(uid)
    if not characters:
        return
    active_index = CHARACTERS_CACHE.get(uid + "_active", 0)
    if active_index >= len(characters):
        active_index = 0
    char = characters[active_index]
    if "changes" in char:
        temp_statuses = char["changes"].get("temporary", [])
        if "status_times" in char:
            for status in temp_statuses:
                if status in char["status_times"]:
                    del char["status_times"][status]
        char["changes"]["temporary"] = []
        save_characters(uid, characters)

# Первая загрузка данных из базы при старте
load_characters()
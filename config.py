import os
from dotenv import load_dotenv

# Загружаем переменные из файла .env
load_dotenv()

# ==========================
# Telegram Настройки
# ==========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

OWNER_ID = int(os.getenv("OWNER_ID", "727703718"))
ROLE_OWNER_ID = int(os.getenv("ROLE_OWNER_ID", "5714213307"))

# ==========================
# Supabase База Данных
# ==========================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ==========================
# Gemini AI Настройки
# ==========================
GEMINI_KEYS = [
    os.getenv(f"GEMINI_KEY_{i}")
    for i in range(1, 9)
]
GEMINI_KEYS = [key for key in GEMINI_KEYS if key]

# Разграничение моделей (не выше gemini-3.6-flash)
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.6-flash")
MODEL_SMART = os.getenv("MODEL_SMART", "gemini-3.6-flash")
MODEL_LITE = os.getenv("MODEL_LITE", "gemini-3.5-flash-lite")

# ==========================
# Логи при запуске
# ==========================
print("OWNER_ID:", OWNER_ID)
print("ROLE_OWNER_ID:", ROLE_OWNER_ID)
print("Найдено Gemini ключей:", len(GEMINI_KEYS))
print("Умная модель:", MODEL_SMART, "| Лёгкая модель:", MODEL_LITE)
print("Подключение к Supabase:", "✅ Настроено" if SUPABASE_URL and SUPABASE_KEY else "❌ Ключи не найдены в .env")
from google import genai
from config import GEMINI_KEYS

# Инициализируем клиента с ключом
client = genai.Client(api_key=GEMINI_KEYS[0])

# Отправляем запрос к актуальной модели
response = client.models.generate_content(
    model="gemini-3.6-flash",
    contents="Привет! Ответь одним словом: работает?"
)

print(response.text)
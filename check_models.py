from google import genai
from config import GEMINI_KEYS


if not GEMINI_KEYS:
    print("Ключи Gemini не найдены")
    exit()


client = genai.Client(
    api_key=GEMINI_KEYS[0]
)


print("Доступные модели:\n")


for model in client.models.list():

    print(model.name)
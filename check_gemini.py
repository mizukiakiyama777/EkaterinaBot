from google import genai

from config import GEMINI_KEYS, MODEL_NAME


print("Всего ключей:", len(GEMINI_KEYS))
print("========================")


for i, key in enumerate(GEMINI_KEYS, start=1):

    print(
        f"\nПроверяю ключ {i}: {key[:8]}..."
    )

    try:

        client = genai.Client(
            api_key=key
        )

        response = client.models.generate_content(
            model=MODEL_NAME,
            contents="Ответь одним словом: работает"
        )

        print(
            "✅ РАБОТАЕТ:",
            response.text
        )


    except Exception as e:

        print(
            "❌ ОШИБКА:"
        )

        print(e)
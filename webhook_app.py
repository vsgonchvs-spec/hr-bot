# webhook_app.py
import os
import asyncio
from fastapi import FastAPI, Request, Header, HTTPException
from aiogram import types

# ВАЖНО: импортируем ИЗ твоего main.py уже созданные bot и dp, а также загружаемые штуки.
# При импорте main.py НЕ запустит polling, потому что у тебя там guard:
# if __name__ == "__main__": asyncio.run(main())
from main import bot, dp  # ничего из main.py удалять/менять не нужно

app = FastAPI()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # секрет для Telegram header-а
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")  # путь, по которому Telegram будет стучаться
WEBHOOK_URL   = os.getenv("WEBHOOK_URL", "")  # полный публичный URL (https://.../webhook)

@app.on_event("startup")
async def on_startup():
    # Ставим вебхук, если задан URL (после первого деплоя ты его пропишешь в переменных Render)
    if WEBHOOK_URL:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        await bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET or None)

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await bot.session.close()
    except Exception:
        pass

@app.get("/")
async def health():
    return {"ok": True, "mode": "webhook", "webhook_url": WEBHOOK_URL or "(not set)"}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None)
):
    # Проверка секрета из заголовка Telegram (если установлен)
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret token")

    data = await request.json()
    update = types.Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

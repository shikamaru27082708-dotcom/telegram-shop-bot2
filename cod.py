# === ЗАПУСК БОТА ЧЕРЕЗ ВЕБХУКИ ===
async def on_startup(app: web.Application):
    print("🚀 Устанавливаем вебхук...")
    await bot.delete_webhook()
    webhook_url = f"https://telegram-shop-bot2.onrender.com/webhook"
    await bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True
    )
    print(f"✅ Вебхук установлен на {webhook_url}")

# Создаём aiohttp приложение
app = web.Application()

async def handle_root(request):
    return web.json_response({
        "status": "running",
        "bot": "Telegram Shop Bot",
        "message": "Бот работает"
    })

app.router.add_get('/', handle_root)
app.router.add_get('/health', handle_root)

webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
webhook_requests_handler.register(app, path="/webhook")

app.on_startup.append(on_startup)

# Точка входа
async def main():
    await init_db_pool()
    await create_tables()
    await add_initial_products_async()
    port = int(os.environ.get('PORT', 10000))
    print(f"🚀 Запуск aiohttp сервера на порту {port}...")
    await web.run_app(app, host='0.0.0.0', port=port)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        asyncio.run(close_db_pool())

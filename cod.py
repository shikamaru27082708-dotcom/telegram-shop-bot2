import logging
import os
import asyncio
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

import asyncpg
from asyncpg import Pool

from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

# Загрузка переменных окружения
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

def get_env_var(var_name: str, var_type=str, required=True):
    value = os.getenv(var_name)
    if required and value is None:
        raise ValueError(f"❌ {var_name} не найден в переменных окружения")
    if value is not None and var_type == int:
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"❌ {var_name} должен быть числом! Получено: {value}")
    return value

# Конфигурация
BOT_TOKEN = get_env_var('BOT_TOKEN')
ADMIN_ID = get_env_var('ADMIN_ID', var_type=int)
ORDERS_CHAT_ID = get_env_var('ORDERS_CHAT_ID', var_type=int)
ITEMS_PER_PAGE = 5

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# === ПОДКЛЮЧЕНИЕ К POSTGRESQL ===
db_pool: Pool = None

async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(
        dsn=os.getenv('DATABASE_URL'),
        min_size=1,
        max_size=5,
        command_timeout=60
    )
    print("✅ Пул подключений к PostgreSQL создан")

async def close_db_pool():
    if db_pool:
        await db_pool.close()
        print("✅ Пул подключений закрыт")

async def create_tables():
    async with db_pool.acquire() as conn:
        # Таблица категорий
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                emoji TEXT,
                display_name TEXT
            )
        ''')
        # Таблица товаров
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                price REAL NOT NULL,
                image_id TEXT,
                category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_available INTEGER DEFAULT 1
            )
        ''')
        # Индексы
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_products_available ON products(is_available)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_products_created ON products(created_at)")

        # Таблица заказов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                username TEXT,
                total_amount REAL,
                status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")

        # Таблица элементов заказа
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS order_items (
                id SERIAL PRIMARY KEY,
                order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
                product_id INTEGER REFERENCES products(id),
                product_name TEXT,
                quantity INTEGER,
                price REAL
            )
        ''')
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id)")

        # Таблица корзины
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS cart (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
                quantity INTEGER DEFAULT 1,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, product_id)
            )
        ''')
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_cart_user ON cart(user_id)")

        # Добавляем категории по умолчанию
        categories = [
            ('pods', '💨', 'Под-Системы'),
            ('liquid', '🧪', 'Жидкость'),
            ('snus', '🟤', 'Снюс/Пластинки'),
            ('disposable', '⚡', 'Одноразовые устройства'),
            ('vaporizers', '🌫', 'Испарители')
        ]
        for cat_id, emoji, display_name in categories:
            await conn.execute('''
                INSERT INTO categories (name, emoji, display_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (name) DO NOTHING
            ''', cat_id, emoji, display_name)
        print("✅ Таблицы созданы/проверены")

# === АСИНХРОННЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ С БД ===

async def get_all_categories() -> List[Tuple]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, emoji, display_name FROM categories ORDER BY id")
        return [tuple(row) for row in rows]

async def get_category_info(category_id: int) -> Tuple[str, str]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT emoji, display_name FROM categories WHERE id = $1", category_id)
        return (row['emoji'], row['display_name']) if row else ("📁", "Категория")

async def get_products_by_category(category_id: int, page: int = 1) -> Tuple[List[Tuple], int]:
    offset = (page - 1) * ITEMS_PER_PAGE
    async with db_pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM products WHERE category_id = $1 AND is_available = 1",
            category_id
        )
        rows = await conn.fetch('''
            SELECT id, name, description, price, image_id, category_id, created_at
            FROM products
            WHERE category_id = $1 AND is_available = 1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
        ''', category_id, ITEMS_PER_PAGE, offset)
        return [tuple(row) for row in rows], total

async def get_product(product_id: int) -> Optional[Tuple]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT id, name, description, price, image_id, category_id, created_at
            FROM products
            WHERE id = $1 AND is_available = 1
        ''', product_id)
        return tuple(row) if row else None

async def add_product(name: str, description: str, price: float, image_id: str, category_id: int) -> int:
    async with db_pool.acquire() as conn:
        product_id = await conn.fetchval('''
            INSERT INTO products (name, description, price, image_id, category_id)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        ''', name, description, price, image_id, category_id)
    return product_id

async def update_product_price(product_id: int, new_price: float) -> bool:
    async with db_pool.acquire() as conn:
        result = await conn.execute('''
            UPDATE products SET price = $1
            WHERE id = $2 AND is_available = 1
        ''', new_price, product_id)
        affected = result.split()[-1]
    return affected != '0'

async def delete_product(product_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            UPDATE products SET is_available = 0
            WHERE id = $1
        ''', product_id)

async def add_to_cart(user_id: int, product_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO cart (user_id, product_id, quantity)
            VALUES ($1, $2, 1)
            ON CONFLICT (user_id, product_id) DO UPDATE
            SET quantity = cart.quantity + 1
        ''', user_id, product_id)

async def get_cart(user_id: int) -> List[Tuple]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT c.id, c.quantity, p.id, p.name, p.price, p.image_id
            FROM cart c
            JOIN products p ON c.product_id = p.id
            WHERE c.user_id = $1
            ORDER BY c.added_at DESC
        ''', user_id)
        return [tuple(row) for row in rows]

async def remove_from_cart(cart_item_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM cart WHERE id = $1', cart_item_id)

async def clear_cart(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute('DELETE FROM cart WHERE user_id = $1', user_id)

async def create_order(user_id: int, user_name: str, username: str, cart_items: List[Tuple]) -> int:
    async with db_pool.acquire() as conn:
        total = sum(item[1] * item[4] for item in cart_items)
        order_id = await conn.fetchval('''
            INSERT INTO orders (user_id, user_name, username, total_amount)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        ''', user_id, user_name, username, total)
        for item in cart_items:
            await conn.execute('''
                INSERT INTO order_items (order_id, product_id, product_name, quantity, price)
                VALUES ($1, $2, $3, $4, $5)
            ''', order_id, item[2], item[3], item[1], item[4])
    return order_id

async def get_order_details(order_id: int) -> Tuple[Optional[Tuple], List[Tuple]]:
    async with db_pool.acquire() as conn:
        order_row = await conn.fetchrow('SELECT * FROM orders WHERE id = $1', order_id)
        items_rows = await conn.fetch('SELECT * FROM order_items WHERE order_id = $1', order_id)
        return (tuple(order_row) if order_row else None,
                [tuple(row) for row in items_rows])

async def get_user_orders(user_id: int, limit: int = 5) -> List[Tuple]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT * FROM orders
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        ''', user_id, limit)
        return [tuple(row) for row in rows]

async def get_all_orders(limit: int = 20) -> List[Tuple]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT * FROM orders
            ORDER BY created_at DESC
            LIMIT $1
        ''', limit)
        return [tuple(row) for row in rows]

async def get_statistics() -> dict:
    async with db_pool.acquire() as conn:
        stats = {}
        stats['products'] = await conn.fetchval("SELECT COUNT(*) FROM products WHERE is_available = 1")
        rows = await conn.fetch("SELECT status, COUNT(*) FROM orders GROUP BY status")
        stats['orders_by_status'] = {row['status']: row['count'] for row in rows}
        stats['revenue'] = await conn.fetchval("SELECT COALESCE(SUM(total_amount), 0) FROM orders")
        stats['avg_order'] = await conn.fetchval("SELECT COALESCE(AVG(total_amount), 0) FROM orders")
        rows = await conn.fetch('''
            SELECT product_name, SUM(quantity) as total
            FROM order_items
            GROUP BY product_name
            ORDER BY total DESC
            LIMIT 5
        ''')
        stats['popular_products'] = [(row['product_name'], row['total']) for row in rows]
        return stats

async def add_initial_products_async():
    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM products WHERE is_available = 1")
        if count > 0:
            print("ℹ️ Товары уже существуют, пропускаем инициализацию")
            return
        rows = await conn.fetch("SELECT id, name FROM categories")
        categories = {row['name']: row['id'] for row in rows}
        # Все товары (скопировано из старой функции)
        pods_products = [
            ("Voopoo Drag X", "Мощный под-мод с регулировкой воздуха, 80W", 3490, categories.get('pods')),
            ("Smok Nord 5", "Компактная под-система с двумя испарителями", 2790, categories.get('pods')),
            ("Uwell Caliburn G2", "Популярная под-система с отличным вкусом", 2190, categories.get('pods')),
            ("Vaporesso XROS 3", "Надежная под-система с регулировкой тяги", 2390, categories.get('pods')),
            ("GeekVape Wenax K1", "Простая под-система для начинающих", 1890, categories.get('pods'))
        ]
        liquid_products = [
            ("Bloody Mary Свежая Клубника", "Фруктовая свежесть, 30ml", 590, categories.get('liquid')),
            ("Dinner Lady Lemon Tart", "Классический лимонный тарт, 50ml", 790, categories.get('liquid')),
            ("Nasty Juice ASAP Grape", "Виноград со льдом, 60ml", 890, categories.get('liquid')),
            ("HUSTLE COLA", "Вкус классической колы, 30ml", 550, categories.get('liquid')),
            ("Pynapple Ice", "Ананас со льдом, 100ml", 990, categories.get('liquid'))
        ]
        snus_products = [
            ("Siberia White Dry", "Крепкий снюс, 20 порций", 450, categories.get('snus')),
            ("Odens Cold White", "Мятный холодок, 24 порции", 420, categories.get('snus')),
            ("Lyft Freeze", "Никотиновые пластинки со льдом", 380, categories.get('snus')),
            ("Velo Ice Cool", "Мятные пластинки, 20 порций", 350, categories.get('snus')),
            ("Pablo Exclusive", "Крепкие никотиновые пластинки", 480, categories.get('snus'))
        ]
        disposable_products = [
            ("Elf Bar 1500", "1500 затяжек, фруктовое ассорти", 890, categories.get('disposable')),
            ("HQD Cuvie Plus", "1200 затяжек, компактный", 750, categories.get('disposable')),
            ("Puff Mi", "800 затяжек, разнообразие вкусов", 650, categories.get('disposable')),
            ("Vozol Gear 10000", "10000 затяжек, с дисплеем", 1990, categories.get('disposable')),
            ("Airscream", "1000 затяжек, стильный дизайн", 790, categories.get('disposable'))
        ]
        vape_products = [
            ("Voopoo PnP Coil", "Сменные испарители 0.3/0.6/0.8 Ohm", 350, categories.get('vaporizers')),
            ("Smok RPM Coil", "Для Nord и RPM устройств", 320, categories.get('vaporizers')),
            ("Uwell Caliburn G Coil", "Для Caliburn G и G2", 380, categories.get('vaporizers')),
            ("Vaporesso GTX Coil", "Для XROS и GTX устройств", 340, categories.get('vaporizers')),
            ("GeekVape B Series", "Для Wenax устройств", 300, categories.get('vaporizers'))
        ]
        all_products = (pods_products + liquid_products + snus_products +
                        disposable_products + vape_products)
        for name, desc, price, cat_id in all_products:
            if cat_id:
                await conn.execute(
                    "INSERT INTO products (name, description, price, category_id) VALUES ($1, $2, $3, $4)",
                    name, desc, price, cat_id
                )
        print("✅ Начальные товары добавлены")

# === СОЗДАНИЕ БОТА И ДИСПЕТЧЕРА ===
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

print("✅ Конфигурация загружена успешно!")
print(f"ADMIN_ID: {ADMIN_ID}")
print(f"ORDERS_CHAT_ID: {ORDERS_CHAT_ID}")

# === СОСТОЯНИЯ ===
class AdminStates(StatesGroup):
    adding_product_name = State()
    adding_product_description = State()
    adding_product_price = State()
    adding_product_image = State()
    adding_product_category = State()
    editing_product_price = State()

# === КЛАВИАТУРЫ (некоторые стали асинхронными) ===
async def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Каталог")],
            [KeyboardButton(text="🛒 Корзина")],
            [KeyboardButton(text="📦 Мои заказы")],
            [KeyboardButton(text="ℹ️ О нас")]
        ],
        resize_keyboard=True
    )

async def get_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Товары")],
            [KeyboardButton(text="➕ Добавить товар")],
            [KeyboardButton(text="📋 Заказы")],
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🔙 Выход")]
        ],
        resize_keyboard=True
    )

async def get_categories_inline_keyboard():
    categories = await get_all_categories()
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.button(
            text=f"{category[2]} {category[3]}",
            callback_data=f"cat_{category[0]}_1"
        )
    builder.adjust(1)
    return builder.as_markup()

def get_products_inline_keyboard(products: List[Tuple], category_id: int, page: int, total_pages: int):
    builder = InlineKeyboardBuilder()
    for product in products:
        builder.button(
            text=f"{product[1][:30]} - {product[3]}₽",
            callback_data=f"prod_{product[0]}"
        )
    builder.adjust(1)

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"cat_{category_id}_{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"• {page}/{total_pages} •", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"cat_{category_id}_{page + 1}"))
    if nav_row:
        builder.row(*nav_row)
    builder.row(
        InlineKeyboardButton(text="🔙 К категориям", callback_data="back_to_cats"),
        InlineKeyboardButton(text="🛒 Корзина", callback_data="show_cart")
    )
    return builder.as_markup()

def get_product_detail_keyboard(product_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🛒 Добавить в корзину", callback_data=f"add_{product_id}"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_cats")
    )
    return builder.as_markup()

def get_cart_inline_keyboard(cart_items: List[Tuple]):
    builder = InlineKeyboardBuilder()
    for item in cart_items:
        builder.row(
            InlineKeyboardButton(
                text=f"❌ {item[3][:20]} x{item[1]} = {item[1] * item[4]}₽",
                callback_data=f"rem_{item[0]}"
            )
        )
    if cart_items:
        builder.row(
            InlineKeyboardButton(text="✅ Оформить заказ", callback_data="create_order")
        )
    builder.row(
        InlineKeyboardButton(text="🔄 Очистить", callback_data="clear_cart"),
        InlineKeyboardButton(text="🔙 В каталог", callback_data="back_to_cats")
    )
    return builder.as_markup()

def format_cart_text(cart_items: List[Tuple]) -> str:
    text = "🛒 Ваша корзина:\n\n"
    total = 0
    for item in cart_items:
        text += f"• {item[3][:30]} x{item[1]} = {item[1] * item[4]}₽\n"
        total += item[1] * item[4]
    text += f"\n💰 Итого: {total}₽"
    return text

# === ОБРАБОТЧИКИ ===

@dp.message(Command("start"))
async def start_command(message: types.Message):
    print(f"🔥 ПОЛУЧЕН START от user {message.from_user.id}")
    try:
        if message.from_user.id == ADMIN_ID:
            await message.answer(
                "👋 Добро пожаловать в админ-панель!",
                reply_markup=await get_admin_keyboard()
            )
            print(f"✅ Отправлено админ-меню пользователю {message.from_user.id}")
        else:
            await message.answer(
                "👋 Добро пожаловать в магазин!",
                reply_markup=await get_main_keyboard()
            )
            print(f"✅ Отправлено меню магазина пользователю {message.from_user.id}")
    except Exception as e:
        print(f"❌ Ошибка при отправке: {e}")

@dp.message(F.text == "🛍 Каталог")
async def show_catalog(message: types.Message):
    await message.answer(
        "📁 Выберите категорию:",
        reply_markup=await get_categories_inline_keyboard()
    )

@dp.callback_query(F.data.startswith("cat_"))
async def process_category(callback: types.CallbackQuery):
    try:
        _, category_id, page = callback.data.split('_')
        category_id = int(category_id)
        page = int(page)

        products, total = await get_products_by_category(category_id, page)
        total_pages = (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

        if not products:
            await callback.answer("В этой категории пока нет товаров")
            return

        emoji, name = await get_category_info(category_id)
        new_text = f"{emoji} {name} (стр. {page}/{total_pages}):"
        new_keyboard = get_products_inline_keyboard(products, category_id, page, total_pages)

        if callback.message.text != new_text or callback.message.reply_markup != new_keyboard:
            await callback.message.edit_text(new_text, reply_markup=new_keyboard)
        else:
            await callback.answer()
    except Exception as e:
        print(f"Ошибка в process_category: {e}")
        await callback.answer("Произошла ошибка")
    await callback.answer()

@dp.callback_query(F.data.startswith("prod_"))
async def process_product(callback: types.CallbackQuery):
    try:
        product_id = int(callback.data.split('_')[1])
        product = await get_product(product_id)
        if not product:
            await callback.answer("Товар не найден")
            return
        text = f"{product[1]}\n\n{product[2]}\n\n💰 Цена: {product[3]}₽"
        if product[4]:
            await bot.send_photo(
                callback.from_user.id,
                product[4],
                caption=text,
                reply_markup=get_product_detail_keyboard(product_id)
            )
        else:
            await bot.send_message(
                callback.from_user.id,
                text,
                reply_markup=get_product_detail_keyboard(product_id)
            )
        await callback.message.delete()
    except Exception as e:
        print(f"Ошибка в process_product: {e}")
        await callback.answer("Произошла ошибка")
    await callback.answer()

@dp.message(F.text == "🛒 Корзина")
@dp.callback_query(F.data == "show_cart")
async def show_cart(event: types.Message | types.CallbackQuery):
    user_id = event.from_user.id
    cart_items = await get_cart(user_id)
    text = "🛒 Ваша корзина пуста" if not cart_items else format_cart_text(cart_items)
    if isinstance(event, types.CallbackQuery):
        if cart_items:
            new_keyboard = get_cart_inline_keyboard(cart_items)
            if event.message.text != text or event.message.reply_markup != new_keyboard:
                await event.message.edit_text(text, reply_markup=new_keyboard)
            else:
                await event.answer()
        else:
            if event.message.text != text:
                await event.message.edit_text(text)
            else:
                await event.answer()
        await event.answer()
    else:
        if cart_items:
            await event.answer(text, reply_markup=get_cart_inline_keyboard(cart_items))
        else:
            await event.answer(text)

@dp.callback_query(F.data.startswith("add_"))
async def add_to_cart_callback(callback: types.CallbackQuery):
    try:
        product_id = int(callback.data.split('_')[1])
        await add_to_cart(callback.from_user.id, product_id)
        await callback.answer("✅ Товар добавлен в корзину!")
    except Exception as e:
        print(f"Ошибка в add_to_cart_callback: {e}")
        await callback.answer("❌ Ошибка")

@dp.callback_query(F.data == "clear_cart")
async def clear_cart_handler(callback: types.CallbackQuery):
    try:
        await clear_cart(callback.from_user.id)
        new_text = "🛒 Ваша корзина пуста"
        if callback.message.text:
            if callback.message.text != new_text:
                await callback.message.edit_text(new_text)
            else:
                await callback.answer("🗑 Корзина уже пуста")
        elif callback.message.caption:
            if callback.message.caption != new_text:
                await callback.message.edit_caption(caption=new_text)
                if callback.message.reply_markup:
                    await callback.message.edit_reply_markup(reply_markup=None)
            else:
                await callback.answer("🗑 Корзина уже пуста")
        else:
            await callback.message.delete()
            await callback.message.answer(new_text)
        await callback.answer("🗑 Корзина очищена")
    except Exception as e:
        print(f"Ошибка в clear_cart_handler: {e}")
        await callback.answer("❌ Произошла ошибка")

@dp.callback_query(F.data.startswith("rem_"))
async def remove_from_cart_callback(callback: types.CallbackQuery):
    try:
        cart_item_id = int(callback.data.split('_')[1])
        await remove_from_cart(cart_item_id)
        cart_items = await get_cart(callback.from_user.id)
        if cart_items:
            new_text = format_cart_text(cart_items)
            new_keyboard = get_cart_inline_keyboard(cart_items)
            if callback.message.text:
                if callback.message.text != new_text or callback.message.reply_markup != new_keyboard:
                    await callback.message.edit_text(new_text, reply_markup=new_keyboard)
                else:
                    await callback.answer("🗑 Товар удален")
            elif callback.message.caption:
                if callback.message.caption != new_text or callback.message.reply_markup != new_keyboard:
                    await callback.message.edit_caption(caption=new_text, reply_markup=new_keyboard)
                else:
                    await callback.answer("🗑 Товар удален")
            else:
                await callback.message.delete()
                await callback.message.answer(new_text, reply_markup=new_keyboard)
        else:
            new_text = "🛒 Ваша корзина пуста"
            if callback.message.text:
                if callback.message.text != new_text:
                    await callback.message.edit_text(new_text)
                else:
                    await callback.answer("🗑 Товар удален")
            elif callback.message.caption:
                if callback.message.caption != new_text:
                    await callback.message.edit_caption(caption=new_text)
                    if callback.message.reply_markup:
                        await callback.message.edit_reply_markup(reply_markup=None)
                else:
                    await callback.answer("🗑 Товар удален")
            else:
                await callback.message.delete()
                await callback.message.answer(new_text)
        await callback.answer("🗑 Товар удален")
    except Exception as e:
        print(f"Ошибка в remove_from_cart_callback: {e}")
        await callback.answer("❌ Ошибка")

@dp.callback_query(F.data == "create_order")
async def create_order_handler(callback: types.CallbackQuery):
    try:
        user_id = callback.from_user.id
        cart_items = await get_cart(user_id)
        if not cart_items:
            await callback.answer("🛒 Корзина пуста")
            return
        user = callback.from_user
        username = f"@{user.username}" if user.username else f"ID: {user.id}"
        order_id = await create_order(user_id, user.full_name, username, cart_items)
        await clear_cart(user_id)

        text = f"🆕 НОВЫЙ ЗАКАЗ #{order_id}\n\n👤 {user.full_name}\n🔗 {username}\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n📦 Состав:\n"
        total = 0
        for item in cart_items:
            text += f"• {item[3][:30]} x{item[1]} = {item[1] * item[4]}₽\n"
            total += item[1] * item[4]
        text += f"\n💰 ИТОГО: {total}₽"

        try:
            await bot.send_message(ORDERS_CHAT_ID, text)
        except:
            await bot.send_message(ADMIN_ID, text)

        new_text = f"✅ ЗАКАЗ #{order_id} ОФОРМЛЕН!\n\nСтатус можно отслеживать в разделе «Мои заказы»."
        if callback.message.text != new_text:
            await callback.message.edit_text(new_text)
        else:
            await callback.answer("✅ Заказ оформлен!")
        await callback.answer("✅ Заказ оформлен!")
    except Exception as e:
        print(f"Ошибка в create_order_handler: {e}")
        await callback.answer("❌ Ошибка при оформлении")

@dp.message(F.text == "📦 Мои заказы")
async def show_my_orders(message: types.Message):
    orders = await get_user_orders(message.from_user.id)
    if not orders:
        await message.answer("📭 У вас пока нет заказов")
        return
    status_map = {'new': '🆕 Новый', 'processing': '⏳ В обработке', 'completed': '✅ Выполнен', 'cancelled': '❌ Отменен'}
    text = "📦 Ваши заказы:\n\n"
    for order in orders:
        text += f"#{order[0]} {status_map.get(order[5], order[5])}\n"
        text += f"💰 {order[4]}₽ • {order[6][:16]}\n\n"
    await message.answer(text.strip())

@dp.message(F.text == "ℹ️ О нас")
async def about_us(message: types.Message):
    text = (
        "🛍 О магазине\n\n"
        "Поддержка-@omen_mngr\n\n"
        "Канал-https://t.me/+qKrkFbenS8MyNWNi\n\n"
        "✅ Только оригинальная продукция\n"
        "💳 Оплата при получении\n"
        "🚀 Быстрая доставка\n\n"
        "📱 Ассортимент:\n"
        "💨 Под-Системы\n🧪 Жидкости\n🟤 Снюс\n⚡ Одноразки\n🌫 Испарители"
    )
    await message.answer(text)

@dp.message(F.text == "🔙 Выход")
async def exit_admin(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Выход", reply_markup=await get_main_keyboard())

@dp.message(F.text == "📦 Товары")
async def admin_products(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    categories = await get_all_categories()
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.button(
            text=f"{category[2]} {category[3]}",
            callback_data=f"adminview_{category[0]}_1"
        )
    builder.adjust(1)
    await message.answer(
        "📁 Выберите категорию для просмотра товаров:",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("editprice_"))
async def edit_price_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет прав")
        return
    try:
        product_id = int(callback.data.split('_')[1])
        product = await get_product(product_id)
        if not product:
            await callback.answer("❌ Товар не найден")
            return
        await state.update_data(product_id=product_id)
        await state.set_state(AdminStates.editing_product_price)
        await callback.message.answer(
            f"✏️ Изменение цены для товара:\n📦 {product[1]}\n💰 Текущая цена: {product[3]}₽\n\nВведите новую цену (только число):"
        )
        await callback.answer()
    except Exception as e:
        print(f"Ошибка в edit_price_start: {e}")
        await callback.answer("❌ Произошла ошибка")

@dp.message(AdminStates.editing_product_price)
async def edit_price_process(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    try:
        new_price = float(message.text.replace(',', '.'))
        if new_price <= 0:
            await message.answer("❌ Цена должна быть положительным числом. Попробуйте снова:")
            return
        if new_price > 1000000:
            await message.answer("❌ Цена слишком высокая (максимум 1 000 000). Введите меньшую цену:")
            return
        data = await state.get_data()
        product_id = data.get('product_id')
        if not product_id:
            await message.answer("❌ Ошибка: ID товара не найден")
            await state.clear()
            return
        product = await get_product(product_id)
        if not product:
            await message.answer("❌ Товар не найден")
            await state.clear()
            return
        success = await update_product_price(product_id, new_price)
        if success:
            await message.answer(
                f"✅ Цена успешно изменена!\n\n📦 Товар: {product[1]}\n💰 Старая цена: {product[3]}₽\n💰 Новая цена: {new_price}₽"
            )
        else:
            await message.answer("❌ Не удалось обновить цену")
        await state.clear()
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число (например: 1000 или 499.90)")
    except Exception as e:
        print(f"Ошибка в edit_price_process: {e}")
        await message.answer("❌ Произошла ошибка")
        await state.clear()

@dp.callback_query(F.data.startswith("adminview_"))
async def admin_category_products(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет прав")
        return
    try:
        parts = callback.data.split('_')
        if len(parts) != 3:
            await callback.answer("Ошибка формата данных")
            return
        category_id = int(parts[1])
        page = int(parts[2])
        products, total = await get_products_by_category(category_id, page)
        total_pages = (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        emoji, category_name = await get_category_info(category_id)

        if not products:
            new_text = f"{emoji} {category_name}\n\n📭 В этой категории нет товаров"
            if callback.message.text != new_text:
                await callback.message.edit_text(new_text)
            await callback.answer()
            return

        new_text = f"{emoji} {category_name} (стр. {page}/{total_pages}):"
        if callback.message.text != new_text:
            await callback.message.edit_text(new_text)

        for product in products:
            product_text = (
                f"📦 ID: {product[0]}\nНазвание: {product[1]}\n"
                f"Описание: {product[2][:50]}..." if len(product[2]) > 50 else f"Описание: {product[2]}\n"
                f"💰 Цена: {product[3]}₽"
            )
            edit_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✏️ Изменить цену", callback_data=f"editprice_{product[0]}"),
                    InlineKeyboardButton(text="❌ Удалить", callback_data=f"delprod_{product[0]}")
                ]
            ])
            if product[4]:
                await bot.send_photo(
                    callback.from_user.id,
                    product[4],
                    caption=product_text,
                    reply_markup=edit_keyboard
                )
            else:
                await callback.message.answer(
                    product_text,
                    reply_markup=edit_keyboard
                )

        if total_pages > 1:
            nav_builder = InlineKeyboardBuilder()
            if page > 1:
                nav_builder.button(text="◀️", callback_data=f"adminview_{category_id}_{page - 1}")
            nav_builder.button(text=f"• {page}/{total_pages} •", callback_data="noop")
            if page < total_pages:
                nav_builder.button(text="▶️", callback_data=f"adminview_{category_id}_{page + 1}")
            nav_builder.adjust(3)
            await callback.message.answer("Перейти на страницу:", reply_markup=nav_builder.as_markup())

        back_builder = InlineKeyboardBuilder()
        back_builder.button(text="🔙 Назад к категориям", callback_data="back_admin_cats")
        await callback.message.answer("Управление:", reply_markup=back_builder.as_markup())
    except Exception as e:
        print(f"Ошибка в admin_category_products: {e}")
        await callback.answer("Произошла ошибка")
    await callback.answer()

@dp.callback_query(F.data == "back_admin_cats")
async def back_to_admin_categories(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет прав")
        return
    await callback.message.delete()
    await admin_products(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("delprod_"))
async def delete_product_handler(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет прав")
        return
    try:
        product_id = int(callback.data.split('_')[1])
        product = await get_product(product_id)
        if product:
            await delete_product(product_id)
            await callback.message.edit_text(
                f"❌ Товар удален\n\nID: {product_id}\nНазвание: {product[1]}"
            )
            await callback.answer("✅ Товар удален")
        else:
            await callback.answer("❌ Товар не найден")
    except Exception as e:
        print(f"Ошибка в delete_product_handler: {e}")
        await callback.answer("❌ Произошла ошибка")

@dp.message(F.text == "➕ Добавить товар")
async def add_product_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    categories = await get_all_categories()
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.button(
            text=f"{category[2]} {category[3]}",
            callback_data=f"addcat_{category[0]}"
        )
    builder.adjust(1)
    await message.answer(
        "📁 Выберите категорию для нового товара:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(AdminStates.adding_product_category)

@dp.callback_query(AdminStates.adding_product_category)
async def add_product_category(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет прав")
        return
    try:
        parts = callback.data.split('_')
        if len(parts) != 2 or parts[0] != 'addcat':
            await callback.answer("Ошибка формата данных")
            return
        category_id = int(parts[1])
        await state.update_data(category_id=category_id)

        # Получаем информацию о категории через БД
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT emoji, display_name FROM categories WHERE id = $1", category_id)
            emoji = row['emoji'] if row else "📁"
            name = row['display_name'] if row else "Категория"

        await callback.message.edit_text(
            f"✅ Выбрана категория: {emoji} {name}\n\nТеперь введите название товара:"
        )
        await state.set_state(AdminStates.adding_product_name)
    except Exception as e:
        print(f"Ошибка в add_product_category: {e}")
        await callback.message.edit_text("❌ Ошибка при выборе категории")
        await state.clear()
    await callback.answer()

@dp.message(AdminStates.adding_product_name)
async def add_product_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Введите описание:")
    await state.set_state(AdminStates.adding_product_description)

@dp.message(AdminStates.adding_product_description)
async def add_product_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await message.answer("Введите цену:")
    await state.set_state(AdminStates.adding_product_price)

@dp.message(AdminStates.adding_product_price)
async def add_product_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text)
        await state.update_data(price=price)
        await message.answer("Отправьте фото (или 'пропустить'):")
        await state.set_state(AdminStates.adding_product_image)
    except:
        await message.answer("❌ Введите число")

@dp.message(AdminStates.adding_product_image)
async def add_product_image(message: types.Message, state: FSMContext):
    data = await state.get_data()
    image_id = message.photo[-1].file_id if message.photo else None
    product_id = await add_product(
        data['name'], data['description'], data['price'],
        image_id, data['category_id']
    )
    await message.answer(f"✅ Товар добавлен! ID: {product_id}")
    await state.clear()

@dp.message(F.text == "📋 Заказы")
async def admin_orders(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    orders = await get_all_orders(20)
    if not orders:
        await message.answer("📭 Нет заказов")
        return
    status_map = {'new': '🆕', 'processing': '⏳', 'completed': '✅', 'cancelled': '❌'}
    text = "📋 Последние заказы:\n\n"
    for order in orders:
        text += f"{status_map.get(order[5], '•')} #{order[0]} {order[2][:15]}\n"
        text += f"💰 {order[4]}₽ • {order[6][:16]}\n\n"
    await message.answer(text.strip())

@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    stats = await get_statistics()
    text = f"📊 СТАТИСТИКА\n\n📦 Товаров: {stats['products']}\n"
    text += f"📋 Заказов: {sum(stats['orders_by_status'].values())}\n"
    text += f"💰 Выручка: {stats['revenue']:.0f}₽\n"
    text += f"📈 Средний чек: {stats['avg_order']:.0f}₽\n\n"
    if stats['popular_products']:
        text += "🔥 Популярные товары:\n"
        for name, qty in stats['popular_products'][:3]:
            text += f"• {name[:20]}: {qty} шт.\n"
    await message.answer(text)

@dp.callback_query(F.data == "back_to_cats")
async def back_to_categories(callback: types.CallbackQuery):
    await callback.message.delete()
    await show_catalog(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "noop")
async def noop(callback: types.CallbackQuery):
    await callback.answer()


# === ЗАПУСК БОТА ЧЕРЕЗ ВЕБХУКИ ===
async def on_startup(app: web.Application):
    print("🚀 Инициализация базы данных...")
    await init_db_pool()
    await create_tables()
    await add_initial_products_async()
    print("✅ База данных инициализирована")

    print("🚀 Устанавливаем вебхук...")
    await bot.delete_webhook()
    webhook_url = f"https://telegram-shop-bot2.onrender.com/webhook"
    await bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True
    )
    print(f"✅ Вебхук установлен на {webhook_url}")


async def on_shutdown(app: web.Application):
    print("🔄 Завершение работы...")
    await close_db_pool()
    await bot.session.close()
    print("✅ Ресурсы освобождены")


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
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000))
    print(f"🚀 Запуск aiohttp сервера на порту {port}...")
    web.run_app(app, host='0.0.0.0', port=port)

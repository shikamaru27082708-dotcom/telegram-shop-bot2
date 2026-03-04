import logging
from pathlib import Path
from dotenv import load_dotenv
import os
import asyncio
import threading
import sqlite3
from datetime import datetime
from typing import List, Tuple, Optional
import functools
from cachetools import TTLCache
from flask import Flask, jsonify
import multiprocessing
import time

# Явно указываем путь к файлу .env
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)


# Функция для безопасного получения переменных
def get_env_var(var_name: str, var_type=str, required=True):
    """Безопасное получение переменных окружения"""
    value = os.getenv(var_name)

    if required and value is None:
        current_dir = Path.cwd()
        env_exists = Path('.env').exists()
        raise ValueError(
            f"❌ {var_name} не найден!\n"
            f"Текущая папка: {current_dir}\n"
            f"Файл .env существует: {env_exists}\n"
            f"Содержимое .env: {'доступно' if env_exists else 'недоступно'}\n"
            f"Создайте файл .env с переменной {var_name}"
        )

    if value is not None and var_type == int:
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"❌ {var_name} должен быть числом! Получено: {value}")

    return value


# Импорты aiogram (должны быть после get_env_var, но до создания бота)
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Конфигурация
BOT_TOKEN = get_env_var('BOT_TOKEN')
ADMIN_ID = get_env_var('ADMIN_ID', var_type=int)
ORDERS_CHAT_ID = get_env_var('ORDERS_CHAT_ID', var_type=int)

# Настройки пагинации
ITEMS_PER_PAGE = 5
CACHE_TTL = 300

# Инициализация кэша
cache = TTLCache(maxsize=100, ttl=CACHE_TTL)

# === СОЗДАЕМ FLASK ПРИЛОЖЕНИЕ ===
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return jsonify({
        "status": "running",
        "bot": "Telegram Shop Bot",
        "message": "Бот работает"
    })

@flask_app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200
# ================================

# === СОЗДАЕМ БОТА И ДИСПЕТЧЕРА ===
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

print("✅ Конфигурация загружена успешно!")
print(f"ADMIN_ID: {ADMIN_ID}")
print(f"ORDERS_CHAT_ID: {ORDERS_CHAT_ID}")
# =================================


# Состояния
class AdminStates(StatesGroup):
    adding_product_name = State()
    adding_product_description = State()
    adding_product_price = State()
    adding_product_image = State()
    adding_product_category = State()
    # Новое состояние для изменения цены
    editing_product_price = State()


# Кэширующий декоратор
def cached(key_prefix: str):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            cache_key = f"{key_prefix}:{str(args)}:{str(kwargs)}"
            if cache_key in cache:
                return cache[cache_key]
            result = func(*args, **kwargs)
            cache[cache_key] = result
            return result
        return wrapper
    return decorator


# Функция для инвалидации кэша
def invalidate_cache(pattern: str = None):
    if pattern:
        keys_to_delete = [k for k in cache.keys() if pattern in str(k)]
        for key in keys_to_delete:
            cache.pop(key, None)
    else:
        cache.clear()


# Инициализация БД с индексами
def init_db():
    with sqlite3.connect('shop.db', timeout=20) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")

        # Таблица категорий
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                emoji TEXT,
                display_name TEXT
            )
        ''')

        # Таблица товаров
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                price REAL NOT NULL,
                image_id TEXT,
                category_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_available INTEGER DEFAULT 1,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            )
        ''')

        # Индексы
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_available ON products(is_available)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_products_created ON products(created_at)")

        # Таблица заказов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                username TEXT,
                total_amount REAL,
                status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")

        # Таблица элементов заказа
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                product_id INTEGER,
                product_name TEXT,
                quantity INTEGER,
                price REAL,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES products(id)
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id)")

        # Таблица корзины
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cart (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER DEFAULT 1,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
                UNIQUE(user_id, product_id)
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cart_user ON cart(user_id)")

        # Добавляем категории
        categories = [
            ('pods', '💨', 'Под-Системы'),
            ('liquid', '🧪', 'Жидкость'),
            ('snus', '🟤', 'Снюс/Пластинки'),
            ('disposable', '⚡', 'Одноразовые устройства'),
            ('vaporizers', '🌫', 'Испарители')
        ]

        cursor.executemany(
            "INSERT OR IGNORE INTO categories (name, emoji, display_name) VALUES (?, ?, ?)",
            categories
        )
        conn.commit()


# Функции для работы с БД
@cached("categories")
def get_all_categories() -> List[Tuple]:
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, emoji, display_name FROM categories ORDER BY id")
        return cursor.fetchall()


def get_products_by_category(category_id: int, page: int = 1) -> Tuple[List[Tuple], int]:
    offset = (page - 1) * ITEMS_PER_PAGE
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM products WHERE category_id = ? AND is_available = 1",
            (category_id,)
        )
        total = cursor.fetchone()[0]

        cursor.execute('''
            SELECT id, name, description, price, image_id, category_id, created_at
            FROM products
            WHERE category_id = ? AND is_available = 1
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        ''', (category_id, ITEMS_PER_PAGE, offset))

        products = cursor.fetchall()
        return products, total


def get_product(product_id: int) -> Optional[Tuple]:
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, description, price, image_id, category_id, created_at
            FROM products
            WHERE id = ? AND is_available = 1
        ''', (product_id,))
        return cursor.fetchone()


def add_product(name: str, description: str, price: float, image_id: str, category_id: int) -> int:
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO products (name, description, price, image_id, category_id) VALUES (?, ?, ?, ?, ?)",
            (name, description, price, image_id, category_id)
        )
        product_id = cursor.lastrowid
        conn.commit()

    invalidate_cache("products")
    invalidate_cache("categories")
    return product_id
def update_product_price(product_id: int, new_price: float) -> bool:
    """Обновляет цену товара"""
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE products SET price = ? WHERE id = ? AND is_available = 1",
            (new_price, product_id)
        )
        conn.commit()
        success = cursor.rowcount > 0
    if success:
        invalidate_cache("products")
    return success

def delete_product(product_id: int):
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE products SET is_available = 0 WHERE id = ?", (product_id,))
        conn.commit()
    invalidate_cache("products")


def add_to_cart(user_id: int, product_id: int):
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO cart (user_id, product_id, quantity) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, product_id) DO UPDATE SET quantity = quantity + 1",
            (user_id, product_id)
        )
        conn.commit()


def get_cart(user_id: int) -> List[Tuple]:
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT c.id, c.quantity, p.id, p.name, p.price, p.image_id
            FROM cart c
            JOIN products p ON c.product_id = p.id
            WHERE c.user_id = ?
            ORDER BY c.added_at DESC
        ''', (user_id,))
        return cursor.fetchall()


def remove_from_cart(cart_item_id: int):
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cart WHERE id = ?", (cart_item_id,))
        conn.commit()


def clear_cart(user_id: int):
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cart WHERE user_id = ?", (user_id,))
        conn.commit()


def create_order(user_id: int, user_name: str, username: str, cart_items: List[Tuple]) -> int:
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        total = sum(item[1] * item[4] for item in cart_items)

        cursor.execute('''
            INSERT INTO orders (user_id, user_name, username, total_amount)
            VALUES (?, ?, ?, ?)
        ''', (user_id, user_name, username, total))

        order_id = cursor.lastrowid

        order_items = [
            (order_id, item[2], item[3], item[1], item[4])
            for item in cart_items
        ]

        cursor.executemany('''
            INSERT INTO order_items (order_id, product_id, product_name, quantity, price)
            VALUES (?, ?, ?, ?, ?)
        ''', order_items)

        conn.commit()
    return order_id


def get_order_details(order_id: int) -> Tuple[Optional[Tuple], List[Tuple]]:
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        order = cursor.fetchone()
        cursor.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,))
        items = cursor.fetchall()
        return order, items


def get_user_orders(user_id: int, limit: int = 5) -> List[Tuple]:
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM orders
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        ''', (user_id, limit))
        return cursor.fetchall()


def get_all_orders(limit: int = 20) -> List[Tuple]:
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM orders
            ORDER BY created_at DESC
            LIMIT ?
        ''', (limit,))
        return cursor.fetchall()


def get_statistics() -> dict:
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()
        stats = {}

        cursor.execute("SELECT COUNT(*) FROM products WHERE is_available = 1")
        stats['products'] = cursor.fetchone()[0]

        cursor.execute('''
            SELECT status, COUNT(*)
            FROM orders
            GROUP BY status
        ''')
        stats['orders_by_status'] = dict(cursor.fetchall())

        cursor.execute("SELECT COALESCE(SUM(total_amount), 0) FROM orders")
        stats['revenue'] = cursor.fetchone()[0]

        cursor.execute("SELECT COALESCE(AVG(total_amount), 0) FROM orders")
        stats['avg_order'] = cursor.fetchone()[0]

        cursor.execute('''
            SELECT product_name, SUM(quantity) as total
            FROM order_items
            GROUP BY product_name
            ORDER BY total DESC
            LIMIT 5
        ''')
        stats['popular_products'] = cursor.fetchall()

        return stats


# Клавиатуры
def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛍 Каталог")],
            [KeyboardButton(text="🛒 Корзина")],
            [KeyboardButton(text="📦 Мои заказы")],
            [KeyboardButton(text="ℹ️ О нас")]
        ],
        resize_keyboard=True
    )


def get_admin_keyboard() -> ReplyKeyboardMarkup:
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


def get_categories_inline_keyboard():
    categories = get_all_categories()
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

    # Навигация
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


# ============================================
# === ВСЕ ОБРАБОТЧИКИ БОТА ===
# ============================================

@dp.message(Command("start"))
async def start_command(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "👋 Добро пожаловать в админ-панель!",
            reply_markup=get_admin_keyboard()
        )
    else:
        await message.answer(
            "👋 Добро пожаловать в магазин!",
            reply_markup=get_main_keyboard()
        )


@dp.message(F.text == "🛍 Каталог")
async def show_catalog(message: types.Message):
    await message.answer(
        "📁 Выберите категорию:",
        reply_markup=get_categories_inline_keyboard()
    )


@dp.callback_query(F.data.startswith("cat_"))
async def process_category(callback: types.CallbackQuery):
    try:
        _, category_id, page = callback.data.split('_')
        category_id = int(category_id)
        page = int(page)

        products, total = get_products_by_category(category_id, page)
        total_pages = (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

        if not products:
            await callback.answer("В этой категории пока нет товаров")
            return

        conn = sqlite3.connect('shop.db')
        cursor = conn.cursor()
        cursor.execute("SELECT emoji, display_name FROM categories WHERE id = ?", (category_id,))
        category_info = cursor.fetchone()
        conn.close()

        emoji = category_info[0] if category_info else "📁"
        name = category_info[1] if category_info else "Категория"

        new_text = f"{emoji} {name} (стр. {page}/{total_pages}):"
        new_keyboard = get_products_inline_keyboard(products, category_id, page, total_pages)

        if callback.message.text != new_text or callback.message.reply_markup != new_keyboard:
            await callback.message.edit_text(new_text, reply_markup=new_keyboard)
        else:
            await callback.answer()

    except Exception as e:
        print(f"Ошибка: {e}")
        await callback.answer("Произошла ошибка")
    await callback.answer()


@dp.callback_query(F.data.startswith("prod_"))
async def process_product(callback: types.CallbackQuery):
    try:
        product_id = int(callback.data.split('_')[1])
        product = get_product(product_id)

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
        print(f"Ошибка: {e}")
        await callback.answer("Произошла ошибка")
    await callback.answer()


@dp.message(F.text == "🛒 Корзина")
@dp.callback_query(F.data == "show_cart")
async def show_cart(event: types.Message | types.CallbackQuery):
    user_id = event.from_user.id
    cart_items = get_cart(user_id)
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
        add_to_cart(callback.from_user.id, product_id)
        await callback.answer("✅ Товар добавлен в корзину!")
    except Exception as e:
        print(f"Ошибка: {e}")
        await callback.answer("❌ Ошибка")


@dp.callback_query(F.data == "clear_cart")
async def clear_cart_handler(callback: types.CallbackQuery):
    try:
        clear_cart(callback.from_user.id)
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
        remove_from_cart(cart_item_id)

        cart_items = get_cart(callback.from_user.id)

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
        cart_items = get_cart(user_id)

        if not cart_items:
            await callback.answer("🛒 Корзина пуста")
            return

        user = callback.from_user
        username = f"@{user.username}" if user.username else f"ID: {user.id}"

        order_id = create_order(user_id, user.full_name, username, cart_items)
        clear_cart(user_id)

        # Уведомление админу
        text = f"🆕 НОВЫЙ ЗАКАЗ #{order_id}\n\n"
        text += f"👤 {user.full_name}\n🔗 {username}\n"
        text += f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n📦 Состав:\n"

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
        print(f"Ошибка: {e}")
        await callback.answer("❌ Ошибка при оформлении")


@dp.message(F.text == "📦 Мои заказы")
async def show_my_orders(message: types.Message):
    orders = get_user_orders(message.from_user.id)

    if not orders:
        await message.answer("📭 У вас пока нет заказов")
        return

    status_map = {
        'new': '🆕 Новый',
        'processing': '⏳ В обработке',
        'completed': '✅ Выполнен',
        'cancelled': '❌ Отменен'
    }

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
        await message.answer("👋 Выход", reply_markup=get_main_keyboard())


@dp.message(F.text == "📦 Товары")
async def admin_products(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    categories = get_all_categories()
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
    """Начало процесса изменения цены"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ У вас нет прав")
        return

    try:
        product_id = int(callback.data.split('_')[1])
        product = get_product(product_id)

        if not product:
            await callback.answer("❌ Товар не найден")
            return

        # Сохраняем ID товара в состояние
        await state.update_data(product_id=product_id)
        await state.set_state(AdminStates.editing_product_price)

        await callback.message.answer(
            f"✏️ Изменение цены для товара:\n"
            f"📦 {product[1]}\n"
            f"💰 Текущая цена: {product[3]}₽\n\n"
            f"Введите новую цену (только число):"
        )
        await callback.answer()
    except Exception as e:
        print(f"Ошибка в edit_price_start: {e}")
        await callback.answer("❌ Произошла ошибка")


@dp.message(AdminStates.editing_product_price)
async def edit_price_process(message: types.Message, state: FSMContext):
    """Обработка ввода новой цены"""
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return

    try:
        # Пробуем преобразовать ввод в число
        new_price = float(message.text.replace(',', '.'))

        if new_price <= 0:
            await message.answer("❌ Цена должна быть положительным числом. Попробуйте снова:")
            return

        if new_price > 1000000:
            await message.answer("❌ Цена слишком высокая (максимум 1 000 000). Введите меньшую цену:")
            return

        # Получаем данные из состояния
        data = await state.get_data()
        product_id = data.get('product_id')

        if not product_id:
            await message.answer("❌ Ошибка: ID товара не найден")
            await state.clear()
            return

        # Получаем информацию о товаре для подтверждения
        product = get_product(product_id)

        if not product:
            await message.answer("❌ Товар не найден")
            await state.clear()
            return

        # Обновляем цену
        success = update_product_price(product_id, new_price)

        if success:
            await message.answer(
                f"✅ Цена успешно изменена!\n\n"
                f"📦 Товар: {product[1]}\n"
                f"💰 Старая цена: {product[3]}₽\n"
                f"💰 Новая цена: {new_price}₽"
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


# Обновите функцию admin_category_products, заменив delete_keyboard на edit_keyboard
# Вот полный обновленный участок функции admin_category_products:

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

        products, total = get_products_by_category(category_id, page)
        total_pages = (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

        conn = sqlite3.connect('shop.db')
        cursor = conn.cursor()
        cursor.execute("SELECT emoji, display_name FROM categories WHERE id = ?", (category_id,))
        category_info = cursor.fetchone()
        conn.close()

        emoji = category_info[0] if category_info else "📁"
        category_name = category_info[1] if category_info else "Категория"

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
                f"📦 ID: {product[0]}\n"
                f"Название: {product[1]}\n"
                f"Описание: {product[2][:50]}..." if len(product[2]) > 50 else f"Описание: {product[2]}\n"
                f"💰 Цена: {product[3]}₽"
            )

            # Обновленная клавиатура с кнопкой изменения цены
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
        print(f"Ошибка: {e}")
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
        product = get_product(product_id)

        if product:
            delete_product(product_id)
            await callback.message.edit_text(
                f"❌ Товар удален\n\nID: {product_id}\nНазвание: {product[1]}"
            )
            await callback.answer("✅ Товар удален")
        else:
            await callback.answer("❌ Товар не найден")
    except Exception as e:
        print(f"Ошибка в delete_product_handler: {e}")
        await callback.answer("❌ Произошла ошибка")


# Добавление товара
@dp.message(F.text == "➕ Добавить товар")
async def add_product_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    categories = get_all_categories()
    from aiogram.utils.keyboard import InlineKeyboardBuilder
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

        conn = sqlite3.connect('shop.db')
        cursor = conn.cursor()
        cursor.execute("SELECT emoji, display_name FROM categories WHERE id = ?", (category_id,))
        category = cursor.fetchone()
        conn.close()

        emoji = category[0] if category else "📁"
        name = category[1] if category else "Категория"

        await callback.message.edit_text(
            f"✅ Выбрана категория: {emoji} {name}\n\n"
            f"Теперь введите название товара:"
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

    product_id = add_product(
        data['name'], data['description'], data['price'],
        image_id, data['category_id']
    )

    await message.answer(f"✅ Товар добавлен! ID: {product_id}")
    await state.clear()


# Заказы в админке
@dp.message(F.text == "📋 Заказы")
async def admin_orders(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    orders = get_all_orders(20)

    if not orders:
        await message.answer("📭 Нет заказов")
        return

    status_map = {'new': '🆕', 'processing': '⏳', 'completed': '✅', 'cancelled': '❌'}
    text = "📋 Последние заказы:\n\n"

    for order in orders:
        text += f"{status_map.get(order[5], '•')} #{order[0]} {order[2][:15]}\n"
        text += f"💰 {order[4]}₽ • {order[6][:16]}\n\n"

    await message.answer(text.strip())


# Статистика
@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    stats = get_statistics()

    text = f"📊 СТАТИСТИКА\n\n"
    text += f"📦 Товаров: {stats['products']}\n"
    text += f"📋 Заказов: {sum(stats['orders_by_status'].values())}\n"
    text += f"💰 Выручка: {stats['revenue']:.0f}₽\n"
    text += f"📈 Средний чек: {stats['avg_order']:.0f}₽\n\n"

    if stats['popular_products']:
        text += "🔥 Популярные товары:\n"
        for name, qty in stats['popular_products'][:3]:
            text += f"• {name[:20]}: {qty} шт.\n"

    await message.answer(text)


# Возврат к категориям
@dp.callback_query(F.data == "back_to_cats")
async def back_to_categories(callback: types.CallbackQuery):
    await callback.message.delete()
    await show_catalog(callback.message)
    await callback.answer()


# Заглушка для неактивных кнопок навигации
@dp.callback_query(F.data == "noop")
async def noop(callback: types.CallbackQuery):
    await callback.answer()


# Добавление начальных товаров (если база пуста)
def add_initial_products():
    with sqlite3.connect('shop.db') as conn:
        cursor = conn.cursor()

        # Проверяем, есть ли уже товары
        cursor.execute("SELECT COUNT(*) FROM products WHERE is_available = 1")
        count = cursor.fetchone()[0]

        if count == 0:
            # Получаем ID категорий
            cursor.execute("SELECT id, name FROM categories")
            categories = {name: id for id, name in cursor.fetchall()}

            # Товары для Под-Систем
            pods_products = [
                ("Voopoo Drag X", "Мощный под-мод с регулировкой воздуха, 80W", 3490, categories.get('pods')),
                ("Smok Nord 5", "Компактная под-система с двумя испарителями", 2790, categories.get('pods')),
                ("Uwell Caliburn G2", "Популярная под-система с отличным вкусом", 2190, categories.get('pods')),
                ("Vaporesso XROS 3", "Надежная под-система с регулировкой тяги", 2390, categories.get('pods')),
                ("GeekVape Wenax K1", "Простая под-система для начинающих", 1890, categories.get('pods'))
            ]

            # Товары для Жидкости
            liquid_products = [
                ("Bloody Mary Свежая Клубника", "Фруктовая свежесть, 30ml", 590, categories.get('liquid')),
                ("Dinner Lady Lemon Tart", "Классический лимонный тарт, 50ml", 790, categories.get('liquid')),
                ("Nasty Juice ASAP Grape", "Виноград со льдом, 60ml", 890, categories.get('liquid')),
                ("HUSTLE COLA", "Вкус классической колы, 30ml", 550, categories.get('liquid')),
                ("Pynapple Ice", "Ананас со льдом, 100ml", 990, categories.get('liquid'))
            ]

            # Товары для Снюс/Пластинки
            snus_products = [
                ("Siberia White Dry", "Крепкий снюс, 20 порций", 450, categories.get('snus')),
                ("Odens Cold White", "Мятный холодок, 24 порции", 420, categories.get('snus')),
                ("Lyft Freeze", "Никотиновые пластинки со льдом", 380, categories.get('snus')),
                ("Velo Ice Cool", "Мятные пластинки, 20 порций", 350, categories.get('snus')),
                ("Pablo Exclusive", "Крепкие никотиновые пластинки", 480, categories.get('snus'))
            ]

            # Товары для Одноразовых устройств
            disposable_products = [
                ("Elf Bar 1500", "1500 затяжек, фруктовое ассорти", 890, categories.get('disposable')),
                ("HQD Cuvie Plus", "1200 затяжек, компактный", 750, categories.get('disposable')),
                ("Puff Mi", "800 затяжек, разнообразие вкусов", 650, categories.get('disposable')),
                ("Vozol Gear 10000", "10000 затяжек, с дисплеем", 1990, categories.get('disposable')),
                ("Airscream", "1000 затяжек, стильный дизайн", 790, categories.get('disposable'))
            ]

            # Товары для Испарителей
            vape_products = [
                ("Voopoo PnP Coil", "Сменные испарители 0.3/0.6/0.8 Ohm", 350, categories.get('vaporizers')),
                ("Smok RPM Coil", "Для Nord и RPM устройств", 320, categories.get('vaporizers')),
                ("Uwell Caliburn G Coil", "Для Caliburn G и G2", 380, categories.get('vaporizers')),
                ("Vaporesso GTX Coil", "Для XROS и GTX устройств", 340, categories.get('vaporizers')),
                ("GeekVape B Series", "Для Wenax устройств", 300, categories.get('vaporizers'))
            ]

            # Добавляем все товары
            all_products = (pods_products + liquid_products + snus_products +
                            disposable_products + vape_products)

            for product in all_products:
                if product[3] is not None:
                    cursor.execute(
                        "INSERT INTO products (name, description, price, category_id) VALUES (?, ?, ?, ?)",
                        (product[0], product[1], product[2], product[3])
                    )

            conn.commit()
            print("✅ Начальные товары добавлены")


# ============================================
# === ЗАПУСК БОТА ===
# ============================================




# ============================================
# === ЗАПУСК БОТА ЧЕРЕЗ ВЕБХУКИ (ДЛЯ RENDER) ===
# ============================================
import aiohttp
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import os

# Функция для установки вебхука при запуске
async def on_startup(app: web.Application):
    # Удаляем предыдущий вебхук, если был
    await bot.delete_webhook()
    # Устанавливаем новый вебхук, указывая URL вашего сервиса на Render
    webhook_url = f"https://telegram-shop-bot2.onrender.com/webhook"
    await bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True
    )
    print(f"✅ Вебхук установлен на {webhook_url}")

# Функция при завершении
async def on_shutdown(app: web.Application):
    print("🔄 Удаляем вебхук...")
    await bot.delete_webhook()
    await bot.session.close()

# Создаем aiohttp приложение
app = web.Application()

# Регистрируем обработчики вебхука
webhook_requests_handler = SimpleRequestHandler(
    dispatcher=dp,
    bot=bot,
)
webhook_requests_handler.register(app, path="/webhook")

# Настраиваем startup/shutdown
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

# Точка входа для Gunicorn
if __name__ == "__main__":
    # Этот блок не выполнится при запуске через Gunicorn
    pass
else:
    # При запуске через Gunicorn просто создаем экземпляр app
    # Инициализируем базу данных (один раз)
    init_db()
    add_initial_products()
    print("✅ База данных инициализирована")
    print("🚀 Бот готов к работе через вебхуки")

# ============================================
# === FLASK ПРИЛОЖЕНИЕ (оставляем для health check) ===
# ============================================
# ... (ваш существующий код Flask) ...

# ============================================
# === FLASK ПРИЛОЖЕНИЕ (оставляем для health check) ===
# ============================================
# ... (ваш существующий код Flask) ...

# ============================================
# === FLASK ЗАПУСКАЕТСЯ GUNICORN ===
# ============================================
print("🚀 Flask приложение готово к работе через Gunicorn")
print("✅ Конфигурация загружена успешно!")
# ============================================
# === ТОЧКА ВХОДА ДЛЯ ЗАПУСКА AioHTTP СЕРВЕРА ===
# ============================================
if __name__ == "__main__":
    # Этот код выполняется только при прямом запуске: python cod.py
    import os
    from aiohttp import web

    port = int(os.environ.get('PORT', 10000))
    print(f"🚀 Запуск aiohttp сервера на порту {port}...")

    # Инициализация БД (если нужно)
    init_db()
    add_initial_products()

    # Запускаем приложение
    web.run_app(app, host='0.0.0.0', port=port)
else:
    # Код в этой ветке выполняется при импорте (например, для Gunicorn, который нам больше не нужен)
    # Мы оставим это для обратной совместимости, но Gunicorn мы больше использовать не будем.
    pass
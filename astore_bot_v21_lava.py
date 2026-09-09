# 🚀 БОТ МАГАЗИНА ASTORE v21.0 — LAVA.TOP + FRAGMENT API
# Фреймворк: aiogram 3.x | База данных: SQLite | Платёжка: Lava.top API | Доставка: Fragment Reseller API

import asyncio
import logging
import sqlite3
import time
import aiohttp
from urllib.parse import urlencode
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = "8815672975:AAFe19Fw6xnflERfRCbtfL8g2urDlFF2W0s"

# СПИСОК АДМИНИСТРАТОРОВ
ADMIN_IDS = [898106003]

# ==================== LAVA.TOP ====================
# Токен API из личного кабинета Lava.top (раздел API / Интеграции)
# Никакой Shop ID не нужен — работа происходит строго по этому Bearer-токену!
LAVA_API_KEY = "M19jYu6MmHPeoNTERmk6ZuAY2wTaNyduUEVRevPfwsdQbx8HCkK6Lh48pNiTydJi"
LAVA_API_URL = "https://api.lava.top/v1"

# РЕАЛЬНЫЙ API КЛЮЧ FRAGMENT ДЛЯ АВТОВЫДАЧИ STARS И PREMIUM
FRAGMENT_API_KEY = "K8J5MX56CYxkcSgjnpHaZBZspTu0CVJOFiYySNIeDEg"
FRAGMENT_API_URL = "https://api.fragment-api.com/v1"  # Базовый URL реселлера Fragment

# ПОДДЕРЖКА
SUPPORT_USERNAME = "rabotnikgoda297"

# Прайс-лист Astore
PRICES_STARS = {
    50: 71,
    100: 143,
    150: 215,
    250: 359,
    500: 718,
    1000: 1437,
    2500: 3593
}

PRICES_PREMIUM = {
    "3 месяца": 1150,
    "6 месяцев": 1990,
    "12 месяцев": 2990
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==================== СОСТОЯНИЯ FSM ====================
class OrderState(StatesGroup):
    waiting_for_friend_username = State()

class AdminState(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_giveaway_title = State()
    waiting_for_giveaway_text = State()

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                target_username TEXT,
                item_type TEXT,
                price INTEGER,
                status TEXT DEFAULT 'pending',
                lava_invoice_id TEXT DEFAULT NULL,
                fragment_tx_id TEXT DEFAULT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                text TEXT,
                active INTEGER DEFAULT 1
            )
        """)
        conn.commit()

init_db()

def register_user(user_id: int, username: str, first_name: str):
    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                       (user_id, username or "без_юзернейма", first_name or "Пользователь"))
        cursor.execute("UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                       (username or "без_юзернейма", first_name or "Пользователь", user_id))
        conn.commit()

# ==================== LAVA.TOP ИНТЕГРАЦИЯ (БЕЗ SHOP ID) ====================

async def create_lava_invoice(amount: float, order_id: int, title: str) -> tuple[str, str]:
    """
    Создание счета на оплату через Lava.top API по единому API-ключу.
    Возвращает ссылку на оплату (pay_url) и ID инвойса Lava (invoice_id).
    """
    headers = {
        "Authorization": f"Bearer {LAVA_API_KEY}",
        "X-Api-Key": LAVA_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "amount": float(amount),
        "currency": "RUB",
        "order_id": str(order_id),
        "comment": f"Astore: {title} (Заказ #{order_id})",
        "success_url": f"https://t.me/{SUPPORT_USERNAME}",
        "fail_url": f"https://t.me/{SUPPORT_USERNAME}"
    }

    endpoints = [
        f"{LAVA_API_URL}/invoices",
        f"{LAVA_API_URL}/invoices/create",
        "https://api.lava.ru/business/invoice/create"
    ]

    for endpoint in endpoints:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(endpoint, json=payload, headers=headers, timeout=12) as resp:
                    resp_text = await resp.text()
                    logging.info(f"Lava Invoice Create [{resp.status}] -> {resp_text}")
                    
                    if resp.status in (200, 201):
                        data = await resp.json()
                        inv_data = data.get("data") or data.get("result") or data
                        
                        pay_url = (
                            inv_data.get("url") or 
                            inv_data.get("pay_url") or 
                            inv_data.get("payment_url") or 
                            inv_data.get("redirect_url")
                        )
                        inv_id = (
                            inv_data.get("id") or 
                            inv_data.get("invoice_id") or 
                            str(order_id)
                        )
                        
                        if pay_url:
                            return pay_url, str(inv_id)
        except Exception as e:
            logging.error(f"Lava endpoint {endpoint} error: {e}")

    # Запасной прямой редирект, если API ответил с ошибкой
    fallback_url = f"https://lava.top/pay/{order_id}?amount={amount}"
    return fallback_url, str(order_id)


async def verify_lava_payment(order_id: int, lava_invoice_id: str = None) -> tuple[bool, str]:
    """
    Проверка статуса платежа через Lava.top API.
    """
    headers = {
        "Authorization": f"Bearer {LAVA_API_KEY}",
        "X-Api-Key": LAVA_API_KEY,
        "Accept": "application/json"
    }

    check_id = lava_invoice_id or str(order_id)
    endpoints = [
        f"{LAVA_API_URL}/invoices/{check_id}",
        f"{LAVA_API_URL}/invoices/status?order_id={order_id}"
    ]

    for endpoint in endpoints:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, headers=headers, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        inv_data = data.get("data") or data.get("result") or data
                        status = str(inv_data.get("status", "")).lower()
                        
                        if status in ("success", "paid", "completed"):
                            return True, "Оплачено"
                        elif status in ("pending", "created"):
                            return False, "Ожидает оплаты"
        except Exception as e:
            logging.error(f"Lava check status error: {e}")

    return False, "Платеж не найден или не оплачен"

# ==================== FRAGMENT API ====================

async def get_fragment_balance() -> dict:
    headers = {
        "X-API-Key": FRAGMENT_API_KEY,
        "Content-Type": "application/json"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{FRAGMENT_API_URL}/balance", headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logging.error(f"Fragment Balance API Error [{resp.status}]: {await resp.text()}")
    except Exception as e:
        logging.error(f"Ошибка получения баланса Fragment: {e}")
    return {}

async def send_fragment_auto_delivery(target_username: str, item_type: str, order_id: int) -> dict:
    clean_username = target_username.replace("@", "").strip()
    headers = {
        "X-API-Key": FRAGMENT_API_KEY,
        "Content-Type": "application/json"
    }
    
    if "Stars" in item_type:
        try:
            quantity = int("".join(filter(str.isdigit, item_type)))
        except ValueError:
            quantity = 50
            
        payload = {
            "type": "stars",
            "username": clean_username,
            "quantity": quantity,
            "currency": "TON",
            "order_id": str(order_id)
        }
        endpoint = f"{FRAGMENT_API_URL}/stars/buy"
    else:
        months = 3
        if "6" in item_type:
            months = 6
        elif "12" in item_type:
            months = 12
            
        payload = {
            "type": "premium",
            "username": clean_username,
            "months": months,
            "currency": "TON",
            "order_id": str(order_id)
        }
        endpoint = f"{FRAGMENT_API_URL}/premium/buy"

    logging.info(f"🚀 Запрос к Fragment API ({endpoint}): {payload}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=payload, headers=headers, timeout=20) as resp:
                response_text = await resp.text()
                logging.info(f"📩 Ответ Fragment API [{resp.status}]: {response_text}")
                
                if resp.status in (200, 201):
                    res_json = await resp.json()
                    return {
                        "success": True,
                        "tx_id": res_json.get("tx_id") or res_json.get("order_id") or "OK",
                        "message": res_json.get("message", "Успешно отправлено")
                    }
                else:
                    return {
                        "success": False,
                        "error": f"HTTP {resp.status}: {response_text}"
                    }
    except Exception as e:
        logging.error(f"❌ Ошибка сетевого запроса к Fragment API: {e}")
        return {
            "success": False,
            "error": str(e)
        }

async def deliver_paid_order(order_id: int, callback_message: types.Message):
    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, target_username, item_type, price, status FROM orders WHERE id = ?", (order_id,))
        order = cursor.fetchone()

    if not order:
        return

    user_id, target, item_type, price, status = order

    if status == "delivered":
        await callback_message.answer("✅ Этот заказ уже был доставлен.")
        return

    await callback_message.edit_text(
        f"🎉 <b>ОПЛАТА #{order_id} ПОДТВЕРЖДЕНА!</b>\n\n"
        f"📌 <b>Товар:</b> {item_type}\n"
        f"🎯 <b>Получатель:</b> {target}\n\n"
        "⚡ <b>Запрос отправлен в Fragment API... Идет выгрузка товара!</b>",
        parse_mode="HTML"
    )

    delivery_res = await send_fragment_auto_delivery(target, item_type, order_id)

    if delivery_res.get("success"):
        tx_id = delivery_res.get("tx_id", "OK")
        with sqlite3.connect("astore.db") as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE orders SET status = 'delivered', fragment_tx_id = ? WHERE id = ?", (tx_id, order_id))
            conn.commit()

        await callback_message.answer(
            f"✅ <b>ТОВАР УСПЕШНО ДОСТАВЛЕН!</b>\n\n"
            f"<b>{item_type}</b> автоматически отправлен на аккаунт {target}.\n"
            f"🆔 Транзакция Fragment: <code>{tx_id}</code>\n\n"
            "Спасибо за покупку в Astore!",
            parse_mode="HTML"
        )
        admin_msg = f"✅ <b>АВТОДОСТАВКА УСПЕШНА!</b>\n📦 Заказ #{order_id}\n🎯 Получатель: {target}\n📌 Товар: {item_type}\n🆔 Fragment TX: <code>{tx_id}</code>"
    else:
        err_msg = delivery_res.get("error", "Ошибка API")
        await callback_message.answer(
            f"⚠️ <b>Оплата принята, но произошла задержка в Fragment API:</b>\n<code>{err_msg}</code>\n\nАдминистратор уведомлен.",
            parse_mode="HTML"
        )
        admin_msg = f"⚠️ <b>ОПЛАЧЕНО, НО ОШИБКА АВТОДОСТАВКИ!</b>\n📦 Заказ #{order_id}\n🎯 Получатель: {target}\n📌 Товар: {item_type}\n❌ Ошибка: {err_msg}"

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_msg, parse_mode="HTML")
        except Exception:
            pass

# ==================== КЛАВИАТУРЫ ====================

def main_reply_keyboard(user_id: int):
    builder = ReplyKeyboardBuilder()
    builder.button(text="⭐ Покупка звёзд")
    builder.button(text="👑 Telegram Premium")
    builder.button(text="🎁 Розыгрыши и Акции")
    builder.button(text="👤 Мой профиль")
    builder.button(text="👨‍💻 Поддержка / FAQ")
    
    if user_id in ADMIN_IDS:
        builder.button(text="👑 Панель Администратора")
        
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True)

def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика кассы", callback_data="admin_stats")
    builder.button(text="💎 Баланс Fragment API", callback_data="admin_fragment_balance")
    builder.button(text="👥 Список пользователей", callback_data="admin_users_list")
    builder.button(text="📢 Массовая рассылка", callback_data="admin_broadcast_start")
    builder.button(text="🎁 Добавить розыгрыш", callback_data="admin_giveaway_start")
    builder.button(text="📦 Последние заказы", callback_data="admin_last_orders")
    builder.button(text="◀️ Главное меню", callback_data="back_main")
    builder.adjust(1)
    return builder.as_markup()

# ==================== ХЕНДЛЕРЫ ====================

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    text = (
        f"👋 <b>Салам, {message.from_user.first_name}!</b>\n\n"
        "🔥 Добро пожаловать в <b>Astore</b> — официальный сервис Telegram Stars и Premium!\n\n"
        "⚡ Мгновенная выгрузка, минимальные цены и удобная оплата через <b>Lava.top (СБП / Карты)</b>.\n\n"
        "👇 <b>Выберите раздел в меню ниже:</b>"
    )
    await message.answer(text, reply_markup=main_reply_keyboard(message.from_user.id), parse_mode="HTML")

@dp.message(F.text == "⭐ Покупка звёзд")
async def text_buy_stars(message: types.Message):
    builder = InlineKeyboardBuilder()
    for amount, price in PRICES_STARS.items():
        builder.button(text=f"⭐ {amount} звезд — {price}₽", callback_data=f"select_stars_{amount}")
    builder.button(text="◀️ Назад в меню", callback_data="back_main")
    builder.adjust(1)
    await message.answer("⭐ <b>Выберите необходимый пакет звёзд Telegram:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.message(F.text == "👑 Telegram Premium")
async def text_buy_premium(message: types.Message):
    builder = InlineKeyboardBuilder()
    for period, price in PRICES_PREMIUM.items():
        builder.button(text=f"👑 Premium {period} — {price}₽", callback_data=f"select_prem_{period}")
    builder.button(text="◀️ Назад в меню", callback_data="back_main")
    builder.adjust(1)
    await message.answer("👑 <b>Выберите срок подписки Telegram Premium:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.message(F.text == "🎁 Розыгрыши и Акции")
async def text_giveaways(message: types.Message):
    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, title, text FROM giveaways WHERE active = 1 ORDER BY id DESC")
        giveaways = cursor.fetchall()
    
    if not giveaways:
        text = "🎁 <b>РАЗДЕЛ РОЗЫГРЫШЕЙ И АКЦИЙ</b>\n\nАктивных розыгрышей пока нет."
    else:
        text = "🎁 <b>АКТИВНЫЕ РОЗЫГРЫШИ И НОВОСТИ ASTORE:</b>\n\n"
        for g_id, title, g_text in giveaways:
            text += f"🏆 <b>{title}</b>\n{g_text}\n───────────────────\n\n"
            
    await message.answer(text, reply_markup=main_reply_keyboard(message.from_user.id), parse_mode="HTML")

@dp.message(F.text == "👤 Мой профиль")
async def text_profile(message: types.Message):
    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), SUM(price) FROM orders WHERE user_id = ? AND status IN ('paid', 'delivered')", (message.from_user.id,))
        count, total = cursor.fetchone()
    
    text = (
        f"👤 <b>Ваш профиль в Astore:</b>\n\n"
        f"🆔 <b>Ваш Telegram ID:</b> <code>{message.from_user.id}</code>\n"
        f"📦 <b>Успешных покупок:</b> {count or 0} шт.\n"
        f"💰 <b>Потрачено всего:</b> {total or 0} ₽\n"
    )
    await message.answer(text, reply_markup=main_reply_keyboard(message.from_user.id), parse_mode="HTML")

@dp.message(F.text == "👨‍💻 Поддержка / FAQ")
async def text_support(message: types.Message):
    text = f"👨‍💻 <b>СЛУЖБА ПОДДЕРЖКИ ASTORE</b>\n\n💬 По любым вопросам пишите админу: @{SUPPORT_USERNAME}"
    builder = InlineKeyboardBuilder()
    builder.button(text=f"💬 Написать в поддержку (@{SUPPORT_USERNAME})", url=f"https://t.me/{SUPPORT_USERNAME}")
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

# --- ОФОРМЛЕНИЕ ЗАКАЗА ---

@dp.callback_query(F.data.startswith("select_"))
async def select_item(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = callback.data
    if data.startswith("select_stars_"):
        amount = int(data.replace("select_stars_", ""))
        price = PRICES_STARS[amount]
        item_title = f"⭐ {amount} Telegram Stars"
    else:
        period = data.replace("select_prem_", "")
        price = PRICES_PREMIUM[period]
        item_title = f"👑 Telegram Premium ({period})"
        
    await state.update_data(item_title=item_title, price=price)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Купить себе", callback_data="recipient_self")
    builder.button(text="🎁 Купить в подарок другу", callback_data="recipient_friend")
    builder.button(text="◀️ Назад", callback_data="back_main")
    builder.adjust(1)
    
    await callback.message.edit_text(
        f"🛒 Вы выбрали: <b>{item_title}</b> ({price}₽)\n\nКому хотите отправить покупку?",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "recipient_self")
async def recipient_self(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    user_target = f"@{callback.from_user.username}" if callback.from_user.username else f"ID_{callback.from_user.id}"
    await create_order_and_send_invoice(callback.message, callback.from_user.id, user_target, state)

@dp.callback_query(F.data == "recipient_friend")
async def recipient_friend(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(OrderState.waiting_for_friend_username)
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Отмена", callback_data="back_main")
    
    await callback.message.edit_text(
        f"🎁 <b>Покупка в подарок другу!</b>\n\n✏️ Отправьте юзернейм друга (например: <code>@{SUPPORT_USERNAME}</code>):",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@dp.message(OrderState.waiting_for_friend_username)
async def process_friend_username(message: types.Message, state: FSMContext):
    target = message.text.strip()
    if not target.startswith("@"):
        target = f"@{target}"
    await create_order_and_send_invoice(message, message.from_user.id, target, state)

async def create_order_and_send_invoice(event_msg, user_id: int, target_username: str, state: FSMContext):
    data = await state.get_data()
    item_title = data["item_title"]
    price = data["price"]
    
    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO orders (user_id, target_username, item_type, price) VALUES (?, ?, ?, ?)",
                       (user_id, target_username, item_title, price))
        order_id = cursor.lastrowid
        conn.commit()
    
    # СОЗДАЕМ ССЫЛКУ LAVA.TOP БЕЗ SHOP ID
    pay_url, lava_inv_id = await create_lava_invoice(price, order_id, item_title)
    
    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE orders SET lava_invoice_id = ? WHERE id = ?", (lava_inv_id, order_id))
        conn.commit()
    
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оплатить через Lava (СБП / Карта)", url=pay_url)
    builder.button(text="🔄 Проверить оплату", callback_data=f"check_pay_{order_id}")
    builder.button(text="◀️ Назад в меню", callback_data="back_main")
    builder.adjust(1)
    
    text = (
        f"📦 <b>Заказ #{order_id} сформирован!</b>\n\n"
        f"📌 <b>Товар:</b> {item_title}\n"
        f"🎯 <b>Получатель:</b> {target_username}\n"
        f"💰 <b>К оплате:</b> <code>{price} ₽</code>\n\n"
        "Нажмите <b>«Оплатить через Lava»</b>, завершите платеж по СБП или картой, а затем нажмите <b>«Проверить оплату»</b>."
    )
    
    if isinstance(event_msg, types.CallbackQuery):
        await event_msg.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await event_msg.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        
    await state.clear()

@dp.callback_query(F.data.startswith("check_pay_"))
async def check_payment(callback: types.CallbackQuery):
    order_id = int(callback.data.replace("check_pay_", ""))

    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, target_username, item_type, price, status, lava_invoice_id FROM orders WHERE id = ?", (order_id,))
        order = cursor.fetchone()

    if not order:
        await callback.answer("❌ Заказ не найден!", show_alert=True)
        return

    user_id, target, item_type, price, status, lava_inv_id = order

    if callback.from_user.id != user_id and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Это не ваш заказ.", show_alert=True)
        return

    if status == "delivered":
        await callback.answer("✅ Этот заказ уже оплачен и доставлен!", show_alert=True)
        return

    if status == "paid":
        await callback.answer("⏳ Заказ уже подтверждён, запускаю выдачу.", show_alert=True)
        await deliver_paid_order(order_id, callback.message)
        return

    # ПРОВЕРКА ЧЕРЕЗ LAVA.TOP API
    is_paid, verify_message = await verify_lava_payment(order_id, lava_inv_id)

    if is_paid:
        with sqlite3.connect("astore.db") as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (order_id,))
            conn.commit()

        await callback.answer("✅ Оплата подтверждена!", show_alert=True)
        await deliver_paid_order(order_id, callback.message)
    else:
        await callback.answer("⏳ Оплата пока не подтверждена в Lava.", show_alert=True)

# ТЕСТОВЫЙ РЕЖИМ ДЛЯ АДМИНА (/confirm <id>)
@dp.message(Command("confirm"))
async def confirm_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /confirm <номер_заказа>")
        return

    order_id = int(parts[1])

    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        row = cursor.fetchone()

        if not row:
            await message.answer("❌ Заказ не найден.")
            return

        if row[0] == "delivered":
            await message.answer("✅ Заказ уже доставлен.")
            return

        cursor.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (order_id,))
        conn.commit()

    await message.answer(f"🧪 Заказ #{order_id} подтвержден вручную. Запускаю Fragment API...")
    await deliver_paid_order(order_id, message)

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Вы вернулись в главное меню.", reply_markup=main_reply_keyboard(callback.from_user.id))

# ==================== АДМИН-ПАНЕЛЬ ====================

@dp.message(Command("admin"))
@dp.message(F.text == "👑 Панель Администратора")
@dp.callback_query(F.data == "admin_menu")
async def admin_panel(event: types.Message | types.CallbackQuery):
    user_id = event.from_user.id
    if isinstance(event, types.CallbackQuery):
        await event.answer()

    if user_id not in ADMIN_IDS:
        return

    text = "👑 <b>ПАНЕЛЬ АДМИНИСТРАТОРА ASTORE</b>\n\nВыберите нужное действие на кнопках ниже:"
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=admin_keyboard(), parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_fragment_balance")
async def admin_fragment_balance(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return

    bal_info = await get_fragment_balance()
    text = f"💎 <b>БАЛАНС В FRAGMENT API:</b>\n\n<code>{bal_info}</code>"
    await callback.message.edit_text(text, reply_markup=admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return

    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*), SUM(price) FROM orders WHERE status IN ('paid', 'delivered')")
        orders_count, total_sum = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM users")
        users_count = cursor.fetchone()[0]

    text = (
        f"📊 <b>СТАТИСТИКА КАССЫ И БОТА:</b>\n\n"
        f"👥 Всего пользователей: <b>{users_count}</b>\n"
        f"📦 Успешных оплат: <b>{orders_count or 0}</b> шт.\n"
        f"💰 Чистая касса: <b>{total_sum or 0} ₽</b>"
    )
    await callback.message.edit_text(text, reply_markup=admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_users_list")
async def admin_users_list(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return

    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, first_name FROM users ORDER BY joined_at DESC LIMIT 30")
        users = cursor.fetchall()
    
    text = "👥 <b>ПОСЛЕДНИЕ 30 ПОЛЬЗОВАТЕЛЕЙ:</b>\n\n"
    for idx, (u_id, username, first_name) in enumerate(users, start=1):
        text += f"{idx}. <b>{first_name}</b> (@{username}) | ID: <code>{u_id}</code>\n"
    
    await callback.message.edit_text(text, reply_markup=admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_last_orders")
async def admin_last_orders(callback: types.CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return

    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, target_username, item_type, price, status FROM orders ORDER BY id DESC LIMIT 5")
        orders = cursor.fetchall()
    
    text = "📦 <b>ПОСЛЕДНИЕ 5 ЗАКАЗОВ:</b>\n\n"
    if not orders:
        text += "Заказов пока нет."
    else:
        for o_id, target, item, price, status in orders:
            st_icon = "✅" if status in ("paid", "delivered") else "⏳"
            text += f"{st_icon} <b>Заказ #{o_id}</b> | {target} | {item} ({price}₽)\n"
        
    await callback.message.edit_text(text, reply_markup=admin_keyboard(), parse_mode="HTML")

# --- МАССОВАЯ РАССЫЛКА ---
@dp.callback_query(F.data == "admin_broadcast_start")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.waiting_for_broadcast)
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Отмена", callback_data="admin_menu")
    await callback.message.edit_text("📢 <b>Отправьте текст сообщения для рассылки всем пользователям:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.message(AdminState.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    broadcast_text = message.text.strip()
    
    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        users = cursor.fetchall()
    
    count = 0
    await message.answer(f"🚀 Запуск рассылки на {len(users)} пользователей...")
    
    for (u_id,) in users:
        try:
            await bot.send_message(u_id, f"📢 <b>НОВОСТИ ASTORE!</b>\n\n{broadcast_text}", parse_mode="HTML")
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
            
    await message.answer(f"✅ Рассылка завершена! Доставлено {count} пользователям.")
    await state.clear()

# --- ДОБАВЛЕНИЕ РОЗЫГРЫША ---
@dp.callback_query(F.data == "admin_giveaway_start")
async def admin_giveaway_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.waiting_for_giveaway_title)
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Отмена", callback_data="admin_menu")
    await callback.message.edit_text("🎁 <b>Введите заголовок для нового розыгрыша:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.message(AdminState.waiting_for_giveaway_title)
async def process_giveaway_title(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.update_data(g_title=message.text.strip())
    await state.set_state(AdminState.waiting_for_giveaway_text)
    await message.answer("✏️ <b>Теперь введите подробное описание розыгрыша и условия:</b>", parse_mode="HTML")

@dp.message(AdminState.waiting_for_giveaway_text)
async def process_giveaway_text(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    title = data["g_title"]
    text = message.text.strip()
    
    with sqlite3.connect("astore.db") as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO giveaways (title, text) VALUES (?, ?)", (title, text))
        conn.commit()
    
    await message.answer(f"✅ Розыгрыш <b>«{title}»</b> успешно опубликован в разделе акция!", parse_mode="HTML")
    await state.clear()

# ==================== ЗАПУСК ====================
async def main():
    print("🚀 Магазин Astore v21.0 (Lava.top + Fragment API) запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

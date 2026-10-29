import asyncio
import os
from aiohttp import web
import logging
import ssl
import sys
from datetime import datetime, timedelta, timezone
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# === НАСТРОЙКИ ===
BOT_TOKEN = "9246224974:AAEjMFjyxDEO-cV5NVMh5sxeqYAbAkyNwhI"
ADMIN_IDS = [955701804, 849450817]  # Твой ID и ID Артёма

# === ПАРАМЕТРЫ ПОДКЛЮЧЕНИЯ К SUPABASE (ПУЛ ТРАНЗАКЦИЙ) ===
DB_USER = "postgres.kuiqxgqjfatxvlyckcgs"
DB_PASSWORD = "Duhuze-8567"  # Замени на свой пароль от базы данных
DB_HOST = "aws-0-eu-west-1.pooler.supabase.com"
DB_PORT = 6543
DB_NAME = "postgres"

# Часовой пояс Екб (UTC+5)
TZ = timezone(timedelta(hours=5))

TIMES_TUDA = ["8:30", "10:15", "12:00", "14:15", "16:00", "17:40"]
TIMES_OBRATNO = ["10:00", "12:00", "13:30", "16:00", "17:30", "19:00", "21:00"]
MAX_SEATS = 4

bot = None
dp = Dispatcher()

# Глобальная переменная для пула соединений с БД
db_pool = None


# === БАЗА ДАННЫХ (POSTGRESQL) ===
async def init_db():
    global db_pool

    # Создаем пул через параметры (надежно для asyncpg и Windows)
    db_pool = await asyncpg.create_pool(
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        ssl="require"
    )

    async with db_pool.acquire() as connection:
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, 
                name TEXT
            )
        ''')
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS slots (
                id SERIAL PRIMARY KEY, 
                date TEXT, 
                direction TEXT, 
                time TEXT
            )
        ''')
        await connection.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                user_id BIGINT, 
                slot_id INT, 
                CONSTRAINT unique_booking UNIQUE (user_id, slot_id)
            )
        ''')


# === СОСТОЯНИЯ ===
class Reg(StatesGroup):
    waiting_for_name = State()
    waiting_for_admin_passenger = State()


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_dates():
    today = datetime.now(TZ).date()
    return [today, today + timedelta(days=1), today + timedelta(days=2)]


def format_date(d):
    return d.strftime("%d.%m.%Y")


def main_keyboard(is_admin=False):
    kb = [
        [InlineKeyboardButton(text="🚐 11 общага ➔ НВК", callback_data="dir_tuda")],
        [InlineKeyboardButton(text="🚐 НВК ➔ 11 общага", callback_data="dir_obratno")],
        [InlineKeyboardButton(text="📋 Мои записи / Отменить", callback_data="my_bookings")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="⚙️ Панель Артёма (Админ)", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def notify_admin(text_notification: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text_notification)
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")


# === СТАРТ И МЕНЮ ===
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    is_admin = (message.from_user.id in ADMIN_IDS)
    await message.answer("Расписание STRRоклиста. Куда едем?", reply_markup=main_keyboard(is_admin))


# === ЛОГИКА ПАССАЖИРА ===
@dp.callback_query(F.data.startswith("dir_"))
async def pass_direction(call: CallbackQuery, state: FSMContext):
    direction = "tuda" if call.data == "dir_tuda" else "obratno"
    user_id = call.from_user.id

    async with db_pool.acquire() as connection:
        user = await connection.fetchrow("SELECT name FROM users WHERE user_id = $1", user_id)

    if not user:
        await state.update_data(direction=direction)
        await call.message.edit_text("Привет! Напиши свое имя (оно будет в списке пассажиров):")
        await state.set_state(Reg.waiting_for_name)
    else:
        await show_dates_to_passenger(call.message, direction)


@dp.message(Reg.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()[:50]
    user_id = message.from_user.id

    async with db_pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO users (user_id, name) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET name = $2",
            user_id, name
        )

    data = await state.get_data()
    await message.answer(f"Записал тебя как {name}!")
    await show_dates_to_passenger(message, data['direction'])
    await state.clear()


async def show_dates_to_passenger(message: Message, direction: str):
    dir_name = "11 общага ➔ НВК" if direction == "tuda" else "НВК ➔ 11 общага"
    dates = get_dates()

    kb = []
    for d in dates:
        kb.append(
            [InlineKeyboardButton(text=f"📅 {format_date(d)}", callback_data=f"pdate_{direction}_{d.isoformat()}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])

    text = f"Маршрут: **{dir_name}**\nВыбери день:"

    if isinstance(message, Message) and message.from_user.id != bot.id:
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")
    else:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("pdate_"))
async def show_slots_for_passenger(call: CallbackQuery):
    _, direction, date_iso = call.data.split("_")

    async with db_pool.acquire() as connection:
        query = """
            SELECT s.id, s.time, COUNT(b.user_id) 
            FROM slots s 
            LEFT JOIN bookings b ON s.id = b.slot_id 
            WHERE s.date = $1 AND s.direction = $2 
            GROUP BY s.id, s.time
            ORDER BY s.time
        """
        slots = await connection.fetch(query, date_iso, direction)

    kb = []
    text = f"Окна на {date_iso} ({'Туда' if direction == 'tuda' else 'Обратно'}):\n\n"

    if not slots:
        text += "Артём еще не открыл окна на этот день 😔"
    else:
        for slot in slots:
            slot_id = slot['id']
            time = slot['time']
            count = slot['count']

            async with db_pool.acquire() as connection:
                passengers = await connection.fetch(
                    "SELECT u.name FROM bookings b JOIN users u ON b.user_id = u.user_id WHERE b.slot_id = $1",
                    slot_id
                )

            pass_names = ", ".join([p['name'] for p in passengers]) if passengers else "никого"
            text += f"🕒 **{time}** | Мест: {MAX_SEATS - count}\nЕдут: {pass_names}\n\n"

            if count < MAX_SEATS:
                kb.append([InlineKeyboardButton(text=f"Забронировать на {time} ({count}/{MAX_SEATS})",
                                                callback_data=f"book_{slot_id}")])

    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"dir_{direction}")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("book_"))
async def book_slot(call: CallbackQuery, state: FSMContext):
    slot_id = int(call.data.split("_")[1])
    user_id = call.from_user.id

    async with db_pool.acquire() as connection:
        user_row = await connection.fetchrow("SELECT name FROM users WHERE user_id = $1", user_id)
        user_name = user_row['name'] if user_row else "Пассажир"

        slot_row = await connection.fetchrow("SELECT date, direction, time FROM slots WHERE id = $1", slot_id)

        count_row = await connection.fetchrow("SELECT COUNT(*) FROM bookings WHERE slot_id = $1", slot_id)
        count = count_row['count']

        if count >= MAX_SEATS:
            await call.answer("Мест больше нет!", show_alert=True)
            return

        try:
            await connection.execute("INSERT INTO bookings (user_id, slot_id) VALUES ($1, $2)", user_id, slot_id)
            await call.answer("Успешно забронировано!", show_alert=True)

            if slot_row:
                s_date = slot_row['date']
                s_dir = slot_row['direction']
                s_time = slot_row['time']
                dir_txt = "Туда (11 -> НВК)" if s_dir == "tuda" else "Обратно (НВК -> 11)"
                await notify_admin(
                    f"➕ **Новая запись!**\n"
                    f"👤 Пассажир: {user_name}\n"
                    f"🚐 Маршрут: {dir_txt}\n"
                    f"📅 Дата: {s_date} | 🕒 Время: {s_time}\n"
                    f"💺 Занято мест: {count + 1}/{MAX_SEATS}"
                )
        except asyncpg.UniqueViolationError:
            await call.answer("Ты уже записан на этот рейс!", show_alert=True)

    await state.clear()
    is_admin = (call.from_user.id in ADMIN_IDS)
    await call.message.edit_text("Расписание STRRоклиста. Куда едем?", reply_markup=main_keyboard(is_admin))


# === МОИ ЗАПИСИ (СНЯТИЕ БРОНИ) ===
@dp.callback_query(F.data == "my_bookings")
async def my_bookings(call: CallbackQuery):
    user_id = call.from_user.id
    today = datetime.now(TZ).date().isoformat()

    async with db_pool.acquire() as connection:
        query = """
            SELECT b.slot_id, s.date, s.direction, s.time 
            FROM bookings b 
            JOIN slots s ON b.slot_id = s.id 
            WHERE b.user_id = $1 AND s.date >= $2
            ORDER BY s.date, s.time
        """
        bookings = await connection.fetch(query, user_id, today)

    if not bookings:
        await call.message.edit_text("У тебя нет активных записей.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main")]]))
        return

    text = "Твои поездки:\n\n"
    kb = []
    for b in bookings:
        slot_id = b['slot_id']
        date = b['date']
        direction = b['direction']
        time = b['time']

        dir_text = "ТУДА" if direction == "tuda" else "ОБРАТНО"
        text += f"📅 {date} | 🕒 {time} | {dir_text}\n"
        kb.append([InlineKeyboardButton(text=f"❌ Отменить {date} {time}", callback_data=f"cancel_{slot_id}")])

    kb.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_booking(call: CallbackQuery):
    slot_id = int(call.data.split("_")[1])
    user_id = call.from_user.id

    async with db_pool.acquire() as connection:
        user_row = await connection.fetchrow("SELECT name FROM users WHERE user_id = $1", user_id)
        user_name = user_row['name'] if user_row else "Пассажир"

        slot_row = await connection.fetchrow("SELECT date, direction, time FROM slots WHERE id = $1", slot_id)

        await connection.execute("DELETE FROM bookings WHERE user_id = $1 AND slot_id = $2", user_id, slot_id)

        if slot_row:
            s_date = slot_row['date']
            s_dir = slot_row['direction']
            s_time = slot_row['time']
            dir_txt = "Туда" if s_dir == "tuda" else "Обратно"
            await notify_admin(
                f"❌ **Отмена брони!**\n"
                f"👤 Пассажир: {user_name} снялся с рейса\n"
                f"🚐 {dir_txt} на {s_date} в {s_time}"
            )

    await call.answer("Бронь отменена!", show_alert=True)
    await my_bookings(call)


# === ПАНЕЛЬ АДМИНА ===
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return await call.answer("Доступ запрещен", show_alert=True)

    dates = get_dates()
    kb = [[InlineKeyboardButton(text=f"📅 {format_date(d)}", callback_data=f"adate_{d.isoformat()}")] for d in dates]
    kb.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="back_main")])

    await call.message.edit_text("⚙️ Выбери день для управления окнами:",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("adate_"))
async def admin_date(call: CallbackQuery):
    date_iso = call.data.split("_")[1]
    kb = [
        [InlineKeyboardButton(text="ТУДА (11 -> НВК)", callback_data=f"adir_tuda_{date_iso}")],
        [InlineKeyboardButton(text="ОБРАТНО (НВК -> 11)", callback_data=f"adir_obratno_{date_iso}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ]
    await call.message.edit_text(f"День: {date_iso}. Выбери направление:",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


async def render_admin_slots(message: Message, direction: str, date_iso: str):
    times = TIMES_TUDA if direction == "tuda" else TIMES_OBRATNO

    async with db_pool.acquire() as connection:
        slots_data = await connection.fetch("SELECT id, time FROM slots WHERE date = $1 AND direction = $2", date_iso,
                                            direction)
        opened_slots_map = {row['time']: row['id'] for row in slots_data}

    text = f"📋 **Управление окнами на {date_iso}** ({'Туда' if direction == 'tuda' else 'Обратно'}):\n\n"
    kb = []

    for t in times:
        if t in opened_slots_map:
            slot_id = opened_slots_map[t]
            async with db_pool.acquire() as connection:
                passengers = await connection.fetch(
                    "SELECT u.name FROM bookings b JOIN users u ON b.user_id = u.user_id WHERE b.slot_id = $1",
                    slot_id
                )

            pass_names = ", ".join([p['name'] for p in passengers]) if passengers else "никого"
            count = len(passengers)

            text += f"🕒 **{t}** | Мест: {MAX_SEATS - count}/{MAX_SEATS}\n👤 Едут: {pass_names}\n\n"

            kb.append(
                [InlineKeyboardButton(text=f"❌ Закрыть слот {t}", callback_data=f"toggle_{direction}_{date_iso}_{t}")])
            if count < MAX_SEATS:
                kb.append([InlineKeyboardButton(text=f"➕ Добавить пассажира на {t}",
                                                callback_data=f"addpass_{slot_id}_{direction}_{date_iso}")])
        else:
            text += f"🕒 **{t}** — ❌ Закрыт\n\n"
            kb.append(
                [InlineKeyboardButton(text=f"✅ Открыть слот {t}", callback_data=f"toggle_{direction}_{date_iso}_{t}")])

    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adate_{date_iso}")])

    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("adir_"))
async def admin_slots(call: CallbackQuery):
    _, direction, date_iso = call.data.split("_")
    await render_admin_slots(call.message, direction, date_iso)


@dp.callback_query(F.data.startswith("toggle_"))
async def toggle_slot(call: CallbackQuery):
    _, direction, date_iso, time = call.data.split("_")

    async with db_pool.acquire() as connection:
        slot = await connection.fetchrow("SELECT id FROM slots WHERE date = $1 AND direction = $2 AND time = $3",
                                         date_iso, direction, time)

        if slot:
            slot_id = slot['id']
            await connection.execute("DELETE FROM bookings WHERE slot_id = $1", slot_id)
            await connection.execute("DELETE FROM slots WHERE id = $1", slot_id)
            await call.answer(f"Окно на {time} закрыто!")
        else:
            await connection.execute("INSERT INTO slots (date, direction, time) VALUES ($1, $2, $3)", date_iso,
                                     direction, time)
            await call.answer(f"Окно на {time} открыто!")

    await render_admin_slots(call.message, direction, date_iso)


# === РУЧНОЕ ДОБАВЛЕНИЕ ПАССАЖИРА АДМИНИСТРАТОРОМ ===
@dp.callback_query(F.data.startswith("addpass_"))
async def admin_add_passenger_start(call: CallbackQuery, state: FSMContext):
    _, slot_id_str, direction, date_iso = call.data.split("_")
    slot_id = int(slot_id_str)

    await state.update_data(slot_id=slot_id, direction=direction, date_iso=date_iso)
    await state.set_state(Reg.waiting_for_admin_passenger)

    await call.message.edit_text(
        "✍️ **Ручное добавление пассажира**\n\n"
        "Напиши имя человека, которого нужно посадить на этот рейс:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adir_{direction}_{date_iso}")]
        ]),
        parse_mode="Markdown"
    )
    await call.answer()


@dp.message(Reg.waiting_for_admin_passenger)
async def admin_add_passenger_finish(message: Message, state: FSMContext):
    passenger_name = message.text.strip()[:50]
    data = await state.get_data()
    slot_id = data.get("slot_id")
    direction = data.get("direction")
    date_iso = data.get("date_iso")

    async with db_pool.acquire() as connection:
        count_row = await connection.fetchrow("SELECT COUNT(*) FROM bookings WHERE slot_id = $1", slot_id)
        count = count_row['count']

        if count >= MAX_SEATS:
            await message.answer("❌ На этот рейс уже нет свободных мест (4/4)!")
            await state.clear()
            return

        fake_user_id = -int(datetime.now().timestamp() * 1000)

        await connection.execute(
            "INSERT INTO users (user_id, name) VALUES ($1, $2)",
            fake_user_id, f"{passenger_name} (от Артёма)"
        )
        try:
            await connection.execute(
                "INSERT INTO bookings (user_id, slot_id) VALUES ($1, $2)",
                fake_user_id, slot_id
            )
            await message.answer(f"✅ Успешно добавлен пассажир: **{passenger_name}**!", parse_mode="Markdown")
        except Exception as e:
            await message.answer(f"❌ Ошибка при добавлении: {e}")

    await state.clear()
    await render_admin_slots(message, direction, date_iso)


# Обработка кнопки Назад
@dp.callback_query(F.data == "back_main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    is_admin = (call.from_user.id in ADMIN_IDS)
    await call.message.edit_text("Расписание STRRоклиста. Куда едем?", reply_markup=main_keyboard(is_admin))


# === ЗАГЛУШКА ДЛЯ RENDER ===
async def health_check(request):
    return web.Response(text="Bot is alive!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Мини-сервер запущен на порту {port} для Render")

# === ОСНОВНОЙ ЗАПУСК ===
async def main():
    global bot
    bot = Bot(token=BOT_TOKEN)

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    await init_db()
    logging.basicConfig(level=logging.INFO)
    print("Бот запущен с PostgreSQL через пул!")
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Сначала запускаем фейковый веб-сервер для Render
    await start_dummy_server()
    # Затем запускаем самого бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())

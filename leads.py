"""Бот-охотник за клиентами для салонов красоты в Telegram. Платный поиск: 25 звёзд за запрос /find.
/find маникюр, стрижка   - оплата 25 Stars, затем поиск по всему Telegram за 30 дней
/watch массаж            - мониторинг чатов аккаунта в реальном времени (до 3 тем)
"""
import asyncio, logging, os, re, sqlite3, sys
from datetime import datetime, timedelta, timezone

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import LabeledPrice, LinkPreviewOptions, Message, PreCheckoutQuery
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()

# Интегрирован ваш токен бота (но вы можете переопределить через .env при желании)
BOT_TOKEN = os.getenv("BOT_TOKEN", "9050961448:AAFN1hq6IsNorOU-rKUq7pHrj75p6_K4MF0")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
AI_KEY = os.getenv("ANTHROPIC_API_KEY")      # необязательно: включает AI-фильтр

# Админы: бесплатный поиск и без лимита тем. Можно по @username и/или по числовому ID.
ADMIN_USERNAMES = {u.strip().lstrip("@").lower()
                   for u in os.getenv("ADMIN_USERNAMES", "MKdostavshik,stodesyatiy").split(",") if u.strip()}
ADMIN_IDS = {int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip().isdigit()}

if not (BOT_TOKEN and API_ID and API_HASH):
    sys.exit("Заполни API_ID и API_HASH в файле .env (API_ID/HASH получаем на my.telegram.org)")

FIND_PRICE = 25      # Telegram Stars за один /find
MAX_HITS = 20        # максимум результатов за один /find
MAX_WATCH = 3        # тем мониторинга на пользователя
NOPREVIEW = LinkPreviewOptions(is_disabled=True)

user = TelegramClient("user_session", int(API_ID), API_HASH)  # аккаунт-«глаза»
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
tasks = set()

db = sqlite3.connect("leads.db")
db.executescript("""
CREATE TABLE IF NOT EXISTS watches(owner INTEGER, topic TEXT, UNIQUE(owner, topic));
CREATE TABLE IF NOT EXISTS sent(owner INTEGER, chat INTEGER, msg INTEGER, UNIQUE(owner, chat, msg));
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER,
  topics TEXT, paid INTEGER DEFAULT 0, charge TEXT DEFAULT '');
""")

# Регулярные выражения адаптированы под бьюти-сферу и салоны красоты
BUY = re.compile(
    r"ищу мастера|ищу салон|нужен мастер|нужна маникюрша|посоветуйте салон|посоветуйте мастера|"
    r"где сделать|где подстричься|где покрасить|хороший салон|хороший мастер|"
    r"записаться на|свободное окошко|свободные окошки|кто принимает|есть окошко|"
    r"педикюр|маникюр|стрижка|окрашивание|бровист|ресницы|эпиляция|массаж|косметолог",
    re.I)
SELL = re.compile(
    r"я мастер|предлагаю услуги|наши цены|прайс-лист|акция на услуги|скидка на первое посещение|"
    r"приглашаю в салон|наш салон находится|открыта запись к нам|ждем вас|#реклама|реклама",
    re.I)


def is_admin(u):
    return u.id in ADMIN_IDS or (u.username or "").lower() in ADMIN_USERNAMES


def parse_topics(s):
    return [t.strip().lower() for t in s.split(",") if t.strip()]


def stem(w):
    return w[:max(4, len(w) - 2)] if len(w) > 4 else w


def topic_match(text, topic):
    t = text.lower()
    return all(stem(w) in t for w in topic.split())


def is_buyer(text):
    return bool(BUY.search(text)) and not SELL.search(text) and len(text) < 1500


def build_queries(topic):
    return [f"ищу {topic}", f"где сделать {topic}", f"посоветуйте {topic}",
            f"мастер {topic}", f"салон {topic}", f"записаться {topic}",
            f"нужен {topic}", f"свободное окошко {topic}"]


def link(chat, msg_id):
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{msg_id}"
    return f"https://t.me/c/{chat.id}/{msg_id}"


async def ai_is_lead(text, topic):
    """AI-фильтр: отсекает рекламу и салоны-конкуренты. Без ключа пропускает всё."""
    if not AI_KEY:
        return True
    prompt = (f"Сообщение из Telegram-чата. Это реальный клиент, который ищет услугу "
              f"«{topic}» для себя в салоне красоты? Салоны, предлагающие свои услуги, реклама "
              f"и мастера по поиску работы — НЕТ. Ответь одним словом: ДА или НЕТ.\n\n{text[:800]}")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Authorization": f"Bearer {AI_KEY}", "anthropic-version": "2026-10-21"},
                json={"model": "claude-haiku-5-5", "max_tokens": 5,
                      "messages": [{"role": "user", "content": prompt}]})
        return "ДА" in r.json()["content"][0]["text"].upper()
    except Exception:
        logging.exception("AI filter failed")
        return True


async def format_hit(msg, chat):
    sender = None
    try:
        sender = await msg.get_sender()
    except Exception:
        pass
    who = f"@{sender.username}" if sender and getattr(sender, "username", None) else "профиль скрыт"
    title = getattr(chat, "title", "чат")
    return f"💅 {title}\n👤 {who}\n\n{msg.raw_text.strip()[:400]}\n\n🔗 {link(chat, msg.id)}"


# ---------- поиск ----------
async def run_find(chat_id, topics, user_id=None, charge=None):
    since = datetime.now(timezone.utc) - timedelta(days=30)
    seen, hits, errors = set(), 0, 0
    for topic in topics:
        for q in build_queries(topic):
            if hits >= MAX_HITS:
                break
            try:
                async for m in user.iter_messages(None, search=q, limit=40):
                    key = (m.chat_id, m.id)
                    text = m.raw_text or ""
                    if key in seen or m.date < since or len(text) < 15:
                        continue
                    if not (topic_match(text, topic) and is_buyer(text)):
                        continue
                    seen.add(key)
                    if not await ai_is_lead(text, topic):
                        continue
                    chat = await m.get_chat()
                    await bot.send_message(chat_id, await format_hit(m, chat),
                                           link_preview_options=NOPREVIEW)
                    hits += 1
                    if hits >= MAX_HITS:
                        break
            except Exception as ex:
                errors += 1
                logging.warning("search failed: %s", ex)
                await asyncio.sleep(5)
            await asyncio.sleep(2)
    if hits == 0 and errors and charge:
        try:
            await bot.refund_star_payment(user_id, charge)
            return await bot.send_message(chat_id, "Поиск не удался из-за ошибки Telegram. Звёзды возвращены.")
        except Exception:
            logging.exception("refund failed")
    await bot.send_message(chat_id, f"Готово, нашёл клиентов: {hits}. Для постоянного потока: /watch {', '.join(topics)}")


def spawn(coro):
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)


# ---------- команды ----------
@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "🌸 Я ищу в Telegram клиентов для салонов красоты и бьюти-мастеров.\n\n"
        f"/find маникюр, стрижка — разовый поиск за 30 дней ({FIND_PRICE} ⭐)\n"
        f"/watch массаж — присылать новые запросы сразу (до {MAX_WATCH} тем)\n"
        "/unwatch массаж — остановить мониторинг\n"
        "/list — мои активные темы\n\n"
        "Услуги пишите через запятую.")


@dp.message(Command("find"))
async def find_cmd(m: Message, command: CommandObject):
    topics = parse_topics(command.args or "")
    if not topics:
        return await m.answer("Пример: /find маникюр, педикюр, брови")
    if is_admin(m.from_user):
        await m.answer("Ищу клиентов, это займёт 1–3 минуты...")
        return spawn(run_find(m.chat.id, topics))
    cur = db.execute("INSERT INTO orders(user, topics) VALUES(?,?)",
                     (m.from_user.id, ",".join(topics)))
    db.commit()
    await m.answer_invoice(
        title="Поиск клиентов для салона",
        description=f"Поиск клиентов в Telegram: {', '.join(topics)}"[:250],
        payload=str(cur.lastrowid), currency="XTR",
        prices=[LabeledPrice(label="Поиск", amount=FIND_PRICE)])


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = db.execute("SELECT 1 FROM orders WHERE id=? AND paid=0", (q.invoice_payload,)).fetchone()
    await q.answer(ok=bool(ok), error_message=None if ok else "Заказ не найден или уже оплачен")


@dp.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    oid = int(sp.invoice_payload)
    row = db.execute("SELECT topics FROM orders WHERE id=?", (oid,)).fetchone()
    db.execute("UPDATE orders SET paid=1, charge=? WHERE id=?", (sp.telegram_payment_charge_id, oid))
    db.commit()
    await m.answer("Оплата получена ✅ Ищу клиентов, это займёт 1–3 минуты...")
    spawn(run_find(m.chat.id, parse_topics(row[0]), m.from_user.id, sp.telegram_payment_charge_id))


@dp.message(Command("watch"))
async def watch(m: Message, command: CommandObject):
    topics = parse_topics(command.args or "")
    if not topics:
        return await m.answer("Пример: /watch маникюр")
    have = db.execute("SELECT COUNT(*) FROM watches WHERE owner=?", (m.from_user.id,)).fetchone()[0]
    if not is_admin(m.from_user) and have + len(topics) > MAX_WATCH:
        return await m.answer(f"Максимум {MAX_WATCH} темы мониторинга. Удалите лишние: /unwatch тема")
    for t in topics:
        db.execute("INSERT OR IGNORE INTO watches VALUES(?,?)", (m.from_user.id, t))
    db.commit()
    await m.answer("Слежу за новыми запросами по темам: " + ", ".join(topics))


@dp.message(Command("unwatch"))
async def unwatch(m: Message, command: CommandObject):
    for t in parse_topics(command.args or ""):
        db.execute("DELETE FROM watches WHERE owner=? AND topic=?", (m.from_user.id, t))
    db.commit()
    await m.answer("Убрал из мониторинга.")


@dp.message(Command("list"))
async def list_(m: Message):
    rows = db.execute("SELECT topic FROM watches WHERE owner=?", (m.from_user.id,)).fetchall()
    await m.answer("\n".join(r[0] for r in rows) or "Пусто. Добавьте тему: /watch маникюр")


# ---------- мониторинг чатов в реальном времени ----------
@user.on(events.NewMessage(incoming=True))
async def monitor(e):
    if e.is_private:
        return
    text = e.raw_text or ""
    if len(text) < 15 or not is_buyer(text):
        return
    for owner, topic in db.execute("SELECT owner, topic FROM watches").fetchall():
        if not topic_match(text, topic):
            continue
        try:
            db.execute("INSERT INTO sent VALUES(?,?,?)", (owner, e.chat_id, e.id))
        except sqlite3.IntegrityError:
            continue
        db.commit()
        if not await ai_is_lead(text, topic):
            continue
        chat = await e.get_chat()
        await bot.send_message(owner, f"🔥 Новый клиент на «{topic}»!\n\n" + await format_hit(e.message, chat),
                               link_preview_options=NOPREVIEW)


async def main():
    logging.basicConfig(level=logging.INFO)
    await user.start()
    await asyncio.gather(user.run_until_disconnected(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
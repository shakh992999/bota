import os
import json
import time
import asyncio
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime

from dotenv import load_dotenv
from groq import Groq
from telethon import TelegramClient, events


load_dotenv()

API_ID = int(os.getenv("TG_API_ID", "39553573"))
API_HASH = os.getenv("TG_API_HASH", "21b65b49999ccab2e9a167c54d8ca26c")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

SESSION_NAME = "userbot_session"
STATE_FILE = Path("userbot_state.json")

AI_COOLDOWN_SECONDS = 8
BUSY_COOLDOWN_SECONDS = 300

# Spamdan himoya uchun minimal interval
MIN_GROUP_INTERVAL_SECONDS = 900  # 15 minut

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
groq_client = Groq(api_key=GROQ_API_KEY)

me_id = None

last_ai_reply = defaultdict(float)
last_busy_reply = defaultdict(float)

chat_memory = defaultdict(lambda: deque(maxlen=8))
group_setup_sessions = {}
group_tasks = {}


DEFAULT_STATE = {
    "private_ai_chats": [],
    "group_ai_chats": [],
    "busy_mode": False,
    "group_jobs": {},
}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return DEFAULT_STATE.copy()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        return {
            "private_ai_chats": data.get("private_ai_chats", []),
            "group_ai_chats": data.get("group_ai_chats", []),
            "busy_mode": data.get("busy_mode", False),
            "group_jobs": data.get("group_jobs", {}),
        }

    except Exception:
        return DEFAULT_STATE.copy()


state = load_state()

private_ai_chats = set(state["private_ai_chats"])
group_ai_chats = set(state["group_ai_chats"])
busy_mode = state["busy_mode"]
group_jobs = state["group_jobs"]


def save_state() -> None:
    data = {
        "private_ai_chats": list(private_ai_chats),
        "group_ai_chats": list(group_ai_chats),
        "busy_mode": busy_mode,
        "group_jobs": group_jobs,
    }

    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


HELP_TEXT = """
╭────────────────────────╮
│      🤖 USERBOT AI      │
╰────────────────────────╯

📌 ASOSIY BUYRUQLAR

.help
➜ Yordam menyusi

.status
➜ Bot holatini ko‘rish

.onai
➜ Shaxsiy chatda AI yoqish

.offai
➜ Shaxsiy chatda AI o‘chirish

.chatai
➜ Guruh/chatda AI yoqish

.chatoff
➜ Guruh/chatda AI o‘chirish

.busyon
➜ Bandman rejimini yoqish

.busyoff
➜ Bandman rejimini o‘chirish


🖼 SAQLASH

.save
➜ Reply qilingan rasm/xabarni Saved Messages ga saqlaydi


📢 GROUP AUTO-POST

.group
➜ Guruhga avtomatik xabar yuborishni sozlash

.groups
➜ Aktiv auto-post ro‘yxati

.groupstop ID
➜ Auto-postni to‘xtatish


⚙️ QOIDA

• Guruh AI faqat sizning xabaringizga reply bo‘lsa javob beradi.
• .group minimal interval: 15 minut.
• Spam uchun ishlatmang.
"""


def make_status_text() -> str:
    return f"""
╭────────────────────╮
│    📊 USERBOT STATUS    │
╰────────────────────╯

🧠 Shaxsiy AI chatlar: {len(private_ai_chats)}
💬 Guruh AI chatlar: {len(group_ai_chats)}
⏳ Bandman rejimi: {"✅ Yoqilgan" if busy_mode else "❌ O‘chirilgan"}
📢 Auto-post vazifalar: {len(group_jobs)}

⚙️ AI cooldown: {AI_COOLDOWN_SECONDS} soniya
⚙️ Busy cooldown: {BUSY_COOLDOWN_SECONDS} soniya
⚙️ Group minimal interval: {MIN_GROUP_INTERVAL_SECONDS // 60} minut
"""


def is_command(text: str) -> bool:
    return text.strip().startswith(".")


def is_valid_text(text: str) -> bool:
    return bool(text and text.strip())


def normalize_interval_minutes(text: str) -> int:
    try:
        minutes = int(text.strip())
    except ValueError:
        raise ValueError("Vaqt faqat raqam bo‘lishi kerak. Masalan: 30")

    min_minutes = MIN_GROUP_INTERVAL_SECONDS // 60

    if minutes < min_minutes:
        raise ValueError(f"Minimal vaqt {min_minutes} minut bo‘lishi kerak.")

    return minutes


def make_job_id() -> str:
    return str(int(time.time()))


def ask_groq(message_text: str, mode: str = "normal", chat_id=None) -> str:
    history_text = ""

    if chat_id is not None and mode == "normal":
        messages = list(chat_memory[chat_id])
        if messages:
            history_text = "\n".join(messages[-6:])

    if mode == "busy":
        system_content = """
Siz Telegram userbot uchun bandman rejimi yordamchisisiz.
Javob qisqa, hurmatli, tabiiy va o‘zbek tilida bo‘lsin.
Yolg‘on va’da bermang.
"""
        user_content = f"""
Kelgan xabar:
{message_text}

Qisqa auto-reply yozing:
- foydalanuvchi hozir band;
- imkon bo‘lsa keyinroq javob beradi;
- ohang hurmatli bo‘lsin.
"""
        max_tokens = 120

    else:
        system_content = """
Siz Telegram uchun aqlli AI yordamchisiz.
O‘zbek tilida javob bering.
Javob aniq, foydali, tabiiy va qisqa bo‘lsin.
Keraksiz uzun gapirmang.
Texnik savol bo‘lsa, amaliy yechim bering.
"""
        user_content = f"""
Oldingi qisqa kontekst:
{history_text}

Yangi xabar:
{message_text}
"""
        max_tokens = 700

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
        temperature=0.45,
        max_tokens=max_tokens,
    )

    answer = response.choices[0].message.content

    if not answer:
        raise ValueError("Groq bo‘sh javob qaytardi.")

    return answer.strip()


async def generate_ai_reply(text: str, mode: str = "normal", chat_id=None) -> str:
    try:
        return await asyncio.to_thread(ask_groq, text, mode, chat_id)

    except Exception as error:
        print(f"[GROQ ERROR] {error}")

        if mode == "busy":
            return "Assalomu alaykum. Hozir bandman, imkon bo‘lishi bilan javob beraman."

        return "Kechirasiz, hozir AI javob bera olmadi. Birozdan keyin urinib ko‘ring."


async def group_auto_sender(job_id: str):
    while job_id in group_jobs:
        job = group_jobs[job_id]

        try:
            await client.send_message(job["target"], job["message"])

            job["last_sent"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_state()

        except Exception as error:
            print(f"[GROUP JOB ERROR] {job_id}: {error}")

        await asyncio.sleep(job["interval_seconds"])


async def start_all_group_jobs():
    for job_id in list(group_jobs.keys()):
        if job_id not in group_tasks:
            group_tasks[job_id] = asyncio.create_task(group_auto_sender(job_id))


async def stop_group_job(job_id: str):
    if job_id in group_tasks:
        group_tasks[job_id].cancel()
        group_tasks.pop(job_id, None)

    group_jobs.pop(job_id, None)
    save_state()


def make_groups_text() -> str:
    if not group_jobs:
        return "📭 Aktiv auto-post vazifa yo‘q."

    text = "📢 Aktiv auto-post vazifalar:\n\n"

    for job_id, job in group_jobs.items():
        text += (
            f"🆔 ID: `{job_id}`\n"
            f"🔗 Guruh: `{job['target']}`\n"
            f"⏱ Interval: {job['interval_seconds'] // 60} minut\n"
            f"🕒 Oxirgi yuborilgan: {job.get('last_sent', 'hali yo‘q')}\n"
            f"💬 Xabar: {job['message'][:80]}...\n\n"
        )

    return text


@client.on(events.NewMessage(outgoing=True))
async def command_handler(event):
    global busy_mode

    text = (event.raw_text or "").strip()
    chat_id = event.chat_id

    # .group bosqichli sozlash
    if chat_id in group_setup_sessions and not text.startswith("."):
        session = group_setup_sessions[chat_id]

        if session["step"] == "link":
            session["target"] = text
            session["step"] = "interval"

            await event.reply(
                "⏱ Endi necha minutda bir xabar tashlashini kiriting.\n\n"
                "Masalan: `30`\n"
                "Minimal: 15 minut"
            )
            return

        if session["step"] == "interval":
            try:
                minutes = normalize_interval_minutes(text)
            except ValueError as error:
                await event.reply(f"❌ {error}")
                return

            session["interval_seconds"] = minutes * 60
            session["step"] = "message"

            await event.reply("💬 Endi guruhga yuboriladigan xabar matnini kiriting.")
            return

        if session["step"] == "message":
            job_id = make_job_id()

            group_jobs[job_id] = {
                "target": session["target"],
                "interval_seconds": session["interval_seconds"],
                "message": text,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_sent": None,
            }

            save_state()

            group_tasks[job_id] = asyncio.create_task(group_auto_sender(job_id))
            group_setup_sessions.pop(chat_id, None)

            await event.reply(
                "✅ Auto-post ishga tushdi.\n\n"
                f"🆔 ID: `{job_id}`\n"
                f"⏱ Interval: {session['interval_seconds'] // 60} minut\n\n"
                f"To‘xtatish uchun:\n`.groupstop {job_id}`"
            )
            return

    if not is_command(text):
        return

    command = text.lower()

    if command == ".help":
        await event.edit(HELP_TEXT)
        return

    if command == ".status":
        await event.edit(make_status_text())
        return

    if command == ".save":
        if not event.is_reply:
            await event.edit("❌ `.save` ishlashi uchun rasm yoki xabarga reply qiling.")
            return

        replied = await event.get_reply_message()

        if not replied:
            await event.edit("❌ Reply qilingan xabar topilmadi.")
            return

        try:
            await client.forward_messages("me", replied)
            await event.edit("✅ Xabar Saved Messages ga saqlandi.")
        except Exception as error:
            await event.edit(f"❌ Saqlashda xatolik: `{error}`")
        return

    if command == ".onai":
        if not event.is_private:
            await event.edit("❌ `.onai` faqat shaxsiy chatda ishlaydi.")
            return

        private_ai_chats.add(chat_id)
        save_state()
        await event.edit("✅ Ushbu shaxsiy chatda AI yoqildi.")
        return

    if command == ".offai":
        private_ai_chats.discard(chat_id)
        save_state()
        await event.edit("✅ Ushbu shaxsiy chatda AI o‘chirildi.")
        return

    if command == ".chatai":
        if event.is_private:
            await event.edit("❌ `.chatai` faqat guruh yoki umumiy chatlarda ishlaydi.")
            return

        group_ai_chats.add(chat_id)
        save_state()
        await event.edit(
            "✅ Ushbu guruh/chatda AI yoqildi.\n\n"
            "📌 AI faqat sizning xabaringizga reply bo‘lsa javob beradi."
        )
        return

    if command == ".chatoff":
        group_ai_chats.discard(chat_id)
        save_state()
        await event.edit("✅ Ushbu guruh/chatda AI o‘chirildi.")
        return

    if command == ".busyon":
        busy_mode = True
        save_state()
        await event.edit("✅ Bandman rejimi yoqildi.")
        return

    if command == ".busyoff":
        busy_mode = False
        save_state()
        await event.edit("✅ Bandman rejimi o‘chirildi.")
        return

    if command == ".group":
        group_setup_sessions[chat_id] = {
            "step": "link",
            "target": None,
            "interval_seconds": None,
            "message": None,
        }

        await event.edit(
            "📢 Auto-post sozlash boshlandi.\n\n"
            "1-qadam: guruh username/linkini kiriting.\n\n"
            "Masalan:\n"
            "`@my_group`\n"
            "yoki\n"
            "`https://t.me/my_group`"
        )
        return

    if command == ".groups":
        await event.edit(make_groups_text())
        return

    if command.startswith(".groupstop"):
        parts = text.split(maxsplit=1)

        if len(parts) != 2:
            await event.edit("❌ To‘g‘ri format:\n`.groupstop ID`")
            return

        job_id = parts[1].strip()

        if job_id not in group_jobs:
            await event.edit("❌ Bunday ID topilmadi.")
            return

        await stop_group_job(job_id)
        await event.edit(f"✅ Auto-post to‘xtatildi.\n\n🆔 ID: `{job_id}`")
        return

    await event.edit("❌ Noma’lum buyruq.\n\n.help yozing.")


@client.on(events.NewMessage(incoming=True))
async def incoming_handler(event):
    chat_id = event.chat_id
    sender_id = event.sender_id
    text = event.raw_text or ""

    if not sender_id:
        return

    if not is_valid_text(text):
        return

    sender = await event.get_sender()

    if getattr(sender, "bot", False):
        return

    now = time.time()

    # Chat memory
    chat_memory[chat_id].append(f"User: {text}")

    # 1) Shaxsiy chat AI
    if event.is_private and chat_id in private_ai_chats:
        if now - last_ai_reply[chat_id] < AI_COOLDOWN_SECONDS:
            return

        reply = await generate_ai_reply(text, mode="normal", chat_id=chat_id)
        await event.reply(reply)

        chat_memory[chat_id].append(f"AI: {reply}")
        last_ai_reply[chat_id] = now
        return

    # 2) Guruh AI — faqat sizning xabaringizga reply bo‘lsa
    if not event.is_private and chat_id in group_ai_chats:
        if not event.is_reply:
            return

        replied_message = await event.get_reply_message()

        if not replied_message:
            return

        if replied_message.sender_id != me_id:
            return

        if now - last_ai_reply[chat_id] < AI_COOLDOWN_SECONDS:
            return

        reply = await generate_ai_reply(text, mode="normal", chat_id=chat_id)
        await event.reply(reply)

        chat_memory[chat_id].append(f"AI: {reply}")
        last_ai_reply[chat_id] = now
        return

    # 3) Bandman rejimi
    if busy_mode and event.is_private:
        if chat_id in private_ai_chats:
            return

        if now - last_busy_reply[sender_id] < BUSY_COOLDOWN_SECONDS:
            return

        reply = await generate_ai_reply(text, mode="busy", chat_id=chat_id)
        await event.reply(reply)

        last_busy_reply[sender_id] = now


async def main():
    global me_id

    me = await client.get_me()
    me_id = me.id

    await start_all_group_jobs()

    print("╭────────────────────────╮")
    print("│   ✅ USERBOT ISHLADI    │")
    print("╰────────────────────────╯")
    print(f"👤 Account: {me.first_name}")
    print("📌 Telegramda .help yozing.")
    print("⏳ Bot to‘xtashi uchun CTRL + C bosing.")

    await client.run_until_disconnected()


if __name__ == "__main__":
    if not API_ID:
        raise RuntimeError("TG_API_ID .env faylda noto‘g‘ri yoki topilmadi.")

    if not API_HASH:
        raise RuntimeError("TG_API_HASH .env faylda topilmadi.")

    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY .env faylda topilmadi.")

    with client:
        client.loop.run_until_complete(main())

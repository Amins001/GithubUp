import os
import re
import time
import asyncio
import subprocess
from datetime import datetime
from urllib.parse import urlparse, unquote
import httpx
from dotenv import load_dotenv
from pyrogram import Client, filters, StopTransmission
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# بارگذاری متغیرها از فایل .env
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # فرمت: username/repo

DOWNLOAD_DIR = "/tmp/tg_gh_uploads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client("github_uploader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
upload_lock = asyncio.Lock()

# متغیرهای سراسری برای مدیریت لغو و تغییر نام پویا (قبل یا حین دانلود)
cancel_requested = False
pending_custom_name: str | None = None
active_download_ext: str = ""
active_final_filename: str = ""

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}


def sanitize_filename(name: str) -> str:
    """پاکسازی نام فایل برای جلوگیری از خراب شدن لینک دانلود"""
    name = name.strip().replace("\n", "_")
    name = re.sub(r'[\\/*?:"<>|\s]+', '_', name)
    return name.strip('_') or f"file_{int(time.time())}"


def build_filename_with_ext(custom_text: str, orig_ext: str) -> str:
    """ساخت نام تمیز و اضافه کردن پسوند اصلی فایل در صورتی که کاربر پسوند ننوشته باشد"""
    clean = sanitize_filename(custom_text)
    _, ext = os.path.splitext(clean)
    if not ext and orig_ext:
        clean += orig_ext
    return clean


def resolve_initial_filename(orig_name: str, explicit_custom: str | None) -> tuple[str, str]:
    """تعیین نام اولیه فایل با بررسی نام از قبل ذخیره شده یا کپشن مجاز"""
    global pending_custom_name
    _, orig_ext = os.path.splitext(orig_name)

    # اولویت ۱: نامی که کاربر قبل از ارسال فایل در چت فرستاده است
    if pending_custom_name:
        chosen = build_filename_with_ext(pending_custom_name, orig_ext)
        pending_custom_name = None
        return chosen, orig_ext

    # اولویت ۲: کپشن مستقیم (برای فایل غیرفورواردی) یا خط دوم لینک
    if explicit_custom:
        first_line = explicit_custom.strip().splitlines()[0].strip()
        if not first_line.startswith("/") and not first_line.startswith("http"):
            chosen = build_filename_with_ext(first_line, orig_ext)
            return chosen, orig_ext

    # اولویت ۳: نام خودکار خود فایل
    return sanitize_filename(orig_name), orig_ext


def human_size(size_bytes: int) -> str:
    """تبدیل بایت به واحدهای خوانا"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


def get_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel_task")]
    ])


async def tg_progress_bar(current, total, client: Client, status_msg: Message, start_time: float, stage: str):
    """نمایش پیشرفت دانلود از تلگرام + نمایش نام فعلی + بررسی درخواست لغو"""
    global cancel_requested, active_final_filename
    if cancel_requested:
        client.stop_transmission()
        return

    now = time.time()
    last_edit = getattr(status_msg, "_last_edit", 0)
    if now - last_edit < 4 and current != total:
        return
    status_msg._last_edit = now

    percentage = current * 100 / total if total else 0
    elapsed = now - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    text = (
        f"🔄 **{stage}**\n\n"
        f"📄 نام نهایی: `{active_final_filename}`\n"
        f"📊 پیشرفت: `{percentage:.1f}%`\n"
        f"📦 حجم: `{human_size(current)}` از `{human_size(total)}`\n"
        f"🚀 سرعت: `{human_size(speed)}/s`\n"
        f"⏳ زمان باقی‌مانده: `{int(eta)} ثانیه`\n\n"
        f"💡 *(برای تغییر نام، همین الان نام جدید را تایپ و ارسال کنید)*"
    )
    try:
        await status_msg.edit_text(text, reply_markup=get_cancel_kb())
    except Exception:
        pass


async def async_file_sender(local_path: str, total_size: int, status_msg: Message, clean_filename: str):
    """خواندن غیرهمزمان فایل برای سازگاری با httpx.AsyncClient + نمایش درصد آپلود"""
    global cancel_requested
    current = 0
    start_t = time.time()
    chunk_size = 512 * 1024

    with open(local_path, "rb") as f:
        while chunk := f.read(chunk_size):
            if cancel_requested:
                raise asyncio.CancelledError("عملیات توسط کاربر لغو شد.")
            current += len(chunk)

            now = time.time()
            last_edit = getattr(status_msg, "_last_edit", 0)
            if now - last_edit >= 4 or current == total_size:
                status_msg._last_edit = now
                elapsed = now - start_t
                speed = current / elapsed if elapsed > 0 else 0
                pct = (current * 100 / total_size) if total_size else 0
                eta = (total_size - current) / speed if speed > 0 else 0
                try:
                    await status_msg.edit_text(
                        f"☁️ **در حال آپلود روی سرور گیت‌هاب...**\n\n"
                        f"📄 نام فایل: `{clean_filename}`\n"
                        f"📊 پیشرفت: `{pct:.1f}%`\n"
                        f"📦 آپلود شده: `{human_size(current)}` از `{human_size(total_size)}`\n"
                        f"🚀 سرعت: `{human_size(speed)}/s`\n"
                        f"⏳ زمان باقی‌مانده: `{int(eta)} ثانیه`",
                        reply_markup=get_cancel_kb()
                    )
                except Exception:
                    pass

            yield chunk
            await asyncio.sleep(0)


async def upload_file_to_github(local_path: str, clean_filename: str, status_msg: Message) -> tuple[int, str, str, int]:
    """ایجاد Release و آپلود استریم غیرهمزمان فایل روی گیت‌هاب"""
    global cancel_requested
    if cancel_requested:
        raise asyncio.CancelledError("عملیات توسط کاربر لغو شد.")

    file_size = os.path.getsize(local_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag_time = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:19]
    tag_name = f"v{tag_time}"
    release_title = f"{clean_filename} | {now_str}"

    await status_msg.edit_text("⚙️ در حال ایجاد Release جدید در گیت‌هاب...", reply_markup=get_cancel_kb())

    async with httpx.AsyncClient(timeout=None) as http:
        create_resp = await http.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases",
            headers=GH_HEADERS,
            json={
                "tag_name": tag_name,
                "name": release_title,
                "body": f"Uploaded via Telegram Bot on {now_str}\nFile: `{clean_filename}` ({human_size(file_size)})",
                "draft": False,
                "prerelease": False
            }
        )
        if create_resp.status_code != 201:
            raise Exception(f"خطا در ساخت Release: {create_resp.text}")

        release_data = create_resp.json()
        release_id = release_data["id"]
        upload_url = release_data["upload_url"].split("{")[0]

        try:
            upload_headers = {
                **GH_HEADERS,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file_size)
            }

            up_resp = await http.post(
                upload_url,
                params={"name": clean_filename},
                headers=upload_headers,
                content=async_file_sender(local_path, file_size, status_msg, clean_filename)
            )

            if up_resp.status_code != 201:
                raise Exception(f"خطا در آپلود فایل: {up_resp.text}")

            asset_data = up_resp.json()
            direct_link = asset_data["browser_download_url"]
            return release_id, release_title, direct_link, file_size

        except Exception as e:
            await http.delete(f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}", headers=GH_HEADERS)
            await http.delete(f"https://api.github.com/repos/{GITHUB_REPO}/git/refs/tags/{tag_name}", headers=GH_HEADERS)
            raise e


@app.on_message(filters.command(["start", "help"]) & filters.user(ADMIN_ID))
async def start_cmd(client: Client, message: Message):
    help_text = (
        "👋 **به ربات آپلودر مستقیم گیت‌هاب خوش آمدید!**\n\n"
        "📌 **راهنمای کامل استفاده و تغییر نام فایل‌ها:**\n\n"
        "1️⃣ **حالت خودکار (بدون نیاز به ورود نام):**\n"
        "فایل یا ویدیو را فوروارد/ارسال کنید یا لینک مستقیم بفرستید؛ ربات بلافاصله و بدون معطلی با نام اصلی شروع به دانلود و آپلود می‌کند. (کپشن‌های تبلیغاتی فایل‌های فوروارد شده به طور خودکار نادیده گرفته می‌شوند).\n\n"
        "2️⃣ **روش‌های تغییر نام فایل (۴ روش به انتخاب شما):**\n"
        "🔹 **قبل از ارسال فایل:** ابتدا نام دلخواه را در یک پیام متنی ساده بفرستید (مثلاً `lecture_01`) و بلافاصله بعدش فایل را فوروارد کنید یا لینک بدهید.\n"
        "🔸 **در حین دانلود (Live):** وقتی ربات در حال دانلود فایل است، هر زمان نام جدید را به صورت متن ساده بفرستید، نام فایل درجا عوض می‌شود!\n"
        "🔹 **با کپشن (فقط در آپلود مستقیم):** اگر فایلی را مستقیم (بدون فوروارد) آپلود می‌کنید، نام دلخواه را در کپشن آن بنویسید.\n"
        "🔸 **برای لینک‌های دانلود (در خط دوم):** لینک را در خط اول و نام دلخواه را در خط دوم همان پیام بنویسید:\n"
        "`https://example.com/file.mp4`\n"
        "`my_custom_name`\n\n"
        "🛠 **دستورات مدیریتی ربات:**\n"
        "• `/files` : مشاهده ۱۰ فایل آخر، تعداد دانلودها و دکمه حذف\n"
        "• `/search <نام>` : جستجو در بین تمام فایل‌های آپلود شده\n"
        "• `/cancel` : لغو فوری دانلود یا آپلود در حال انجام\n"
        "• `/update` : دریافت آخرین کد از گیت‌هاب (`git pull`) و ری‌استارت خودکار\n\n"
        "🎬 **پخش آنلاین:** لینک‌های خروجی را می‌توانید در پلیرهایی مثل **VLC** یا **MX Player** بگذارید تا بدون نیاز به دانلود کامل، آنلاین پخش شوند."
    )
    await message.reply_text(help_text, disable_web_page_preview=True)


@app.on_message(filters.command("cancel") & filters.user(ADMIN_ID))
async def cancel_cmd(client: Client, message: Message):
    global cancel_requested, pending_custom_name
    pending_custom_name = None
    if upload_lock.locked():
        cancel_requested = True
        await message.reply_text("🛑 درخواست لغو ثبت شد؛ عملیات در حال توقف و پاکسازی است...")
    else:
        await message.reply_text("ℹ️ عملیات فعالی وجود نداشت (نام ذخیره شده قبلی هم پاک شد).")


@app.on_callback_query(filters.regex(r"^cancel_task$") & filters.user(ADMIN_ID))
async def cancel_cb(client: Client, cb: CallbackQuery):
    global cancel_requested
    if upload_lock.locked():
        cancel_requested = True
        await cb.answer("🛑 درخواست لغو عملیات ارسال شد...", show_alert=True)
    else:
        await cb.answer("عملیاتی در جریان نیست.", show_alert=True)


@app.on_message(filters.command("files") & filters.user(ADMIN_ID))
async def list_files_cmd(client: Client, message: Message):
    status = await message.reply_text("🔍 در حال دریافت لیست ۱۰ فایل آخر از گیت‌هاب...")
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=10",
            headers=GH_HEADERS
        )
        if resp.status_code != 200:
            return await status.edit_text(f"❌ خطا در دریافت لیست: `{resp.status_code}`")

        releases = resp.json()
        if not releases:
            return await status.edit_text("📭 هیچ فایلی در بخش Releases یافت نشد.")

        await status.delete()
        for rel in releases:
            rel_id = rel["id"]
            title = rel["name"] or rel["tag_name"]
            assets = rel.get("assets", [])
            if assets:
                dl_url = assets[0]["browser_download_url"]
                size = human_size(assets[0]["size"])
                dl_count = assets[0].get("download_count", 0)
                text = (
                    f"📄 **{title}**\n"
                    f"📦 حجم: `{size}` | 📥 دانلود: `{dl_count} بار`\n"
                    f"🔗 `{dl_url}`"
                )
            else:
                text = f"📄 **{title}** (بدون فایل ضمیمه)"

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 حذف از گیت‌هاب", callback_data=f"del_{rel_id}")]
            ])
            await message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)


@app.on_message(filters.command("search") & filters.user(ADMIN_ID))
async def search_files_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚠️ لطفاً عبارت مورد نظر را وارد کنید.\nمثال: `/search movie`")

    query = " ".join(message.command[1:]).lower()
    status = await message.reply_text(f"🔍 در حال جستجوی `{query}` در گیت‌هاب...")

    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=100",
            headers=GH_HEADERS
        )
        if resp.status_code != 200:
            return await status.edit_text(f"❌ خطا در ارتباط با گیت‌هاب: `{resp.status_code}`")

        releases = resp.json()
        matched = []
        for rel in releases:
            title = (rel.get("name") or rel.get("tag_name") or "").lower()
            assets = rel.get("assets", [])
            asset_name = assets[0]["name"].lower() if assets else ""
            if query in title or query in asset_name:
                matched.append(rel)

        if not matched:
            return await status.edit_text(f"📭 هیچ فایلی شامل عبارت `{query}` پیدا نشد.")

        await status.delete()
        for rel in matched[:15]:
            rel_id = rel["id"]
            title = rel["name"] or rel["tag_name"]
            assets = rel.get("assets", [])
            if assets:
                dl_url = assets[0]["browser_download_url"]
                size = human_size(assets[0]["size"])
                dl_count = assets[0].get("download_count", 0)
                text = (
                    f"📄 **{title}**\n"
                    f"📦 حجم: `{size}` | 📥 دانلود: `{dl_count} بار`\n"
                    f"🔗 `{dl_url}`"
                )
            else:
                text = f"📄 **{title}**"

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 حذف از گیت‌هاب", callback_data=f"del_{rel_id}")]
            ])
            await message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)


async def process_url_download(message: Message, url: str, explicit_name: str | None):
    """تابع مشترک برای دانلود از لینک مستقیم (با دستور /url یا ارسال مستقیم لینک)"""
    global cancel_requested, active_download_ext, active_final_filename

    parsed_path = unquote(urlparse(url).path)
    orig_name = os.path.basename(parsed_path) or f"url_file_{int(time.time())}.bin"
    initial_name, orig_ext = resolve_initial_filename(orig_name, explicit_name)

    tag_time = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:19]
    local_path = os.path.join(DOWNLOAD_DIR, f"{tag_time}_{initial_name}")

    status_msg = await message.reply_text("⏳ در صف انتظار برای دانلود از لینک...")

    async with upload_lock:
        cancel_requested = False
        active_download_ext = orig_ext
        active_final_filename = initial_name

        try:
            start_t = time.time()
            await status_msg.edit_text(
                f"🌐 شروع دانلود از لینک:\n`{active_final_filename}`\n\n"
                f"💡 *(می‌توانید در حین دانلود، نام جدید را در چت بفرستید)*",
                reply_markup=get_cancel_kb()
            )

            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as http:
                async with http.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length", 0))
                    if total > 2000 * 1024 * 1024:
                        raise Exception("حجم فایل بیشتر از سقف ۲ گیگابایت گیت‌هاب است.")

                    current = 0
                    with open(local_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=1024 * 512):
                            if cancel_requested:
                                raise asyncio.CancelledError("عملیات توسط کاربر لغو شد.")
                            f.write(chunk)
                            current += len(chunk)

                            now = time.time()
                            last_edit = getattr(status_msg, "_last_edit", 0)
                            if now - last_edit >= 4:
                                status_msg._last_edit = now
                                elapsed = now - start_t
                                speed = current / elapsed if elapsed > 0 else 0
                                pct = (current * 100 / total) if total else 0
                                try:
                                    await status_msg.edit_text(
                                        f"🌐 **در حال دانلود از لینک به سرور...**\n\n"
                                        f"📄 نام نهایی: `{active_final_filename}`\n"
                                        f"📊 پیشرفت: `{pct:.1f}%`\n"
                                        f"📦 دریافت شده: `{human_size(current)}` از `{human_size(total) if total else 'نامشخص'}`\n"
                                        f"🚀 سرعت: `{human_size(speed)}/s`\n\n"
                                        f"💡 *(برای تغییر نام، همین الان نام جدید را تایپ و ارسال کنید)*",
                                        reply_markup=get_cancel_kb()
                                    )
                                except Exception:
                                    pass

            # استفاده از آخرین نام (در صورتی که کاربر حین دانلود نام را تغییر داده باشد)
            final_clean_name = active_final_filename
            release_id, release_title, direct_link, file_size = await upload_file_to_github(
                local_path, final_clean_name, status_msg
            )

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 دانلود مستقیم فایل", url=direct_link)],
                [InlineKeyboardButton("🗑 حذف این فایل از گیت‌هاب", callback_data=f"del_{release_id}")]
            ])

            await status_msg.edit_text(
                f"✅ **لینک با موفقیت در گیت‌هاب آپلود شد!**\n\n"
                f"🏷 **عنوان ریلیز:** `{release_title}`\n"
                f"📦 **حجم:** `{human_size(file_size)}`\n\n"
                f"🔗 **لینک مستقیم (به نام فایل):**\n`{direct_link}`",
                reply_markup=kb,
                disable_web_page_preview=True
            )

        except asyncio.CancelledError:
            await status_msg.edit_text("🛑 **عملیات دانلود/آپلود با موفقیت لغو شد و فایل موقت پاک گردید.**")
        except Exception as e:
            await status_msg.edit_text(f"❌ **خطا در پردازش لینک:**\n`{str(e)}`")
        finally:
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass


@app.on_message(filters.command("url") & filters.user(ADMIN_ID))
async def url_leech_cmd(client: Client, message: Message):
    text_lines = message.text.strip().splitlines()
    first_line_parts = text_lines[0].split(maxsplit=2)

    if len(first_line_parts) < 2:
        return await message.reply_text(
            "⚠️ لطفاً لینک مستقیم فایل را وارد کنید یا مستقیم لینک را بدون دستور بفرستید.\n"
            "مثال همراه با نام در خط دوم:\n"
            "`https://example.com/file.mp4`\n"
            "`my_video.mp4`"
        )

    url = first_line_parts[1].strip()
    custom_name = None
    if len( text_lines ) > 1 and text_lines[1].strip():
        custom_name = text_lines[1].strip()
    elif len(first_line_parts) > 2:
        custom_name = first_line_parts[2].strip()

    await process_url_download(message, url, custom_name)


@app.on_message(filters.command("update") & filters.user(ADMIN_ID))
async def update_bot_cmd(client: Client, message: Message):
    status = await message.reply_text("🔄 در حال دریافت آخرین نسخه کد از گیت‌هاب (`git pull`)...")
    try:
        proc = subprocess.run(["git", "pull"], capture_output=True, text=True, timeout=30)
        output = (proc.stdout + "\n" + proc.stderr).strip()

        if proc.returncode != 0:
            return await status.edit_text(f"❌ **خطا در اجرای git pull:**\n`{output}`")

        await status.edit_text(
            f"✅ **کد با موفقیت بروزرسانی شد!**\n\n"
            f"📋 خروجی:\n`{output}`\n\n"
            f"♻️ ربات در حال ری‌استارت شدن است (تا ۵ ثانیه دیگر آماده می‌شود)..."
        )
        os._exit(0)
    except Exception as e:
        await status.edit_text(f"❌ **خطا در بروزرسانی:**\n`{str(e)}`")


@app.on_message((filters.video | filters.document | filters.audio) & filters.user(ADMIN_ID))
async def handle_media(client: Client, message: Message):
    global cancel_requested, active_download_ext, active_final_filename
    media = message.video or message.document or message.audio
    file_size = media.file_size or 0

    if file_size > 2000 * 1024 * 1024:
        return await message.reply_text("❌ حجم فایل بیشتر از سقف مجاز گیت‌هاب (۲ گیگابایت) است.")

    tag_time = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:19]
    orig_name = getattr(media, "file_name", None)
    if not orig_name:
        ext = ".mp4" if message.video else (".mp3" if message.audio else ".bin")
        orig_name = f"media_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"

    # ایده ۴: اگر پیام فوروارد شده باشد، کپشن تبلیغاتی آن را نادیده بگیر
    is_forwarded = bool(message.forward_date or message.forward_from or message.forward_from_chat or message.forward_sender_name)
    allowed_caption = None if is_forwarded else message.caption

    initial_name, orig_ext = resolve_initial_filename(orig_name, allowed_caption)
    local_path = os.path.join(DOWNLOAD_DIR, f"{tag_time}_{initial_name}")

    status_msg = await message.reply_text("⏳ در صف انتظار برای پردازش...")

    async with upload_lock:
        cancel_requested = False
        active_download_ext = orig_ext
        active_final_filename = initial_name

        try:
            start_t = time.time()
            await status_msg.edit_text(
                f"📥 شروع دانلود `{active_final_filename}` از تلگرام...\n\n"
                f"💡 *(می‌توانید در حین دانلود، نام جدید را در چت بفرستید)*",
                reply_markup=get_cancel_kb()
            )
            dl_result = await message.download(
                file_name=local_path,
                progress=tg_progress_bar,
                progress_args=(client, status_msg, start_t, "در حال دانلود از تلگرام به سرور...")
            )

            if cancel_requested or not dl_result or not os.path.exists(local_path):
                raise asyncio.CancelledError("عملیات توسط کاربر لغو شد.")

            # استفاده از آخرین نام (در صورتی که کاربر در حین دانلود نام جدیدی در چت فرستاده باشد)
            final_clean_name = active_final_filename
            release_id, release_title, direct_link, final_size = await upload_file_to_github(
                local_path, final_clean_name, status_msg
            )

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 دانلود مستقیم فایل", url=direct_link)],
                [InlineKeyboardButton("🗑 حذف این فایل از گیت‌هاب", callback_data=f"del_{release_id}")]
            ])

            await status_msg.edit_text(
                f"✅ **فایل با موفقیت در گیت‌هاب آپلود شد!**\n\n"
                f"🏷 **عنوان ریلیز:** `{release_title}`\n"
                f"📦 **حجم:** `{human_size(final_size)}`\n\n"
                f"🔗 **لینک مستقیم (به نام فایل):**\n`{direct_link}`",
                reply_markup=kb,
                disable_web_page_preview=True
            )

        except (asyncio.CancelledError, StopTransmission):
            await status_msg.edit_text("🛑 **عملیات دانلود/آپلود با موفقیت لغو شد و فایل موقت از سرور پاک گردید.**")
        except Exception as e:
            await status_msg.edit_text(f"❌ **خطا در پردازش فایل:**\n`{str(e)}`")
        finally:
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass


@app.on_message(filters.text & filters.user(ADMIN_ID) & ~filters.regex(r"^/"))
async def handle_plain_text_or_links(client: Client, message: Message):
    """مدیریت هوشمند پیام‌های متنی: تشخیص لینک (خط دوم = نام) یا تنظیم نام (قبل/حین دانلود)"""
    global pending_custom_name, active_final_filename, active_download_ext

    lines = [line.strip() for line in message.text.strip().splitlines() if line.strip()]
    if not lines:
        return

    first_line = lines[0]

    # ۱. اگر خط اول لینک مستقیم باشد (ایده ۳ - روش ب: خط دوم = نام فایل)
    if first_line.startswith("http://") or first_line.startswith("https://"):
        parts = first_line.split(maxsplit=1)
        url = parts[0]
        custom_name = None
        if len(lines) > 1:
            custom_name = lines[1]
        elif len(parts) > 1:
            custom_name = parts[1]

        return await process_url_download(message, url, custom_name)

    # ۲. اگر متن معمولی باشد و همین الان فایلی در حال دانلود باشد -> تغییر نام درجا (Live Rename)
    if upload_lock.locked():
        new_name = build_filename_with_ext(first_line, active_download_ext)
        active_final_filename = new_name
        return await message.reply_text(
            f"✏️ **نام فایل در حال دانلود تغییر یافت به:**\n`{new_name}`\n\n"
            f"*(پس از پایان دانلود، با همین نام روی گیت‌هاب آپلود خواهد شد)*"
        )

    # ۳. اگر متن معمولی باشد و دانلودی در جریان نباشد -> ذخیره نام برای فایل/لینک بعدی (Pre-Name)
    pending_custom_name = sanitize_filename(first_line)
    await message.reply_text(
        f"📌 **نام `{pending_custom_name}` برای فایل بعدی ذخیره شد!**\n\n"
        f"حالا ویدیو/فایل خود را فوروارد یا ارسال کنید (یا لینک بفرستید) تا با این نام آپلود شود.\n"
        f"*(برای انصراف از این نام، دستور `/cancel` را بزنید)*"
    )


@app.on_callback_query(filters.regex(r"^del_(\d+)$") & filters.user(ADMIN_ID))
async def delete_release_cb(client: Client, cb: CallbackQuery):
    release_id = cb.matches[0].group(1)
    await cb.answer("در حال حذف از گیت‌هاب...")

    async with httpx.AsyncClient(timeout=30) as http:
        get_resp = await http.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}",
            headers=GH_HEADERS
        )
        tag_name = get_resp.json().get("tag_name") if get_resp.status_code == 200 else None

        del_resp = await http.delete(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}",
            headers=GH_HEADERS
        )

        if del_resp.status_code == 204:
            if tag_name:
                await http.delete(
                    f"https://api.github.com/repos/{GITHUB_REPO}/git/refs/tags/{tag_name}",
                    headers=GH_HEADERS
                )
            await cb.message.edit_text("🗑 **این فایل و Release مربوط به آن به طور کامل از گیت‌هاب حذف شد.**")
        else:
            await cb.answer(f"❌ خطا در حذف: {del_resp.status_code}", show_alert=True)


if __name__ == "__main__":
    print("🚀 GitHub Uploader Bot is running...")
    app.run()

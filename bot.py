import os
import re
import glob
import time
import shutil
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
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client("github_uploader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
upload_lock = asyncio.Lock()

# متغیرهای وضعیت سراسری
cancel_requested = False
pending_custom_name: str | None = None
pending_zip_password: str | None = None
awaiting_gh_rename_asset_id: int | None = None
active_download_ext: str = ""
active_final_filename: str = ""

# وضعیت حالت آپلود گروهی (Batch Mode)
batch_state = {
    "active": False,
    "title": "",
    "release_id": None,
    "upload_url": None,
    "tag_name": None,
    "files": []  # لیست تاپل‌های (name, url, size, asset_id)
}

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
    """ساخت نام تمیز و حفظ پسوند اصلی در صورت عدم ورود پسوند"""
    clean = sanitize_filename(custom_text)
    _, ext = os.path.splitext(clean)
    if not ext and orig_ext:
        clean += orig_ext
    return clean


def resolve_initial_filename(orig_name: str, explicit_custom: str | None) -> tuple[str, str]:
    """تعیین نام اولیه فایل بر اساس اولویت‌ها"""
    global pending_custom_name
    _, orig_ext = os.path.splitext(orig_name)

    if pending_custom_name:
        chosen = build_filename_with_ext(pending_custom_name, orig_ext)
        pending_custom_name = None
        return chosen, orig_ext

    if explicit_custom:
        first_line = explicit_custom.strip().splitlines()[0].strip()
        if not first_line.startswith("/") and not first_line.startswith("http"):
            chosen = build_filename_with_ext(first_line, orig_ext)
            return chosen, orig_ext

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


def get_file_manage_kb(direct_link: str, release_id: int, asset_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 دانلود مستقیم فایل", url=direct_link)],
        [
            InlineKeyboardButton("✏️ تغییر نام در گیت‌هاب", callback_data=f"ren_{asset_id}"),
            InlineKeyboardButton("🗑 حذف از گیت‌هاب", callback_data=f"del_{release_id}")
        ]
    ])


def is_ytdlp_url(url: str) -> bool:
    """تشخیص لینک‌های شبکه‌های اجتماعی برای دانلود با yt-dlp"""
    domains = [
        "youtube.com", "youtu.be", "instagram.com", "twitter.com", "x.com",
        "tiktok.com", "soundcloud.com", "twitch.tv", "vimeo.com", "reddit.com"
    ]
    host = urlparse(url).netloc.lower()
    return any(d in host for d in domains)


async def compress_with_password(input_path: str, final_filename: str, password: str, status_msg: Message) -> tuple[str, str]:
    """فشرده‌سازی فایل با پسورد توسط 7z بدون فشرده‌سازی سنگین پردازنده (-mx=1)"""
    base_name, _ = os.path.splitext(final_filename)
    archive_name = f"{base_name}.7z"
    archive_path = f"{input_path}.7z"

    await status_msg.edit_text(
        f"🔐 **در حال رمزگذاری و فشرده‌سازی فایل...**\n\n"
        f"📦 فایل خروجی: `{archive_name}`\n"
        f"🔑 رمز عبور: `{password}`",
        reply_markup=get_cancel_kb()
    )

    # کپی یا تغییر نام موقت تا فایل داخل آرشیو نام تمیزی داشته باشد
    clean_inside_path = os.path.join(os.path.dirname(input_path), final_filename)
    if input_path != clean_inside_path:
        shutil.move(input_path, clean_inside_path)

    proc = await asyncio.create_subprocess_exec(
        "7z", "a", "-t7z", "-mx=1", "-mhe=on", f"-p{password}", archive_path, clean_inside_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()

    if os.path.exists(clean_inside_path):
        os.remove(clean_inside_path)

    if proc.returncode != 0 or not os.path.exists(archive_path):
        raise Exception(f"خطا در فشرده‌سازی 7z: {stderr.decode('utf-8', errors='ignore')}")

    return archive_path, archive_name


async def tg_progress_bar(current, total, client: Client, status_msg: Message, start_time: float, stage: str):
    """نمایش پیشرفت دانلود از تلگرام"""
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

    zip_note = f"\n🔐 رمز آرشیو: `{pending_zip_password}`" if pending_zip_password else ""
    batch_note = f"\n📚 حالت گروهی: `{batch_state['title']}`" if batch_state["active"] else ""

    text = (
        f"🔄 **{stage}**\n\n"
        f"📄 نام نهایی: `{active_final_filename}`{zip_note}{batch_note}\n"
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
    """خواندن غیرهمزمان فایل برای آپلود به گیت‌هاب"""
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


async def ensure_batch_release(http: httpx.AsyncClient, status_msg: Message):
    """اگر حالت گروهی فعال است و هنوز Release ساخته نشده، آن را بسازد"""
    if batch_state["release_id"] and batch_state["upload_url"]:
        return batch_state["release_id"], batch_state["upload_url"], batch_state["tag_name"]

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag_time = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag_name = f"batch-{tag_time}"
    release_title = f"📦 {batch_state['title']} | {now_str}"

    await status_msg.edit_text("⚙️ در حال ایجاد Release گروهی در گیت‌هاب...", reply_markup=get_cancel_kb())
    create_resp = await http.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/releases",
        headers=GH_HEADERS,
        json={
            "tag_name": tag_name,
            "name": release_title,
            "body": f"Batch Release created on {now_str}\nTitle: **{batch_state['title']}**",
            "draft": False,
            "prerelease": False
        }
    )
    if create_resp.status_code != 201:
        raise Exception(f"خطا در ساخت Release گروهی: {create_resp.text}")

    data = create_resp.json()
    batch_state["release_id"] = data["id"]
    batch_state["upload_url"] = data["upload_url"].split("{")[0]
    batch_state["tag_name"] = tag_name
    return batch_state["release_id"], batch_state["upload_url"], tag_name


async def upload_file_to_github(local_path: str, clean_filename: str, status_msg: Message) -> tuple[int, int, str, str, int]:
    """ایجاد یا استفاده از Release و آپلود فایل روی گیت‌هاب"""
    global cancel_requested
    if cancel_requested:
        raise asyncio.CancelledError("عملیات توسط کاربر لغو شد.")

    file_size = os.path.getsize(local_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag_time = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:19]

    async with httpx.AsyncClient(timeout=None) as http:
        is_batch = batch_state["active"]
        if is_batch:
            release_id, upload_url, tag_name = await ensure_batch_release(http, status_msg)
            release_title = f"📦 {batch_state['title']}"
            # در حالت گروهی اگر دو فایل همنام باشند، پیشوند زمان اضافه کن تا خطای تکراری ندهد
            existing_names = [f[0] for f in batch_state["files"]]
            if clean_filename in existing_names:
                clean_filename = f"{int(time.time())}_{clean_filename}"
        else:
            tag_name = f"v{tag_time}"
            release_title = f"{clean_filename} | {now_str}"
            await status_msg.edit_text("⚙️ در حال ایجاد Release جدید در گیت‌هاب...", reply_markup=get_cancel_kb())
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
            asset_id = asset_data["id"]
            direct_link = asset_data["browser_download_url"]

            if is_batch:
                batch_state["files"].append((clean_filename, direct_link, file_size, asset_id))

            return release_id, asset_id, release_title, direct_link, file_size

        except Exception as e:
            if not is_batch:
                await http.delete(f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}", headers=GH_HEADERS)
                await http.delete(f"https://api.github.com/repos/{GITHUB_REPO}/git/refs/tags/{tag_name}", headers=GH_HEADERS)
            raise e


async def finalize_and_upload(local_path: str, status_msg: Message):
    """اعمال فشرده‌سازی رمزدار (در صورت فعال بودن) و آپلود نهایی به گیت‌هاب"""
    global pending_zip_password, active_final_filename

    final_clean_name = active_final_filename
    used_password = pending_zip_password
    pending_zip_password = None

    if used_password:
        local_path, final_clean_name = await compress_with_password(
            local_path, final_clean_name, used_password, status_msg
        )

    release_id, asset_id, release_title, direct_link, final_size = await upload_file_to_github(
        local_path, final_clean_name, status_msg
    )

    pw_text = f"\n🔐 **رمز فایل فشرده:** `{used_password}`" if used_password else ""
    batch_text = (
        f"\n\n📚 **فایل شماره {len(batch_state['files'])} به ریلیز گروهی اضافه شد!**\n"
        f"*(فایل بعدی را بفرستید یا برای پایان دستور `/done` را بزنید)*"
    ) if batch_state["active"] else ""

    await status_msg.edit_text(
        f"✅ **فایل با موفقیت در گیت‌هاب آپلود شد!**\n\n"
        f"🏷 **ریلیز:** `{release_title}`\n"
        f"📄 **نام فایل:** `{final_clean_name}`\n"
        f"📦 **حجم:** `{human_size(final_size)}`{pw_text}\n\n"
        f"🔗 **لینک مستقیم:**\n`{direct_link}`{batch_text}",
        reply_markup=get_file_manage_kb(direct_link, release_id, asset_id),
        disable_web_page_preview=True
    )


@app.on_message(filters.command(["start", "help"]) & filters.user(ADMIN_ID))
async def start_cmd(client: Client, message: Message):
    help_text = (
        "👋 **به ربات پیشرفته آپلودر گیت‌هاب خوش آمدید!**\n\n"
        "📌 **۱. روش‌های آپلود (تلگرام، لینک مستقیم، مگا، درایو، یوتیوب/توییتر):**\n"
        "• هر فایل یا ویدیویی را بفرستید یا فوروارد کنید.\n"
        "• هر لینکی (مستقیم، **Mega.nz**، **Google Drive**، توییتر، تیک‌تاک، یوتیوب و...) را مستقیم در چت بفرستید تا دانلود و روی گیت‌هاب آپلود شود.\n\n"
        "✏️ **۲. روش‌های تغییر نام فایل (۵ روش):**\n"
        "• **قبل از ارسال:** نام را در یک پیام متنی ساده بفرستید و بعد فایل/لینک را ارسال کنید.\n"
        "• **در حین دانلود:** وقتی فایل در حال دانلود است، نام جدید را در چت بنویسید.\n"
        "• **بعد از آپلود در گیت‌هاب:** روی دکمه `✏️ تغییر نام در گیت‌هاب` زیر پیام فایل کلیک کنید (بدون نیاز به آپلود مجدد!).\n"
        "• **در خط دوم لینک:** خط اول لینک و خط دوم نام دلخواه.\n"
        "• **با کپشن:** در آپلود مستقیم (غیرفورواردی) نام را در کپشن بنویسید.\n\n"
        "📦 **۳. آپلود گروهی چند فایل در یک ریلیز (`/batch`):**\n"
        "• `/batch نام_مجموعه` : شروع حالت گروهی (همه فایل‌های بعدی در یک Release واحد قرار می‌گیرند).\n"
        "• `/done` : پایان حالت گروهی و دریافت لیست یکجای تمام لینک‌ها.\n\n"
        "🔐 **۴. فشرده‌سازی و رمزگذاری روی فایل‌ها (`/zip`):**\n"
        "• `/zip 12345` : قبل از ارسال فایل یا لینک این دستور را بزنید تا ربات فایل را با رمز `12345` به `.7z` تبدیل و آپلود کند.\n\n"
        "🛠 **۵. سایر دستورات مدیریتی:**\n"
        "• `/files` : مشاهده ۱۰ ریلیز آخر، ویرایش نام و حذف\n"
        "• `/search <نام>` : جستجو در تمام فایل‌های گیت‌هاب\n"
        "• `/cookies` : ریپلای روی فایل `cookies.txt` برای دانلود از یوتیوب/اینستاگرام\n"
        "• `/cancel` : لغو فوری عملیات فعلی\n"
        "• `/update` : آپدیت خودکار کد از گیت‌هاب و ری‌استارت"
    )
    await message.reply_text(help_text, disable_web_page_preview=True)


@app.on_message(filters.command("batch") & filters.user(ADMIN_ID))
async def batch_start_cmd(client: Client, message: Message):
    title = " ".join(message.command[1:]).strip() or f"Batch_{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
    batch_state["active"] = True
    batch_state["title"] = title
    batch_state["release_id"] = None
    batch_state["upload_url"] = None
    batch_state["tag_name"] = None
    batch_state["files"] = []

    await message.reply_text(
        f"📚 **حالت آپلود گروهی (Batch Mode) فعال شد!**\n\n"
        f"🏷 عنوان مجموعه: `{title}`\n\n"
        f"حالا تمام فایل‌ها یا لینک‌های خود را یکی‌یکی یا با هم بفرستید؛ همگی داخل **یک Release واحد** در گیت‌هاب آپلود می‌شوند.\n"
        f"✅ هر زمان کارتان تمام شد، دستور `/done` را بزنید."
    )


@app.on_message(filters.command("done") & filters.user(ADMIN_ID))
async def batch_done_cmd(client: Client, message: Message):
    if not batch_state["active"]:
        return await message.reply_text("ℹ️ حالت آپلود گروهی فعال نیست. برای شروع از `/batch نام` استفاده کنید.")

    files = batch_state["files"]
    title = batch_state["title"]
    rel_id = batch_state["release_id"]

    batch_state["active"] = False
    batch_state["files"] = []

    if not files:
        return await message.reply_text("📭 حالت گروهی بسته شد (هیچ فایلی آپلود نشده بود).")

    lines = [f"🎉 **آپلود گروهی «{title}» به پایان رسید!**\n"]
    total_bytes = 0
    for idx, (fname, url, fsize, _) in enumerate(files, 1):
        total_bytes += fsize
        lines.append(f"{idx}. 📄 `{fname}` ({human_size(fsize)})\n🔗 `{url}`\n")

    lines.append(f"📦 **مجموع کل:** `{len(files)} فایل` | `{human_size(total_bytes)}`")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 حذف کل این Release گروهی", callback_data=f"del_{rel_id}")]
    ])
    await message.reply_text("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)


@app.on_message(filters.command("zip") & filters.user(ADMIN_ID))
async def zip_password_cmd(client: Client, message: Message):
    global pending_zip_password
    if len(message.command) < 2:
        pending_zip_password = None
        return await message.reply_text(
            "🔓 رمزگذاری غیرفعال شد.\n"
            "برای رمزگذاری فایل بعدی، رمز را جلوی دستور بنویسید:\n"
            "`/zip MySecret123`"
        )

    pending_zip_password = message.command[1].strip()
    await message.reply_text(
        f"🔐 **رمز عبور `{pending_zip_password}` برای فایل بعدی تنظیم شد!**\n\n"
        f"حالا فایل یا لینک خود را بفرستید؛ ربات پس از دانلود آن را داخل آرشیو رمزدار `.7z` قرار داده و سپس در گیت‌هاب آپلود می‌کند."
    )


@app.on_message(filters.command("cookies") & filters.user(ADMIN_ID))
async def save_cookies_cmd(client: Client, message: Message):
    if not message.reply_to_message or not message.reply_to_message.document:
        return await message.reply_text(
            "🍪 برای تنظیم کوکی یوتیوب/اینستاگرام، فایل `cookies.txt` (با فرمت Netscape) را در چت بفرستید و روی آن `/cookies` را ریپلای کنید."
        )
    await message.reply_to_message.download(file_name=COOKIES_FILE)
    await message.reply_text("✅ فایل `cookies.txt` با موفقیت ذخیره شد و در دانلودهای `yt-dlp` استفاده خواهد شد.")


@app.on_message(filters.command("cancel") & filters.user(ADMIN_ID))
async def cancel_cmd(client: Client, message: Message):
    global cancel_requested, pending_custom_name, pending_zip_password, awaiting_gh_rename_asset_id
    pending_custom_name = None
    pending_zip_password = None
    awaiting_gh_rename_asset_id = None

    if upload_lock.locked():
        cancel_requested = True
        await message.reply_text("🛑 درخواست لغو ثبت شد؛ عملیات در حال توقف و پاکسازی است...")
    else:
        await message.reply_text("ℹ️ عملیات و وضعیت‌های موقت پاکسازی شدند.")


@app.on_callback_query(filters.regex(r"^cancel_task$") & filters.user(ADMIN_ID))
async def cancel_cb(client: Client, cb: CallbackQuery):
    global cancel_requested
    if upload_lock.locked():
        cancel_requested = True
        await cb.answer("🛑 درخواست لغو عملیات ارسال شد...", show_alert=True)
    else:
        await cb.answer("عملیاتی در جریان نیست.", show_alert=True)


@app.on_callback_query(filters.regex(r"^ren_(\d+)$") & filters.user(ADMIN_ID))
async def rename_gh_asset_cb(client: Client, cb: CallbackQuery):
    global awaiting_gh_rename_asset_id
    asset_id = int(cb.matches[0].group(1))
    awaiting_gh_rename_asset_id = asset_id
    await cb.answer()
    await cb.message.reply_text(
        f"✏️ **تغییر نام مستقیم در گیت‌هاب (Asset ID: `{asset_id}`)**\n\n"
        f"لطفاً نام جدید فایل را (همراه با پسوند، مثلاً `movie_01.mp4`) همین الان ارسال کنید:\n"
        f"*(برای انصراف `/cancel` را بزنید)*"
    )


@app.on_message(filters.command("files") & filters.user(ADMIN_ID))
async def list_files_cmd(client: Client, message: Message):
    status = await message.reply_text("🔍 در حال دریافت لیست ۱۰ ریلیز آخر از گیت‌هاب...")
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

            if len(assets) == 1:
                a = assets[0]
                text = (
                    f"📄 **{title}**\n"
                    f"📦 حجم: `{human_size(a['size'])}` | 📥 دانلود: `{a.get('download_count', 0)} بار`\n"
                    f"🔗 `{a['browser_download_url']}`"
                )
                kb = get_file_manage_kb(a["browser_download_url"], rel_id, a["id"])
            elif len(assets) > 1:
                lines = [f"📚 **{title}** ({len(assets)} فایل):"]
                buttons = []
                for idx, a in enumerate(assets, 1):
                    lines.append(
                        f"{idx}. `{a['name']}` ({human_size(a['size'])} | 📥 {a.get('download_count', 0)})\n"
                        f"🔗 `{a['browser_download_url']}`"
                    )
                    buttons.append([InlineKeyboardButton(f"✏️ تغییر نام فایل {idx}", callback_data=f"ren_{a['id']}")])
                buttons.append([InlineKeyboardButton("🗑 حذف کل این Release", callback_data=f"del_{rel_id}")])
                text = "\n".join(lines)
                kb = InlineKeyboardMarkup(buttons)
            else:
                text = f"📄 **{title}** (بدون فایل ضمیمه)"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 حذف از گیت‌هاب", callback_data=f"del_{rel_id}")]])

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
            if query in title or any(query in a["name"].lower() for a in assets):
                matched.append(rel)

        if not matched:
            return await status.edit_text(f"📭 هیچ فایلی شامل عبارت `{query}` پیدا نشد.")

        await status.delete()
        for rel in matched[:12]:
            rel_id = rel["id"]
            title = rel["name"] or rel["tag_name"]
            assets = rel.get("assets", [])
            if assets:
                a = assets[0]
                text = (
                    f"📄 **{title}**\n"
                    f"📦 حجم: `{human_size(a['size'])}` | 📥 دانلود: `{a.get('download_count', 0)} بار`\n"
                    f"🔗 `{a['browser_download_url']}`"
                )
                kb = get_file_manage_kb(a["browser_download_url"], rel_id, a["id"])
            else:
                text = f"📄 **{title}**"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 حذف از گیت‌هاب", callback_data=f"del_{rel_id}")]])

            await message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)


async def download_external_tool(cmd_list: list[str], work_dir: str, status_msg: Message, label: str) -> str:
    """اجرای غیرهمزمان ابزارهای دانلود خارجی (megadl, gdown, yt-dlp) با قابلیت لغو"""
    global cancel_requested
    proc = await asyncio.create_subprocess_exec(
        *cmd_list,
        cwd=work_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    start_t = time.time()
    while proc.returncode is None:
        if cancel_requested:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
            raise asyncio.CancelledError("عملیات توسط کاربر لغو شد.")

        await asyncio.sleep(2)
        now = time.time()
        if now - getattr(status_msg, "_last_edit", 0) >= 4:
            status_msg._last_edit = now
            # بررسی حجم فایل در حال دانلود در پوشه موقت
            files = glob.glob(os.path.join(work_dir, "*"))
            cur_size = sum(os.path.getsize(f) for f in files if os.path.isfile(f))
            elapsed = int(now - start_t)
            try:
                await status_msg.edit_text(
                    f"🌐 **{label}**\n\n"
                    f"📄 نام نهایی: `{active_final_filename}`\n"
                    f"📦 حجم دریافت شده تا الان: `{human_size(cur_size)}`\n"
                    f"⏱ زمان سپری شده: `{elapsed} ثانیه`\n\n"
                    f"💡 *(برای تغییر نام، همین الان نام جدید را در چت بفرستید)*",
                    reply_markup=get_cancel_kb()
                )
            except Exception:
                pass

    out, _ = await proc.communicate()
    if proc.returncode != 0:
        err_tail = out.decode("utf-8", errors="ignore")[-400:]
        raise Exception(f"خطا در دانلود ({label}):\n{err_tail}")

    downloaded = [
        os.path.join(work_dir, f) for f in os.listdir(work_dir)
        if os.path.isfile(os.path.join(work_dir, f)) and not f.endswith(".part")
    ]
    if not downloaded:
        raise Exception("فایلی پس از اتمام دانلود یافت نشد.")

    # انتخاب بزرگترین فایل دانلود شده در پوشه موقت
    downloaded.sort(key=lambda x: os.path.getsize(x), reverse=True)
    return downloaded[0]


async def process_url_download(message: Message, url: str, explicit_name: str | None):
    """مدیریت هوشمند دانلود از انواع لینک‌ها (Mega.nz، Google Drive، yt-dlp و لینک مستقیم)"""
    global cancel_requested, active_download_ext, active_final_filename

    status_msg = await message.reply_text("⏳ در صف انتظار برای پردازش لینک...")
    job_dir = os.path.join(DOWNLOAD_DIR, f"job_{int(time.time()*1000)}")
    os.makedirs(job_dir, exist_ok=True)
    local_path = ""

    async with upload_lock:
        cancel_requested = False
        try:
            host = urlparse(url).netloc.lower()

            # ۱. دانلود از Mega.nz
            if "mega.nz" in host or "mega.co.nz" in host:
                initial_name, orig_ext = resolve_initial_filename("mega_file.bin", explicit_name)
                active_download_ext = orig_ext
                active_final_filename = initial_name
                await status_msg.edit_text("☁️ در حال اتصال به سرورهای **Mega.nz**...", reply_markup=get_cancel_kb())

                local_path = await download_external_tool(
                    ["megadl", "--no-progress", "--path", job_dir, url],
                    job_dir, status_msg, "در حال دانلود از Mega.nz..."
                )
                real_name = os.path.basename(local_path)
                _, real_ext = os.path.splitext(real_name)
                active_download_ext = real_ext
                if active_final_filename == "mega_file.bin":
                    active_final_filename = sanitize_filename(real_name)
                else:
                    active_final_filename = build_filename_with_ext(active_final_filename, real_ext)

            # ۲. دانلود از Google Drive
            elif "drive.google.com" in host:
                initial_name, orig_ext = resolve_initial_filename("gdrive_file.bin", explicit_name)
                active_download_ext = orig_ext
                active_final_filename = initial_name
                await status_msg.edit_text("☁️ در حال دانلود از **Google Drive**...", reply_markup=get_cancel_kb())

                gdown_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "gdown")
                if not os.path.exists(gdown_bin):
                    gdown_bin = "gdown"

                local_path = await download_external_tool(
                    [gdown_bin, "--fuzzy", url, "-O", f"{job_dir}/"],
                    job_dir, status_msg, "در حال دانلود از Google Drive..."
                )
                real_name = os.path.basename(local_path)
                _, real_ext = os.path.splitext(real_name)
                active_download_ext = real_ext
                if active_final_filename == "gdrive_file.bin":
                    active_final_filename = sanitize_filename(real_name)
                else:
                    active_final_filename = build_filename_with_ext(active_final_filename, real_ext)

            # ۳. دانلود از یوتیوب، توییتر، اینستاگرام، تیک‌تاک و... با yt-dlp
            elif is_ytdlp_url(url):
                initial_name, _ = resolve_initial_filename("video.mp4", explicit_name)
                active_download_ext = ".mp4"
                active_final_filename = initial_name
                await status_msg.edit_text("🎬 در حال استخراج ویدیو با **yt-dlp**...", reply_markup=get_cancel_kb())

                ytdlp_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "yt-dlp")
                if not os.path.exists(ytdlp_bin):
                    ytdlp_bin = "yt-dlp"

                cmd = [
                    ytdlp_bin, "--no-playlist", "--merge-output-format", "mp4",
                    "-o", "%(title).80s.%(ext)s", url
                ]
                if os.path.exists(COOKIES_FILE):
                    cmd.extend(["--cookies", COOKIES_FILE])

                local_path = await download_external_tool(
                    cmd, job_dir, status_msg, "در حال دانلود رسانه با yt-dlp..."
                )
                real_name = os.path.basename(local_path)
                _, real_ext = os.path.splitext(real_name)
                active_download_ext = real_ext
                if active_final_filename == "video.mp4":
                    active_final_filename = sanitize_filename(real_name)
                else:
                    active_final_filename = build_filename_with_ext(active_final_filename, real_ext)

            # ۴. لینک مستقیم معمولی (HTTP/HTTPS)
            else:
                parsed_path = unquote(urlparse(url).path)
                orig_name = os.path.basename(parsed_path) or f"url_file_{int(time.time())}.bin"
                initial_name, orig_ext = resolve_initial_filename(orig_name, explicit_name)
                active_download_ext = orig_ext
                active_final_filename = initial_name
                local_path = os.path.join(job_dir, initial_name)

                start_t = time.time()
                await status_msg.edit_text(
                    f"🌐 شروع دانلود از لینک مستقیم:\n`{active_final_filename}`",
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
                                if now - getattr(status_msg, "_last_edit", 0) >= 4:
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
                                            f"💡 *(برای تغییر نام، همین الان نام جدید را در چت بفرستید)*",
                                            reply_markup=get_cancel_kb()
                                        )
                                    except Exception:
                                        pass

            if os.path.getsize(local_path) > 2000 * 1024 * 1024:
                raise Exception("حجم فایل دانلود شده بیشتر از سقف ۲ گیگابایت گیت‌هاب است.")

            await finalize_and_upload(local_path, status_msg)

        except asyncio.CancelledError:
            await status_msg.edit_text("🛑 **عملیات دانلود/آپلود با موفقیت لغو شد و فایل موقت پاک گردید.**")
        except Exception as e:
            await status_msg.edit_text(f"❌ **خطا در پردازش لینک:**\n`{str(e)}`")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)


@app.on_message(filters.command("url") & filters.user(ADMIN_ID))
async def url_leech_cmd(client: Client, message: Message):
    text_lines = message.text.strip().splitlines()
    first_line_parts = text_lines[0].split(maxsplit=2)

    if len(first_line_parts) < 2:
        return await message.reply_text(
            "⚠️ لطفاً لینک فایل را وارد کنید یا مستقیم لینک را بدون دستور بفرستید.\n"
            "مثال همراه با نام در خط دوم:\n"
            "`https://mega.nz/file/...`\n"
            "`my_file.zip`"
        )

    url = first_line_parts[1].strip()
    custom_name = None
    if len(text_lines) > 1 and text_lines[1].strip():
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
            f"♻️ ربات در حال ری‌استارت شدن است..."
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

    is_forwarded = bool(message.forward_date or message.forward_from or message.forward_from_chat or message.forward_sender_name)
    allowed_caption = None if is_forwarded else message.caption

    initial_name, orig_ext = resolve_initial_filename(orig_name, allowed_caption)
    job_dir = os.path.join(DOWNLOAD_DIR, f"tg_{tag_time}")
    os.makedirs(job_dir, exist_ok=True)
    local_path = os.path.join(job_dir, initial_name)

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

            await finalize_and_upload(local_path, status_msg)

        except (asyncio.CancelledError, StopTransmission):
            await status_msg.edit_text("🛑 **عملیات دانلود/آپلود با موفقیت لغو شد و فایل موقت از سرور پاک گردید.**")
        except Exception as e:
            await status_msg.edit_text(f"❌ **خطا در پردازش فایل:**\n`{str(e)}`")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)


@app.on_message(filters.text & filters.user(ADMIN_ID) & ~filters.regex(r"^/"))
async def handle_plain_text_or_links(client: Client, message: Message):
    """مدیریت پیام‌های متنی: تغییر نام در گیت‌هاب، تشخیص لینک، یا تغییر نام قبل/حین دانلود"""
    global pending_custom_name, active_final_filename, active_download_ext, awaiting_gh_rename_asset_id

    lines = [line.strip() for line in message.text.strip().splitlines() if line.strip()]
    if not lines:
        return
    first_line = lines[0]

    # ۱. اگر کاربر روی دکمه «تغییر نام در گیت‌هاب» کلیک کرده باشد
    if awaiting_gh_rename_asset_id and not first_line.startswith("http"):
        asset_id = awaiting_gh_rename_asset_id
        awaiting_gh_rename_asset_id = None
        new_asset_name = sanitize_filename(first_line)
        status = await message.reply_text(f"⚙️ در حال تغییر نام فایل در گیت‌هاب به `{new_asset_name}`...")

        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.patch(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/assets/{asset_id}",
                headers=GH_HEADERS,
                json={"name": new_asset_name}
            )
            if resp.status_code == 200:
                data = resp.json()
                new_url = data["browser_download_url"]
                return await status.edit_text(
                    f"✅ **نام فایل در گیت‌هاب بدون نیاز به آپلود مجدد تغییر یافت!**\n\n"
                    f"📄 **نام جدید:** `{data['name']}`\n"
                    f"🔗 **لینک مستقیم جدید:**\n`{new_url}`",
                    disable_web_page_preview=True
                )
            else:
                return await status.edit_text(f"❌ خطا در تغییر نام در گیت‌هاب: `{resp.status_code}`\n`{resp.text}`")

    # ۲. اگر خط اول لینک باشد (مستقیم، مگا، گوگل درایو، یوتیوب و...)
    if first_line.startswith("http://") or first_line.startswith("https://"):
        parts = first_line.split(maxsplit=1)
        url = parts[0]
        custom_name = None
        if len(lines) > 1:
            custom_name = lines[1]
        elif len(parts) > 1:
            custom_name = parts[1]

        return await process_url_download(message, url, custom_name)

    # ۳. اگر فایلی در حال دانلود است -> تغییر نام درجا (Live Rename)
    if upload_lock.locked():
        new_name = build_filename_with_ext(first_line, active_download_ext)
        active_final_filename = new_name
        return await message.reply_text(
            f"✏️ **نام فایل در حال دانلود تغییر یافت به:**\n`{new_name}`\n\n"
            f"*(پس از پایان دانلود، با همین نام پردازش و آپلود خواهد شد)*"
        )

    # ۴. ذخیره نام برای فایل/لینک بعدی
    pending_custom_name = sanitize_filename(first_line)
    await message.reply_text(
        f"📌 **نام `{pending_custom_name}` برای فایل بعدی ذخیره شد!**\n\n"
        f"حالا ویدیو/فایل خود را فوروارد یا ارسال کنید (یا لینک بفرستید) تا با این نام آپلود شود."
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

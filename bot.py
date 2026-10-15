import os
import re
import sys
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

# متغیر سراسری برای مدیریت لغو عملیات در حال انجام
cancel_requested = False

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


def resolve_custom_filename(orig_name: str, caption: str | None) -> str:
    """تغییر نام فایل بر اساس کپشن ارسالی با حفظ پسوند اصلی در صورت فراموشی"""
    orig_base, orig_ext = os.path.splitext(orig_name)
    if not caption:
        return sanitize_filename(orig_name)

    first_line = caption.strip().splitlines()[0].strip()
    # نادیده گرفتن کپشن اگر دستور ربات یا لینک باشد
    if first_line.startswith("/") or first_line.startswith("http"):
        return sanitize_filename(orig_name)

    custom_name = sanitize_filename(first_line)
    _, custom_ext = os.path.splitext(custom_name)

    # اگر کاربر در کپشن پسوند ننوشته بود، پسوند اصلی فایل را اضافه کن
    if not custom_ext and orig_ext:
        custom_name += orig_ext

    return custom_name


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
    """نمایش پیشرفت دانلود از تلگرام + بررسی درخواست لغو"""
    global cancel_requested
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
        f"📊 پیشرفت: `{percentage:.1f}%`\n"
        f"📦 حجم: `{human_size(current)}` از `{human_size(total)}`\n"
        f"🚀 سرعت: `{human_size(speed)}/s`\n"
        f"⏳ زمان باقی‌مانده: `{int(eta)} ثانیه`"
    )
    try:
        await status_msg.edit_text(text, reply_markup=get_cancel_kb())
    except Exception:
        pass


async def upload_file_to_github(local_path: str, clean_filename: str, status_msg: Message) -> tuple[int, str, str, int]:
    """ایجاد Release و آپلود فایل روی گیت‌هاب"""
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

        if cancel_requested:
            await http.delete(f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}", headers=GH_HEADERS)
            raise asyncio.CancelledError("عملیات توسط کاربر لغو شد.")

        await status_msg.edit_text(
            f"☁️ **در حال آپلود روی سرور گیت‌هاب...**\n\n"
            f"📄 نام فایل: `{clean_filename}`\n"
            f"📦 حجم: `{human_size(file_size)}`\n"
            f"*(لطفاً صبر کنید...)*",
            reply_markup=get_cancel_kb()
        )

        upload_headers = {
            **GH_HEADERS,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(file_size)
        }

        with open(local_path, "rb") as f:
            up_resp = await http.post(
                upload_url,
                params={"name": clean_filename},
                headers=upload_headers,
                content=f
            )

        if up_resp.status_code != 201:
            raise Exception(f"خطا در آپلود فایل: {up_resp.text}")

        asset_data = up_resp.json()
        direct_link = asset_data["browser_download_url"]
        return release_id, release_title, direct_link, file_size


@app.on_message(filters.command(["start", "help"]) & filters.user(ADMIN_ID))
async def start_cmd(client: Client, message: Message):
    help_text = (
        "👋 **به ربات آپلودر مستقیم گیت‌هاب خوش آمدید!**\n\n"
        "📌 **امکانات و نحوه استفاده:**\n\n"
        "1️⃣ **آپلود فایل یا ویدیو:**\n"
        "هر فایل، ویدیو یا فایلی را (تا سقف ۲ گیگابایت) مستقیم ارسال یا فوروارد کنید تا لینک مستقیم گیت‌هاب دریافت کنید.\n\n"
        "2️⃣ **تغییر نام فایل قبل از آپلود (Rename):**\n"
        "کافیست هنگام ارسال یا فوروارد فایل، نام جدید را در **کپشن (Caption)** بنویسید (مثلاً `episode_01.mp4` یا فقط `episode_01`). ربات فایل را با نام دلخواه شما آپلود می‌کند.\n\n"
        "3️⃣ **لیچر لینک مستقیم (`/url`):**\n"
        "برای دانلود مستقیم فایل از اینترنت به سرور و آپلود در گیت‌هاب:\n"
        "`/url https://example.com/video.mp4`\n"
        "یا همراه با تغییر نام:\n"
        "`/url https://example.com/video.mp4 new_name.mp4`\n\n"
        "4️⃣ **جستجو در فایل‌های آپلود شده (`/search`):**\n"
        "`/search نام_فایل`\n\n"
        "5️⃣ **مدیریت و حذف فایل‌های اخیر (`/files`):**\n"
        "مشاهده ۱۰ فایل آخر به همراه تعداد دانلود و دکمه حذف از گیت‌هاب.\n\n"
        "6️⃣ **لغو عملیات در حال انجام (`/cancel`):**\n"
        "توقف فوری دانلود یا آپلود فعلی و پاکسازی فایل موقت.\n\n"
        "7️⃣ **بروزرسانی خودکار ربات (`/update`):**\n"
        "دریافت آخرین تغییرات کد از مخزن گیت‌هاب (`git pull`) و ری‌استارت خودکار ربات.\n\n"
        "🎬 **نکته پخش آنلاین:** لینک‌های خروجی را می‌توانید در پلیرهایی مثل **VLC** یا **MX Player** قرار دهید تا بدون نیاز به دانلود کامل، به صورت آنلاین استریم شوند."
    )
    await message.reply_text(help_text, disable_web_page_preview=True)


@app.on_message(filters.command("cancel") & filters.user(ADMIN_ID))
async def cancel_cmd(client: Client, message: Message):
    global cancel_requested
    if upload_lock.locked():
        cancel_requested = True
        await message.reply_text("🛑 درخواست لغو ثبت شد؛ عملیات در حال توقف و پاکسازی است...")
    else:
        await message.reply_text("ℹ️ در حال حاضر هیچ عملیاتی در حال انجام نیست.")


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


@app.on_message(filters.command("url") & filters.user(ADMIN_ID))
async def url_leech_cmd(client: Client, message: Message):
    global cancel_requested
    if len(message.command) < 2:
        return await message.reply_text(
            "⚠️ لطفاً لینک مستقیم فایل را وارد کنید.\n"
            "مثال:\n`/url https://example.com/file.mp4`\n"
            "یا با نام دلخواه:\n`/url https://example.com/file.mp4 my_video.mp4`"
        )

    url = message.command[1]
    custom_name = " ".join(message.command[2:]) if len(message.command) > 2 else None

    parsed_path = unquote(urlparse(url).path)
    orig_name = os.path.basename(parsed_path) or f"url_file_{int(time.time())}.bin"
    clean_filename = resolve_custom_filename(orig_name, custom_name)

    tag_time = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:19]
    local_path = os.path.join(DOWNLOAD_DIR, f"{tag_time}_{clean_filename}")

    status_msg = await message.reply_text("⏳ در صف انتظار برای دانلود از لینک...")

    async with upload_lock:
        cancel_requested = False
        try:
            start_t = time.time()
            await status_msg.edit_text(
                f"🌐 شروع دانلود از لینک:\n`{clean_filename}`",
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
                                        f"📄 نام: `{clean_filename}`\n"
                                        f"📊 پیشرفت: `{pct:.1f}%`\n"
                                        f"📦 دریافت شده: `{human_size(current)}` از `{human_size(total) if total else 'نامشخص'}`\n"
                                        f"🚀 سرعت: `{human_size(speed)}/s`",
                                        reply_markup=get_cancel_kb()
                                    )
                                except Exception:
                                    pass

            release_id, release_title, direct_link, file_size = await upload_file_to_github(
                local_path, clean_filename, status_msg
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
        # خروج از برنامه تا systemd به صورت خودکار سرویس را با کد جدید بالا بیاورد
        os._exit(0)
    except Exception as e:
        await status.edit_text(f"❌ **خطا در بروزرسانی:**\n`{str(e)}`")


@app.on_message((filters.video | filters.document | filters.audio) & filters.user(ADMIN_ID))
async def handle_media(client: Client, message: Message):
    global cancel_requested
    media = message.video or message.document or message.audio
    file_size = media.file_size or 0

    if file_size > 2000 * 1024 * 1024:
        return await message.reply_text("❌ حجم فایل بیشتر از سقف مجاز گیت‌هاب (۲ گیگابایت) است.")

    tag_time = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:19]
    orig_name = getattr(media, "file_name", None)
    if not orig_name:
        ext = ".mp4" if message.video else (".mp3" if message.audio else ".bin")
        orig_name = f"media_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"

    # بررسی کپشن برای تغییر نام فایل
    clean_filename = resolve_custom_filename(orig_name, message.caption)
    local_path = os.path.join(DOWNLOAD_DIR, f"{tag_time}_{clean_filename}")

    status_msg = await message.reply_text("⏳ در صف انتظار برای پردازش...")

    async with upload_lock:
        cancel_requested = False
        try:
            start_t = time.time()
            await status_msg.edit_text(
                f"📥 شروع دانلود `{clean_filename}` از تلگرام...",
                reply_markup=get_cancel_kb()
            )
            dl_result = await message.download(
                file_name=local_path,
                progress=tg_progress_bar,
                progress_args=(client, status_msg, start_t, "در حال دانلود از تلگرام به سرور...")
            )

            if cancel_requested or not dl_result or not os.path.exists(local_path):
                raise asyncio.CancelledError("عملیات توسط کاربر لغو شد.")

            release_id, release_title, direct_link, final_size = await upload_file_to_github(
                local_path, clean_filename, status_msg
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

import os
import re
import time
import asyncio
from datetime import datetime
import httpx
from dotenv import load_dotenv
from pyrogram import Client, filters
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

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}


def sanitize_filename(name: str) -> str:
    """پاکسازی نام فایل برای جلوگیری از به هم ریختن لینک دانلود"""
    name = re.sub(r'[\\/*?:"<>|\s]+', '_', name)
    return name.strip('_') or f"file_{int(time.time())}"


def human_size(size_bytes: int) -> str:
    """تبدیل بایت به مگابایت و گیگابایت خوانا"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} TB"


async def progress_bar(current, total, status_msg: Message, start_time: float, stage: str):
    """نمایش وضعیت پیشرفت دانلود در تلگرام (بروزرسانی هر ۴ ثانیه برای جلوگیری از FloodWait)"""
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
        await status_msg.edit_text(text)
    except Exception:
        pass


@app.on_message(filters.command("start") & filters.user(ADMIN_ID))
async def start_cmd(client: Client, message: Message):
    await message.reply_text(
        "👋 **ربات آپلودر مستقیم گیت‌هاب آماده است!**\n\n"
        "📹 هر فایل یا ویدیویی (تا سقف ۲ گیگابایت) بفرستید تا در GitHub Releases آپلود شود و لینک مستقیم تحویل بگیرید.\n\n"
        "📋 دستور `/files` : مشاهده و مدیریت ۱۰ فایل آخر آپلود شده"
    )


@app.on_message(filters.command("files") & filters.user(ADMIN_ID))
async def list_files_cmd(client: Client, message: Message):
    status = await message.reply_text("🔍 در حال دریافت لیست فایل‌ها از گیت‌هاب...")
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
                text = f"📄 **{title}**\n📦 حجم: `{size}`\n🔗 [لینک دانلود مستقیم]({dl_url})"
            else:
                text = f"📄 **{title}** (بدون فایل ضمیمه)"

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 حذف از گیت‌هاب", callback_data=f"del_{rel_id}")]
            ])
            await message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)


@app.on_message((filters.video | filters.document | filters.audio) & filters.user(ADMIN_ID))
async def handle_media(client: Client, message: Message):
    media = message.video or message.document or message.audio
    file_size = media.file_size or 0

    if file_size > 2000 * 1024 * 1024:
        return await message.reply_text("❌ حجم فایل بیشتر از سقف مجاز گیت‌هاب (۲ گیگابایت) است.")

    # تعیین نام فایل و حفظ پسوند اصلی
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag_time = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:19]

    orig_name = getattr(media, "file_name", None)
    if not orig_name:
        ext = ".mp4" if message.video else (".mp3" if message.audio else ".bin")
        orig_name = f"media_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"

    clean_filename = sanitize_filename(orig_name)
    local_path = os.path.join(DOWNLOAD_DIR, f"{tag_time}_{clean_filename}")

    status_msg = await message.reply_text("⏳ در صف انتظار برای پردازش...")

    async with upload_lock:
        try:
            # ۱. دانلود موقت روی سرور AWS
            start_t = time.time()
            await status_msg.edit_text(f"📥 شروع دانلود `{clean_filename}` از تلگرام...")
            await message.download(
                file_name=local_path,
                progress=progress_bar,
                progress_args=(status_msg, start_t, "در حال دانلود از تلگرام به سرور...")
            )

            # ۲. ساخت Release اختصاصی با نام فایل + تاریخ و ساعت
            await status_msg.edit_text("⚙️ در حال ایجاد Release جدید در گیت‌هاب...")
            tag_name = f"v{tag_time}"
            release_title = f"{clean_filename} | {now_str}"

            async with httpx.AsyncClient(timeout=None) as http:
                create_resp = await http.post(
                    f"https://api.github.com/repos/{GITHUB_REPO}/releases",
                    headers=GH_HEADERS,
                    json={
                        "tag_name": tag_name,
                        "name": release_title,
                        "body": f"Uploaded via Telegram Bot on {now_str}\nOriginal File: `{clean_filename}` ({human_size(file_size)})",
                        "draft": False,
                        "prerelease": False
                    }
                )
                if create_resp.status_code != 201:
                    raise Exception(f"خطا در ساخت Release: {create_resp.text}")

                release_data = create_resp.json()
                release_id = release_data["id"]
                upload_url = release_data["upload_url"].split("{")[0]

                # ۳. آپلود فایل به Release با نام دقیق فایل اصلی
                await status_msg.edit_text(
                    f"☁️ **در حال آپلود روی سرور گیت‌هاب...**\n"
                    f"📄 نام فایل: `{clean_filename}`\n"
                    f"📦 حجم: `{human_size(file_size)}`\n"
                    f"*(لطفاً صبر کنید...)*"
                )

                upload_headers = {
                    **GH_HEADERS,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(os.path.getsize(local_path))
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

            # ۴. ارسال لینک مستقیم و دکمه حذف
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 دانلود مستقیم فایل", url=direct_link)],
                [InlineKeyboardButton("🗑 حذف این فایل از گیت‌هاب", callback_data=f"del_{release_id}")]
            ])

            await status_msg.edit_text(
                f"✅ **فایل با موفقیت در گیت‌هاب آپلود شد!**\n\n"
                f"🏷 **عنوان ریلیز:** `{release_title}`\n"
                f"📦 **حجم:** `{human_size(file_size)}`\n\n"
                f"🔗 **لینک مستقیم (به نام فایل):**\n`{direct_link}`",
                reply_markup=kb,
                disable_web_page_preview=True
            )

        except Exception as e:
            await status_msg.edit_text(f"❌ **خطا در پردازش فایل:**\n`{str(e)}`")
        finally:
            # ۵. حذف تضمینی فایل موقت از روی هارد سرور
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
        # دریافت نام تگ قبل از حذف ریلیز (برای پاک کردن کامل تگ از گیت‌هاب)
        get_resp = await http.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/{release_id}",
            headers=GH_HEADERS
        )
        tag_name = get_resp.json().get("tag_name") if get_resp.status_code == 200 else None

        # حذف Release و فایل داخل آن
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

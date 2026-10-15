# ☁️ ربات تلگرام آپلودر پیشرفته گیت‌هاب (Telegram to GitHub Releases)

یک ربات تلگرام هوشمند و پرقدرت مبتنی بر **Pyrogram** برای دانلود فایل‌های تلگرامی (یا لینک‌های مستقیم اینترنت) و آپلود خودکار آن‌ها در بخش **Releases** مخزن گیت‌هاب با **لینک دانلود مستقیم و دائمی**.

---

## ✨ امکانات و ویژگی‌های پیشرفته

* 🚀 **بدون محدودیت ۲۰ مگابایت:** پشتیبانی از دریافت و آپلود فایل‌های حجیم تا سقف **۲ گیگابایت** با شتاب‌دهنده `TgCrypto`.
* ✏️ **تغییر نام با کپشن:** کافی است هنگام ارسال ویدیو یا فایل، نام دلخواه خود را در کپشن (توضیحات زیر فایل) بنویسید تا ربات آن را با همان نام جدید روی گیت‌هاب ذخیره کند!
* 🌐 **لیچر مستقیم (`/url`):** ارسال لینک مستقیم هر فایلی از اینترنت به ربات تا سرور شما مستقیماً آن را روی گیت‌هاب آپلود کند.
* ❌ **دکمه لغو عملیات (`Cancel`):** امکان لغو در هر لحظه از دانلود یا آپلود فایل‌های سنگین.
* 🔎 **جستجوی پیشرفته (`/search`):** جستجوی کلمات کلیدی در میان تمامی فایل‌های قدیمی آپلود شده در گیت‌هاب.
* 🔄 **بروزرسانی خودکار (`/update`):** آپلود کدهای جدید در گیت‌هاب و اعمال آن روی سرور با یک دستور در تلگرام.
* 🗑 **مدیریت کامل:** مشاهده لیست فایل‌ها با دستور `/files` و حذف تک‌کلیکی از گیت‌هاب.

---

## 📋 پیش‌نیازها

برای راهه‌اندازی به این ۶ مقدار نیاز دارید:
1. `API_ID` و `API_HASH` (از سایت `my.telegram.org`)
2. `BOT_TOKEN` (از ربات `@BotFather`)
3. `ADMIN_ID` (از ربات `@userinfobot`)
4. `GITHUB_REPO` (به فرمت `username/repo-name` و مخزن حتماً **Public** باشد)
5. `GITHUB_TOKEN` (توکن کلاسیک با دسترسی `repo`)

---

## 🛠 آموزش نصب گام‌به‌گام روی اوبونتو

### ۱. کلون کردن و نصب پیش‌نیازها
```bash
sudo apt update && sudo apt install -y git python3-venv python3-dev build-essential
cd ~
git clone https://github.com/USERNAME/REPO.git gh-uploader
cd ~/gh-uploader
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

### ۲. ساخت فایل `.env`
```bash
nano ~/gh-uploader/.env
```
مقادیر خود را داخل آن بگذارید:
```env
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
BOT_TOKEN=7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ADMIN_ID=987654321
GITHUB_REPO=USERNAME/REPO
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### ۳. راه‌اندازی سرویس دائمی (Systemd)
دستور زیر را یکجا اجرا کنید:
```bash
sudo tee /etc/systemd/system/gh-uploader.service > /dev/null <<EOF
[Unit]
Description=Telegram to GitHub Releases Uploader Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/gh-uploader
ExecStart=$HOME/gh-uploader/venv/bin/python $HOME/gh-uploader/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now gh-uploader
```

---

## 🤖 دستورات ربات در تلگرام

* `/start` : نمایش منوی راهنما و وضعیت ربات
* `/files` : مشاهده ۱۰ فایل آخر آپلود شده به همراه دکمه حذف
* `/search <نام>` : جستجوی فایل در گیت‌هاب
* `/url <لینک>` : دانلود از اینترنت و آپلود مستقیم به گیت‌هاب
* `/update` : دریافت آخرین تغییرات از گیت‌هاب و ری‌استارت ربات روی سرور
* **ارسال فایل با کپشن متنی** : تغییر خودکار نام فایل قبل از آپلود

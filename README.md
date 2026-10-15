☁️ ربات تلگرام آپلودر مستقیم گیت‌هاب (Telegram to GitHub Releases)

یک ربات تلگرام سریع، سبک و اختصاصی مبتنی بر پروتکل MTProto (Pyrogram) که فایل‌ها و ویدیوهای تلگرامی را (تا سقف ۲ گیگابایت در هر فایل) دریافت کرده، در بخش Releases مخزن گیت‌هاب آپلود می‌کند و لینک مستقیم و دائمی دانلود (به نام خود فایل) تحویل می‌دهد.

✨ ویژگی‌ها و امکانات

🚀 بدون محدودیت ۲۰ مگابایت تلگرام: پشتیبانی از دانلود و آپلود فایل‌ها تا سقف ۲۰۰۰ مگابایت (2GB) به کمک Pyrogram و شتاب‌دهنده TgCrypto.

🔗 لینک مستقیم با حفظ نام اصلی فایل: ساخت یک Release مجزا برای هر فایل با عنوان تاریخ و ساعت، که باعث می‌شود لینک خروجی دقیقاً به نام فایل اصلی ختم شود و فایل‌های هم‌نام با هم تداخل پیدا نکنند.

🧹 مصرف بهینه و پاکسازی خودکار دیسک: ذخیره موقت فایل در پوشه /tmp و حذف فوری آن به محض اتمام آپلود (اشغال دائمی هارد سرور = صفر).

📊 نوار پیشرفت زنده: نمایش درصد پیشرفت، حجم، سرعت لحظه‌ای و زمان باقی‌مانده در تلگرام.

🗑 مدیریت کامل از داخل تلگرام: مشاهده لیست آخرین فایل‌ها با دستور /files و حذف کامل فایل و تگ ریلیز از گیت‌هاب با یک دکمه شیشه‌ای.

🔒 کاملاً خصوصی: قفل شده روی آیدی عددی ادمین (ADMIN_ID).

📋 پیش‌نیازها (دریافت کلیدها و توکن‌ها)

قبل از راه‌اندازی روی سرور، این ۶ مقدار را آماده کنید:

API_ID و API_HASH تلگرام:

وارد سایت my.telegram.org شوید، با شماره تلگرام خود لاگین کنید و به بخش API development tools بروید تا این دو مقدار را دریافت نمایید.

BOT_TOKEN (توکن ربات):

در تلگرام به ربات @BotFather دستور /newbot بدهید و توکن ربات جدید را بگیرید.

ADMIN_ID (آیدی عددی اکانت شما):

به ربات @userinfobot پیام دهید و عدد Id خود را کپی کنید.

GITHUB_REPO (نام مخزن گیت‌هاب):

یک مخزن Public (عمومی) در گیت‌هاب بسازید (تا لینک‌های دانلود بدون پسورد و مستقیم کار کنند). فرمت نام باید به صورت username/repo-name باشد.

GITHUB_TOKEN (توکن دسترسی گیت‌هاب):

در گیت‌هاب به مسیر Settings ⬅️ Developer settings ⬅️ Personal access tokens ⬅️ Tokens (classic) بروید.

روی Generate new token (classic) کلیک کنید، انقضا را روی No expiration بگذارید و تیک repo را فعال کنید تا توکن (ghp_...) ساخته شود.

🛠 آموزش نصب گام‌به‌گام روی سرور اوبونتو (Ubuntu)

۱. نصب ابزارهای پایه و کلون کردن مخزن

وارد ترمینال سرور شوید و دستورات زیر را اجرا کنید (نام کاربری و مخزن خود را جایگزین USERNAME/REPO کنید):

sudo apt update && sudo apt install -y git python3-venv python3-dev build-essential
cd ~
git clone https://github.com/USERNAME/REPO.git gh-uploader
cd ~/gh-uploader


۲. ساخت محیط مجازی پایتون و نصب پکیج‌ها

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt


۳. ساخت فایل تنظیمات امنیتی (.env)

فایل .env را در پوشه پروژه ایجاد کنید:

nano ~/gh-uploader/.env


مقادیر خود را دقیقاً به فرمت زیر داخل آن قرار دهید (سپس با Ctrl+O و Enter ذخیره کرده و با Ctrl+X خارج شوید):

API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
BOT_TOKEN=7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ADMIN_ID=987654321
GITHUB_REPO=USERNAME/REPO
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx


⚙️ اجرای دائمی در پس‌زمینه (Systemd Service)

برای اینکه ربات ۲۴ ساعته فعال بماند و در صورت ری‌بوت شدن سرور به صورت خودکار روشن شود، دستور زیر را یکجا کپی و در ترمینال اجرا کنید:

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


🔄 دستورات کاربردی مدیریت سرور و آپدیت ربات

بررسی وضعیت روشن بودن ربات:

sudo systemctl status gh-uploader


مشاهده لاگ‌های زنده ربات (برای عیب‌یابی):

journalctl -u gh-uploader -f


آپدیت کردن کد ربات از گیت‌هاب (پس از تغییر کد در ریپو):

cd ~/gh-uploader && git pull && sudo systemctl restart gh-uploader


توقف یا ری‌استارت دستی ربات:

sudo systemctl restart gh-uploader
sudo systemctl stop gh-uploader


🤖 دستورات داخل تلگرام

/start : بررسی وضعیت آنلاین بودن ربات و راهنمای اولیه

/files : دریافت لیست ۱۰ فایل آخر آپلود شده در گیت‌هاب به همراه حجم، لینک مستقیم و دکمه شیشه‌ای حذف سریع

ارسال یا فوروارد فایل/ویدیو : شروع خودکار دانلود و آپلود به GitHub Releases

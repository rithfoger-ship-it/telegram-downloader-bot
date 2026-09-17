import os
import logging
import asyncio
import json
import urllib.request
import urllib.parse
import shutil
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import yt_dlp
from keep_alive import keep_alive

# CONFIG
load_dotenv(dotenv_path=Path(__file__).parent / ".env")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MAX_TELEGRAM_MB = 50
MAX_TELEGRAM_BYTES = MAX_TELEGRAM_MB * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SUPPORTED_HINTS = ("tiktok.com", "facebook.com", "fb.watch", "youtube.com", "youtu.be", "instagram.com")

def is_supported_url(text: str) -> bool:
    return any(h in text for h in SUPPORTED_HINTS)

def expand_url(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.geturl()
    except Exception as e:
        logger.error(f"Expand URL failed: {e}")
        return url

def fetch_tiktok_tikwm(url: str):
    try:
        full_url = expand_url(url)
        api_url = f"https://www.tikwm.com/api/?url={urllib.parse.quote(full_url)}"
        req = urllib.request.Request(
            api_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status == 200:
                res_data = json.loads(response.read().decode('utf-8'))
                if res_data.get("code") == 0:
                    return res_data.get("data")
    except Exception as e:
        logger.error(f"Tikwm API error: {e}")
    return None

def ydl_download(url: str, out_dir: Path) -> dict:
    outtmpl = str(out_dir / "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "writethumbnail": False,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info

def collect_downloaded_files(info: dict, out_dir: Path) -> list[Path]:
    files = []
    if "requested_downloads" in info and info["requested_downloads"]:
        for d in info["requested_downloads"]:
            fp = Path(d["filepath"])
            if fp.exists():
                files.append(fp)
    elif "entries" in info and info["entries"]:
        for entry in info["entries"]:
            if not entry:
                continue
            for d in entry.get("requested_downloads", []) or []:
                fp = Path(d["filepath"])
                if fp.exists():
                    files.append(fp)
    if not files:
        vid_id = info.get("id", "")
        for f in out_dir.glob(f"*{vid_id}*"):
            files.append(f)
    return files

# HANDLERS
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Send me a link from TikTok, Facebook, YouTube, or Instagram\n"
        "and I will download videos, photos, or slideshows for you."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if not is_supported_url(text):
        await update.message.reply_text("Please send a valid link from TikTok, Facebook, YouTube, or Instagram.")
        return

    url = text.strip()
    status = await update.message.reply_text("📥 Processing your link, please wait...")

    # 1. SPECIAL TIKTOK HANDLING (Tikwm API for Video & Photos)
    if any(domain in url for domain in ["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"]):
        try:
            data = await asyncio.to_thread(fetch_tiktok_tikwm, url)
            if data:
                # Photos / Slideshow
                images = data.get("images", [])
                if images:
                    await status.edit_text("📸 Sending photos...")
                    formatted_images = []
                    for img in images:
                        if img.startswith("//"):
                            img = "https:" + img
                        formatted_images.append(img)
                    
                    # Send in batches of 10 max per album
                    for i in range(0, len(formatted_images), 10):
                        chunk = formatted_images[i:i + 10]
                        media_group = [InputMediaPhoto(media=img_url) for img_url in chunk]
                        await update.message.reply_media_group(media=media_group)
                    
                    await status.delete()
                    return

                # Video
                video_url = data.get("play") or data.get("wmplay")
                if video_url:
                    if video_url.startswith("//"):
                        video_url = "https:" + video_url
                    await status.edit_text("🎥 Sending video...")
                    await update.message.reply_video(video=video_url)
                    await status.delete()
                    return
        except Exception as e:
            logger.error(f"TikTok Direct API processing error: {e}")

    # 2. FALLBACK TO YT-DLP FOR OTHER PLATFORMS (FB/YT/IG)
    work_dir = Path(os.getcwd()) / "downloads" / str(update.message.message_id)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        info = await asyncio.to_thread(ydl_download, url, work_dir)
        files = collect_downloaded_files(info, work_dir)

        if not files:
            await status.edit_text("❌ Sorry, download failed (invalid link or private video).")
            return

        await status.edit_text(f"📤 Got {len(files)} file(s). Uploading to Telegram...")
        for f in files:
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                with open(f, "rb") as photo_file:
                    await update.message.reply_photo(photo=photo_file)
            else:
                if f.stat().st_size > MAX_TELEGRAM_BYTES:
                    await update.message.reply_text(f"⚠️ Warning: {f.name} exceeds 50MB limit for Telegram bots.")
                    continue
                with open(f, "rb") as video_file:
                    await update.message.reply_video(video=video_file, supports_streaming=True)

        await status.delete()

    except Exception as e:
        logger.exception("Download failed")
        await status.edit_text(f"❌ Something went wrong: {e}")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Please set BOT_TOKEN in environment variables.")

    keep_alive()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .connect_timeout(60)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()

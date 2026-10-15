
import asyncio
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
X_COOKIES = os.getenv("X_COOKIES")

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Отправь ссылку на публикацию X (Twitter) с видео."
    )


def is_x_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme in ("http", "https")
            and (
                host == "x.com"
                or host.endswith(".x.com")
                or host == "twitter.com"
                or host.endswith(".twitter.com")
            )
        )
    except ValueError:
        return False


def download_video(url: str, folder: str) -> Path:
    output_template = str(Path(folder) / "%(id)s.%(ext)s")
    cookies_path = None

    try:
        ydl_opts = {
            "format": "best[ext=mp4]/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 2,
        }

        # Создаём временный файл cookies только при наличии переменной.
        if X_COOKIES:
            fd, cookies_path = tempfile.mkstemp(suffix=".txt")

            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(X_COOKIES.strip() + "\n")

            ydl_opts["cookiefile"] = cookies_path

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        files = [
            path for path in Path(folder).iterdir()
            if path.is_file()
            and not path.name.endswith((".part", ".ytdl"))
            and path.name != "cookies.txt"
        ]

        if not files:
            raise RuntimeError("Не удалось получить видеофайл.")

        return max(files, key=lambda path: path.stat().st_size)

    finally:
        # Удаляем временный файл с авторизационными cookies.
        if cookies_path:
            try:
                os.remove(cookies_path)
            except OSError:
                logger.warning("Не удалось удалить временный файл cookies.")


@dp.message(F.text)
async def download_video_handler(message: types.Message):
    url = message.text.strip()

    if not is_x_url(url):
        await message.answer(
            "Отправь корректную ссылку на публикацию X (Twitter)."
        )
        return

    status_msg = await message.answer("Скачиваю видео, подожди...")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = await asyncio.to_thread(
                download_video, url, temp_dir
            )

            if video_path.stat().st_size > 49 * 1024 * 1024:
                await status_msg.edit_text(
                    "Видео слишком большое для отправки этим ботом "
                    "через стандартный Telegram Bot API."
                )
                return

            await status_msg.edit_text("Отправляю видео...")

            await message.answer_video(
                video=types.FSInputFile(str(video_path)),
                caption="Готово!"
            )

        await status_msg.delete()

    except Exception as e:
        logger.exception("Ошибка при обработке видео")

        error_text = f"{type(e).__name__}: {e}"
        await status_msg.edit_text(
            "Не удалось обработать видео.\n\n"
            f"Ошибка: {error_text[:1500]}"
        )


async def main():
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

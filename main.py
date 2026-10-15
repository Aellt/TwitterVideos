import asyncio
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart


# ---------- Настройки ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
X_COOKIES = os.getenv("X_COOKIES")

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------- Проверка cookies ----------

def prepare_cookies() -> str | None:
    """
    Подготавливает содержимое X_COOKIES для записи в файл.
    Никогда не выводит сами cookies в логи.
    """
    if not X_COOKIES or not X_COOKIES.strip():
        logger.warning(
            "X_COOKIES не задана или пуста. "
            "Загрузка будет выполняться без авторизации."
        )
        return None

    cookies = X_COOKIES.strip()

    # Нормализуем переносы строк, не склеивая записи.
    cookies = cookies.replace("\r\n", "\n").replace("\r", "\n")

    # На случай, если панель сохранила переносы как буквальные \n.
    if "\n" not in cookies and "\\n" in cookies:
        cookies = cookies.replace("\\n", "\n")

    lines = cookies.splitlines()
    nonempty_lines = [line for line in lines if line.strip()]

    has_header = any(
        line.strip() in (
            "# Netscape HTTP Cookie File",
            "# HTTP Cookie File",
        )
        for line in lines[:5]
    )

    logger.info(
        "X_COOKIES получена: длина=%d, строк=%d, "
        "переносы=%s, заголовок_Netscape=%s",
        len(cookies),
        len(nonempty_lines),
        "\n" in cookies,
        has_header,
    )

    if not has_header:
        logger.warning(
            "В X_COOKIES не найден стандартный заголовок Netscape. "
            "Проверь экспорт cookies.txt."
        )

    if len(nonempty_lines) < 2:
        logger.warning(
            "В X_COOKIES подозрительно мало строк. "
            "Возможна неполная передача файла."
        )

    return cookies


COOKIES_CONTENT = prepare_cookies()


# ---------- Работа со ссылками ----------

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


# ---------- Скачивание видео ----------

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
            "retries": 3,
            "fragment_retries": 3,
            "overwrites": True,
        }

        # Передаём cookies через временный файл Netscape.
        if COOKIES_CONTENT:
            fd, cookies_path = tempfile.mkstemp(
                prefix="x_cookies_",
                suffix=".txt",
            )

            try:
                with os.fdopen(
                    fd,
                    "w",
                    encoding="utf-8",
                    newline="\n",
                ) as cookies_file:
                    cookies_file.write(COOKIES_CONTENT)
                    cookies_file.write("\n")
            except Exception:
                # Если запись не удалась, закрываем дескриптор,
                # если он ещё открыт, и удаляем временный файл.
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

            ydl_opts["cookiefile"] = cookies_path

        logger.info(
            "Начинаю загрузку видео. Авторизация cookies: %s",
            "включена" if COOKIES_CONTENT else "отсутствует",
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Ищем только результат загрузки в нашей временной папке.
        folder_path = Path(folder)

        files = [
            path
            for path in folder_path.iterdir()
            if path.is_file()
            and path.suffix.lower() not in (".part", ".ytdl", ".txt")
            and path.name != "cookies.txt"
            and path.stat().st_size > 0
        ]

        if not files:
            raise RuntimeError(
                "yt-dlp завершился, но видеофайл не найден."
            )

        # Папка создаётся специально для одной загрузки.
        video_path = max(files, key=lambda path: path.stat().st_size)

        logger.info(
            "Видео загружено: %s, размер=%d байт",
            video_path.name,
            video_path.stat().st_size,
        )

        return video_path

    except Exception:
        # Полная техническая ошибка останется в логах хостинга.
        logger.exception("Ошибка yt-dlp при загрузке видео")
        raise

    finally:
        # Не оставляем временный файл авторизации.
        if cookies_path:
            try:
                os.remove(cookies_path)
            except OSError:
                logger.warning(
                    "Не удалось удалить временный файл cookies."
                )


# ---------- Команды Telegram ----------

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Отправь ссылку на публикацию X (Twitter) с видео."
    )


@dp.message(F.text)
async def download_video_handler(message: types.Message):
    url = message.text.strip()

    if not is_x_url(url):
        await message.answer(
            "Отправь корректную ссылку на публикацию X (Twitter)."
        )
        return

    status_msg = await message.answer(
        "Скачиваю видео, подожди..."
    )

    try:
        with tempfile.TemporaryDirectory(
            prefix="x_video_"
        ) as temp_dir:

            video_path = await asyncio.to_thread(
                download_video,
                url,
                temp_dir,
            )

            size = video_path.stat().st_size

            # Ограничение для отправки через обычный Bot API.
            if size > 49 * 1024 * 1024:
                await status_msg.edit_text(
                    "Видео слишком большое для отправки "
                    "этим ботом через стандартный Telegram Bot API."
                )
                return

            await status_msg.edit_text("Отправляю видео...")

            await message.answer_video(
                video=types.FSInputFile(str(video_path)),
                caption="Готово!",
            )

        await status_msg.delete()

    except Exception as e:
        # Не отправляем пользователю внутренние пути,
        # содержимое cookies или полный технический traceback.
        logger.error(
            "Обработка ссылки завершилась ошибкой: %s",
            type(e).__name__,
        )

        try:
            await status_msg.edit_text(
                "Не удалось скачать видео. "
                "Проверь ссылку и попробуй ещё раз. "
                "Если ошибка повторяется, проверь логи хостинга."
            )
        except Exception:
            logger.exception(
                "Не удалось обновить статусное сообщение."
            )


# ---------- Запуск ----------

async def main():
    logger.info("Запуск Telegram-бота")
    logger.info("Версия yt-dlp: %s", yt_dlp.version.__version__)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
```

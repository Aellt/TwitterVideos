import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart


# ---------- Настройки ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,  # убирает дублирование строк в логах
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
X_COOKIES = os.getenv("X_COOKIES")

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

HTTPONLY_PREFIX = "#HttpOnly_"
X_DOMAINS = {"x.com": "twitter.com", "twitter.com": "x.com"}


# ---------- Подготовка cookies ----------

def prepare_cookies() -> str | None:
    """
    Разбирает X_COOKIES, чинит формат (табы, переносы, HttpOnly),
    дублирует куки для x.com и twitter.com и возвращает
    готовое содержимое Netscape cookies.txt.
    Значения cookies никогда не попадают в логи.
    """
    if not X_COOKIES or not X_COOKIES.strip():
        logger.warning(
            "X_COOKIES не задана или пуста. "
            "Загрузка будет без авторизации."
        )
        return None

    raw = X_COOKIES.strip()
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    # Если панель сохранила переносы как буквальные \n.
    if "\n" not in raw and "\\n" in raw:
        raw = raw.replace("\\n", "\n")

    result_lines: list[str] = []
    seen: set[str] = set()
    names: set[str] = set()
    bad_lines = 0
    fixed_spaces = 0
    expired_auth = False
    now = time.time()

    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # Обычные комментарии пропускаем (заголовок добавим сами),
        # а #HttpOnly_ — это настоящие куки.
        if stripped.startswith("#") and not stripped.startswith(HTTPONLY_PREFIX):
            continue

        parts = stripped.split("\t")
        if len(parts) < 7:
            # Табы могли превратиться в пробелы.
            parts = stripped.split(None, 6)
            if len(parts) == 7:
                fixed_spaces += 1
        if len(parts) < 7:
            bad_lines += 1
            continue

        parts = [p.strip() for p in parts[:6]] + [parts[6].strip()]
        domain_raw, name = parts[0], parts[5]

        prefix = HTTPONLY_PREFIX if domain_raw.startswith(HTTPONLY_PREFIX) else ""
        domain = domain_raw[len(prefix):]
        base = domain.lstrip(".").lower()

        variants = [parts]

        # Дублируем куки для второго домена (x.com <-> twitter.com).
        if base in X_DOMAINS:
            other = X_DOMAINS[base]
            dot = "." if domain.startswith(".") else ""
            copy = list(parts)
            copy[0] = f"{prefix}{dot}{other}"
            variants.append(copy)

            names.add(name)
            if name == "auth_token":
                try:
                    expiry = int(parts[4])
                    if expiry and expiry < now:
                        expired_auth = True
                except ValueError:
                    pass

        for variant in variants:
            out = "\t".join(variant)
            if out not in seen:
                seen.add(out)
                result_lines.append(out)

    has_auth = "auth_token" in names
    has_ct0 = "ct0" in names

    logger.info(
        "Cookies разобраны: строк=%d, битых=%d, "
        "исправлено_пробелов=%d, auth_token=%s, ct0=%s",
        len(result_lines),
        bad_lines,
        fixed_spaces,
        has_auth,
        has_ct0,
    )

    if not has_auth or not has_ct0:
        logger.warning(
            "В cookies нет auth_token и/или ct0 для x.com/twitter.com. "
            "Авторизация не заработает — экспортируй cookies заново."
        )
    if expired_auth:
        logger.warning(
            "Срок действия auth_token уже истёк — нужен свежий экспорт."
        )
    if not result_lines:
        return None

    content = "# Netscape HTTP Cookie File\n" + "\n".join(result_lines) + "\n"

    # Проверяем, что yt-dlp сам читает этот файл.
    try:
        from yt_dlp.cookies import YoutubeDLCookieJar

        fd, test_path = tempfile.mkstemp(prefix="x_cookies_test_", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            jar = YoutubeDLCookieJar(test_path)
            jar.load(ignore_discard=True, ignore_expires=True)
            loaded = {c.name for c in jar if "x.com" in c.domain or "twitter.com" in c.domain}
            logger.info(
                "yt-dlp загрузил cookies: всего=%d, auth_token=%s, ct0=%s",
                len(list(jar)),
                "auth_token" in loaded,
                "ct0" in loaded,
            )
        finally:
            try:
                os.remove(test_path)
            except OSError:
                pass
    except Exception as e:
        logger.warning(
            "Проверка cookies через yt-dlp не удалась: %s: %s",
            type(e).__name__,
            e,
        )

    return content


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
            "no_warnings": False,   # предупреждения yt-dlp тоже полезны
            "logger": logger,       # весь вывод yt-dlp идёт в наш logging
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "overwrites": True,
        }

        if COOKIES_CONTENT:
            fd, cookies_path = tempfile.mkstemp(
                prefix="x_cookies_",
                suffix=".txt",
            )
            try:
                with os.fdopen(
                    fd, "w", encoding="utf-8", newline="\n"
                ) as cookies_file:
                    cookies_file.write(COOKIES_CONTENT)
            except Exception:
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

        folder_path = Path(folder)
        files = [
            path
            for path in folder_path.iterdir()
            if path.is_file()
            and path.suffix.lower() not in (".part", ".ytdl", ".txt")
            and path.stat().st_size > 0
        ]

        if not files:
            raise RuntimeError("yt-dlp завершился, но видеофайл не найден.")

        video_path = max(files, key=lambda path: path.stat().st_size)

        logger.info(
            "Видео загружено: %s, размер=%d байт",
            video_path.name,
            video_path.stat().st_size,
        )
        return video_path

    finally:
        if cookies_path:
            try:
                os.remove(cookies_path)
            except OSError:
                logger.warning("Не удалось удалить временный файл cookies.")


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

    status_msg = await message.answer("Скачиваю видео, подожди...")

    try:
        with tempfile.TemporaryDirectory(prefix="x_video_") as temp_dir:
            video_path = await asyncio.to_thread(
                download_video, url, temp_dir
            )

            size = video_path.stat().st_size

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
        # Текст ошибки yt-dlp не содержит cookies, его безопасно логировать.
        error_text = str(e)
        logger.error(
            "Обработка ссылки завершилась ошибкой: %s: %s",
            type(e).__name__,
            error_text,
        )

        if "No video could be found" in error_text:
            user_text = (
                "В этом посте не найдено видео. Возможно, X не отдаёт "
                "его без авторизации — проверь cookies в логах хостинга."
            )
        else:
            user_text = (
                "Не удалось скачать видео. Проверь ссылку и попробуй ещё раз. "
                "Если ошибка повторяется, проверь логи хостинга."
            )

        try:
            await status_msg.edit_text(user_text)
        except Exception:
            logger.exception("Не удалось обновить статусное сообщение.")


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

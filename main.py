import asyncio
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, StickerFormat, StickerType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    Message,
    MessageEntity,
    CallbackQuery,
)
from PIL import Image, ImageOps, UnidentifiedImageError
import imageio_ffmpeg

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pong-emoji")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная BOT_TOKEN не задана в Railway Variables")

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class PuzzleState(StatesGroup):
    choosing_grid = State()


def grid_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{rows}×{cols} · {rows * cols}",
            callback_data=f"grid:{rows}:{cols}",
        )
        for rows in (1, 2)
        for cols in range(1, 11)
    ]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def make_tiles(raw: bytes, rows: int, cols: int) -> list[bytes]:
    try:
        with Image.open(BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Слишком большое изображение. Максимум — 30 мегапикселей.")
            # Fit the complete source into a canvas matching the selected grid.
            # Preserve the full source without cropping its edges.
            width, height = cols * 100, rows * 100
            source_image = image.convert("RGBA")
            fitted = ImageOps.contain(
                source_image, (width, height), method=Image.Resampling.LANCZOS
            )
            image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            image.alpha_composite(
                fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2)
            )
            result = []
            for row in range(rows):
                for col in range(cols):
                    tile = image.crop((col * 100, row * 100, (col + 1) * 100, (row + 1) * 100))
                    # Keep the complete 100x100 fragment so adjacent emoji touch.
                    output = BytesIO()
                    tile.save(output, format="WEBP", quality=90, method=6)
                    data = output.getvalue()
                    quality = 80
                    while len(data) > 60 * 1024 and quality >= 30:
                        output = BytesIO()
                        tile.save(output, format="WEBP", quality=quality, method=6)
                        data = output.getvalue()
                        quality -= 10
                    if len(data) > 60 * 1024:
                        raise ValueError("Не удалось сжать один из фрагментов до лимита Telegram.")
                    result.append(data)
            return result
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Не получилось прочитать изображение. Отправь JPG, PNG или WEBP.") from exc


def make_mosaic_preview(raw: bytes, grid: int, video: bool = False) -> bytes:
    """Build a miniature image of the complete mosaic for the chat preview."""
    size = grid * 100
    if video:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        vf = (
            f"scale={size}:{size}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"format=rgba,"
            f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color=black@0"
        )
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-frames:v", "1", "-vf", vf,
            "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        ]
        proc = subprocess.run(cmd, input=raw, capture_output=True, timeout=90)
        if proc.returncode or not proc.stdout:
            logger.error("Could not create video mosaic preview: %s", proc.stderr.decode("utf-8", errors="ignore")[-800:])
            raise ValueError("Не удалось подготовить миниатюру видео.")
        with Image.open(BytesIO(proc.stdout)) as frame:
            image = frame.convert("RGBA")
    else:
        try:
            with Image.open(BytesIO(raw)) as source:
                source = ImageOps.exif_transpose(source)
                source.load()
                fitted = ImageOps.contain(source.convert("RGBA"), (size, size), method=Image.Resampling.LANCZOS)
                image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                image.alpha_composite(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("Не получилось собрать миниатюру изображения.") from exc

    preview_size = max(1, round(size / 3))
    image = image.resize((preview_size, preview_size), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def create_custom_emoji_pack(user_id: int, rows: int, cols: int, tiles: list[bytes], video: bool = False) -> tuple[str, list[str]]:
    me = await bot.get_me()
    username = (me.username or "").lower()
    if not username:
        raise RuntimeError("У бота не найден username в Telegram")

    # Telegram requires the pack name to end with _by_<bot_username>.
    unique = time.time_ns() // 1_000_000
    pack_name = f"p{user_id}_{unique}_by_{username}"
    pack_title = f"Эмодзи-пазл {rows}×{cols}"
    # Final local guard: never send files close to Telegram's hard upload limits.
    max_upload_size = 180 * 1024 if video else 60 * 1024
    oversized = [(i + 1, len(data)) for i, data in enumerate(tiles) if not data or len(data) > max_upload_size]
    if oversized:
        index, size = oversized[0]
        raise ValueError(
            f"Фрагмент №{index} слишком большой ({size} байт). "
            "Уменьши сетку или выбери другое исходное медиа."
        )

    stickers = [
        InputSticker(
            sticker=BufferedInputFile(data, filename=f"tile_{index + 1}.webm" if video else f"tile_{index + 1}.webp"),
            format=StickerFormat.VIDEO if video else StickerFormat.STATIC,
            emoji_list=["🧩"],
        )
        for index, data in enumerate(tiles)
    ]

    # createNewStickerSet accepts up to 50 stickers at once.
    await bot.create_new_sticker_set(
        user_id=user_id,
        name=pack_name,
        title=pack_title,
        stickers=stickers[:50],
        sticker_type=StickerType.CUSTOM_EMOJI,
    )

    # Custom emoji packs support up to 200 items, so 100 tiles fit in one pack.
    for sticker in stickers[50:]:
        await bot.add_sticker_to_set(user_id=user_id, name=pack_name, sticker=sticker)

    sticker_set = await bot.get_sticker_set(name=pack_name)
    # Bot API no longer returns StickerSet.is_video (deprecated/removed).
    # Verify the format on every Sticker instead, and verify the set type.
    if sticker_set.sticker_type != StickerType.CUSTOM_EMOJI:
        raise RuntimeError(
            f"Telegram создал набор неожиданного типа: {sticker_set.sticker_type}"
        )
    if video:
        invalid_video = [
            index + 1
            for index, sticker in enumerate(sticker_set.stickers)
            if (
                not bool(getattr(sticker, "is_video", False))
                or getattr(sticker, "width", None) != 100
                or getattr(sticker, "height", None) != 100
            )
        ]
        logger.info(
            "Telegram sticker verification: set_type=%s, stickers=%s, video=%s, dimensions=%s",
            sticker_set.sticker_type,
            len(sticker_set.stickers),
            [bool(getattr(sticker, "is_video", False)) for sticker in sticker_set.stickers],
            [(getattr(sticker, "width", None), getattr(sticker, "height", None)) for sticker in sticker_set.stickers],
        )
        if invalid_video:
            raise RuntimeError(
                "Telegram сохранил фрагменты не как видеоэмодзи 100×100: "
                + ", ".join(map(str, invalid_video[:10]))
            )
    ids = [sticker.custom_emoji_id for sticker in sticker_set.stickers]
    if len(ids) != len(tiles) or any(not item for item in ids):
        raise RuntimeError(
            f"Telegram вернул неполный набор: {len(ids)} из {len(tiles)} эмодзи."
        )
    return pack_name, ids


def make_video_tiles(raw: bytes, rows: int, cols: int) -> list[bytes]:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    results = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "source_video"
        source.write_bytes(raw)

        # Decode and normalize the source ONCE. Every tile is cropped from
        # this exact master, so scale, crop and frame timing cannot drift.
        master_width, master_height = cols * 100, rows * 100
        master_video = root / "master.nut"
        master_vf = (
            f"fps=30,"
            f"scale={master_width}:{master_height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"format=yuva420p,"
            f"pad={master_width}:{master_height}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
            f"setsar=1"
        )
        normalize_cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-t", "3", "-an", "-vf", master_vf,
            "-c:v", "ffv1", "-pix_fmt", "yuva420p", "-r", "30", str(master_video),
        ]
        normalized = subprocess.run(normalize_cmd, capture_output=True, timeout=120)
        if normalized.returncode or not master_video.exists() or master_video.stat().st_size == 0:
            logger.error("Video normalization failed: %s", normalized.stderr.decode("utf-8", errors="ignore")[-1200:])
            raise ValueError("Не удалось подготовить видео. Попробуй другой файл.")

        for row in range(rows):
            for col in range(cols):
                output = root / f"tile_{row}_{col}.webm"
                # Preserve the full 100x100 crop so adjacent emoji touch.
                vf = f"crop=100:100:{col * 100}:{row * 100},format=yuva420p"
                data = b""
                last_error = ""
                for crf in (34, 38, 42, 46, 50):
                    cmd = [
                        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(master_video), "-t", "3", "-an", "-vf", vf,
                        "-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "4",
                        "-row-mt", "1", "-auto-alt-ref", "0",
                        "-b:v", "0", "-crf", str(crf),
                        "-pix_fmt", "yuva420p", "-metadata:s:v:0", "alpha_mode=1",
                        "-r", "30", "-f", "webm", str(output),
                    ]
                    p = subprocess.run(cmd, capture_output=True, timeout=120)
                    if p.returncode or not output.exists():
                        last_error = p.stderr.decode("utf-8", errors="ignore")[-700:]
                        continue
                    data = output.read_bytes()
                    if data and len(data) <= 180 * 1024:
                        # Decode the finished WebM before uploading it.
                        check = subprocess.run(
                            [
                                ffmpeg, "-v", "error", "-i", str(output),
                                "-map", "0:v:0", "-f", "framemd5", "-",
                            ],
                            capture_output=True, timeout=60,
                        )
                        decoded_frames = [
                            line for line in check.stdout.decode("utf-8", errors="ignore").splitlines()
                            if line and not line.startswith("#")
                        ]
                        if check.returncode == 0 and len(decoded_frames) >= 2:
                            break
                        last_error = (
                            check.stderr.decode("utf-8", errors="ignore")[-500:]
                            or f"decoded_frames={len(decoded_frames)}"
                        )
                    data = b""

                if not data or len(data) > 180 * 1024:
                    logger.error("Could not encode video tile %s,%s: %s", row, col, last_error)
                    raise ValueError(
                        f"Не удалось подготовить видеофрагмент {row + 1}:{col + 1}. "
                        "Попробуй видео короче или меньшую сетку."
                    )
                logger.info(
                    "Video tile %s/%s: %s bytes, validated WebM/VP9",
                    row * cols + col + 1, rows * cols, len(data),
                )
                results.append(data)

    return results

def build_emoji_preview(rows: int, cols: int, emoji_ids: list[str]) -> tuple[str, list[MessageEntity]]:
    # Telegram entity offsets are measured in UTF-16 code units.
    placeholder = "🧩"
    lines = []
    entities = []
    offset = 0
    for row in range(rows):
        lines.append(placeholder * cols)
        for col in range(cols):
            entities.append(MessageEntity(
                type="custom_emoji",
                offset=offset,
                length=2,
                custom_emoji_id=emoji_ids[row * cols + col],
            ))
            offset += 2
        if row != rows - 1:
            offset += 1
    return "\n".join(lines), entities


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Я превращаю фото и видео в набор настоящих Telegram Custom Emoji 🧩\n\n"
        "Отправь фото или видео — я разрежу его на сетку от 1×1 до 2×10, "
        "создам персональный набор эмодзи и покажу собранную картинку."
    )


@dp.message(F.photo)
async def receive_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        await message.answer("Файл слишком большой. Максимальный размер — 15 МБ.")
        return
    file = await bot.get_file(photo.file_id)
    buffer = BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    raw = buffer.getvalue()
    if len(raw) > MAX_IMAGE_BYTES:
        await message.answer("Файл слишком большой. Максимальный размер — 15 МБ.")
        return
    await state.set_state(PuzzleState.choosing_grid)
    await state.update_data(image_bytes=raw, media_type="image", source_message_id=message.message_id)
    await message.answer("Выбери размер сетки (строки × столбцы):", reply_markup=grid_keyboard())


@dp.message(F.video)
async def receive_video(message: Message, state: FSMContext) -> None:
    video = message.video
    if video.file_size and video.file_size > MAX_IMAGE_BYTES:
        await message.answer('Видео слишком большое. Максимум 15 МБ.')
        return
    file = await bot.get_file(video.file_id)
    buffer = BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    raw = buffer.getvalue()
    if len(raw) > MAX_IMAGE_BYTES:
        await message.answer('Видео слишком большое. Максимум 15 МБ.')
        return
    await state.set_state(PuzzleState.choosing_grid)
    await state.update_data(image_bytes=raw, media_type="video", source_message_id=message.message_id)
    await message.answer("Выбери размер сетки (строки × столбцы). Используются первые 3 секунды видео:", reply_markup=grid_keyboard())


@dp.message(F.document)
async def receive_document(message: Message, state: FSMContext) -> None:
    document = message.document
    is_video = bool(document.mime_type and document.mime_type.startswith("video/"))
    is_image = bool(document.mime_type and document.mime_type.startswith("image/"))
    if not (is_image or is_video):
        await message.answer("Пришли фото или видео как файл.")
        return
    if document.file_size and document.file_size > MAX_IMAGE_BYTES:
        await message.answer("Файл слишком большой. Максимальный размер — 15 МБ.")
        return
    file = await bot.get_file(document.file_id)
    buffer = BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    raw = buffer.getvalue()
    if len(raw) > MAX_IMAGE_BYTES:
        await message.answer("Файл слишком большой. Максимальный размер — 15 МБ.")
        return
    await state.set_state(PuzzleState.choosing_grid)
    await state.update_data(image_bytes=raw, media_type="video" if is_video else "image", source_message_id=message.message_id)
    await message.answer("Выбери сетку. Будут использованы первые 3 секунды видео:" if is_video else "Выбери размер сетки (строки × столбцы):", reply_markup=grid_keyboard())


@dp.callback_query(PuzzleState.choosing_grid, F.data.startswith("grid:"))
async def choose_grid(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    raw = data.get("image_bytes")
    is_video = data.get("media_type") == "video"
    source_message_id = data.get("source_message_id")
    if not raw:
        await state.clear()
        await callback.message.answer("Изображение потерялось. Отправь фотографию ещё раз.")
        return

    try:
        _, rows_text, cols_text = callback.data.split(":")
        rows, cols = int(rows_text), int(cols_text)
    except (ValueError, AttributeError):
        return
    if rows not in (1, 2) or not 1 <= cols <= 10:
        return

    await state.clear()
    status = await callback.message.answer(
        f"🧩 Готовлю сетку {rows}×{cols} ({rows * cols} эмодзи)… Это может занять немного времени."
    )
    await bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)

    try:
        tiles = await asyncio.to_thread(
            make_video_tiles if is_video else make_tiles, raw, rows, cols
        )
        pack_name, emoji_ids = await create_custom_emoji_pack(
            callback.from_user.id, rows, cols, tiles, video=is_video
        )

        preview_text, entities = build_emoji_preview(rows, cols, emoji_ids)
        await callback.message.answer(
            f"Готово! {rows}×{cols} = {rows * cols} отдельных эмодзи. Ниже — собранная картинка из самих эмодзи.",
        )
        await callback.message.answer(
            preview_text,
            entities=entities,
            reply_to_message_id=source_message_id,
            link_preview_options={"is_disabled": True},
        )
        await callback.message.answer(
            f"➕ <a href=\"https://t.me/addemoji/{pack_name}\">Добавить набор эмодзи в Telegram</a>",
            parse_mode="HTML",
        )
        await status.delete()
    except ValueError as exc:
        await status.edit_text(str(exc))
    except TelegramAPIError as exc:
        logger.exception("Telegram API error while creating pack")
        await status.edit_text(
            "Telegram не смог создать набор эмодзи. Попробуй другое фото или меньшую сетку.\n"
            f"Причина: {str(exc)[:700]}"
        )
    except Exception:
        logger.exception("Unexpected error while creating emoji pack")
        await status.edit_text("Произошла ошибка при создании набора. Попробуй ещё раз позже.")


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer("Отправь фото или видео, и я превращу его в пазл из Telegram-эмодзи 🧩")


async def main() -> None:
    logger.info("Starting PONG EMOJI bot")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import logging
import os
import time
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
    rows = []
    values = list(range(3, 11))
    for i in range(0, len(values), 2):
        rows.append([
            InlineKeyboardButton(text=f"{n}×{n} · {n*n} эмодзи", callback_data=f"grid:{n}")
            for n in values[i:i + 2]
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def make_tiles(raw: bytes, grid: int) -> list[bytes]:
    try:
        with Image.open(BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Слишком большое изображение. Максимум — 30 мегапикселей.")
            image = image.convert("RGBA")
            side = min(image.size)
            left = (image.width - side) // 2
            top = (image.height - side) // 2
            image = image.crop((left, top, left + side, top + side))
            image = image.resize((grid * 100, grid * 100), Image.Resampling.LANCZOS)

            result = []
            for row in range(grid):
                for col in range(grid):
                    tile = image.crop((col * 100, row * 100, (col + 1) * 100, (row + 1) * 100))
                    output = BytesIO()
                    tile.save(output, format="WEBP", quality=88, method=6)
                    data = output.getvalue()
                    quality = 80
                    while len(data) > 64 * 1024 and quality >= 40:
                        output = BytesIO()
                        tile.save(output, format="WEBP", quality=quality, method=6)
                        data = output.getvalue()
                        quality -= 10
                    if len(data) > 64 * 1024:
                        raise ValueError("Не удалось сжать один из фрагментов до лимита Telegram.")
                    result.append(data)
            return result
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Не получилось прочитать изображение. Отправь JPG, PNG или WEBP.") from exc


async def create_custom_emoji_pack(user_id: int, grid: int, tiles: list[bytes]) -> tuple[str, list[str]]:
    me = await bot.get_me()
    username = (me.username or "").lower()
    if not username:
        raise RuntimeError("У бота не найден username в Telegram")

    # Telegram requires the pack name to end with _by_<bot_username>.
    unique = time.time_ns() // 1_000_000
    pack_name = f"p{user_id}_{unique}_by_{username}"
    pack_title = f"Фото-пазл {grid}×{grid}"
    stickers = [
        InputSticker(
            sticker=BufferedInputFile(data, filename=f"tile_{index + 1}.webp"),
            format=StickerFormat.STATIC,
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
    ids = [sticker.custom_emoji_id for sticker in sticker_set.stickers]
    if len(ids) != len(tiles) or any(not item for item in ids):
        raise RuntimeError(
            f"Telegram вернул неполный набор: {len(ids)} из {len(tiles)} эмодзи."
        )
    return pack_name, ids


def build_emoji_preview(grid: int, emoji_ids: list[str]) -> tuple[str, list[MessageEntity]]:
    # Telegram entity offsets are measured in UTF-16 code units.
    placeholder = "😀"
    lines = []
    entities = []
    offset = 0
    for row in range(grid):
        lines.append(placeholder * grid)
        for _ in range(grid):
            entities.append(MessageEntity(
                type="custom_emoji",
                offset=offset,
                length=2,
                custom_emoji_id=emoji_ids[row * grid + (_)],
            ))
            offset += 2
        if row != grid - 1:
            offset += 1  # newline
    return "\n".join(lines), entities


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Я превращаю фотографию в набор настоящих Telegram Custom Emoji 🧩\n\n"
        "Отправь мне фотографию — я разрежу её на пазлы от 3×3 до 10×10, "
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
    await state.update_data(image_bytes=raw)
    await message.answer("Выбери, на сколько эмодзи разрезать фотографию:", reply_markup=grid_keyboard())


@dp.message(F.document)
async def receive_document(message: Message, state: FSMContext) -> None:
    document = message.document
    if not document.mime_type or not document.mime_type.startswith("image/"):
        await message.answer("Пришли фотографию как изображение или файлом.")
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
    await state.update_data(image_bytes=raw)
    await message.answer("Выбери размер пазла:", reply_markup=grid_keyboard())


@dp.callback_query(PuzzleState.choosing_grid, F.data.startswith("grid:"))
async def choose_grid(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    raw = data.get("image_bytes")
    if not raw:
        await state.clear()
        await callback.message.answer("Изображение потерялось. Отправь фотографию ещё раз.")
        return

    grid = int(callback.data.split(":", 1)[1])
    if grid < 3 or grid > 10:
        return

    await state.clear()
    status = await callback.message.answer(f"🧩 Готовлю {grid * grid} эмодзи… Это может занять немного времени.")
    await bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)

    try:
        tiles = await asyncio.to_thread(make_tiles, raw, grid)
        pack_name, emoji_ids = await create_custom_emoji_pack(callback.from_user.id, grid, tiles)

        preview_text, entities = build_emoji_preview(grid, emoji_ids)
        await callback.message.answer(
            f"Готово! {grid}×{grid} = {grid * grid} отдельных эмодзи.\n"
            "Ниже — собранная картинка. Добавь набор, чтобы использовать каждый фрагмент отдельно."
        )
        await callback.message.answer(
            preview_text,
            entities=entities,
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
    await message.answer("Отправь фотографию, и я превращу её в пазл из Telegram-эмодзи 🧩")


async def main() -> None:
    logger.info("Starting PONG EMOJI bot")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())

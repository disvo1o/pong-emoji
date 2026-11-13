# PONG EMOJI 🧩

Telegram bot that turns a photo into a real Telegram Custom Emoji puzzle pack.

## What it does

1. A user sends a photo or video (as a Telegram upload or file).
2. The bot offers grids from **3×3 to 10×10**.
3. It fits the full photo proportionally onto a transparent square canvas without cropping or stretching. Video input uses the first 3 seconds and becomes looping 100×100 VP9/WebM emoji tiles.
4. It creates a personal **Custom Emoji** set for that Telegram user.
5. It sends the assembled image made from the new custom emojis and a link to add the pack.

## Deploy on Railway

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. In Railway, create a project and deploy this repository from GitHub.
3. Open the service's **Variables** and add:
   - `BOT_TOKEN` = your BotFather token
4. Railway uses `railway.json` to run `python main.py`.
5. Open your bot in Telegram and send `/start`.

No database is needed for this first version. The pending photo is kept in the bot's in-memory FSM storage until the user chooses a grid.

## Technical notes

- Python 3.12, aiogram 3.22, Pillow, imageio-ffmpeg.
- Static photos are uploaded as 100×100 WEBP Custom Emoji. Video emoji use VP9/WebM, no audio, up to 3 seconds and 256 KB per tile.
- Telegram accepts 1–50 stickers in the initial `createNewStickerSet` call; remaining tiles are added one by one.
- Custom Emoji sticker sets support up to 200 items, so the largest 10×10 grid (100 pieces) fits in one pack.
- Telegram requires the pack short name to end with `_by_<bot_username>`; the bot generates a unique valid name automatically.
- Source images are limited to 15 MB and 30 megapixels.

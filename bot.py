import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("8632182947:AAEZf_4ku452RgFF6EyucbJTQOmiJuqCUD0")

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check <token>")
        return

    query = context.args[0]
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    response = requests.get(url)
    data = response.json()

    if not data.get("pairs"):
        await update.message.reply_text("Token not found.")
        return

    pair = data["pairs"][0]

    message = (
        f"Token: {pair['baseToken']['name']}\n"
        f"Price: ${pair['priceUsd']}\n"
        f"Liquidity: ${pair['liquidity']['usd']}\n"
        f"24H Volume: ${pair['volume']['h24']}"
    )

    await update.message.reply_text(message)

app = ApplicationBuilder().token(8632182947:AAEZf_4ku452RgFF6EyucbJTQOmiJuqCUD0).build()
app.add_handler(CommandHandler("check", check))

print("Bot is running...")
app.run_polling()

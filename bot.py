import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")

DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"

def to_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def pick_best_solana_pair(pairs: list[dict]) -> dict | None:
    sol = [p for p in pairs if p.get("chainId") == "solana"]
    if not sol:
        return None
    # Pick highest liquidity USD
    sol.sort(key=lambda p: to_float((p.get("liquidity") or {}).get("usd")), reverse=True)
    return sol[0]

def risk_score(pair: dict) -> tuple[int, str, list[str]]:
    """
    Simple heuristic score 0–100.
    Higher is better. Adds reasons to explain.
    """
    liq = to_float((pair.get("liquidity") or {}).get("usd"))
    vol24 = to_float((pair.get("volume") or {}).get("h24"))
    fdv = to_float(pair.get("fdv"), default=0.0)
    chg5m = to_float((pair.get("priceChange") or {}).get("m5"))
    chg1h = to_float((pair.get("priceChange") or {}).get("h1"))

    score = 50
    reasons = []

    # Liquidity (most important)
    if liq >= 200_000:
        score += 20; reasons.append("Strong liquidity")
    elif liq >= 50_000:
        score += 10; reasons.append("Decent liquidity")
    elif liq >= 20_000:
        score += 0; reasons.append("Low liquidity")
    else:
        score -= 15; reasons.append("Very low liquidity")

    # Volume vs liquidity
    if liq > 0:
        v_ratio = vol24 / liq
        if v_ratio >= 2.0:
            score += 10; reasons.append("High activity vs liquidity")
        elif v_ratio >= 0.5:
            score += 5; reasons.append("Healthy activity")
        elif v_ratio < 0.1:
            score -= 5; reasons.append("Weak activity")
    else:
        score -= 10; reasons.append("No liquidity data")

    # FDV vs liquidity (very rough rug/overvaluation proxy)
    if liq > 0 and fdv > 0:
        fdv_liq = fdv / liq
        if fdv_liq >= 500:
            score -= 10; reasons.append("FDV extremely high vs liquidity")
        elif fdv_liq >= 200:
            score -= 5; reasons.append("FDV high vs liquidity")

    # Short-term momentum flags (not “good/bad” alone—just signal)
    if chg5m >= 20 or chg1h >= 50:
        score -= 5; reasons.append("Sharp pump risk")
    if chg5m <= -20 or chg1h <= -50:
        score -= 5; reasons.append("Sharp dump risk")

    # Clamp
    score = max(0, min(100, score))

    if score >= 70:
        label = "LOW RISK (relative)"
    elif score >= 50:
        label = "MODERATE RISK"
    else:
        label = "HIGH RISK"

    return score, label, reasons

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/check <token symbol or address>\n"
        "/score <token symbol or address>\n"
        "Tip: addresses are more accurate than symbols."
    )

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check <token symbol or address>")
        return

    q = context.args[0].strip()
    r = requests.get(DEX_SEARCH_URL, params={"q": q}, timeout=15)
    data = r.json()

    pairs = data.get("pairs") or []
    best = pick_best_solana_pair(pairs)
    if not best:
        await update.message.reply_text("No Solana pair found for that query.")
        return

    base = (best.get("baseToken") or {}).get("name", "Unknown")
    symbol = (best.get("baseToken") or {}).get("symbol", "")
    price = best.get("priceUsd", "N/A")
    liq = to_float((best.get("liquidity") or {}).get("usd"))
    vol24 = to_float((best.get("volume") or {}).get("h24"))
    fdv = best.get("fdv", "N/A")
    dex = best.get("dexId", "N/A")
    url = best.get("url", "")

    msg = (
        f"{base} ({symbol})\n"
        f"DEX: {dex}\n"
        f"Price: ${price}\n"
        f"Liquidity: ${liq:,.0f}\n"
        f"24h Volume: ${vol24:,.0f}\n"
        f"FDV: {fdv}\n"
    )
    if url:
        msg += f"\nChart: {url}"

    await update.message.reply_text(msg)

async def score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /score <token symbol or address>")
        return

    q = context.args[0].strip()
    r = requests.get(DEX_SEARCH_URL, params={"q": q}, timeout=15)
    data = r.json()

    pairs = data.get("pairs") or []
    best = pick_best_solana_pair(pairs)
    if not best:
        await update.message.reply_text("No Solana pair found for that query.")
        return

    base = (best.get("baseToken") or {}).get("name", "Unknown")
    symbol = (best.get("baseToken") or {}).get("symbol", "")

    score, label, reasons = risk_score(best)

    liq = to_float((best.get("liquidity") or {}).get("usd"))
    vol24 = to_float((best.get("volume") or {}).get("h24"))
    chg5m = to_float((best.get("priceChange") or {}).get("m5"))
    chg1h = to_float((best.get("priceChange") or {}).get("h1"))
    chg24 = to_float((best.get("priceChange") or {}).get("h24"))
    url = best.get("url", "")

    reasons_txt = " • " + "\n • ".join(reasons) if reasons else ""
    msg = (
        f"{base} ({symbol})\n"
        f"Score: {score}/100 — {label}\n\n"
        f"Liquidity: ${liq:,.0f}\n"
        f"24h Volume: ${vol24:,.0f}\n"
        f"Change: 5m {chg5m:.1f}% | 1h {chg1h:.1f}% | 24h {chg24:.1f}%\n"
        f"{reasons_txt}"
    )
    if url:
        msg += f"\n\nChart: {url}"

    await update.message.reply_text(msg)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("score", score_cmd))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()

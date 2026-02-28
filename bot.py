import os
import time
import requests
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")

BASE = "https://api.dexscreener.com"
TIMEOUT = 20

# ---- Tuning knobs (edit as you like) ----
CHAIN_DEFAULT = "solana"

MIN_LIQ_USD = float(os.getenv("MIN_LIQ_USD", "20000"))          # filter rugs
MIN_VOL24_USD = float(os.getenv("MIN_VOL24_USD", "50000"))      # filter dead
MAX_FDV_LIQ = float(os.getenv("MAX_FDV_LIQ", "500"))            # extreme overvaluation cap
SCREEN_LIMIT = int(os.getenv("SCREEN_LIMIT", "25"))             # how many results to return
AUTOSCREEN_MINUTES = int(os.getenv("AUTOSCREEN_MINUTES", "2")) # periodic screening frequency


# -------------------------
# HTTP helpers
# -------------------------
def http_get(url: str, params: Optional[dict] = None) -> Any:
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def to_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def fmt_money(x: float) -> str:
    return f"${x:,.0f}"


def safe_list(x) -> List[Any]:
    # Normalize: list | {"data": list} | {"pairs": list} | dict -> []
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        if isinstance(x.get("data"), list):
            return x["data"]
        if isinstance(x.get("pairs"), list):
            return x["pairs"]
    return []


# -------------------------
# DexScreener endpoints
# -------------------------
def ds_search(q: str) -> Dict[str, Any]:
    return http_get(f"{BASE}/latest/dex/search", params={"q": q})


def ds_token_pools(chain_id: str, token_address: str) -> List[Dict[str, Any]]:
    # returns list (usually), but normalize anyway
    data = http_get(f"{BASE}/token-pairs/v1/{chain_id}/{token_address}")
    return safe_list(data)


def ds_pairs(chain_id: str, pair_id: str) -> Dict[str, Any]:
    return http_get(f"{BASE}/latest/dex/pairs/{chain_id}/{pair_id}")


def ds_boosts_latest() -> List[Dict[str, Any]]:
    return safe_list(http_get(f"{BASE}/token-boosts/latest/v1"))


def ds_boosts_top() -> List[Dict[str, Any]]:
    return safe_list(http_get(f"{BASE}/token-boosts/top/v1"))


def ds_profiles_latest() -> List[Dict[str, Any]]:
    return safe_list(http_get(f"{BASE}/token-profiles/latest/v1"))


def ds_takeovers_latest() -> List[Dict[str, Any]]:
    return safe_list(http_get(f"{BASE}/community-takeovers/latest/v1"))


def ds_ads_latest() -> List[Dict[str, Any]]:
    return safe_list(http_get(f"{BASE}/ads/latest/v1"))


def ds_orders(chain_id: str, token_address: str) -> List[Dict[str, Any]]:
    data = http_get(f"{BASE}/orders/v1/{chain_id}/{token_address}")
    return safe_list(data)


# -------------------------
# Analytics
# -------------------------
def pick_best_pair(pairs: List[Dict[str, Any]], chain_id: str = CHAIN_DEFAULT) -> Optional[Dict[str, Any]]:
    pairs = [p for p in pairs if p.get("chainId") == chain_id]
    if not pairs:
        return None
    pairs.sort(key=lambda p: to_float((p.get("liquidity") or {}).get("usd")), reverse=True)
    return pairs[0]


def age_minutes(pair_created_at: Any) -> Optional[float]:
    # DexScreener commonly returns milliseconds timestamp
    ts = to_float(pair_created_at, default=0.0)
    if ts <= 0:
        return None
    # handle seconds vs ms
    if ts > 10_000_000_000:  # ms
        ts /= 1000.0
    return (time.time() - ts) / 60.0


def compute_metrics(pair: Dict[str, Any]) -> Dict[str, Any]:
    liq = to_float((pair.get("liquidity") or {}).get("usd"))
    vol24 = to_float((pair.get("volume") or {}).get("h24"))
    fdv = to_float(pair.get("fdv"), default=0.0)

    chg = pair.get("priceChange") or {}
    chg5m = to_float(chg.get("m5"))
    chg1h = to_float(chg.get("h1"))
    chg24 = to_float(chg.get("h24"))

    v_liq = (vol24 / liq) if liq > 0 else 0.0
    fdv_liq = (fdv / liq) if (liq > 0 and fdv > 0) else 0.0

    age_m = age_minutes(pair.get("pairCreatedAt"))

    flags = []
    if liq < MIN_LIQ_USD:
        flags.append("Very low liquidity")
    if vol24 < MIN_VOL24_USD:
        flags.append("Low 24h volume")
    if fdv_liq and fdv_liq > 200:
        flags.append("FDV high vs liquidity")
    if fdv_liq and fdv_liq > 500:
        flags.append("FDV extreme vs liquidity")

    # Pump/dump pressure flags
    if (chg5m >= 15 and chg1h >= 30 and liq < 50_000):
        flags.append("Likely coordinated pump risk")
    if (chg5m <= -15 or chg1h <= -30):
        flags.append("Sharp dump risk")

    # Age flags
    if age_m is not None:
        if age_m < 60:
            flags.append("Ultra-new (<1h)")
        elif age_m < 24 * 60:
            flags.append("New (<24h)")

    # Score (0–100): health heuristic
    score = 50
    if liq >= 200_000: score += 20
    elif liq >= 50_000: score += 10
    elif liq < 20_000: score -= 15

    if v_liq >= 2.0: score += 10
    elif v_liq >= 0.5: score += 5
    elif v_liq < 0.1: score -= 5

    if fdv_liq >= 500: score -= 10
    elif fdv_liq >= 200: score -= 5

    if chg5m >= 20 or chg1h >= 50: score -= 5
    if chg5m <= -20 or chg1h <= -50: score -= 5

    score = max(0, min(100, score))
    label = "LOW RISK (relative)" if score >= 70 else "MODERATE RISK" if score >= 50 else "HIGH RISK"

    return {
        "liq": liq,
        "vol24": vol24,
        "fdv": fdv,
        "chg5m": chg5m,
        "chg1h": chg1h,
        "chg24": chg24,
        "v_liq": v_liq,
        "fdv_liq": fdv_liq,
        "age_m": age_m,
        "flags": flags,
        "score": score,
        "label": label,
    }


def candidate_passes(m: Dict[str, Any]) -> bool:
    # Screening filters (you can relax/tighten)
    if m["liq"] < MIN_LIQ_USD:
        return False
    if m["vol24"] < MIN_VOL24_USD:
        return False
    if m["fdv_liq"] and m["fdv_liq"] > MAX_FDV_LIQ:
        return False
    return True


# -------------------------
# Screening engine
# -------------------------
def fetch_boost_candidates(chain_id: str = CHAIN_DEFAULT) -> List[Dict[str, Any]]:
    # Combine boosted lists
    latest = ds_boosts_latest()
    top = ds_boosts_top()

    # Token boosts objects typically include chainId + tokenAddress
    raw = latest + top

    # Keep unique tokens on the chain
    seen = set()
    tokens = []
    for it in raw:
        c = it.get("chainId")
        addr = it.get("tokenAddress")
        if not c or not addr:
            continue
        if c != chain_id:
            continue
        key = (c, addr)
        if key in seen:
            continue
        seen.add(key)
        tokens.append(it)

    return tokens


def screen_tokens(chain_id: str = CHAIN_DEFAULT, limit: int = SCREEN_LIMIT) -> List[Dict[str, Any]]:
    candidates = fetch_boost_candidates(chain_id)

    scored = []
    for t in candidates:
        token_addr = t["tokenAddress"]

        pools = ds_token_pools(chain_id, token_addr)
        best = pick_best_pair(pools, chain_id)
        if not best:
            continue

        m = compute_metrics(best)

        # Boost manipulation flag: boosted + low liquidity
        if m["liq"] < 40_000:
            m["flags"].append("Boost + low liquidity (marketing pump risk)")

        if not candidate_passes(m):
            continue

        scored.append({
            "tokenAddress": token_addr,
            "pair": best,
            "m": m
        })

    # Rank: score desc, then liquidity desc
    scored.sort(key=lambda x: (x["m"]["score"], x["m"]["liq"]), reverse=True)
    return scored[:limit]


def format_candidate(item: Dict[str, Any], idx: int) -> str:
    p = item["pair"]
    m = item["m"]
    base = (p.get("baseToken") or {}).get("symbol", "UNK")
    name = (p.get("baseToken") or {}).get("name", "Unknown")
    url = p.get("url", "")
    age_txt = "N/A"
    if m["age_m"] is not None:
        mins = m["age_m"]
        if mins < 120:
            age_txt = f"{mins:.0f}m"
        elif mins < 48*60:
            age_txt = f"{mins/60:.1f}h"
        else:
            age_txt = f"{mins/1440:.1f}d"

    flags = ""
    if m["flags"]:
        flags = " | ⚠️ " + "; ".join(m["flags"][:3])

    return (
        f"{idx}. {name} ({base})\n"
        f"Score: {m['score']}/100 ({m['label']}) | Age: {age_txt}\n"
        f"Liq: {fmt_money(m['liq'])} | Vol24: {fmt_money(m['vol24'])}\n"
        f"FDV/Liq: {m['fdv_liq']:.1f} | Vol/Liq: {m['v_liq']:.2f} | "
        f"Δ5m {m['chg5m']:.1f}% Δ1h {m['chg1h']:.1f}% Δ24h {m['chg24']:.1f}%"
        f"{flags}\n"
        f"{url}"
    )


# -------------------------
# Telegram handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Solana DexScreener Analytics Bot\n\n"
        "Core:\n"
        "  /check <symbol|address> [chainId]\n"
        "  /score <symbol|address> [chainId]\n"
        "  /pools <tokenAddress> [chainId]\n"
        "  /orders <tokenAddress> [chainId]\n\n"
        "Screening:\n"
        "  /screen [chainId]  -> ranked boosted candidates\n"
        "  /autoscreen on|off [chainId]\n\n"
        "Tip: addresses are more accurate than symbols."
    )


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check <symbol|address> [chainId]")
        return

    q = context.args[0].strip()
    chain = context.args[1].strip() if len(context.args) >= 2 else CHAIN_DEFAULT

    try:
        data = ds_search(q)
        best = pick_best_pair(safe_list(data.get("pairs")), chain)
        if not best:
            await update.message.reply_text(f"No pair found for chain '{chain}'.")
            return

        m = compute_metrics(best)

        base = (best.get("baseToken") or {}).get("name", "Unknown")
        symbol = (best.get("baseToken") or {}).get("symbol", "")
        dex = best.get("dexId", "N/A")
        price = best.get("priceUsd", "N/A")
        url = best.get("url", "")

        msg = (
            f"{base} ({symbol}) [{chain}]\n"
            f"DEX: {dex}\n"
            f"Price: ${price}\n"
            f"Liquidity: {fmt_money(m['liq'])}\n"
            f"24h Volume: {fmt_money(m['vol24'])}\n"
            f"FDV: {best.get('fdv','N/A')}\n"
        )
        if url:
            msg += f"\nChart: {url}"

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /score <symbol|address> [chainId]")
        return

    q = context.args[0].strip()
    chain = context.args[1].strip() if len(context.args) >= 2 else CHAIN_DEFAULT

    try:
        data = ds_search(q)
        best = pick_best_pair(safe_list(data.get("pairs")), chain)
        if not best:
            await update.message.reply_text(f"No pair found for chain '{chain}'.")
            return

        m = compute_metrics(best)
        base = (best.get("baseToken") or {}).get("name", "Unknown")
        symbol = (best.get("baseToken") or {}).get("symbol", "")
        url = best.get("url", "")

        flags = "\n".join([f"• {f}" for f in m["flags"]]) if m["flags"] else "• None"

        msg = (
            f"{base} ({symbol}) [{chain}]\n"
            f"Score: {m['score']}/100 — {m['label']}\n\n"
            f"Liquidity: {fmt_money(m['liq'])}\n"
            f"24h Volume: {fmt_money(m['vol24'])}\n"
            f"FDV/Liq: {m['fdv_liq']:.1f}\n"
            f"Vol/Liq: {m['v_liq']:.2f}\n"
            f"Change: 5m {m['chg5m']:.1f}% | 1h {m['chg1h']:.1f}% | 24h {m['chg24']:.1f}%\n\n"
            f"Flags:\n{flags}"
        )
        if url:
            msg += f"\n\nChart: {url}"

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def pools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /pools <tokenAddress> [chainId]")
        return

    token = context.args[0].strip()
    chain = context.args[1].strip() if len(context.args) >= 2 else CHAIN_DEFAULT

    try:
        items = ds_token_pools(chain, token)
        if not items:
            await update.message.reply_text("No pools found.")
            return

        items.sort(key=lambda p: to_float((p.get("liquidity") or {}).get("usd")), reverse=True)
        top = items[:5]

        lines = [f"Top pools for {token} [{chain}] (top 5 by liquidity):"]
        for p in top:
            dex = p.get("dexId", "N/A")
            pair = p.get("pairAddress", "N/A")
            liq = to_float((p.get("liquidity") or {}).get("usd"))
            price = p.get("priceUsd", "N/A")
            url = p.get("url", "")
            lines.append(f"• {dex} | liq {fmt_money(liq)} | price ${price} | pair {pair}")
            if url:
                lines.append(f"  {url}")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /orders <tokenAddress> [chainId]")
        return

    token = context.args[0].strip()
    chain = context.args[1].strip() if len(context.args) >= 2 else CHAIN_DEFAULT

    try:
        items = ds_orders(chain, token)
        if not items:
            await update.message.reply_text("No paid orders found.")
            return

        lines = [f"Orders for {token} [{chain}] (preview):"]
        for it in items[: min(8, len(items))]:
            if not isinstance(it, dict):
                continue
            lines.append(
                f"• type={it.get('type')} | status={it.get('status')} | paymentTs={it.get('paymentTimestamp')}"
            )

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chain = context.args[0].strip() if context.args else CHAIN_DEFAULT

    try:
        results = screen_tokens(chain_id=chain, limit=SCREEN_LIMIT)
        if not results:
            await update.message.reply_text(
                f"No candidates met filters.\n"
                f"(Try lowering MIN_LIQ_USD / MIN_VOL24_USD or raising MAX_FDV_LIQ.)"
            )
            return

        msg_parts = [f"Screen results [{chain}] (filters: liq≥{fmt_money(MIN_LIQ_USD)}, vol24≥{fmt_money(MIN_VOL24_USD)}, fdv/liq≤{MAX_FDV_LIQ})\n"]
        for i, item in enumerate(results, start=1):
            msg_parts.append(format_candidate(item, i))
            msg_parts.append("")  # spacer

        await update.message.reply_text("\n".join(msg_parts[: 1 + (SCREEN_LIMIT * 5)]))

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


# ---- Auto-screen scheduling (per chat) ----
AUTOSCREEN_JOBS: Dict[int, Any] = {}

async def autoscreen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /autoscreen on|off [chainId]")
        return

    mode = context.args[0].lower()
    chain = context.args[1].strip() if len(context.args) >= 2 else CHAIN_DEFAULT
    chat_id = update.effective_chat.id

    if mode == "off":
        job = AUTOSCREEN_JOBS.pop(chat_id, None)
        if job:
            job.schedule_removal()
        await update.message.reply_text("Auto-screen disabled.")
        return

    # mode == on
    if chat_id in AUTOSCREEN_JOBS:
        await update.message.reply_text("Auto-screen is already ON.")
        return

    async def job_callback(ctx: ContextTypes.DEFAULT_TYPE):
        try:
            results = screen_tokens(chain_id=chain, limit=min(5, SCREEN_LIMIT))
            if not results:
                return
            lines = [f"Auto-screen [{chain}] top picks (heuristic):"]
            for i, item in enumerate(results, start=1):
                p = item["pair"]
                m = item["m"]
                sym = (p.get("baseToken") or {}).get("symbol", "UNK")
                url = p.get("url", "")
                flags = "; ".join(m["flags"][:2]) if m["flags"] else "no major flags"
                lines.append(
                    f"{i}) {sym} | score {m['score']}/100 | liq {fmt_money(m['liq'])} | vol24 {fmt_money(m['vol24'])} | {flags}\n{url}"
                )
            await ctx.bot.send_message(chat_id=chat_id, text="\n".join(lines))
        except Exception:
            # keep silent; avoid spamming errors
            return

    job = context.job_queue.run_repeating(job_callback, interval=AUTOSCREEN_MINUTES * 60, first=5)
    AUTOSCREEN_JOBS[chat_id] = job

    await update.message.reply_text(
        f"Auto-screen enabled: every {AUTOSCREEN_MINUTES} minutes on [{chain}].\n"
        f"Use /autoscreen off to stop."
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it in Railway Variables.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("score", score_cmd))
    app.add_handler(CommandHandler("pools", pools))
    app.add_handler(CommandHandler("orders", orders))
    app.add_handler(CommandHandler("screen", screen))
    app.add_handler(CommandHandler("autoscreen", autoscreen))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

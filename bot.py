import os
import re
import requests
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")

BASE = "https://api.dexscreener.com"
TIMEOUT = 20

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

MAX_SINGLE_HOLDER_PCT = float(os.getenv("MAX_SINGLE_HOLDER_PCT", "7.0"))
MAX_TOP3_PCT = float(os.getenv("MAX_TOP3_PCT", "25.0"))
MAX_TOP10_PCT = float(os.getenv("MAX_TOP10_PCT", "60.0"))
MIN_AGE_HOURS = float(os.getenv("MIN_AGE_HOURS", "36.0"))

HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/?api-key="

def helius_rpc(method: str, params: list):
    if not HELIUS_API_KEY:
        raise RuntimeError("HELIUS_API_KEY is missing in Railway Variables.")
    url = f"{HELIUS_RPC_URL}{HELIUS_API_KEY}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(url, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j.get("result")

def get_supply_ui(mint: str) -> float:
    res = helius_rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
    val = (res or {}).get("value", {})
    amount = float(val.get("amount", "0"))
    decimals = int(val.get("decimals", 0))
    return amount / (10 ** decimals) if decimals >= 0 else 0.0

def holder_concentration(mint: str) -> dict:
    """
    Returns single/top3/top10 percentages based on token accounts (not perfect per-wallet,
    but strong enough for screening).
    """
    res = helius_rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    supply = get_supply_ui(mint)
    if supply <= 0:
        return {"single": 0.0, "top3": 0.0, "top10": 0.0}

    arr = (res or {}).get("value", []) or []
    amounts = []
    for it in arr[:10]:
        try:
            amounts.append(float(it.get("amount", "0")))
        except Exception:
            amounts.append(0.0)

    single = (amounts[0] / supply * 100.0) if amounts else 0.0
    top3 = (sum(amounts[:3]) / supply * 100.0) if len(amounts) >= 3 else single
    top10 = (sum(amounts[:10]) / supply * 100.0) if amounts else single

    return {"single": single, "top3": top3, "top10": top10}

def token_authorities(mint: str) -> dict:
    """
    Checks mint + freeze authority. If either exists, token can be manipulated more easily.
    """
    res = helius_rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    value = (res or {}).get("value")
    if not value:
        return {"mintAuthority": None, "freezeAuthority": None}

    parsed = (((value.get("data") or {}).get("parsed") or {}).get("info") or {})
    # For SPL Token mints, these keys typically exist in jsonParsed
    return {
        "mintAuthority": parsed.get("mintAuthority"),
        "freezeAuthority": parsed.get("freezeAuthority"),
    }

def age_ok(pair: dict) -> tuple[bool, str]:
    # Uses DexScreener pairCreatedAt (ms or sec). Enforces >= MIN_AGE_HOURS.
    ts = to_float(pair.get("pairCreatedAt"), 0.0)
    if ts <= 0:
        return False, "Missing age data"
    if ts > 10_000_000_000:  # ms -> sec
        ts /= 1000.0
    age_hours = (time.time() - ts) / 3600.0
    if age_hours < MIN_AGE_HOURS:
        return False, f"Too new ({age_hours:.1f}h < {MIN_AGE_HOURS:.0f}h)"
    return True, f"{age_hours:.1f}h"

# -------------------------
# Helpers
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


def short(s: str, n: int = 160) -> str:
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def is_probably_address(s: str) -> bool:
    # Rough: base58-ish length check for Solana token addresses (not perfect; good enough for UX).
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", s))


def pick_best_pair(pairs: List[Dict[str, Any]], chain_id: Optional[str] = "solana") -> Optional[Dict[str, Any]]:
    if chain_id:
        pairs = [p for p in pairs if p.get("chainId") == chain_id]
    if not pairs:
        return None
    # pick highest liquidity USD
    pairs.sort(key=lambda p: to_float((p.get("liquidity") or {}).get("usd")), reverse=True)
    return pairs[0]


def risk_score(pair: Dict[str, Any]) -> Tuple[int, str, List[str]]:
    """
    Heuristic score 0–100 (NOT a trading signal).
    Higher = generally healthier market structure (liquidity/activity).
    """
    liq = to_float((pair.get("liquidity") or {}).get("usd"))
    vol24 = to_float((pair.get("volume") or {}).get("h24"))
    fdv = to_float(pair.get("fdv"), 0.0)
    chg5m = to_float((pair.get("priceChange") or {}).get("m5"))
    chg1h = to_float((pair.get("priceChange") or {}).get("h1"))

    score = 50
    reasons: List[str] = []

    # Liquidity
    if liq >= 200_000:
        score += 20; reasons.append("Strong liquidity")
    elif liq >= 50_000:
        score += 10; reasons.append("Decent liquidity")
    elif liq >= 20_000:
        reasons.append("Low liquidity")
    else:
        score -= 15; reasons.append("Very low liquidity")

    # Volume vs liquidity
    if liq > 0:
        v_ratio = vol24 / liq
        if v_ratio >= 2.0:
            score += 10; reasons.append("Very high activity vs liquidity")
        elif v_ratio >= 0.5:
            score += 5; reasons.append("Healthy activity")
        elif v_ratio < 0.1:
            score -= 5; reasons.append("Weak activity")
    else:
        score -= 10; reasons.append("No liquidity data")

    # FDV vs liquidity (rough)
    if liq > 0 and fdv > 0:
        fdv_liq = fdv / liq
        if fdv_liq >= 500:
            score -= 10; reasons.append("FDV extremely high vs liquidity")
        elif fdv_liq >= 200:
            score -= 5; reasons.append("FDV high vs liquidity")

    # Sudden moves
    if chg5m >= 20 or chg1h >= 50:
        score -= 5; reasons.append("Sharp pump risk")
    if chg5m <= -20 or chg1h <= -50:
        score -= 5; reasons.append("Sharp dump risk")

    score = max(0, min(100, score))
    label = "LOW RISK (relative)" if score >= 70 else "MODERATE RISK" if score >= 50 else "HIGH RISK"
    return score, label, reasons


# -------------------------
# DexScreener endpoints (from reference)
# -------------------------
def ds_search(q: str) -> Dict[str, Any]:
    return http_get(f"{BASE}/latest/dex/search", params={"q": q})  # :contentReference[oaicite:1]{index=1}


def ds_pairs(chain_id: str, pair_id: str) -> Dict[str, Any]:
    return http_get(f"{BASE}/latest/dex/pairs/{chain_id}/{pair_id}")  # :contentReference[oaicite:2]{index=2}


def ds_token_pools(chain_id: str, token_address: str) -> List[Dict[str, Any]]:
    return http_get(f"{BASE}/token-pairs/v1/{chain_id}/{token_address}")  # :contentReference[oaicite:3]{index=3}


def ds_tokens_batch(chain_id: str, token_addresses_csv: str) -> List[Dict[str, Any]]:
    return http_get(f"{BASE}/tokens/v1/{chain_id}/{token_addresses_csv}")  # :contentReference[oaicite:4]{index=4}


def ds_profiles_latest() -> List[Dict[str, Any]]:
    return http_get(f"{BASE}/token-profiles/latest/v1")  # :contentReference[oaicite:5]{index=5}


def ds_takeovers_latest() -> List[Dict[str, Any]]:
    return http_get(f"{BASE}/community-takeovers/latest/v1")  # :contentReference[oaicite:6]{index=6}


def ds_ads_latest() -> List[Dict[str, Any]]:
    return http_get(f"{BASE}/ads/latest/v1")  # :contentReference[oaicite:7]{index=7}


def ds_boosts_latest() -> List[Dict[str, Any]]:
    return http_get(f"{BASE}/token-boosts/latest/v1")  # :contentReference[oaicite:8]{index=8}


def ds_boosts_top() -> List[Dict[str, Any]]:
    return http_get(f"{BASE}/token-boosts/top/v1")  # :contentReference[oaicite:9]{index=9}

def screen_tokens(chain_id: str = "solana", limit: int = 10) -> List[Dict[str, Any]]:
    boosted = ds_boosts_latest()

    if not isinstance(boosted, list):
        return []

    candidates = []

    for item in boosted:
        if not isinstance(item, dict):
            continue

        if item.get("chainId") != chain_id:
            continue

        token_addr = item.get("tokenAddress")
        if not token_addr:
            continue

        try:
            pools = ds_token_pools(chain_id, token_addr)
            if not isinstance(pools, list):
                continue

            best = pick_best_pair(pools, chain_id)
            if not best:
                continue

            score, label, reasons = risk_score(best)

            candidates.append({
                "pair": best,
                "score": score,
                "label": label,
                "reasons": reasons
            })

        except Exception:
            continue

    # sort by score then liquidity
    candidates.sort(
        key=lambda x: (
            x["score"],
            to_float((x["pair"].get("liquidity") or {}).get("usd"))
        ),
        reverse=True
    )

    return candidates[:limit]
    
def ds_orders(chain_id: str, token_address: str):
    data = http_get(f"{BASE}/orders/v1/{chain_id}/{token_address}")

    # Normalize to list
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], list):
            return data["data"]
        return []

    return []
    
def screen_tokens(chain_id: str = "solana", limit: int = 10) -> List[Dict[str, Any]]:
    boosted = ds_boosts_latest()
    if not isinstance(boosted, list):
        return []

    seen = set()
    candidates: List[Dict[str, Any]] = []

    for item in boosted:
        if not isinstance(item, dict):
            continue
        if item.get("chainId") != chain_id:
            continue

        token_addr = item.get("tokenAddress")
        if not token_addr or token_addr in seen:
            continue
        seen.add(token_addr)

        try:
            pools = ds_token_pools(chain_id, token_addr)
            if not isinstance(pools, list) or not pools:
                continue

            best = pick_best_pair(pools, chain_id)
            if not best:
                continue

            # ✅ AGE FILTER: >= 36 hours (or MIN_AGE_HOURS)
            ok, age_info = age_ok(best)
            if not ok:
                continue

            # Identify mint (DexScreener base token address)
            mint = ((best.get("baseToken") or {}).get("address")) or token_addr

            # ✅ HOLDER CONCENTRATION (Helius)
            hc = holder_concentration(mint)
            if hc["single"] > MAX_SINGLE_HOLDER_PCT:
                continue
             SOFT PENALTIES
            penalty = 0
            soft_flags = []
            
            if hc["top3"] > MAX_TOP3_PCT:
                penalty += 5
                soft_flags.append("High top3 concentration")
            
            if hc["top10"] > MAX_TOP10_PCT:
                penalty += 5
                soft_flags.append("High top10 concentration")

            # ✅ AUTHORITIES (Helius)
            auth = token_authorities(mint)
            # Strict: reject if mint authority exists (can mint more supply)
            # HARD REJECT if mint authority exists
            if auth.get("mintAuthority"):
                continue
            
            # SOFT penalty if freeze authority exists
            if auth.get("freezeAuthority"):
                penalty += 5
                soft_flags.append("Freeze authority enabled")

            score, label, reasons = risk_score(best)
            score = max(0, score - penalty)

            liq = to_float((best.get("liquidity") or {}).get("usd"))
            vol24 = to_float((best.get("volume") or {}).get("h24"))

            candidates.append({
                "pair": best,
                "mint": mint,
                "age": age_info,
                "holders": hc,
                "auth": auth,
                "score": score,
                "label": label,
                "reasons": reasons,
                "liq": liq,
                "vol24": vol24,
                "soft_flags": soft_flags,    
            })

        except Exception:
            continue

    # Rank by score, then liquidity
    candidates.sort(key=lambda x: (x["score"], x["liq"]), reverse=True)
    return candidates[:limit]

# -------------------------
# Telegram commands
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "DexScreener Bot (Solana-first)\n\n"
        "Core:\n"
        "  /check <symbol|address> [chainId]\n"
        "  /score <symbol|address> [chainId]\n"
        "  /pools <tokenAddress> [chainId]\n"
        "  /pair <pairId> [chainId]\n"
        "  /tokens <addr1,addr2,...> [chainId]\n\n"
        "Discovery/Meta:\n"
        "  /boosts_latest\n"
        "  /boosts_top\n"
        "  /profiles_latest\n"
        "  /takeovers_latest\n"
        "  /ads_latest\n"
        "  /orders <tokenAddress> [chainId]\n\n"
        "Tip: addresses are more accurate than symbols."
    )


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /check <symbol|address> [chainId]")
        return

    q = context.args[0].strip()
    chain = context.args[1].strip() if len(context.args) >= 2 else "solana"

    try:
        data = ds_search(q)
        best = pick_best_pair(data.get("pairs") or [], chain)
        if not best:
            await update.message.reply_text(f"No pair found for chain '{chain}'.")
            return

        base = (best.get("baseToken") or {}).get("name", "Unknown")
        symbol = (best.get("baseToken") or {}).get("symbol", "")
        dex = best.get("dexId", "N/A")
        price = best.get("priceUsd", "N/A")
        liq = to_float((best.get("liquidity") or {}).get("usd"))
        vol24 = to_float((best.get("volume") or {}).get("h24"))
        fdv = best.get("fdv", "N/A")
        url = best.get("url", "")

        msg = (
            f"{base} ({symbol}) [{chain}]\n"
            f"DEX: {dex}\n"
            f"Price: ${price}\n"
            f"Liquidity: {fmt_money(liq)}\n"
            f"24h Volume: {fmt_money(vol24)}\n"
            f"FDV: {fdv}\n"
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
    chain = context.args[1].strip() if len(context.args) >= 2 else "solana"

    try:
        data = ds_search(q)
        best = pick_best_pair(data.get("pairs") or [], chain)
        if not best:
            await update.message.reply_text(f"No pair found for chain '{chain}'.")
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

        msg = (
            f"{base} ({symbol}) [{chain}]\n"
            f"Score: {score}/100 — {label}\n"
            f"(Heuristic health check, not financial advice)\n\n"
            f"Liquidity: {fmt_money(liq)}\n"
            f"24h Volume: {fmt_money(vol24)}\n"
            f"Change: 5m {chg5m:.1f}% | 1h {chg1h:.1f}% | 24h {chg24:.1f}%\n"
        )
        if reasons:
            msg += "\nReasons:\n" + "\n".join([f"• {r}" for r in reasons])
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
    chain = context.args[1].strip() if len(context.args) >= 2 else "solana"

    try:
        items = ds_token_pools(chain, token)
        if not items:
            await update.message.reply_text("No pools found.")
            return

        # top 5 by liquidity
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


async def pair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /pair <pairId> [chainId]")
        return

    pair_id = context.args[0].strip()
    chain = context.args[1].strip() if len(context.args) >= 2 else "solana"

    try:
        data = ds_pairs(chain, pair_id)
        pairs = data.get("pairs") or []
        if not pairs:
            await update.message.reply_text("Pair not found.")
            return

        p = pairs[0]
        base = (p.get("baseToken") or {}).get("name", "Unknown")
        symbol = (p.get("baseToken") or {}).get("symbol", "")
        dex = p.get("dexId", "N/A")
        price = p.get("priceUsd", "N/A")
        liq = to_float((p.get("liquidity") or {}).get("usd"))
        vol24 = to_float((p.get("volume") or {}).get("h24"))
        url = p.get("url", "")

        msg = (
            f"Pair lookup [{chain}]\n"
            f"{base} ({symbol})\n"
            f"DEX: {dex}\n"
            f"Price: ${price}\n"
            f"Liquidity: {fmt_money(liq)}\n"
            f"24h Volume: {fmt_money(vol24)}\n"
        )
        if url:
            msg += f"\nChart: {url}"

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /tokens <addr1,addr2,...> [chainId]  (max 30 addresses)")
        return

    addrs = context.args[0].strip()
    chain = context.args[1].strip() if len(context.args) >= 2 else "solana"

    try:
        items = ds_tokens_batch(chain, addrs)
        if not items:
            await update.message.reply_text("No results.")
            return

        # show top 10 by liquidity
        items.sort(key=lambda p: to_float((p.get("liquidity") or {}).get("usd")), reverse=True)
        top = items[:10]

        lines = [f"Tokens batch [{chain}] (top 10 by liquidity):"]
        for p in top:
            base = (p.get("baseToken") or {}).get("symbol", "UNK")
            liq = to_float((p.get("liquidity") or {}).get("usd"))
            price = p.get("priceUsd", "N/A")
            pair_addr = p.get("pairAddress", "N/A")
            lines.append(f"• {base} | price ${price} | liq {fmt_money(liq)} | pair {pair_addr}")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


def _list_preview(items, title: str, n: int = 8) -> str:
    # Normalize response structure safely

    if isinstance(items, dict):
        # Try common container keys
        if "data" in items:
            items = items["data"]
        elif "pairs" in items:
            items = items["pairs"]
        else:
            # If dict but not structured as expected, wrap it
            items = [items]

    # If still not list, force into list
    if not isinstance(items, list):
        items = [items]

    lines = [title]

    # Safe slicing
    for it in items[: min(n, len(items))]:
        if not isinstance(it, dict):
            continue

        chain = it.get("chainId", "")
        addr = it.get("tokenAddress", "")
        typ = it.get("type", "")
        date = it.get("date") or it.get("claimDate") or ""
        url = it.get("url", "")

        bits = []
        if chain:
            bits.append(chain)
        if addr:
            bits.append(addr)
        if typ:
            bits.append(f"type={typ}")
        if date:
            bits.append(f"date={date}")

        lines.append("• " + " | ".join(bits))

        if url:
            lines.append(f"  {url}")

    return "\n".join(lines)
async def boosts_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = ds_boosts_latest()
        await update.message.reply_text(_list_preview(items, "Latest boosted tokens (preview):"))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def boosts_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = ds_boosts_top()
        await update.message.reply_text(_list_preview(items, "Top boosted tokens (preview):"))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def profiles_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = ds_profiles_latest()
        await update.message.reply_text(_list_preview(items, "Latest token profiles (preview):"))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def takeovers_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = ds_takeovers_latest()
        await update.message.reply_text(_list_preview(items, "Latest community takeovers (preview):"))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def ads_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        items = ds_ads_latest()
        await update.message.reply_text(_list_preview(items, "Latest ads (preview):"))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /orders <tokenAddress> [chainId]")
        return

    token = context.args[0].strip()
    chain = context.args[1].strip() if len(context.args) >= 2 else "solana"

    try:
        items = ds_orders(chain, token)

        if not items:
            await update.message.reply_text("No paid orders found.")
            return

        lines = [f"Orders for {token} [{chain}] (preview):"]

        for it in items[:min(8, len(items))]:
            if not isinstance(it, dict):
                continue

            lines.append(
                f"• type={it.get('type')} | "
                f"status={it.get('status')} | "
                f"paymentTs={it.get('paymentTimestamp')}"
            )

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chain = context.args[0].strip() if context.args else "solana"

    try:
        results = screen_tokens(chain_id=chain, limit=10)

        if not results:
            await update.message.reply_text("No candidates found.")
            return

        lines = [f"Screen Results [{chain}] (boosted tokens):\n"]

        for idx, item in enumerate(results, start=1):
            p = item["pair"]
            base = (p.get("baseToken") or {}).get("symbol", "UNK")
            name = (p.get("baseToken") or {}).get("name", "Unknown")
            liq = to_float((p.get("liquidity") or {}).get("usd"))
            vol24 = to_float((p.get("volume") or {}).get("h24"))
            url = p.get("url", "")

            lines.append(
                f"{idx}) {name} ({base})\n"
                f"Score: {item['score']}/100 — {item['label']}\n"
                f"Liq: {fmt_money(liq)} | Vol24: {fmt_money(vol24)}"
            )

            if url:
                lines.append(url)

            lines.append("")

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        
async def screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chain = context.args[0].strip() if context.args else "solana"

    try:
        results = screen_tokens(chain_id=chain, limit=10)
        if not results:
            await update.message.reply_text(
                f"No candidates passed filters.\n"
                f"Rules: age≥{MIN_AGE_HOURS:.0f}h, single≤{MAX_SINGLE_HOLDER_PCT}%, top3≤{MAX_TOP3_PCT}%, top10≤{MAX_TOP10_PCT}%, no mint/freeze authority."
            )
            return

        lines = [f"✅ Screen Results [{chain}] (Top 10)\n"
                 f"Filters: age≥{MIN_AGE_HOURS:.0f}h | single≤{MAX_SINGLE_HOLDER_PCT}% | top3≤{MAX_TOP3_PCT}% | top10≤{MAX_TOP10_PCT}% | no mint/freeze auth\n"]

        for i, item in enumerate(results, start=1):
            p = item["pair"]
            name = (p.get("baseToken") or {}).get("name", "Unknown")
            sym = (p.get("baseToken") or {}).get("symbol", "UNK")
            url = p.get("url", "")

            hc = item["holders"]
            lines.append(
                f"{i}) {name} ({sym}) | Score {item['score']}/100 ({item['label']}) | Age {item['age']}\n"
                f"   Liq {fmt_money(item['liq'])} | Vol24 {fmt_money(item['vol24'])}\n"
                f"   Holders: single {hc['single']:.2f}% | top3 {hc['top3']:.2f}% | top10 {hc['top10']:.2f}%\n"
                f"{url}"
            )
            
            if item["soft_flags"]:
            lines.append("   ⚠️ " + "; ".join(item["soft_flags"]))

        await update.message.reply_text("\n\n".join(lines))

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it in Railway Variables.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("score", score_cmd))
    app.add_handler(CommandHandler("pools", pools))
    app.add_handler(CommandHandler("pair", pair))
    app.add_handler(CommandHandler("tokens", tokens))
    app.add_handler(CommandHandler("screen", screen))
    app.add_handler(CommandHandler("screen", screen))

    app.add_handler(CommandHandler("boosts_latest", boosts_latest))
    app.add_handler(CommandHandler("boosts_top", boosts_top))
    app.add_handler(CommandHandler("profiles_latest", profiles_latest))
    app.add_handler(CommandHandler("takeovers_latest", takeovers_latest))
    app.add_handler(CommandHandler("ads_latest", ads_latest))
    app.add_handler(CommandHandler("orders", orders))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

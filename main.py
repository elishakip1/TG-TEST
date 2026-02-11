import re
import uuid
import yaml
import logging
import requests
import asyncio
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logger = logging.getLogger("ULTIMATESHOP")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_file = logging.FileHandler("bot.log", mode="a", encoding="utf-8")
_file.setLevel(logging.INFO)
_file.setFormatter(_fmt)
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(_fmt)
logger.handlers.clear()
logger.addHandler(_file)
logger.addHandler(_console)

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

BOT_TOKEN = config["telegram"]["token"]
ADMIN_ID = int(config.get("telegram", {}).get("admin_id", 2124577519))
DV_BASE_URL = config["server"]["dv_base_url"].rstrip("/")
DV_API_KEY = config["server"]["dv_api_key"]
SCAN_MINUTES = int(config.get("scanner", {}).get("interval_minutes", 5))
ADDRESS_SCAN_HOURS = int(config.get("scanner", {}).get("address_lookback_hours", 36))
MIN_CONFIRMATIONS = int(config.get("scanner", {}).get("min_confirmations", 2))

supabase_url = config["supabase"]["url"]
supabase_key = config["supabase"]["key"]
supabase: Client = create_client(supabase_url, supabase_key)

CURRENCY_MAP = {
    "BTC": "BTC",
    "LTC": "LTC",
    "USDT (TRC20)": "USDT_TRC20",
    "BNB (BEP20)": "BNB_BEP20",
}

COINGECKO_IDS = {
    "BTC": "bitcoin",
    "LTC": "litecoin",
    "USDT_TRC20": "tether",
    "BNB_BEP20": "binancecoin",
}

FALLBACK_EXCHANGE_RATES = {
    "BTC": 45000.0,
    "LTC": 80.0,
    "USDT_TRC20": 1.0,
    "BNB_BEP20": 600.0,
}


def get_exchange_rates_usd() -> dict:
    ids = ",".join(sorted(set(COINGECKO_IDS.values())))
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd"},
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning("CoinGecko price fetch failed status=%s; using fallback rates", r.status_code)
            return FALLBACK_EXCHANGE_RATES.copy()

        payload = r.json()
        rates = {}
        for currency, cg_id in COINGECKO_IDS.items():
            rate = float(payload.get(cg_id, {}).get("usd", 0) or 0)
            rates[currency] = rate if rate > 0 else FALLBACK_EXCHANGE_RATES[currency]
        return rates
    except Exception as exc:
        logger.warning("CoinGecko price fetch error: %s; using fallback rates", exc)
        return FALLBACK_EXCHANGE_RATES.copy()


def kb_home(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("➕ Deposit"), KeyboardButton("💰 Balance")],
        [KeyboardButton("🛒 Shop")],
        [KeyboardButton("👥 Invite"), KeyboardButton("ℹ️ About")],
        [KeyboardButton("🎧 Support")],
    ]
    if is_admin:
        rows.append([KeyboardButton("📨 Tickets"), KeyboardButton("📢 Broadcast")])
        rows.append([KeyboardButton("🔒 Ban"), KeyboardButton("💳 Credit")])
    rows.append([KeyboardButton("🏠 Home")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def kb_cancel() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel")]], resize_keyboard=True)


def kb_deposit_currency() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("BTC"), KeyboardButton("LTC")],
            [KeyboardButton("USDT (TRC20)"), KeyboardButton("BNB (BEP20)")],
            [KeyboardButton("❌ Cancel")],
        ],
        resize_keyboard=True,
    )


def kb_after_deposit() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🔄 Check Deposit")], [KeyboardButton("➕ New Deposit"), KeyboardButton("🏠 Home")]],
        resize_keyboard=True,
    )


def kb_balance_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Check Deposit", callback_data="check_deposit_now")]])


def extract_address_from_response(text: str, currency: str) -> str | None:
    if currency == "BTC":
        patterns = [r"bc1[a-z0-9]{39,59}", r"[13][a-km-zA-HJ-NP-Z1-9]{25,34}"]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0)
    elif currency == "LTC":
        patterns = [r"ltc1[a-z0-9]{39,59}", r"[LM3][a-km-zA-HJ-NP-Z1-9]{26,33}"]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0)
    elif currency == "USDT_TRC20":
        match = re.search(r"T[A-Za-z0-9]{33}", text)
        if match:
            return match.group(0)
    elif currency == "BNB_BEP20":
        match = re.search(r"0x[a-fA-F0-9]{40}", text)
        if match:
            return match.group(0)
    return None


def create_deposit_address(user_id: int, currency: str) -> dict | None:
    external_id = f"{user_id}_{uuid.uuid4().hex[:8]}"
    dv_currency = CURRENCY_MAP.get(currency, currency)
    url = f"{DV_BASE_URL}/api/v1/external/wallet"
    headers = {"x-api-key": DV_API_KEY, "Content-Type": "application/json"}
    payload = {"amount": 0, "store_external_id": external_id, "currency": dv_currency}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        address = extract_address_from_response(response.text, dv_currency)
        if not address:
            logger.error("No address found for %s", currency)
            return None
        return {"address": address, "external_id": external_id, "currency": currency, "dv_currency": dv_currency}
    except Exception as e:
        logger.error("Address creation failed: %s", e)
        return None


def get_or_create_user(user_id: int, referrer: int = None) -> dict:
    try:
        result = supabase.table("users").select("*").eq("user_id", user_id).execute()
        if result.data:
            user = result.data[0]
            if referrer and not user.get("referred_by"):
                supabase.table("users").update({"referred_by": referrer}).eq("user_id", user_id).execute()
            return user
        new_user = {
            "user_id": user_id,
            "balance_usd": 0.0,
            "referred_by": referrer,
            "is_banned": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        supabase.table("users").insert(new_user).execute()
        return new_user
    except Exception as e:
        logger.error("User error: %s", e)
        return {"user_id": user_id, "balance_usd": 0.0, "is_banned": False}


def get_balance(user_id: int) -> float:
    return float(get_or_create_user(user_id).get("balance_usd", 0.0))


def update_balance(user_id: int, amount_usd: float, operation: str = "add") -> bool:
    try:
        current = get_balance(user_id)
        new_balance = current + amount_usd if operation == "add" else current - amount_usd
        supabase.table("users").update({"balance_usd": new_balance, "updated_at": datetime.now(timezone.utc).isoformat()}).eq(
            "user_id", user_id
        ).execute()
        logger.info("Balance updated user=%s op=%s amount=%.2f new=%.2f", user_id, operation, amount_usd, new_balance)
        return True
    except Exception as e:
        logger.error("Balance update failed: %s", e)
        return False


def get_or_create_address(user_id: int, currency: str) -> dict | None:
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        result = (
            supabase.table("addresses")
            .select("*")
            .eq("user_id", user_id)
            .eq("currency", currency)
            .gte("expires_at", cutoff)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]
        address_data = create_deposit_address(user_id, currency)
        if not address_data:
            return None
        new_address = {
            "user_id": user_id,
            "currency": currency,
            "address": address_data["address"],
            "external_id": address_data["external_id"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        }
        supabase.table("addresses").insert(new_address).execute()
        return new_address
    except Exception as e:
        logger.error("Address error: %s", e)
        return None


def _fetch_btc_txs(address: str) -> list[dict]:
    url = f"https://api.blockcypher.com/v1/btc/main/addrs/{address}/full?limit=50"
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        return []
    data = r.json()
    txs = []
    for tx in data.get("txs", []):
        tx_hash = tx.get("hash")
        value_sats = 0
        for out in tx.get("outputs", []):
            if address in out.get("addresses", []):
                value_sats += int(out.get("value", 0))
        if tx_hash and value_sats > 0:
            confirmations = int(tx.get("confirmations", 0) or 0)
            txs.append({"hash": tx_hash, "amount": value_sats / 100_000_000, "confirmations": confirmations})
    return txs


def _fetch_ltc_txs(address: str) -> list[dict]:
    url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/full?limit=50"
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        return []
    data = r.json()
    txs = []
    for tx in data.get("txs", []):
        tx_hash = tx.get("hash")
        value_litoshi = 0
        for out in tx.get("outputs", []):
            if address in out.get("addresses", []):
                value_litoshi += int(out.get("value", 0))
        if tx_hash and value_litoshi > 0:
            confirmations = int(tx.get("confirmations", 0) or 0)
            txs.append({"hash": tx_hash, "amount": value_litoshi / 100_000_000, "confirmations": confirmations})
    return txs


def _fetch_trc20_txs(address: str) -> list[dict]:
    url = (
        "https://apilist.tronscanapi.com/api/token_trc20/transfers"
        f"?limit=20&start=0&sort=-timestamp&count=true&relatedAddress={address}"
    )
    r = requests.get(url, timeout=20)
    if r.status_code != 200:
        return []
    txs = []
    for tx in r.json().get("token_transfers", []):
        if tx.get("to_address") != address:
            continue
        if str(tx.get("tokenAbbr", "")).upper() != "USDT":
            continue
        quant = float(tx.get("quant", 0))
        dec = int(tx.get("tokenDecimal", 6) or 6)
        amount = quant / (10**dec)
        tx_hash = tx.get("transaction_id")
        if tx_hash and amount > 0:
            confirmations = 2 if tx.get("confirmed") else 0
            txs.append({"hash": tx_hash, "amount": amount, "confirmations": confirmations})
    return txs


def _fetch_bnb_txs(address: str) -> list[dict]:
    url = f"https://blockscout.com/bsc/mainnet/api?module=account&action=txlist&address={address}&sort=desc"
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        return []
    payload = r.json()
    if str(payload.get("status")) != "1":
        return []
    txs = []
    for tx in payload.get("result", [])[:100]:
        tx_hash = tx.get("hash")
        to_addr = str(tx.get("to", "")).lower()
        if to_addr != address.lower():
            continue
        confirmations = int(tx.get("confirmations", 0) or 0)
        amount_wei = int(tx.get("value", 0) or 0)
        if tx_hash and amount_wei > 0:
            txs.append({"hash": tx_hash, "amount": amount_wei / 1_000_000_000_000_000_000, "confirmations": confirmations})
    return txs


def fetch_chain_transactions(address: str, currency: str) -> list[dict]:
    if currency == "BTC":
        return _fetch_btc_txs(address)
    if currency == "LTC":
        return _fetch_ltc_txs(address)
    if currency == "USDT (TRC20)":
        return _fetch_trc20_txs(address)
    if currency == "BNB (BEP20)":
        return _fetch_bnb_txs(address)
    return []


def _address_explorer_url(address: str, currency: str) -> str | None:
    if currency == "BTC":
        return f"https://www.blockchain.com/explorer/addresses/btc/{address}"
    if currency == "LTC":
        return f"https://blockchair.com/litecoin/address/{address}"
    if currency == "USDT (TRC20)":
        return f"https://tronscan.org/#/address/{address}"
    if currency == "BNB (BEP20)":
        return f"https://bscscan.com/address/{address}"
    return None


def get_recent_addresses_with_links(user_id: int, limit: int = 4) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ADDRESS_SCAN_HOURS)).isoformat()
    res = (
        supabase.table("addresses")
        .select("address,currency,created_at")
        .eq("user_id", user_id)
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    entries = []
    seen = set()
    for row in res.data or []:
        address = row.get("address")
        currency = row.get("currency")
        if not address or address in seen:
            continue
        seen.add(address)
        link = _address_explorer_url(address, currency)
        if not link:
            continue
        entries.append({"currency": currency, "address": address, "link": link})
    return entries


async def get_incoming_transactions(user_id: int) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ADDRESS_SCAN_HOURS)).isoformat()
    addresses = supabase.table("addresses").select("*").eq("user_id", user_id).gte("created_at", cutoff).execute()
    incoming = []
    for addr in addresses.data or []:
        try:
            txs = fetch_chain_transactions(addr["address"], addr["currency"])
            for tx in txs:
                tx_hash = tx.get("hash")
                confirmations = int(tx.get("confirmations", 0) or 0)
                amount = float(tx.get("amount", 0) or 0)
                if not tx_hash or amount <= 0 or confirmations >= MIN_CONFIRMATIONS:
                    continue
                existing = supabase.table("deposits").select("id").eq("tx_hash", tx_hash).execute()
                if existing.data:
                    continue
                incoming.append({
                    "currency": addr["currency"],
                    "amount": amount,
                    "confirmations": confirmations,
                    "tx_hash": tx_hash[:16],
                })
        except Exception as exc:
            logger.error("Incoming TX check failed for %s: %s", addr.get("address"), exc)
    return incoming


def _build_check_response(uid: int, deposits: list, incoming: list, scan_links: list[dict] | None = None) -> str:
    parts = []
    if deposits:
        total = sum(d["amount_usd"] for d in deposits)
        parts.append(f"✅ Found {len(deposits)} new deposit(s)!\n💰 +${total:.2f}")
    else:
        parts.append("✅ No new confirmed deposits")

    if incoming:
        preview = incoming[:3]
        lines = [
            f"• {tx['amount']:.6f} {tx['currency']} ({tx['confirmations']}/{MIN_CONFIRMATIONS}) #{tx['tx_hash']}"
            for tx in preview
        ]
        more = "" if len(incoming) <= 3 else f"\n...and {len(incoming) - 3} more incoming tx"
        parts.append("⏳ Incoming transactions:\n" + "\n".join(lines) + more)

    if scan_links:
        link_lines = [f"• {entry['currency']}: {entry['link']}" for entry in scan_links]
        parts.append("🔎 Scanner links (recent addresses):\n" + "\n".join(link_lines))

    parts.append(f"💳 Balance: ${get_balance(uid):.2f}")
    return "\n\n".join(parts)


async def _notify_admin_deposit(bot, deposit_event: dict):
    if not bot:
        return
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "💰 New Deposit\n"
                f"User: {deposit_event['user_id']}\n"
                f"Amount: {deposit_event['crypto']}\n"
                f"USD: ${deposit_event['amount_usd']:.2f}\n"
                f"Tx: {deposit_event['tx_hash']}"
            ),
        )
    except Exception as exc:
        logger.error("Admin notification failed: %s", exc)


async def check_deposits(user_id: int = None, bot=None) -> list:
    try:
        rates = get_exchange_rates_usd()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ADDRESS_SCAN_HOURS)).isoformat()
        query = supabase.table("addresses").select("*").gte("created_at", cutoff)
        if user_id:
            query = query.eq("user_id", user_id)
        addresses = query.execute()
        new_deposits = []

        for addr in addresses.data or []:
            try:
                txs = fetch_chain_transactions(addr["address"], addr["currency"])
                for tx in txs:
                    tx_hash = tx.get("hash")
                    if not tx_hash:
                        continue
                    existing = supabase.table("deposits").select("id").eq("tx_hash", tx_hash).execute()
                    if existing.data:
                        continue

                    crypto_amount = float(tx.get("amount", 0.0))
                    confirmations = int(tx.get("confirmations", 0) or 0)
                    if crypto_amount <= 0 or confirmations < MIN_CONFIRMATIONS:
                        continue
                    dv_currency = CURRENCY_MAP.get(addr["currency"], addr["currency"])
                    rate = rates.get(dv_currency, FALLBACK_EXCHANGE_RATES.get(dv_currency, 1.0))
                    usd_amount = crypto_amount * rate

                    deposit = {
                        "user_id": addr["user_id"],
                        "address_id": addr["id"],
                        "currency": addr["currency"],
                        "crypto_amount": crypto_amount,
                        "usd_amount": usd_amount,
                        "rate": rate,
                        "tx_hash": tx_hash,
                        "tx_id": tx_hash,
                        "confirmed": True,
                    }
                    supabase.table("deposits").insert(deposit).execute()
                    update_balance(addr["user_id"], usd_amount, "add")
                    event = {
                        "user_id": addr["user_id"],
                        "amount_usd": usd_amount,
                        "crypto": f"{crypto_amount:.4f} {addr['currency']}",
                        "tx_hash": tx_hash[:16],
                    }
                    new_deposits.append(event)
                    if bot:
                        try:
                            await bot.send_message(
                                chat_id=addr["user_id"],
                                text=f"💰 Deposit received!\n{crypto_amount:.4f} {addr['currency']} = ${usd_amount:.2f}\nNew balance: ${get_balance(addr['user_id']):.2f}",
                            )
                        except Exception:
                            pass
                        await _notify_admin_deposit(bot, event)
            except Exception as e:
                logger.error("TX check failed for %s: %s", addr.get("address"), e)
        return new_deposits
    except Exception as e:
        logger.error("Deposit check failed: %s", e)
        return []


async def deposit_worker(context: ContextTypes.DEFAULT_TYPE):
    try:
        deposits = await check_deposits(user_id=None, bot=context.application.bot)
        if deposits:
            total = sum(d["amount_usd"] for d in deposits)
            logger.info("Auto-deposits: %s total $%.2f", len(deposits), total)
    except Exception as e:
        logger.error("Worker error: %s", e)


async def background_scan_loop(app: Application):
    interval = max(60, SCAN_MINUTES * 60)
    await asyncio.sleep(30)
    while True:
        try:
            deposits = await check_deposits(user_id=None, bot=app.bot)
            if deposits:
                total = sum(d["amount_usd"] for d in deposits)
                logger.info("Auto-deposits(loop): %s total $%.2f", len(deposits), total)
        except Exception as exc:
            logger.error("Background loop error: %s", exc)
        await asyncio.sleep(interval)


async def post_init(app: Application):
    if app.job_queue:
        app.job_queue.run_repeating(deposit_worker, interval=max(60, SCAN_MINUTES * 60), first=30)
        logger.info("Background scanner started via JobQueue")
    else:
        app.create_task(background_scan_loop(app))
        logger.warning("JobQueue missing; using asyncio fallback loop for background scanner")


def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ref = int(context.args[0]) if context.args and context.args[0].isdigit() else None
    if ref == uid:
        ref = None
    get_or_create_user(uid, ref)
    await update.message.reply_text("🏠 Home", reply_markup=kb_home(is_admin(uid)))


async def deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "DEPOSIT_CURRENCY"
    await update.message.reply_text("Choose currency:", reply_markup=kb_deposit_currency())


async def deposit_currency_received(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if text == "❌ Cancel":
        await update.message.reply_text("🏠 Home", reply_markup=kb_home(is_admin(update.effective_user.id)))
        context.user_data["state"] = None
        return
    uid = update.effective_user.id
    address = get_or_create_address(uid, text)
    if not address:
        await update.message.reply_text("❌ Failed to generate address", reply_markup=kb_home(is_admin(uid)))
        context.user_data["state"] = None
        return
    msg = f"✅ {text}\n\n<code>{address['address']}</code>\n\n⏱️ Expires: 24h"
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=kb_after_deposit())
    context.user_data["state"] = None


async def check_my_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("🔄 Checking blockchain...")
    deposits = await check_deposits(user_id=uid, bot=context.application.bot)
    incoming = await get_incoming_transactions(uid)
    scan_links = get_recent_addresses_with_links(uid)
    await update.message.reply_text(
        _build_check_response(uid, deposits, incoming, scan_links),
        reply_markup=kb_after_deposit(),
    )


async def check_my_deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    await query.edit_message_text("🔄 Checking blockchain for all your recent addresses...")
    deposits = await check_deposits(user_id=uid, bot=context.application.bot)
    incoming = await get_incoming_transactions(uid)
    scan_links = get_recent_addresses_with_links(uid)
    await context.bot.send_message(
        chat_id=uid,
        text=_build_check_response(uid, deposits, incoming, scan_links),
        reply_markup=kb_after_deposit(),
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"🆔 {uid}\n💰 ${get_balance(uid):.2f} USD",
        reply_markup=kb_home(is_admin(uid)),
    )
    await update.message.reply_text("Quick actions:", reply_markup=kb_balance_actions())


# ---------- SHOP ----------
async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "SHOP_RANGE"
    await update.message.reply_text("Enter year range (e.g., 1960-2003):", reply_markup=kb_cancel())


def parse_year_range(text: str) -> tuple | None:
    m = re.fullmatch(r"\s*(\d{4})\s*-\s*(\d{4})\s*", text)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if a <= 0 or b <= 0 or a > b or b > datetime.now().year + 1:
        return None
    return a, b


async def shop_range_received(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    years = parse_year_range(text)
    if not years:
        await update.message.reply_text("❌ Invalid range", reply_markup=kb_cancel())
        return

    start, end = years
    context.user_data["shop_start"] = start
    context.user_data["shop_end"] = end
    context.user_data["state"] = "SHOP_QTY"

    res = (
        supabase.table("books")
        .select("id", count="exact")
        .eq("sold", False)
        .eq("status", "active")
        .gte("db", f"{start}-01-01")
        .lte("db", f"{end}-12-31")
        .execute()
    )

    available = getattr(res, "count", 0) or 0
    await update.message.reply_text(
        f"✅ {start}-{end}\nAvailable: {available}\n\nEnter quantity:",
        reply_markup=kb_cancel(),
    )


async def shop_qty_received(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    uid = update.effective_user.id
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Positive number only", reply_markup=kb_cancel())
        return

    qty = int(text)
    start = context.user_data.get("shop_start")
    end = context.user_data.get("shop_end")
    unit_price = 10.0
    total = qty * unit_price

    balance_usd = get_balance(uid)
    if balance_usd < total:
        await update.message.reply_text(
            f"❌ Insufficient balance!\nNeed: ${total:.2f}\nHave: ${balance_usd:.2f}",
            reply_markup=kb_home(is_admin(uid)),
        )
        context.user_data["state"] = None
        return

    try:
        data = supabase.rpc(
            "purchase_books",
            {
                "p_user_id": uid,
                "p_start_year": start,
                "p_end_year": end,
                "p_qty": qty,
                "p_unit_price": unit_price,
            },
        ).execute().data

        if not data:
            await update.message.reply_text("❌ No books available", reply_markup=kb_home(is_admin(uid)))
            context.user_data["state"] = None
            return

        update_balance(uid, total, "subtract")

        lines = []
        for row in data:
            lines.append(",".join([
                str(row.get("title", "")),
                str(row.get("last_name", "")),
                str(row.get("source_id", "")),
                str(row.get("db", "")),
                str(row.get("uid", "")),
                str(row.get("email", "")),
                str(row.get("dncode", "")),
                str(row.get("pcode", "")),
                str(row.get("barcode", "")),
                str(row.get("status", "")),
                str(row.get("worker_id", "")),
            ]))

        await context.bot.send_document(
            chat_id=uid,
            document="\n".join(lines).encode("utf-8"),
            filename=f"order_{data[0]['order_id']}.txt",
            caption=f"✅ Purchase complete!\nTotal: ${total:.2f}\nBalance: ${get_balance(uid):.2f}",
        )

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🛒 New order\nUser: {uid}\nQty: {qty}\nTotal: ${total:.2f}",
        )

    except Exception as e:
        logger.error("Purchase failed: %s", e)
        await update.message.reply_text("❌ Purchase failed", reply_markup=kb_home(is_admin(uid)))

    context.user_data["state"] = None
    await update.message.reply_text("🏠 Home", reply_markup=kb_home(is_admin(uid)))


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = "SUPPORT_TEXT"
    await update.message.reply_text("Message for admin:", reply_markup=kb_cancel())


async def support_received(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    uid = update.effective_user.id
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"🎫 Support from {uid}:\n\n{text}")
    await update.message.reply_text("✅ Sent", reply_markup=kb_home(is_admin(uid)))
    context.user_data["state"] = None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    state = context.user_data.get("state")
    if text in ["❌ Cancel", "🏠 Home"]:
        context.user_data["state"] = None
        await update.message.reply_text("🏠 Home", reply_markup=kb_home(is_admin(uid)))
        return
    if state == "DEPOSIT_CURRENCY":
        await deposit_currency_received(update, context, text)
    elif state == "SHOP_RANGE":
        await shop_range_received(update, context, text)
    elif state == "SHOP_QTY":
        await shop_qty_received(update, context, text)
    elif state == "SUPPORT_TEXT":
        await support_received(update, context, text)
    elif text == "➕ Deposit":
        await deposit(update, context)
    elif text == "💰 Balance":
        await balance(update, context)
    elif text == "🛒 Shop":
        await shop(update, context)
    elif text == "🔄 Check Deposit":
        await check_my_deposit(update, context)
    elif text == "➕ New Deposit":
        await deposit(update, context)
    elif text == "🎧 Support":
        await support(update, context)
    elif text == "👥 Invite":
        await update.message.reply_text("📢 Invite friends!", reply_markup=kb_home(is_admin(uid)))
    elif text == "ℹ️ About":
        await update.message.reply_text("🏪 ULTIMATESHOP", reply_markup=kb_home(is_admin(uid)))
    else:
        await update.message.reply_text("❌ Unknown", reply_markup=kb_home(is_admin(uid)))


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_my_deposit_callback, pattern="^check_deposit_now$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("ULTIMATESHOP FINAL - Starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

import logging
from datetime import datetime, timezone

import requests
import yaml
from flask import Flask, jsonify, request
from supabase import create_client

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

SUPABASE_URL = config["supabase"]["url"]
SUPABASE_KEY = config["supabase"]["key"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

BOT_TOKEN = config["telegram"]["token"]
ADMIN_ID = int(config.get("telegram", {}).get("admin_id", 2124577519))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("WEBHOOK")

app = Flask(__name__)


def send_telegram(chat_id: int, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=8,
        )
    except Exception as exc:
        logger.warning("Telegram notify failed chat_id=%s: %s", chat_id, exc)


def _extract_payload(payload: dict):
    wallet = payload.get("wallet", {}) or {}
    tx = payload.get("transactions", {}) or {}
    external_id = wallet.get("store_external_id", "")
    user_id = int(str(external_id).split("_")[0])
    tx_hash = tx.get("tx_hash") or tx.get("hash")
    currency = tx.get("currency")
    crypto_amount = float(tx.get("amount", 0) or 0)
    usd_amount = float(tx.get("amount_usd", 0) or 0)
    address = wallet.get("address")
    return user_id, tx_hash, currency, crypto_amount, usd_amount, address


@app.route("/", methods=["GET"])
def index():
    return jsonify({"ok": True, "service": "dvnet-webhook", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/test", methods=["GET"])
def test():
    return jsonify({"ok": True, "admin_id": ADMIN_ID})


@app.route("/dvnet-webhook", methods=["POST"])
def dvnet_webhook():
    try:
        payload = request.get_json(silent=True) or {}
        status = str(payload.get("status", "")).lower()
        if status != "completed":
            return jsonify({"status": "ignored", "reason": "not_completed"}), 200

        user_id, tx_hash, currency, crypto_amount, usd_amount, address = _extract_payload(payload)
        if not tx_hash or crypto_amount <= 0 or usd_amount <= 0:
            return jsonify({"status": "ignored", "reason": "invalid_amount_or_hash"}), 200

        existing = supabase.table("deposits").select("id").eq("tx_hash", tx_hash).limit(1).execute()
        if existing.data:
            return jsonify({"status": "duplicate"}), 200

        address_row = (
            supabase.table("addresses")
            .select("id")
            .eq("user_id", user_id)
            .eq("address", address)
            .eq("currency", currency)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not address_row.data:
            return jsonify({"status": "ignored", "reason": "address_not_found"}), 200

        address_id = address_row.data[0]["id"]

        user_row = supabase.table("users").select("balance_usd").eq("user_id", user_id).limit(1).execute()
        if user_row.data:
            current_balance = float(user_row.data[0].get("balance_usd", 0) or 0)
        else:
            supabase.table("users").insert(
                {
                    "user_id": user_id,
                    "balance_usd": 0.0,
                    "is_banned": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
            current_balance = 0.0

        new_balance = current_balance + usd_amount

        supabase.table("users").update(
            {"balance_usd": new_balance, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("user_id", user_id).execute()

        supabase.table("deposits").insert(
            {
                "user_id": user_id,
                "address_id": address_id,
                "currency": currency,
                "crypto_amount": crypto_amount,
                "usd_amount": usd_amount,
                "rate": usd_amount / crypto_amount if crypto_amount > 0 else 0,
                "tx_hash": tx_hash,
                "tx_id": tx.get("tx_id") if isinstance((tx := payload.get("transactions", {})), dict) else None,
                "confirmed": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()

        send_telegram(user_id, f"💰 Deposit received!\n{crypto_amount:.6f} {currency}\nUSD: ${usd_amount:.2f}\nBalance: ${new_balance:.2f}")
        send_telegram(ADMIN_ID, f"💰 New deposit\nUser: {user_id}\n{crypto_amount:.6f} {currency}\nUSD: ${usd_amount:.2f}\nTx: {str(tx_hash)[:20]}")

        return jsonify({"status": "success", "user_id": user_id, "new_balance": new_balance}), 200
    except Exception as exc:
        logger.exception("Webhook processing failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500


if __name__ == "__main__":
    print("DV webhook receiver starting on 0.0.0.0:5000")
    print("Expected webhook URL: http://166.88.96.191:5000/dvnet-webhook")
    app.run(host="0.0.0.0", port=5000, debug=False)

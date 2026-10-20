#!/usr/bin/env python3
"""
Maison Bijou – Guest Check-in Backend
TTLock integration with 4-digit codes + semi-automatic bookings
"""

import hashlib
import json
import os
import random
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# File HTML e video sono nella root del progetto (struttura piatta su GitHub)
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

# ---------- Config (da variabili d'ambiente in produzione) ----------
CLIENT_ID = os.environ.get("TTLOCK_CLIENT_ID", "f3a355d932374a58949b8f6853dfc6a2")
CLIENT_SECRET = os.environ.get("TTLOCK_CLIENT_SECRET", "e7da1d3ba4f5dd9a89b40b78d092d7d5")
USERNAME = os.environ.get("TTLOCK_USERNAME", "salvo141077@icloud.com")
PASSWORD = os.environ.get("TTLOCK_PASSWORD", "S@lvo978")
BASE_URL = "https://euapi.ttlock.com"

# Lock mapping (esclusi Magazzino e Sala Comune)
LOCKS = {
    "ingresso": {"lockId": 26363104, "name": "Porta Ingresso"},
    "andalusite": {"lockId": 21638085, "name": "Andalusite"},
    "acqua_marina": {"lockId": 21633703, "name": "Acqua Marina"},
    "diamante": {"lockId": 21632657, "name": "Diamante"},
    "smeraldo": {"lockId": 21632205, "name": "Smeraldo"},
    "ambra": {"lockId": 19752529, "name": "Ambra"},
}

ROOMS = ["andalusite", "acqua_marina", "diamante", "smeraldo", "ambra"]

DATA_FILE = Path(__file__).parent / "bookings.json"

# Token cache
_token = {"access": None, "expires": 0}


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest().lower()


def get_token() -> str:
    now = time.time()
    if _token["access"] and now < _token["expires"] - 300:
        return _token["access"]

    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": USERNAME,
        "password": md5(PASSWORD),
    }).encode()

    req = urllib.request.Request(
        f"{BASE_URL}/oauth2/token",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())

    if "access_token" not in result:
        raise Exception(result.get("errmsg") or str(result))

    _token["access"] = result["access_token"]
    _token["expires"] = now + result.get("expires_in", 7776000)
    return _token["access"]


def add_4digit_code(lock_id: int, code: str, name: str, start_ms: int, end_ms: int) -> dict:
    """Crea un codice personalizzato a 4 cifre via gateway."""
    token = get_token()
    now = int(time.time() * 1000)

    body = urllib.parse.urlencode({
        "clientId": CLIENT_ID,
        "accessToken": token,
        "lockId": str(lock_id),
        "keyboardPwd": code,
        "keyboardPwdName": name[:50],
        "startDate": str(start_ms),
        "endDate": str(end_ms),
        "addType": "2",
        "date": str(now),
    }).encode()

    req = urllib.request.Request(
        f"{BASE_URL}/v3/keyboardPwd/add",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode())

    if result.get("errcode") and result["errcode"] != 0:
        raise Exception(result.get("errmsg") or f"TTLock error {result.get('errcode')}")

    return {
        "keyboardPwdId": result.get("keyboardPwdId"),
        "passcode": code,
        "lockId": lock_id,
    }


def random_4digit() -> str:
    return str(random.randint(1000, 9999))


def load_bookings() -> list:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return []


def save_bookings(bookings: list):
    DATA_FILE.write_text(json.dumps(bookings, indent=2, ensure_ascii=False))


# ---------- API ----------

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "locks": len(LOCKS)})


@app.route("/api/locks")
def list_locks():
    return jsonify({
        "success": True,
        "locks": [
            {"key": k, "lockId": v["lockId"], "name": v["name"]}
            for k, v in LOCKS.items()
        ],
        "rooms": [
            {"key": k, "lockId": LOCKS[k]["lockId"], "name": LOCKS[k]["name"]}
            for k in ROOMS
        ],
    })


@app.route("/api/bookings", methods=["GET"])
def get_bookings():
    return jsonify({"success": True, "bookings": load_bookings()})


@app.route("/api/bookings", methods=["POST"])
def create_booking():
    """
    Body:
    {
      "guestName": "Marco Rossi",
      "room": "diamante",          # key from ROOMS
      "checkIn": "2026-10-20T15:00",
      "checkOut": "2026-10-23T10:00"
    }
    """
    data = request.json or {}
    guest_name = (data.get("guestName") or "").strip()
    room_key = data.get("room")
    check_in = data.get("checkIn")
    check_out = data.get("checkOut")

    if not guest_name:
        return jsonify({"error": "guestName obbligatorio"}), 400
    if room_key not in ROOMS:
        return jsonify({"error": f"room non valida. Usa: {ROOMS}"}), 400
    if not check_in or not check_out:
        return jsonify({"error": "checkIn e checkOut obbligatori"}), 400

    try:
        start_ms = int(datetime.fromisoformat(check_in).timestamp() * 1000)
        end_ms = int(datetime.fromisoformat(check_out).timestamp() * 1000)
    except Exception:
        return jsonify({"error": "Formato date non valido (usa YYYY-MM-DDTHH:MM)"}), 400

    if end_ms <= start_ms:
        return jsonify({"error": "checkOut deve essere dopo checkIn"}), 400

    # Camera: codice valido solo 24 ore dal check-in (poi si usa la scheda)
    room_end_ms = start_ms + (24 * 60 * 60 * 1000)

    # Genera 2 codici a 4 cifre diversi
    code_ingresso = random_4digit()
    code_camera = random_4digit()
    while code_camera == code_ingresso:
        code_camera = random_4digit()

    name_label = f"{guest_name} – {LOCKS[room_key]['name']}"

    try:
        # Codice porta ingresso → tutto il soggiorno
        res_ing = add_4digit_code(
            LOCKS["ingresso"]["lockId"],
            code_ingresso,
            f"{guest_name} Ingresso",
            start_ms,
            end_ms,
        )
        # Codice camera → solo 24 ore dal check-in
        res_cam = add_4digit_code(
            LOCKS[room_key]["lockId"],
            code_camera,
            name_label,
            start_ms,
            room_end_ms,
        )
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

    booking_id = f"bk_{int(time.time())}_{random.randint(100,999)}"
    room_valid_until = datetime.fromtimestamp(room_end_ms / 1000).isoformat(timespec="minutes")
    booking = {
        "id": booking_id,
        "guestName": guest_name,
        "room": room_key,
        "roomName": LOCKS[room_key]["name"],
        "checkIn": check_in,
        "checkOut": check_out,
        "codeIngresso": code_ingresso,
        "codeCamera": code_camera,
        "roomCodeValidUntil": room_valid_until,
        "keyboardPwdIdIngresso": res_ing["keyboardPwdId"],
        "keyboardPwdIdCamera": res_cam["keyboardPwdId"],
        "createdAt": datetime.now().isoformat(),
        "guestLink": f"/guest/{booking_id}",
    }

    bookings = load_bookings()
    bookings.insert(0, booking)
    save_bookings(bookings)

    return jsonify({"success": True, "booking": booking})


@app.route("/api/bookings/<booking_id>")
def get_booking(booking_id):
    for b in load_bookings():
        if b["id"] == booking_id:
            return jsonify({"success": True, "booking": b})
    return jsonify({"error": "Prenotazione non trovata"}), 404


@app.route("/api/test-code", methods=["POST"])
def test_code():
    """Genera un codice di prova a 4 cifre su una serratura."""
    data = request.json or {}
    lock_key = data.get("lock", "ingresso")
    hours = float(data.get("hours", 2))

    if lock_key not in LOCKS:
        return jsonify({"error": "lock non valido"}), 400

    now = int(time.time() * 1000)
    start = now
    end = now + int(hours * 3600 * 1000)
    code = random_4digit()

    try:
        result = add_4digit_code(
            LOCKS[lock_key]["lockId"],
            code,
            "Test",
            start,
            end,
        )
        return jsonify({
            "success": True,
            "code": code,
            "lock": LOCKS[lock_key]["name"],
            "validHours": hours,
            "keyboardPwdId": result["keyboardPwdId"],
        })
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500


# Serve frontend (file nella root del repo)
@app.route("/")
def index():
    return send_from_directory(".", "host.html")


@app.route("/guest/<booking_id>")
def guest_page(booking_id):
    return send_from_directory(".", "guest.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n🏠 Maison Bijou Check-in Server")
    print(f"   Host dashboard:  http://localhost:{port}/")
    print(f"   API health:      http://localhost:{port}/api/health\n")
    app.run(host="0.0.0.0", port=port, debug=False)

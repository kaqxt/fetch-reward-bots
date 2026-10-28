#!/usr/bin/env python3
"""
Fetch Rewards Mass Creator — 70k scale
"""

import os
import sys
import time
import json
import random
import string
import signal
import logging
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional, List, Dict
from pathlib import Path

import requests
from faker import Faker

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
TARGET_ACCOUNTS   = 100          # start small, raise later
WORKERS           = 8
PROXY_FILE        = "proxies.txt"
SMS_API_KEY       = "YOUR_5SIM_OR_SMSACTIVATE_KEY"
SMS_PROVIDER      = "5sim"
COUNTRY           = "usa"
SERVICE           = "other"
REFERRAL          = None
DELAY_BETWEEN     = (8, 22)
MAX_RETRIES       = 4
SMS_TIMEOUT       = 110
DB_PATH           = "fetch_accounts.db"
LOG_DIR           = "logs"
KILL_FILE         = "STOP"

# REPLACE THESE AFTER YOU CAPTURE REAL TRAFFIC
START_URL  = "https://api.fetch.com/v1/auth/phone/start"
VERIFY_URL = "https://api.fetch.com/v1/auth/phone/verify"

# ────────────────────────────────────────────────
Path(LOG_DIR).mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(processName)s] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(f"{LOG_DIR}/factory.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("factory")
fake = Faker("en_US")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE,
            first_name TEXT,
            last_name TEXT,
            referral TEXT,
            proxy TEXT,
            status TEXT,
            created_at REAL,
            notes TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            key TEXT PRIMARY KEY,
            value INTEGER
        )
    """)
    conn.commit()
    conn.close()

def save_account(phone, first, last, referral, proxy, status, notes=""):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (phone, first_name, last_name, referral, proxy, status, created_at, notes) VALUES (?,?,?,?,?,?,?,?)",
            (phone, first, last, referral, proxy, status, time.time(), notes)
        )
        conn.commit()
    finally:
        conn.close()

def increment_stat(key: str, amount: int = 1):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)", (key,))
    conn.execute("UPDATE stats SET value = value + ? WHERE key = ?", (amount, key))
    conn.commit()
    conn.close()

def get_stat(key: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM stats WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else 0

def load_proxies() -> List[str]:
    if not Path(PROXY_FILE).exists():
        return []
    return [l.strip() for l in open(PROXY_FILE) if l.strip() and not l.startswith("#")]

class SMSClient:
    def __init__(self, key: str):
        self.key = key
        self.base = "https://5sim.net/v1"
        self.h = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    def balance(self) -> float:
        r = requests.get(f"{self.base}/user/profile", headers=self.h, timeout=15)
        r.raise_for_status()
        return float(r.json().get("balance", 0))

    def buy(self) -> Dict:
        url = f"{self.base}/user/buy/activation/{COUNTRY}/any/{SERVICE}"
        r = requests.get(url, headers=self.h, timeout=25)
        r.raise_for_status()
        data = r.json()
        if "phone" not in data:
            raise RuntimeError(str(data))
        return data

    def wait_code(self, act_id: str) -> Optional[str]:
        end = time.time() + SMS_TIMEOUT
        while time.time() < end:
            if Path(KILL_FILE).exists():
                return None
            r = requests.get(f"{self.base}/user/check/{act_id}", headers=self.h, timeout=12)
            r.raise_for_status()
            data = r.json()
            st = data.get("status")
            if st == "RECEIVED":
                sms = data.get("sms") or []
                if sms:
                    code = sms[0].get("code")
                    if not code:
                        import re
                        m = re.search(r"\b(\d{4,8})\b", sms[0].get("text", ""))
                        code = m.group(1) if m else None
                    return code
            if st in ("CANCELED", "TIMEOUT", "BANNED"):
                return None
            time.sleep(4 + random.random())
        return None

    def finish(self, act_id: str):
        try:
            requests.get(f"{self.base}/user/finish/{act_id}", headers=self.h, timeout=8)
        except Exception:
            pass

    def cancel(self, act_id: str):
        try:
            requests.get(f"{self.base}/user/cancel/{act_id}", headers=self.h, timeout=8)
        except Exception:
            pass

class Session:
    def __init__(self, proxy: Optional[str]):
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.device_id = "".join(random.choices("0123456789abcdef", k=32))
        self.s.headers.update({
            "User-Agent": f"Fetch/{random.randint(3,4)}.{random.randint(40,60)}.{random.randint(0,9)} (iPhone; iOS 17.{random.randint(0,6)}; Scale/3.00)",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "X-Device-Platform": "ios",
            "X-Device-Id": self.device_id,
        })

    def start(self, phone: str, first: str, last: str, referral: Optional[str]) -> bool:
        payload = {
            "phone_number": phone,
            "country_code": "1",
            "first_name": first,
            "last_name": last,
            "device_id": self.device_id,
            "referral_code": referral or "",
            "platform": "ios",
        }
        try:
            r = self.s.post(START_URL, json=payload, timeout=18)
            return r.status_code in (200, 201, 202)
        except Exception as e:
            log.debug(f"start err: {e}")
            return False

    def verify(self, phone: str, code: str) -> bool:
        payload = {
            "phone_number": phone,
            "code": code,
            "device_id": self.device_id,
        }
        try:
            r = self.s.post(VERIFY_URL, json=payload, timeout=18)
            return r.status_code == 200
        except Exception as e:
            log.debug(f"verify err: {e}")
            return False

def worker_loop(worker_id: int, proxies: List[str]):
    sms = SMSClient(SMS_API_KEY)
    while True:
        if Path(KILL_FILE).exists():
            log.info(f"worker {worker_id} saw kill file, exiting")
            break
        total = get_stat("created")
        if total >= TARGET_ACCOUNTS:
            log.info(f"worker {worker_id} target reached")
            break

        proxy = random.choice(proxies) if proxies else None
        try:
            order = sms.buy()
        except Exception as e:
            log.warning(f"w{worker_id} buy fail: {e}")
            time.sleep(10)
            continue

        phone = order["phone"]
        act_id = str(order["id"])
        first = fake.first_name()
        last = fake.last_name()

        sess = Session(proxy)
        ok = sess.start(phone, first, last, REFERRAL)
        if not ok:
            sms.cancel(act_id)
            increment_stat("start_fail")
            time.sleep(random.uniform(*DELAY_BETWEEN))
            continue

        code = sms.wait_code(act_id)
        if not code:
            sms.cancel(act_id)
            increment_stat("sms_timeout")
            time.sleep(random.uniform(*DELAY_BETWEEN))
            continue

        if sess.verify(phone, code):
            sms.finish(act_id)
            save_account(phone, first, last, REFERRAL, proxy, "created")
            increment_stat("created")
            log.info(f"w{worker_id} CREATED {phone}  total≈{get_stat('created')}")
        else:
            sms.cancel(act_id)
            save_account(phone, first, last, REFERRAL, proxy, "verify_fail")
            increment_stat("verify_fail")

        time.sleep(random.uniform(*DELAY_BETWEEN))

def main():
    init_db()
    proxies = load_proxies()
    log.info(f"proxies loaded: {len(proxies)}")
    log.info(f"target: {TARGET_ACCOUNTS} | workers: {WORKERS}")

    sms = SMSClient(SMS_API_KEY)
    try:
        bal = sms.balance()
        log.info(f"SMS balance: {bal}")
        if bal < 10:
            log.warning("low SMS balance")
    except Exception as e:
        log.error(f"cannot reach SMS provider: {e}")
        sys.exit(1)

    if Path(KILL_FILE).exists():
        Path(KILL_FILE).unlink()

    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(worker_loop, i, proxies) for i in range(WORKERS)]
        try:
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    log.error(f"worker died: {e}")
        except KeyboardInterrupt:
            log.info("ctrl-c — writing kill file")
            Path(KILL_FILE).touch()

    created = get_stat("created")
    log.info(f"FINISHED. created={created}")
    print(f"\nDone. Accounts created: {created}")

if __name__ == "__main__":
    main()
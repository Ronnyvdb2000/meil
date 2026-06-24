#!/usr/bin/env python3
"""
imap_to_turso.py

Haalt ALLE mail (alle mappen) op via IMAP en slaat ze op in een Turso
database via de Turso HTTP API (geen native library nodig).
Attachments worden als losse bestanden op schijf bewaard.

Vereiste packages:
    pip install requests python-dotenv

Gebruik:
    python imap_to_turso.py
"""

import imaplib
import email
from email.header import decode_header, make_header
import os
import sys
import time
import json
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

# ---------------------------------------------------------------------------
# CONFIGURATIE
# ---------------------------------------------------------------------------

CONFIG = {
    "IMAP_HOST": os.environ.get("IMAP_HOST", "imap.online.be"),
    "IMAP_PORT": int(os.environ.get("IMAP_PORT", 993)),
    "IMAP_USER": os.environ.get("IMAP_USER", ""),
    "IMAP_PASS": os.environ.get("IMAP_PASS", ""),
    "TURSO_DATABASE_URL": os.environ.get("TURSO_DATABASE_URL", ""),
    "TURSO_AUTH_TOKEN": os.environ.get("TURSO_AUTH_TOKEN", ""),
    "ATTACHMENT_DIR": os.environ.get("ATTACHMENT_DIR", "./attachments"),
    "BATCH_SIZE": 50,
}

# ---------------------------------------------------------------------------
# TURSO HTTP API
# ---------------------------------------------------------------------------

class TursoDB:
    """Eenvoudige wrapper rond de Turso HTTP pipeline API."""

    def __init__(self, url, token):
        # url: libsql://... → omzetten naar https://...
        self.base_url = url.replace("libsql://", "https://") + "/v2/pipeline"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._pending = []  # batch van statements

    def _execute(self, statements, retries=5, backoff=2):
        """Voer een lijst van {"type": "execute", "stmt": {...}} uit, met retry bij 502/503."""
        payload = {"requests": statements + [{"type": "close"}]}
        for attempt in range(retries):
            try:
                resp = requests.post(self.base_url, headers=self.headers, json=payload, timeout=30)
                if resp.status_code in (502, 503, 504) and attempt < retries - 1:
                    wait = backoff ** attempt
                    print(f"  [~] Turso {resp.status_code}, wacht {wait}s en probeer opnieuw...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.ConnectionError:
                if attempt < retries - 1:
                    time.sleep(backoff ** attempt)
                    continue
                raise

    def execute(self, sql, params=None):
        """Voer één statement uit en geef resultaat terug."""
        stmt = {"sql": sql}
        if params:
            stmt["named_parameters"] = [
                {"name": f"p{i+1}", "value": self._to_value(v)}
                for i, v in enumerate(params)
            ]
            # vervang ? door :p1, :p2, ...
            for i in range(len(params)):
                stmt["sql"] = stmt["sql"].replace("?", f":p{i+1}", 1)
        result = self._execute([{"type": "execute", "stmt": stmt}])
        return result["results"][0]

    def executemany_batch(self, statements_with_params):
        """Voer meerdere statements tegelijk uit (batch commit)."""
        requests_list = []
        for sql, params in statements_with_params:
            stmt = {"sql": sql}
            if params:
                args = []
                new_sql = sql
                for i, v in enumerate(params):
                    new_sql = new_sql.replace("?", f":p{i+1}", 1)
                    args.append({"name": f"p{i+1}", "value": self._to_value(v)})
                stmt["sql"] = new_sql
                stmt["named_parameters"] = args
            requests_list.append({"type": "execute", "stmt": stmt})
        self._execute(requests_list)

    def executescript(self, sql):
        """Voer meerdere statements uit (gescheiden door ;)."""
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        reqs = [{"type": "execute", "stmt": {"sql": s}} for s in statements]
        self._execute(reqs)

    def fetchone(self, sql, params=None):
        result = self.execute(sql, params)
        rows = result.get("response", {}).get("result", {}).get("rows", [])
        if rows:
            cols = result["response"]["result"]["cols"]
            return {c["name"]: rows[0][i]["value"] for i, c in enumerate(cols)}
        return None

    def _to_value(self, v):
        if v is None:
            return {"type": "null", "value": None}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        return {"type": "text", "value": str(v)}

    def last_insert_id(self):
        result = self.fetchone("SELECT last_insert_rowid() as id")
        return int(result["id"]) if result else None


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    uid TEXT NOT NULL,
    message_id TEXT,
    from_addr TEXT,
    to_addr TEXT,
    cc_addr TEXT,
    subject TEXT,
    date_sent TEXT,
    body_text TEXT,
    body_html TEXT,
    has_attachments INTEGER DEFAULT 0,
    raw_size INTEGER,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(folder, uid)
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    filename TEXT,
    content_type TEXT,
    file_path TEXT,
    file_size INTEGER,
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_addr);
CREATE INDEX IF NOT EXISTS idx_messages_subject ON messages(subject);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date_sent);
CREATE INDEX IF NOT EXISTS idx_messages_folder ON messages(folder)
"""


def get_db():
    url = CONFIG["TURSO_DATABASE_URL"]
    token = CONFIG["TURSO_AUTH_TOKEN"]
    if not url or not token:
        print("FOUT: TURSO_DATABASE_URL en TURSO_AUTH_TOKEN zijn verplicht.")
        sys.exit(1)
    print(f"[*] Verbinden met Turso: {url}")
    db = TursoDB(url, token)
    db.executescript(SCHEMA)
    print("[+] Database schema klaar")
    return db


# ---------------------------------------------------------------------------
# EMAIL HULPFUNCTIES
# ---------------------------------------------------------------------------

def decode_str(value):
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def get_body(msg):
    body_text, body_html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if content_type == "text/plain" and not body_text:
                body_text = decoded
            elif content_type == "text/html" and not body_html:
                body_html = decoded
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace") if payload else ""
        except Exception:
            decoded = ""
        if msg.get_content_type() == "text/html":
            body_html = decoded
        else:
            body_text = decoded
    return body_text, body_html


def extract_attachments(msg, message_db_id, attachment_dir):
    results = []
    if not msg.is_multipart():
        return results
    msg_dir = Path(attachment_dir) / str(message_db_id)
    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" not in disposition and not part.get_filename():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = decode_str(filename)
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        msg_dir.mkdir(parents=True, exist_ok=True)
        safe_name = filename.replace("/", "_").replace("\\", "_")
        file_path = msg_dir / safe_name
        counter = 1
        while file_path.exists():
            stem, ext = os.path.splitext(safe_name)
            file_path = msg_dir / f"{stem}_{counter}{ext}"
            counter += 1
        with open(file_path, "wb") as f:
            f.write(payload)
        results.append((filename, part.get_content_type(), str(file_path), len(payload)))
    return results


def fetch_all_folders(imap_conn):
    folders = []
    status, mailbox_list = imap_conn.list()
    if status != "OK":
        return folders
    for entry in mailbox_list:
        decoded = entry.decode(errors="replace")
        parts = decoded.split(' "/" ')
        if len(parts) == 2:
            folder_name = parts[1].strip('"')
        else:
            folder_name = decoded.split()[-1].strip('"')
        folders.append(folder_name)
    return folders


def import_folder(imap_conn, db, folder_name, attachment_dir):
    try:
        status, _ = imap_conn.select(f'"{folder_name}"', readonly=True)
        if status != "OK":
            print(f"  [!] Kan map niet openen: {folder_name}")
            return 0
    except Exception as e:
        print(f"  [!] Fout bij openen map {folder_name}: {e}")
        return 0

    status, data = imap_conn.search(None, "ALL")
    if status != "OK":
        return 0

    uids = data[0].split()
    print(f"  [*] {folder_name}: {len(uids)} berichten")

    count = 0
    batch = []

    for i, uid in enumerate(uids, 1):
        uid_str = uid.decode()

        existing = db.fetchone(
            "SELECT id FROM messages WHERE folder = ? AND uid = ?",
            (folder_name, uid_str)
        )
        if existing:
            continue

        try:
            status, msg_data = imap_conn.fetch(uid, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            from_addr = decode_str(msg.get("From"))
            to_addr = decode_str(msg.get("To"))
            cc_addr = decode_str(msg.get("Cc"))
            subject = decode_str(msg.get("Subject"))
            date_sent = msg.get("Date", "")
            message_id = msg.get("Message-ID", "")

            body_text, body_html = get_body(msg)

            db.execute(
                """INSERT OR IGNORE INTO messages
                   (folder, uid, message_id, from_addr, to_addr, cc_addr,
                    subject, date_sent, body_text, body_html, raw_size)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (folder_name, uid_str, message_id, from_addr, to_addr, cc_addr,
                 subject, date_sent, body_text, body_html, len(raw_email))
            )

            row = db.fetchone(
                "SELECT id FROM messages WHERE folder = ? AND uid = ?",
                (folder_name, uid_str)
            )
            msg_db_id = int(row["id"]) if row else None

            if msg_db_id:
                attachments = extract_attachments(msg, msg_db_id, attachment_dir)
                if attachments:
                    db.execute("UPDATE messages SET has_attachments = 1 WHERE id = ?", (msg_db_id,))
                    for filename, content_type, file_path, file_size in attachments:
                        db.execute(
                            """INSERT INTO attachments
                               (message_id, filename, content_type, file_path, file_size)
                               VALUES (?, ?, ?, ?, ?)""",
                            (msg_db_id, filename, content_type, file_path, file_size)
                        )

            count += 1
            if count % CONFIG["BATCH_SIZE"] == 0:
                print(f"      ... {count}/{len(uids)} verwerkt")

        except Exception as e:
            print(f"  [!] Fout bij bericht uid={uid_str} in {folder_name}: {e}")
            continue

    return count


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    if not CONFIG["IMAP_USER"] or not CONFIG["IMAP_PASS"]:
        print("FOUT: stel IMAP_USER en IMAP_PASS in in .env")
        sys.exit(1)

    Path(CONFIG["ATTACHMENT_DIR"]).mkdir(parents=True, exist_ok=True)

    print(f"[*] Verbinden met IMAP {CONFIG['IMAP_HOST']}:{CONFIG['IMAP_PORT']} ...")
    imap_conn = imaplib.IMAP4_SSL(CONFIG["IMAP_HOST"], CONFIG["IMAP_PORT"])
    imap_conn.login(CONFIG["IMAP_USER"], CONFIG["IMAP_PASS"])
    print("[+] IMAP-login geslaagd")

    db = get_db()

    folders = fetch_all_folders(imap_conn)
    print(f"[*] Mappen: {folders}")

    total = 0
    start = time.time()
    for folder in folders:
        total += import_folder(imap_conn, db, folder, CONFIG["ATTACHMENT_DIR"])

    imap_conn.logout()
    elapsed = time.time() - start
    print(f"\n[+] Klaar! {total} nieuwe berichten geimporteerd in {elapsed:.1f}s")


if __name__ == "__main__":
    main()

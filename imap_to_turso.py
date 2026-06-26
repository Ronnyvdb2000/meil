#!/usr/bin/env python3
"""
imap_to_turso.py - IMAP naar Turso archief
Vereiste packages: pip install requests python-dotenv
"""

import imaplib
import email
from email.header import decode_header, make_header
import os
import sys
import time
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
    "IMAP_HOST": os.environ.get("IMAP_HOST", "mail.online.be"),
    "IMAP_PORT": int(os.environ.get("IMAP_PORT", 993)),
    "IMAP_USER": os.environ.get("IMAP_USER", ""),
    "IMAP_PASS": os.environ.get("IMAP_PASS", ""),
    "TURSO_DATABASE_URL": os.environ.get("TURSO_DATABASE_URL", ""),
    "TURSO_AUTH_TOKEN": os.environ.get("TURSO_AUTH_TOKEN", ""),
    "ATTACHMENT_DIR": os.environ.get("ATTACHMENT_DIR", "./attachments"),
    "BATCH_SIZE": 25,
}

# ---------------------------------------------------------------------------
# TURSO HTTP API — correcte implementatie met positional args
# ---------------------------------------------------------------------------
class TursoDB:
    def __init__(self, url, token):
        self.base_url = url.replace("libsql://", "https://") + "/v2/pipeline"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _to_value(self, v):
        if v is None:
            return {"type": "null", "value": None}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        return {"type": "text", "value": str(v)}

    def _build_stmt(self, sql, params=None):
        stmt = {"sql": sql}
        if params:
            # Gebruik positionele 'args' — dit is de correcte Turso HTTP API veldnaam
            stmt["args"] = [self._to_value(v) for v in params]
        return stmt

    def _execute(self, statements, retries=6, backoff=2):
        payload = {"requests": statements + [{"type": "close"}]}
        for attempt in range(retries):
            try:
                resp = requests.post(
                    self.base_url, headers=self.headers,
                    json=payload, timeout=60
                )
                if resp.status_code in (502, 503, 504) and attempt < retries - 1:
                    wait = backoff ** attempt
                    print(f"  [~] Turso {resp.status_code}, wacht {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                # Controleer op SQL-fouten in het antwoord
                for i, result in enumerate(data.get("results", [])):
                    if result.get("type") == "error":
                        raise RuntimeError(f"Turso SQL fout: {result.get('error', {}).get('message', result)}")
                return data
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ReadTimeout,
                    requests.exceptions.Timeout):
                if attempt < retries - 1:
                    wait = backoff ** attempt
                    print(f"  [~] Turso timeout (poging {attempt+1}), wacht {wait}s...")
                    time.sleep(wait)
                    continue
                raise

    def execute(self, sql, params=None):
        stmt = self._build_stmt(sql, params)
        result = self._execute([{"type": "execute", "stmt": stmt}])
        return result["results"][0]

    def executemany_batch(self, statements_with_params):
        reqs = [
            {"type": "execute", "stmt": self._build_stmt(sql, params)}
            for sql, params in statements_with_params
        ]
        self._execute(reqs)

    def executescript(self, sql):
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

    def fetchall(self, sql, params=None):
        result = self.execute(sql, params)
        rows = result.get("response", {}).get("result", {}).get("rows", [])
        if not rows:
            return []
        cols = result["response"]["result"]["cols"]
        return [{c["name"]: row[i]["value"] for i, c in enumerate(cols)} for row in rows]

    def last_insert_id(self):
        result = self.fetchone("SELECT last_insert_rowid() as id")
        v = result["id"] if result else None
        return int(v) if v else None

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
    # Verificatie
    n = db.fetchone("SELECT COUNT(*) as n FROM messages")
    print(f"[+] Database klaar — {n['n']} berichten al aanwezig")
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
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                continue
            if content_type == "text/plain" and not body_text:
                body_text = decoded
            elif content_type == "text/html" and not body_html:
                body_html = decoded
    else:
        try:
            payload = msg.get_payload(decode=True)
            decoded = payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""
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
        if "attachment" not in str(part.get("Content-Disposition", "")) and not part.get_filename():
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
        folder_name = parts[1].strip('"') if len(parts) == 2 else decoded.split()[-1].strip('"')
        folders.append(folder_name)
    return folders

# ---------------------------------------------------------------------------
# IMAP MET HERVERBINDING
# ---------------------------------------------------------------------------
def imap_connect():
    conn = imaplib.IMAP4_SSL(CONFIG["IMAP_HOST"], CONFIG["IMAP_PORT"])
    conn.login(CONFIG["IMAP_USER"], CONFIG["IMAP_PASS"])
    return conn

def imap_fetch_with_reconnect(imap_conn, uid, folder_name):
    for attempt in range(4):
        try:
            status, msg_data = imap_conn.fetch(uid, "(RFC822)")
            return imap_conn, status, msg_data
        except Exception as e:
            if attempt < 3:
                print(f"  [~] IMAP-fout ({e}), herverbinden (poging {attempt+1})...")
                time.sleep(2 ** attempt)
                try:
                    imap_conn = imap_connect()
                    imap_conn.select(f'"{folder_name}"', readonly=True)
                    print(f"  [+] Herverbonden")
                except Exception as e2:
                    print(f"  [!] Herverbinden mislukt: {e2}")
            else:
                return imap_conn, "NO", None
    return imap_conn, "NO", None

def import_folder(imap_conn, db, folder_name, attachment_dir):
    try:
        status, _ = imap_conn.select(f'"{folder_name}"', readonly=True)
        if status != "OK":
            print(f"  [!] Kan map niet openen: {folder_name}")
            return imap_conn, 0
    except Exception as e:
        print(f"  [!] Fout bij openen map {folder_name}: {e}")
        return imap_conn, 0

    status, data = imap_conn.search(None, "ALL")
    if status != "OK":
        return imap_conn, 0

    uids = data[0].split()
    print(f"  [*] {folder_name}: {len(uids)} berichten")

    existing_rows = db.fetchall("SELECT uid FROM messages WHERE folder = ?", (folder_name,))
    existing_uids = {row["uid"] for row in existing_rows}
    nieuw = len(uids) - len(existing_uids)
    print(f"      {len(existing_uids)} al in database, {nieuw} nieuw")

    if nieuw == 0:
        return imap_conn, 0

    count = 0
    for uid in uids:
        uid_str = uid.decode()
        if uid_str in existing_uids:
            continue

        imap_conn, status, msg_data = imap_fetch_with_reconnect(imap_conn, uid, folder_name)
        if status != "OK" or not msg_data or msg_data[0] is None:
            print(f"  [!] uid={uid_str} overgeslagen (fetch mislukt)")
            continue

        try:
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            body_text, body_html = get_body(msg)

            db.execute(
                "INSERT OR IGNORE INTO messages (folder, uid, message_id, from_addr, to_addr, cc_addr, subject, date_sent, body_text, body_html, raw_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (folder_name, uid_str,
                 decode_str(msg.get("Message-ID")),
                 decode_str(msg.get("From")),
                 decode_str(msg.get("To")),
                 decode_str(msg.get("Cc")),
                 decode_str(msg.get("Subject")),
                 msg.get("Date", ""),
                 body_text, body_html, len(raw_email))
            )

            msg_db_id = db.last_insert_id()
            if msg_db_id:
                attachments = extract_attachments(msg, msg_db_id, attachment_dir)
                if attachments:
                    db.executemany_batch([(
                        "INSERT INTO attachments (message_id, filename, content_type, file_path, file_size) VALUES (?, ?, ?, ?, ?)",
                        (msg_db_id, fn, ct, fp, fs)
                    ) for fn, ct, fp, fs in attachments])
                    db.execute("UPDATE messages SET has_attachments = 1 WHERE id = ?", (msg_db_id,))

            existing_uids.add(uid_str)
            count += 1
            if count % CONFIG["BATCH_SIZE"] == 0:
                print(f"      ... {count}/{nieuw} nieuw verwerkt")

        except Exception as e:
            print(f"  [!] Fout bij verwerken uid={uid_str}: {e}")
            continue

    return imap_conn, count

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    if not CONFIG["IMAP_USER"] or not CONFIG["IMAP_PASS"]:
        print("FOUT: stel IMAP_USER en IMAP_PASS in in .env")
        sys.exit(1)

    Path(CONFIG["ATTACHMENT_DIR"]).mkdir(parents=True, exist_ok=True)

    print(f"[*] Verbinden met IMAP {CONFIG['IMAP_HOST']}:{CONFIG['IMAP_PORT']} ...")
    imap_conn = imap_connect()
    print("[+] IMAP-login geslaagd")

    db = get_db()
    folders = fetch_all_folders(imap_conn)
    print(f"[*] Mappen: {folders}")

    total = 0
    start = time.time()
    for folder in folders:
        imap_conn, n = import_folder(imap_conn, db, folder, CONFIG["ATTACHMENT_DIR"])
        total += n

    try:
        imap_conn.logout()
    except Exception:
        pass

    elapsed = time.time() - start
    n_db = db.fetchone("SELECT COUNT(*) as n FROM messages")
    print(f"\n[+] Klaar! {total} nieuw geimporteerd in {elapsed:.1f}s")
    print(f"[+] Totaal in database: {n_db['n']} berichten")

if __name__ == "__main__":
    main()

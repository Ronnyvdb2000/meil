#!/usr/bin/env python3
"""
imap_to_turso.py

Haalt ALLE mail (alle mappen) op via IMAP en slaat ze op in een Turso
(libSQL) database. Attachments worden als losse bestanden op schijf
bewaard, alleen het pad komt in de database.

Configuratie via environment variables (zie .env.example) of pas
de CONFIG dict hieronder direct aan.

Vereiste packages:
    pip install libsql-experimental python-dotenv --break-system-packages

Gebruik:
    python imap_to_turso.py
"""

import imaplib
import email
from email.header import decode_header, make_header
import os
import sys
import time
import hashlib
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import libsql_experimental as libsql

# ---------------------------------------------------------------------------
# CONFIGURATIE
# ---------------------------------------------------------------------------

CONFIG = {
    # IMAP-instellingen
    "IMAP_HOST": os.environ.get("IMAP_HOST", "imap.online.be"),
    "IMAP_PORT": int(os.environ.get("IMAP_PORT", 993)),
    "IMAP_USER": os.environ.get("IMAP_USER", ""),
    "IMAP_PASS": os.environ.get("IMAP_PASS", ""),

    # Turso-instellingen
    "TURSO_DATABASE_URL": os.environ.get("TURSO_DATABASE_URL", ""),  # bv. libsql://mail-archief-xxx.turso.io
    "TURSO_AUTH_TOKEN": os.environ.get("TURSO_AUTH_TOKEN", ""),

    # Lokale opslag voor attachments
    "ATTACHMENT_DIR": os.environ.get("ATTACHMENT_DIR", "./attachments"),

    # Batchgrootte voor commits
    "BATCH_SIZE": 50,
}

# ---------------------------------------------------------------------------
# DATABASE SCHEMA
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
CREATE INDEX IF NOT EXISTS idx_messages_folder ON messages(folder);
"""


def get_db():
    """Maak verbinding met Turso. Valt terug op lokaal SQLite-bestand als
    er geen Turso-credentials zijn ingesteld (handig om eerst lokaal te testen)."""
    url = CONFIG["TURSO_DATABASE_URL"]
    token = CONFIG["TURSO_AUTH_TOKEN"]

    if url and token:
        print(f"[*] Verbinden met Turso: {url}")
        conn = libsql.connect("local_replica.db", sync_url=url, auth_token=token)
        conn.sync()
    else:
        print("[!] Geen Turso-credentials gevonden, gebruik lokaal bestand mail_archive.db")
        conn = libsql.connect("mail_archive.db")

    conn.executescript(SCHEMA)
    return conn


def decode_str(value):
    """Decodeer MIME-encoded headers (bv. '=?UTF-8?B?...?=') naar leesbare tekst."""
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def get_body(msg):
    """Haal tekst- en HTML-body uit een email.message.Message."""
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
    """Schrijf attachments naar schijf, geef lijst van (filename, content_type, path, size) terug."""
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
        # voorkom overschrijven bij dubbele bestandsnamen
        safe_name = filename.replace("/", "_").replace("\\", "_")
        file_path = msg_dir / safe_name
        counter = 1
        while file_path.exists():
            stem, ext = os.path.splitext(safe_name)
            file_path = msg_dir / f"{stem}_{counter}{ext}"
            counter += 1

        with open(file_path, "wb") as f:
            f.write(payload)

        results.append((
            filename,
            part.get_content_type(),
            str(file_path),
            len(payload),
        ))

    return results


def fetch_all_folders(imap_conn):
    """Geef lijst van mapnamen terug."""
    folders = []
    status, mailbox_list = imap_conn.list()
    if status != "OK":
        return folders

    for entry in mailbox_list:
        decoded = entry.decode(errors="replace")
        # formaat: (\Flags) "/" "Mapnaam"
        parts = decoded.split(' "/" ')
        if len(parts) == 2:
            folder_name = parts[1].strip('"')
            folders.append(folder_name)
        else:
            # fallback parsing
            folder_name = decoded.split()[-1].strip('"')
            folders.append(folder_name)

    return folders


def import_folder(imap_conn, db_conn, folder_name, attachment_dir):
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
    for i, uid in enumerate(uids, 1):
        uid_str = uid.decode()

        # skip als al geimporteerd
        existing = db_conn.execute(
            "SELECT id FROM messages WHERE folder = ? AND uid = ?",
            (folder_name, uid_str)
        ).fetchall()
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

            cursor = db_conn.execute(
                """INSERT INTO messages
                   (folder, uid, message_id, from_addr, to_addr, cc_addr,
                    subject, date_sent, body_text, body_html, raw_size)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (folder_name, uid_str, message_id, from_addr, to_addr, cc_addr,
                 subject, date_sent, body_text, body_html, len(raw_email))
            )
            msg_db_id = cursor.lastrowid if hasattr(cursor, "lastrowid") else None

            if msg_db_id is None:
                row = db_conn.execute(
                    "SELECT id FROM messages WHERE folder = ? AND uid = ?",
                    (folder_name, uid_str)
                ).fetchone()
                msg_db_id = row[0]

            attachments = extract_attachments(msg, msg_db_id, attachment_dir)
            if attachments:
                db_conn.execute(
                    "UPDATE messages SET has_attachments = 1 WHERE id = ?",
                    (msg_db_id,)
                )
                for filename, content_type, file_path, file_size in attachments:
                    db_conn.execute(
                        """INSERT INTO attachments
                           (message_id, filename, content_type, file_path, file_size)
                           VALUES (?, ?, ?, ?, ?)""",
                        (msg_db_id, filename, content_type, file_path, file_size)
                    )

            count += 1

            if count % CONFIG["BATCH_SIZE"] == 0:
                db_conn.commit()
                if hasattr(db_conn, "sync"):
                    try:
                        db_conn.sync()
                    except Exception:
                        pass
                print(f"      ... {count}/{len(uids)} verwerkt")

        except Exception as e:
            print(f"  [!] Fout bij bericht uid={uid_str} in {folder_name}: {e}")
            continue

    db_conn.commit()
    return count


def main():
    if not CONFIG["IMAP_USER"] or not CONFIG["IMAP_PASS"]:
        print("FOUT: stel IMAP_USER en IMAP_PASS in (via environment variables of .env)")
        sys.exit(1)

    Path(CONFIG["ATTACHMENT_DIR"]).mkdir(parents=True, exist_ok=True)

    print(f"[*] Verbinden met IMAP-server {CONFIG['IMAP_HOST']}:{CONFIG['IMAP_PORT']} ...")
    imap_conn = imaplib.IMAP4_SSL(CONFIG["IMAP_HOST"], CONFIG["IMAP_PORT"])
    imap_conn.login(CONFIG["IMAP_USER"], CONFIG["IMAP_PASS"])
    print("[+] IMAP-login geslaagd")

    db_conn = get_db()

    folders = fetch_all_folders(imap_conn)
    print(f"[*] Gevonden mappen: {folders}")

    total = 0
    start = time.time()
    for folder in folders:
        # sla mappen over die geen mail bevatten zoals 'Noselect' mappen
        if "[Gmail]" in folder and folder.count("/") == 0:
            continue
        total += import_folder(imap_conn, db_conn, folder, CONFIG["ATTACHMENT_DIR"])

    if hasattr(db_conn, "sync"):
        try:
            db_conn.sync()
        except Exception:
            pass

    imap_conn.logout()

    elapsed = time.time() - start
    print(f"\n[+] Klaar! {total} nieuwe berichten geimporteerd in {elapsed:.1f}s")


if __name__ == "__main__":
    main()

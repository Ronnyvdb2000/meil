# meil

Importeert alle mail van een IMAP-account (Online.be / Proximedia) naar een [Turso](https://turso.tech) (libSQL) database. Attachments worden lokaal op schijf bewaard, enkel het bestandspad wordt in de database opgeslagen.

## Database-structuur

```
messages
├── id
├── folder          (bv. INBOX, Sent, Spam)
├── uid             (IMAP UID)
├── message_id      (email Message-ID header)
├── from_addr
├── to_addr
├── cc_addr
├── subject
├── date_sent
├── body_text
├── body_html
├── has_attachments
├── raw_size
└── imported_at

attachments
├── id
├── message_id      (FK → messages.id)
├── filename
├── content_type
├── file_path
└── file_size
```

## Installatie

```bash
pip install -r requirements.txt
```

## Configuratie

Kopieer `.env.example` naar `.env` en vul aan:

```bash
cp .env.example .env
```

| Variable             | Waarde                                       |
|----------------------|----------------------------------------------|
| `IMAP_HOST`          | `imap.online.be`                             |
| `IMAP_PORT`          | `993`                                        |
| `IMAP_USER`          | Je volledige e-mailadres                     |
| `IMAP_PASS`          | Je wachtwoord                                |
| `TURSO_DATABASE_URL` | `libsql://mail-archief-jouwnaam.turso.io`    |
| `TURSO_AUTH_TOKEN`   | Token van `turso db tokens create <naam>`    |
| `ATTACHMENT_DIR`     | Pad naar map voor bijlagen (standaard `./attachments`) |

> **Tip:** zonder Turso-credentials schrijft het script naar een lokaal `mail_archive.db` SQLite-bestand — handig om eerst te testen.

## Gebruik

```bash
python imap_to_turso.py
```

Het script is **idempotent**: berichten die al in de database zitten (op basis van map + UID) worden overgeslagen. Je kan het dus gerust meerdere keren draaien om nieuwe mail bij te halen.

## Turso database aanmaken

```bash
turso db create mail-archief
turso db show mail-archief --url
turso db tokens create mail-archief
```

## Bestandsstructuur

```
meil/
├── imap_to_turso.py   # hoofdscript
├── requirements.txt
├── .env.example       # sjabloon voor credentials
├── .env               # jouw credentials (niet in git!)
├── .gitignore
└── README.md
```

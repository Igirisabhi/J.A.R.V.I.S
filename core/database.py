"""SQLite persistence for frequently edited Jarvis commands and gestures."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "config" / "jarvis.db"
CORRECTIONS_PATH = ROOT / "config" / "command_corrections.json"
GESTURE_PATH = ROOT / "config" / "gesture_settings.json"
SCHEMA_VERSION = 1
_INITIALISE_LOCK = threading.RLock()
_INITIALISED = False


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _normalise_phrase(value: str) -> str:
    return " ".join(str(value).split()).casefold()


def _initialise() -> None:
    global _INITIALISED
    with _INITIALISE_LOCK:
        if _INITIALISED:
            return
        with _connect() as db:
            db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_commands (
                phrase TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS hand_sign_mappings (
                sign_key TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS custom_gestures (
                name_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                pose TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '',
                landmark_template TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
            )
            row = db.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                db.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(row["version"]) != SCHEMA_VERSION:
                raise sqlite3.DatabaseError(f"Unsupported Jarvis database schema: {row['version']}")
            _migrate_json_locked(db)
        _INITIALISED = True


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _backup_once(path: Path) -> None:
    if path.is_file():
        backup = path.with_name(path.name + ".pre-sqlite-backup")
        if not backup.exists():
            shutil.copy2(path, backup)


def _migrate_json_locked(db: sqlite3.Connection) -> None:
    corrections = {}
    gestures = {}
    try:
        corrections = json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        pass
    try:
        gestures = json.loads(GESTURE_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        pass

    if isinstance(corrections, dict):
        for phrase, command in (corrections.get("voice_commands") or {}).items():
            if str(phrase).strip() and str(command).strip():
                db.execute(
                    "INSERT OR IGNORE INTO voice_commands(phrase, command) VALUES (?, ?)",
                    (_normalise_phrase(phrase), str(command).strip()),
                )
    if isinstance(gestures, dict):
        for sign, action in (gestures.get("hand_sign_mappings") or {}).items():
            if str(sign).strip() and str(action).strip():
                db.execute(
                    "INSERT OR IGNORE INTO hand_sign_mappings(sign_key, action) VALUES (?, ?)",
                    (str(sign).strip(), str(action).strip()),
                )
        for item in gestures.get("custom_gestures") or []:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                continue
            name = str(item["name"]).strip()
            db.execute(
                """INSERT OR IGNORE INTO custom_gestures
                   (name_key, name, pose, action, icon, landmark_template)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    name.casefold(),
                    name,
                    str(item.get("pose", "")).strip(),
                    str(item.get("action", "")).strip(),
                    str(item.get("icon", "")).strip(),
                    json.dumps(item.get("landmark_template")) if item.get("landmark_template") is not None else None,
                ),
            )
    if isinstance(corrections, dict) and "voice_commands" in corrections:
        _backup_once(CORRECTIONS_PATH)
        corrections.pop("voice_commands", None)
        _atomic_write_json(CORRECTIONS_PATH, corrections)
    if isinstance(gestures, dict) and ("hand_sign_mappings" in gestures or "custom_gestures" in gestures):
        _backup_once(GESTURE_PATH)
        gestures.pop("hand_sign_mappings", None)
        gestures.pop("custom_gestures", None)
        _atomic_write_json(GESTURE_PATH, gestures)


def load_voice_commands() -> dict[str, str]:
    _initialise()
    with _connect() as db:
        return {row["phrase"]: row["command"] for row in db.execute(
            "SELECT phrase, command FROM voice_commands ORDER BY phrase"
        )}


def save_voice_command(phrase: str, command: str) -> None:
    _initialise()
    with _connect() as db:
        db.execute(
            """INSERT INTO voice_commands(phrase, command) VALUES (?, ?)
               ON CONFLICT(phrase) DO UPDATE SET command=excluded.command,
               updated_at=CURRENT_TIMESTAMP""",
            (_normalise_phrase(phrase), str(command).strip()),
        )


def delete_voice_command(phrase: str) -> bool:
    _initialise()
    with _connect() as db:
        result = db.execute("DELETE FROM voice_commands WHERE phrase = ?", (_normalise_phrase(phrase),))
        return result.rowcount > 0


def load_gesture_data() -> tuple[dict[str, str], list[dict]]:
    _initialise()
    with _connect() as db:
        hand_signs = {row["sign_key"]: row["action"] for row in db.execute(
            "SELECT sign_key, action FROM hand_sign_mappings ORDER BY sign_key"
        )}
        custom = []
        for row in db.execute("SELECT * FROM custom_gestures ORDER BY name"):
            item = {"name": row["name"], "pose": row["pose"], "action": row["action"], "icon": row["icon"]}
            if row["landmark_template"]:
                try:
                    template = json.loads(row["landmark_template"])
                except json.JSONDecodeError as error:
                    raise sqlite3.DatabaseError(f"Invalid landmark template for gesture {row['name']}: {error}") from error
                if not isinstance(template, list) or len(template) not in {63, 126}:
                    raise sqlite3.DatabaseError(f"Invalid landmark template for gesture {row['name']}")
                item["landmark_template"] = template
            custom.append(item)
        return hand_signs, custom


def save_gesture_data(hand_signs: dict, custom: list[dict]) -> None:
    _initialise()
    with _connect() as db:
        db.execute("DELETE FROM hand_sign_mappings")
        db.executemany(
            "INSERT INTO hand_sign_mappings(sign_key, action) VALUES (?, ?)",
            [(str(key).strip(), str(value).strip()) for key, value in hand_signs.items()],
        )
        db.execute("DELETE FROM custom_gestures")
        db.executemany(
            """INSERT INTO custom_gestures
               (name_key, name, pose, action, icon, landmark_template)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    str(item["name"]).strip().casefold(),
                    str(item["name"]).strip(),
                    str(item.get("pose", "")).strip(),
                    str(item.get("action", "")).strip(),
                    str(item.get("icon", "")).strip(),
                    json.dumps(item["landmark_template"]) if item.get("landmark_template") is not None else None,
                )
                for item in custom
            ],
        )

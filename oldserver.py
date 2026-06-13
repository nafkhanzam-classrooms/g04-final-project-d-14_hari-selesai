import base64
import binascii
import datetime
import hashlib
import json
import os
import uuid
import logging
import socket
import sqlite3
import sys
import threading


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("server.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("StudyBuddyServer")

history_logger = logging.getLogger("ChatHistory")
history_logger.propagate = False
history_logger.setLevel(logging.INFO)
chat_history_handler = logging.FileHandler("chat_history.log", encoding="utf-8")
chat_history_handler.setFormatter(logging.Formatter("%(message)s"))
history_logger.addHandler(chat_history_handler)

state_lock = threading.Lock()
db_lock = threading.Lock()

clients = {}
authenticating_users = set()
user_passwords = {}
user_statuses = {}
rooms = {"lobby": []}
room_messages = {"lobby": []}
private_messages = {}
room_topics = {"lobby": "hangout umum"}

USER_STORE_FILE = "users.json"
DEFAULT_STATUS = "Belum set status belajar"
MAX_VOICE_BASE64_LENGTH = 8 * 1024 * 1024
MAX_VOICE_DURATION_MS = 60_000
ALLOWED_AUDIO_MIME_PREFIX = "audio/"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILE_BASE64_LENGTH = 6 * 1024 * 1024
ALLOWED_REACTIONS = {"👍", "❤️", "😂", "🎉", "😮", "😢"}
MAX_ROOM_HISTORY = 20
DATABASE_FILE = "studibudi.db"


def get_db_connection():
    connection = sqlite3.connect(DATABASE_FILE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    """Membuat database SQLite dan tabel persistence secara otomatis."""
    with db_lock:
        with get_db_connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rooms (
                    name TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    room_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    FOREIGN KEY (room_name) REFERENCES rooms(name) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_messages_room_id
                    ON messages(room_name, id DESC);

                CREATE TABLE IF NOT EXISTS pending_private_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pending_private_recipient
                    ON pending_private_messages(recipient, id);
                """
            )
            connection.execute(
                """
                INSERT INTO rooms(name, topic, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO NOTHING
                """,
                ("lobby", "hangout umum", now_iso()),
            )


def persist_room(room_name: str, topic: str) -> bool:
    try:
        with db_lock:
            with get_db_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO rooms(name, topic, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET topic = excluded.topic
                    """,
                    (room_name, topic, now_iso()),
                )
        return True
    except sqlite3.Error as exc:
        log_error("[DATABASE] Failed to save room %s: %s", room_name, exc)
        return False


def persist_message(entry: dict) -> bool:
    """Menyimpan atau memperbarui pesan beserta reaction-nya."""
    try:
        payload = json.dumps(entry, ensure_ascii=False)
        with db_lock:
            with get_db_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO messages(
                        message_id, room_name, kind, sender, timestamp, data_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        room_name = excluded.room_name,
                        kind = excluded.kind,
                        sender = excluded.sender,
                        timestamp = excluded.timestamp,
                        data_json = excluded.data_json
                    """,
                    (
                        entry["message_id"],
                        entry["room"],
                        entry.get("kind", "text"),
                        entry["sender"],
                        entry["timestamp"],
                        payload,
                    ),
                )
        return True
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        log_error("[DATABASE] Failed to save message: %s", exc)
        return False


def persist_pending_private_message(recipient: str, entry: dict) -> bool:
    try:
        with db_lock:
            with get_db_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO pending_private_messages(
                        recipient, sender, text, timestamp
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (recipient, entry["sender"], entry["text"], entry["timestamp"]),
                )
        return True
    except (sqlite3.Error, KeyError) as exc:
        log_error("[DATABASE] Failed to queue private message: %s", exc)
        return False


def load_pending_private_messages(recipient: str) -> list[dict]:
    try:
        with db_lock:
            with get_db_connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, sender, text, timestamp
                    FROM pending_private_messages
                    WHERE recipient = ?
                    ORDER BY id ASC
                    """,
                    (recipient,),
                ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        log_error("[DATABASE] Failed to load private messages: %s", exc)
        return []


def delete_pending_private_messages(message_ids: list[int]) -> None:
    if not message_ids:
        return
    try:
        with db_lock:
            with get_db_connection() as connection:
                connection.executemany(
                    "DELETE FROM pending_private_messages WHERE id = ?",
                    [(message_id,) for message_id in message_ids],
                )
    except sqlite3.Error as exc:
        log_error("[DATABASE] Failed to delete delivered private messages: %s", exc)


def load_persistent_state() -> None:
    """Memuat room dan maksimal 20 history terakhir tiap room saat startup."""
    try:
        with db_lock:
            with get_db_connection() as connection:
                room_rows = connection.execute(
                    "SELECT name, topic FROM rooms ORDER BY created_at ASC, name ASC"
                ).fetchall()

                loaded_rooms = {}
                loaded_topics = {}
                loaded_messages = {}

                for row in room_rows:
                    room_name = row["name"]
                    loaded_rooms[room_name] = []
                    loaded_topics[room_name] = row["topic"]

                    message_rows = connection.execute(
                        """
                        SELECT data_json
                        FROM messages
                        WHERE room_name = ?
                        ORDER BY id DESC
                        LIMIT ?
                        """,
                        (room_name, MAX_ROOM_HISTORY),
                    ).fetchall()

                    history = []
                    for message_row in reversed(message_rows):
                        try:
                            entry = json.loads(message_row["data_json"])
                            if isinstance(entry, dict):
                                entry.setdefault("reactions", {})
                                history.append(entry)
                        except (json.JSONDecodeError, TypeError):
                            log_error(
                                "[DATABASE] Skipping corrupted message in room %s",
                                room_name,
                            )
                    loaded_messages[room_name] = history

        if "lobby" not in loaded_rooms:
            loaded_rooms["lobby"] = []
            loaded_topics["lobby"] = "hangout umum"
            loaded_messages["lobby"] = []

        with state_lock:
            rooms.clear()
            rooms.update(loaded_rooms)
            room_topics.clear()
            room_topics.update(loaded_topics)
            room_messages.clear()
            room_messages.update(loaded_messages)

        total_messages = sum(len(items) for items in loaded_messages.values())
        log_server(
            "[DATABASE] Loaded %d rooms and %d recent messages from %s",
            len(loaded_rooms),
            total_messages,
            DATABASE_FILE,
        )
    except sqlite3.Error as exc:
        log_error("[DATABASE] Failed to load persistent state: %s", exc)


def log_server(message: str, *args) -> None:
    logger.info(message, *args)


def log_error(message: str, *args) -> None:
    logger.error(message, *args)


def load_user_passwords() -> dict:
    try:
        with open(USER_STORE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log_error("Failed to load user store from %s", USER_STORE_FILE)
        return {}


def save_user_passwords() -> None:
    try:
        with state_lock:
            snapshot = dict(user_passwords)
        with open(USER_STORE_FILE, "w", encoding="utf-8") as file:
            json.dump(snapshot, file, ensure_ascii=False, indent=2)
    except OSError:
        log_error("Failed to save user store to %s", USER_STORE_FILE)


def now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def new_message_id() -> str:
    return uuid.uuid4().hex[:12]


def trim_room_history_locked(room_name: str) -> None:
    room_messages[room_name] = room_messages.get(room_name, [])[-MAX_ROOM_HISTORY:]


def encode_msg_obj(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def send_msg(sock, msg_type: str, payload, sender=None, room=None) -> bool:
    obj = {
        "type": msg_type,
        "sender": sender,
        "room": room,
        "payload": payload,
        "timestamp": now_iso(),
    }
    try:
        sock.sendall(encode_msg_obj(obj))
        return True
    except (OSError, RuntimeError):
        return False


def send_system(sock, text: str) -> bool:
    return send_msg(sock, "system", {"text": text})


def read_client_packet(reader):
    """Membaca satu paket JSON Lines. Raw text tetap diterima."""
    line = reader.readline()
    if not line:
        return None

    try:
        packet = json.loads(line)
        packet_type = packet.get("type")
        payload = packet.get("payload", {})

        if packet_type == "input" and isinstance(payload, dict):
            return str(payload.get("text", "")).strip()

        if packet_type == "voice_input" and isinstance(payload, dict):
            return {"type": "voice_input", "payload": payload}

        if packet_type == "file_input" and isinstance(payload, dict):
            return {"type": "file_input", "payload": payload}

        if packet_type == "reaction_input" and isinstance(payload, dict):
            return {"type": "reaction_input", "payload": payload}
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    return line.strip()


def find_user_room_locked(username: str) -> str:
    for room_name, members in rooms.items():
        if username in members:
            return room_name
    return "lobby"


def build_online_users_payload() -> dict:
    with state_lock:
        users = [
            {
                "username": username,
                "status": user_statuses.get(username, DEFAULT_STATUS),
                "room": find_user_room_locked(username),
            }
            for username in sorted(clients)
        ]
    return {"users": users}


def send_online_users(target_socket=None) -> None:
    payload = build_online_users_payload()
    if target_socket is not None:
        send_msg(target_socket, "online_users", payload)
        return

    with state_lock:
        sockets = list(clients.values())
    for sock in sockets:
        send_msg(sock, "online_users", payload)


def broadcast_status_update(username: str, status: str) -> None:
    payload = {
        "username": username,
        "status": status,
        "timestamp": now_iso(),
    }
    with state_lock:
        sockets = list(clients.values())
    for sock in sockets:
        send_msg(sock, "status_update", payload, sender=username)


def deliver_pending_private_messages(username: str, client_socket) -> None:
    # Antrean lama di memori tetap dibaca agar kompatibel dengan struktur sebelumnya.
    with state_lock:
        memory_pending = private_messages.pop(username, [])

    database_pending = load_pending_private_messages(username)
    pending = [
        {**entry, "database_id": None} for entry in memory_pending
    ] + [
        {
            "sender": entry["sender"],
            "text": entry["text"],
            "timestamp": entry["timestamp"],
            "database_id": entry["id"],
        }
        for entry in database_pending
    ]

    if not pending:
        return

    send_system(client_socket, "Pending private messages:")
    delivered_database_ids = []
    for entry in pending:
        delivered = send_msg(
            client_socket,
            "pm",
            {
                "sender": entry["sender"],
                "text": entry["text"],
                "timestamp": entry["timestamp"],
            },
            sender=entry["sender"],
        )
        if delivered and entry.get("database_id") is not None:
            delivered_database_ids.append(entry["database_id"])

    delete_pending_private_messages(delivered_database_ids)
    send_system(client_socket, "End pending messages")


def authenticate_user(client_socket, reader=None):
    if reader is None:
        reader = client_socket.makefile("r", encoding="utf-8")

    send_msg(client_socket, "prompt", {"text": "Enter your username:"})
    username = read_client_packet(reader)
    if not isinstance(username, str) or not username:
        send_msg(client_socket, "auth_result", {"success": False, "message": "Username tidak boleh kosong."})
        return None

    send_msg(client_socket, "prompt", {"text": "Enter your password:"})
    password = read_client_packet(reader)
    if not isinstance(password, str) or not password:
        send_msg(client_socket, "auth_result", {"success": False, "message": "Password tidak boleh kosong."})
        return None

    is_new_user = False
    with state_lock:
        if username in clients or username in authenticating_users:
            send_msg(client_socket, "auth_result", {"success": False, "message": "This user is already logged in."})
            return None

        stored_password = user_passwords.get(username)
        if stored_password is not None and stored_password != hash_password(password):
            send_msg(client_socket, "auth_result", {"success": False, "message": "Invalid password. Connection closing."})
            return None

        authenticating_users.add(username)
        if stored_password is None:
            user_passwords[username] = hash_password(password)
            is_new_user = True

    if is_new_user:
        save_user_passwords()
        message = "New user created and logged in."
        log_server("[SERVER] New user registered: %s", username)
    else:
        message = "Login successful."
        log_server("[SERVER] %s logged in", username)

    sent = send_msg(
        client_socket,
        "auth_result",
        {"success": True, "message": message, "username": username},
    )

    with state_lock:
        authenticating_users.discard(username)
        if sent:
            clients[username] = client_socket
            user_statuses.setdefault(username, DEFAULT_STATUS)

    return username if sent else None


user_passwords = load_user_passwords()
initialize_database()
load_persistent_state()


def validate_voice_payload(payload: dict):
    audio_data = payload.get("audio")
    mime_type = str(payload.get("mime_type", "audio/webm"))

    try:
        duration_ms = int(payload.get("duration_ms", 0))
    except (TypeError, ValueError):
        duration_ms = 0

    if not isinstance(audio_data, str) or not audio_data:
        return None, "Data voice message kosong."
    if len(audio_data) > MAX_VOICE_BASE64_LENGTH:
        return None, "Voice message terlalu besar. Maksimal sekitar 60 detik."
    if not mime_type.startswith(ALLOWED_AUDIO_MIME_PREFIX):
        return None, "Format voice message tidak didukung."
    if duration_ms < 0 or duration_ms > MAX_VOICE_DURATION_MS + 2_000:
        return None, "Durasi voice message melebihi batas 60 detik."

    return {
        "audio": audio_data,
        "mime_type": mime_type,
        "duration_ms": duration_ms,
    }, None


def handle_voice_message(username: str, current_room: str, payload: dict, client_socket) -> None:
    voice, error = validate_voice_payload(payload)
    if error:
        send_system(client_socket, error)
        return

    timestamp = now_iso()
    entry = {
        "message_id": new_message_id(),
        "kind": "voice",
        "room": current_room,
        "sender": username,
        "audio": voice["audio"],
        "mime_type": voice["mime_type"],
        "duration_ms": voice["duration_ms"],
        "timestamp": timestamp,
        "reactions": {},
    }

    with state_lock:
        room_messages.setdefault(current_room, []).append(entry)
        trim_room_history_locked(current_room)

    persist_message(entry)
    broadcast_voice_to_room(current_room, entry)
    history_logger.info(
        "[%s] [%s] %s: [VOICE %.1f seconds]",
        timestamp,
        current_room,
        username,
        voice["duration_ms"] / 1000,
    )
    log_server(
        "[%s] %s sent voice message (%.1f seconds)",
        current_room,
        username,
        voice["duration_ms"] / 1000,
    )


def validate_file_payload(payload: dict):
    filename = os.path.basename(str(payload.get("filename", "")).strip())[:120]
    mime_type = str(payload.get("mime_type", "application/octet-stream")).strip()[:100]
    file_data = payload.get("data")

    try:
        declared_size = int(payload.get("size", 0))
    except (TypeError, ValueError):
        declared_size = 0

    if not filename:
        return None, "Nama file tidak valid."
    if not isinstance(file_data, str) or not file_data:
        return None, "Data file kosong."
    if len(file_data) > MAX_FILE_BASE64_LENGTH:
        return None, "File terlalu besar. Maksimal 4 MB."

    try:
        raw_size = len(base64.b64decode(file_data, validate=True))
    except (binascii.Error, ValueError):
        return None, "Data file Base64 tidak valid."

    if raw_size <= 0 or raw_size > MAX_FILE_BYTES:
        return None, "File terlalu besar. Maksimal 4 MB."
    if declared_size and declared_size != raw_size:
        return None, "Ukuran file tidak sesuai."

    return {
        "filename": filename,
        "mime_type": mime_type or "application/octet-stream",
        "data": file_data,
        "size": raw_size,
    }, None


def handle_file_message(username: str, current_room: str, payload: dict, client_socket) -> None:
    file_info, error = validate_file_payload(payload)
    if error:
        send_system(client_socket, error)
        return

    timestamp = now_iso()
    entry = {
        "message_id": new_message_id(),
        "kind": "file",
        "room": current_room,
        "sender": username,
        "filename": file_info["filename"],
        "mime_type": file_info["mime_type"],
        "data": file_info["data"],
        "size": file_info["size"],
        "timestamp": timestamp,
        "reactions": {},
    }

    with state_lock:
        room_messages.setdefault(current_room, []).append(entry)
        trim_room_history_locked(current_room)

    persist_message(entry)
    broadcast_file_to_room(current_room, entry)
    history_logger.info(
        "[%s] [%s] %s: [FILE %s, %d bytes]",
        timestamp, current_room, username, file_info["filename"], file_info["size"]
    )
    log_server(
        "[%s] %s sent file %s (%d bytes)",
        current_room, username, file_info["filename"], file_info["size"]
    )


def handle_reaction(username: str, current_room: str, payload: dict, client_socket) -> None:
    message_id = str(payload.get("message_id", "")).strip()
    emoji = str(payload.get("emoji", "")).strip()

    if not message_id or emoji not in ALLOWED_REACTIONS:
        send_system(client_socket, "Reaction tidak valid.")
        return

    updated = None
    updated_entry = None
    with state_lock:
        for entry in room_messages.get(current_room, []):
            if entry.get("message_id") != message_id:
                continue

            reactions = entry.setdefault("reactions", {})
            users = reactions.setdefault(emoji, [])
            if username in users:
                users.remove(username)
                if not users:
                    reactions.pop(emoji, None)
            else:
                users.append(username)

            updated = {key: list(value) for key, value in reactions.items()}
            updated_entry = dict(entry)
            updated_entry["reactions"] = updated
            break

    if updated is None or updated_entry is None:
        send_system(client_socket, "Pesan untuk reaction tidak ditemukan di room aktif.")
        return

    persist_message(updated_entry)
    broadcast_reaction_to_room(current_room, message_id, updated)


def send_history_entry(client_socket, entry: dict, room_name: str) -> None:
    common = {
        "message_id": entry.get("message_id"),
        "timestamp": entry.get("timestamp", ""),
        "history": True,
        "reactions": entry.get("reactions", {}),
    }

    if entry.get("kind") == "voice":
        send_msg(
            client_socket,
            "voice",
            {**common, "audio": entry["audio"], "mime_type": entry["mime_type"], "duration_ms": entry.get("duration_ms", 0)},
            sender=entry["sender"],
            room=room_name,
        )
    elif entry.get("kind") == "file":
        send_msg(
            client_socket,
            "file",
            {**common, "filename": entry["filename"], "mime_type": entry["mime_type"], "data": entry["data"], "size": entry.get("size", 0)},
            sender=entry["sender"],
            room=room_name,
        )
    else:
        send_msg(
            client_socket,
            "history",
            {
                "message_id": entry.get("message_id"),
                "sender": entry["sender"],
                "text": entry["text"],
                "timestamp": entry["timestamp"],
                "reactions": entry.get("reactions", {}),
            },
            sender=entry["sender"],
            room=room_name,
        )


def handle_client(client_socket, addr) -> None:
    username = None
    current_room = "lobby"

    try:
        reader = client_socket.makefile("r", encoding="utf-8")
        username = authenticate_user(client_socket, reader)
        if not username:
            return

        with state_lock:
            if username not in rooms["lobby"]:
                rooms["lobby"].append(username)

        deliver_pending_private_messages(username, client_socket)
        log_server("[SERVER] %s joined from %s", username, addr)
        broadcast_to_room(
            "lobby",
            {
                "room": "lobby",
                "sender": "System",
                "text": f"{username} joined the lobby",
                "timestamp": now_iso(),
            },
            "System",
        )
        send_online_users()

        while True:
            message = read_client_packet(reader)
            if message is None:
                break

            if isinstance(message, dict):
                packet_type = message.get("type")
                payload = message.get("payload", {})
                if packet_type == "voice_input":
                    handle_voice_message(username, current_room, payload, client_socket)
                elif packet_type == "file_input":
                    handle_file_message(username, current_room, payload, client_socket)
                elif packet_type == "reaction_input":
                    handle_reaction(username, current_room, payload, client_socket)
                continue

            if not isinstance(message, str) or not message:
                continue

            if message.startswith("/"):
                current_room = handle_command(username, message, client_socket, current_room)
                if current_room is None:
                    break
                continue

            timestamp = now_iso()
            entry = {
                "message_id": new_message_id(),
                "kind": "text",
                "room": current_room,
                "sender": username,
                "text": message,
                "timestamp": timestamp,
                "reactions": {},
            }

            with state_lock:
                room_messages.setdefault(current_room, []).append(entry)
                trim_room_history_locked(current_room)

            persist_message(entry)
            broadcast_to_room(current_room, entry, username)
            history_logger.info("[%s] [%s] %s: %s", timestamp, current_room, username, message)
            log_server("[%s] %s: %s", current_room, username, message)

    except (ConnectionResetError, BrokenPipeError):
        log_server("[SERVER] Connection lost from %s", addr)
    except Exception as exc:
        log_error("[ERROR] Client %s: %s", addr, exc)
    finally:
        if username:
            with state_lock:
                if clients.get(username) is client_socket:
                    del clients[username]
                for members in rooms.values():
                    if username in members:
                        members.remove(username)

            broadcast_to_room(
                current_room,
                {
                    "room": current_room,
                    "sender": "System",
                    "text": f"{username} left",
                    "timestamp": now_iso(),
                },
                "System",
            )
            send_online_users()
            log_server("[SERVER] %s disconnected", username)

        try:
            client_socket.close()
        except OSError:
            pass


def handle_command(username, cmd, client_socket, current_room):
    parts = cmd.split(" ", 2)
    command = parts[0].lower()

    if command == "/create":
        if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
            send_system(client_socket, "Usage: /create <room_name> <topic>")
            return current_room

        room_name = parts[1].lower().strip()
        topic = parts[2].strip()

        with state_lock:
            if room_name not in rooms:
                rooms[room_name] = []
                room_messages[room_name] = []
                room_topics[room_name] = topic
                created = True
            else:
                created = False

        if created:
            persist_room(room_name, topic)
            send_system(client_socket, f"Created room: {room_name} (topic: {topic})")
            log_server("[SERVER] Room created: %s (topic: %s)", room_name, topic)
        else:
            send_system(client_socket, "Room already exists!")
        return current_room

    if command == "/join":
        if len(parts) < 2 or not parts[1].strip():
            send_system(client_socket, "Usage: /join <room_name>")
            return current_room

        room_name = parts[1].lower().strip()

        with state_lock:
            if room_name not in rooms:
                room_exists = False
                history = []
                topic = ""
            else:
                room_exists = True
                if username in rooms.get(current_room, []):
                    rooms[current_room].remove(username)
                if username not in rooms[room_name]:
                    rooms[room_name].append(username)
                history = list(room_messages[room_name][-5:])
                topic = room_topics.get(room_name, "")

        if not room_exists:
            send_system(client_socket, "Room doesn't exist!")
            return current_room

        current_room = room_name
        send_msg(
            client_socket,
            "room_joined",
            {"room": room_name, "topic": topic, "text": f"Joined room '{room_name}'"},
            room=room_name,
        )

        if history:
            for entry in history:
                send_history_entry(client_socket, entry, room_name)
        else:
            send_system(client_socket, "(no messages yet)")
        send_system(client_socket, "---")

        broadcast_to_room(
            room_name,
            {
                "room": room_name,
                "sender": "System",
                "text": f"{username} joined",
                "timestamp": now_iso(),
            },
            "System",
        )
        send_online_users()
        log_server("[SERVER] %s joined room: %s", username, room_name)
        return current_room

    if command == "/leave":
        if current_room == "lobby":
            send_system(client_socket, "You are already in the lobby.")
            return current_room

        old_room = current_room
        with state_lock:
            if username in rooms.get(old_room, []):
                rooms[old_room].remove(username)
            if username not in rooms["lobby"]:
                rooms["lobby"].append(username)
            history = list(room_messages["lobby"][-5:])
            lobby_topic = room_topics.get("lobby", "")

        current_room = "lobby"
        broadcast_to_room(
            old_room,
            {"room": old_room, "sender": "System", "text": f"{username} left", "timestamp": now_iso()},
            "System",
        )
        send_msg(
            client_socket,
            "room_joined",
            {"room": "lobby", "topic": lobby_topic, "text": f"Left '{old_room}' and joined lobby"},
            room="lobby",
        )

        if history:
            for entry in history:
                send_history_entry(client_socket, entry, "lobby")
        else:
            send_system(client_socket, "(no messages yet)")
        send_system(client_socket, "---")

        broadcast_to_room(
            "lobby",
            {"room": "lobby", "sender": "System", "text": f"{username} joined the lobby", "timestamp": now_iso()},
            "System",
        )
        send_online_users()
        log_server("[SERVER] %s left room %s and returned to lobby", username, old_room)
        return current_room

    if command == "/rooms":
        with state_lock:
            rooms_list = [
                {"name": room_name, "members": len(members), "topic": room_topics.get(room_name, "")}
                for room_name, members in rooms.items()
            ]
        send_msg(client_socket, "rooms", {"rooms": rooms_list})
        return current_room

    if command == "/msg":
        if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
            send_system(client_socket, "Usage: /msg <user> <message>")
            return current_room

        recipient = parts[1].strip()
        message_text = parts[2].strip()
        timestamp = now_iso()
        entry = {"sender": username, "text": message_text, "timestamp": timestamp}

        with state_lock:
            if recipient not in user_passwords:
                recipient_exists = False
                recipient_socket = None
            else:
                recipient_exists = True
                recipient_socket = clients.get(recipient)

        if recipient_exists and recipient_socket is None:
            persist_pending_private_message(recipient, entry)

        if not recipient_exists:
            send_system(client_socket, "User not found.")
            return current_room

        if recipient_socket is not None:
            send_msg(
                recipient_socket,
                "pm",
                {"sender": username, "text": message_text, "timestamp": timestamp},
                sender=username,
            )
            delivery_note = ""
        else:
            delivery_note = " (queued: user is offline)"

        send_system(client_socket, f"[PM to {recipient}] {message_text}{delivery_note}")
        log_server("[PM] %s -> %s: %s", username, recipient, message_text)
        return current_room

    if command == "/users":
        with state_lock:
            members = list(rooms.get(current_room, []))
        lines = [f"Users in {current_room}:"]
        lines.extend(f"  - {user}" for user in members)
        send_system(client_socket, "\n".join(lines))
        return current_room

    if command == "/online":
        send_online_users(client_socket)
        return current_room

    if command == "/status":
        status = cmd.partition(" ")[2].strip()
        if not status:
            send_system(client_socket, "Usage: /status <learning_status>")
            return current_room

        with state_lock:
            user_statuses[username] = status
        broadcast_status_update(username, status)
        log_server("[STATUS] %s: %s", username, status)
        return current_room

    if command == "/help":
        help_text = (
            "Commands:\n"
            "  /create <room> <topic>  - Create a study room\n"
            "  /join <room>            - Join a room\n"
            "  /leave                  - Leave current room and return to lobby\n"
            "  /msg <user> <message>   - Send a private message\n"
            "  /rooms                  - List all rooms\n"
            "  /users                  - List room members\n"
            "  /online                 - List online users and status\n"
            "  /status <text>          - Set learning status\n"
            "  /help                   - Show this help\n"
            "  Voice message           - Use microphone button on web client\n"
            "  File transfer           - Use attachment button on web client\n"
            "  Emoji/reaction          - Use emoji and reaction buttons on web client"
        )
        send_system(client_socket, help_text)
        return current_room

    send_system(client_socket, "Unknown command. Type /help")
    return current_room


def broadcast_to_room(room_name, message, sender) -> None:
    with state_lock:
        usernames = list(rooms.get(room_name, []))
        sockets = [clients.get(username) for username in usernames]

    for sock in sockets:
        if sock is None:
            continue
        payload = dict(message) if isinstance(message, dict) else {"text": str(message)}
        send_msg(sock, "chat", payload, sender=sender, room=room_name)


def broadcast_voice_to_room(room_name: str, entry: dict) -> None:
    payload = {
        "message_id": entry.get("message_id"),
        "audio": entry["audio"],
        "mime_type": entry["mime_type"],
        "duration_ms": entry.get("duration_ms", 0),
        "timestamp": entry["timestamp"],
        "history": False,
        "reactions": entry.get("reactions", {}),
    }

    with state_lock:
        usernames = list(rooms.get(room_name, []))
        sockets = [clients.get(username) for username in usernames]

    for sock in sockets:
        if sock is not None:
            send_msg(sock, "voice", payload, sender=entry["sender"], room=room_name)


def broadcast_file_to_room(room_name: str, entry: dict) -> None:
    payload = {
        "message_id": entry.get("message_id"),
        "filename": entry["filename"],
        "mime_type": entry["mime_type"],
        "data": entry["data"],
        "size": entry.get("size", 0),
        "timestamp": entry["timestamp"],
        "history": False,
        "reactions": entry.get("reactions", {}),
    }

    with state_lock:
        sockets = [clients.get(username) for username in rooms.get(room_name, [])]
    for sock in sockets:
        if sock is not None:
            send_msg(sock, "file", payload, sender=entry["sender"], room=room_name)


def broadcast_reaction_to_room(room_name: str, message_id: str, reactions: dict) -> None:
    payload = {"message_id": message_id, "reactions": reactions}
    with state_lock:
        sockets = [clients.get(username) for username in rooms.get(room_name, [])]
    for sock in sockets:
        if sock is not None:
            send_msg(sock, "reaction_update", payload, room=room_name)


def main() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 12345))
    server.listen(100)

    log_server("[SERVER] StudiBudi Chat Server started on port 12345")
    log_server("[SERVER] Waiting for connections...")

    try:
        while True:
            client_socket, addr = server.accept()
            log_server("[SERVER] New connection from %s", addr)
            thread = threading.Thread(target=handle_client, args=(client_socket, addr), daemon=True)
            thread.start()
    except KeyboardInterrupt:
        log_server("[SERVER] Shutting down...")
    finally:
        server.close()


if __name__ == "__main__":
    main()

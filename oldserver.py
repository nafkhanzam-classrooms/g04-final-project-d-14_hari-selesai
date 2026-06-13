import datetime
import hashlib
import json
import logging
import socket
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

# Lock untuk seluruh shared state agar aman ketika banyak client aktif bersamaan.
state_lock = threading.Lock()

clients = {}  # username -> socket
user_passwords = {}  # username -> hashed password
user_statuses = {}  # username -> status belajar
rooms = {"lobby": []}  # room_name -> list username
room_messages = {"lobby": []}  # room_name -> list message entry
private_messages = {}  # username -> pending private messages
room_topics = {"lobby": "hangout umum"}

USER_STORE_FILE = "users.json"
DEFAULT_STATUS = "Belum set status belajar"


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


def format_history_entry(entry: dict) -> str:
    return f"[{entry['timestamp']}] [{entry.get('room', '-')}] {entry['sender']}: {entry['text']}"


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def encode_msg_obj(obj: dict) -> bytes:
    """Serialisasi seluruh paket server sebagai JSON Lines."""
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def send_msg(
    sock,
    msg_type: str,
    payload: dict | str,
    sender: str | None = None,
    room: str | None = None,
) -> bool:
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


def read_input(reader) -> str | None:
    """Membaca satu paket input. Raw text tetap diterima untuk kompatibilitas."""
    line = reader.readline()
    if not line:
        return None

    try:
        packet = json.loads(line)
        payload = packet.get("payload", {})
        if packet.get("type") == "input" and isinstance(payload, dict):
            return str(payload.get("text", "")).strip()
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
    with state_lock:
        pending = private_messages.pop(username, [])

    if not pending:
        return

    send_system(client_socket, "Pending private messages:")
    for entry in pending:
        send_msg(
            client_socket,
            "pm",
            {
                "sender": entry["sender"],
                "text": entry["text"],
                "timestamp": entry["timestamp"],
            },
            sender=entry["sender"],
        )
    send_system(client_socket, "End pending messages")


def authenticate_user(client_socket, reader=None) -> str | None:
    """Autentikasi sederhana: akun baru dibuat otomatis pada login pertama."""
    if reader is None:
        reader = client_socket.makefile("r", encoding="utf-8")

    send_msg(client_socket, "prompt", {"text": "Enter your username:"})
    username = read_input(reader)
    if not username:
        send_msg(
            client_socket,
            "auth_result",
            {"success": False, "message": "Username tidak boleh kosong."},
        )
        return None

    send_msg(client_socket, "prompt", {"text": "Enter your password:"})
    password = read_input(reader)
    if not password:
        send_msg(
            client_socket,
            "auth_result",
            {"success": False, "message": "Password tidak boleh kosong."},
        )
        return None

    is_new_user = False
    with state_lock:
        if username in clients:
            send_msg(
                client_socket,
                "auth_result",
                {
                    "success": False,
                    "message": "This user is already logged in.",
                },
            )
            return None

        stored_password = user_passwords.get(username)
        if stored_password is not None and stored_password != hash_password(password):
            send_msg(
                client_socket,
                "auth_result",
                {
                    "success": False,
                    "message": "Invalid password. Connection closing.",
                },
            )
            return None

        if stored_password is None:
            user_passwords[username] = hash_password(password)
            is_new_user = True

        # Username langsung direservasi di dalam lock untuk mencegah login ganda.
        clients[username] = client_socket
        user_statuses.setdefault(username, DEFAULT_STATUS)

    if is_new_user:
        save_user_passwords()
        message = "New user created and logged in."
        log_server("[SERVER] New user registered: %s", username)
    else:
        message = "Login successful."
        log_server("[SERVER] %s logged in", username)

    send_msg(
        client_socket,
        "auth_result",
        {"success": True, "message": message, "username": username},
    )
    return username


user_passwords = load_user_passwords()


def handle_client(client_socket, addr) -> None:
    username = None
    current_room = "lobby"
    reader = None

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
            msg = read_input(reader)
            if msg is None:
                break
            if not msg:
                continue

            if msg.startswith("/"):
                current_room = handle_command(
                    username, msg, client_socket, current_room
                )
                if current_room is None:
                    break
            else:
                timestamp = now_iso()
                entry = {
                    "room": current_room,
                    "sender": username,
                    "text": msg,
                    "timestamp": timestamp,
                }

                with state_lock:
                    room_messages.setdefault(current_room, []).append(entry)

                broadcast_to_room(current_room, entry, username)
                history_logger.info("%s", format_history_entry(entry))
                log_server("[%s] %s: %s", current_room, username, msg)

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
            send_system(
                client_socket,
                f"Created room: {room_name} (topic: {topic})",
            )
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
            {
                "room": room_name,
                "topic": topic,
                "text": f"Joined room '{room_name}'",
            },
            room=room_name,
        )

        if history:
            for entry in history:
                send_msg(
                    client_socket,
                    "history",
                    {
                        "sender": entry["sender"],
                        "text": entry["text"],
                        "timestamp": entry["timestamp"],
                    },
                    sender=entry["sender"],
                    room=room_name,
                )
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
            {
                "room": old_room,
                "sender": "System",
                "text": f"{username} left",
                "timestamp": now_iso(),
            },
            "System",
        )
        send_msg(
            client_socket,
            "room_joined",
            {
                "room": "lobby",
                "topic": lobby_topic,
                "text": f"Left '{old_room}' and joined lobby",
            },
            room="lobby",
        )

        if history:
            for entry in history:
                send_msg(
                    client_socket,
                    "history",
                    {
                        "sender": entry["sender"],
                        "text": entry["text"],
                        "timestamp": entry["timestamp"],
                    },
                    sender=entry["sender"],
                    room="lobby",
                )
        else:
            send_system(client_socket, "(no messages yet)")
        send_system(client_socket, "---")

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
        log_server(
            "[SERVER] %s left room %s and returned to lobby",
            username,
            old_room,
        )
        return current_room

    if command == "/rooms":
        with state_lock:
            rooms_list = [
                {
                    "name": room_name,
                    "members": len(members),
                    "topic": room_topics.get(room_name, ""),
                }
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
        entry = {
            "sender": username,
            "text": message_text,
            "timestamp": timestamp,
        }

        with state_lock:
            if recipient not in user_passwords:
                recipient_exists = False
                recipient_socket = None
            else:
                recipient_exists = True
                recipient_socket = clients.get(recipient)
                # Pesan hanya disimpan sebagai pending jika penerima sedang offline.
                if recipient_socket is None:
                    private_messages.setdefault(recipient, []).append(entry)

        if not recipient_exists:
            send_system(client_socket, "User not found.")
            return current_room

        if recipient_socket is not None:
            send_msg(
                recipient_socket,
                "pm",
                {
                    "sender": username,
                    "text": message_text,
                    "timestamp": timestamp,
                },
                sender=username,
            )
            delivery_note = ""
        else:
            delivery_note = " (queued: user is offline)"

        send_system(
            client_socket,
            f"[PM to {recipient}] {message_text}{delivery_note}",
        )
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
            "  /msg <user> <message>   - Send a private message to another user\n"
            "  /rooms                  - List all rooms\n"
            "  /users                  - List room members\n"
            "  /online                 - List online users and their status\n"
            "  /status <text>          - Set learning status\n"
            "  /help                   - Show this help"
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
        if isinstance(message, dict):
            payload = dict(message)
        else:
            payload = {"text": str(message)}
        send_msg(sock, "chat", payload, sender=sender, room=room_name)


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
            thread = threading.Thread(
                target=handle_client,
                args=(client_socket, addr),
                daemon=True,
            )
            thread.start()
    except KeyboardInterrupt:
        log_server("[SERVER] Shutting down...")
    finally:
        server.close()


if __name__ == "__main__":
    main()

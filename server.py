import socket
import threading
import json
import datetime
import logging
import hashlib
import sys

# ─────────────────────────── Logging Setup ───────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("server.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("StudiBudiServer")

history_logger = logging.getLogger("ChatHistory")
history_logger.propagate = False
history_logger.setLevel(logging.INFO)
_chat_handler = logging.FileHandler("chat_history.log", encoding="utf-8")
_chat_handler.setFormatter(logging.Formatter("%(message)s"))
history_logger.addHandler(_chat_handler)

# ─────────────────────────── Shared State ────────────────────────────

state_lock = threading.Lock()

clients: dict[str, socket.socket] = {}          # username -> socket
user_passwords: dict[str, str] = {}             # username -> hashed password
user_study_status: dict[str, str] = {}          # username -> status string (topic)

rooms: dict[str, list[str]] = {"lobby": []}     # room_name -> [username, ...]
room_messages: dict[str, list[dict]] = {"lobby": []}
room_topics: dict[str, str] = {"lobby": "Hangout umum"}

private_messages: dict[str, list[dict]] = {}    # username -> [pending PM dicts]

USER_STORE_FILE = "users.json"

# ────────────────────────── Persistence ──────────────────────────────

def load_user_passwords() -> dict:
    try:
        with open(USER_STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.error("Failed to load user store from %s", USER_STORE_FILE)
        return {}


def save_user_passwords() -> None:
    try:
        with open(USER_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(user_passwords, f, ensure_ascii=False, indent=2)
    except OSError:
        logger.error("Failed to save user store to %s", USER_STORE_FILE)


user_passwords = load_user_passwords()

# ─────────────────────────── Utilities ───────────────────────────────

def now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def encode_packet(msg_type: str, payload: dict, sender: str = None, room: str = None) -> bytes:
    obj = {
        "type": msg_type,
        "sender": sender,
        "room": room,
        "payload": payload,
        "timestamp": now_iso(),
    }
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def send_msg(sock: socket.socket, msg_type: str, payload: dict,
             sender: str = None, room: str = None) -> None:
    """Send a single JSON packet to a socket. Always uses send_msg — no raw bytes."""
    try:
        sock.sendall(encode_packet(msg_type, payload, sender, room))
    except OSError:
        pass


def send_system(sock: socket.socket, text: str) -> None:
    send_msg(sock, "system", {"text": text})


# ────────────────────────── Broadcasting ─────────────────────────────

def broadcast_to_room(room_name: str, text: str, sender: str = "System",
                      exclude: str = None) -> None:
    """Broadcast a plain-text chat message to everyone in a room."""
    with state_lock:
        if room_name not in rooms:
            return
        targets = [(u, clients.get(u)) for u in rooms[room_name] if u != exclude]

    ts = now_iso()
    for username, sock in targets:
        if sock:
            send_msg(sock, "chat",
                     {"text": text, "sender": sender, "room": room_name, "timestamp": ts},
                     sender=sender, room=room_name)


def broadcast_study_status(username: str, status: str) -> None:
    """Broadcast a user's study status to ALL connected clients."""
    with state_lock:
        targets = [(u, clients.get(u)) for u in clients]

    ts = now_iso()
    for _, sock in targets:
        if sock:
            send_msg(sock, "status_update",
                     {"username": username, "status": status, "timestamp": ts})


# ───────────────────────── Authentication ────────────────────────────

def read_input_packet(reader) -> str | None:
    """Read one newline-delimited JSON packet from reader, return text payload."""
    line = reader.readline()
    if not line:
        return None
    try:
        obj = json.loads(line)
        return obj.get("payload", {}).get("text", "").strip()
    except Exception:
        return line.strip()


def authenticate_user(client_socket: socket.socket, reader) -> str | None:
    send_msg(client_socket, "prompt", {"text": "Enter your username:"})
    username = read_input_packet(reader)
    if not username:
        return None

    send_msg(client_socket, "prompt", {"text": "Enter your password:"})
    password = read_input_packet(reader)
    if not password:
        return None

    with state_lock:
        # Duplicate login check
        if username in clients:
            send_system(client_socket, "ERROR: User already logged in. Connection closing.")
            return None

        if username in user_passwords:
            if user_passwords[username] != hash_password(password):
                send_system(client_socket, "ERROR: Invalid password. Connection closing.")
                return None
            send_system(client_socket, f"Login successful. Welcome back, {username}!")
            logger.info("%s logged in", username)
        else:
            user_passwords[username] = hash_password(password)
            save_user_passwords()
            send_system(client_socket, f"New account created. Welcome, {username}!")
            logger.info("New user registered: %s", username)

    return username


# ──────────────────────── Private Messages ───────────────────────────

def deliver_pending_pms(username: str, sock: socket.socket) -> None:
    with state_lock:
        pending = private_messages.pop(username, [])
    if not pending:
        return
    send_system(sock, f"--- {len(pending)} pending private message(s) ---")
    for entry in pending:
        send_msg(sock, "pm", {"sender": entry["sender"],
                              "text": entry["text"],
                              "timestamp": entry["timestamp"]})
    send_system(sock, "--- End of pending messages ---")


# ──────────────────────── Command Handlers ───────────────────────────

def cmd_create(username, parts, sock, current_room) -> str:
    if len(parts) < 3:
        send_system(sock, "Usage: /create <room_name> <topic>")
        return current_room
    room_name = parts[1].lower()
    topic = parts[2]
    with state_lock:
        if room_name in rooms:
            send_system(sock, f"Room '{room_name}' already exists.")
            return current_room
        rooms[room_name] = []
        room_messages[room_name] = []
        room_topics[room_name] = topic
    send_system(sock, f"Room '{room_name}' created (topic: {topic}).")
    logger.info("Room created: %s (topic: %s) by %s", room_name, topic, username)
    return current_room


def cmd_join(username, parts, sock, current_room) -> str:
    if len(parts) < 2:
        send_system(sock, "Usage: /join <room_name>")
        return current_room
    room_name = parts[1].lower()

    with state_lock:
        if room_name not in rooms:
            send_system(sock, f"Room '{room_name}' does not exist.")
            return current_room
        if room_name == current_room:
            send_system(sock, f"You are already in '{room_name}'.")
            return current_room

        # Move user from old room to new room
        if username in rooms.get(current_room, []):
            rooms[current_room].remove(username)
        if username not in rooms[room_name]:
            rooms[room_name].append(username)

        history = room_messages[room_name][-5:]

    send_system(sock, f"Joined room '{room_name}' (topic: {room_topics.get(room_name, '-')}).")
    if history:
        send_system(sock, "--- Last 5 messages ---")
        for entry in history:
            send_msg(sock, "history", entry)
        send_system(sock, "--- End of history ---")
    else:
        send_system(sock, "(No messages yet in this room.)")

    broadcast_to_room(room_name, f"{username} joined the room.", exclude=username)
    logger.info("%s joined room: %s", username, room_name)
    return room_name


def cmd_leave(username, parts, sock, current_room) -> str:
    if current_room == "lobby":
        send_system(sock, "You are already in the lobby.")
        return current_room

    old_room = current_room
    with state_lock:
        if username in rooms.get(old_room, []):
            rooms[old_room].remove(username)
        if username not in rooms["lobby"]:
            rooms["lobby"].append(username)
        history = room_messages["lobby"][-5:]

    broadcast_to_room(old_room, f"{username} left the room.")
    send_system(sock, f"Left '{old_room}'. Back in lobby.")
    if history:
        send_system(sock, "--- Last 5 messages in lobby ---")
        for entry in history:
            send_msg(sock, "history", entry)
        send_system(sock, "--- End of history ---")
    broadcast_to_room("lobby", f"{username} returned to the lobby.", exclude=username)
    logger.info("%s left room %s -> lobby", username, old_room)
    return "lobby"


def cmd_rooms(username, parts, sock, current_room) -> str:
    with state_lock:
        rooms_list = [
            {"name": rn, "members": len(rooms[rn]), "topic": room_topics.get(rn, "-")}
            for rn in rooms
        ]
    send_msg(sock, "rooms", {"rooms": rooms_list})
    return current_room


def cmd_users(username, parts, sock, current_room) -> str:
    with state_lock:
        # Users in current room
        room_users = list(rooms.get(current_room, []))
        # All online users
        all_online = list(clients.keys())

    lines = [f"Users in '{current_room}': " + ", ".join(room_users) if room_users else f"No users in '{current_room}'."]
    lines.append("All online: " + ", ".join(all_online) if all_online else "No users online.")
    send_system(sock, "\n".join(lines))
    return current_room


def cmd_online(username, parts, sock, current_room) -> str:
    """Show all online users with their study status."""
    with state_lock:
        online = {u: user_study_status.get(u, "(no status)") for u in clients}
    if not online:
        send_system(sock, "No users currently online.")
        return current_room
    lines = ["--- Online Users ---"]
    for u, status in online.items():
        lines.append(f"  {u}: {status}")
    lines.append("--------------------")
    send_system(sock, "\n".join(lines))
    return current_room


def cmd_status(username, parts, sock, current_room) -> str:
    """Set and broadcast study status: /status <topik yang sedang dikerjakan>"""
    if len(parts) < 2:
        send_system(sock, "Usage: /status <your study topic>")
        return current_room
    status_text = " ".join(parts[1:])
    with state_lock:
        user_study_status[username] = status_text
    send_system(sock, f"Status updated: '{status_text}'")
    broadcast_study_status(username, status_text)
    logger.info("%s updated status: %s", username, status_text)
    return current_room


def cmd_msg(username, parts, sock, current_room) -> str:
    if len(parts) < 3:
        send_system(sock, "Usage: /msg <user> <message>")
        return current_room
    recipient = parts[1]
    message_text = " ".join(parts[2:])

    with state_lock:
        if recipient not in user_passwords:
            send_system(sock, f"User '{recipient}' not found.")
            return current_room
        entry = {"sender": username, "text": message_text, "timestamp": now_iso()}
        private_messages.setdefault(recipient, []).append(entry)
        recipient_socket = clients.get(recipient)

    if recipient_socket:
        send_msg(recipient_socket, "pm",
                 {"sender": username, "text": message_text, "timestamp": entry["timestamp"]})
    send_system(sock, f"[PM → {recipient}] {message_text}")
    logger.info("[PM] %s -> %s: %s", username, recipient, message_text)
    return current_room


def cmd_help(username, parts, sock, current_room) -> str:
    help_text = (
        "━━━━━━━━━━━━ StudiBudi Commands ━━━━━━━━━━━━\n"
        "  /status <topic>        - Set your study status (broadcasts to all)\n"
        "  /online                - See all online users & their study status\n"
        "  /rooms                 - List all study rooms\n"
        "  /create <room> <topic> - Create a new study room\n"
        "  /join <room>           - Join a study room\n"
        "  /leave                 - Leave current room (back to lobby)\n"
        "  /users                 - List users in current room + all online\n"
        "  /msg <user> <message>  - Send a private message\n"
        "  /help                  - Show this help\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    send_system(sock, help_text)
    return current_room


COMMAND_MAP = {
    "/create": cmd_create,
    "/join":   cmd_join,
    "/leave":  cmd_leave,
    "/rooms":  cmd_rooms,
    "/users":  cmd_users,
    "/online": cmd_online,
    "/status": cmd_status,
    "/msg":    cmd_msg,
    "/help":   cmd_help,
}


def handle_command(username: str, cmd: str, sock: socket.socket, current_room: str) -> str | None:
    parts = cmd.split()
    command = parts[0].lower()
    handler = COMMAND_MAP.get(command)
    if handler:
        return handler(username, parts, sock, current_room)
    else:
        send_system(sock, f"Unknown command '{command}'. Type /help for commands.")
        return current_room


# ─────────────────────────── Client Loop ─────────────────────────────

def handle_client(client_socket: socket.socket, addr):
    username = None
    current_room = "lobby"

    try:
        reader = client_socket.makefile('r', encoding='utf-8')

        username = authenticate_user(client_socket, reader)
        if not username:
            client_socket.close()
            return

        with state_lock:
            clients[username] = client_socket
            if username not in rooms["lobby"]:
                rooms["lobby"].append(username)
            user_study_status.setdefault(username, "(no status)")

        deliver_pending_pms(username, client_socket)

        send_system(client_socket,
                    "Welcome to StudiBudi! Type /help for commands or /status <topic> to broadcast what you're studying.")

        broadcast_to_room("lobby", f"{username} is online!", exclude=username)
        logger.info("%s connected from %s", username, addr)

        # ── Main receive loop ──
        while True:
            line = reader.readline()
            if not line:
                break

            try:
                pkt = json.loads(line)
                if pkt.get("type") != "input":
                    continue
                msg = pkt.get("payload", {}).get("text", "").strip()
            except Exception:
                msg = line.strip()

            if not msg:
                continue

            if msg.startswith("/"):
                result = handle_command(username, msg, client_socket, current_room)
                if result is None:
                    break
                current_room = result
            else:
                # Regular chat message
                ts = now_iso()
                entry = {"sender": username, "text": msg, "timestamp": ts}
                with state_lock:
                    room_messages[current_room].append(entry)
                history_logger.info("[%s] [%s] %s: %s", current_room, ts, username, msg)
                broadcast_to_room(current_room, msg, sender=username)
                logger.info("[%s] %s: %s", current_room, username, msg)

    except Exception as e:
        logger.error("Error handling client %s: %s", addr, e)

    finally:
        if username:
            with state_lock:
                clients.pop(username, None)
                user_study_status.pop(username, None)
                for room in rooms:
                    if username in rooms[room]:
                        rooms[room].remove(username)

            broadcast_to_room(current_room, f"{username} disconnected.")
            logger.info("%s disconnected", username)

        client_socket.close()


# ─────────────────────────────── Main ────────────────────────────────

def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 12345))
    server.listen(10)

    logger.info("StudiBudi Chat Server started on port 12345")
    logger.info("Waiting for connections...")

    try:
        while True:
            client_socket, addr = server.accept()
            logger.info("New connection from %s", addr)
            t = threading.Thread(target=handle_client, args=(client_socket, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    finally:
        server.close()


if __name__ == "__main__":
    main()
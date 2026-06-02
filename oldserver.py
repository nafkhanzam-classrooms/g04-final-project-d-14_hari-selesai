import socket
import threading
import json
import datetime
import logging
import hashlib
import sys

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("server.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("StudyBuddyServer")

history_logger = logging.getLogger("ChatHistory")
history_logger.propagate = False
history_logger.setLevel(logging.INFO)
chat_history_handler = logging.FileHandler("chat_history.log", encoding="utf-8")
chat_history_handler.setFormatter(logging.Formatter("%(message)s"))
history_logger.addHandler(chat_history_handler)

# thread-safe lock for shared state
state_lock = threading.Lock()

clients = {}  # username is key, socket is value
user_passwords = {}  # username is key, hashed password is value
rooms = {"lobby": []}  # room_name is key, list of usernames is value
room_messages = {"lobby": []}  # room_name is key, list of message entries is value
private_messages = {}  # username is key, list of pending private message dicts
room_topics = {"lobby": "hangout umum"}

USER_STORE_FILE = "users.json"

def load_user_passwords() -> dict:
    try:
        with open(USER_STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log_error("Failed to load user store from %s", USER_STORE_FILE)
        return {}


def save_user_passwords() -> None:
    try:
        with open(USER_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(user_passwords, f, ensure_ascii=False, indent=2)
    except OSError:
        log_error("Failed to save user store to %s", USER_STORE_FILE)

def now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def format_history_entry(entry: dict) -> str:
    return f"[{entry['timestamp']}] {entry['sender']}: {entry['text']}"

def log_server(message: str, *args):
    logger.info(message, *args)

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def send_to_client(sock: socket.socket, text: str):
    try:
        sock.sendall((text).encode('utf-8'))
    except OSError:
        pass

def encode_msg_obj(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

def send_msg(sock: socket.socket, msg_type: str, payload: dict | str, sender: str = None, room: str = None):
    obj = {
        "type": msg_type,
        "sender": sender,
        "room": room,
        "payload": payload,
        "timestamp": now_iso(),
    }
    try:
        sock.sendall(encode_msg_obj(obj))
    except OSError:
        pass

def deliver_pending_private_messages(username: str, client_socket: socket.socket):
    with state_lock:
        pending = private_messages.pop(username, [])
    if not pending:
        return
    # send pending messages as JSON 'pm' packets
    send_msg(client_socket, "system", {"text": "Pending private messages:"})
    for entry in pending:
        send_msg(client_socket, "pm", {"sender": entry["sender"], "text": entry["text"], "timestamp": entry["timestamp"]})
    send_msg(client_socket, "system", {"text": "End pending messages"})

def authenticate_user(client_socket: socket.socket, reader=None) -> str | None:
    # reader: file-like object from socket.makefile('r', encoding='utf-8')
    send_msg(client_socket, "prompt", {"text": "Enter your username:"})
    if reader is None:
        username = client_socket.recv(1024).decode().strip()
    else:
        line = reader.readline()
        if not line:
            return None
        try:
            obj = json.loads(line)
            username = obj.get("payload", {}).get("text", "").strip()
        except Exception:
            username = line.strip()
    if not username:
        return None

    send_msg(client_socket, "prompt", {"text": "Enter your password:"})
    if reader is None:
        password = client_socket.recv(1024).decode().strip()
    else:
        line = reader.readline()
        if not line:
            return None
        try:
            obj = json.loads(line)
            password = obj.get("payload", {}).get("text", "").strip()
        except Exception:
            password = line.strip()
    if not password:
        return None

    with state_lock:
        if username in clients:
            send_to_client(client_socket, "This user is already logged in.\n")
            return None

        if username in user_passwords:
            if user_passwords[username] != hash_password(password):
                send_msg(client_socket, "system", {"text": "Invalid password. Connection closing."})
                return None
            send_msg(client_socket, "system", {"text": "Login successful."})
            log_server("[SERVER] %s logged in", username)
        else:
            user_passwords[username] = hash_password(password)
            save_user_passwords()
            send_msg(client_socket, "system", {"text": "New user created and logged in."})
            log_server("[SERVER] New user registered: %s", username)

    return username

def log_error(message: str, *args):
    logger.error(message, *args)

user_passwords = load_user_passwords()

def handle_client(client_socket, addr):
    username = None
    current_room = "lobby"
    
    try:
        # use a reader for newline-delimited JSON input
        reader = client_socket.makefile('r', encoding='utf-8')
        username = authenticate_user(client_socket, reader)
        if not username:
            return

        # add to clients and lobby (thread-safe)
        with state_lock:
            clients[username] = client_socket
            if username not in rooms["lobby"]:
                rooms["lobby"].append(username)

        deliver_pending_private_messages(username, client_socket)
        log_server("[SERVER] %s joined from %s", username, addr)
        broadcast_to_room("lobby", f"System: {username} joined the lobby", "System")
        
        # main message loop (read newline-delimited JSON packets)
        while True:
            line = reader.readline()
            if not line:
                break
            try:
                pkt = json.loads(line)
                if pkt.get('type') == 'input':
                    msg = pkt.get('payload', {}).get('text', '').strip()
                else:
                    # unknown packet types are ignored
                    continue
            except Exception:
                # fall back to raw line
                msg = line.strip()

            if not msg:
                break

            if msg.startswith('/'):
                current_room = handle_command(username, msg, client_socket, current_room)
                if current_room is None:
                    break
            else:
                # regular message - send to current room
                payload = {"room": current_room, "sender": username, "text": msg, "timestamp": now_iso()}
                broadcast_to_room(current_room, payload, username)
                entry = {"sender": username, "text": msg, "timestamp": now_iso()}
                with state_lock:
                    room_messages[current_room].append(entry)
                history_logger.info("%s", format_history_entry(entry))
                log_server("[%s] %s: %s", current_room, username, msg)
    
    except Exception as e:
        log_error("[ERROR] %s", e)
    
    finally:
        # cleanup (thread-safe)
        if username:
            with state_lock:
                if username in clients:
                    del clients[username]
                
                # remove from all rooms
                for room in rooms:
                    if username in rooms[room]:
                        rooms[room].remove(username)
            
            broadcast_to_room(current_room, f"System: {username} left", "System")
            log_server("[SERVER] %s disconnected", username)
        
        client_socket.close()

def handle_command(username, cmd, client_socket, current_room):
    parts = cmd.split(" ", 2)
    command = parts[0].lower()
    
    if command == "/create":
        if len(parts) < 3:
            client_socket.send(b"Usage: /create <room_name> <topic>\n")
            return current_room
        
        room_name = parts[1].lower()
        topic = parts[2]
        
        with state_lock:
            if room_name not in rooms:
                rooms[room_name] = []
                room_messages[room_name] = []
                room_topics[room_name] = topic
                send_msg(client_socket, "system", {"text": f"Created room: {room_name} (topic: {topic})"})
                log_server("[SERVER] Room created: %s (topic: %s)", room_name, topic)
            else:
                send_msg(client_socket, "system", {"text": "Room already exists!"})
        return current_room
    
    elif command == "/join":
        if len(parts) < 2:
            client_socket.send(b"Usage: /join <room_name>\n")
            return current_room
        
        room_name = parts[1].lower()
        
        with state_lock:
            if room_name not in rooms:
                client_socket.send(b"Room doesn't exist!\n")
                return current_room
            
            # remove from old room and add to new
            if username in rooms.get(current_room, []):
                rooms[current_room].remove(username)

            if username not in rooms.get(room_name, []):
                rooms[room_name].append(username)
            current_room = room_name
            
            # get history snapshot
            history = room_messages[room_name][-5:] if room_messages[room_name] else []
        
        # send history (outside lock) as JSON system/history packets
        send_msg(client_socket, "system", {"text": f"Joined {room_name}"})
        if history:
            for entry in history:
                send_msg(client_socket, "history", {"sender": entry["sender"], "text": entry["text"], "timestamp": entry["timestamp"]})
        else:
            send_msg(client_socket, "system", {"text": "(no messages yet)"})
        send_msg(client_socket, "system", {"text": "---"})
        
        broadcast_to_room(room_name, {"room": room_name, "sender": "System", "text": f"{username} joined", "timestamp": now_iso()}, "System")
        log_server("[SERVER] %s joined room: %s", username, room_name)
        return current_room
    
    elif command == "/leave":
        if current_room == "lobby":
            client_socket.send(b"You are already in the lobby.\n")
            return current_room
        
        old_room = current_room
        with state_lock:
            if username in rooms.get(old_room, []):
                rooms[old_room].remove(username)
            if username not in rooms["lobby"]:
                rooms["lobby"].append(username)
            current_room = "lobby"
            history = room_messages["lobby"][-5:] if room_messages["lobby"] else []
        
        broadcast_to_room(old_room, {"room": old_room, "sender": "System", "text": f"{username} left", "timestamp": now_iso()}, "System")
        send_msg(client_socket, "system", {"text": "Joined lobby"})
        if history:
            for entry in history:
                send_msg(client_socket, "history", {"sender": entry["sender"], "text": entry["text"], "timestamp": entry["timestamp"]})
        else:
            send_msg(client_socket, "system", {"text": "(no messages yet)"})
        send_msg(client_socket, "system", {"text": "---"})
        broadcast_to_room("lobby", {"room": "lobby", "sender": "System", "text": f"{username} joined the lobby", "timestamp": now_iso()}, "System")
        log_server("[SERVER] %s left room %s and returned to lobby", username, old_room)
        return current_room
    
    elif command == "/rooms":
        with state_lock:
            rooms_list = [{"name": rn, "members": len(rooms[rn]), "topic": room_topics.get(rn)} for rn in rooms]
        send_msg(client_socket, "rooms", {"rooms": rooms_list})
        return current_room
    
    elif command == "/msg":
        if len(parts) < 3:
            client_socket.send(b"Usage: /msg <user> <message>\n")
            return current_room
        recipient = parts[1]
        message_text = parts[2]

        with state_lock:
            if recipient not in user_passwords:
                client_socket.send(b"User not found.\n")
                return current_room
            entry = {"sender": username, "text": message_text, "timestamp": now_iso()}
            private_messages.setdefault(recipient, []).append(entry)
            recipient_socket = clients.get(recipient)

        if recipient_socket:
            send_msg(recipient_socket, "pm", {"sender": username, "text": message_text, "timestamp": entry["timestamp"]})
        send_msg(client_socket, "system", {"text": f"[PM to {recipient}] {message_text}"})
        log_server("[PM] %s -> %s: %s", username, recipient, message_text)
        return current_room
    
    elif command == "/users":
        with state_lock:
            msg = f"Users in {current_room}:\n"
            for user in rooms.get(current_room, []):
                msg += f"  - {user}\n"
        client_socket.send(msg.encode())
        return current_room
    
    elif command == "/help":
        help_text = """
Commands:
  /create <room> <topic>  - Create a study room
  /join <room>            - Join a room
  /leave                  - Leave current room and return to lobby
  /msg <user> <message>   - Send a private message to another user
  /rooms                  - List all rooms
  /users                  - List room members
  /help                   - Show this help
"""
        client_socket.send(help_text.encode())
        return current_room
    
    else:
        client_socket.send(b"Unknown command. Type /help\n")
        return current_room


def broadcast_to_room(room_name, message, sender):
    with state_lock:
        if room_name not in rooms:
            return
        # copy the list to avoid holding lock during socket operations
        usernames = rooms[room_name].copy()

    for username in usernames:
        with state_lock:
            sock = clients.get(username)
        if not sock:
            continue
        try:
            # message can be either pre-built payload dict or string
            if isinstance(message, dict):
                send_msg(sock, "chat", message, sender=sender, room=room_name)
            else:
                send_msg(sock, "chat", {"text": str(message)}, sender=sender, room=room_name)
        except:
            pass

def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 12345))
    server.listen(10)
    
    log_server("[SERVER] StudiBudi Chat Server started on port 12345")
    log_server("[SERVER] Waiting for connections...")
    
    try:
        while True:
            client_socket, addr = server.accept()
            log_server("[SERVER] New connection from %s", addr)
            
            # handle each client in a separate thread
            thread = threading.Thread(target=handle_client, args=(client_socket, addr))
            thread.daemon = True
            thread.start()
    
    except KeyboardInterrupt:
        log_server("\n[SERVER] Shutting down...")
    finally:
        server.close()


if __name__ == "__main__":
    main()
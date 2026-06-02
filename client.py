import socket
import threading
import sys
import json


def send_input(sock: socket.socket, text: str):
    pkt = {"type": "input", "payload": {"text": text}}
    try:
        sock.sendall((json.dumps(pkt, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass


def receive_messages(reader):
    while True:
        try:
            line = reader.readline()
            if not line:
                break
            try:
                pkt = json.loads(line)
            except Exception:
                print(line.strip())
                continue

            t = pkt.get("type")
            payload = pkt.get("payload", {})
            if t == "system" or t == "prompt":
                text = payload.get("text", "")
                print(text)
            elif t == "chat":
                room = pkt.get("room") or payload.get("room")
                sender = pkt.get("sender") or payload.get("sender")
                ts = pkt.get("timestamp") or payload.get("timestamp")
                text = payload.get("text")
                print(f"[{ts}] {sender}@{room}: {text}")
            elif t == "history":
                sender = payload.get("sender")
                ts = payload.get("timestamp")
                text = payload.get("text")
                print(f"[history {ts}] {sender}: {text}")
            elif t == "pm":
                sender = payload.get("sender")
                text = payload.get("text")
                print(f"[PM from {sender}] {text}")
            elif t == "rooms":
                rooms = payload.get("rooms", [])
                print("Available rooms:")
                for r in rooms:
                    print(f"  - {r.get('name')} ({r.get('members')} members)")
            else:
                # unknown types
                print(payload)
        except Exception:
            break

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", 12345))
    except Exception as e:
        print(f"Failed to connect: {e}")
        return
    
    print("Connected to StudiBudi Chat Server!")
    
    # get username/password prompts from server (JSON) and respond
    reader = sock.makefile('r', encoding='utf-8')
    # read username prompt
    line = reader.readline()
    try:
        pkt = json.loads(line)
        prompt = pkt.get('payload', {}).get('text', '')
    except Exception:
        prompt = line
    print(prompt, end=' ')
    username = input()
    send_input(sock, username)
    # read password prompt
    line = reader.readline()
    try:
        pkt = json.loads(line)
        prompt = pkt.get('payload', {}).get('text', '')
    except Exception:
        prompt = line
    print(prompt, end=' ')
    password = input()
    send_input(sock, password)
    
    # start receive thread
    recv_thread = threading.Thread(target=receive_messages, args=(reader,))
    recv_thread.daemon = True
    recv_thread.start()
    
    # main loop
    print("Type your messages or /help for commands:\n")
    try:
        while True:
            msg = input("> ")
            if msg:
                send_input(sock, msg)
    except KeyboardInterrupt:
        print("\nDisconnected.")
    finally:
        sock.close()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS

ROOT = os.path.dirname(__file__)
DATA_DIR = os.path.join(ROOT, "data")
GAMES_PATH = os.path.join(DATA_DIR, "games.json")
STATE_PATH = os.path.join(ROOT, "science_state.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) discord/1.0.9253 Chrome/148.0.7778.280 Electron/42.7.1 Safari/537.36"

def req(url, headers=None, data=None, method="GET"):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    r = urllib.request.Request(url, headers=h, method=method)
    if data:
        r.data = json.dumps(data).encode("utf-8")
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=15) as res:
            b = res.read().decode("utf-8")
            return res.status, json.loads(b) if b else {}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return 0, {}

def get_super_props(sig, hb):
    _, d = req("https://cordapi.dolfi.es/api/v2/properties/windows", method="POST")
    props = d.get("properties") or {
        "os": "Windows", "browser": "Discord Client", "release_channel": "stable",
        "client_version": "1.0.9253", "os_version": "10.0.26200", "os_arch": "x64",
        "app_arch": "x64", "system_locale": "en-GB", "has_client_mods": False,
        "browser_user_agent": UA, "browser_version": "42.7.1", "os_sdk_version": "26200",
        "client_build_number": 594031, "native_build_number": 88414,
        "client_event_source": None, "client_app_state": "focused"
    }
    props.update({"client_launch_id": str(uuid.uuid4()), "launch_signature": sig, "client_heartbeat_session_id": hb})
    return base64.b64encode(json.dumps(props, separators=(",", ":")).encode("utf-8")).decode("ascii")

def load_games():
    if not os.path.exists(GAMES_PATH):
        os.makedirs(DATA_DIR, exist_ok=True)
        status, data = req("https://cdn.discordapp.com/detectables/games.json")
        if status != 200 or not data:
            return []
        with open(GAMES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    else:
        with open(GAMES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

    games, seen = [], set()
    for g in data:
        gid = str(g.get("id", ""))
        if not gid.isdigit() or gid in seen:
            continue
        exes = [e["name"] for e in g.get("executables", []) if e.get("os") == "win32" and e.get("name")]
        if exes:
            seen.add(gid)
            games.append({"id": gid, "name": g.get("name", "Unknown"), "exe": exes[0]})
    return games

def make_events(game, ms, hb_id, sig, seq, fp=""):
    now = int(time.time() * 1000)
    start = max(0, now - ms)
    sid = str(uuid.uuid4())
    
    seq += 1
    hb1 = {
        "type": "running_game_heartbeat",
        "properties": {
            "client_track_timestamp": start, "client_heartbeat_session_id": hb_id,
            "event_sequence_number": seq, "game_id": game["id"], "game_name": game["name"],
            "game_executable": game["exe"], "game_detection_enabled": True, "initial_heartbeat": True,
            "final_heartbeat": False, "game_session_id": sid, "duration_tracked_ms": 0,
            "launch_signature": sig, "client_app_state": "focused", "client_send_timestamp": start
        }
    }
    
    seq += 1
    launch_props = {
        "client_track_timestamp": now, "client_heartbeat_session_id": hb_id,
        "event_sequence_number": seq, "game": game["name"], "game_id": game["id"],
        "verified": True, "elevated": False, "is_launcher": False, "game_platform": "desktop",
        "detection_method": "verified_game", "is_overlay_enabled": False, "is_overlay_game_enabled": True,
        "is_overlay_game_source": "OOP_DEFAULT_DATABASE", "fullscreen_type": "UNKNOWN",
        "hardware_display_count": 1, "overlay_method": "Disabled", "activity_status_enabled": True,
        "activity_status_shared_guilds": [], "current_user_status": "online", "game_detection_enabled": True,
        "executable_path": game["exe"], "rendered_locale": "en-GB", "launch_signature": sig,
        "client_app_state": "focused", "client_send_timestamp": now
    }
    if fp:
        launch_props["executable_fingerprint"] = fp

    seq += 1
    hb2 = {
        "type": "running_game_heartbeat",
        "properties": {
            "client_track_timestamp": now, "client_heartbeat_session_id": hb_id,
            "event_sequence_number": seq, "game_id": game["id"], "game_name": game["name"],
            "game_executable": game["exe"], "game_detection_enabled": True, "initial_heartbeat": False,
            "final_heartbeat": True, "game_session_id": sid, "duration_tracked_ms": ms,
            "launch_signature": sig, "client_app_state": "focused", "client_send_timestamp": now
        }
    }
    
    return [hb1, {"type": "launch_game", "properties": launch_props}, hb2], seq

def process_worker(token: str, ms: int, limit: int | None = None):
    try:
        st, me = req("https://discord.com/api/v9/users/@me?with_analytics_token=true", headers={"Authorization": token})
        if st != 200 or "analytics_token" not in me:
            print(f"[!] Auth failed for token: {st}")
            return
        
        analytics_token = me["analytics_token"]
        games = load_games()
        if not games:
            print("[!] No games loaded.")
            return

        target_games = games[:limit] if limit and isinstance(limit, int) else games

        hb_id, sig = str(uuid.uuid4()), str(uuid.uuid4())
        super_props = get_super_props(sig, hb_id)
        seq = 0
        batch_size = 50
        done = 0

        headers = {
            "Authorization": token,
            "Accept": "*/*",
            "Origin": "https://discord.com",
            "Referer": "https://discord.com/channels/@me",
            "X-Super-Properties": super_props,
            "X-Discord-Locale": "en-GB",
            "X-Discord-Timezone": "Europe/Oslo"
        }

        print(f"[*] Task started for {len(target_games)} games...")
        
        for i in range(0, len(target_games), batch_size):
            chunk = target_games[i:i + batch_size]
            events = []
            for g in chunk:
                evs, seq = make_events(g, ms, hb_id, sig, seq)
                events.extend(evs)

            st, _ = req("https://discord.com/api/v9/science", headers=headers, data={"token": analytics_token, "events": events}, method="POST")
            if st == 204:
                done += len(chunk)
            elif st in (401, 403):
                print("[!] Authorization error during process.")
                break
            time.sleep(0.3)

        print(f"[+] Task completed. ({done}/{len(target_games)} games processed).")
    except Exception as e:
        print(f"[!] Background worker error: {e}")

# --- Flask Server Setup with CORS Enabled ---
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

@app.route('/run', methods=['POST'])
def handle_run():
    data = request.get_json() or {}
    token = data.get('token')
    hours = float(data.get('hours', 1))
    limit = data.get('games') or data.get('limit')

    if not token:
        return jsonify({"status": "error", "message": "Token is required"}), 400

    ms = int(hours * 3600 * 1000)
    
    # تشغيل العملية في خلفية السيرفر
    thread = threading.Thread(target=process_worker, args=(token, ms, limit))
    thread.daemon = True
    thread.start()

    return jsonify({"status": "success", "message": "Task launched in background!"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

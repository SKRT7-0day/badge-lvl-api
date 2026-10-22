#!/usr/bin/env python3

from __future__ import annotations

import base64
import json
import sys
import time
import uuid
import urllib.error
import urllib.request
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator, Sequence
from flask import Flask, request, jsonify
from flask_cors import CORS

ROOT = Path(__file__).resolve().parent
GAMES_FILE = ROOT / "data" / "games.json"
STATE_FILE = ROOT / "science_state.json"

ME_URL = "https://discord.com/api/v9/users/@me?with_analytics_token=true"
SCIENCE_URL = "https://discord.com/api/v9/science"
PROPERTIES_URL = "https://cordapi.dolfi.es/api/v2/properties/windows"
GAMES_CDN_URL = "https://cdn.discordapp.com/detectables/games.json"

BATCH_SIZE = 50
BATCH_DELAY = 0.3
AUTH_MAX_AGE = 12 * 3600
DEBUG = False

CLIENT_VERSION = "1.0.9253"
CLIENT_BUILD_NUMBER = 594031
NATIVE_BUILD_NUMBER = 88414
OS_VERSION = "10.0.26200"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) discord/1.0.9253 Chrome/148.0.7778.280 "
    "Electron/42.7.1 Safari/537.36"
)

class Console:
    def ok(self, text: str) -> None: print(f"[OK] {text}")
    def sent(self, text: str) -> None: print(f"[SYNC] {text}")
    def warn(self, text: str) -> None: print(f"[WARN] {text}")
    def err(self, text: str) -> None: print(f"[ERR] {text}")
    def info(self, text: str) -> None: print(f"[INFO] {text}")

@dataclass
class State:
    token: str = ""
    fingerprint: str = ""
    analytics_token: str = ""
    fetched_at: int = 0

    @property
    def is_stale(self) -> bool:
        return not self.analytics_token or (time.time() - self.fetched_at) > AUTH_MAX_AGE

def _local_props() -> dict:
    return {
        "os": "Windows",
        "browser": "Discord Client",
        "release_channel": "stable",
        "client_version": CLIENT_VERSION,
        "os_version": OS_VERSION,
        "os_arch": "x64",
        "app_arch": "x64",
        "system_locale": "en-GB",
        "has_client_mods": False,
        "browser_user_agent": USER_AGENT,
        "browser_version": "42.7.1",
        "os_sdk_version": "26200",
        "client_build_number": CLIENT_BUILD_NUMBER,
        "native_build_number": NATIVE_BUILD_NUMBER,
        "client_event_source": None,
        "client_app_state": "focused",
    }

def build_super_props(launch_signature: str, heartbeat_session: str) -> str:
    try:
        req = urllib.request.Request(PROPERTIES_URL, method="POST", data=b"{}",
                                     headers={"content-type": "application/json", "user-agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as response:
            props = json.loads(response.read().decode("utf-8"))["properties"]
    except Exception:
        props = _local_props()
    props["client_launch_id"] = str(uuid.uuid4())
    props["launch_signature"] = launch_signature
    props["client_heartbeat_session_id"] = heartbeat_session
    raw = json.dumps(props, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")

def _read_analytics_token(auth_token: str) -> str:
    req = urllib.request.Request(ME_URL, headers={"authorization": auth_token, "user-agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))
    token = data.get("analytics_token")
    if not token:
        raise RuntimeError("no analytics_token")
    return token

def ensure_token(state: State) -> None:
    if not state.is_stale:
        return
    state.analytics_token = _read_analytics_token(state.token)
    state.fetched_at = int(time.time())

@dataclass(frozen=True)
class Session:
    heartbeat_session: str
    launch_signature: str
    super_props: str

    @classmethod
    def new(cls) -> "Session":
        hb = str(uuid.uuid4())
        sig = str(uuid.uuid4())
        return cls(hb, sig, build_super_props(sig, hb))

@dataclass(frozen=True)
class Game:
    id: str
    name: str
    exe: str

def _download_games() -> str:
    request = urllib.request.Request(GAMES_CDN_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8")
    GAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    GAMES_FILE.write_text(raw, encoding="utf-8")
    return raw

def load_games() -> list[Game]:
    if GAMES_FILE.exists():
        raw = GAMES_FILE.read_text(encoding="utf-8")
    else:
        try:
            raw = _download_games()
        except Exception:
            return []
    data = json.loads(raw)
    games: list[Game] = []
    seen: set[str] = set()
    for entry in data:
        gid = str(entry.get("id", ""))
        if not gid.isdigit() or gid in seen: continue
        exe = "game.exe"
        for ex in entry.get("executables", []):
            if ex.get("os") == "win32" and ex.get("name"):
                exe = ex["name"]
                break
        seen.add(gid)
        games.append(Game(gid, entry.get("name", "Unknown"), exe))
    return games

class ScienceClient:
    def __init__(self, state: State, session: Session) -> None:
        self.state = state
        self.session = session
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def build_launch(self, game: Game) -> dict:
        now = int(time.time() * 1000)
        return {
            "type": "launch_game",
            "properties": {
                "client_track_timestamp": now,
                "client_heartbeat_session_id": self.session.heartbeat_session,
                "event_sequence_number": self._next_seq(),
                "game": game.name,
                "game_id": game.id,
                "verified": True,
                "game_platform": "desktop",
                "detection_method": "verified_game",
                "executable_path": game.exe,
                "launch_signature": self.session.launch_signature,
                "client_send_timestamp": now,
            }
        }

    def build_heartbeat(self, game: Game, duration_ms: int, session_id: str, initial: bool, final: bool, ts: int | None = None) -> dict:
        ts = int(time.time() * 1000) if ts is None else ts
        return {
            "type": "running_game_heartbeat",
            "properties": {
                "client_track_timestamp": ts,
                "client_heartbeat_session_id": self.session.heartbeat_session,
                "event_sequence_number": self._next_seq(),
                "game_id": game.id,
                "game_name": game.name,
                "game_executable": game.exe,
                "initial_heartbeat": initial,
                "final_heartbeat": final,
                "game_session_id": session_id,
                "duration_tracked_ms": duration_ms,
                "launch_signature": self.session.launch_signature,
                "client_send_timestamp": ts,
            }
        }

    def build_session(self, game: Game, duration_ms: int) -> list[dict]:
        sid = str(uuid.uuid4())
        now = int(time.time() * 1000)
        start = now - duration_ms
        return [
            self.build_heartbeat(game, 0, sid, initial=True, final=False, ts=start),
            self.build_launch(game),
            self.build_heartbeat(game, duration_ms, sid, initial=False, final=True, ts=now),
        ]

    def post(self, events: Sequence[dict]) -> int:
        payload = {"token": self.state.analytics_token, "events": list(events)}
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "accept": "*/*",
            "authorization": self.state.token,
            "content-type": "application/json",
            "user-agent": USER_AGENT,
            "x-super-properties": self.session.super_props,
        }
        request = urllib.request.Request(SCIENCE_URL, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception:
            return 0

class Spoofer:
    def __init__(self, client: ScienceClient, console: Console) -> None:
        self.client = client
        self.console = console

    def run(self, games: Sequence[Game], build_events) -> int:
        sent_ok = 0
        for start in range(0, len(games), BATCH_SIZE):
            chunk = games[start:start + BATCH_SIZE]
            events = []
            for game in chunk:
                events.extend(build_events(game))
            status = self.client.post(events)
            if status == 204:
                sent_ok += len(chunk)
            time.sleep(BATCH_DELAY)
        return sent_ok

# --- تطبيق السيرفر (Flask API) ---
app = Flask(__name__)
CORS(app)

def run_bg_task(token: str, hours: float, limit: int | None):
    try:
        state = State(token=token)
        ensure_token(state)
        session = Session.new()
        client = ScienceClient(state, session)
        games = load_games()
        if limit:
            games = games[:limit]
        duration_ms = int(hours * 3600 * 1000)
        spoofer = Spoofer(client, Console())
        spoofer.run(games, lambda g: client.build_session(g, duration_ms))
    except Exception as e:
        print(f"Error in background: {e}")

@app.route('/run', methods=['POST'])
def handle_run():
    data = request.get_json() or {}
    token = data.get('token')
    hours = float(data.get('hours', 1))
    limit = data.get('limit')

    if not token:
        return jsonify({"status": "error", "message": "Token is required"}), 400

    thread = threading.Thread(target=run_bg_task, args=(token, hours, limit))
    thread.start()

    return jsonify({"status": "success", "message": "Started in background!"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

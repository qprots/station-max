#!/usr/bin/env python3
"""Host-side Spotify controller for Yandex Station Max (OpenStation).

Why host-side
-------------
Android kills background processes that were started over adb as soon as the
adb session is closed, so on-device shell watchers cannot be relied upon.  The
station-max project already keeps long-lived helpers on the host (the OpenStation
watcher and the static server), and this controller follows the same model: it
watches the OpenStation assistant state over HTTP and drives the Spotify TV app
over adb.

What it does
------------
* pauses Spotify as soon as the assistant starts listening, so the microphone
  does not hear the music and TTS does not fight Spotify for ALSA;
* resumes playback only if *this controller* paused it and the user did not ask
  to stop, and only while Spotify remains the active source;
* executes deterministic playback commands: stop, pause, resume, next,
  previous, ``spotify:`` URIs, open.spotify.com links and prefilled search;
* exposes a small localhost HTTP API for manual control and diagnostics.

It never touches playback when Spotify is not the active source.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Sequence, Set

SPOTIFY_PACKAGE = "com.spotify.tv.android"
SPOTIFY_ACTIVITY = "com.spotify.tv.android/.SpotifyTVActivity"
SPOTIFY_SESSION = "spotify-android-tv-media-session"
YANDEX_MUSIC_PACKAGE = "com.openstation.launcher"
IGNORED_AUDIO_PACKAGES = {SPOTIFY_PACKAGE, "com.android.bluetooth"}

STATE_NONE = 0
STATE_STOPPED = 1
STATE_PAUSED = 2
STATE_PLAYING = 3
STATE_FAST_FORWARDING = 4
STATE_REWINDING = 5
STATE_BUFFERING = 6
STATE_ERROR = 7

ACTIVE_STATES = {STATE_PLAYING, STATE_BUFFERING, STATE_FAST_FORWARDING, STATE_REWINDING}
PAUSED_STATES = {STATE_NONE, STATE_STOPPED, STATE_PAUSED, STATE_ERROR}
BUSY_ASSISTANT_STATES = {"connecting", "listening", "thinking", "speaking"}

KEY_PLAY = "126"  # KEYCODE_MEDIA_PLAY
KEY_PAUSE = "127"  # KEYCODE_MEDIA_PAUSE
KEY_NEXT = "87"  # KEYCODE_MEDIA_NEXT
KEY_PREVIOUS = "88"  # KEYCODE_MEDIA_PREVIOUS
KEY_WAKEUP = "224"  # KEYCODE_WAKEUP


# ---------------------------------------------------------------------------
# media_session parsing
# ---------------------------------------------------------------------------


@dataclass
class MediaSession:
    package: str
    state: Optional[int] = None
    active: bool = False
    description: str = ""
    name: str = ""


def parse_media_sessions(dump: str) -> List[MediaSession]:
    """Parse ``dumpsys media_session`` into a list of sessions.

    Android 9 prints one block per session: a ``<name> <package>/<name>`` header,
    then ``package=``, ``active=``, ``state=PlaybackState {...}`` and ``metadata:``.
    We only need those four fields, so the parser is intentionally tolerant.
    """

    sessions: List[MediaSession] = []
    current: Optional[MediaSession] = None
    pending_name = ""

    for raw in dump.splitlines():
        line = raw.strip()

        if line.startswith("Sessions Stack"):
            current = None
            continue

        # Session header looks like:
        #   spotify-android-tv-media-session com.spotify.tv.android/spotify-... (userId=0)
        header = re.match(r"^(\S+) (\S+)/(\S+) \(userId=\d+\)$", line)
        if header:
            pending_name = header.group(1)
            continue

        if line.startswith("package="):
            current = MediaSession(package=line.split("=", 1)[1].strip(), name=pending_name)
            sessions.append(current)
            continue

        if current is None:
            continue

        if line.startswith("active="):
            current.active = line.split("=", 1)[1].strip() == "true"
        elif line.startswith("state=PlaybackState"):
            match = re.search(r"state=(\d+)", line)
            current.state = int(match.group(1)) if match else None
        elif line == "state=null":
            current.state = None
        elif line.startswith("metadata:"):
            match = re.search(r"description=(.*)$", line)
            if match:
                current.description = match.group(1).strip()

    return sessions


def session_for_package(sessions: Sequence[MediaSession], package: str) -> Optional[MediaSession]:
    """Return the most relevant session for a package (playing beats paused)."""

    candidates = [s for s in sessions if s.package == package]
    if not candidates:
        return None

    def rank(session: MediaSession) -> int:
        return session.state if session.state is not None else -1

    return max(candidates, key=rank)


# ---------------------------------------------------------------------------
# adb / Spotify transport
# ---------------------------------------------------------------------------


class AdbError(RuntimeError):
    pass


class Adb:
    """Thin adb shell wrapper with a single reconnect attempt."""

    def __init__(self, path: str, serial: str, timeout: float = 12.0):
        self.path = os.path.expanduser(path)
        self.serial = serial
        self.timeout = timeout
        self._lock = threading.Lock()

    def _run(self, args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _connect(self) -> None:
        try:
            self._run([self.path, "connect", self.serial], timeout=10.0)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def shell(self, command: str, timeout: Optional[float] = None) -> str:
        timeout = timeout or self.timeout
        with self._lock:
            try:
                proc = self._run([self.path, "-s", self.serial, "shell", command], timeout)
            except FileNotFoundError as exc:
                raise AdbError("adb binary not found: %s" % self.path) from exc
            except subprocess.TimeoutExpired as exc:
                raise AdbError("adb shell timed out: %s" % command) from exc

            if proc.returncode != 0 and not (proc.stdout or "").strip():
                self._connect()
                try:
                    proc = self._run([self.path, "-s", self.serial, "shell", command], timeout)
                except subprocess.TimeoutExpired as exc:
                    raise AdbError("adb shell timed out after reconnect: %s" % command) from exc

            output = (proc.stdout or "").strip()
            if proc.returncode != 0 and not output:
                raise AdbError((proc.stderr or "adb shell failed").strip())
            return output


class SpotifyMedia:
    """Spotify TV playback control through adb."""

    def __init__(self, adb: Adb):
        self.adb = adb
        self.last_state: Optional[int] = None
        self.last_description = ""

    def sessions(self) -> List[MediaSession]:
        return parse_media_sessions(self.adb.shell("dumpsys media_session", timeout=15.0))

    def state(self) -> Optional[int]:
        session = session_for_package(self.sessions(), SPOTIFY_PACKAGE)
        if session is None:
            self.last_state = None
            self.last_description = ""
            return None
        self.last_state = session.state
        self.last_description = session.description
        return session.state

    def spotify_state(self) -> Optional[int]:
        """Controller-facing alias for :meth:`state`."""

        return self.state()

    def other_playing_packages(self) -> Set[str]:
        sessions = self.sessions()
        playing = set()
        for session in sessions:
            if session.package in IGNORED_AUDIO_PACKAGES:
                continue
            if session.state in ACTIVE_STATES:
                playing.add(session.package)
        return playing

    def press(self, code: str) -> None:
        self.adb.shell("input keyevent %s" % code, timeout=10.0)

    def pause(self) -> None:
        self.press(KEY_PAUSE)

    def play(self) -> None:
        self.press(KEY_PLAY)

    def next(self) -> None:
        self.press(KEY_NEXT)

    def previous(self) -> None:
        self.press(KEY_PREVIOUS)

    def open_uri(self, uri: str) -> None:
        # Wake the panel first: Spotify TV ignores VIEW intents while idle.
        try:
            self.press(KEY_WAKEUP)
        except AdbError:
            pass
        self.adb.shell(
            "am start -a android.intent.action.VIEW -d %s -p %s"
            % (shlex.quote(uri), shlex.quote(SPOTIFY_PACKAGE)),
            timeout=20.0,
        )

    def search(self, query: str) -> None:
        self.open_uri("spotify:search:%s" % urllib.parse.quote(query, safe=""))

    def open_app(self, play_if_paused: bool = True, settle: float = 1.2) -> None:
        try:
            self.press(KEY_WAKEUP)
        except AdbError:
            pass
        self.adb.shell(
            "am start -n %s" % shlex.quote(SPOTIFY_ACTIVITY),
            timeout=20.0,
        )
        if not play_if_paused:
            return
        time.sleep(settle)
        current = self.state()
        if current in PAUSED_STATES and not self.other_playing_packages():
            self.play()


# ---------------------------------------------------------------------------
# Intent parsing (Russian voice phrases)
# ---------------------------------------------------------------------------


@dataclass
class Intent:
    kind: str
    query: str = ""
    uri: str = ""


INTENT_NONE = "none"
INTENT_STOP = "stop"
INTENT_PAUSE = "pause"
INTENT_RESUME = "resume"
INTENT_NEXT = "next"
INTENT_PREVIOUS = "previous"
INTENT_SEARCH = "search"
INTENT_PLAY_URI = "play_uri"
INTENT_OPEN = "open"

SPOTIFY_MENTION_RE = re.compile(
    r"\b(спотиф\w*|spotify|spotif\w*|спотифи\w*|спотифа\w*)\b", re.IGNORECASE
)
SPOTIFY_URI_RE = re.compile(
    r"(spotify:(?:track|album|artist|playlist|episode|show):[A-Za-z0-9]+"
    r"|https?://open\.spotify\.com/\S+)",
    re.IGNORECASE,
)
FOREIGN_OBJECT_RE = re.compile(
    r"\b(свет\w*|ламп\w*|телевизор\w*|телик\w*|ютуб\w*|youtube|видео\w*|"
    r"громкост\w*|звук\w*|будильник\w*|таймер\w*|кондиционер\w*|обогреватель\w*)\b",
    re.IGNORECASE,
)
STOP_RE = re.compile(r"\b(выключ\w*|отключ\w*|останов\w*|стоп\w*|хватит|замолч\w*)\b", re.IGNORECASE)
PAUSE_RE = re.compile(r"\b(пауза|паузу|приостанов\w*|на паузу)\b", re.IGNORECASE)
RESUME_RE = re.compile(r"\b(продолж\w*|возобнов\w*|играй|играть|врубай)\b", re.IGNORECASE)
RESUME_ALT_RE = re.compile(r"\bвключи\s+(музыку|плеер|трек|треки)\b", re.IGNORECASE)
NEXT_RE = re.compile(r"\b(следующ\w*|дальше|переключи\w*|перелистн\w*)\b", re.IGNORECASE)
PREVIOUS_RE = re.compile(r"\b(предыдущ\w*|назад|верни)\b", re.IGNORECASE)
PLAY_TRIGGER_RE = re.compile(
    r"\b(включи|поставь|запусти|найди|вруби|сыграй|хочу|поищи)\b", re.IGNORECASE
)

QUERY_FILLER_RE = re.compile(
    r"^(?:мне|нам|пожалуйста|плиз|пожалуй|"
    r"песню|песня|трек|трека|музыку|музыка|композицию|композиция|"
    r"исполнителя|исполнитель|группу|группа|альбом|"
    r"на|в|по|из|с|the)\s+",
    re.IGNORECASE,
)

MUSIC_OBJECT_RE = re.compile(
    r"\b(музык\w*|трек\w*|песн\w*|плеер\w*|спотиф\w*|spotify)\b", re.IGNORECASE
)


def normalize_text(text: str) -> str:
    text = (text or "").strip().lower().replace("ё", "е")
    text = re.sub(r"[«»\"'`]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,!?;:—-")


def extract_query(text: str) -> str:
    """Return the requested track/artist after the Spotify mention."""

    match = SPOTIFY_MENTION_RE.search(text)
    tail = text[match.end():] if match else text
    tail = PLAY_TRIGGER_RE.sub(" ", tail)
    tail = SPOTIFY_MENTION_RE.sub(" ", tail)
    tail = re.sub(r"\b(включи|поставь|запусти|найди|вруби|сыграй|поищи)\b", " ", tail)
    tail = re.sub(r"\s+", " ", tail).strip(" .,!?;:—-")
    while True:
        cleaned = QUERY_FILLER_RE.sub("", tail).strip(" .,!?;:—-")
        if cleaned == tail:
            break
        tail = cleaned
    return tail


def parse_intent(text: str, spotify_recent: bool = False) -> Intent:
    """Map a recognized phrase to a Spotify action."""

    normalized = normalize_text(text)
    if not normalized:
        return Intent(INTENT_NONE)

    uri_match = SPOTIFY_URI_RE.search(text or "")
    if uri_match:
        return Intent(INTENT_PLAY_URI, uri=uri_match.group(1))

    mentions_spotify = bool(SPOTIFY_MENTION_RE.search(normalized))
    if FOREIGN_OBJECT_RE.search(normalized) and not mentions_spotify:
        return Intent(INTENT_NONE)

    if STOP_RE.search(normalized):
        # "выключи" is enough: when music is playing users rarely finish the phrase.
        if mentions_spotify or MUSIC_OBJECT_RE.search(normalized):
            return Intent(INTENT_STOP)
        if re.fullmatch(r"(алиса\s+)?(выключи|отключи|останови|стоп|хватит)(\s+все)?", normalized):
            return Intent(INTENT_STOP)
        return Intent(INTENT_NONE)

    if PAUSE_RE.search(normalized):
        return Intent(INTENT_PAUSE)

    if NEXT_RE.search(normalized):
        return Intent(INTENT_NEXT)

    if PREVIOUS_RE.search(normalized):
        return Intent(INTENT_PREVIOUS)

    if mentions_spotify:
        query = extract_query(normalized)
        if query and query not in {"спотифай", "spotify", "музыку", "музыка"}:
            return Intent(INTENT_SEARCH, query=query)
        return Intent(INTENT_OPEN)

    if RESUME_RE.search(normalized) or RESUME_ALT_RE.search(normalized):
        return Intent(INTENT_RESUME)

    return Intent(INTENT_NONE)


# ---------------------------------------------------------------------------
# Controller state machine
# ---------------------------------------------------------------------------


@dataclass
class AssistantSnapshot:
    state: str = "idle"
    last_user_text: str = ""
    last_user_ts: str = ""

    @classmethod
    def from_status(cls, status: Optional[Dict]) -> Optional["AssistantSnapshot"]:
        if not isinstance(status, dict):
            return None
        assistant = status.get("assistant") or {}

        # lastUserText is cleared when the session ends, while history keeps the
        # recognized phrase. The controller needs the phrase *after* the answer,
        # so history is the primary source.
        last_text = str(assistant.get("lastUserText") or "")
        last_ts = ""
        for entry in reversed(assistant.get("history") or []):
            if entry.get("role") == "user":
                last_text = str(entry.get("text") or last_text)
                last_ts = str(entry.get("ts") or "")
                break

        return cls(
            state=str(assistant.get("state") or "idle"),
            last_user_text=last_text,
            last_user_ts=last_ts,
        )


class Controller:
    """Pause/resume state machine plus intent execution.

    The controller is clock-injected and transport-injected so it can be tested
    without a device.
    """

    def __init__(
        self,
        media,
        settle_seconds: float = 0.8,
        resume_window_seconds: float = 300.0,
        log=None,
    ):
        self.media = media
        self.settle_seconds = settle_seconds
        self.resume_window_seconds = resume_window_seconds
        self._log = log or (lambda message: None)
        self._lock = threading.Lock()

        self.busy = False
        self.idle_since: Optional[float] = None
        self.pending: Optional[Intent] = None
        self.paused_by_us = False
        self.last_playing_at: Optional[float] = None
        self.text_key = ("", "")
        self.idle_text_key = ("", "")
        self.idle_primed = False
        self.last_state: Optional[int] = None
        self.last_action = ""

    # -- helpers ---------------------------------------------------------

    def _recent(self, now: float) -> bool:
        if self.paused_by_us:
            return True
        if self.last_playing_at is None:
            return False
        return (now - self.last_playing_at) <= self.resume_window_seconds

    def _sample_state(self) -> Optional[int]:
        state = self.media.spotify_state()
        self.last_state = state
        return state

    def _refresh_state(self, now: float) -> Optional[int]:
        state = self._sample_state()
        if state in ACTIVE_STATES:
            self.last_playing_at = now
        return state

    def _pause_if_playing(self, now: float, attempts: int = 3) -> bool:
        state = self._refresh_state(now)
        if state not in ACTIVE_STATES:
            return False
        for attempt in range(attempts):
            self.media.press(KEY_PAUSE)
            if self._wait_for_paused(1.0):
                return True
            self._log("pause not confirmed (attempt %d), retrying" % (attempt + 1))
        return False

    def _wait_for_paused(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._sample_state() in PAUSED_STATES:
                return True
            time.sleep(0.15)
        return False

    def _resume(self, now: float, force: bool = False, attempts: int = 2) -> bool:
        state = self._refresh_state(now)
        if state not in PAUSED_STATES:
            return False
        if not force and self.media.other_playing_packages():
            return False
        for attempt in range(attempts):
            self.media.press(KEY_PLAY)
            if self._wait_for_playing(1.0):
                self.last_playing_at = now
                return True
            self._log("play not confirmed (attempt %d), retrying" % (attempt + 1))
        return False

    def _wait_for_playing(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._sample_state() in ACTIVE_STATES:
                return True
            time.sleep(0.15)
        return False

    def _duck(self, now: float) -> None:
        state = self._refresh_state(now)
        if state in ACTIVE_STATES:
            paused = self._pause_if_playing(now)
            self.paused_by_us = paused
            self.last_action = "duck" if paused else "duck-failed"
            self._log(
                "assistant started: Spotify %s"
                % ("paused" if paused else "pause was not confirmed")
            )
        else:
            self.paused_by_us = False

    def _on_text(self, text: str, now: float) -> None:
        intent = parse_intent(text, spotify_recent=self._recent(now))
        if intent.kind == INTENT_NONE:
            return
        self.pending = intent
        self._log("intent %s %s" % (intent.kind, intent.query or intent.uri))

        # Cut the music immediately for explicit stop/pause, before the answer.
        if intent.kind in (INTENT_STOP, INTENT_PAUSE):
            if self._pause_if_playing(now):
                self.paused_by_us = False
            self.last_action = intent.kind

    def _execute_intent(self, intent: Intent, now: float) -> None:
        state = self._refresh_state(now)
        self.last_action = intent.kind

        if intent.kind in (INTENT_STOP, INTENT_PAUSE):
            self._pause_if_playing(now)
            self.paused_by_us = False
            return

        if intent.kind == INTENT_RESUME:
            if state in PAUSED_STATES:
                self._resume(now, force=True)
            self.paused_by_us = False
            return

        if intent.kind in (INTENT_NEXT, INTENT_PREVIOUS):
            # Spotify TV ignores transport keys while paused, and the assistant
            # usually starts Yandex Music for the same phrase. Play Spotify first
            # so it takes audio focus and becomes the media-button session.
            if state in PAUSED_STATES:
                self._resume(now, force=True)
                state = self._refresh_state(now)
            self.media.press(KEY_NEXT if intent.kind == INTENT_NEXT else KEY_PREVIOUS)
            if state in PAUSED_STATES:
                self._resume(now, force=True)
            self.paused_by_us = False
            return

        if intent.kind == INTENT_PLAY_URI:
            self.media.open_uri(intent.uri)
            self.last_playing_at = now
            self.paused_by_us = False
            return

        if intent.kind == INTENT_SEARCH:
            self.media.search(intent.query)
            self.last_playing_at = now
            self.paused_by_us = False
            return

        if intent.kind == INTENT_OPEN:
            self.media.open_app()
            self.last_playing_at = now
            self.paused_by_us = False
            return

    # -- main tick -------------------------------------------------------

    def tick(self, assistant: Optional[AssistantSnapshot], now: float) -> None:
        if assistant is None:
            return

        with self._lock:
            busy_now = assistant.state in BUSY_ASSISTANT_STATES

            if busy_now and not self.busy:
                self.busy = True
                self.idle_since = None
                self.pending = None
                self.paused_by_us = False
                # The first busy snapshot may already contain the new phrase.
                # Compare against the phrase seen while idle, not against this one.
                if not self.idle_primed:
                    self.idle_text_key = (assistant.last_user_ts, assistant.last_user_text)
                    self.idle_primed = True
                self.text_key = self.idle_text_key
                self._duck(now)
            elif not busy_now and self.busy:
                self.busy = False
                self.idle_since = now

            if busy_now:
                key = (assistant.last_user_ts, assistant.last_user_text)
                if assistant.last_user_text and key != self.text_key:
                    self.text_key = key
                    self._on_text(assistant.last_user_text, now)
            else:
                self.idle_text_key = (assistant.last_user_ts, assistant.last_user_text)
                self.idle_primed = True

            if self.busy or self.idle_since is None:
                return

            if (now - self.idle_since) < self.settle_seconds:
                return

            pending = self.pending
            self.pending = None
            self.idle_since = None

            if pending is not None:
                self._execute_intent(pending, now)
                return

            if self.paused_by_us:
                if not self._resume(now):
                    self._log("resume skipped: another player owns audio")
                else:
                    self.last_action = "resume"
                    self._log("assistant idle: Spotify resumed")
                self.paused_by_us = False

    # -- manual API ------------------------------------------------------

    def command(self, action: str, uri: str = "", query: str = "", now: Optional[float] = None) -> Dict:
        now = time.monotonic() if now is None else now
        action = (action or "").strip().lower()

        with self._lock:
            if action in ("pause", "stop"):
                paused = self._pause_if_playing(now)
                self.paused_by_us = False
                self.pending = None
                self.last_action = action
                return {"ok": True, "action": action, "paused": paused}

            if action in ("play", "resume"):
                played = self._resume(now)
                self.paused_by_us = False
                self.pending = None
                self.last_action = action
                return {"ok": True, "action": action, "played": played}

            if action == "next":
                self.media.press(KEY_NEXT)
                if self.paused_by_us:
                    self._resume(now)
                self.paused_by_us = False
                self.last_action = action
                return {"ok": True, "action": action}

            if action == "previous":
                self.media.press(KEY_PREVIOUS)
                if self.paused_by_us:
                    self._resume(now)
                self.paused_by_us = False
                self.last_action = action
                return {"ok": True, "action": action}

            if action == "search":
                if not query:
                    return {"ok": False, "error": "query is required"}
                self.media.search(query)
                self.paused_by_us = False
                self.last_action = action
                return {"ok": True, "action": action, "query": query}

            if action == "play_uri":
                if not uri:
                    return {"ok": False, "error": "uri is required"}
                self.media.open_uri(uri)
                self.paused_by_us = False
                self.last_action = action
                return {"ok": True, "action": action, "uri": uri}

            return {"ok": False, "error": "unknown action", "action": action}

    def status(self, now: Optional[float] = None) -> Dict:
        now = time.monotonic() if now is None else now
        return {
            "busy": self.busy,
            "paused_by_us": self.paused_by_us,
            "pending": self.pending.kind if self.pending else None,
            "last_action": self.last_action,
            "spotify_state": self.last_state,
            "seconds_since_playing": (
                None if self.last_playing_at is None else round(now - self.last_playing_at, 1)
            ),
        }


# ---------------------------------------------------------------------------
# OpenStation status watcher
# ---------------------------------------------------------------------------


class StationClient:
    def __init__(self, base_url: str, timeout: float = 4.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get_json(self, path: str = "/api/status", timeout: Optional[float] = None) -> Dict:
        with self._opener.open(self.base_url + path, timeout=timeout or self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


class StatusWatcher(threading.Thread):
    """Polls OpenStation and publishes a snapshot for the controller thread."""

    def __init__(self, client: StationClient, interval: float = 0.25):
        super().__init__(name="station-status", daemon=True)
        self.client = client
        self.interval = interval
        self.snapshot: Optional[AssistantSnapshot] = None
        self.error: Optional[str] = None
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.snapshot = AssistantSnapshot.from_status(self.client.get_json())
                self.error = None
            except Exception as exc:  # noqa: BLE001 - keep polling through device restarts
                self.error = "%s: %s" % (type(exc).__name__, exc)
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Local HTTP API
# ---------------------------------------------------------------------------


class ApiHandler(BaseHTTPRequestHandler):
    controller: Controller = None  # type: ignore[assignment]
    watcher: StatusWatcher = None  # type: ignore[assignment]

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401 - silence default logging
        return

    def _send(self, payload: Dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?")[0] != "/status":
            self._send({"ok": False, "error": "not found"}, 404)
            return
        snapshot = self.watcher.snapshot
        self._send(
            {
                "ok": True,
                "assistant": {
                    "state": snapshot.state if snapshot else None,
                    "last_user_text": snapshot.last_user_text if snapshot else None,
                    "error": self.watcher.error,
                },
                "spotify": self.controller.status(),
            }
        )

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/command":
            self._send({"ok": False, "error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send({"ok": False, "error": "invalid json"}, 400)
            return
        try:
            result = self.controller.command(
                payload.get("action", ""),
                uri=payload.get("uri", ""),
                query=payload.get("query", ""),
            )
        except AdbError as exc:
            self._send({"ok": False, "error": str(exc)}, 502)
            return
        self._send(result, 200 if result.get("ok") else 400)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Station Max Spotify controller")
    parser.add_argument("--station", default=os.environ.get("STATION_URL", "http://192.168.10.219:8080"))
    parser.add_argument("--adb", default=os.environ.get("STATION_ADB", "adb"))
    parser.add_argument("--serial", default=os.environ.get("STATION_SERIAL", "192.168.10.219:5555"))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("STATION_POLL_INTERVAL", "0.25")))
    parser.add_argument("--settle", type=float, default=float(os.environ.get("STATION_SETTLE_SECONDS", "0.8")))
    parser.add_argument(
        "--resume-window",
        type=float,
        default=float(os.environ.get("STATION_RESUME_WINDOW", "300")),
    )
    parser.add_argument("--api-host", default=os.environ.get("STATION_API_HOST", "127.0.0.1"))
    parser.add_argument("--api-port", type=int, default=int(os.environ.get("STATION_API_PORT", "8099")))
    parser.add_argument("--no-api", action="store_true", help="disable the localhost control API")
    parser.add_argument("--log-file", default=os.environ.get("STATION_LOG_FILE"))
    parser.add_argument("--once", action="store_true", help="print device state and exit")
    return parser


def make_logger(path: Optional[str]):
    lock = threading.Lock()

    def log(message: str) -> None:
        line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message)
        with lock:
            print(line, flush=True)
            if path:
                try:
                    with open(os.path.expanduser(path), "a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                except OSError:
                    pass

    return log


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    log = make_logger(args.log_file)

    adb = Adb(args.adb, args.serial)
    media = SpotifyMedia(adb)

    if args.once:
        sessions = media.sessions()
        print(
            json.dumps(
                [
                    {
                        "name": s.name,
                        "package": s.package,
                        "state": s.state,
                        "active": s.active,
                        "description": s.description,
                    }
                    for s in sessions
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    controller = Controller(
        media,
        settle_seconds=args.settle,
        resume_window_seconds=args.resume_window,
        log=log,
    )
    watcher = StatusWatcher(StationClient(args.station), interval=args.interval)

    if not args.no_api:
        ApiHandler.controller = controller
        ApiHandler.watcher = watcher
        server = ThreadingHTTPServer((args.api_host, args.api_port), ApiHandler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="api", daemon=True).start()
        log("api listening on %s:%d" % (args.api_host, args.api_port))

    watcher.start()
    log(
        "watching %s, adb %s (%s), settle=%.2fs"
        % (args.station, args.adb, args.serial, args.settle)
    )

    try:
        while True:
            try:
                controller.tick(watcher.snapshot, time.monotonic())
            except AdbError as exc:
                log("adb error: %s" % exc)
                time.sleep(2.0)
            except Exception as exc:  # noqa: BLE001 - daemon must not die on one bad tick
                log("tick error: %s: %s" % (type(exc).__name__, exc))
                time.sleep(1.0)
            time.sleep(0.1)
    except KeyboardInterrupt:
        log("stopped")
    finally:
        watcher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Unit tests for the station-max Spotify controller.

Run with: python3 -m unittest discover -s spotify/tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spotify_control as sc  # noqa: E402


DUMPSYS_SAMPLE = """
  Media Sessions
  Sessions Stack - have 3 sessions:
    spotify-android-tv-media-session com.spotify.tv.android/spotify-android-tv-media-session (userId=0)
      ownerPid=7911, ownerUid=10015, userId=0
      package=com.spotify.tv.android
      active=true
      state=PlaybackState {state=2, position=205875, buffered position=0, speed=1.0, updated=22929837, actions=2368383, custom actions=[], active item id=-1, error=null}
      metadata:size=8, description=Лиза, Andrey Gubin, Мальчик бродяга

    A2dpMediaBrowserService com.android.bluetooth/A2dpMediaBrowserService (userId=0)
      package=com.android.bluetooth
      active=true
      state=null

    OpenStationMusic com.openstation.launcher/OpenStationMusic (userId=0)
      package=com.openstation.launcher
      active=false
      state=PlaybackState {state=1, position=0, buffered position=0, speed=0.0, updated=22454266, actions=895, custom actions=[], active item id=-1, error=null}
      metadata:size=4, description=Про Андрея, П.С., Про Андрея
"""


class FakeMedia:
    def __init__(self, state=sc.STATE_PAUSED, others=()):
        self.current = state
        self.keys = []
        self.uris = []
        self.others = set(others)

    def spotify_state(self):
        return self.current

    def press(self, code):
        self.keys.append(code)
        if code == sc.KEY_PLAY:
            self.current = sc.STATE_PLAYING
        elif code == sc.KEY_PAUSE and self.current in sc.ACTIVE_STATES:
            self.current = sc.STATE_PAUSED
        return True

    def other_playing_packages(self):
        return set(self.others)

    def open_uri(self, uri):
        self.uris.append(("uri", uri))
        self.current = sc.STATE_PLAYING

    def search(self, query):
        self.uris.append(("search", query))
        self.current = sc.STATE_PLAYING

    def open_app(self, play_if_paused=True, settle=1.2):
        self.uris.append(("open", ""))


def snap(state, text="", ts=""):
    return sc.AssistantSnapshot(state=state, last_user_text=text, last_user_ts=ts)


class ParseMediaTests(unittest.TestCase):
    def test_parses_all_sessions(self):
        sessions = sc.parse_media_sessions(DUMPSYS_SAMPLE)
        self.assertEqual([s.package for s in sessions], [
            "com.spotify.tv.android",
            "com.android.bluetooth",
            "com.openstation.launcher",
        ])
        spotify = sessions[0]
        self.assertEqual(spotify.state, sc.STATE_PAUSED)
        self.assertTrue(spotify.active)
        self.assertIn("Andrey Gubin", spotify.description)
        self.assertIsNone(sessions[1].state)

    def test_selects_most_active_session_per_package(self):
        sessions = sc.parse_media_sessions(DUMPSYS_SAMPLE)
        picked = sc.session_for_package(sessions, "com.spotify.tv.android")
        self.assertEqual(picked.state, sc.STATE_PAUSED)
        self.assertIsNone(sc.session_for_package(sessions, "org.example.missing"))


class IntentTests(unittest.TestCase):
    def check(self, text, kind, query="", uri=""):
        intent = sc.parse_intent(text, spotify_recent=True)
        self.assertEqual(intent.kind, kind, "text=%r intent=%r" % (text, intent))
        if query:
            self.assertEqual(intent.query, query)
        if uri:
            self.assertEqual(intent.uri, uri)

    def test_stop_phrases(self):
        self.check("выключи музыку", sc.INTENT_STOP)
        self.check("Выключи.", sc.INTENT_STOP)
        self.check("останови музыку, надоела", sc.INTENT_STOP)
        self.check("стоп", sc.INTENT_STOP)
        self.check("хватит музыку", sc.INTENT_STOP)

    def test_pause_resume(self):
        self.check("пауза", sc.INTENT_PAUSE)
        self.check("поставь на паузу", sc.INTENT_PAUSE)
        self.check("продолжи", sc.INTENT_RESUME)
        self.check("включи музыку", sc.INTENT_RESUME)

    def test_transport(self):
        self.check("следующий трек", sc.INTENT_NEXT)
        self.check("дальше", sc.INTENT_NEXT)
        self.check("предыдущий трек", sc.INTENT_PREVIOUS)
        self.check("верни назад", sc.INTENT_PREVIOUS)

    def test_spotify_search(self):
        self.check("включи в спотифай песню Лиза", sc.INTENT_SEARCH, query="лиза")
        self.check("поставь в Spotify Daft Punk", sc.INTENT_SEARCH, query="daft punk")
        self.check("включи спотифай", sc.INTENT_OPEN)

    def test_explicit_uri(self):
        uri = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
        self.check("включи " + uri, sc.INTENT_PLAY_URI, uri=uri)
        link = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        self.check("поставь " + link, sc.INTENT_PLAY_URI, uri=link)

    def test_foreign_commands_are_ignored(self):
        self.check("выключи свет", sc.INTENT_NONE)
        self.check("включи телевизор", sc.INTENT_NONE)
        self.check("громкость на десять", sc.INTENT_NONE)
        self.check("который час", sc.INTENT_NONE)

    def test_other_music_without_spotify_is_ignored(self):
        self.assertEqual(sc.parse_intent("включи песню лиза", spotify_recent=False).kind, sc.INTENT_NONE)


class ControllerTests(unittest.TestCase):
    def make(self, media, **kwargs):
        return sc.Controller(media, settle_seconds=0.8, **kwargs)

    def test_duck_and_resume(self):
        media = FakeMedia(sc.STATE_PLAYING)
        c = self.make(media)

        c.tick(snap("listening"), 0.0)
        self.assertEqual(media.current, sc.STATE_PAUSED)
        self.assertEqual(media.keys, [sc.KEY_PAUSE])
        self.assertTrue(c.paused_by_us)

        c.tick(snap("speaking"), 1.0)
        c.tick(snap("idle"), 2.0)
        c.tick(snap("idle"), 2.5)
        self.assertEqual(media.current, sc.STATE_PAUSED)

        c.tick(snap("idle"), 2.9)
        self.assertEqual(media.current, sc.STATE_PLAYING)
        self.assertEqual(media.keys[-1], sc.KEY_PLAY)
        self.assertFalse(c.paused_by_us)

    def test_stop_intent_is_not_resumed(self):
        media = FakeMedia(sc.STATE_PLAYING)
        c = self.make(media)

        c.tick(snap("listening"), 0.0)
        c.tick(snap("thinking", "выключи музыку", "15:04:37"), 0.5)
        c.tick(snap("speaking", "выключи музыку", "15:04:37"), 1.0)
        c.tick(snap("idle", "выключи музыку", "15:04:37"), 2.0)
        c.tick(snap("idle", "выключи музыку", "15:04:37"), 3.0)

        self.assertEqual(media.current, sc.STATE_PAUSED)
        self.assertFalse(c.paused_by_us)
        self.assertNotIn(sc.KEY_PLAY, media.keys)

    def test_does_not_resume_manually_paused_music(self):
        media = FakeMedia(sc.STATE_PAUSED)
        c = self.make(media)

        c.tick(snap("listening"), 0.0)
        self.assertEqual(media.keys, [])
        c.tick(snap("idle"), 1.0)
        c.tick(snap("idle"), 2.0)

        self.assertEqual(media.current, sc.STATE_PAUSED)
        self.assertNotIn(sc.KEY_PLAY, media.keys)

    def test_does_not_resume_over_yandex_music(self):
        media = FakeMedia(sc.STATE_PLAYING)
        c = self.make(media)

        c.tick(snap("listening"), 0.0)
        media.others = {"com.openstation.launcher"}
        c.tick(snap("idle"), 1.0)
        c.tick(snap("idle"), 2.0)

        self.assertEqual(media.current, sc.STATE_PAUSED)
        self.assertNotIn(sc.KEY_PLAY, media.keys)

    def test_next_track_resumes_after_duck(self):
        media = FakeMedia(sc.STATE_PLAYING)
        c = self.make(media)

        c.tick(snap("listening"), 0.0)
        c.tick(snap("thinking", "следующий трек", "15:10:00"), 0.6)
        c.tick(snap("idle", "следующий трек", "15:10:00"), 1.5)
        c.tick(snap("idle", "следующий трек", "15:10:00"), 2.4)

        self.assertIn(sc.KEY_NEXT, media.keys)
        self.assertEqual(media.current, sc.STATE_PLAYING)

    def test_spotify_search_runs_after_answer(self):
        media = FakeMedia(sc.STATE_PAUSED)
        c = self.make(media)

        c.tick(snap("listening"), 0.0)
        c.tick(snap("thinking", "включи в спотифай Daft Punk", "15:11:00"), 0.5)
        c.tick(snap("idle", "включи в спотифай Daft Punk", "15:11:00"), 1.4)
        c.tick(snap("idle", "включи в спотифай Daft Punk", "15:11:00"), 2.3)

        self.assertEqual(media.uris, [("search", "daft punk")])

    def test_old_text_is_not_replayed(self):
        media = FakeMedia(sc.STATE_PLAYING)
        c = self.make(media)

        # The watcher already has an old phrase; a new wake must not reuse it.
        c.tick(snap("listening", "выключи музыку", "15:00:00"), 0.0)
        c.tick(snap("idle", "выключи музыку", "15:00:00"), 1.0)
        c.tick(snap("idle", "выключи музыку", "15:00:00"), 2.0)

        self.assertEqual(media.current, sc.STATE_PLAYING)

    def test_manual_api(self):
        media = FakeMedia(sc.STATE_PLAYING)
        c = self.make(media)
        self.assertTrue(c.command("pause")["paused"])
        self.assertEqual(media.current, sc.STATE_PAUSED)
        self.assertTrue(c.command("play")["played"])
        self.assertEqual(media.current, sc.STATE_PLAYING)
        self.assertFalse(c.command("bogus")["ok"])


if __name__ == "__main__":
    unittest.main()


class SnapshotTests(unittest.TestCase):
    def test_uses_history_when_last_user_text_is_cleared(self):
        status = {
            "assistant": {
                "state": "idle",
                "lastUserText": "",
                "history": [
                    {"role": "user", "text": "выключи музыку", "ts": "15:35:16"},
                    {"role": "model", "text": "Сделала.", "ts": "15:35:18"},
                ],
            }
        }
        snapshot = sc.AssistantSnapshot.from_status(status)
        self.assertEqual(snapshot.last_user_text, "выключи музыку")
        self.assertEqual(snapshot.last_user_ts, "15:35:16")

    def test_falls_back_to_last_user_text(self):
        status = {"assistant": {"state": "thinking", "lastUserText": "пауза", "history": []}}
        self.assertEqual(sc.AssistantSnapshot.from_status(status).last_user_text, "пауза")


class LateTextTests(unittest.TestCase):
    def test_first_busy_snapshot_already_has_new_command(self):
        media = FakeMedia(sc.STATE_PLAYING)
        c = sc.Controller(media, settle_seconds=0.8)

        c.tick(snap("idle", "старая команда", "15:00:00"), 0.0)
        # The watcher sees "connecting" only after history already has the phrase.
        c.tick(snap("connecting", "выключи музыку", "15:00:10"), 0.2)
        c.tick(snap("idle", "выключи музыку", "15:00:10"), 2.0)
        c.tick(snap("idle", "выключи музыку", "15:00:10"), 3.0)

        self.assertEqual(media.current, sc.STATE_PAUSED)
        self.assertNotIn(sc.KEY_PLAY, media.keys)

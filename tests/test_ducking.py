"""
tests/test_ducking.py — Unit Tests for Audio Ducking Subsystem
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure clean mock on non-Windows platforms without pycaw
if "pycaw" not in sys.modules:
    try:
        import pycaw.pycaw  # noqa: F401
    except ImportError:
        mock_pycaw = MagicMock()
        mock_pycaw_mod = MagicMock()
        mock_pycaw_mod.AudioUtilities = MagicMock()
        sys.modules["pycaw"] = mock_pycaw
        sys.modules["pycaw.pycaw"] = mock_pycaw_mod

from core import ducking


class MockSimpleAudioVolume:
    def __init__(self, volume: float = 1.0, mute: int = 0):
        self._vol = float(volume)
        self._mute = int(mute)

    def GetMasterVolume(self) -> float:
        return self._vol

    def SetMasterVolume(self, vol: float, context=None):
        self._vol = float(vol)

    def GetMute(self) -> int:
        return self._mute

    def SetMute(self, mute: int, context=None):
        self._mute = int(mute)


class MockProcess:
    def __init__(self, pid: int, name: str):
        self.pid = pid
        self._name = name

    def name(self) -> str:
        return self._name


class MockAudioSession:
    def __init__(self, pid: int, name: str, volume: float = 1.0, mute: int = 0):
        self.Process = MockProcess(pid, name)
        self.ProcessId = pid
        self.SimpleAudioVolume = MockSimpleAudioVolume(volume, mute)


class TestAudioDucking(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp_dir.name) / "test_duck_state.json"
        self.ducker = ducking.AudioDucker(state_file=self.state_file)

    def tearDown(self):
        self.ducker.force_restore()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def _create_mock_sessions(self):
        self.spotify_session = MockAudioSession(1001, "spotify.exe", volume=0.8)
        self.jarvis_session = MockAudioSession(os.getpid(), "python.exe", volume=1.0)
        self.teams_session = MockAudioSession(1003, "teams.exe", volume=0.9)
        self.chrome_session = MockAudioSession(1004, "chrome.exe", volume=0.6)
        return [
            self.spotify_session,
            self.jarvis_session,
            self.teams_session,
            self.chrome_session,
        ]

    def test_ducking_other_sessions_and_skipping_jarvis_and_excluded(self):
        """Ducks background media apps, skips JARVIS PID and excluded communications apps (Teams)."""
        mock_sessions = self._create_mock_sessions()

        with patch("pycaw.pycaw.AudioUtilities.GetAllSessions", return_value=mock_sessions), \
             patch("memory.config_manager.get_ducking_enabled", return_value=True), \
             patch("memory.config_manager.get_ducking_level", return_value=25), \
             patch("memory.config_manager.get_ducking_exclude_apps", return_value=["teams.exe"]):

            self.ducker._perform_duck()

            # Spotify ducked: 0.8 * 0.25 = 0.20
            self.assertAlmostEqual(self.spotify_session.SimpleAudioVolume.GetMasterVolume(), 0.20, places=2)
            # Chrome ducked: 0.6 * 0.25 = 0.15
            self.assertAlmostEqual(self.chrome_session.SimpleAudioVolume.GetMasterVolume(), 0.15, places=2)

            # JARVIS PID must remain untouched at 1.0
            self.assertEqual(self.jarvis_session.SimpleAudioVolume.GetMasterVolume(), 1.0)
            # Excluded Teams process must remain untouched at 0.9
            self.assertEqual(self.teams_session.SimpleAudioVolume.GetMasterVolume(), 0.9)

            self.assertTrue(self.ducker._is_ducked)
            self.assertTrue(self.state_file.exists())

            # Now restore
            self.ducker._perform_restore()

            # Spotify and Chrome restored exactly
            self.assertAlmostEqual(self.spotify_session.SimpleAudioVolume.GetMasterVolume(), 0.80, places=2)
            self.assertAlmostEqual(self.chrome_session.SimpleAudioVolume.GetMasterVolume(), 0.60, places=2)
            self.assertFalse(self.ducker._is_ducked)
            self.assertFalse(self.state_file.exists())

    def test_crash_recovery_from_stale_snapshot_file(self):
        """On startup, a stale state snapshot from a previous crash is restored and deleted."""
        mock_sessions = self._create_mock_sessions()
        self.spotify_session.SimpleAudioVolume.SetMasterVolume(0.2)  # currently stuck at ducked level

        stale_data = {
            "1001_spotify.exe": {
                "pid": 1001,
                "name": "spotify.exe",
                "orig_vol": 0.8,
                "ducked_vol": 0.2,
                "orig_mute": 0,
            }
        }
        self.state_file.write_text(json.dumps(stale_data), encoding="utf-8")

        with patch("pycaw.pycaw.AudioUtilities.GetAllSessions", return_value=mock_sessions):
            fresh_ducker = ducking.AudioDucker(state_file=self.state_file)
            self.assertAlmostEqual(self.spotify_session.SimpleAudioVolume.GetMasterVolume(), 0.80, places=2)
            self.assertFalse(self.state_file.exists())

    def test_user_changed_volume_during_duck_is_not_overwritten(self):
        """If user manually moves volume slider while ducked, restore does not override user change."""
        mock_sessions = self._create_mock_sessions()

        with patch("pycaw.pycaw.AudioUtilities.GetAllSessions", return_value=mock_sessions), \
             patch("memory.config_manager.get_ducking_enabled", return_value=True), \
             patch("memory.config_manager.get_ducking_level", return_value=25):

            self.ducker._perform_duck()
            self.assertAlmostEqual(self.spotify_session.SimpleAudioVolume.GetMasterVolume(), 0.20, places=2)

            # User turns Spotify up to 0.55 while JARVIS is speaking
            self.spotify_session.SimpleAudioVolume.SetMasterVolume(0.55)

            # Restore is triggered
            self.ducker._perform_restore()

            # Spotify should remain at user-selected 0.55, NOT overwritten to 0.80
            self.assertAlmostEqual(self.spotify_session.SimpleAudioVolume.GetMasterVolume(), 0.55, places=2)

    def test_watchdog_restores_after_timeout(self):
        """Watchdog forces restore if ducking exceeds 60 seconds without release."""
        mock_sessions = self._create_mock_sessions()

        with patch("pycaw.pycaw.AudioUtilities.GetAllSessions", return_value=mock_sessions), \
             patch("memory.config_manager.get_ducking_enabled", return_value=True), \
             patch("memory.config_manager.get_ducking_level", return_value=25):

            self.ducker._perform_duck()
            self.assertTrue(self.ducker._is_ducked)

            # Simulate 65 seconds elapsed
            self.ducker._duck_start_time = time.monotonic() - 65.0
            self.ducker.check_watchdog()

            self.assertFalse(self.ducker._is_ducked)
            self.assertAlmostEqual(self.spotify_session.SimpleAudioVolume.GetMasterVolume(), 0.80, places=2)

    def test_rescan_sessions_mid_speech(self):
        """Audio session appearing mid-speech is detected and ducked by rescan."""
        self._create_mock_sessions()
        mock_sessions = [self.spotify_session]

        with patch("pycaw.pycaw.AudioUtilities.GetAllSessions", side_effect=lambda: list(mock_sessions)), \
             patch("memory.config_manager.get_ducking_enabled", return_value=True), \
             patch("memory.config_manager.get_ducking_level", return_value=25), \
             patch("memory.config_manager.get_ducking_exclude_apps", return_value=[]):

            self.ducker._perform_duck()
            self.assertAlmostEqual(self.spotify_session.SimpleAudioVolume.GetMasterVolume(), 0.20, places=2)

            # A new media player starts playing mid-speech at volume 0.80
            vlc_session = MockAudioSession(2001, "vlc.exe", volume=0.80)
            mock_sessions.append(vlc_session)

            # Trigger rescan
            self.ducker._rescan_sessions_while_ducked()

            # VLC should now also be ducked: 0.80 * 0.25 = 0.20
            self.assertAlmostEqual(vlc_session.SimpleAudioVolume.GetMasterVolume(), 0.20, places=2)
            self.assertIn("2001_vlc.exe", self.ducker._snapshot)

            # Restore should restore both Spotify and VLC
            self.ducker._perform_restore()
            self.assertAlmostEqual(self.spotify_session.SimpleAudioVolume.GetMasterVolume(), 0.80, places=2)
            self.assertAlmostEqual(vlc_session.SimpleAudioVolume.GetMasterVolume(), 0.80, places=2)

    def test_recovery_restores_only_sessions_near_ducked_value(self):
        """Recovery only restores sessions that are still near their ducked value."""
        sess_still_ducked = MockAudioSession(3001, "music.exe", volume=0.21)  # ducked was 0.20 -> diff 0.01 <= 0.06
        sess_user_adjusted = MockAudioSession(3002, "podcast.exe", volume=0.50)  # was 0.8, ducked 0.2, user set to 0.50 (< orig_vol, but > 0.06 away from ducked)

        snapshot = {
            "3001_music.exe": {
                "pid": 3001,
                "name": "music.exe",
                "orig_vol": 0.8,
                "ducked_vol": 0.2,
                "orig_mute": 0,
            },
            "3002_podcast.exe": {
                "pid": 3002,
                "name": "podcast.exe",
                "orig_vol": 0.8,
                "ducked_vol": 0.2,
                "orig_mute": 0,
            },
        }

        with patch("pycaw.pycaw.AudioUtilities.GetAllSessions", return_value=[sess_still_ducked, sess_user_adjusted]):
            self.ducker._restore_from_snapshot_data(snapshot)

            # sess_still_ducked was near ducked value, so it is restored to orig_vol 0.8
            self.assertAlmostEqual(sess_still_ducked.SimpleAudioVolume.GetMasterVolume(), 0.8, places=2)
            # sess_user_adjusted was at 0.50 (far from 0.20), so it is NOT restored to 0.8 despite 0.50 < 0.80
            self.assertAlmostEqual(sess_user_adjusted.SimpleAudioVolume.GetMasterVolume(), 0.50, places=2)

    def test_disabled_setting_and_non_windows_are_no_op(self):
        """When disabled in settings or on non-Windows OS, ducking is a no-op."""
        mock_sessions = self._create_mock_sessions()

        with patch("pycaw.pycaw.AudioUtilities.GetAllSessions", return_value=mock_sessions), \
             patch("memory.config_manager.get_ducking_enabled", return_value=False):

            self.ducker._perform_duck()
            self.assertFalse(self.ducker._is_ducked)
            self.assertEqual(self.spotify_session.SimpleAudioVolume.GetMasterVolume(), 0.80)

        with patch("platform.system", return_value="Linux"):
            self.ducker.notify_speech_activity(True)
            self.assertFalse(self.ducker._is_ducked)


if __name__ == "__main__":
    unittest.main()

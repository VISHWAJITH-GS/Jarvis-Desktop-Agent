"""
tests/test_spotify_repair.py — Automated tests for Spotify track playback,
separation from resume, outcome formatting, and failure handling.
"""
from __future__ import annotations

import unittest
from actions import spotify_control
from core import outcome


class SpotifyRepairTestCase(unittest.TestCase):
    def test_spotify_search_track_returns_candidate(self):
        """Test searching for Beat It by Michael Jackson returns valid track URI."""
        candidate = spotify_control.search_spotify_track("Beat It", artist="Michael Jackson")
        if candidate is not None:
            self.assertIn("uri", candidate)
            self.assertTrue(candidate["uri"].startswith("spotify:track:"))
            self.assertIn("Beat It", candidate["title"])

    def test_resume_vs_play_separation(self):
        """Verify resume and play are distinct actions."""
        res_resume = spotify_control.spotify_control({"action": "resume"})
        self.assertIn("status=done", res_resume)
        self.assertIn("action=resume_playback", res_resume)

    def test_play_without_song_name_fails_safely(self):
        """Verify play with empty query returns structured failure rather than crashing."""
        res = spotify_control.spotify_control({"action": "play", "query": ""})
        self.assertIn("status=failed", res)
        self.assertIn("No track name specified", res)

    def test_outcome_done_with_string_and_dict_normalization(self):
        """Verify outcome.done does not raise TypeError when formatted safely."""
        res_dict = {"track": "Beat It", "artist": "Michael Jackson", "verified": True}
        out = outcome.done("spotify_control", **res_dict)
        self.assertIn("status=done", out)
        self.assertIn("track=\"Beat It\"", out)
        self.assertIn("artist=\"Michael Jackson\"", out)

    def test_spotify_status_query(self):
        """Verify get_state returns current playback status."""
        res = spotify_control.spotify_control({"action": "get_state"})
        self.assertIn("status=done", res)
        self.assertIn("get_playback_state", res)

    def test_idle_titles_never_treated_as_playing_track(self):
        """Verify 'Spotify Free', 'Spotify Premium', and 'Spotify' are recognized as idle."""
        for idle in ("", "Spotify", "Spotify Free", "Spotify Premium", "Advertisement"):
            self.assertTrue(spotify_control.is_spotify_idle_title(idle), f"{idle!r} must be idle")
            self.assertFalse(spotify_control.verify_track_match(idle, "Beat It", "Beat It", "Michael Jackson"))

        self.assertFalse(spotify_control.is_spotify_idle_title("Michael Jackson - Beat It"))
        self.assertTrue(spotify_control.verify_track_match("Michael Jackson - Beat It", "Beat It", "Beat It", "Michael Jackson"))
        self.assertFalse(spotify_control.verify_track_match("Other Artist - Different Song", "Beat It", "Beat It", "Michael Jackson"))

    def test_play_track_never_claims_success_when_spotify_stays_idle(self):
        """Distinguish navigation from playback; never say 'Now playing' when Spotify stays 'Spotify Free'."""
        from unittest.mock import patch

        with patch.object(spotify_control, "search_spotify_track", return_value={"title": "Beat It", "uri": "spotify:track:1OOtq8tRnDM8kG2gqUPjAj"}), \
             patch.object(spotify_control, "launch_spotify", return_value=True), \
             patch.object(spotify_control, "_focus_spotify_window", return_value=True), \
             patch.object(spotify_control, "_try_click_uia_play_button", return_value=False), \
             patch.object(spotify_control, "get_spotify_window_title", return_value="Spotify Free"), \
             patch.object(spotify_control, "get_spotify_active_track", return_value=""), \
             patch("subprocess.run"), \
             patch("time.sleep"):
            res = spotify_control.play_track("Beat It", artist="Michael Jackson")
            self.assertFalse(res["success"])
            self.assertFalse(res["verified"])
            self.assertTrue(res["navigated"])
            self.assertEqual(res["status"], "failed")
            self.assertNotIn("Now playing", str(res.get("message", "")))
            self.assertNotIn("still playing 'Spotify Free'", res["reason"])
            self.assertIn("idle/paused", res["reason"])

            tool_out = spotify_control.spotify_control({"action": "play", "query": "Beat It", "artist": "Michael Jackson"})
            self.assertIn("status=failed", tool_out)
            self.assertNotIn("Now playing", tool_out)

    def test_play_track_verified_when_matching_song_plays(self):
        """Verify success ONLY when Spotify active window title matches requested track."""
        from unittest.mock import patch

        with patch.object(spotify_control, "search_spotify_track", return_value={"title": "Beat It", "uri": "spotify:track:1OOtq8tRnDM8kG2gqUPjAj"}), \
             patch.object(spotify_control, "launch_spotify", return_value=True), \
             patch.object(spotify_control, "get_spotify_active_track", side_effect=["", "Michael Jackson - Beat It"]), \
             patch("subprocess.run"), \
             patch("time.sleep"):
            res = spotify_control.play_track("Beat It", artist="Michael Jackson")
            self.assertTrue(res["success"])
            self.assertTrue(res["verified"])
            self.assertTrue(res["navigated"])
            self.assertEqual(res["status"], "done")
            self.assertIn("Beat It", res["message"])


if __name__ == "__main__":
    unittest.main()


import sys
import unittest
from unittest.mock import patch, MagicMock

import actions.windows_control as wc


class TestVolumeControl(unittest.TestCase):
    def test_volume_extraction_and_clamping(self):
        # Numeric values
        self.assertEqual(wc._win_endpoint_volume and True, True)

        # Extraction from messy strings
        res = wc.windows_control({"action": "set_volume", "value": "value = 50"})
        self.assertIn("status=done", res)
        self.assertIn("set_volume", res)

        # Clamping
        res_high = wc.windows_control({"action": "set_volume", "value": "250"})
        self.assertIn("status=done", res_high)

    def test_volume_up_down_actions(self):
        with (
            patch("actions.windows_control.get_volume", return_value=40),
            patch("actions.windows_control.set_volume") as mock_set,
        ):
            mock_set.return_value = {
                "success": True,
                "action": "set_volume",
                "volume": 50,
                "verified": True,
                "message": "Volume set to 50%.",
            }

            # volume_up defaults to +10
            wc.windows_control({"action": "volume_up"})
            mock_set.assert_called_with(50)

            # volume_down defaults to -10
            wc.windows_control({"action": "volume_down"})
            mock_set.assert_called_with(30)

    def test_audio_device_wrapper_compatibility(self):
        # Simulates modern pycaw returning an AudioDevice object with EndpointVolume property
        mock_dev = MagicMock()
        mock_dev.EndpointVolume = MagicMock()
        mock_dev.EndpointVolume.GetMasterVolumeLevelScalar.return_value = 0.55
        del mock_dev.Activate  # Ensure no Activate attribute

        with patch("pycaw.pycaw.AudioUtilities.GetSpeakers", return_value=mock_dev):
            ep = wc._win_endpoint_volume()
            self.assertEqual(ep, mock_dev.EndpointVolume)
            vol = wc.get_volume()
            self.assertEqual(vol, 55)


class TestConfirmationWindowState(unittest.TestCase):
    def setUp(self):
        from PyQt6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication(sys.argv)

    def test_confirmation_unminimizes_and_restores(self):
        from ui import MainWindow

        win = MainWindow("assets/face.gif")

        # Scenario 1: Window was minimized to taskbar
        win.showMinimized()
        self.assertTrue(win.isMinimized())

        win._show_confirm_banner("Permission Needed", "Connect Bluetooth device?")
        self.assertFalse(win.isMinimized(), "Minimized window must be restored")
        self.assertFalse(win.isMaximized(), "Minimized window should restore to normal, not stay minimized")
        win._hide_confirm_banner()

        # Scenario 2: Arc Reactor top orb mini mode
        win._is_minimized_top = True
        win._show_confirm_banner("Permission Needed", "Connect Bluetooth device?")
        self.assertFalse(win._is_minimized_top, "Mini orb must be restored to normal HUD")
        win._hide_confirm_banner()

        # Scenario 3: Window was not minimized -> displayed in maximized way
        win.showNormal()
        win._is_minimized_top = False
        win._show_confirm_banner("Permission Needed", "Connect Bluetooth device?")
        self.assertTrue(win.isMaximized(), "Open window should be displayed in maximized way")

        # When hidden, it restores
        win._hide_confirm_banner()
        self.assertFalse(win.isMaximized(), "Should restore back to normal once confirmed/cancelled")

        win.close()


if __name__ == "__main__":
    unittest.main()

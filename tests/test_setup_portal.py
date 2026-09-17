import asyncio
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from live_dashboard.setup_portal import SetupCoordinator, WROVER_FQBN


class TestSetupPortal(unittest.TestCase):
    def make_coordinator(self) -> SetupCoordinator:
        environment = {
            "UTSM_SETUP_ENABLED": "1",
            "UTSM_FIRMWARE_REPO": str(Path(__file__).parents[1]),
            "UTSM_SOFTWARE_REPO": str(Path(__file__).parents[1]),
        }
        with patch.dict(os.environ, environment, clear=False):
            return SetupCoordinator()

    def test_uses_verified_wrover_build_options(self):
        self.assertEqual(
            WROVER_FQBN,
            "esp32:esp32:esp32:PartitionScheme=huge_app,PSRAM=enabled",
        )

    def test_invalid_port_is_rejected_before_programming(self):
        coordinator = self.make_coordinator()
        with self.assertRaisesRegex(ValueError, "valid Windows COM port"):
            asyncio.run(coordinator.start("not-a-port"))
        self.assertFalse(coordinator.running)

    def test_disconnected_port_is_rejected(self):
        coordinator = self.make_coordinator()
        coordinator.serial_ports = lambda: []
        with self.assertRaisesRegex(ValueError, "not currently connected"):
            asyncio.run(coordinator.start("COM9"))

    def test_busy_port_is_rejected_before_programming(self):
        coordinator = self.make_coordinator()
        coordinator.serial_ports = lambda: [{"device": "COM5"}]
        coordinator.assert_port_available = lambda port: (_ for _ in ()).throw(
            ValueError(f"{port} is connected but Windows cannot open it.")
        )
        with self.assertRaisesRegex(ValueError, "Windows cannot open"):
            asyncio.run(coordinator.start("COM5"))
        self.assertFalse(coordinator.running)

    def test_page_contains_single_program_action_and_progress_steps(self):
        page = (
            Path(__file__).parents[1] / "live_dashboard" / "static" / "setup.html"
        ).read_text(encoding="utf-8")
        self.assertIn("Program WROVER and start telemetry", page)
        self.assertIn("1. Tunnel + key", page)
        self.assertIn("2. Compile", page)
        self.assertIn("3. Program", page)
        self.assertIn("location.assign('/live')", page)

    def test_relay_serial_lines_report_each_live_path_stage(self):
        coordinator = self.make_coordinator()
        coordinator._record_relay_line("Registered: operator='302220', signal CSQ=30")
        coordinator._record_relay_line("LTE connected; IP: 10.233.107.181")
        coordinator._record_relay_line(
            "LIVE seq=23 POST failed after 67228 ms json=310 B"
        )
        self.assertTrue(coordinator.relay_status["lte_registered"])
        self.assertEqual(coordinator.relay_status["lte_ip"], "10.233.107.181")
        self.assertTrue(coordinator.relay_status["c3_connected"])
        self.assertEqual(coordinator.relay_status["last_car_sequence"], 23)
        self.assertIsNotNone(coordinator.relay_status["c3_last_seen_at_ms"])
        self.assertIn("POST failed", coordinator.relay_status["last_error"])

        coordinator._record_relay_line("Dashboard POST status=715")
        coordinator._record_relay_line(
            "LIVE seq=24 POST failed after 20337 ms json=310 B"
        )
        self.assertEqual(coordinator.relay_status["post_status"], 715)
        self.assertEqual(
            coordinator.relay_status["last_error"],
            "TLS handshake failed (715) · car seq 24 not delivered",
        )

    def test_stale_c3_packet_is_not_reported_as_connected(self):
        coordinator = self.make_coordinator()
        coordinator.enabled = False
        coordinator._record_relay_line("Dropping live seq=837 while LTE is offline")
        coordinator.relay_status["c3_last_seen_at_ms"] = int(time.time() * 1000) - 31_000
        snapshot = coordinator.relay_snapshot()
        self.assertFalse(snapshot["c3_connected"])
        self.assertGreaterEqual(snapshot["c3_age_seconds"], 31)

    def test_modem_initialization_failure_is_exposed(self):
        coordinator = self.make_coordinator()
        coordinator._record_relay_line(
            "A7670X answered AT, but TinyGSM initialization failed"
        )
        self.assertEqual(
            coordinator.relay_status["last_error"],
            "A7670 modem initialization failed",
        )

    def test_waiting_for_registration_clears_stale_lte_state(self):
        coordinator = self.make_coordinator()
        coordinator._record_relay_line("LTE connected; IP: 10.1.2.3")
        coordinator._record_relay_line("Waiting for LTE registration...")
        self.assertFalse(coordinator.relay_status["lte_registered"])
        self.assertIsNone(coordinator.relay_status["lte_ip"])
        coordinator._record_relay_line("LTE registration timed out")
        self.assertEqual(
            coordinator.relay_status["last_error"], "LTE registration timed out"
        )

    def test_live_page_shows_wrover_c3_and_lte_statuses(self):
        page = (
            Path(__file__).parents[1] / "live_dashboard" / "static" / "live.html"
        ).read_text(encoding="utf-8")
        self.assertIn("WROVER / USB", page)
        self.assertIn("Telemetry ESP32-C3", page)
        self.assertIn("LTE → dashboard", page)
        self.assertIn("refreshRelayStatus", page)


if __name__ == "__main__":
    unittest.main()

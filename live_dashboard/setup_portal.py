from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


WROVER_FQBN = "esp32:esp32:esp32:PartitionScheme=huge_app,PSRAM=enabled"
COM_PORT_PATTERN = re.compile(r"^COM\d+$", re.IGNORECASE)


class SetupCoordinator:
    """Runs the local-only WROVER preparation and upload workflow."""

    def __init__(self) -> None:
        self.enabled = os.environ.get("UTSM_SETUP_ENABLED") == "1"
        self.firmware_repo = self._environment_path("UTSM_FIRMWARE_REPO")
        self.software_repo = self._environment_path("UTSM_SOFTWARE_REPO")
        self.phase = "ready"
        self.message = "Connect only the WROVER/A7670 USB cable, then choose its port."
        self.running = False
        self.success = False
        self.error: str | None = None
        self.logs: list[str] = []
        self.selected_port: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._serial_stop = threading.Event()
        self._serial_thread: threading.Thread | None = None
        self._serial_connection: Any = None
        self.relay_status: dict[str, Any] = {
            "port": None,
            "serial_connected": False,
            "mode": None,
            "c3_connected": False,
            "last_car_sequence": None,
            "c3_last_seen_at_ms": None,
            "lte_registered": False,
            "lte_ip": None,
            "post_status": None,
            "last_error": None,
            "last_line": None,
            "updated_at_ms": None,
        }

    @staticmethod
    def _environment_path(name: str) -> Path | None:
        value = os.environ.get(name)
        return Path(value).resolve() if value else None

    def serial_ports(self) -> list[dict[str, str]]:
        try:
            from serial.tools import list_ports
        except ImportError:
            return []

        return [
            {
                "device": port.device,
                "description": port.description or "Serial device",
                "hwid": port.hwid or "",
            }
            for port in sorted(list_ports.comports(), key=lambda item: item.device)
        ]

    @staticmethod
    def assert_port_available(port: str) -> None:
        try:
            import serial

            connection = serial.Serial()
            connection.port = port
            connection.baudrate = 115200
            connection.timeout = 0.2
            connection.dtr = False
            connection.rts = False
            connection.open()
            connection.close()
        except Exception as error:
            raise ValueError(
                f"{port} is connected but Windows cannot open it. Close any "
                "Serial Monitor, unplug and reconnect the WROVER USB cable, "
                f"then try again. ({error})"
            ) from error

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "phase": self.phase,
            "message": self.message,
            "running": self.running,
            "success": self.success,
            "error": self.error,
            "logs": self.logs[-120:],
            "selected_port": self.selected_port,
            "ports": self.serial_ports(),
            "relay": self.relay_snapshot(),
        }

    def relay_snapshot(self) -> dict[str, Any]:
        self._ensure_serial_monitor()
        snapshot = dict(self.relay_status)
        last_seen = snapshot["c3_last_seen_at_ms"]
        age_ms = int(time.time() * 1000) - last_seen if last_seen else None
        snapshot["c3_connected"] = age_ms is not None and age_ms <= 30_000
        snapshot["c3_age_seconds"] = (
            round(max(0, age_ms) / 1000, 1) if age_ms is not None else None
        )
        return snapshot

    def _ensure_serial_monitor(self) -> None:
        if not self.enabled or self.running:
            return
        if self._serial_thread and self._serial_thread.is_alive():
            return
        ports = self.serial_ports()
        wrover_ports = [
            item["device"]
            for item in ports
            if "1A86:55D4" in item.get("hwid", "").upper()
            or "CH9102" in item.get("description", "").upper()
        ]
        if len(wrover_ports) == 1:
            self._start_serial_monitor(wrover_ports[0])

    def _stop_serial_monitor(self) -> None:
        self._serial_stop.set()
        connection = self._serial_connection
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        thread = self._serial_thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._serial_thread = None
        self._serial_connection = None

    def _start_serial_monitor(self, port: str) -> None:
        self._stop_serial_monitor()
        self._serial_stop = threading.Event()
        self.relay_status.update(
            {
                "port": port.upper(),
                "serial_connected": False,
                "mode": None,
                "c3_connected": False,
                "last_car_sequence": None,
                "c3_last_seen_at_ms": None,
                "lte_registered": False,
                "lte_ip": None,
                "post_status": None,
                "last_error": None,
                "last_line": None,
                "updated_at_ms": int(time.time() * 1000),
            }
        )
        self._serial_thread = threading.Thread(
            target=self._serial_monitor_loop,
            args=(port.upper(), self._serial_stop),
            daemon=True,
            name="utsm-wrover-serial-monitor",
        )
        self._serial_thread.start()

    def _serial_monitor_loop(self, port: str, stop: threading.Event) -> None:
        try:
            import serial

            connection = serial.Serial()
            connection.port = port
            connection.baudrate = 115200
            connection.timeout = 0.5
            connection.dtr = False
            connection.rts = False
            connection.open()
            self._serial_connection = connection
            self.relay_status["serial_connected"] = True
            while not stop.is_set():
                raw = connection.readline()
                if not raw:
                    continue
                line = raw.decode(errors="replace").strip()
                if line:
                    self._record_relay_line(line)
        except Exception as error:
            if not stop.is_set():
                self.relay_status["last_error"] = f"Serial monitor: {error}"
                self.relay_status["updated_at_ms"] = int(time.time() * 1000)
        finally:
            self.relay_status["serial_connected"] = False
            connection = self._serial_connection
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._serial_connection = None

    def _record_relay_line(self, line: str) -> None:
        status = self.relay_status
        status["last_line"] = line
        status["updated_at_ms"] = int(time.time() * 1000)
        if line.startswith("UTSM T-A7670X live telemetry relay"):
            status["c3_connected"] = False
            status["last_car_sequence"] = None
            status["c3_last_seen_at_ms"] = None
        if line.startswith("Mode: "):
            status["mode"] = line.removeprefix("Mode: ")
        if line.startswith("Registered:"):
            status["lte_registered"] = True
            status["last_error"] = None
        if line.startswith("Waiting for LTE registration"):
            status["lte_registered"] = False
            status["lte_ip"] = None
        if line.startswith("LTE connected; IP:"):
            status["lte_registered"] = True
            status["lte_ip"] = line.split(":", 1)[1].strip()
        live_match = re.search(r"(?:LIVE|Dropping live) seq=(\d+)", line)
        if live_match:
            status["c3_connected"] = True
            status["last_car_sequence"] = int(live_match.group(1))
            status["c3_last_seen_at_ms"] = status["updated_at_ms"]
        if line.startswith("ESP-NOW superseded car="):
            status["c3_connected"] = True
            status["c3_last_seen_at_ms"] = status["updated_at_ms"]
        post_match = re.search(r"Dashboard POST status=(-?\d+)", line)
        if post_match:
            post_status = int(post_match.group(1))
            status["post_status"] = post_status
            status["last_error"] = (
                None if 200 <= post_status < 300
                else "TLS handshake failed (715)" if post_status == 715
                else f"Dashboard POST failed ({post_status})"
            )
        if "POST failed after" in line:
            if status["post_status"] == 715:
                sequence = status["last_car_sequence"]
                suffix = f" · car seq {sequence} not delivered" if sequence is not None else ""
                status["last_error"] = f"TLS handshake failed (715){suffix}"
            else:
                status["last_error"] = line
        if "not answering AT commands" in line:
            status["last_error"] = "A7670 modem is not answering AT commands"
        if "TinyGSM initialization failed" in line:
            status["last_error"] = "A7670 modem initialization failed"
        if "LTE registration timed out" in line:
            status["last_error"] = "LTE registration timed out"
        if "SIM unlock failed" in line:
            status["last_error"] = "SIM unlock failed; check the SIM PIN"
        if "Packet-data connection failed" in line:
            status["last_error"] = "LTE packet-data connection failed"

    async def start(self, port: str) -> dict[str, Any]:
        if not self.enabled:
            raise ValueError("The setup portal was not enabled by the launcher.")
        if self.running:
            raise RuntimeError("WROVER setup is already running.")
        if not COM_PORT_PATTERN.fullmatch(port):
            raise ValueError("Select a valid Windows COM port.")

        self._stop_serial_monitor()

        available = {item["device"].upper() for item in self.serial_ports()}
        if port.upper() not in available:
            raise ValueError(f"{port.upper()} is not currently connected.")
        self.assert_port_available(port.upper())
        if self.firmware_repo is None or self.software_repo is None:
            raise ValueError("The launcher did not provide the repository paths.")

        self.phase = "starting"
        self.message = "Starting the WROVER setup workflow..."
        self.running = True
        self.success = False
        self.error = None
        self.logs = []
        self.selected_port = port.upper()
        self._task = asyncio.create_task(self._run(self.selected_port))
        return self.snapshot()

    async def _run(self, port: str) -> None:
        try:
            prepare_script = self.firmware_repo / "prepare_live_motor_temp.ps1"
            relay_sketch = self.firmware_repo / "lte_relay"
            if not prepare_script.is_file() or not relay_sketch.is_dir():
                raise RuntimeError("The firmware preparation files are missing.")

            powershell = shutil.which("powershell.exe") or shutil.which("powershell")
            arduino_cli = shutil.which("arduino-cli")
            if arduino_cli is None:
                fallback = Path(r"C:\Program Files\Arduino CLI\arduino-cli.exe")
                if fallback.is_file():
                    arduino_cli = str(fallback)
            if powershell is None:
                raise RuntimeError("Windows PowerShell was not found.")
            if arduino_cli is None:
                raise RuntimeError("Arduino CLI was not found.")

            self.phase = "tunnel"
            self.message = "Preparing credentials and a healthy Cloudflare tunnel..."
            await self._run_command(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(prepare_script),
                    "-NoLaunchIde",
                    "-SoftwareRepoPath",
                    str(self.software_repo),
                ],
                cwd=self.firmware_repo,
            )

            self.phase = "compile"
            self.message = "Compiling the WROVER relay firmware with the tunnel key..."
            await self._run_command(
                [
                    arduino_cli,
                    "compile",
                    "--fqbn",
                    WROVER_FQBN,
                    str(relay_sketch),
                ],
                cwd=self.firmware_repo,
            )

            if port.upper() not in {
                item["device"].upper() for item in self.serial_ports()
            }:
                raise RuntimeError(
                    f"{port} disconnected before upload. Reconnect the WROVER and try again."
                )
            self.assert_port_available(port)

            self.phase = "upload"
            self.message = f"Uploading the relay firmware to {port}..."
            await self._run_command(
                [
                    arduino_cli,
                    "upload",
                    "--port",
                    port,
                    "--fqbn",
                    WROVER_FQBN,
                    str(relay_sketch),
                ],
                cwd=self.firmware_repo,
            )

            self._start_serial_monitor(port)

            self.phase = "complete"
            self.message = "WROVER programmed. Opening the live telemetry dashboard..."
            self.success = True
        except Exception as error:
            self.phase = "failed"
            self.error = str(error)
            self.message = "Setup stopped before the WROVER was programmed."
            self.logs.append(f"ERROR: {error}")
        finally:
            self.running = False

    async def _run_command(self, command: list[str], cwd: Path) -> None:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=creationflags,
        )
        assert process.stdout is not None
        while process.returncode is None:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=1.0)
            except TimeoutError:
                continue
            if not line:
                await process.wait()
                break
            text = line.decode(errors="replace").rstrip()
            if text:
                self.logs.append(text)
                if len(self.logs) > 300:
                    del self.logs[:100]

        # cloudflared is deliberately left running by the preparation script.
        # On Windows it can retain an inherited stdout handle after PowerShell
        # exits, so process completion, not pipe EOF, is authoritative here.
        return_code = await process.wait()
        if return_code != 0:
            executable = Path(command[0]).name
            raise RuntimeError(f"{executable} exited with code {return_code}.")


setup_coordinator = SetupCoordinator()

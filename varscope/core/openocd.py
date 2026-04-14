from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from varscope.presets import bundled_openocd_dir


class OpenOcdError(RuntimeError):
    pass


@dataclass(slots=True)
class OpenOcdLaunch:
    cfg_path: Path
    executable: Path | None = None


class OpenOcdClient:
    def __init__(self, launch: OpenOcdLaunch):
        self.launch = launch
        self._tcl_port = _free_tcp_port()
        self._process: subprocess.Popen[str] | None = None
        self._socket: socket.socket | None = None
        self._log_buffer: deque[str] = deque(maxlen=500)
        self._log_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        executable = find_openocd_binary(self.launch.executable)
        command = [str(executable)]
        script_dir = _openocd_script_dir(executable)
        if script_dir is not None:
            command.extend(["-s", str(script_dir)])
        command.extend(
            [
                "-c",
                "gdb_port disabled",
                "-c",
                "telnet_port disabled",
                "-c",
                f"tcl_port {self._tcl_port}",
                "-f",
                str(self.launch.cfg_path),
            ]
        )
        command.extend(["-c", "init"])

        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_hidden_subprocess_kwargs(),
        )
        self._log_thread = threading.Thread(target=self._drain_logs, name="openocd-log", daemon=True)
        self._log_thread.start()
        self._connect_socket()
        version = self.run_command("version")
        if "Open On-Chip Debugger" not in version:
            raise OpenOcdError(f"OpenOCD started but did not return a valid version: {version!r}")

    def close(self) -> None:
        with self._lock:
            if self._socket is not None:
                try:
                    self._send_command("shutdown")
                except Exception:
                    pass
                try:
                    self._socket.close()
                finally:
                    self._socket = None
        if self._process is not None:
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def run_command(self, command: str) -> str:
        with self._lock:
            return self._send_command(command)

    def target_state(self) -> str | None:
        output = self.run_command("targets")
        return _parse_target_state(output)

    def ensure_target_running(self) -> None:
        state = self.target_state()
        if state == "running":
            return

        # First try a plain resume to avoid disturbing runtime state.
        try:
            self.run_command("resume")
        except Exception:
            pass
        deadline = time.time() + 0.6
        while time.time() < deadline:
            current = self.target_state()
            if current == "running":
                return
            time.sleep(0.05)

        # Some probe/target combinations stay in reset/unknown; fallback to reset run.
        try:
            self.run_command("reset run")
        except Exception:
            pass

        deadline = time.time() + 1.5
        while time.time() < deadline:
            current = self.target_state()
            if current == "running":
                return
            time.sleep(0.05)

        raise OpenOcdError(
            f"Target did not enter running state (current: {self.target_state() or 'unknown'}). "
            "Tried both resume and reset run."
        )

    def read_memory(self, address: int, size: int) -> bytes:
        if size <= 0:
            return b""

        payload = bytearray()
        for chunk_address, width_bits, count in _plan_read_chunks(address, size):
            response = self.run_command(f"read_memory 0x{chunk_address:X} {width_bits} {count}")
            values = _parse_read_values(response)
            if len(values) != count:
                raise OpenOcdError(
                    f"OpenOCD read_memory returned {len(values)} values, expected {count}. Response: {response!r}"
                )
            payload.extend(_pack_read_values(values, width_bits))

        if len(payload) != size:
            raise OpenOcdError(
                f"OpenOCD read_memory returned {len(payload)} bytes, expected {size}."
            )
        return bytes(payload)

    def recent_logs(self) -> list[str]:
        return list(self._log_buffer)

    def _connect_socket(self) -> None:
        deadline = time.time() + 8.0
        last_error: Exception | None = None
        while time.time() < deadline:
            if self._process is not None and self._process.poll() is not None:
                break
            try:
                sock = socket.create_connection(("127.0.0.1", self._tcl_port), timeout=1.0)
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
            except OSError as exc:
                last_error = exc
                time.sleep(0.15)
                continue
            sock.settimeout(2.0)
            self._socket = sock
            return
        log_hint = "\n".join(self.recent_logs()[-10:])
        raise OpenOcdError(
            f"Unable to connect to OpenOCD Tcl port {self._tcl_port}."
            + (f" Last error: {last_error}" if last_error else "")
            + (f"\nRecent logs:\n{log_hint}" if log_hint else "")
        )

    def _send_command(self, command: str) -> str:
        if self._socket is None:
            raise OpenOcdError("OpenOCD Tcl socket is not connected.")
        payload = command.encode("utf-8") + b"\x1a"
        self._socket.sendall(payload)
        response = bytearray()
        while True:
            chunk = self._socket.recv(65536)
            if not chunk:
                break
            terminator = chunk.find(b"\x1a")
            if terminator >= 0:
                response.extend(chunk[:terminator])
                break
            response.extend(chunk)
        return response.decode("utf-8", errors="replace").strip()

    def _drain_logs(self) -> None:
        if self._process is None or self._process.stdout is None:
            return
        for line in self._process.stdout:
            self._log_buffer.append(line.rstrip())


def find_openocd_binary(explicit_path: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit_path is not None:
        candidates.append(Path(explicit_path))
    env_path = os.environ.get("VARSCOPE_OPENOCD")
    if env_path:
        candidates.append(Path(env_path))

    bundled = bundled_openocd_dir()
    candidates.extend(
        [
            bundled / "bin" / "openocd.exe",
            bundled / "bin" / "openocd",
            bundled / "openocd.exe",
            bundled / "openocd",
        ]
    )

    which = shutil.which("openocd")
    if which:
        candidates.append(Path(which))

    for path in candidates:
        if path.exists():
            return path
    raise OpenOcdError(
        "openocd executable not found. Add openocd to PATH, set VARSCOPE_OPENOCD, "
        "or place bundled binaries under runtime/openocd."
    )


def _openocd_script_dir(executable: Path) -> Path | None:
    bundled_root = bundled_openocd_dir()
    candidates = [
        bundled_root / "share" / "openocd" / "scripts",
        bundled_root / "scripts",
        executable.parent.parent / "share" / "openocd" / "scripts",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _hidden_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def _plan_read_chunks(address: int, size: int) -> list[tuple[int, int, int]]:
    chunks: list[tuple[int, int, int]] = []
    cursor = address
    remaining = size

    while remaining > 0 and cursor % 4 != 0:
        if cursor % 2 == 0 and remaining >= 2:
            chunks.append((cursor, 16, 1))
            cursor += 2
            remaining -= 2
        else:
            chunks.append((cursor, 8, 1))
            cursor += 1
            remaining -= 1

    if remaining >= 4:
        word_count = remaining // 4
        chunks.append((cursor, 32, word_count))
        cursor += word_count * 4
        remaining -= word_count * 4

    if remaining >= 2:
        halfword_count = remaining // 2
        chunks.append((cursor, 16, halfword_count))
        cursor += halfword_count * 2
        remaining -= halfword_count * 2

    if remaining > 0:
        chunks.append((cursor, 8, remaining))

    return chunks


def _parse_read_values(response: str) -> list[int]:
    tokens = [token for token in response.replace("{", " ").replace("}", " ").split() if token]
    values: list[int] = []
    for token in tokens:
        try:
            values.append(int(token, 0))
        except ValueError as exc:
            raise OpenOcdError(
                f"OpenOCD read_memory returned unparseable data: {response!r}"
            ) from exc
    return values


def _pack_read_values(values: list[int], width_bits: int) -> bytes:
    width_bytes = width_bits // 8
    payload = bytearray()
    mask = (1 << width_bits) - 1
    for value in values:
        payload.extend(int(value & mask).to_bytes(width_bytes, "little", signed=False))
    return bytes(payload)


def _parse_target_state(output: str) -> str | None:
    match = re.search(r"\b(running|halted|reset|unknown)\b", output.lower())
    if match is None:
        return None
    return match.group(1)

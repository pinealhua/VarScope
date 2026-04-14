from __future__ import annotations

import csv
import sqlite3
import threading
from pathlib import Path

from varscope.core.decoder import format_value
from varscope.models import ResolvedWatch, SampleFrame, SessionMetadata


def create_session_metadata(
    *,
    elf_path: Path | None,
    elf_sha256: str | None,
    cfg_path: Path | None,
    started_ns: int,
) -> SessionMetadata:
    return SessionMetadata(
        elf_path=Path(elf_path) if elf_path is not None else None,
        elf_sha256=elf_sha256,
        cfg_path=Path(cfg_path) if cfg_path is not None else None,
        started_ns=started_ns,
        stopped_ns=None,
    )


class SessionStore:
    def __init__(self, path: Path, conn: sqlite3.Connection):
        self.path = Path(path)
        self.conn = conn
        self._lock = threading.RLock()

    @classmethod
    def create(cls, path: Path, metadata: SessionMetadata, watches: list[ResolvedWatch]) -> SessionStore:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        store = cls(path, conn)
        store._init_schema()
        store._write_metadata(metadata)
        store._write_watches(watches)
        return store

    @classmethod
    def open_existing(cls, path: Path) -> SessionStore:
        conn = sqlite3.connect(Path(path), check_same_thread=False)
        return cls(Path(path), conn)

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA temp_store=MEMORY;
                PRAGMA busy_timeout=5000;
                PRAGMA wal_autocheckpoint=1000;
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS watches (
                    watch_index INTEGER PRIMARY KEY,
                    expression TEXT NOT NULL,
                    address INTEGER NOT NULL,
                    byte_size INTEGER NOT NULL,
                    type_name TEXT NOT NULL,
                    decoder_kind TEXT NOT NULL,
                    numeric INTEGER NOT NULL,
                    description TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    frame_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ns INTEGER NOT NULL,
                    watch_index INTEGER NOT NULL,
                    raw_value BLOB NOT NULL,
                    decoded_value TEXT,
                    FOREIGN KEY(watch_index) REFERENCES watches(watch_index)
                );
                CREATE INDEX IF NOT EXISTS idx_samples_time ON samples(timestamp_ns);
                CREATE INDEX IF NOT EXISTS idx_samples_watch ON samples(watch_index);
                """
            )
            self.conn.commit()

    def _write_metadata(self, metadata: SessionMetadata) -> None:
        rows = {
            "elf_path": str(metadata.elf_path) if metadata.elf_path else "",
            "elf_sha256": metadata.elf_sha256 or "",
            "cfg_path": str(metadata.cfg_path) if metadata.cfg_path else "",
            "started_ns": str(metadata.started_ns),
            "stopped_ns": "" if metadata.stopped_ns is None else str(metadata.stopped_ns),
        }
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                list(rows.items()),
            )
            self.conn.commit()

    def _write_watches(self, watches: list[ResolvedWatch]) -> None:
        rows = [
            (
                idx,
                watch.definition.expression,
                watch.address,
                watch.byte_size,
                watch.type_name,
                watch.decoder_kind,
                1 if watch.numeric else 0,
                watch.description,
            )
            for idx, watch in enumerate(watches)
        ]
        with self._lock:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO watches(
                    watch_index, expression, address, byte_size, type_name, decoder_kind, numeric, description
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self.conn.commit()

    def append_batch(self, frames: list[SampleFrame]) -> None:
        if not frames:
            return

        rows: list[tuple[int, int, bytes, str]] = []
        for frame in frames:
            for watch_index, (raw, decoded) in enumerate(zip(frame.raw_values, frame.decoded_values, strict=True)):
                rows.append((frame.timestamp_ns, watch_index, bytes(raw), format_value(decoded)))

        with self._lock:
            self.conn.executemany(
                "INSERT INTO samples(timestamp_ns, watch_index, raw_value, decoded_value) VALUES(?, ?, ?, ?)",
                rows,
            )
            self.conn.commit()

    def update_stopped_ns(self, stopped_ns: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('stopped_ns', ?)",
                (str(stopped_ns),),
            )
            self.conn.commit()

    def export_csv(self, destination: Path, watches: list[ResolvedWatch]) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        watch_names = [watch.definition.display_name for watch in watches]
        watch_count = len(watch_names)

        with self._lock:
            cursor = self.conn.execute(
                """
                SELECT timestamp_ns, watch_index, decoded_value
                FROM samples
                ORDER BY timestamp_ns ASC, watch_index ASC
                """
            )

            grouped: dict[int, list[str]] = {}
            for ts, watch_index, decoded in cursor:
                if ts not in grouped:
                    grouped[ts] = ["" for _ in range(watch_count)]
                if 0 <= watch_index < watch_count:
                    grouped[ts][watch_index] = decoded if decoded is not None else ""

        timestamps = sorted(grouped.keys())
        base_timestamp_ns = timestamps[0] if timestamps else 0

        with destination.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", *watch_names])
            for ts in timestamps:
                time_s = (ts - base_timestamp_ns) / 1_000_000_000.0
                writer.writerow([f"{time_s:.9f}", *grouped[ts]])

    def close(self) -> None:
        with self._lock:
            self.conn.close()

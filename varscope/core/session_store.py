from __future__ import annotations

import csv
import sqlite3
import threading
from pathlib import Path

from varscope.core.decoder import compile_payload_decoder, format_value
from varscope.core.memory import extract_raw_values
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
                CREATE TABLE IF NOT EXISTS frames (
                    frame_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ns INTEGER NOT NULL,
                    raw_blob BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_samples_time ON samples(timestamp_ns);
                CREATE INDEX IF NOT EXISTS idx_samples_watch ON samples(watch_index);
                CREATE INDEX IF NOT EXISTS idx_frames_time ON frames(timestamp_ns);
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

        rows = [
            (frame.timestamp_ns, sqlite3.Binary(self._materialize_raw_blob(frame)))
            for frame in frames
        ]

        with self._lock:
            self.conn.executemany(
                "INSERT INTO frames(timestamp_ns, raw_blob) VALUES(?, ?)",
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

    def _materialize_raw_values(self, frame: SampleFrame) -> list[bytes]:
        if frame.raw_values is not None:
            return frame.raw_values
        if frame.region_payloads is None or frame.read_regions is None:
            raise ValueError("Frame is missing raw values and region payload metadata.")
        return extract_raw_values(
            list(frame.read_regions),
            list(frame.region_payloads),
            len(frame.decoded_values),
        )

    def _materialize_raw_blob(self, frame: SampleFrame) -> bytes:
        raw_values = self._materialize_raw_values(frame)
        return b"".join(raw_values)

    def export_csv(self, destination: Path, watches: list[ResolvedWatch]) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        self._validate_export_watches(watches)
        watch_names = [watch.definition.display_name for watch in watches]

        with destination.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", *watch_names])
            if not self._export_frame_rows_csv(writer, watches):
                self._export_legacy_samples_csv(writer, len(watch_names))

    def _export_frame_rows_csv(self, writer: csv.writer, watches: list[ResolvedWatch]) -> bool:
        watch_sizes = [watch.byte_size for watch in watches]
        total_size = sum(watch_sizes)
        decoders = [compile_payload_decoder(watch.type_info) for watch in watches]

        with self._lock:
            try:
                cursor = self.conn.execute(
                    """
                    SELECT timestamp_ns, raw_blob
                    FROM frames
                    ORDER BY timestamp_ns ASC, frame_id ASC
                    """
                )
            except sqlite3.OperationalError:
                return False

            rows = cursor.fetchall()

        if not rows:
            return False

        base_timestamp_ns = rows[0][0]
        for timestamp_ns, raw_blob in rows:
            raw_view = memoryview(raw_blob)
            if len(raw_view) != total_size:
                raise ValueError(
                    f"Stored frame blob size {len(raw_view)} does not match expected watch payload size {total_size}."
                )

            offset = 0
            decoded_text: list[str] = []
            for watch_size, decoder in zip(watch_sizes, decoders, strict=True):
                decoded_text.append(format_value(decoder(raw_view, offset)))
                offset += watch_size

            time_s = (timestamp_ns - base_timestamp_ns) / 1_000_000_000.0
            writer.writerow([f"{time_s:.9f}", *decoded_text])
        return True

    def _export_legacy_samples_csv(self, writer: csv.writer, watch_count: int) -> None:
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
        for ts in timestamps:
            time_s = (ts - base_timestamp_ns) / 1_000_000_000.0
            writer.writerow([f"{time_s:.9f}", *grouped[ts]])

    def _validate_export_watches(self, watches: list[ResolvedWatch]) -> None:
        with self._lock:
            stored_rows = self.conn.execute(
                """
                SELECT expression, address, byte_size, decoder_kind, numeric
                FROM watches
                ORDER BY watch_index ASC
                """
            ).fetchall()

        if not stored_rows:
            return
        if len(stored_rows) != len(watches):
            raise ValueError(
                "Stored capture watch count does not match the export watch list. "
                "Refusing to export because columns could be decoded against the wrong variables."
            )

        for index, (stored, watch) in enumerate(zip(stored_rows, watches, strict=True)):
            expression, address, byte_size, decoder_kind, numeric = stored
            if (
                expression != watch.definition.expression
                or int(address) != int(watch.address)
                or int(byte_size) != int(watch.byte_size)
                or decoder_kind != watch.decoder_kind
                or bool(numeric) != bool(watch.numeric)
            ):
                raise ValueError(
                    "Stored capture watch metadata does not match the export watch list at "
                    f"column {index + 1} ({watch.definition.display_name!r}). "
                    "Refusing to export because CSV columns would not be time-aligned with the stored frame data."
                )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

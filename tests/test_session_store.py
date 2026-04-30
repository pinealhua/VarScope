from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from varscope.core.memory import plan_read_regions
from varscope.core.session_store import SessionStore, create_session_metadata
from varscope.core.typesystem import TypeInfo
from varscope.models import ResolvedWatch, SampleFrame, WatchDefinition


def _watch(expression: str = "counter", address: int = 0x20000000) -> ResolvedWatch:
    t_u32 = TypeInfo(kind="base", name="uint32_t", byte_size=4)
    return ResolvedWatch(
        definition=WatchDefinition(expression=expression),
        address=address,
        byte_size=4,
        type_name="uint32_t",
        decoder_kind="uint32",
        numeric=True,
        description="",
        type_info=t_u32,
    )


def _metadata():
    return create_session_metadata(
        elf_path=None,
        elf_sha256=None,
        cfg_path=None,
        started_ns=1,
    )


class SessionStoreTests(unittest.TestCase):
    def test_create_append_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "capture.sqlite"
            watch = _watch()
            store = SessionStore.create(db_path, _metadata(), [watch])
            store.append_batch(
                [
                    SampleFrame(timestamp_ns=1_000_000_000, raw_values=[b"\x01\x00\x00\x00"], decoded_values=[1]),
                    SampleFrame(timestamp_ns=1_250_000_000, raw_values=[b"\x02\x00\x00\x00"], decoded_values=[2]),
                ]
            )
            csv_path = Path(tmp) / "out.csv"
            store.export_csv(csv_path, [watch])
            store.close()

            conn = sqlite3.connect(db_path)
            try:
                frame_count = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
                sample_count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            finally:
                conn.close()

            text = csv_path.read_text(encoding="utf-8")
            self.assertEqual(frame_count, 2)
            self.assertEqual(sample_count, 0)
            self.assertIn("time_s,counter", text)
            self.assertIn("0.000000000,1", text)
            self.assertIn("0.250000000,2", text)
            self.assertNotIn("timestamp_ns", text)

    def test_export_refuses_watch_order_that_does_not_match_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "capture.sqlite"
            watts = _watch("power_w", 0x20000000)
            rpm = _watch("motor_rpm", 0x20000004)
            store = SessionStore.create(db_path, _metadata(), [watts, rpm])
            store.append_batch(
                [
                    SampleFrame(
                        timestamp_ns=1_000_000_000,
                        raw_values=[b"\x64\x00\x00\x00", b"\xe8\x03\x00\x00"],
                        decoded_values=[100, 1000],
                    )
                ]
            )

            with self.assertRaisesRegex(ValueError, "metadata does not match"):
                store.export_csv(Path(tmp) / "out.csv", [rpm, watts])
            store.close()

    def test_append_batch_from_worker_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "capture.sqlite"
            watch = _watch()
            store = SessionStore.create(db_path, _metadata(), [watch])
            error: list[BaseException] = []

            def append() -> None:
                try:
                    store.append_batch(
                        [SampleFrame(timestamp_ns=1, raw_values=[b"\x2a\x00\x00\x00"], decoded_values=[42])]
                    )
                except BaseException as exc:
                    error.append(exc)

            thread = threading.Thread(target=append)
            thread.start()
            thread.join(timeout=2.0)
            store.update_stopped_ns(2)
            csv_path = Path(tmp) / "out.csv"
            store.export_csv(csv_path, [watch])
            store.close()

            self.assertFalse(error)
            self.assertIn("42", csv_path.read_text(encoding="utf-8"))

    def test_append_batch_materializes_raw_values_from_region_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "capture.sqlite"
            watch = _watch()
            store = SessionStore.create(db_path, _metadata(), [watch])

            regions = plan_read_regions([watch])
            store.append_batch(
                [
                    SampleFrame(
                        timestamp_ns=1,
                        raw_values=None,
                        decoded_values=[42],
                        region_payloads=(b"\x2a\x00\x00\x00",),
                        read_regions=tuple(regions),
                    )
                ]
            )

            csv_path = Path(tmp) / "out.csv"
            store.export_csv(csv_path, [watch])
            store.close()

            self.assertIn("42", csv_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

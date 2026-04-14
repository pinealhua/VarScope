from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from varscope.core.session_store import SessionStore, create_session_metadata
from varscope.core.typesystem import TypeInfo
from varscope.models import ResolvedWatch, SampleFrame, WatchDefinition


def _watch() -> ResolvedWatch:
    t_u32 = TypeInfo(kind="base", name="uint32_t", byte_size=4)
    return ResolvedWatch(
        definition=WatchDefinition(expression="counter"),
        address=0x20000000,
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

            text = csv_path.read_text(encoding="utf-8")
            self.assertIn("time_s,counter", text)
            self.assertIn("0.000000000,1", text)
            self.assertIn("0.250000000,2", text)
            self.assertNotIn("timestamp_ns", text)

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


if __name__ == "__main__":
    unittest.main()

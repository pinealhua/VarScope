from __future__ import annotations

import queue
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from varscope.core.controller import SamplerWorker, SessionController
from varscope.core.expressions import ExpressionError
from varscope.core.typesystem import StructField, TypeInfo
from varscope.models import ResolvedWatch, SampleFrame, WatchDefinition


@dataclass
class _Var:
    name: str
    address: int
    type_info: TypeInfo


class _Db:
    def __init__(self, variables: dict[str, _Var]):
        self.variables = variables

    def all_variables(self):
        return list(self.variables.values())

    def lookup_variable(self, name: str):
        return self.variables.get(name)

    def is_ambiguous(self, name: str) -> bool:
        return False


class _OpenOcd:
    def __init__(self) -> None:
        self.ensure_calls = 0

    def ensure_target_running(self) -> None:
        self.ensure_calls += 1


@dataclass
class _SymbolDbMeta:
    elf_path: Path
    elf_sha256: str


class _Store:
    pass


class _Recorder:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.started = False
        self.stop_drain: bool | None = None
        self.error: Exception | None = None

    def start(self) -> None:
        self.started = True

    def stop(self, *, drain: bool = True) -> None:
        self.stop_drain = drain

    def join(self, timeout: float | None = None) -> None:
        pass


class _Sampler:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass

    def join(self, timeout: float | None = None) -> None:
        pass


def _resolved_watch(expression: str, address: int, type_info: TypeInfo) -> ResolvedWatch:
    return ResolvedWatch(
        definition=WatchDefinition(expression=expression),
        address=address,
        byte_size=type_info.byte_size,
        type_name=type_info.name,
        decoder_kind="uint",
        numeric=True,
        description=type_info.name,
        type_info=type_info,
    )


class SessionControllerCompletionTests(unittest.TestCase):
    def test_completion_names_expand_nested_struct_members(self) -> None:
        u16 = TypeInfo(kind="base", name="uint16_t", byte_size=2)
        attitude = TypeInfo(
            kind="struct",
            name="Attitude",
            byte_size=4,
            fields=[
                StructField(name="pitch", type_info=u16, bit_offset=0),
                StructField(name="yaw", type_info=u16, bit_offset=16),
            ],
        )
        state = TypeInfo(
            kind="struct",
            name="GimbalState",
            byte_size=4,
            fields=[StructField(name="attitude", type_info=attitude, bit_offset=0)],
        )
        controller = SessionController(Path("."))
        controller.symbol_db = _Db({"gimbal": _Var("gimbal", 0x20000000, state)})

        names = controller.completion_names("yaw")

        self.assertEqual(names, ["gimbal.attitude.yaw"])
        self.assertNotIn("gimbal", controller.completion_names())

    def test_add_watch_rejects_aggregate_roots(self) -> None:
        u16 = TypeInfo(kind="base", name="uint16_t", byte_size=2)
        point = TypeInfo(
            kind="struct",
            name="Point",
            byte_size=4,
            fields=[
                StructField(name="x", type_info=u16, bit_offset=0),
                StructField(name="y", type_info=u16, bit_offset=16),
            ],
        )
        controller = SessionController(Path("."))
        controller.symbol_db = _Db({"point": _Var("point", 0x20000000, point)})

        with self.assertRaises(ExpressionError):
            controller.add_watch(WatchDefinition(expression="point"))

        watch = controller.add_watch(WatchDefinition(expression="point.y"))
        self.assertEqual(watch.address, 0x20000002)

    def test_drain_frames_with_timestamps_preserves_all_drained_frames(self) -> None:
        controller = SessionController(Path("."))
        for i in range(10):
            controller.frame_queue.put(SampleFrame(timestamp_ns=i, raw_values=[], decoded_values=[]))

        frames, timestamps = controller.drain_frames_with_timestamps(max_frames=10)

        self.assertEqual(timestamps, list(range(10)))
        self.assertEqual(len(frames), 10)
        self.assertEqual(frames[0].timestamp_ns, 0)
        self.assertEqual(frames[-1].timestamp_ns, 9)
        self.assertEqual(controller.drain_frames(), [])

    def test_sampler_display_queue_drops_old_frames_when_full(self) -> None:
        frame_queue: queue.Queue[SampleFrame] = queue.Queue(maxsize=2)
        worker = SamplerWorker(
            client=object(),
            watches=[],
            frame_queue=frame_queue,
            recorder=None,
            sample_interval_s=0.0,
            error_queue=queue.Queue(),
        )

        for i in range(3):
            worker._enqueue_display_frame(SampleFrame(timestamp_ns=i, raw_values=[], decoded_values=[]))

        self.assertEqual(worker.dropped_display_frames, 1)
        self.assertEqual([frame_queue.get_nowait().timestamp_ns for _ in range(2)], [1, 2])

    def test_sampler_worker_uses_watch_snapshot(self) -> None:
        u16 = TypeInfo(kind="base", name="uint16_t", byte_size=2)
        watches = [
            _resolved_watch("Eul.pitch", 0x20000000, u16),
            _resolved_watch("Eul.roll", 0x20000002, u16),
        ]

        worker = SamplerWorker(
            client=object(),
            watches=watches,
            frame_queue=object(),
            recorder=None,
            sample_interval_s=0.0,
            error_queue=object(),
        )
        watches.append(_resolved_watch("Eul.yaw", 0x20000004, u16))

        self.assertEqual(len(worker.watches), 2)
        self.assertEqual(sum(len(region.slices) for region in worker.regions), 2)

    def test_stop_capture_does_not_drain_recorder_backlog(self) -> None:
        controller = SessionController(Path("."))
        recorder = _Recorder(_Store())
        controller.recorder = recorder  # type: ignore[assignment]

        controller.stop_capture()

        self.assertFalse(recorder.stop_drain)
        self.assertIsNone(controller.recorder)

    def test_start_capture_freezes_watch_list_for_store_and_sampler(self) -> None:
        u16 = TypeInfo(kind="base", name="uint16_t", byte_size=2)
        watches = [
            _resolved_watch("Eul.pitch", 0x20000000, u16),
            _resolved_watch("Eul.roll", 0x20000002, u16),
        ]
        controller = SessionController(Path("."))
        controller.openocd = _OpenOcd()
        controller.symbol_db = _SymbolDbMeta(Path("test.elf"), "sha256")
        controller.resolved_watches = watches
        captured_store_watches: list[ResolvedWatch] | None = None
        captured_sampler_watches: list[ResolvedWatch] | None = None

        def create_store(_path, _metadata, store_watches):
            nonlocal captured_store_watches
            captured_store_watches = store_watches
            return _Store()

        def make_sampler(**kwargs):
            nonlocal captured_sampler_watches
            captured_sampler_watches = kwargs["watches"]
            return _Sampler(**kwargs)

        with (
            patch("varscope.core.controller.temp_session_path", return_value=Path("capture.sqlite")),
            patch("varscope.core.controller.SessionStore.create", side_effect=create_store),
            patch("varscope.core.controller.RecorderWorker", side_effect=_Recorder),
            patch("varscope.core.controller.SamplerWorker", side_effect=make_sampler),
        ):
            controller.start_capture(sample_interval_s=0.0)

        watches.append(_resolved_watch("Eul.yaw", 0x20000004, u16))

        self.assertIsNot(captured_store_watches, watches)
        self.assertIsNot(captured_sampler_watches, watches)
        self.assertEqual(len(captured_store_watches or []), 2)
        self.assertEqual(len(captured_sampler_watches or []), 2)


if __name__ == "__main__":
    unittest.main()
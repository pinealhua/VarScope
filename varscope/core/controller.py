from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from varscope.core.decoder import decode_value, decoder_kind_for_type
from varscope.core.expressions import ExpressionError, parse_expression, resolve_expression
from varscope.core.memory import extract_raw_values, plan_read_regions
from varscope.core.openocd import OpenOcdClient, OpenOcdError, OpenOcdLaunch
from varscope.core.session_store import SessionStore, create_session_metadata
from varscope.core.workspace_store import temp_session_path
from varscope.models import ResolvedWatch, SampleFrame, WatchDefinition


TARGET_CHECK_INTERVAL_NS = 5_000_000_000
RECORDER_BATCH_SIZE = 2048
RECORDER_FLUSH_INTERVAL_S = 0.25
RECORDER_IDLE_SLEEP_S = 0.005
DISPLAY_FRAME_QUEUE_MAX = 4096

class RecorderWorker(threading.Thread):
    def __init__(self, store: SessionStore):
        super().__init__(name="varscope-recorder", daemon=True)
        self.store = store
        self.queue: queue.SimpleQueue[SampleFrame] = queue.SimpleQueue()
        self.stop_event = threading.Event()
        self.error: Exception | None = None
        self._drain_on_stop = True

    def enqueue(self, frame: SampleFrame) -> None:
        if not self.stop_event.is_set():
            self.queue.put(frame)

    def stop(self, *, drain: bool = True) -> None:
        self._drain_on_stop = drain
        self.stop_event.set()

    def run(self) -> None:
        batch: list[SampleFrame] = []
        last_flush = time.monotonic()
        while not self.stop_event.is_set() or (self._drain_on_stop and not self.queue.empty()):
            try:
                frame = self.queue.get_nowait()
            except queue.Empty:
                time.sleep(RECORDER_IDLE_SLEEP_S)
                frame = None  # type: ignore[assignment]
            if frame is not None:
                batch.append(frame)
            now = time.monotonic()
            if batch and (
                len(batch) >= RECORDER_BATCH_SIZE
                or now - last_flush >= RECORDER_FLUSH_INTERVAL_S
                or self.stop_event.is_set()
            ):
                try:
                    self.store.append_batch(batch)
                except Exception as exc:
                    self.error = exc
                    return
                batch.clear()
                last_flush = now
        if batch:
            try:
                self.store.append_batch(batch)
            except Exception as exc:
                self.error = exc


class SamplerWorker(threading.Thread):
    def __init__(
        self,
        client: OpenOcdClient,
        watches: list[ResolvedWatch],
        frame_queue: "queue.Queue[SampleFrame]",
        recorder: RecorderWorker | None,
        sample_interval_s: float,
        error_queue: "queue.Queue[str]",
    ):
        super().__init__(name="varscope-sampler", daemon=True)
        self.client = client
        self.watches = list(watches)
        self.frame_queue = frame_queue
        self.recorder = recorder
        self.sample_interval_s = sample_interval_s
        self.error_queue = error_queue
        self.stop_event = threading.Event()
        self.regions = plan_read_regions(self.watches)
        self._next_target_check_ns = time.monotonic_ns() + TARGET_CHECK_INTERVAL_NS
        self.dropped_display_frames = 0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            begin = time.perf_counter()
            try:
                now_ns = time.monotonic_ns()
                if now_ns >= self._next_target_check_ns:
                    self.client.ensure_target_running()
                    self._next_target_check_ns = now_ns + TARGET_CHECK_INTERVAL_NS

                payloads = []
                for region in self.regions:
                    if self.stop_event.is_set():
                        return
                    payloads.append(self.client.read_memory(region.address, region.size))
                raw_values = extract_raw_values(self.regions, payloads, len(self.watches))
                if self.stop_event.is_set():
                    return
                decoded = [
                    decode_value(raw, watch.type_info)
                    for raw, watch in zip(raw_values, self.watches, strict=True)
                ]
                if self.stop_event.is_set():
                    return
                frame = SampleFrame(timestamp_ns=time.time_ns(), raw_values=raw_values, decoded_values=decoded)
                self._enqueue_display_frame(frame)
                if self.recorder is not None:
                    self.recorder.enqueue(frame)
            except Exception as exc:
                self.error_queue.put(str(exc))
                return

            elapsed = time.perf_counter() - begin
            sleep_s = max(0.0, self.sample_interval_s) - elapsed
            if sleep_s > 0:
                time.sleep(sleep_s)


    def _enqueue_display_frame(self, frame: SampleFrame) -> None:
        try:
            self.frame_queue.put_nowait(frame)
            return
        except queue.Full:
            self.dropped_display_frames += 1

        try:
            self.frame_queue.get_nowait()
        except queue.Empty:
            pass

        try:
            self.frame_queue.put_nowait(frame)
        except queue.Full:
            self.dropped_display_frames += 1


class SessionController:
    def __init__(self, app_root: Path):
        self.app_root = Path(app_root)
        self.symbol_db = None
        self.openocd: OpenOcdClient | None = None
        self.watch_definitions: list[WatchDefinition] = []
        self.resolved_watches: list[ResolvedWatch] = []
        self.frame_queue: queue.Queue[SampleFrame] = queue.Queue(maxsize=DISPLAY_FRAME_QUEUE_MAX)
        self.error_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.recorder: RecorderWorker | None = None
        self.sampler: SamplerWorker | None = None
        self.session_store: SessionStore | None = None
        self.current_session_path: Path | None = None
        self.current_cfg_path: Path | None = None

    def load_elf(self, elf_path: Path) -> tuple[Path, str, list[WatchDefinition]]:
        from varscope.core.dwarf_symbols import ElfSymbolLoader

        loader = ElfSymbolLoader(Path(elf_path))
        self.symbol_db = loader.load()
        dropped = self._re_resolve_watches()
        return self.symbol_db.elf_path, self.symbol_db.elf_sha256, dropped

    def completion_names(
        self,
        prefix: str = "",
        *,
        max_array_items: int = 32,
        max_results: int = 4096,
    ) -> list[str]:
        if self.symbol_db is None:
            return []

        prefix = prefix.lower()
        names: list[str] = []
        seen: set[str] = set()
        for record in self.symbol_db.all_variables():
            leaf_paths = record.type_info.leaf_paths(
                record.name,
                max_array_items=max_array_items,
                max_results=max_results,
            )
            if not leaf_paths and record.type_info.unwrap().is_scalar_like():
                leaf_paths = [record.name]
            for name in leaf_paths:
                if name in seen:
                    continue
                if prefix and prefix not in name.lower():
                    continue
                seen.add(name)
                names.append(name)
                if len(names) >= max_results:
                    return sorted(names, key=str.lower)
        return sorted(names, key=str.lower)

    def add_watch(self, definition: WatchDefinition) -> ResolvedWatch:
        resolved = self._resolve_watch(definition)
        self.watch_definitions.append(definition)
        self.resolved_watches.append(resolved)
        return resolved

    def remove_watch(self, index: int) -> None:
        del self.watch_definitions[index]
        del self.resolved_watches[index]

    def clear_watches(self) -> None:
        self.watch_definitions.clear()
        self.resolved_watches.clear()

    def connect(self, cfg_path: Path, openocd_path: Path | None = None) -> None:
        self.disconnect()
        launch = OpenOcdLaunch(cfg_path=Path(cfg_path), executable=openocd_path)
        client = OpenOcdClient(launch)
        client.start()
        self.openocd = client
        self.current_cfg_path = Path(cfg_path)

    def disconnect(self) -> None:
        self.stop_capture()
        if self.openocd is not None:
            self.openocd.close()
            self.openocd = None

    def start_capture(self, sample_interval_s: float = 0.0) -> Path:
        if self.openocd is None:
            raise OpenOcdError("OpenOCD is not connected.")
        if not self.resolved_watches:
            raise ValueError("Add at least one watch before starting capture.")

        self.openocd.ensure_target_running()

        self.stop_capture()
        self._clear_runtime_queues()

        started_ns = time.time_ns()
        path = temp_session_path(self.app_root)
        metadata = create_session_metadata(
            elf_path=self.symbol_db.elf_path if self.symbol_db else None,
            elf_sha256=self.symbol_db.elf_sha256 if self.symbol_db else None,
            cfg_path=self.current_cfg_path,
            started_ns=started_ns,
        )
        capture_watches = list(self.resolved_watches)
        self.session_store = SessionStore.create(path, metadata, capture_watches)
        self.current_session_path = path

        self.recorder = RecorderWorker(self.session_store)
        self.recorder.start()
        self.sampler = SamplerWorker(
            client=self.openocd,
            watches=capture_watches,
            frame_queue=self.frame_queue,
            recorder=self.recorder,
            sample_interval_s=sample_interval_s,
            error_queue=self.error_queue,
        )
        self.sampler.start()
        return path

    def stop_capture(self) -> None:
        if self.sampler is not None:
            self.sampler.stop()
            self.sampler.join(timeout=1.5)
            self.sampler = None
        if self.recorder is not None:
            self.recorder.stop(drain=False)
            self.recorder.join(timeout=2.0)
            if self.recorder.error is not None:
                self.error_queue.put(str(self.recorder.error))
            self.recorder = None
        if self.session_store is not None:
            self.session_store.update_stopped_ns(time.time_ns())
            self.session_store.close()
            self.session_store = None

    def drain_frames(self, *, max_frames: int | None = None) -> list[SampleFrame]:
        frames: list[SampleFrame] = []
        if max_frames is not None and max_frames <= 0:
            return frames

        while True:
            if max_frames is not None and len(frames) >= max_frames:
                return frames
            try:
                frames.append(self.frame_queue.get_nowait())
            except queue.Empty:
                return frames

    def drain_frames_with_timestamps(
        self,
        *,
        max_frames: int | None = None,
    ) -> tuple[list[SampleFrame], list[int]]:
        frames = self.drain_frames(max_frames=max_frames)
        return frames, [frame.timestamp_ns for frame in frames]

    def pop_error(self) -> str | None:
        try:
            return self.error_queue.get_nowait()
        except queue.Empty:
            return None

    def export_csv(self, destination: Path) -> None:
        if self.current_session_path is None:
            raise ValueError("No session is available to export.")
        store = self.session_store or SessionStore.open_existing(self.current_session_path)
        should_close = store is not self.session_store
        try:
            store.export_csv(destination, self.resolved_watches)
        finally:
            if should_close:
                store.close()

    def is_capturing(self) -> bool:
        return self.sampler is not None and self.sampler.is_alive()

    def shutdown(self) -> None:
        self.disconnect()

    def _clear_runtime_queues(self) -> None:
        while True:
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self.error_queue.get_nowait()
            except queue.Empty:
                break
    def _re_resolve_watches(self) -> list[WatchDefinition]:
        if not self.watch_definitions:
            self.resolved_watches = []
            return []

        kept_definitions: list[WatchDefinition] = []
        kept_resolved: list[ResolvedWatch] = []
        dropped: list[WatchDefinition] = []
        for definition in self.watch_definitions:
            try:
                resolved = self._resolve_watch(definition)
            except Exception:
                dropped.append(definition)
                continue
            kept_definitions.append(definition)
            kept_resolved.append(resolved)

        self.watch_definitions = kept_definitions
        self.resolved_watches = kept_resolved
        return dropped

    def _resolve_watch(self, definition: WatchDefinition) -> ResolvedWatch:
        if self.symbol_db is None:
            raise ValueError("Load an ELF file before adding watches.")
        parsed = parse_expression(definition.expression)
        if self.symbol_db.is_ambiguous(parsed.root):
            raise ExpressionError(
                f"Global variable '{parsed.root}' is ambiguous in ELF symbols."
            )

        result = resolve_expression(definition.expression, self.symbol_db)
        type_info = result.type_info
        if not type_info.unwrap().is_scalar_like():
            raise ExpressionError(
                f"Expression '{definition.expression}' resolves to {type_info.unwrap().describe()}, "
                "which cannot be decoded directly. Select a scalar member instead."
            )
        return ResolvedWatch(
            definition=definition,
            address=result.address,
            byte_size=result.byte_size,
            type_name=type_info.unwrap().describe(),
            decoder_kind=decoder_kind_for_type(type_info),
            numeric=type_info.unwrap().is_numeric(),
            description=f"0x{result.address:08X} / {type_info.unwrap().describe()}",
            type_info=type_info,
        )

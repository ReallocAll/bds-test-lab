import asyncio
import hashlib
import json
import os
import threading
import time
from collections.abc import Generator
from pathlib import Path

from endstone.event import PlayerMoveEvent, event_handler
from endstone.plugin import Plugin


class HotspotPlugin(Plugin):
    """Deterministic real-Endstone workloads for Spark Python attribution tests."""

    api_version = "0.11"

    def on_enable(self) -> None:
        self.mode = os.environ.get("SPARK_PYTHON_HOTSPOT_MODE", "single").strip().lower()
        self.iterations = max(100, int(os.environ.get("SPARK_PYTHON_HOTSPOT_ITERATIONS", "12000")))
        self._stop_worker = threading.Event()
        self._worker: threading.Thread | None = None
        self._generator = self._generator_sequence()
        self._event_counter = 0
        self._tick_metrics: list[tuple[int, float, float]] = []
        metrics_path = os.environ.get("SPARK_PYTHON_TICK_METRICS", "").strip()
        self._metrics_path = Path(metrics_path) if metrics_path else None
        self.register_events(self)
        self.server.scheduler.run_task(self, self.light_tick, delay=0, period=1)
        if self.mode in {"worker", "mixed", "fleet"}:
            self._worker = threading.Thread(
                target=self.worker_thread_hotspot,
                name="spark-python-hotspot-worker",
                daemon=True,
            )
            self._worker.start()
        self.logger.info(f"Spark Python hotspot test enabled: mode={self.mode} iterations={self.iterations}")

    def on_disable(self) -> None:
        self._stop_worker.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=2.0)
        self._write_tick_metrics()
        self.logger.info("Spark Python hotspot test disabled")

    def _record_tick_metrics(self) -> None:
        if self._metrics_path is None:
            return
        self._tick_metrics.append(
            (time.monotonic_ns(), float(self.server.current_mspt), float(self.server.current_tps))
        )

    def _write_tick_metrics(self) -> None:
        if self._metrics_path is None:
            return
        payload = {
            "samples": [
                {"monotonic_ns": timestamp, "mspt": mspt, "tps": tps}
                for timestamp, mspt, tps in self._tick_metrics
            ]
        }
        self._metrics_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._metrics_path.with_suffix(self._metrics_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self._metrics_path)

    def light_tick(self) -> None:
        self._record_tick_metrics()
        mode = self.mode
        if mode == "off":
            return
        if mode == "single":
            self.cpu_hotspot()
        elif mode == "nested":
            self.nested_hotspot()
        elif mode == "dual":
            self.dual_hotspot()
        elif mode in {"mixed", "fleet"}:
            self.cpu_hotspot(self.iterations // 3)
            self.nested_hotspot(self.iterations // 4)
            self.stdlib_hotspot(self.iterations // 24)
            self.exception_hotspot(12)
            self.generator_hotspot(18)
            self.async_hotspot(8)
        elif mode == "worker":
            self.cpu_hotspot(self.iterations // 8)
        elif mode == "stress":
            self.small_call_stress(256)

    def cpu_hotspot(self, iterations: int | None = None) -> int:
        return self.integer_hash_loop(iterations or self.iterations)

    @staticmethod
    def integer_hash_loop(iterations: int) -> int:
        value = 0x9E3779B97F4A7C15
        for index in range(iterations):
            value ^= (index + 0x517CC1B727220A95) & 0xFFFFFFFFFFFFFFFF
            value = ((value << 13) | (value >> 51)) & 0xFFFFFFFFFFFFFFFF
            value = (value * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        return value

    def small_call_stress(self, calls: int) -> int:
        value = 0
        for index in range(calls):
            value ^= self._tiny_function(index, value)
        return value

    @staticmethod
    def _tiny_function(index: int, value: int) -> int:
        return ((index * 33) ^ (value >> 3) ^ 0x9E37) & 0xFFFFFFFF

    def nested_hotspot(self, iterations: int | None = None) -> int:
        return self.level_one(iterations or self.iterations)

    def level_one(self, iterations: int) -> int:
        return self.level_two(iterations)

    def level_two(self, iterations: int) -> int:
        return self.level_three(iterations)

    def level_three(self, iterations: int) -> int:
        return self.cpu_leaf(iterations)

    @staticmethod
    def cpu_leaf(iterations: int) -> int:
        total = 1
        for index in range(iterations):
            total = (total * 33 + (index ^ (total >> 7))) & 0xFFFFFFFFFFFFFFFF
        return total

    def dual_hotspot(self) -> tuple[int, int]:
        return self.hotspot_a((self.iterations * 7) // 10), self.hotspot_b((self.iterations * 3) // 10)

    def hotspot_a(self, iterations: int) -> int:
        return self.integer_hash_loop(iterations)

    def hotspot_b(self, iterations: int) -> int:
        return self.integer_hash_loop(iterations)

    @staticmethod
    def stdlib_hotspot(rounds: int) -> str:
        payload = {"spark": "python-attribution", "values": list(range(64))}
        digest = b"seed"
        for _ in range(max(1, rounds)):
            encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
            digest = hashlib.sha256(digest + encoded).digest()
        return digest.hex()

    @staticmethod
    def _generator_sequence() -> Generator[int, None, None]:
        value = 0
        while True:
            value = (value * 1103515245 + 12345) & 0x7FFFFFFF
            yield value

    def generator_hotspot(self, resumes: int) -> int:
        value = 0
        for _ in range(resumes):
            value ^= next(self._generator)
        return value

    async def _async_leaf(self, rounds: int) -> int:
        value = 0
        for index in range(rounds):
            value ^= self.integer_hash_loop(80 + index)
            await asyncio.sleep(0)
        return value

    def async_hotspot(self, rounds: int) -> int:
        return asyncio.run(self._async_leaf(rounds))

    @staticmethod
    def exception_hotspot(rounds: int) -> int:
        caught = 0
        for index in range(rounds):
            try:
                HotspotPlugin._raise_for_test(index)
            except RuntimeError:
                caught += 1
        return caught

    @staticmethod
    def _raise_for_test(index: int) -> None:
        if index >= 0:
            raise RuntimeError("spark-python-attribution-test")

    def worker_thread_hotspot(self) -> None:
        while not self._stop_worker.is_set():
            self.integer_hash_loop(max(2000, self.iterations // 2))
            self._stop_worker.wait(0.002)

    @event_handler
    def on_player_move(self, event: PlayerMoveEvent) -> None:
        del event
        self._event_counter += 1
        if self.mode in {"mixed", "fleet"} and self._event_counter % 4 == 0:
            self.event_callback_hotspot()

    def event_callback_hotspot(self) -> int:
        return self.integer_hash_loop(max(400, self.iterations // 20))

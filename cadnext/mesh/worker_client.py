"""One owned, memory-limited geometry subprocess; unrelated CAD is never touched."""

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from cadnext.mesh.contracts import MeshError

IPC_LIMIT = 1024 * 1024
OPERATIONS = frozenset({"inspect", "fit", "section", "compare"})


class MeshWorkerClient:
    def __init__(self, *, command=None, timeout_s=60, memory_bytes=1024**3):
        self._command = command or [sys.executable, "-m", "cadnext.mesh.worker"]
        self._timeout = timeout_s
        self._memory_bytes = memory_bytes
        self._lock = asyncio.Lock()
        self.process = None
        self._job = None
        self._closed = False

    def _limit_process(self):
        if sys.platform != "win32":
            raise MeshError("unsupported_platform", "Ограничение памяти worker требует Windows.")
        import win32api
        import win32job

        job = win32job.CreateJobObject(None, "")
        try:
            limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
            limits["ProcessMemoryLimit"] = self._memory_bytes
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
            handle = win32api.OpenProcess(0x0100 | 0x0001, False, self.process.pid)
            try:
                win32job.AssignProcessToJobObject(job, handle)
            finally:
                handle.Close()
        except BaseException:
            job.Close()
            raise
        self._job = job

    async def _start(self):
        if self.process is not None and self.process.returncode is None:
            return
        await self._dispose()
        env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
               if key in os.environ}
        env.update(PYTHONUTF8="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        self.process = await asyncio.create_subprocess_exec(
            *self._command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=Path(__file__).resolve().parents[2], env=env,
            limit=IPC_LIMIT, creationflags=0x08000000 if sys.platform == "win32" else 0)
        # The production child imports only stdlib until its first bounded request.
        self._limit_process()

    async def _dispose(self, *, graceful=False):
        process = self.process
        if process is not None:
            if process.returncode is None:
                if graceful:
                    process.stdin.close()
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except TimeoutError:
                        process.kill()
                else:
                    process.kill()
            await process.wait()
            self.process = None
        if self._job is not None:
            self._job.Close()
            self._job = None

    async def request(self, operation, payload, *, stop) -> dict:
        if operation not in OPERATIONS:
            raise MeshError("unknown_operation", "Неизвестная операция анализа сетки.")
        request_id = uuid4().hex
        message = {"request_id": request_id, "operation": operation, "payload": payload}
        raw = json.dumps(message, allow_nan=False, ensure_ascii=True).encode()+b"\n"
        if len(raw) > IPC_LIMIT:
            raise MeshError("ipc_limit", "Запрос анализа сетки слишком велик.")
        async with self._lock:
            if self._closed or stop.is_set():
                raise MeshError("stopped", "Анализ сетки остановлен.")
            exchange = stopped = None
            try:
                await self._start()

                async def receive():
                    self.process.stdin.write(raw)
                    await self.process.stdin.drain()
                    line = await self.process.stdout.readline()
                    if not line or len(line) > IPC_LIMIT:
                        raise MeshError("worker_crash", "Процесс анализа сетки завершился без ответа.")
                    response = json.loads(line)
                    if (response.get("request_id") != request_id or
                            response.get("sha256") != payload.get("sha256") or
                            response.get("frame_id") != payload.get("frame_id")):
                        raise MeshError("stale_response", "Ответ анализа относится к другому эталону или запросу.")
                    return response

                exchange = asyncio.create_task(receive())
                stopped = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait({exchange, stopped}, timeout=self._timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set():
                    raise MeshError("stopped", "Анализ сетки остановлен.")
                if exchange not in done:
                    raise MeshError("worker_timeout", "Превышено время анализа сетки.")
                response = exchange.result()
                if "error" in response:
                    raise MeshError(response["error"]["code"], response["error"]["message"])
                return response
            except BaseException as exc:
                # Geometry is read-only and owned; terminating this child cannot cancel COM.
                await asyncio.shield(self._dispose())
                if isinstance(exc, (MeshError, asyncio.CancelledError)):
                    raise
                raise MeshError("worker_failure", "Не удалось выполнить анализ сетки.") from None
            finally:
                for task in (exchange, stopped):
                    if task:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task

    async def aclose(self):
        self._closed = True
        async with self._lock:
            await self._dispose(graceful=True)

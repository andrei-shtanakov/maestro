"""Local execution backend: an asyncio subprocess wrapped as a TaskHandle."""

import asyncio
import contextlib
import os
import shutil
from datetime import UTC, datetime

from maestro.execution.env import build_local_env
from maestro.execution.models import (
    BackendHealth,
    CapabilityResult,
    CollectResult,
    ExecutionHandleRef,
    ExecutionRequest,
    ExecutionResult,
    ProbeResult,
)


_TAIL_LIMIT = 4000


class LocalTaskHandle:
    """TaskHandle over a local asyncio.subprocess.Process."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        req: ExecutionRequest,
        log_fd: int | None,
        ref: ExecutionHandleRef,
    ) -> None:
        self._proc = proc
        self._req = req
        self._log_fd = log_fd
        self.ref = ref

    @property
    def os_pid(self) -> int | None:
        return self._proc.pid

    def poll(self) -> int | None:
        # asyncio sets returncode via the loop's child watcher; sync + no I/O.
        return self._proc.returncode

    async def wait(self) -> ExecutionResult:
        stdout_tail = ""
        stderr_tail = ""
        timed_out = False
        try:
            if self._req.capture_output:
                out, err = await self._await(self._proc.communicate())
                stdout_tail = _decode_tail(out)
                stderr_tail = _decode_tail(err)
                # Mirror captured output into the log for uniformity.
                self._req.log_path.write_text(
                    (stdout_tail + ("\n" + stderr_tail if stderr_tail else "")),
                    encoding="utf-8",
                )
            else:
                await self._await(self._proc.wait())
        except TimeoutError:
            timed_out = True
            self._proc.kill()
            await self._proc.wait()
        finally:
            self._close_log()
        return ExecutionResult(
            exit_code=None if timed_out else self._proc.returncode,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            output_log_path=self._req.log_path,
            timed_out=timed_out,
        )

    async def _await(self, coro):
        if self._req.timeout_seconds is not None:
            return await asyncio.wait_for(coro, timeout=self._req.timeout_seconds)
        return await coro

    async def terminate(self, grace_seconds: float) -> None:
        if self._proc.returncode is not None:
            return
        self._proc.terminate()
        await asyncio.sleep(grace_seconds)
        if self._proc.returncode is None:
            self._proc.kill()
        await self._proc.wait()
        self._close_log()

    async def kill(self) -> None:
        if self._proc.returncode is None:
            self._proc.kill()
        await self._proc.wait()
        self._close_log()

    async def collect(self) -> CollectResult:
        return CollectResult(applied=False, detail="local: no collect needed")

    async def cleanup(self) -> None:
        self._close_log()

    def _close_log(self) -> None:
        if self._log_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._log_fd)
            self._log_fd = None


def _decode_tail(data: bytes | None) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    return text[-_TAIL_LIMIT:]


class LocalBackend:
    """Runs an ExecutionRequest as a local asyncio subprocess."""

    id = "local"

    async def healthcheck(self) -> BackendHealth:
        return BackendHealth(reachable=True)

    async def can_run(self, req: ExecutionRequest) -> CapabilityResult:
        missing = [t for t in req.required_tools if shutil.which(t) is None]
        return CapabilityResult(ok=not missing, missing_tools=missing)

    async def run(self, req: ExecutionRequest) -> LocalTaskHandle:
        env = build_local_env(req)
        log_fd: int | None = None
        if req.capture_output:
            stdout = asyncio.subprocess.PIPE
            stderr = asyncio.subprocess.PIPE
        else:
            log_fd = os.open(str(req.log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            stdout = log_fd
            stderr = asyncio.subprocess.STDOUT
        try:
            proc = await asyncio.create_subprocess_exec(
                *req.argv,
                cwd=req.workdir,
                env=env,
                stdin=asyncio.subprocess.PIPE if req.stdin is not None else None,
                stdout=stdout,
                stderr=stderr,
            )
        except BaseException:
            if log_fd is not None:
                os.close(log_fd)
            raise
        if req.stdin is not None and proc.stdin is not None:
            proc.stdin.write(req.stdin.encode("utf-8"))
            proc.stdin.close()
        ref = ExecutionHandleRef(
            backend_id=self.id,
            run_id=req.run_id,
            transport_ref=f"local_pid:{proc.pid}",
            started_at=datetime.now(UTC),
        )
        return LocalTaskHandle(proc, req, log_fd, ref)

    async def probe(self, ref: ExecutionHandleRef) -> ProbeResult:
        if not ref.transport_ref.startswith("local_pid:"):
            return ProbeResult(alive=False, detail="not a local ref")
        pid = int(ref.transport_ref.split(":", 1)[1])
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ProbeResult(alive=False)
        except PermissionError:
            return ProbeResult(alive=True, detail="exists (EPERM)")
        return ProbeResult(alive=True)

"""Handle ownership remains observable when native cleanup fails."""

import pytest

from steam_agent.execution.windows_process import WindowsJobProcess


def test_failed_job_close_retains_ownership_for_retry() -> None:
    class Kernel:
        fail = True
        closed: list[int] = []

        def CloseHandle(self, handle: int) -> bool:
            self.closed.append(handle)
            return not (handle == 11 and self.fail)

    kernel = Kernel()
    process = WindowsJobProcess.__new__(WindowsJobProcess)
    process._kernel = kernel
    process._job, process._process = 11, 12
    with pytest.raises(OSError, match="closure unconfirmed"):
        process.close()
    assert (process._job, process._process) == (11, 12)
    kernel.fail = False
    process.close()
    assert (process._job, process._process) == (None, None)
    assert kernel.closed == [11, 11, 12]


def test_failed_process_close_preserves_remaining_handle() -> None:
    class Kernel:
        def CloseHandle(self, handle: int) -> bool:
            return handle == 11

    process = WindowsJobProcess.__new__(WindowsJobProcess)
    process._kernel = Kernel()
    process._job, process._process = 11, 12
    with pytest.raises(OSError, match="closure unconfirmed"):
        process.close()
    assert (process._job, process._process) == (None, 12)

"""Bounded pauses of the cyclic garbage collector around bulk allocations.

Decoding or validating tens of thousands of chunk records allocates hundreds of
thousands of container objects.  With a large heap alive (the processing and
indexing pipeline keeps several copies of the chunk data around) every
generation-0 threshold crossing then schedules young and, regularly, full
collections that traverse that whole heap, and none of this data forms
reference cycles.  Pausing the collector for the duration of such a bulk phase
removes that traversal cost without changing any result; the first allocation
after the pause runs one ordinary young-generation collection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import functools
import gc
from threading import Lock
from typing import Any, TypeVar


_Function = TypeVar("_Function", bound=Callable[..., Any])

_STATE_LOCK = Lock()
_pause_depth = 0
_restore_on_exit = False


@contextmanager
def gc_paused() -> Iterator[None]:
    """Disable the cyclic GC while the block runs, restoring the previous state.

    Reentrant and safe across threads: the collector is re-enabled only when
    the outermost active pause ends, and only if it was enabled when that pause
    began.  A caller that disabled the collector itself keeps it disabled.
    """

    global _pause_depth, _restore_on_exit
    with _STATE_LOCK:
        if _pause_depth == 0:
            _restore_on_exit = gc.isenabled()
            if _restore_on_exit:
                gc.disable()
        _pause_depth += 1
    try:
        yield
    finally:
        with _STATE_LOCK:
            _pause_depth -= 1
            if _pause_depth == 0 and _restore_on_exit:
                gc.enable()
                _restore_on_exit = False


def gc_paused_call(function: _Function) -> _Function:
    """Run a (non-generator) function inside :func:`gc_paused`."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with gc_paused():
            return function(*args, **kwargs)

    return wrapper  # type: ignore[return-value]

"""Locks shared by every process running the suite on this machine, so a
parallel run (./run_evals.sh --parallel N: N pytest-xdist workers) is graded
as fairly as a serial one. See plans/parallel-runs.md.

    timing()            held while a query is timed, its warm-up included:
                        no other query the suite sends runs meanwhile
    querying()          held by every other query the suite runs itself. Any
                        number run at once, but never during timing()
    superset_request()  one of SUPERSET_MAX_CONCURRENT slots (default 1), held
                        for each request to Superset, so no request waits in
                        Superset's own queue: Plugin Latency times the plugin,
                        not the queue. A request waits for its slot before its
                        clock starts
    judge_call()        one of JUDGE_MAX_CONCURRENT slots (default 4), held for
                        each judgement by a judge that runs as a program
                        on this machine (a_judge_call() in async code)

Plugin and judge calls don't take timing() or querying(): they go on while a
query is timed. Superset's development server, which the main stack runs
(`flask run --without-threads`), answers one request at a time, hence the
default of 1. Raise SUPERSET_MAX_CONCURRENT only for a Superset that answers
more at once (gunicorn's workers times its threads).

Each lock is an flock() on a file in EVAL_LOCK_DIR (default: the temp
folder), so a process that dies lets go of its locks. A thread holding
timing() runs its queries without querying(), which would otherwise wait on
the thread itself.

With EVAL_LOCK_LOG set to a folder, each process writes there when it held
timing(), querying() and superset_request(), and

    python evals/suite/locks.py results/latest_locks

checks that no query overlapped a timed one and that Superset never had more
requests from the suite at once than it has slots. ./run_evals.sh --parallel
does this after the run and adds the result to the report.
"""

import asyncio
import fcntl
import os
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO, Iterator, List, Optional, Tuple

# What this thread holds: its database lock ("timing", "querying" or None),
# and whether it has a Superset slot.
_held = threading.local()
_writing = threading.Lock()


def _lock_file(name: str) -> IO:
    folder = Path(os.getenv("EVAL_LOCK_DIR") or tempfile.gettempdir())
    return open(folder / f"llm-eval-suite-{name}.lock", "a")


def _record(kind: str, start: float, slots: Optional[int] = None) -> None:
    """Notes in EVAL_LOCK_LOG, if it is set, that this thread held `kind`
    from `start` until now. Called before the lock is let go, so the times
    logged are inside the time it was held."""
    folder = os.getenv("EVAL_LOCK_LOG")
    if not folder:
        return
    fields = [kind, str(os.getpid()), str(threading.get_ident()), f"{start:.6f}", f"{time.time():.6f}"]
    if slots is not None:
        fields.append(str(slots))
    with _writing:
        Path(folder).mkdir(parents=True, exist_ok=True)
        with open(Path(folder) / f"{os.getpid()}.log", "a") as log:
            log.write("\t".join(fields) + "\n")


@contextmanager
def _database(kind: str) -> Iterator[None]:
    held = getattr(_held, "database", None)
    if held == "timing" or held == kind:
        # This thread already holds it, and timing() covers querying().
        yield
        return
    if held:
        raise RuntimeError("timing() inside querying() would wait on this thread's own lock")
    with _lock_file("database") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if kind == "timing" else fcntl.LOCK_SH)
        _held.database = kind
        start = time.time()
        try:
            yield
        finally:
            _record(kind, start)
            _held.database = None
    # Closing the file let the lock go.


def timing():
    """Held while a query is timed: waits until no other query the suite
    sends is running, and keeps new ones waiting until it ends."""
    return _database("timing")


def querying():
    """Held while the suite runs any other query: never during timing()."""
    return _database("querying")


def _slots(setting: str, default: int) -> int:
    return max(int(os.getenv(setting) or default), 1)


def _free_slot(name: str, slots: int) -> Optional[IO]:
    """One of `slots` slot files, locked, or None when every one is taken."""
    for i in range(slots):
        slot = _lock_file(f"{name}-{i}")
        try:
            fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return slot
        except BlockingIOError:
            slot.close()
    return None


@contextmanager
def superset_request() -> Iterator[None]:
    """Held for one request to Superset: waits for one of
    SUPERSET_MAX_CONCURRENT slots. A thread that has one already (logging in
    again during a request) doesn't take a second."""
    if getattr(_held, "superset", False):
        yield
        return
    slots = _slots("SUPERSET_MAX_CONCURRENT", 1)
    while (slot := _free_slot("superset", slots)) is None:
        time.sleep(0.02)
    _held.superset = True
    start = time.time()
    try:
        yield
    finally:
        _record("superset", start, slots)
        _held.superset = False
        slot.close()


@contextmanager
def judge_call() -> Iterator[None]:
    """Held for one judgement by a judge that runs as a program on this
    machine: waits for one of
    JUDGE_MAX_CONCURRENT slots."""
    slots = _slots("JUDGE_MAX_CONCURRENT", 4)
    while (slot := _free_slot("judge", slots)) is None:
        time.sleep(0.5)
    with slot:
        yield


@asynccontextmanager
async def a_judge_call():
    """judge_call() for async code: waits without holding up the event loop."""
    slots = _slots("JUDGE_MAX_CONCURRENT", 4)
    while (slot := _free_slot("judge", slots)) is None:
        await asyncio.sleep(0.5)
    with slot:
        yield


###############################################
# Checking a run's lock log
###############################################

# (start, end, holder): the holder is a process and thread.
Span = Tuple[float, float, Tuple[str, str]]


def _most_at_once(spans: List[Span]) -> int:
    edges = sorted([(s, 1) for s, _, _ in spans] + [(e, -1) for _, e, _ in spans])
    most = now = 0
    for _, step in edges:  # at the same moment, an end (-1) sorts first
        now += step
        most = max(most, now)
    return most


def check(folder: str) -> Tuple[bool, str]:
    """(whether the locks worked, a line saying so) from the lock log in
    `folder`: no query from another thread overlapped a timed one, and no
    more requests went to Superset at once than it has slots."""
    timed: List[Span] = []
    queries: List[Span] = []
    requests: List[Span] = []
    slots = 1
    for path in sorted(Path(folder).glob("*.log")):
        for line in path.read_text().splitlines():
            kind, pid, thread, start, end, *rest = line.split("\t")
            span = (float(start), float(end), (pid, thread))
            if kind == "timing":
                timed.append(span)
            elif kind == "querying":
                queries.append(span)
            elif kind == "superset":
                requests.append(span)
                slots = max(slots, int(rest[0])) if rest else slots
    if not timed and not requests:
        return True, "Locks: nothing was logged."
    clashes = [
        t for t in timed
        if any(o[2] != t[2] and o[0] < t[1] and t[0] < o[1] for o in timed + queries)
    ]
    busiest = _most_at_once(requests)
    problems = []
    if clashes:
        first = datetime.fromtimestamp(min(t[0] for t in clashes)).strftime("%H:%M:%S")
        problems.append(
            f"{len(clashes)} of {len(timed)} timed queries overlapped another query "
            f"from the suite (the first at {first}), so their times may be slow"
        )
    if busiest > slots:
        problems.append(
            f"up to {busiest} requests went to Superset at once, more than its "
            f"{slots} (SUPERSET_MAX_CONCURRENT), so Plugin Latency may include queueing"
        )
    if problems:
        return False, "Locks: " + "; ".join(problems) + "."
    return True, (
        f"Locks: no other query ran while a query was timed ({len(timed)} timed), and "
        f"Superset never had more than {busiest} of the suite's requests at once "
        f"({slots} allowed)."
    )


if __name__ == "__main__":
    ok, text = check(sys.argv[1])
    print(text)
    sys.exit(0 if ok else 1)

import queue
import threading
import traceback
from typing import Callable

from .extractor import CancelToken

POLL_MS = 80
QUEUE_MAX = 1000
PUT_TIMEOUT = 0.2


class Job:
    """A cancellable background unit of work with a bounded event channel.

    The worker thread never touches a tkinter object -- not even
    StringVar.set() or after(). Everything crosses via this queue, which the
    main thread drains on a timer.
    """

    def __init__(self):
        self.q = queue.Queue(maxsize=QUEUE_MAX)
        self.cancel = CancelToken()
        self.thread = None
        self.finished = False

    def emit(self, kind, payload=None):
        """Enqueue an event, blocking in short slices so cancel still works.

        The bounded queue is backpressure: if the UI stalls, the worker waits
        rather than growing memory without limit.
        """
        while True:
            try:
                self.q.put((kind, payload), timeout=PUT_TIMEOUT)
                return
            except queue.Full:
                if self.cancel.cancelled:
                    return

    def drain(self, on_event, max_items=200):
        """Pull up to max_items events. Returns True while the job is live."""
        for _ in range(max_items):
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            on_event(kind, payload)
        return not (self.finished and self.q.empty())


def start_job(target: Callable[[Job], None], on_event: Callable[[str, object], None],
              root, interval_ms: int = POLL_MS) -> Job:
    """Run target(job) off-thread and dispatch its events on the main thread.

    root must be the Tk root: after() is only valid from the main thread.
    """
    job = Job()

    def runner():
        try:
            target(job)
        except BaseException:
            job.emit('error', traceback.format_exc())
        finally:
            job.finished = True
            job.emit('__end__', None)

    def poll():
        alive = job.drain(on_event)
        if alive:
            root.after(interval_ms, poll)

    job.thread = threading.Thread(target=runner, daemon=True)
    job.thread.start()
    root.after(interval_ms, poll)
    return job

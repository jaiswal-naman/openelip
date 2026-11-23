"""Worker composition root: loads every module's job handlers, then runs the scheduler, the queue and the relay.

Run:  python -m app.worker
"""

import os
import signal
import socket
import threading
from datetime import datetime, timezone
from types import FrameType

from app.brief import jobs as brief_jobs  # noqa: F401  (importing it registers the brief handlers)
from app.brief import privacy as brief_privacy  # noqa: F401  (the export job reads the registry in this process)
from app.ingest import jobs  # noqa: F401  (importing it registers the ingest handlers)
from app.ingest import privacy as ingest_privacy  # noqa: F401  (the export job reads the registry in this process)
from app.match import jobs as match_jobs  # noqa: F401  (importing it registers the match handlers)
from app.match import privacy as match_privacy  # noqa: F401  (the export job reads the registry in this process)
from app.platform import config, db, log, queue, relay, scheduler
from app.profile import jobs as profile_jobs
from app.profile import privacy as profile_privacy  # noqa: F401  (the export job reads the registry in this process)
from app.trust import jobs as trust_jobs  # noqa: F401  (importing it registers the export and erase jobs and the sweep)

POLL_SECONDS = 1


def main() -> None:
    stop = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    # Ctrl+C raises KeyboardInterrupt in the main thread on every platform; SIGTERM is what `docker stop` sends.
    signal.signal(signal.SIGTERM, request_stop)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    queues = config.get().worker_queues  # empty means every queue
    profile_jobs.register()
    log.event("worker.started")
    try:
        while not stop.is_set():
            ran = 0
            try:
                with db.transaction(role="worker") as conn:
                    scheduler.tick(conn, datetime.now(timezone.utc))
                ran = queue.run_once(lambda: db.transaction(role="worker"), worker_id, queues)
                with db.transaction(role="worker") as conn:
                    relay.relay_once(conn)
            except Exception as error:  # a database restart or a bad pass must not end the worker; log and go on
                log.event("worker.error", error=error)
            if not ran:
                stop.wait(POLL_SECONDS)  # a pass that ran a job goes straight on to the next one
    except KeyboardInterrupt:
        pass
    log.event("worker.stopped")


if __name__ == "__main__":
    main()

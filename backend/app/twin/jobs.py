"""The Twin's queue handler, `twin.resume` (ARCHITECTURE 5.9 rules 7, 8, 11). Importing this module is how the worker
registers it, and `app.twin.api` imports it for the job's name.

A job starts, resumes or continues the question's thread under its advisory lock (`guardian.runtime.drive`). A thread
that is busy raises ThreadBusy, so the queue retries the job on its first backoff. Causes: `start` (the question was
asked), `a{agent_action_id}.{status}` (the Guardian moved the action the thread waits on) and `reseat.{timestamp}`. A
question that already has its final message starts nothing: the job only asks for the thread's checkpoints to be
removed, so a repeated or late event can never make the Twin answer twice (INDEX decision 36).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import db, log, queue

RESUME = "twin.resume"


@queue.handler(RESUME)
def resume(conn: Connection, payload: dict[str, Any]) -> None:
    run_thread(UUID(payload["question_id"]), str(payload["cause"]))


def run_thread(question_id: UUID, cause: str) -> str:
    """Drive the question's thread for this cause: `settled` when it already has its final message (nothing runs),
    `gone` when the question no longer exists, else what `drive` did."""
    from app.guardian import runtime

    from . import deliver, graph

    with db.transaction(role="worker") as conn:
        owner = conn.execute(
            text("SELECT user_id FROM twin_messages WHERE id = :q AND author = 'employer'"), {"q": question_id}
        ).scalar_one_or_none()
        if owner is None:
            return "gone"
        if deliver.has_final(conn, question_id):
            deliver.enqueue_delete(conn, question_id, owner)
            return "settled"
    outcome = runtime.drive(
        deliver.thread_id(question_id), graph.compiled(), cause=cause, start_state=graph.start_state(question_id)
    )
    log.event("twin.thread", outcome=outcome)
    return outcome

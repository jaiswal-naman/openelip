"""Queue handlers of the profile module: profile.parse (a resume's text to unconfirmed facts, see parse_job.py)."""

from app.platform import queue

from . import parse_job
from .documents import PARSE_JOB


def register() -> None:
    """Called once by the worker at start-up, so the handlers exist before the first job is claimed. The attempt limit
    belongs to the job, not the handler: documents.store_document sets it when it queues the parse."""
    queue.handler(PARSE_JOB)(parse_job.parse)

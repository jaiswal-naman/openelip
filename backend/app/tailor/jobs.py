"""Queue handlers of the tailor module: `tailor.generate` builds one tailored resume (TLR-01; plan 05-05).

The worker imports this module once (plan 05-01), which registers the handler.
"""

from app.platform import queue

from . import service

queue.handler(service.JOB_KIND)(service.generate)

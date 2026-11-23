"""Queue handlers of the apply module: `apply.packet` drafts one application packet (APP-02; plan 05-05).

The worker imports this module once (plan 05-01), which registers the handler.
"""

from app.platform import queue

from . import packet

queue.handler(packet.JOB_KIND)(packet.draft)

"""Queue handlers of the brief module: brief.sweep, which every minute finds the people whose brief is due, and
brief.compose, which composes one person's brief for one local date.

None yet: plan 04-05 registers them here, with queue.handler and scheduler.every, and the worker already imports this
module, so they exist before the first job is claimed.
"""

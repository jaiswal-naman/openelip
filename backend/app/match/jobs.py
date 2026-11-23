"""Queue handlers of the match module: match.zones, match.plan, match.nightly, match.onboarding_sweep and match.run.

None yet: plan 04-03 registers them here, with queue.handler and scheduler.every, and the worker already imports this
module, so they exist before the first job is claimed.
"""

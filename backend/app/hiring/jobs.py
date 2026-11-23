"""Everything the worker must load for the hiring desk: the queue handlers of the ask pipeline and the mail templates of
the two email modules. The worker imports this module as hiring_jobs, so they are registered before the first job is
claimed (importing a module is how the handlers and the relay's resolvers get registered)."""

from . import ask_jobs, introduction_mail, org_mail  # noqa: F401

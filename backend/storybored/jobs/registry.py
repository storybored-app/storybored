"""Job handler registry.

Handlers self-register at import time:

    from storybored.jobs.registry import register

    @register("image_gen")
    async def image_gen(job, ctx):
        ...

Handlers are `async fn(job, ctx)`; `job` is the Job row, `ctx` is a
storybored.jobs.runner.JobContext (session factory, settings, events publisher,
update_progress helper, cooperative-cancel checks). The return value (dict or
None) is stored as the job's result_json.
"""

from collections.abc import Awaitable, Callable

Handler = Callable[..., Awaitable]

_HANDLERS: dict[str, Handler] = {}


def register(job_type: str) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        _HANDLERS[job_type] = fn
        return fn

    return decorator


def get_handler(job_type: str) -> Handler | None:
    return _HANDLERS.get(job_type)


def registered_types() -> list[str]:
    return sorted(_HANDLERS)

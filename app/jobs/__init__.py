"""Work that runs on a schedule rather than in response to a request.

A composition root beside `app/api`, not a bounded context: it owns no tables
and no rules, and exists to call services on a timer. `app.api` and `app.jobs`
must not import each other -- a route that needs to force a sweep should call
the same service the job calls.
"""

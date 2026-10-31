"""REST API package (specification §7, roadmap step 3).

Routers are mounted by `publisher.main.create_app`:
`/health` (unauthenticated) plus `/api/v1/posts` and `/api/v1/tasks`
(protected by the `X-API-Key` dependency).
"""

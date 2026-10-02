"""Todo export (#40): copy todos to the task system a user already works with.

``base.TodoSink`` is the interface, ``registry`` maps target types (``caldav``) to
implementations. ``service`` holds the per-user settings and the sync, ``tasks`` the jobs,
``router`` the API. docs/ARCHITECTURE.md §4.3 describes the flow.
"""

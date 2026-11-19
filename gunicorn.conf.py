"""Gunicorn hooks for optional PostgreSQL worker initialization.

SQLite remains the default. The hooks do nothing unless
CARD_SCANNER_DATABASE_BACKEND=postgres is explicitly configured.
"""


def post_fork(server, worker):
    import os

    backend = os.environ.get(
        "CARD_SCANNER_DATABASE_BACKEND", "sqlite"
    ).strip().lower()

    if backend != "postgres":
        return

    from postgres_app_startup import initialize_postgres_worker

    result = initialize_postgres_worker()
    server.log.info(
        "PostgreSQL worker initialized: database=%s schemas=%s migrations=%s",
        result["database"],
        result["schemas"],
        result["applied_migrations"],
    )


def worker_exit(server, worker):
    import os

    backend = os.environ.get(
        "CARD_SCANNER_DATABASE_BACKEND", "sqlite"
    ).strip().lower()

    if backend != "postgres":
        return

    from postgres_app_startup import shutdown_postgres_worker

    shutdown_postgres_worker()

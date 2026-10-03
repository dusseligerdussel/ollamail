"""Application entry point."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.admin.metrics import router as metrics_router
from app.admin.system import router as admin_system_router
from app.ai.llm import LLMGateway
from app.ai.settings.router import router as ai_settings_router
from app.ai.settings.router import status_router as ai_status_router
from app.ai.settings.runtime import build_resolver
from app.audit.router import router as audit_router
from app.auth.admin_router import router as auth_admin_router
from app.auth.csrf import CSRFMiddleware
from app.auth.invitations import router as invitations_router
from app.auth.mfa.router import router as mfa_router
from app.auth.providers import AuthProviderRegistry, github, oidc, saml
from app.auth.providers.ldap.router import login_router as ldap_login_router
from app.auth.providers.ldap.router import router as ldap_router
from app.auth.reauth import router as reauth_router
from app.auth.router import router as auth_router
from app.auth.router import setup_router
from app.auth.setup import log_setup_status
from app.core.config import Settings, get_settings
from app.core.crypto import configure_keyring
from app.core.db import Database, bind_process_database
from app.core.errors import install_error_handlers
from app.core.events import EventBroker
from app.core.events import router as events_router
from app.core.health import ReadinessRegistry, register_readiness_check
from app.core.health import router as health_router
from app.core.jobs import JobQueue
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.openapi import generate_operation_id
from app.digest.router import feed_router as digest_feed_router
from app.digest.router import router as digests_router
from app.drafts.router import router as drafts_router
from app.mail.api.messages import providers_router as mailbox_providers_router
from app.mail.api.messages import router as messages_router
from app.mail.api.router import router as mailboxes_router
from app.mail.api.shared import router as shared_mailboxes_router
from app.mail.providers.gmail_connect import router as gmail_connect_router
from app.mail.providers.graph_router import NOTIFICATIONS_PATH
from app.mail.providers.graph_router import router as graph_router
from app.privacy.router import admin_router as privacy_admin_router
from app.privacy.router import router as privacy_router
from app.rag.router import router as rag_router
from app.scim.admin_router import router as scim_admin_router
from app.scim.router import CSRF_EXEMPT_PREFIX as SCIM_PATH_PREFIX
from app.scim.router import router as scim_router
from app.search.router import router as search_router
from app.todos.export.gtasks_connect import router as gtasks_connect_router
from app.todos.export.mstodo_router import router as mstodo_router
from app.todos.export.router import router as todo_export_router
from app.todos.router import router as todos_router
from app.triage.router import router as triage_router
from app.users.router import router as users_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings.logging)
    database = Database(settings.database)
    # AI settings from the database (admin page), refreshed on change (app/ai/settings).
    ai_resolver = build_resolver(settings, database)
    llm = LLMGateway(ai_resolver)
    events = EventBroker(settings.database)
    job_queue = JobQueue()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Refuse to start without a valid OLLAMAIL_SECRET_KEY.
        configure_keyring(settings.security)
        await log_setup_status(database, settings)
        ai_resolver.start()
        pull = None
        if settings.llm.pull_missing_models:
            pull = asyncio.create_task(llm.pull_missing_models())
        # Code shared with the worker (``process_database``) uses the api's engine.
        with bind_process_database(database):
            yield
        if pull is not None:
            pull.cancel()
            with suppress(asyncio.CancelledError):
                await pull
        await llm.aclose()
        await ai_resolver.aclose()
        await events.stop()
        await job_queue.close()
        await database.dispose()

    app = FastAPI(
        title="ollamail",
        lifespan=lifespan,
        generate_unique_id_function=generate_operation_id,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.events = events
    app.state.job_queue = job_queue
    app.state.readiness = ReadinessRegistry()
    register_readiness_check(app, "database", database.ping)
    app.state.llm = llm
    app.state.ai_resolver = ai_resolver
    if settings.llm.readiness_check:
        register_readiness_check(app, "llm", llm.check_ready)

    app.state.auth_providers = AuthProviderRegistry()
    # Provider types the admin UI can configure.
    app.state.idp_kinds = {"oidc", "ldap", "github", "saml"}

    install_error_handlers(app)
    # Added first, so it runs inside RequestContextMiddleware (403s carry a request ID).
    # SAML IdPs post the response cross-site to the ACS; it is protected by the flow
    # cookie, InResponseTo and the response signature instead.
    app.add_middleware(
        CSRFMiddleware,
        settings=settings,
        exempt_paths=[NOTIFICATIONS_PATH, SCIM_PATH_PREFIX, saml.ACS_PATH],
    )
    app.add_middleware(RequestContextMiddleware)
    # Outermost: everything inside sees the real client IP. Replaces uvicorn's own proxy
    # header handling (started with --no-proxy-headers, see backend/Dockerfile).
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.security.forwarded_allow_ips)
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(events_router)
    app.include_router(setup_router)
    app.include_router(auth_router)
    app.include_router(mfa_router)
    app.include_router(reauth_router)
    app.include_router(ldap_login_router)
    app.include_router(ldap_router)
    app.include_router(users_router)
    app.include_router(auth_admin_router)
    app.include_router(invitations_router)
    app.include_router(todos_router)
    app.include_router(todo_export_router)
    app.include_router(mstodo_router)
    app.include_router(gtasks_connect_router)
    app.include_router(triage_router)
    app.include_router(audit_router)
    app.include_router(ai_settings_router)
    app.include_router(ai_status_router)
    app.include_router(admin_system_router)
    # Before the mailbox router: ``/mailboxes/providers`` must not match ``/{mailbox_id}``.
    app.include_router(mailbox_providers_router)
    app.include_router(mailboxes_router)
    app.include_router(shared_mailboxes_router)
    app.include_router(messages_router)
    app.include_router(gmail_connect_router)
    app.include_router(graph_router)
    app.include_router(digests_router)
    app.include_router(digest_feed_router)
    app.include_router(rag_router)
    app.include_router(drafts_router)
    app.include_router(search_router)
    app.include_router(privacy_router)
    app.include_router(privacy_admin_router)
    app.include_router(scim_router)
    app.include_router(scim_admin_router)
    oidc.install(app, settings)
    github.install(app)
    saml.install(app)
    return app


app = create_app()

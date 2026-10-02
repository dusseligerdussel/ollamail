"""Import every module that defines ORM models so ``Base.metadata`` is complete.

Alembic autogenerate relies on this. Add one import line per new models module.
"""

from app.audit import models as audit_models
from app.auth import models as auth_models
from app.auth.providers.github import models as github_models
from app.auth.providers.ldap import models as ldap_models
from app.auth.providers.oidc import models as oidc_models
from app.core.db import Base
from app.digest import models as digest_models
from app.mail import models as mail_models
from app.processing import models as processing_models
from app.rag import models as rag_models
from app.search import models as search_models
from app.todos import models as todo_models
from app.triage import models as triage_models
from app.users import models as user_models

__all__ = [
    "Base",
    "audit_models",
    "auth_models",
    "digest_models",
    "github_models",
    "ldap_models",
    "mail_models",
    "oidc_models",
    "processing_models",
    "rag_models",
    "search_models",
    "todo_models",
    "triage_models",
    "user_models",
]

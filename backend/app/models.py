"""Import every module that defines ORM models so ``Base.metadata`` is complete.

Alembic autogenerate relies on this. Add one import line per new models module.
"""

from app.audit import models as audit_models
from app.auth import models as auth_models
from app.core.db import Base
from app.mail import models as mail_models
from app.processing import models as processing_models
from app.users import models as user_models

__all__ = ["Base", "audit_models", "auth_models", "mail_models", "processing_models", "user_models"]

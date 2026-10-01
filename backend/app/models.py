"""Import every module that defines ORM models so ``Base.metadata`` is complete.

Alembic autogenerate relies on this. Add one import line per new models module.
"""

from app.core.db import Base
from app.mail import models as mail_models

__all__ = ["Base", "mail_models"]

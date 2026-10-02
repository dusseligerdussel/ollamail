"""Import every module that defines ORM models so ``Base.metadata`` is complete.

Alembic autogenerate relies on this. Add one import line per new models module.
"""

from app.auth import models as auth_models
from app.core.db import Base
from app.mail import models as mail_models
from app.processing import models as processing_models
from app.todos import models as todo_models
from app.users import models as user_models

__all__ = [
    "Base",
    "auth_models",
    "mail_models",
    "processing_models",
    "todo_models",
    "user_models",
]

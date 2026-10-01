"""Import every module that defines ORM models so ``Base.metadata`` is complete.

Alembic autogenerate relies on this. Add one import line per new models module.
"""

from app.core.db import Base

__all__ = ["Base"]

"""Mail provider interface and registry (docs/ARCHITECTURE.md §3.1)."""

from app.mail.providers.base import MailboxConfig, MailProvider
from app.mail.providers.registry import ProviderRegistry, registry

__all__ = ["MailProvider", "MailboxConfig", "ProviderRegistry", "registry"]

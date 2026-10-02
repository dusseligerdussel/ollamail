"""Mail provider interface and registry (docs/ARCHITECTURE.md §3.1)."""

# Built-in providers register themselves on import.
from app.mail.providers import imap
from app.mail.providers.base import MailboxConfig, MailProvider
from app.mail.providers.registry import ProviderRegistry, registry

__all__ = ["MailProvider", "MailboxConfig", "ProviderRegistry", "imap", "registry"]

"""Errors of the GitHub login. Codes are static, safe to log and shown on the login page."""

import enum


class GitHubErrorCode(enum.StrEnum):
    # GitHub returned an error to the callback (e.g. the user cancelled).
    IDP_ERROR = "idp_error"
    # The code exchange failed.
    TOKEN_EXCHANGE_FAILED = "token_exchange_failed"
    # The GitHub API was unreachable or answered unexpectedly.
    API_FAILED = "provider_unavailable"
    # The user is not a member of an allowed organization or team.
    NOT_MEMBER = "not_member"


class GitHubError(Exception):
    def __init__(self, code: GitHubErrorCode, reason: str | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        # Static detail for the log (which step failed); never API data.
        self.reason = reason or code.value

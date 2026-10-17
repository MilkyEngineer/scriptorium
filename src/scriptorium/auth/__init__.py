"""OAuth 2.1 resource-server side of Scriptorium (Architecture §5).

Scriptorium never issues tokens: it validates JWTs from the configured issuer
(:mod:`.oidc`), serves the protected-resource metadata and the 401/403 contract
(:mod:`.metadata`), and binds the resulting user to the request (:mod:`.context`).
"""

from scriptorium.auth.context import NoUserContextError, current_user, user_context
from scriptorium.auth.oidc import (
    AuthError,
    DiscoveryError,
    InsufficientScope,
    OIDCValidator,
    TokenExpired,
    TokenInvalid,
    User,
    UserNotAllowed,
)

__all__ = [
    "AuthError",
    "DiscoveryError",
    "InsufficientScope",
    "NoUserContextError",
    "OIDCValidator",
    "TokenExpired",
    "TokenInvalid",
    "User",
    "UserNotAllowed",
    "current_user",
    "user_context",
]

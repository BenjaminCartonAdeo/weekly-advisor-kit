"""npm package identity normalization — leaf module (stdlib only).

Isolé de ``watch_context`` parce que ``watch_distill`` en a besoin : garder
l'identité npm ici d'un côté et l'inventaire de l'autre imposait un import différé
(cycle : ``watch_distill`` → ``watch_context`` → ``watch_distill``). Ce module
n'importe que ``re`` : il reste une feuille du paquet, doncaucun cycle n'est
possible par construction.
"""

from __future__ import annotations

import re

_NPM_PACKAGE_RE = re.compile(
    r"^(?:@[a-z0-9._~-]+/)?[a-z0-9._~-]+$",
    re.IGNORECASE,
)


def normalize_npm_package(value: str | None) -> str | None:
    """Return a normalized package identity without its version/specifier.

    ``@scope/name@latest`` and ``@scope/name@1.2.3`` therefore both normalize
    to ``@scope/name``.  Git, file, and URL specifications do not themselves
    identify an npm package and return ``None``.  Package names are compared
    case-insensitively because npm package identities are effectively
    lower-case, while the returned value remains a normal package identity.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower().startswith("npm:"):
        text = text[4:].strip()
    if not text or text.startswith((".", "/", "~", "git+", "git:", "ssh:")):
        return None
    if text.lower().startswith(
        ("http://", "https://", "ssh://", "git://", "github:", "gitlab:", "bitbucket:")
    ):
        return None

    if text.startswith("@"):
        slash = text.find("/")
        if slash <= 1:
            return None
        separator = text.find("@", slash + 1)
    else:
        separator = text.find("@")
    identity = text if separator < 0 else text[:separator]
    identity = identity.strip()
    if not _NPM_PACKAGE_RE.fullmatch(identity):
        return None
    return identity.casefold()


def _package_spec_parts(value: str) -> tuple[str, str | None]:
    """Split a package spec into its package portion and optional suffix."""

    text = value.strip()
    if text.lower().startswith("npm:"):
        text = text[4:].strip()
    if text.startswith("@"):
        slash = text.find("/")
        separator = text.find("@", slash + 1) if slash >= 0 else -1
    else:
        separator = text.find("@")
    if separator < 0:
        return text, None
    return text[:separator], text[separator + 1 :].strip() or None

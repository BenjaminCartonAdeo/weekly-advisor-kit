"""Registre explicite des providers de sessions — table fail-soft.

Les trois modules de `providers/implementations/` y sont câblés
explicitement : chacun expose `PROVIDER_TYPE: str` et une factory
`build_provider(source_cfg, cfg) -> SessionProvider | None`. Type inconnu ou
source indisponible (factory → None) → avertissement + skip, jamais de crash.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING

from .base import SessionProvider, validate_provider
from .implementations import claude_code, copilot_vscode, opencode

if TYPE_CHECKING:
    from ..config import TelemetryConfig

#: Factory d'un provider : (entrée session_sources, config globale) → provider.
#: None = source indisponible sur cette machine (harnais absent, base absente…).
ProviderFactory = Callable[[dict, "TelemetryConfig"], SessionProvider | None]

#: Table explicite : aucun scan dynamique, comportement stable et lisible.
_BUILTIN_FACTORIES: dict[str, ProviderFactory] = {
    opencode.PROVIDER_TYPE: opencode.build_provider,
    claude_code.PROVIDER_TYPE: claude_code.build_provider,
    copilot_vscode.PROVIDER_TYPE: copilot_vscode.build_provider,
}


def discover_provider_factories() -> dict[str, ProviderFactory]:
    """Retourne une copie de la table explicite des trois factories connues."""
    return dict(_BUILTIN_FACTORIES)


def build_providers(
    cfg: TelemetryConfig, *, factories: dict[str, ProviderFactory] | None = None
) -> list[SessionProvider]:
    """Construit les providers des sources actives de `cfg.session_sources`.

    Fail-soft : entrée désactivée ("enabled": false) → silence ; type inconnu,
    source indisponible (factory → None), échec d'initialisation ou provider
    non conforme au contrat (`validate_provider`) → UserWarning + skip. Ne
    lève jamais pour une source individuelle.
    """
    if factories is None:
        factories = discover_provider_factories()
    providers: list[SessionProvider] = []
    for source in cfg.session_sources:
        stype = source.get("type") if isinstance(source, dict) else None
        if isinstance(source, dict) and source.get("enabled", True) is False:
            continue
        factory = factories.get(stype) if isinstance(stype, str) else None
        if factory is None:
            warnings.warn(f"source de sessions ignorée : type inconnu {stype!r}", stacklevel=2)
            continue
        try:
            provider = factory(source, cfg)
        except Exception as exc:  # fail-soft — une source ne casse jamais le run
            warnings.warn(
                f"source de sessions {stype!r} ignorée : échec d'initialisation ({exc})",
                stacklevel=2,
            )
            continue
        if provider is None:
            warnings.warn(f"source de sessions {stype!r} indisponible — ignorée", stacklevel=2)
            continue
        deviations = validate_provider(provider)
        if deviations:
            warnings.warn(
                f"source de sessions {stype!r} ignorée : provider non conforme au contrat "
                f"({'; '.join(deviations)})",
                stacklevel=2,
            )
            continue
        providers.append(provider)
    return providers

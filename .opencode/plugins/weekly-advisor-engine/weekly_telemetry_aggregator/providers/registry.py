"""Registre des providers de sessions — auto-découverte fail-soft.

Scan pkgutil de `providers/implementations/` : chaque module y expose
`PROVIDER_TYPE: str` et une factory `build_provider(source_cfg, cfg) ->
SessionProvider | None`. Type inconnu ou source indisponible (factory → None)
→ avertissement + skip, jamais de crash.
"""

from __future__ import annotations

import importlib
import pkgutil
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING

from .base import SessionProvider

if TYPE_CHECKING:
    from ..config import TelemetryConfig

#: Factory d'un provider : (entrée session_sources, config globale) → provider.
#: None = source indisponible sur cette machine (harnais absent, base absente…).
ProviderFactory = Callable[[dict, "TelemetryConfig"], SessionProvider | None]


def discover_provider_factories() -> dict[str, ProviderFactory]:
    """Découvre les factories des modules de `providers/implementations/`.

    Modules sans couple valide (`PROVIDER_TYPE` str + `build_provider`
    appelable) ignorés ; type dupliqué → avertissement, premier déclaré gagne
    (ordre stable de `iter_modules`).
    """
    from . import implementations

    factories: dict[str, ProviderFactory] = {}
    for mod_info in pkgutil.iter_modules(implementations.__path__):
        if mod_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{implementations.__name__}.{mod_info.name}")
        ptype = getattr(module, "PROVIDER_TYPE", None)
        factory = getattr(module, "build_provider", None)
        if not isinstance(ptype, str) or not callable(factory):
            continue
        if ptype in factories:
            warnings.warn(
                f"PROVIDER_TYPE dupliqué {ptype!r} dans {mod_info.name} — "
                "première factory conservée",
                stacklevel=2,
            )
            continue
        factories[ptype] = factory
    return factories


def build_providers(
    cfg: TelemetryConfig, *, factories: dict[str, ProviderFactory] | None = None
) -> list[SessionProvider]:
    """Construit les providers des sources actives de `cfg.session_sources`.

    Fail-soft : entrée désactivée ("enabled": false) → silence ; type inconnu,
    source indisponible (factory → None) ou échec d'initialisation →
    UserWarning + skip. Ne lève jamais pour une source individuelle.
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
            warnings.warn(
                f"source de sessions ignorée : type inconnu {stype!r}", stacklevel=2
            )
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
            warnings.warn(
                f"source de sessions {stype!r} indisponible — ignorée", stacklevel=2
            )
            continue
        providers.append(provider)
    return providers

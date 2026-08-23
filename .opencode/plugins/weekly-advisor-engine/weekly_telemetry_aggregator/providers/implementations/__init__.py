"""Implémentations de `SessionProvider` — une par harnais télémétrique.

Chaque module expose `PROVIDER_TYPE: str` et
`build_provider(source_cfg, cfg) -> SessionProvider | None`
(découverts automatiquement par `providers.registry`).
"""

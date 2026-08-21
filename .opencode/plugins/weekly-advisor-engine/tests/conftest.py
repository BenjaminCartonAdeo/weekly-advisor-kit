"""Hermèmetique de la suite : aucun test n'ouvre de navigateur (v6.1.a).

`WEEKLY_NO_BROWSER=1` est forcé pour chaque test et `webbrowser.open` est remplacé
par un stub qui échoue bruyamment : tout contournement futur devient un échec de
test visible au lieu d'ouvrir des onglets sur la machine de développement.
Les tests qui exercent volontairement l'ouverture réécrivent ce stub (monkeypatch)
ou suppriment la variable d'environnement dans leur propre portée.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    monkeypatch.setenv("WEEKLY_NO_BROWSER", "1")

    def _forbidden(uri):  # pragma: no cover — déclenché seulement en cas de contournement
        raise AssertionError(
            f"un test tente d'ouvrir le navigateur ({uri!r}) — "
            "réécris webbrowser.open explicitement dans le test"
        )

    monkeypatch.setattr("webbrowser.open", _forbidden)

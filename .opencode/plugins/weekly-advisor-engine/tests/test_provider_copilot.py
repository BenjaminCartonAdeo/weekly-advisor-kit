"""Shim CLI-only — évite duplication 498L identique à test_provider_copilot_cli.py."""

# ponytail: delete duplicate suite, re-export thin shim
from tests.test_provider_copilot_cli import *  # noqa: F401,F403

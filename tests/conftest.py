"""Shared pytest fixtures."""

import copy
from typing import Any

import pytest
import yaml

from coldchain.config import DEFAULT_CONFIG_PATH


@pytest.fixture
def raw_config() -> dict[str, Any]:
    """A fresh, mutable copy of the default config as a plain dict."""
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(data)

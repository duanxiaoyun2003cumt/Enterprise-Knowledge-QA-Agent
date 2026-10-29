"""Environment-based configuration for the model client.

Only ``LLM_TIMEOUT`` has a default. The API key, base URL, and model name must
be supplied explicitly so a missing value never silently turns into a fake
"working" configuration.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_TIMEOUT_SECONDS = 30.0

_REQUIRED_VARIABLES = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")


class ConfigurationError(RuntimeError):
    """Raised when required model configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Validated model connection settings.

    ``api_key`` uses ``repr=False`` so accidental printing of the settings
    object cannot leak the credential.
    """

    api_key: str = field(repr=False)
    base_url: str
    model: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Load settings from ``.env`` and the process environment.

        An explicitly provided mapping (useful in tests) takes precedence over
        the process environment. ``.env`` never overrides variables that are
        already exported in the shell.
        """
        load_dotenv(ENV_FILE, override=False)
        source = os.environ if env is None else env

        missing = [
            name
            for name in _REQUIRED_VARIABLES
            if not (source.get(name) or "").strip()
        ]
        if missing:
            raise ConfigurationError(
                "Missing required environment variable(s): "
                f"{', '.join(missing)}. Copy .env.example to .env and fill in "
                "real values, or export the variables in your shell. "
                "Never commit real credentials."
            )

        base_url = source["LLM_BASE_URL"].strip().rstrip("/")
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ConfigurationError(
                "LLM_BASE_URL must be an absolute http:// or https:// URL, "
                "for example https://your-provider.example.com/v1."
            )

        raw_timeout = (source.get("LLM_TIMEOUT") or "").strip()
        if raw_timeout:
            try:
                timeout = float(raw_timeout)
            except ValueError as exc:
                raise ConfigurationError(
                    f"LLM_TIMEOUT must be a number of seconds; got {raw_timeout!r}."
                ) from exc
            if not math.isfinite(timeout) or timeout <= 0:
                raise ConfigurationError(
                    "LLM_TIMEOUT must be a finite number greater than 0 seconds."
                )
        else:
            timeout = DEFAULT_TIMEOUT_SECONDS

        return cls(
            api_key=source["LLM_API_KEY"].strip(),
            base_url=base_url,
            model=source["LLM_MODEL"].strip(),
            timeout=timeout,
        )

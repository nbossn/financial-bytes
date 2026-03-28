"""HTTP client for the massive.com REST API."""
import httpx
from loguru import logger

from src.config import settings


class MassiveAPIError(Exception):
    pass


class MassiveClient:
    def __init__(self):
        self._client = httpx.Client(
            base_url=settings.massive_base_url,
            headers={
                "Authorization": f"Bearer {settings.massive_api_key}",
                "Accept": "application/json",
                "User-Agent": "financial-bytes/0.1.0",
            },
            timeout=30.0,
        )

    def get(self, path: str, params: dict | None = None) -> dict:
        """Make authenticated GET request, return parsed JSON."""
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"massive.com API error {e.response.status_code}: {path} — {e.response.text[:200]}")
            raise MassiveAPIError(f"HTTP {e.response.status_code}: {path}") from e
        except httpx.RequestError as e:
            logger.error(f"massive.com request failed: {path} — {e}")
            raise MassiveAPIError(f"Request failed: {path}") from e

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

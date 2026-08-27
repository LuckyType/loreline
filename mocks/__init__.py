"""Mock provider servers for tests and local development.

These FastAPI apps imitate external/self-hosted STT provider HTTP/WS contracts
per their public docs, so connectors can be exercised without real credentials
or network access.
"""

from __future__ import annotations

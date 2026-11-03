"""ASGI entry point: `uvicorn opsassist.asgi:app`."""

from opsassist.api.app import create_app

app = create_app()

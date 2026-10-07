"""Errors shared by import, catalogue and storage operations."""

from pathlib import Path


class CatalogueError(ValueError):
    """Invalid input or an unsafe catalogue operation."""

    def __init__(
        self, message: str, *, path: str | Path | None = None,
        hint: str | None = None, impact: str | None = None,
    ) -> None:
        self.message = message
        self.path = Path(path) if path is not None else None
        self.hint, self.impact = hint, impact
        # Keep the full diagnostic path in durable journals; CLI renders it separately.
        super().__init__(f"{message}: {path}" if path is not None else message)

class SourceError(CatalogueError):
    """Rejected source content; may be skipped without weakening I/O or destination guards."""

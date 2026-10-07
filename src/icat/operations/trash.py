"""Apply the requested trash action before staging or metadata requests."""

import logging

from ..catalogue.codec import encode
from ..catalogue.trash import prepare_trash
from .state import Session
from . import publication
from ..io import paths as layout


logger = logging.getLogger(__name__)


def apply_trash(session: Session) -> None:
    options, progress, operation = session.options, session.progress, session.operation
    dst, catalogue_path = session.dst, session.catalogue_path
    entries, original, preferences = session.entries, session.original, session.preferences
    paths = session.inventory.candidates

    trash_entries = [entry for entry in entries if options.trash and entry["hash"] in preferences.trash]
    journal = operation.data
    journal.update({
        "destination": f"{dst}",
        "trash": [{"hash": entry["hash"], "image": entry["image"]} for entry in trash_entries],
        "trash_reset": options.trash_reset,
        "initial_games": len(entries), "candidate_sources": len(paths),
    })
    record = operation.record
    record("prepared")

    if options.trash:
        progress.start("trash")
        retained = [entry for entry in entries if entry["hash"] not in preferences.trash]
        # Validate the replacement index before the first destructive action.
        cleaned = encode(retained) if trash_entries else None
        removals = prepare_trash(dst, entries, preferences.trash)
        progress.set_total(2 * len(removals) + 1)

        for item in removals:
            progress.set_item(f"{layout.quote_path(item.path.relative_to(dst))} — checking")
            item.verify()
            progress.advance()

        preferences.check_unchanged()
        publication.check_catalogue(catalogue_path, original)
        record("removing_trash")

        for item in removals:
            progress.set_item(f"{layout.quote_path(item.path.relative_to(dst))} — removing")
            preferences.check_unchanged()
            publication.check_catalogue(catalogue_path, original)

            with operation.track_removal(item.path, collection="trash_removed", digest=item.digest):
                item.remove()

            progress.advance()

        if cleaned is not None:
            # Keep the old paths until every unlink is durable, so an interrupted
            # cleanup can be retried with --trash even when some ROMs are missing.
            preferences.check_unchanged()
            publication.publish_catalogue(catalogue_path, cleaned, original=original)
            entries, original = retained, cleaned
            progress.set_counts(catalogue_entries=len(entries))

        record("trash_complete")
        progress.advance()
        logger.info("Trash cleanup: %d games removed; preferences retained", len(trash_entries))

    elif options.trash_reset:
        progress.start("trash", total=1)
        preferences.reset_trash()
        record("trash_reset")
        progress.advance()
        logger.info("Trash preferences reset; no game files removed")

    session.entries, session.original = entries, original
    session.trash_entries = trash_entries

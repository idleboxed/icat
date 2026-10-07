"""Command line, logging setup and exit codes only."""

import argparse
import logging
import math
import os
import sys
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

from . import __version__
from .errors import CatalogueError
from .reporting.diagnostics import Diagnostics
from .metadata.provider import Provider, build_provider
from .io.http import HttpClient
from .roms.platforms import PLATFORMS
from .metadata.sources import SOURCE_TYPES
from .operations.options import Options
from .operations.session import run
from .reporting.terminal import MessageFormatter, PlainProgressHandler, RichProgressHandler, should_use_rich
from .io import paths


logger = logging.getLogger(__name__)


def parse_concurrency(value: str) -> int:
    # Argparse would otherwise expose the renamed callback in CLI diagnostics.
    try:
        parsed = int(value)

    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}") from None

    if not 1 <= parsed <= 32:
        raise argparse.ArgumentTypeError("must be between 1 and 32")

    return parsed


def parse_retry_count(value: str) -> int:
    try:
        parsed = int(value)

    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid retry_count value: {value!r}") from None

    if not 0 <= parsed <= 5:
        raise argparse.ArgumentTypeError("must be between 0 and 5")

    return parsed


def create_seconds_parser(minimum: float, maximum: float, *, positive: bool = False) -> Callable[[str], float]:
    def parse(value: str) -> float:
        parsed = float(value)

        if not math.isfinite(parsed) or not minimum <= parsed <= maximum or (positive and parsed == 0):
            lower = "> 0" if positive else f">= {minimum:g}"
            raise argparse.ArgumentTypeError(f"must be finite, {lower} and <= {maximum:g} seconds")

        return parsed

    return parse


def parse_platform_list(value: str) -> tuple[str, ...]:
    platforms = tuple(value.split(","))
    unknown = [platform for platform in platforms if platform not in PLATFORMS]

    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown platform: {unknown[0] or "<empty>"}; available: {", ".join(PLATFORMS)}"
        )

    return platforms


@contextmanager
def configure_cli_logging(
    mode: str = "auto", *, diagnostics: Diagnostics | None = None, destination: Path | None = None,
) -> Iterator[None]:
    package = logging.getLogger("icat")
    handler = None
    old_level = package.level

    if not package.hasHandlers():
        handler = (
            RichProgressHandler(sys.stderr, diagnostics or Diagnostics(), destination=destination)
            if should_use_rich(mode, sys.stderr, os.environ)
            else PlainProgressHandler(sys.stderr, destination=destination)
        )
        package.addHandler(handler)
        package.setLevel(logging.INFO)

    try:

        with handler.live if isinstance(handler, RichProgressHandler) else nullcontext():
            try:
                yield

            finally:

                if handler is not None:
                    handler.flush_repeats()

    finally:

        if handler:
            package.removeHandler(handler)
            handler.close()
            package.setLevel(old_level)


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(
        src=None, move_roms=False, trash=False, trash_reset=False, trash_only=False,
        fix_nes_headers=True, platforms=None, concurrency=5,
    )
    locations = parser.add_argument_group("Locations")
    locations.add_argument(
        "--dst", type=Path, default=paths.GAMES_DIR, help="IGUI games directory (default: %(default)s)",
    )
    locations.add_argument(
        "--cache", type=Path, default=paths.get_default_cache_dir(),
        help="PC-only HTTP and database cache, outside src/dst (default: %(default)s)",
    )
    locations.add_argument(
        "--logs_dir", type=Path, default=paths.LOGS_DIR,
        help="Root for per-command logs (default: %(default)s in the current directory)",
    )
    output = parser.add_argument_group("Display")
    output.add_argument(
        "--progress", choices=("auto", "rich", "plain"), default="auto",
        help="Progress display: Rich in a terminal, plain text in redirected output (default: auto)",
    )


def add_metadata_options(parser: argparse.ArgumentParser) -> None:
    metadata = parser.add_argument_group("Metadata")
    metadata.add_argument(
        "--refresh-db", action="store_true", help="Update persistent file databases; keep old on failure",
    )
    metadata.add_argument(
        "--sources", nargs="+", choices=[source.name for source in SOURCE_TYPES],
        help="Sources in priority order (default: all built-in sources; keyed sources need their own key)",
    )
    http = parser.add_argument_group("HTTP options")
    http.add_argument(
        "--http-concurrency", dest="concurrency", type=parse_concurrency, default=5,
        help="Maximum concurrent HTTP requests (default: 5)",
    )
    http.add_argument(
        "--http-timeout", dest="timeout", type=create_seconds_parser(0, 300, positive=True), default=20,
        help="HTTP connect/read timeout in seconds (default: 20)",
    )
    http.add_argument(
        "--http-retries", dest="retries", type=parse_retry_count, default=2,
        help="Retries for transient GET failures, 0..5 (default: 2)",
    )
    http.add_argument(
        "--http-max-wait", dest="max_wait", type=create_seconds_parser(0, 3600), default=180,
        help="Maximum scheduling/rate-limit wait budget per GET, seconds (default: 180)",
    )
    http.add_argument(
        "--http-offline", dest="offline", action="store_true", help="Use cached metadata only; never use the network",
    )
    http.add_argument(
        "--http-refresh", dest="refresh", action="store_true",
        help="Refresh cached online metadata (preserves existing fields)",
    )


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="icat", description="Prepare IGUI catalogues on the PC. No device probing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n  icat sync --src roms --dst games\n"
            "  icat metadata --dst games\n  icat trash clean --dst games"
        ),
    )
    parser.add_argument("--version", action="version", version=f"icat {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser(
        "sync", help="Add ROMs and fill missing metadata; COPY by default, not a mirror",
        description="Add games without mirroring the source. Preserve existing games and filled metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n  icat sync --src roms --dst games\n"
            "  icat sync --platform NES,FDS\n  icat sync --http-offline\n"
            "  icat sync --move-roms\n\n"
            "MOVE removes an entire accepted archive, including unselected files.\n"
            "For metadata or trash only, use: icat metadata / icat trash.\n"
            "Exit codes: 0 complete; 1 stopped; 2 invalid arguments; 3 rejected sources; 130 cancelled."
        ),
    )
    add_common_options(sync)
    inputs = sync.add_argument_group("Import")
    inputs.add_argument("--src", type=Path, default=paths.SOURCE_DIR, help="Source directory (default: %(default)s)")
    inputs.add_argument(
        "--platform", dest="platforms", action="append", type=parse_platform_list, metavar="PLATFORM[,PLATFORM...]",
        help=f"Filter detected platforms; use commas or repeat the option. Available: {", ".join(PLATFORMS)}",
    )
    inputs.add_argument(
        "--no-fix-nes-headers", dest="fix_nes_headers", action="store_false",
        help="Disable default hash-verified iNES size repair; keep strict header validation",
    )
    destructive = sync.add_argument_group("Explicit removal and trash")
    destructive.add_argument(
        "--move-roms", action="store_true",
        help="Delete sources after verified cataloguing, including repaired sources and unselected archive variants",
    )
    trash_flags = destructive.add_mutually_exclusive_group()
    trash_flags.add_argument(
        "--trash", action="store_true", help="Remove games marked in prefs.trash BEFORE importing; keep prefs",
    )
    trash_flags.add_argument(
        "--trash-reset", action="store_true", help="Clear prefs.trash before importing; do not delete games",
    )
    add_metadata_options(sync)
    metadata = commands.add_parser(
        "metadata", help="Fill missing metadata in an existing catalogue; no source directory needed",
        description=(
            "Check existing games and fill missing metadata. No import or source removal."
        ),
    )
    add_common_options(metadata)
    add_metadata_options(metadata)
    trash = commands.add_parser("trash", help="Clean or reset IGUI trash without importing or fetching metadata")
    actions = trash.add_subparsers(dest="trash_action", required=True)

    for name, help_text in (
        ("clean", "Remove marked games and thumbnails; keep preferences"),
        ("reset", "Clear trash marks only; keep game files"),
    ):
        action = actions.add_parser(name, help=help_text, description=help_text)
        add_common_options(action)
        action.set_defaults(trash_only=True, trash=name == "clean", trash_reset=name == "reset")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = create_argument_parser()
    args = parser.parse_args(argv)

    if getattr(args, "sources", None) and len(args.sources) != len(set(args.sources)):
        parser.error("--sources must not contain duplicates")

    diagnostics = Diagnostics()

    with configure_cli_logging(args.progress, diagnostics=diagnostics, destination=args.dst):
        try:
            options = Options(
                src=args.src, dst=args.dst, cache=args.cache, logs=args.logs_dir, concurrency=args.concurrency,
                move_roms=args.move_roms, trash=args.trash, trash_reset=args.trash_reset,
                trash_only=args.trash_only, fix_nes_headers=args.fix_nes_headers,
                platforms=(
                    tuple(dict.fromkeys(platform for group in args.platforms for platform in group))
                    if args.platforms else None
                ),
            )

            if args.trash_only:
                provider = Provider([])

            else:
                http = HttpClient(
                    args.cache, concurrency=args.concurrency, timeout=args.timeout, offline=args.offline,
                    refresh=args.refresh, max_retries=args.retries,
                    max_wait=args.max_wait,
                )
                http.diagnostics = diagnostics
                provider = build_provider(
                    http, environ=os.environ, refresh_databases=args.refresh_db, source_names=args.sources,
                )

                for source in provider.sources:

                    if source.key_env:

                        if source.key:
                            logger.info("%s enabled: %s is set", source.name, source.key_env)

                        else:
                            available = any(not item.key_env or item.key for item in provider.sources)
                            logger.info(
                                "%s disabled: %s not set; %s", source.name, source.key_env,
                                "other selected sources remain available"
                                if available else "no enabled metadata source",
                            )

            result = run(options, provider)

        except KeyboardInterrupt:
            logger.error("Interrupted; check the operation journal. Written files are not rolled back.")
            logger.info("Next: wait for this process to exit, then inspect the journal before retrying.")
            return 130

        except (OSError, CatalogueError, zipfile.BadZipFile, NotImplementedError, ValueError) as exc:
            status = "Sync failed" if args.command == "sync" else f"{args.command.capitalize()} failed"
            logger.error("%s", MessageFormatter(args.dst).format_error(exc, status=status))
            logger.info(
                "Impact: %s", getattr(exc, "impact", None)
                or "Operation stopped; completed writes or removals are not rolled back.",
            )
            logger.info(
                "Next: %s", getattr(exc, "hint", None)
                or "Inspect the error and the run journal, if created, before retrying.",
            )
            return 1

    return 3 if result.rejected else 0

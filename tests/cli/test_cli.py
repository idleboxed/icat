import logging
import re
from pathlib import Path

import pytest

from icat.cli import main
from icat.metadata.provider import Provider
from icat.operations.options import Options
from icat.operations.result import Result


def test_repeated_cli_does_not_modify_logging_or_write_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["sync", "--http-offline", "--src", f"{tmp_path / "missing"}", "--dst", f"{tmp_path / "games"}"]
    root_handlers = list(logging.getLogger().handlers)
    package = logging.getLogger("icat")
    package_handlers, level = list(package.handlers), package.level

    assert main(args) == main(args) == 1

    assert logging.getLogger().handlers == root_handlers
    assert package.handlers == package_handlers and package.level == level
    assert capsys.readouterr().out == ""

def test_cli_help_describes_cataloguing_not_publication(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit) as exc:
        main(["sync", "--help"])

    assert exc.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "verified cataloguing" in text
    assert "public" not in text.lower()

def test_interruption_warns_about_written_files_without_publication_terms(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def interrupt_operation(options: Options, provider: Provider) -> Result:
        raise KeyboardInterrupt

    monkeypatch.setattr("icat.cli.run", interrupt_operation)

    result = main(["sync", "--sources", "hasheous", "--http-offline"])

    assert result == 130
    assert "Written files are not rolled back" in caplog.text
    assert "publish" not in caplog.text.lower()

def test_cli_returns_partial_status_when_all_sources_are_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="icat")
    source = tmp_path / "roms"
    source.mkdir()
    bad = source / "bad.nes"
    bad.write_bytes(b"invalid")

    result = main(
        [
            "sync", "--http-offline", "--src", f"{source}", "--dst", f"{tmp_path / "games"}",
            "--cache", f"{tmp_path / "cache"}", "--move-roms",
        ]
    )

    assert result == 3
    assert bad.read_bytes() == b"invalid"
    assert "COMPLETED WITH REJECTIONS" in caplog.text
    assert "1 rejected source files" in caplog.text
    assert "Sync failed" not in caplog.text
    assert capsys.readouterr().out == ""

@pytest.mark.parametrize("value", ["0", "33", "-2"])
def test_bad_concurrency_is_a_cli_error(value: str) -> None:

    with pytest.raises(SystemExit) as exc:
        main(["sync", "--http-concurrency", value])

    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("option", "message"),
    [
        pytest.param("--http-concurrency", "invalid integer value", id="concurrency"),
        pytest.param("--http-retries", "invalid retry_count value", id="retries"),
    ],
)
def test_non_numeric_http_options_keep_cli_diagnostics(
    option: str, message: str, capsys: pytest.CaptureFixture[str],
) -> None:

    with pytest.raises(SystemExit, match="^2$") as error:
        main(["sync", option, "not-a-number"])

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith(f"icat sync: error: argument {option}: {message}: 'not-a-number'\n")


def test_cli_help_lists_every_supported_platform(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit) as exc:
        main(["sync", "--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--platform PLATFORM[,PLATFORM...]" in help_text
    available = (
        "Available: NES, FDS, GB, GBC, GBA, MD, 32X, SMS, GG, SG1000, SNES, PCE, WS, WSC, "
        "A2600, A5200, A7800, N64, NDS"
    )
    assert available in " ".join(help_text.split())

def test_cli_accepts_repeatable_platform_filter_and_rejects_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    captured = []

    def capture(options: Options, provider: Provider) -> Result:
        captured.append(options)
        return Result(0, 0, 0, 0, 0)

    monkeypatch.setattr("icat.cli.run", capture)

    assert main(["sync", "--http-offline", "--platform", "NES,FDS", "--platform", "NES"]) == 0
    assert captured[0].platforms == ("NES", "FDS")

    for value in ("PS1", "NES,,FDS"):

        with pytest.raises(SystemExit) as exc:
            main(["sync", "--platform", value])

        assert exc.value.code == 2
        assert "unknown platform" in capsys.readouterr().err

def test_removed_igdb_source_is_not_offered_or_accepted(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit) as exc:
        main(["sync", "--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    sources_line = next(line for line in help_text.splitlines() if "--sources" in line)
    assert "igdb" not in sources_line

    with pytest.raises(SystemExit) as exc:
        main(["sync", "--sources", "igdb"])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err

def test_http_options_have_http_prefix_and_old_names_are_rejected(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit) as exc:
        main(["sync", "--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out

    for name in ("concurrency", "timeout", "retries", "max-wait", "offline", "refresh"):
        assert f"--http-{name}" in help_text

    with pytest.raises(SystemExit) as exc:
        main(["sync", "--offline"])

    assert exc.value.code == 2

def test_http_options_are_contiguous_in_usage_and_help(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit) as exc:
        main(["sync", "--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "HTTP options:\n" in help_text
    expected = [
        "--http-concurrency", "--http-timeout", "--http-retries", "--http-max-wait",
        "--http-offline", "--http-refresh",
    ]
    usage = help_text.split("\n\n", 1)[0]
    http_help = help_text.split("HTTP options:\n", 1)[1].split("Examples:", 1)[0]

    for section in (usage, http_help):
        options = re.findall(r"--[a-z][a-z-]*", section)
        positions = [index for index, option in enumerate(options) if option.startswith("--http-")]
        assert [options[index] for index in positions] == expected
        assert positions == list(range(positions[0], positions[0] + len(expected)))

@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], (False, False)),
        (["--refresh-db"], (True, False)),
        (["--http-refresh"], (False, True)),
        (["--refresh-db", "--http-refresh"], (True, True)),
    ],
)
def test_database_and_http_refresh_flags_are_independent(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], expected: tuple[bool, bool],
) -> None:
    calls = []

    def capture(options: Options, provider: Provider) -> Result:
        calls.append(provider)
        return Result(0, 0, 0, 0, 0)

    monkeypatch.setattr("icat.cli.run", capture)

    result = main(["sync", "--sources", "openvgdb", *flags])

    assert result == 0
    source = calls[0].sources[0]
    assert (source.datasets.refresh, source.http.refresh) == expected

def test_old_database_refresh_flag_is_not_offered_or_accepted(capsys: pytest.CaptureFixture[str]) -> None:

    with pytest.raises(SystemExit) as exc:
        main(["sync", "--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--refresh-db" in help_text
    assert "--refresh-databases" not in help_text

    with pytest.raises(SystemExit) as exc:
        main(["sync", "--refresh-databases"])

    assert exc.value.code == 2
    assert "unrecognized arguments: --refresh-databases" in capsys.readouterr().err

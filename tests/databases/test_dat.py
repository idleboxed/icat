import pytest

from icat.databases.dat import LIMIT, read_dat


def test_dat_quotes_escapes_comments_and_repeated_rom_blocks() -> None:
    data = rb"""
        # Synthetic DAT, not a real game.
        clrmamepro ( name "A (platform)" )
        game ( name "Title \"quoted\" \\ # ()" rom ( crc 12345678 ) rom ( crc ABCDEF00 ) )
    """

    header, game = list(read_dat(data))

    assert header[1].get_scalar("name") == "A (platform)"
    assert game[1].get_scalar("name") == "Title \"quoted\" \\ # ()"
    assert [rom.get_scalar("crc") for rom in game[1].get_children("rom")] == ["12345678", "ABCDEF00"]
    assert game[1].get_scalar("missing") is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"game ( name \"unfinished )", "DAT token"),
        (b"game ( rom ( crc 12345678 )", "Truncated DAT"),
        (b"game ( users )", "Missing DAT"),
        (b"game ( ) )", "Expected DAT"),
        (b"game ( \x00 )", "DAT token"),
        (b"game 2", "Expected DAT"),
        (b"game ( 123 bad )", "Invalid DAT field"),
        (b"a ( " * 9 + b")" * 9, "nesting"),
        (b" " * (LIMIT + 1), "size limit"),
    ],
)
def test_dat_rejects_malformed_or_unbounded_input(data: bytes, message: str) -> None:

    with pytest.raises(ValueError, match=message):
        list(read_dat(data))


def test_dat_does_not_silently_choose_a_duplicate_scalar_or_wrong_field_type() -> None:
    _tag, game = list(read_dat(b"game ( users 1 users 2 rom invalid )"))[0]

    with pytest.raises(ValueError, match="one DAT scalar: users"):
        game.get_scalar("users")

    with pytest.raises(ValueError, match="DAT blocks: rom"):
        game.get_children("rom")

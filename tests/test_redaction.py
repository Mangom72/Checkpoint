import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint.redaction import RepoNameRedactor


def record(message, *args):
    return logging.LogRecord("t", logging.INFO, __file__, 1, message, args, None)


def test_disabled_redactor_is_a_passthrough():
    r = RepoNameRedactor(enabled=False)
    r.register(["me/secret-thing"])
    rec = record("backing up %s", "me/secret-thing")
    assert r.filter(rec) is True
    assert rec.getMessage() == "backing up me/secret-thing"


def test_full_name_is_replaced_with_a_stable_alias():
    r = RepoNameRedactor(enabled=True)
    r.register(["me/secret-thing", "me/other"])
    rec = record("[1/2] %s done in %ss", "me/secret-thing", 3)
    r.filter(rec)
    assert rec.getMessage() == "[1/2] repo#01 done in 3s"
    assert "secret-thing" not in rec.getMessage()

    again = record("%s failed", "me/secret-thing")
    r.filter(again)
    assert again.getMessage() == "repo#01 failed", "같은 레포는 항상 같은 별칭이어야 함"


def test_archive_slug_and_bare_name_are_covered():
    r = RepoNameRedactor(enabled=True)
    r.register(["me/secret-thing"])
    for text in (
        "rclone copyto -> gdrive:backups/repos/me__secret-thing.tar.gz",
        "cloning secret-thing",
        "https://github.com/me/secret-thing.git failed",
    ):
        rec = record("%s", text)
        r.filter(rec)
        assert "secret-thing" not in rec.getMessage(), text


def test_longer_spellings_win():
    r = RepoNameRedactor(enabled=True)
    r.register(["me/app", "other/app-extra"])
    rec = record("%s and %s", "other/app-extra", "me/app")
    r.filter(rec)
    assert rec.getMessage() == "repo#02 and repo#01"


def test_alias_table_is_exposed_for_the_manifest():
    r = RepoNameRedactor(enabled=True)
    r.register(["me/a", "me/b"])
    assert r.aliases == {"me/a": "repo#01", "me/b": "repo#02"}


def test_scrub_handles_plain_strings():
    r = RepoNameRedactor(enabled=True)
    r.register(["me/private-notes"])
    assert r.scrub("error in me/private-notes") == "error in repo#01"

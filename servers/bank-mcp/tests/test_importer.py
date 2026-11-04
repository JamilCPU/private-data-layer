import logging
import shutil
from pathlib import Path

import pytest

from pdl_bank import cli_import
from pdl_bank.categorize import load_rules
from pdl_bank.importer import ImportResult, import_file, import_inbox
from pdl_bank.memory_store import MemoryStore
from pdl_bank.parsers import load_mappings

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures"
MAPPINGS = load_mappings(ROOT / "csv-mappings.example.yaml")
RULES = load_rules(ROOT / "rules.example.yaml")


def _copy(name: str, dest: Path, as_name: str | None = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / (as_name or name)
    shutil.copy(FIX / name, target)
    return target


def _by_desc(store: MemoryStore) -> dict[str, str | None]:
    found = store.search(None, None, None, None, None, 100)
    return {t.description: t.category for t in found}


def test_import_file_applies_categories(tmp_path: Path) -> None:
    store = MemoryStore()
    result = import_file(_copy("sample.csv", tmp_path), store, MAPPINGS, RULES)
    assert result == ImportResult("sample.csv", "imported", 4, None)
    cats = _by_desc(store)
    assert cats["STREAMFLIX MONTHLY"] == "subscriptions"
    assert cats["GREEN GROCER #12"] == "groceries"
    assert cats["PAYROLL DEPOSIT"] == "income"
    assert len(store.accounts()) == 1


def test_reimport_is_duplicate(tmp_path: Path) -> None:
    store = MemoryStore()
    import_file(_copy("sample.csv", tmp_path), store, MAPPINGS, RULES)
    again = import_file(_copy("sample.csv", tmp_path, "copy.csv"), store, MAPPINGS, RULES)
    assert again == ImportResult("copy.csv", "duplicate", 0, None)


def test_overlapping_files_do_not_double_count(tmp_path: Path) -> None:
    store = MemoryStore()
    first = tmp_path / "a.csv"
    first.write_text("Date,Description,Amount\n2026-01-03,CAFE ONE,-4.00\n2026-01-04,TAXI,-9.00\n")
    second = tmp_path / "b.csv"
    second.write_text(
        "Date,Description,Amount\n2026-01-04,TAXI,-9.00\n2026-01-05,PIZZA PLACE,-12.00\n"
    )
    assert import_file(first, store, MAPPINGS, RULES).new_transactions == 2
    res = import_file(second, store, MAPPINGS, RULES)
    assert (res.status, res.new_transactions) == ("imported", 1)
    assert len(store.search(None, None, None, None, None, 100)) == 3


def test_malformed_file_errors_and_loop_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a_bad.csv").write_text("nonsense,columns\n1,2\n")
    _copy("sample.csv", inbox)
    store = MemoryStore()
    with caplog.at_level(logging.WARNING):
        results = import_inbox(inbox, store, MAPPINGS, RULES, processed=None)
    assert [(r.file, r.status) for r in results] == [
        ("a_bad.csv", "error"),
        ("sample.csv", "imported"),
    ]
    assert results[0].reason
    assert any(r.levelno == logging.WARNING and "a_bad.csv" in r.message for r in caplog.records)


def test_oversized_file_errors(tmp_path: Path) -> None:
    big = tmp_path / "big.csv"
    with big.open("wb") as fh:
        fh.seek(20_000_000)
        fh.write(b"x")
    assert big.stat().st_size == 20_000_001
    res = import_file(big, MemoryStore(), MAPPINGS, RULES)
    assert res.status == "error" and res.reason and "too large" in res.reason


def test_unsupported_and_hidden_are_skipped_in_place(tmp_path: Path) -> None:
    inbox = tmp_path / "in"
    inbox.mkdir()
    (inbox / "notes.txt").write_text("hi")
    (inbox / ".hidden.csv").write_text("Date,Description,Amount\n")
    (inbox / "subdir").mkdir()
    processed = tmp_path / "done"
    results = import_inbox(inbox, MemoryStore(), MAPPINGS, RULES, processed=processed)
    assert [(r.file, r.status) for r in results] == [
        (".hidden.csv", "skipped"),
        ("notes.txt", "skipped"),
    ]
    assert (inbox / "notes.txt").exists() and (inbox / ".hidden.csv").exists()
    assert not any(processed.iterdir())


def test_uppercase_suffix_is_imported(tmp_path: Path) -> None:
    path = _copy("sample.csv", tmp_path, "STATEMENT.CSV")
    assert import_file(path, MemoryStore(), MAPPINGS, RULES).status == "imported"


def test_processed_move_and_collision_suffix(tmp_path: Path) -> None:
    inbox, processed = tmp_path / "in", tmp_path / "out"
    store = MemoryStore()
    _copy("sample.csv", inbox)
    results = import_inbox(inbox, store, MAPPINGS, RULES, processed=processed)
    assert results[0].status == "imported"
    assert (processed / "sample.csv").exists() and not (inbox / "sample.csv").exists()
    _copy("sample.csv", inbox)
    results = import_inbox(inbox, store, MAPPINGS, RULES, processed=processed)
    assert results[0].status == "duplicate"
    moved = sorted(p.name for p in processed.iterdir())
    assert len(moved) == 2 and "sample.csv" in moved
    suffixed = next(n for n in moved if n != "sample.csv")
    # "sample." + 8 hex + ".csv"
    assert suffixed.startswith("sample.") and suffixed.endswith(".csv") and len(suffixed) == 19


def test_error_file_not_moved(tmp_path: Path) -> None:
    inbox, processed = tmp_path / "in", tmp_path / "out"
    inbox.mkdir()
    (inbox / "bad.csv").write_text("zzz\n")
    import_inbox(inbox, MemoryStore(), MAPPINGS, RULES, processed=processed)
    assert (inbox / "bad.csv").exists()


def _cli_args(inbox: Path, *extra: str) -> list[str]:
    return [
        "--inbox",
        str(inbox),
        "--rules",
        str(ROOT / "rules.example.yaml"),
        "--mappings",
        str(ROOT / "csv-mappings.example.yaml"),
        *extra,
    ]


def test_cli_memory_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("sample.csv", "sample.ofx", "sample.qfx"):
        _copy(name, tmp_path / "in")
    code = cli_import.main(_cli_args(tmp_path / "in", "--memory", "--once"))
    out = capsys.readouterr().out
    assert code == 0
    assert "sample.csv: imported" in out


def test_cli_exit_1_on_error(tmp_path: Path) -> None:
    (tmp_path / "bad.csv").write_text("zzz\n")
    assert cli_import.main(_cli_args(tmp_path, "--memory")) == 1


def test_cli_bad_rules_exits_2(tmp_path: Path) -> None:
    rules = tmp_path / "r.yaml"
    rules.write_text("nope: 1\n")
    args = [
        "--inbox",
        str(tmp_path),
        "--rules",
        str(rules),
        "--mappings",
        str(ROOT / "csv-mappings.example.yaml"),
        "--memory",
    ]
    assert cli_import.main(args) == 2


def test_cli_without_memory_exits_2_when_factory_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> object:
        raise ImportError("no store_factory")

    monkeypatch.setattr(cli_import, "_load_store_factory", boom)
    assert cli_import.main(_cli_args(tmp_path)) == 2
    assert "use --memory" in capsys.readouterr().err


def test_cli_without_memory_uses_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    monkeypatch.setattr(cli_import, "_load_store_factory", lambda: lambda env: store)
    _copy("sample.csv", tmp_path / "in")
    assert cli_import.main(_cli_args(tmp_path / "in")) == 0
    assert len(store.accounts()) == 1


def test_cli_interval_loops_until_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[int] = []

    def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_import.time, "sleep", fake_sleep)
    code = cli_import.main(_cli_args(tmp_path, "--memory", "--interval", "7"))
    assert code == 0 and sleeps == [7, 7]


def test_cli_rejects_nonpositive_interval(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_import.main(_cli_args(tmp_path, "--memory", "--interval", "0"))
    assert exc.value.code == 2

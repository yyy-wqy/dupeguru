import io
import zipfile
from pathlib import Path

import pytest

from hscommon.jobprogress.job import JobCancelled

from core import app as core_app, engine, results
from core.scanner import ScanType, Scanner
from core.se import fs
from core.tests.base import DupeGuru
from core.zip_scan import (
    ReadOnlyGroup,
    ReadOnlyVirtualFileError,
    ZipAnalyzer,
    ZipMember,
    ZipThresholdAction,
)


def write_zip(path, entries, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)


def wrapped(path):
    file = fs.File(Path(path))
    file.is_ref = False
    return file


def test_package_equivalence_ignores_compression_parameters(tmp_path):
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    entries = [("docs/a.txt", b"alpha"), ("docs/b.pdf", b"beta")]
    write_zip(first, entries, zipfile.ZIP_STORED)
    write_zip(second, entries, zipfile.ZIP_DEFLATED)

    analysis = ZipAnalyzer().analyze_files([wrapped(first), wrapped(second)])

    assert len(analysis.equivalence_matches) == 1
    assert analysis.equivalence_matches[0].percentage == 100
    assert not analysis.problems


def test_changed_member_path_and_duplicate_count_are_not_equivalent(tmp_path):
    first = tmp_path / "first.zip"
    renamed = tmp_path / "renamed.zip"
    repeated = tmp_path / "repeated.zip"
    write_zip(first, [("a.txt", b"same")])
    write_zip(renamed, [("b.txt", b"same")])
    write_zip(repeated, [("a.txt", b"same"), ("a.txt", b"same")])

    analysis = ZipAnalyzer().analyze_files([wrapped(first), wrapped(renamed), wrapped(repeated)])

    assert analysis.equivalence_matches == []
    assert len(analysis.subset_hints) == 1
    assert analysis.subset_hints[0].subset.path == first
    assert analysis.subset_hints[0].superset.path == repeated


def test_directories_are_ignored_and_nested_archives_are_not_expanded(tmp_path):
    path = tmp_path / "nested.zip"
    inner_bytes = b"not-an-archive-for-this-test"
    write_zip(path, [("empty/", b""), ("inner.zip", inner_bytes), ("note.txt", b"hello")])

    analysis = ZipAnalyzer().analyze_files([wrapped(path)])

    record = analysis.archives[0]
    assert [entry.member_path for entry in record.entries] == ["inner.zip", "note.txt"]
    assert [member.member_path for member in record.virtual_members] == ["note.txt"]


def test_warning_and_hard_threshold_boundaries(tmp_path):
    warning_zip = tmp_path / "warning.zip"
    hard_zip = tmp_path / "hard.zip"
    write_zip(warning_zip, [("a.txt", b"x" * 10)])
    write_zip(hard_zip, [("a.txt", b"x" * 20)])

    skipped = ZipAnalyzer(warning_limit=10, hard_limit=20).analyze_files([wrapped(warning_zip)])
    assert skipped.problems[0].code == "warning_limit_skipped"

    continued = ZipAnalyzer(
        warning_limit=10,
        hard_limit=20,
        threshold_decider=lambda _file, _size: ZipThresholdAction.CONTINUE,
    ).analyze_files([wrapped(warning_zip)])
    assert len(continued.archives) == 1

    hard_limited = ZipAnalyzer(
        warning_limit=10,
        hard_limit=20,
        threshold_decider=lambda _file, _size: pytest.fail("hard limit must take precedence"),
    ).analyze_files([wrapped(hard_zip)])
    assert hard_limited.problems[0].code == "hard_limit"

    with pytest.raises(JobCancelled):
        ZipAnalyzer(
            warning_limit=10,
            hard_limit=20,
            threshold_decider=lambda _file, _size: ZipThresholdAction.CANCEL,
        ).analyze_files([wrapped(warning_zip)])


def test_unreadable_archive_is_downgraded_without_stopping_batch(tmp_path):
    broken = tmp_path / "broken.zip"
    good = tmp_path / "good.zip"
    broken.write_bytes(b"not a zip")
    write_zip(good, [("a.txt", b"ok")])

    analysis = ZipAnalyzer().analyze_files([wrapped(broken), wrapped(good)])

    assert [problem.container.path for problem in analysis.problems] == [broken]
    assert analysis.problems[0].code == "unreadable_archive"
    assert [record.container.path for record in analysis.archives] == [good]


def test_encrypted_member_downgrades_the_whole_archive(tmp_path, monkeypatch):
    path = tmp_path / "encrypted.zip"
    path.write_bytes(b"placeholder")

    class EncryptedInfo:
        filename = "secret.txt"
        file_size = 6
        flag_bits = 0x1

        @staticmethod
        def is_dir():
            return False

    class EncryptedArchive:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def infolist():
            return [EncryptedInfo()]

    monkeypatch.setattr(zipfile, "ZipFile", lambda *_args, **_kwargs: EncryptedArchive())

    analysis = ZipAnalyzer().analyze_files([wrapped(path)])

    assert analysis.archives == []
    assert analysis.problems[0].code == "encrypted_member"
    assert analysis.problems[0].member_path == "secret.txt"


def test_standard_content_scan_creates_mixed_group_with_real_reference(tmp_path):
    archive_path = tmp_path / "archive.zip"
    real_path = tmp_path / "outside.docx"
    content = b"same document bytes"
    write_zip(archive_path, [("inside.docx", content)])
    real_path.write_bytes(content)

    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS
    groups = scanner.get_dupe_groups([wrapped(archive_path), wrapped(real_path)])

    mixed = next(group for group in groups if any(getattr(item, "is_virtual", False) for item in group))
    assert mixed.ref.path == real_path
    assert not getattr(mixed.ref, "is_virtual", False)
    assert len([item for item in mixed if getattr(item, "is_virtual", False)]) == 1


@pytest.mark.parametrize(
    ("size", "big_file_threshold"),
    [(40 * 1024, 0), (4 * 1024 * 1024, 1024 * 1024)],
)
def test_large_virtual_member_matches_real_file_with_standard_hash_pipeline(tmp_path, size, big_file_threshold):
    archive_path = tmp_path / "archive.zip"
    real_path = tmp_path / "outside.pdf"
    pattern = bytes(range(251))
    content = (pattern * (size // len(pattern) + 1))[:size]
    write_zip(archive_path, [("inside.pdf", content)], zipfile.ZIP_DEFLATED)
    real_path.write_bytes(content)

    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS
    scanner.big_file_size_threshold = big_file_threshold
    groups = scanner.get_dupe_groups([wrapped(archive_path), wrapped(real_path)])

    mixed = [group for group in groups if any(getattr(item, "is_virtual", False) for item in group)]
    assert len(mixed) == 1
    assert mixed[0].ref.path == real_path


def test_pure_virtual_matches_are_kept_as_readonly_hints(tmp_path):
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    entries = [("a.txt", b"same")]
    write_zip(first, entries, zipfile.ZIP_STORED)
    write_zip(second, entries, zipfile.ZIP_DEFLATED)

    scanner = Scanner()
    scanner.scan_type = ScanType.CONTENTS
    groups = scanner.get_dupe_groups([wrapped(first), wrapped(second)])

    assert any(group.ref.path in {first, second} for group in groups)
    assert len(scanner.readonly_groups) == 1
    readonly = scanner.readonly_groups[0]
    assert readonly.ref is None
    assert all(getattr(member, "is_virtual", False) for member in readonly)


def test_virtual_members_are_unmarkable_and_cannot_become_reference(tmp_path):
    container = tmp_path / "archive.zip"
    write_zip(container, [("a.txt", b"same")])
    member = ZipAnalyzer().analyze_files([wrapped(container)]).virtual_members[0]
    real_path = tmp_path / "a.txt"
    real_path.write_bytes(b"same")
    real = wrapped(real_path)
    group = engine.Group()
    group.add_match(engine.Match(real, member, 100))
    group.prioritize(lambda item: (getattr(item, "is_virtual", False), -item.size))

    app = DupeGuru()
    app.results.groups = [group]

    assert not app.results.is_markable(member)
    assert not app.results.make_ref(member)
    assert group.ref is real
    assert not group.switch_ref(member)
    with pytest.raises(ReadOnlyVirtualFileError):
        member.rename("changed.txt")


def test_virtual_results_round_trip_as_readonly_records(tmp_path):
    container = tmp_path / "archive.zip"
    real_path = tmp_path / "outside.txt"
    content = b"same"
    write_zip(container, [("inside.txt", content), ("second.txt", content)])
    real_path.write_bytes(content)
    members = ZipAnalyzer().analyze_files([wrapped(container)]).virtual_members
    real = wrapped(real_path)

    mixed = engine.Group()
    mixed.add_match(engine.Match(real, members[0], 100))
    mixed.prioritize(lambda item: (getattr(item, "is_virtual", False), -item.size))
    readonly = ReadOnlyGroup(members, [engine.Match(members[0], members[1], 100)])

    app = DupeGuru()
    app.results.groups = [mixed]
    app.results.readonly_groups = [readonly]
    output = tmp_path / "results.dupeguru"
    app.results.save_to_xml(output)

    restored = results.Results(app)
    restored.load_from_xml(output, lambda path: wrapped(path) if Path(path).is_file() else None)

    restored_virtual = next(item for item in restored.groups[0] if getattr(item, "is_virtual", False))
    assert restored.groups[0].ref.path == real_path
    assert not restored.is_markable(restored_virtual)
    assert len(restored.readonly_groups) == 1
    assert restored.readonly_groups[0].ref is None
    assert all(getattr(item, "is_virtual", False) for item in restored.readonly_groups[0])


def test_readonly_only_results_are_modified_filterable_and_exportable(tmp_path):
    container = tmp_path / "archive.zip"
    write_zip(container, [("inside.txt", b"same"), ("other.txt", b"same")])
    members = ZipAnalyzer().analyze_files([wrapped(container)]).virtual_members
    readonly = ReadOnlyGroup(members, [engine.Match(members[0], members[1], 100)])
    app = DupeGuru()
    app.results.groups = []
    app.results.readonly_groups = [readonly]

    assert app.results.is_modified
    _columns, rows = app._get_export_data()
    assert len(rows) == 2

    app.results.apply_filter("inside")
    assert app.results.readonly_groups == [readonly]
    assert app.results.readonly_dupes == [members[0]]
    app.results.apply_filter("missing")
    assert app.results.readonly_groups == []
    assert app.results.readonly_dupes == []


def test_custom_command_ignores_virtual_members_in_mixed_selection(tmp_path, monkeypatch):
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_bytes(b"same")
    second_path.write_bytes(b"same")
    first = wrapped(first_path)
    second = wrapped(second_path)
    group = engine.Group()
    group.add_match(engine.Match(first, second, 100))
    group.prioritize(lambda item: str(item.path))

    container = tmp_path / "archive.zip"
    write_zip(container, [("inside.txt", b"same"), ("other.txt", b"same")])
    members = ZipAnalyzer().analyze_files([wrapped(container)]).virtual_members
    readonly = ReadOnlyGroup(members, [engine.Match(members[0], members[1], 100)])

    app = DupeGuru()
    app.results.groups = [group]
    app.results.readonly_groups = [readonly]
    app.selected_dupes = [first, members[0]]
    app.view.get_default = lambda _key: "tool %d %r"
    calls = []

    class Process:
        stdout = io.BytesIO()

    def popen(command, **_kwargs):
        calls.append(command)
        return Process()

    monkeypatch.setattr(core_app.subprocess, "Popen", popen)

    app.invoke_custom_command()

    assert len(calls) == 1
    assert str(first.path) in calls[0]
    assert str(members[0].path) not in calls[0]

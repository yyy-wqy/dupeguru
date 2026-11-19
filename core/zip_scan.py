# Copyright 2026
#
# This software is licensed under the "GPLv3" License as described in the "LICENSE" file.

"""ZIP archive analysis used by the standard byte-content scanner.

The archive itself remains a normal :class:`core.fs.File`.  Members are represented by
read-only ``ZipMember`` objects and are never extracted to disk.  An archive is eligible for
package-level equivalence only when every non-directory member was read successfully.
"""

import os
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from hscommon.jobprogress import job
from hscommon.trans import tr
from hscommon.util import format_size, get_file_ext

from core import engine
from core.fs import CHUNK_SIZE, MIN_FILE_SIZE, PARTIAL_OFFSET_SIZE, FSError, File, hasher
from core.util import format_dupe_count, format_perc, format_timestamp


MIB = 2**20
GIB = 2**30
DEFAULT_WARNING_LIMIT = 500 * MIB
DEFAULT_HARD_LIMIT = 2 * GIB
READ_CHUNK_SIZE = MIB
SUPPORTED_VIRTUAL_EXTENSIONS = frozenset({"txt", "docx", "pdf"})


class ZipThresholdAction:
    CONTINUE = "continue"
    SKIP = "skip"
    CANCEL = "cancel"


class ReadOnlyVirtualFileError(FSError):
    cls_message = "Archive member '{name}' is read-only and cannot be modified."


@dataclass(frozen=True, order=True)
class ZipEntryFingerprint:
    member_path: str
    size: int
    digest: bytes


class ZipMember(File):
    """A logical, read-only file stored inside a ZIP container."""

    __slots__ = ("container_path", "member_path", "member_index", "is_virtual", "readonly")

    def __init__(
        self,
        container_path,
        member_path,
        member_index,
        size,
        digest,
        mtime=None,
        digest_partial=None,
        digest_samples=None,
    ):
        self.container_path = Path(container_path)
        self.member_path = member_path.replace("\\", "/").lstrip("/")
        self.member_index = int(member_index)
        logical_path = Path(str(self.container_path) + os.sep + self.member_path.replace("/", os.sep))
        File.__init__(self, logical_path)
        self.unicode_path = "%s/%s" % (self.container_path, self.member_path)
        self.size = int(size)
        if mtime is None:
            try:
                mtime = self.container_path.stat().st_mtime
            except OSError:
                mtime = 0
        self.mtime = mtime
        self.digest = bytes(digest)
        self.digest_partial = self.digest if digest_partial is None else bytes(digest_partial)
        self.digest_samples = self.digest if digest_samples is None else bytes(digest_samples)
        self.is_ref = False
        self.words = []
        self.is_virtual = True
        self.readonly = True

    @classmethod
    def from_metadata(
        cls,
        container_path,
        member_path,
        member_index,
        size,
        digest_hex,
        mtime=0,
        digest_partial_hex=None,
        digest_samples_hex=None,
    ):
        def decode(value, fallback=b""):
            if value is None:
                return fallback
            try:
                return bytes.fromhex(value)
            except (TypeError, ValueError):
                return fallback

        digest = decode(digest_hex)
        return cls(
            container_path,
            member_path,
            member_index,
            size,
            digest,
            mtime,
            decode(digest_partial_hex, digest),
            decode(digest_samples_hex, digest),
        )

    def _read_info(self, field):
        # All member metadata is populated while the archive stream is open.
        return

    def exists(self):
        try:
            return self.container_path.is_file()
        except OSError:
            return False

    def rename(self, newname):
        raise ReadOnlyVirtualFileError(self)

    def get_display_info(self, group, delta):
        size = self.size
        mtime = self.mtime
        match = group.get_match_of(self)
        if match:
            percentage = match.percentage
            dupe_count = 0
            ref = group.ref
            if delta and ref is not None:
                size -= ref.size
                mtime -= ref.mtime
        else:
            percentage = group.percentage
            dupe_count = len(group.dupes)
        return {
            "name": self.name,
            "folder_path": str(self.folder_path),
            "size": format_size(size, 0, 1, False),
            "extension": self.extension,
            "mtime": format_timestamp(mtime, delta and match),
            "percentage": format_perc(percentage),
            "words": "",
            "dupe_count": format_dupe_count(dupe_count),
        }

    @property
    def extension(self):
        return get_file_ext(self.name)

    @property
    def name(self):
        return PurePosixPath(self.member_path).name

    @property
    def folder_path(self):
        parent = PurePosixPath(self.member_path).parent
        if str(parent) == ".":
            return self.container_path
        return Path(str(self.container_path) + os.sep + str(parent).replace("/", os.sep))


class ReadOnlyGroup:
    """Display-only group for matches containing no real filesystem member.

    It deliberately has no reference item.  This prevents a virtual member from becoming
    ``Group.ref`` while still allowing the existing result table to render the hint.
    """

    def __init__(self, members, matches=None):
        self.ordered = list(members)
        self.matches = set(matches or ())
        self.ref = None

    def __contains__(self, item):
        return item in self.ordered

    def __getitem__(self, key):
        return self.ordered[key]

    def __iter__(self):
        return iter(self.ordered)

    def __len__(self):
        return len(self.ordered)

    @property
    def dupes(self):
        return self.ordered

    @property
    def percentage(self):
        return 100

    def get_match_of(self, item):
        for match in self.matches:
            if item in match:
                return match
        return None


@dataclass
class ZipArchiveRecord:
    container: File
    entries: List[ZipEntryFingerprint]
    virtual_members: List[ZipMember]

    @property
    def signature(self):
        # Sorting retains repeated triples, so duplicate names remain significant.
        return tuple(sorted(self.entries))

    @property
    def multiset(self):
        return Counter(self.entries)


@dataclass
class ZipProblem:
    container: File
    code: str
    message: str
    member_path: Optional[str] = None


@dataclass(frozen=True)
class ZipSubsetHint:
    subset: File
    superset: File


@dataclass
class ZipAnalysis:
    archives: List[ZipArchiveRecord] = field(default_factory=list)
    equivalence_matches: List[engine.Match] = field(default_factory=list)
    subset_hints: List[ZipSubsetHint] = field(default_factory=list)
    problems: List[ZipProblem] = field(default_factory=list)

    @property
    def virtual_members(self):
        result = []
        for archive in self.archives:
            result.extend(archive.virtual_members)
        return result


class ZipAnalyzer:
    """Analyze ZIP files without extracting members to the filesystem."""

    def __init__(
        self,
        warning_limit=DEFAULT_WARNING_LIMIT,
        hard_limit=DEFAULT_HARD_LIMIT,
        threshold_decider=None,
        chunk_size=READ_CHUNK_SIZE,
    ):
        if warning_limit < 0 or hard_limit <= 0 or warning_limit > hard_limit:
            raise ValueError("Invalid ZIP scan thresholds")
        self.warning_limit = int(warning_limit)
        self.hard_limit = int(hard_limit)
        self.threshold_decider = threshold_decider
        self.chunk_size = max(1, int(chunk_size))

    def analyze_files(self, files: Iterable[File], j=job.nulljob):
        analysis = ZipAnalysis()
        for file in files:
            if get_file_ext(file.name).lower() != "zip":
                continue
            j.check_if_cancelled()
            record, problem = self._analyze_archive(file, j)
            if problem is not None:
                analysis.problems.append(problem)
            if record is not None:
                analysis.archives.append(record)
        self._compare_archives(analysis)
        return analysis

    def _threshold_action(self, container, declared_size):
        if self.threshold_decider is None:
            return ZipThresholdAction.SKIP
        return self.threshold_decider(container, declared_size)

    def _analyze_archive(self, container, j):
        try:
            with zipfile.ZipFile(container.path, "r") as archive:
                infos = [info for info in archive.infolist() if not self._is_directory(info)]
                declared_size = sum(info.file_size for info in infos)

                # The mandatory limit takes precedence over the warning dialog.
                if declared_size >= self.hard_limit:
                    return None, self._problem(
                        container,
                        "hard_limit",
                        tr("ZIP internal analysis skipped because its uncompressed size reached the hard limit."),
                    )
                if declared_size >= self.warning_limit:
                    action = self._threshold_action(container, declared_size)
                    if action == ZipThresholdAction.CANCEL:
                        raise job.JobCancelled()
                    if action != ZipThresholdAction.CONTINUE:
                        return None, self._problem(
                            container,
                            "warning_limit_skipped",
                            tr("ZIP internal analysis was skipped at the warning threshold."),
                        )

                encrypted = next((info for info in infos if info.flag_bits & 0x1), None)
                if encrypted is not None:
                    return None, self._problem(
                        container,
                        "encrypted_member",
                        tr("ZIP internal analysis skipped because an encrypted member cannot be read."),
                        encrypted.filename,
                    )

                entries = []
                virtual_members = []
                actual_total = 0
                for member_index, info in enumerate(infos):
                    j.check_if_cancelled()
                    digest, digest_partial, digest_samples, bytes_read, hit_limit = self._hash_member(
                        archive, info, actual_total, j
                    )
                    if hit_limit:
                        return None, self._problem(
                            container,
                            "hard_limit",
                            tr("ZIP internal analysis stopped after the actual bytes read reached the hard limit."),
                            info.filename,
                        )
                    actual_total += bytes_read
                    member_path = info.filename.replace("\\", "/").rstrip("/")
                    entries.append(ZipEntryFingerprint(member_path, bytes_read, digest))
                    extension = PurePosixPath(member_path).suffix.lower().lstrip(".")
                    if extension in SUPPORTED_VIRTUAL_EXTENSIONS:
                        virtual_members.append(
                            ZipMember(
                                container.path,
                                member_path,
                                member_index,
                                bytes_read,
                                digest,
                                getattr(container, "mtime", 0),
                                digest_partial,
                                digest_samples,
                            )
                        )
                return ZipArchiveRecord(container, entries, virtual_members), None
        except job.JobCancelled:
            raise
        except (
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            RuntimeError,
            NotImplementedError,
            OSError,
            EOFError,
        ) as error:
            return None, self._problem(
                container,
                "unreadable_archive",
                tr("ZIP internal analysis failed and was downgraded to external byte comparison: %s") % error,
            )

    def _hash_member(self, archive, info, current_total, j):
        digest = hasher()
        bytes_read = 0
        partial_buffer = bytearray()
        sample_buffers = []
        if info.file_size > MIN_FILE_SIZE:
            sample_starts = (
                info.file_size * 25 // 100,
                info.file_size * 60 // 100,
                max(0, info.file_size - CHUNK_SIZE),
            )
            sample_buffers = [(start, min(start + CHUNK_SIZE, info.file_size), bytearray()) for start in sample_starts]

        def append_overlap(buffer, range_start, range_end, chunk, chunk_start):
            overlap_start = max(range_start, chunk_start)
            overlap_end = min(range_end, chunk_start + len(chunk))
            if overlap_start < overlap_end:
                start = overlap_start - chunk_start
                buffer.extend(chunk[start : start + overlap_end - overlap_start])

        with archive.open(info, "r") as stream:
            while True:
                chunk = stream.read(self.chunk_size)
                if not chunk:
                    break
                chunk_start = bytes_read
                bytes_read += len(chunk)
                if current_total + bytes_read >= self.hard_limit:
                    return b"", b"", b"", bytes_read, True
                digest.update(chunk)
                append_overlap(
                    partial_buffer,
                    PARTIAL_OFFSET_SIZE[0],
                    PARTIAL_OFFSET_SIZE[0] + PARTIAL_OFFSET_SIZE[1],
                    chunk,
                    chunk_start,
                )
                for sample_start, sample_end, sample_buffer in sample_buffers:
                    append_overlap(sample_buffer, sample_start, sample_end, chunk, chunk_start)
                j.check_if_cancelled()
        full_digest = digest.digest()
        if bytes_read < PARTIAL_OFFSET_SIZE[0] + PARTIAL_OFFSET_SIZE[1]:
            partial_digest = full_digest
        else:
            partial_digest = hasher(partial_buffer).digest()
        if bytes_read <= MIN_FILE_SIZE:
            samples_digest = full_digest
        else:
            samples_hasher = hasher()
            for _sample_start, _sample_end, sample_buffer in sample_buffers:
                samples_hasher.update(sample_buffer)
            samples_digest = samples_hasher.digest()
        return full_digest, partial_digest, samples_digest, bytes_read, False

    @staticmethod
    def _is_directory(info):
        is_dir = getattr(info, "is_dir", None)
        return is_dir() if is_dir is not None else info.filename.endswith("/")

    @staticmethod
    def _problem(container, code, message, member_path=None):
        return ZipProblem(container, code, message, member_path)

    @staticmethod
    def _is_proper_subset(first, second):
        if first == second:
            return False
        return all(count <= second[key] for key, count in first.items())

    def _compare_archives(self, analysis):
        by_signature = defaultdict(list)
        for archive in analysis.archives:
            by_signature[archive.signature].append(archive)
        for same_content in by_signature.values():
            for first, second in combinations(same_content, 2):
                analysis.equivalence_matches.append(engine.Match(first.container, second.container, 100))

        for first, second in combinations(analysis.archives, 2):
            first_entries = first.multiset
            second_entries = second.multiset
            if self._is_proper_subset(first_entries, second_entries):
                analysis.subset_hints.append(ZipSubsetHint(first.container, second.container))
            elif self._is_proper_subset(second_entries, first_entries):
                analysis.subset_hints.append(ZipSubsetHint(second.container, first.container))


def is_virtual_file(file):
    return bool(getattr(file, "is_virtual", False))


def make_readonly_group(group):
    return ReadOnlyGroup(list(group), group.matches)

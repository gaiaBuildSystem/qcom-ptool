# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
"""
Internal representation shared by all input loaders.

Every loader (``loaders/conf.py`` today, ``loaders/yaml.py`` tomorrow) must
return a ``LoadedSpec`` so the rest of ``gen_partition.py`` is independent
of the source format.

``DiskParams`` and ``PartitionEntry`` are type aliases for ``dict[str, str]``
rather than ``TypedDict``s; the XML emitter passes the dicts straight to
``ET.SubElement(..., attrib=...)`` which requires a plain ``dict[str, str]``
at runtime, and aliasing keeps that contract precise without TypedDict's
``total=False`` ``object`` widening. Migrating to dataclasses (with explicit
``to_attrib()`` methods) is a clean follow-up if stronger validation becomes
worth its blast radius.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TypedDict

# Parsed ``--disk`` line normalised into the keys ``ptool`` consumes.
# Expected keys: type, size, SECTOR_SIZE_IN_BYTES, WRITE_PROTECT_BOUNDARY_IN_KB,
# GROW_LAST_PARTITION_TO_FILL_DISK, ALIGN_PARTITIONS_TO_PERFORMANCE_BOUNDARY,
# PERFORMANCE_BOUNDARY_IN_KB. See DISK_PARAMS_DEFAULTS for the canonical set.
DiskParams = dict[str, str]

# Parsed ``--partition`` line normalised for the XML attribute set.
# Expected keys: label, size_in_kb, type, bootable, readonly, filename, fileoffset,
# file_partition_size_kb, sparse, active, successful, unbootable.
# See PARTITION_ENTRY_DEFAULTS for the canonical set.
# ``fileoffset`` and ``file_partition_size_kb`` are only present when the
# corresponding option was given on the partition line.
PartitionEntry = dict[str, str]

# Mapping of physical partition / LUN id (kept as a string for backward
# compatibility with the existing line parser) to its partition entries.
PartitionsByLun = dict[str, list[PartitionEntry]]


# Defaults used by the conf loader (and any future loader that wants to
# inherit the same baseline). Kept as module-level constants so loaders can
# ``.copy()`` from them rather than rebuilding the structure each call.
# ``OrderedDict`` is preserved here because the XML emitter relies on its
# iteration order to produce stable output across Python versions.
DISK_PARAMS_DEFAULTS: DiskParams = OrderedDict(
    {
        "type": "",
        "size": "",
        "SECTOR_SIZE_IN_BYTES": "512",
        "WRITE_PROTECT_BOUNDARY_IN_KB": "65536",
        "GROW_LAST_PARTITION_TO_FILL_DISK": "false",
        "ALIGN_PARTITIONS_TO_PERFORMANCE_BOUNDARY": "true",
        "PERFORMANCE_BOUNDARY_IN_KB": "4",
    }
)


PARTITION_ENTRY_DEFAULTS: PartitionEntry = {
    "label": "",
    "size_in_kb": "",
    "type": "00000000-0000-0000-0000-000000000000",
    "bootable": "false",
    "readonly": "true",
    "filename": "",
    "sparse": "false",
    "active": "false",
    "successful": "false",
    "unbootable": "false",
}


class LoadedSpec(TypedDict):
    """What every loader returns from ``load(path)``."""

    disk: DiskParams
    partitions: PartitionsByLun


def validate_sliced_partitions(disk: DiskParams, partitions: PartitionsByLun) -> None:
    """Validate partition entries that carve slices out of shared files.

    A partition whose data comes from the middle of a file (``fileoffset``)
    must also declare ``file_partition_size_kb``; otherwise the flasher
    cannot know where the slice ends and will stream into whatever follows
    in the file. The one exception is the last partition of a LUN when the
    disk uses grow-to-fill-disk: its rawprogram entry carries zero sectors,
    so both flashers intentionally read from the offset to end-of-file, and
    declaring a size would defeat that behaviour. Slices of the same file on
    one LUN must not overlap either.

    Raises ``ValueError`` with a message naming the offending partition(s);
    loaders wrap it in their own error type.
    """
    sector_size = int(disk.get("SECTOR_SIZE_IN_BYTES", "512") or 512)
    grow_last = disk.get("GROW_LAST_PARTITION_TO_FILL_DISK", "false") == "true"
    for phys_part, entries in partitions.items():
        by_file: dict[str, list[PartitionEntry]] = {}
        for entry in entries:
            filename = entry.get("filename", "")
            if not filename:
                continue
            ## The grow convention (see ptool.py) grows the last partition of a
            ## LUN, regardless of whether it is backed by a file.
            is_growing_last = grow_last and entry is entries[-1]
            if (
                "fileoffset" in entry
                and "file_partition_size_kb" not in entry
                and not is_growing_last
            ):
                raise ValueError(
                    "partition '%s' (lun %s): --file-offset=%s without "
                    "--file-partition-size is ambiguous; declare how many "
                    "sectors of '%s' belong to this slice"
                    % (entry.get("label", "?"), phys_part, entry["fileoffset"], filename)
                )
            by_file.setdefault(filename, []).append(entry)
        for filename, slices in by_file.items():
            sized = [
                (int(e.get("fileoffset", "0")), int(e["file_partition_size_kb"]) * 1024, e)
                for e in slices
                if "file_partition_size_kb" in e
            ]
            sized.sort(key=lambda t: t[0])
            for (off_a, size_a, entry_a), (off_b, _size_b, entry_b) in zip(
                sized, sized[1:]
            ):
                if off_a + size_a // sector_size > off_b:
                    raise ValueError(
                        "partitions '%s' and '%s' (lun %s): slices of '%s' "
                        "overlap (slice at offset %d extends past the next "
                        "slice at offset %d)"
                        % (
                            entry_a.get("label", "?"),
                            entry_b.get("label", "?"),
                            phys_part,
                            filename,
                            off_a,
                            off_b,
                        )
                    )

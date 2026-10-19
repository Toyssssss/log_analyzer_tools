import os
from dataclasses import dataclass
from typing import List, Optional

from .manifest import FileEntry, Manifest
from .source import DiskSource, FlatSource, HybridSource


@dataclass(frozen=True)
class Scope:
    """What the current search covers, and how to read its files.

    One object rather than a key plus a manifest plus three places that each
    rebuild a source: the search, the detail pane and the export path all read
    `source` / `entries` from here, so none of them needs to know whether the
    bytes came from a cache or from a folder the user pointed at.
    """

    manifest: Manifest
    source: object
    key: Optional[str] = None       # None => nothing cached, nothing to clear
    root: str = ''                  # ws_root, or the chosen folder/file
    flat: bool = False              # True => read in place, never extracted
    # Cache keys this scope created for archives found inside `root`. Only a
    # folder scope has them; they are what Clear current has to delete, and
    # what tells the user why the workspace grew after opening a folder.
    local_keys: tuple = ()

    @property
    def entries(self) -> List[FileEntry]:
        return self.manifest.entries


def for_archive(ws_root: str, key: str, manifest: Manifest) -> Scope:
    return Scope(manifest=manifest, source=DiskSource(ws_root, key),
                 key=key, root=ws_root, flat=False)


def for_flat(root: str, manifest: Manifest) -> Scope:
    """In-place scope. The source's base is the chosen folder, or the chosen
    file's parent, so a single file needs no special join at open() time."""
    base = root if os.path.isdir(root) else os.path.dirname(root)
    return Scope(manifest=manifest, source=FlatSource(base),
                 key=None, root=os.path.abspath(root), flat=True)


def for_folder_tree(root: str, manifest: Manifest, ws_root: str,
                    local_keys: tuple = ()) -> Scope:
    """Folder scope that also covers archives extracted out of that folder.

    Entries carry their own cache key (FileEntry.key); the hybrid source routes
    each one to the folder or to the matching cache directory, so the search,
    detail pane and export keep reading `scope.source` with no special casing.

    `key` stays None: this scope owns no single cache entry, and Clear current
    uses `local_keys` instead.
    """
    base = root if os.path.isdir(root) else os.path.dirname(root)
    return Scope(manifest=manifest, source=HybridSource(FlatSource(base), ws_root),
                 key=None, root=os.path.abspath(root), flat=True,
                 local_keys=tuple(local_keys))

"""Private systemd credential ACL validation."""

import os
import struct
from pathlib import Path


def _unit_acl_is_private(path: Path, access: int) -> bool:
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return False
    raw = getxattr(path, "system.posix_acl_access")
    if len(raw) != 44 or struct.unpack_from("<I", raw)[0] != 2:
        return False
    entries = {struct.unpack_from("<HHI", raw, i) for i in range(4, 44, 8)}
    undefined = 0xFFFFFFFF
    return entries == {
        (1, access, undefined),  # root owner
        (2, access, os.geteuid()),  # this service user only
        (4, 0, undefined),  # owning group has no access
        (16, access, undefined),  # ACL mask
        (32, 0, undefined),  # others have no access
    }

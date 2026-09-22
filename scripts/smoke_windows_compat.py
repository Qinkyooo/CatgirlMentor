"""Verify the Windows package does not load RapidFuzz's optional native code.

Run with the packaged Python. The import audit fails before a native loader can
run, exercising the same restriction even on a build host without Smart App Control.
"""

import sys
from pathlib import Path


def reject_rapidfuzz_native(event, args):
    if event == "import" and len(args) > 1:
        name, filename = args[:2]
        if str(name).startswith("rapidfuzz") and str(filename).endswith((".pyd", ".so")):
            raise RuntimeError(f"Unexpected RapidFuzz native import: {name}")


sys.addaudithook(reject_rapidfuzz_native)

from nanobot.utils.file_edit_events import FileDiff  # noqa: E402

diff = FileDiff.from_text("甲\r\n乙\r\n", "甲\n丙\n丁\n")
assert (diff.added, diff.deleted) == (2, 1)
assert any(line == "+丙" for line in diff.unified_lines("before", "after", 3))

import rapidfuzz  # noqa: E402
from rapidfuzz.distance import Indel  # noqa: E402

assert Indel.opcodes.__module__.endswith("_py"), Indel.opcodes.__module__
assert not list(Path(rapidfuzz.__file__).parent.rglob("*.pyd")), "Native RapidFuzz shipped"
print("PASS: file-edit diffs use pure Python; no RapidFuzz native binaries loaded or shipped")

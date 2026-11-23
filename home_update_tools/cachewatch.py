"""Log file events (open, create, write, move, delete) in the audio cache folder, with timestamps."""
import ctypes
import ctypes.util
import os
import struct
import sys
import time

ROOT = sys.argv[1]
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
FLAGS = {0x20: "OPEN", 0x8: "CLOSE_WRITE", 0x80: "MOVED_TO", 0x100: "CREATE", 0x200: "DELETE"}
ISDIR = 0x40000000
fd = libc.inotify_init1(0)
paths: dict[int, str] = {}


def watch(path: str) -> None:
    wd = libc.inotify_add_watch(fd, path.encode(), sum(FLAGS))
    if wd >= 0:
        paths[wd] = path


for dirpath, _dirs, _files in os.walk(ROOT):
    watch(dirpath)
print(f"{time.strftime('%H:%M:%S')} watching {len(paths)} folders under {ROOT}", flush=True)
while True:
    data = os.read(fd, 65536)
    pos = 0
    while pos < len(data):
        wd, mask, _cookie, length = struct.unpack_from("iIII", data, pos)
        name = data[pos + 16 : pos + 16 + length].rstrip(b"\0").decode(errors="replace")
        pos += 16 + length
        folder = paths.get(wd, "?")
        if mask & ISDIR and mask & 0x100:
            watch(os.path.join(folder, name))
        for bit, label in FLAGS.items():
            if mask & bit:
                rel = os.path.relpath(os.path.join(folder, name), ROOT)
                print(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {label:11s} {rel}", flush=True)

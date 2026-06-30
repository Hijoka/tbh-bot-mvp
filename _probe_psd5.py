"""
Hypothesis: the previous byte-find scan missed pointers that straddle chunk boundaries.
Fix: search with 8-byte overlap. Also: scan 8-byte-aligned positions.
Also probe: is the klass pointer returned by class_by_index different from the actual runtime klass?
"""
import ctypes, struct, sys, time
from ctypes import wintypes

PID = 25876
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]

VirtualQueryEx = kernel32.VirtualQueryEx
VirtualQueryEx.restype = ctypes.c_size_t
VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]

ReadProcessMemory = kernel32.ReadProcessMemory
ReadProcessMemory.restype = wintypes.BOOL
ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

OpenProcess = kernel32.OpenProcess
OpenProcess.restype = wintypes.HANDLE
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

CloseHandle = kernel32.CloseHandle

h = OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, PID)
if not h:
    raise SystemExit(f"OpenProcess failed: {ctypes.get_last_error()}")
print(f"[+] opened pid={PID}")

# Enumerate regions
regions = []
addr = 0
while True:
    mbi = MEMORY_BASIC_INFORMATION()
    ret = VirtualQueryEx(h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi))
    if ret == 0:
        break
    if mbi.State == 0x1000 and (mbi.Protect & 0x04 or mbi.Protect & 0x40 or mbi.Protect & 0x02):
        regions.append((mbi.BaseAddress, mbi.RegionSize))
    addr = mbi.BaseAddress + mbi.RegionSize if mbi.BaseAddress else addr + 0x1000

total_size = sum(s for _, s in regions)
print(f"[+] {len(regions)} regions, total {total_size/1024/1024:.1f} MB")

# PlayerSaveData klass pointer (from earlier probe on this same PID)
PSD_KLASS = 0x2bf51cfb2b0

CHUNK = 0x100000  # 1 MB
OVERLAP = 8  # 8 bytes overlap

def scan_chunked(needle_bytes, max_hits=50):
    hits = []
    scanned = 0
    t0 = time.time()
    for base, size in regions:
        offset = 0
        while offset < size:
            this_chunk = min(CHUNK + OVERLAP if offset > 0 else CHUNK, size - offset)
            buf = ctypes.create_string_buffer(this_chunk)
            got = ctypes.c_size_t(0)
            ok = ReadProcessMemory(h, base + offset, buf, this_chunk, ctypes.byref(got))
            if not ok or got.value == 0:
                offset += CHUNK
                continue
            data = buf.raw[:got.value]
            scanned += got.value
            pos = 0
            while True:
                idx = data.find(needle_bytes, pos)
                if idx < 0:
                    break
                addr_hit = base + offset + idx
                if addr_hit != PSD_KLASS:
                    hits.append(addr_hit)
                    if len(hits) >= max_hits:
                        break
                pos = idx + 1
            offset += CHUNK
            if len(hits) >= max_hits:
                break
        if len(hits) >= max_hits:
            break
    elapsed = time.time() - t0
    return hits, scanned, elapsed

# === Test 1: scan for klass pointer (with overlap) ===
print("\n[1] Scanning for PlayerSaveData klass pointer (with 8-byte overlap)...")
hits, scanned, dt = scan_chunked(struct.pack("<Q", PSD_KLASS), max_hits=30)
print(f"    {len(hits)} hits in {scanned/1024/1024:.1f} MB scanned, {dt:.1f}s")
for h_addr in hits[:10]:
    print(f"    - 0x{h_addr:x}")

# === Test 2: scan for "PlayerSaveData" ASCII string ===
print("\n[2] Scanning for ASCII 'PlayerSaveData' string...")
hits2, scanned2, dt2 = scan_chunked(b"PlayerSaveData", max_hits=30)
print(f"    {len(hits2)} hits in {scanned2/1024/1024:.1f} MB scanned, {dt2:.1f}s")
for h_addr in hits2[:10]:
    print(f"    - 0x{h_addr:x}")

# === Test 3: 8-byte ALIGNED scan using ReadProcessMemory for uint64 reads ===
print("\n[3] ALIGNED scan: reading uint64 at every 8-byte-aligned position...")
def read_u64(addr):
    buf = ctypes.create_string_buffer(8)
    got = ctypes.c_size_t(0)
    ok = ReadProcessMemory(h, addr, buf, 8, ctypes.byref(got))
    if not ok or got.value != 8:
        return None
    return struct.unpack("<Q", buf.raw)[0]

# Pick a smaller region to test pattern - look at first 200 MB of heap
heap_regions = [(b, s) for b, s in regions if 0x20000000000 <= b < 0x30000000000 or 0x100000000 <= b < 0x80000000000]
print(f"    heap-like regions: {len(heap_regions)}")

aligned_hits = []
scanned_a = 0
t0 = time.time()
# Use a generous scan limit
MAX_REGIONS = 20  # just test first 20 regions for now
for ridx, (base, size) in enumerate(heap_regions[:MAX_REGIONS]):
    scan_size = min(size, 0x1000000)  # cap each region at 16 MB for this test
    offset = 0
    while offset < scan_size:
        if offset % 0x100000 == 0 and offset > 0:
            print(f"      region{ridx} progress: {offset/0x100000:.0f}MB...", flush=True)
        val = read_u64(base + offset)
        if val is None:
            offset += 8
            continue
        if val == PSD_KLASS:
            aligned_hits.append(base + offset)
            if len(aligned_hits) >= 20:
                break
        offset += 8
        scanned_a += 8
    if len(aligned_hits) >= 20:
        break
    if time.time() - t0 > 30:
        print(f"      [timeout at 30s]")
        break
print(f"    {len(aligned_hits)} aligned hits in {scanned_a/1024/1024:.1f} MB, {time.time()-t0:.1f}s")
for hh in aligned_hits[:10]:
    print(f"    - 0x{hh:x}")

CloseHandle(h)
print("\n[+] done")

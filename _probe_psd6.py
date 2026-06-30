"""
Validate PlayerSaveData candidates and walk inventory.
"""
import ctypes, struct, sys, time
from ctypes import wintypes

PID = 25876
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

OpenProcess = kernel32.OpenProcess
OpenProcess.restype = wintypes.HANDLE
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

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

CloseHandle = kernel32.CloseHandle

h = OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, PID)
if not h:
    raise SystemExit(f"OpenProcess failed: {ctypes.get_last_error()}")
print(f"[+] opened pid={PID}")

def ri64(addr):
    """Read signed 64-bit at addr. Returns None on failure."""
    buf = ctypes.create_string_buffer(8)
    got = ctypes.c_size_t(0)
    ok = ReadProcessMemory(h, addr, buf, 8, ctypes.byref(got))
    if not ok or got.value != 8:
        return None
    return struct.unpack("<q", buf.raw)[0]

def ru64(addr):
    buf = ctypes.create_string_buffer(8)
    got = ctypes.c_size_t(0)
    ok = ReadProcessMemory(h, addr, buf, 8, ctypes.byref(got))
    if not ok or got.value != 8:
        return None
    return struct.unpack("<Q", buf.raw)[0]

def ri32(addr):
    buf = ctypes.create_string_buffer(4)
    got = ctypes.c_size_t(0)
    ok = ReadProcessMemory(h, addr, buf, 4, ctypes.byref(got))
    if not ok or got.value != 4:
        return None
    return struct.unpack("<i", buf.raw)[0]

def read_bytes(addr, n):
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    ok = ReadProcessMemory(h, addr, buf, n, ctypes.byref(got))
    if not ok:
        return None
    return buf.raw[:got.value]

# Step 1: scan for PlayerSaveData klass pointer
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

PSD_KLASS = 0x2bf51cfb2b0
needle = struct.pack("<Q", PSD_KLASS)
CHUNK = 0x100000
OVERLAP = 8

print("[*] scanning for PlayerSaveData klass pointer...")
candidates = []
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
        pos = 0
        while True:
            idx = data.find(needle, pos)
            if idx < 0:
                break
            addr_hit = base + offset + idx
            if addr_hit != PSD_KLASS:
                candidates.append(addr_hit)
            pos = idx + 1
        offset += CHUNK
    if len(candidates) >= 100:
        break
print(f"[+] found {len(candidates)} candidates in {time.time()-t0:.1f}s")
if candidates:
    print(f"    first 10: {[hex(c) for c in candidates[:10]]}")
    print(f"    last 10:  {[hex(c) for c in candidates[-10:]]}")

# Step 2: validate candidates by reading klass at +0 and checking it == PSD_KLASS
print("\n[*] validating klass at +0 for each candidate...")
validated = []
for cand in candidates:
    k = ru64(cand)
    if k == PSD_KLASS:
        validated.append(cand)
print(f"[+] {len(validated)} candidates have klass == PSD_KLASS at +0")
if validated:
    print(f"    first 5: {[hex(v) for v in validated[:5]]}")

# Step 3: read offset +0x10 to +0xC0 for each validated
print("\n[*] reading PSD structure (offsets +0 to +0xC8) for first 5 validated:")
for cand in validated[:5]:
    print(f"\n--- Candidate 0x{cand:x} ---")
    # Read klass at +0
    k = ru64(cand + 0)
    print(f"  +0x00 klass: 0x{k:x}")
    # Walk 0x08 to 0xD8 in 8-byte steps
    for off in range(0x08, 0xE0, 0x08):
        val = ri64(cand + off)
        if val is None:
            print(f"  +0x{off:02x}: <unreadable>")
        else:
            # Distinguish: if it's a small int or looks like a pointer
            hex_v = f"0x{val & 0xFFFFFFFFFFFFFFFF:x}"
            if 0x100000000 <= val < 0x800000000000:
                tag = " [PTR]"
            elif -10000000 <= val <= 10000000:
                tag = ""
            else:
                tag = ""
            print(f"  +0x{off:02x}: {val} {tag}{hex_v if val > 0xFFFF or val < -0xFFFF else ''}")

# Step 4: try CURRENCIES at +0x40
print("\n[*] probing +0x40 as CURRENCIES pointer for first validated:")
if validated:
    cand = validated[0]
    ccy = ru64(cand + 0x40)
    print(f"  +0x40 raw: 0x{ccy:x}" if ccy else f"  +0x40: {ccy}")
    if ccy and 0x100000000 < ccy < 0x800000000000:
        print(f"  looks like a pointer, dereferencing 0x{ccy:x}...")
        for off in range(0, 0x40, 0x8):
            val = ri64(ccy + off)
            print(f"    +0x{off:02x}: {val}")

CloseHandle(h)
print("\n[+] done")

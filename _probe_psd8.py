"""
Filter PSD candidates to those in heap range (not IL2CPP metadata).
Also: try to identify which one is the "live" PSD by checking structure.
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
class MBI(ctypes.Structure):
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
VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
ReadProcessMemory = kernel32.ReadProcessMemory
ReadProcessMemory.restype = wintypes.BOOL
ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
CloseHandle = kernel32.CloseHandle

h = OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, PID)

def ri64(addr):
    buf = ctypes.create_string_buffer(8)
    got = ctypes.c_size_t(0)
    if not ReadProcessMemory(h, addr, buf, 8, ctypes.byref(got)) or got.value != 8:
        return None
    return struct.unpack("<q", buf.raw)[0]

PSD_KLASS = 0x2bf51cfb2b0

# Try to find IL2CPP module base via ProcessFirstModule
class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("ModBaseAddr", ctypes.c_void_p),
        ("ModBaseSize", wintypes.DWORD),
        ("th32ThreadID", wintypes.HANDLE),
        ("szModule", ctypes.c_char * 256),
        ("szExePath", ctypes.c_char * 260),
    ]
TH32CS_SNAPMODULE = 0x00000008
CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
CreateToolhelp32Snapshot.restype = wintypes.HANDLE
CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
Module32First = kernel32.Module32First
Module32First.restype = wintypes.BOOL
Module32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32)]
Module32Next = kernel32.Module32Next
Module32Next.restype = wintypes.BOOL
Module32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32)]

snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, PID)
me = MODULEENTRY32()
me.dwSize = ctypes.sizeof(me)
mods = []
if Module32First(snap, ctypes.byref(me)):
    while True:
        mods.append((me.szModule.decode(errors='replace'), me.ModBaseAddr, me.ModBaseSize))
        if not Module32Next(snap, ctypes.byref(me)):
            break
CloseHandle(snap)
print("[+] loaded modules:")
for name, base, size in mods:
    if size > 100000:
        print(f"    {name}: base=0x{base:x} size=0x{size:x}")
ga_base = ga_size = None
for name, base, size in mods:
    if "GameAssembly" in name or "gameassembly" in name.lower():
        ga_base = base
        ga_size = size
        break

if ga_base is None:
    print("[-] GameAssembly not found, using last-resort guess")
    ga_base = 0x2bf500000000
    ga_size = 0x10000000
print(f"[+] GA base=0x{ga_base:x} size=0x{ga_size:x}")

heap_lo = 0x10000
heap_hi = ga_base  # heap = below IL2CPP module
print(f"[+] heap range: 0x{heap_lo:x} - 0x{heap_hi:x} ({heap_hi-heap_lo} bytes = {(heap_hi-heap_lo)/1024/1024:.0f} MB)")

# Now scan, filter candidates to heap range
regions = []
addr = 0
while True:
    mbi = MBI()
    ret = VirtualQueryEx(h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi))
    if ret == 0:
        break
    if mbi.State == 0x1000 and (mbi.Protect & 0x04 or mbi.Protect & 0x40 or mbi.Protect & 0x02):
        regions.append((mbi.BaseAddress, mbi.RegionSize))
    addr = mbi.BaseAddress + mbi.RegionSize if mbi.BaseAddress else addr + 0x1000

# Only scan heap regions
heap_regions = [(b, s) for b, s in regions if b < ga_base]
print(f"[+] {len(heap_regions)} heap regions")

needle = struct.pack("<Q", PSD_KLASS)
CHUNK = 0x100000
OVERLAP = 8
candidates = []
t0 = time.time()
for base, size in heap_regions:
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
            if addr_hit != PSD_KLASS and addr_hit < ga_base:
                candidates.append(addr_hit)
            pos = idx + 1
        offset += CHUNK
print(f"[+] {len(candidates)} heap-only candidates in {time.time()-t0:.1f}s")

# Validate each has klass at +0
validated = []
for cand in candidates:
    k = ri64(cand)
    if k == PSD_KLASS:
        validated.append(cand)
print(f"[+] {len(validated)} candidates have klass == PSD_KLASS at +0")
# Group by address
seen = set()
unique_validated = []
for v in validated:
    if v not in seen:
        seen.add(v)
        unique_validated.append(v)

# Sort and show
unique_validated.sort()
print(f"\n[*] unique validated heap candidates ({len(unique_validated)}):")
for v in unique_validated[:30]:
    # Check +0x40 to see what it points to
    p40 = ri64(v + 0x40)
    p40_desc = ""
    if p40 is not None:
        if 0x100000000 < p40 < 0x800000000000:
            # Could be currencies (might be unique per instance) or klass re-read
            p40_desc = f"+0x40=0x{p40:x}"
        else:
            p40_desc = f"+0x40={p40}"
    print(f"  0x{v:x}  {p40_desc}")

CloseHandle(h)

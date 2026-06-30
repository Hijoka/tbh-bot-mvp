"""
Deeper probe of PlayerSaveData candidate 0x2bf00853c50.
- Walk entire struct (0x0 to 0x1000)
- Identify which pointer at which offset points to a "list of items" or "dictionary"
- Try to interpret the data
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
print(f"[+] pid={PID}")

def ri64(addr):
    buf = ctypes.create_string_buffer(8)
    got = ctypes.c_size_t(0)
    if not ReadProcessMemory(h, addr, buf, 8, ctypes.byref(got)) or got.value != 8:
        return None
    return struct.unpack("<q", buf.raw)[0]

def ri32(addr):
    buf = ctypes.create_string_buffer(4)
    got = ctypes.c_size_t(0)
    if not ReadProcessMemory(h, addr, buf, 4, ctypes.byref(got)) or got.value != 4:
        return None
    return struct.unpack("<i", buf.raw)[0]

def read_bytes(addr, n):
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    if not ReadProcessMemory(h, addr, buf, n, ctypes.byref(got)):
        return None
    return buf.raw[:got.value]

PSD_LIVE = 0x2bf00853c50

# === Re-confirm the live PSD's klass ===
PSD_KLASS = 0x2bf51cfb2b0
k = ri64(PSD_LIVE)
print(f"[+] 0x{PSD_LIVE:x} +0x00 klass: 0x{k:x}  (expected 0x{PSD_KLASS:x}) -> {'OK' if k == PSD_KLASS else 'WRONG'}")

# === Dump a HUGE range: 0x0 to 0x1000 ===
print(f"\n[*] dumping 0x{PSD_LIVE:x}+0x00 to +0x1000 (256 qwords):")
for off in range(0x00, 0x1000, 0x08):
    v = ri64(PSD_LIVE + off)
    if v is None:
        print(f"  +0x{off:04x}: <unreadable>")
        continue
    # Pretty-print
    is_ptr = 0x100000000 <= v < 0x800000000000
    tag = " [PTR]" if is_ptr else ""
    if -10000000 < v < 10000000:
        vs = f"{v}"
    elif is_ptr:
        vs = f"0x{v:x}"
    else:
        vs = f"{v} (0x{v & 0xFFFFFFFFFFFFFFFF:x})"
    print(f"  +0x{off:04x}: {vs}{tag}")

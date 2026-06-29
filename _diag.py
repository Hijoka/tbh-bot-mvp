import sys, traceback
sys.path.insert(0, '.')
from memory_attach import _current_pid, GameAttach, _CALIB
from vendor.shared.memory import open_process, module_base, close, Reader
from vendor.il2cpp.typeinfo import ga_module, table_base, class_by_index, class_name
from vendor.config.offsets import EAggregateType

print("[diag] seed anchor_rva=0x%x idx_ut=%d" % (_CALIB['anchor_rva'], _CALIB['idx_ut']))
pid = _current_pid()
print("[diag] find_pid(TaskBarHero.exe) = %s" % pid)
if pid is None:
    print("[diag] GAME NOT RUNNING -- start TBH first"); sys.exit(0)
try:
    h = open_process(pid)
    print("[diag] open_process -> handle=%s" % h)
    if not h: print("[diag] FAILED open_process"); sys.exit(1)
    base = module_base(pid)
    print("[diag] module_base(GameAssembly.dll) = %s" % (hex(base) if base else "None"))
    if not base: close(h); sys.exit(1)
    ga, sz = ga_module(pid)
    print("[diag] ga_module -> base=%s size=%s" % (hex(ga) if ga else "None", hex(sz) if sz else "None"))
    if not ga: close(h); sys.exit(1)
    r = Reader(h)
    tb = table_base(r, ga, _CALIB['anchor_rva'])
    print("[diag] table_base = %s" % (hex(tb) if tb else "None"))
    if not tb: close(h); sys.exit(1)
    K = class_by_index(r, tb, _CALIB['idx_ut'])
    print("[diag] class_by_index(idx=%d) = %s" % (_CALIB['idx_ut'], hex(K) if K else "None"))
    if not K: close(h); sys.exit(1)
    nm = class_name(r, K)
    print("[diag] class_name = %r" % nm)
    if nm is None:
        print("[diag] *** BUILD MISMATCH ***"); close(h); sys.exit(1)
    a = GameAttach()
    a.handle, a.pid, a.ga_base, a.ga_size, a.tbase, a.ut_klass = h, pid, ga, sz, tb, K
    ob = a.read_aggregate(EAggregateType.BoxObtain)
    op = a.read_aggregate(EAggregateType.BoxOpen)
    pending = max(0, ob-op) if (ob is not None and op is not None) else None
    print("[diag] BoxObtain=%s  BoxOpen=%s  pending=%s" % (ob, op, pending))
    close(h)
except Exception:
    traceback.print_exc()
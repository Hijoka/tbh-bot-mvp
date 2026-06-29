"""_diag2.py — outer-dict dump + alt-class probes.

We know BoxOpen=16 returns 615 but BoxObtain=3 returns None. Two theories:
  (a) BoxObtain key just isn't in this user's AggregateManager (sparse dict, 0 omitted).
  (b) We're reading from the wrong class entirely (class_name='vs' is suspicious).

This script:
  1. Walks the outer Dict<EAggregateType, Dict> for the class we currently have
     and prints ALL keys + values (subkey, count). If we see BoxOpen but not
     BoxObtain, that's theory (a). If we see neither or random keys, that's (b).
  2. Tries idx_ut values in [2825, 2826, 2827, 2828, 2829] (the canonical
     TypeDefIndex for AggregateManager is build-dependent, ±2 around 2827).
     For each, print class_name. If any returns a name that looks right
     (longer than 4 chars or contains "Agg" / "Manager"), that's the real one.

Usage: py _diag2.py
"""
import sys, traceback
sys.path.insert(0, '.')
from memory_attach import _current_pid, GameAttach, _CALIB
from vendor.shared.memory import open_process, module_base, close, Reader
from vendor.il2cpp.typeinfo import ga_module, table_base, class_by_index, class_name, bbwf_from_klass
from vendor.config.offsets import EAggregateType, AggregateManager

pid = _current_pid()
print("[diag2] find_pid = %s" % pid)
if pid is None:
    print("[diag2] GAME NOT RUNNING"); sys.exit(0)

try:
    h = open_process(pid)
    base = module_base(pid)
    ga, sz = ga_module(pid)
    r = Reader(h)
    tb = table_base(r, ga, _CALIB['anchor_rva'])
    print("[diag2] attach chain OK (ga=%s tb=%s)" % (hex(ga), hex(tb)))

    # 1. Dump outer dict for current idx_ut=2827
    K = class_by_index(r, tb, _CALIB['idx_ut'])
    nm = class_name(r, K)
    print("\n[diag2] current idx_ut=%d -> klass=%s name=%r" % (_CALIB['idx_ut'], hex(K), nm))
    inst = bbwf_from_klass(r, K)
    print("[diag2]   instance = %s" % (hex(inst) if inst else "None"))
    if inst:
        outer = r.rptr(inst + AggregateManager.AGGREGATES)
        print("[diag2]   AGGREGATES outer = %s" % (hex(outer) if outer else "None"))
        if outer:
            print("[diag2]   walking outer Dict<EAggregateType, Dict>:")
            for k, v in r.dict8b_items(outer):
                # k is the EAggregateType int; v is pointer to inner Dict
                name = EAggregateType(k).name if k in [m.value for m in EAggregateType] else "?"
                if v and v > 0x10000:
                    sub_keys = list(r.dict8b_items(v))
                    print("[diag2]     key=%d (%s) -> inner=%s subkeys=%s" % (k, name, hex(v), sub_keys))
                else:
                    print("[diag2]     key=%d (%s) -> inner=%s" % (k, name, hex(v) if v else "None"))

    # 2. Probe idx_ut in [-2..+2] around 2827
    print("\n[diag2] probing TypeDefIndex candidates around 2827:")
    for idx in range(_CALIB['idx_ut'] - 2, _CALIB['idx_ut'] + 3):
        K2 = class_by_index(r, tb, idx)
        if not K2:
            print("[diag2]   idx=%d -> None" % idx); continue
        nm2 = class_name(r, K2)
        print("[diag2]   idx=%d -> name=%r" % (idx, nm2))

    close(h)
except Exception:
    traceback.print_exc()
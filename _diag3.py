"""_diag3.py — full outer-dict dump + scan for missing BoxObtain key.

Theory: BoxObtain=3 might be stored at a different EAggregateType value on
this build, OR as a subkey under an existing entry, OR in a second dict.

This script:
  1. Walks the FULL outer dict and dumps all key/int values with hex+decimal.
  2. Tries reading key=3 specifically (BoxObtain) and prints whether it's
     missing or just empty.
  3. Tries inner dict SubKey=0/1/2/3 for all keys to map SubKey to a label.
  4. Lists the BoxOpen subkeys with both decimal and hex to spot patterns
     (could SubKey be a hash? tier id?).

Usage: py _diag3.py
"""
import sys, traceback
sys.path.insert(0, '.')
from memory_attach import _current_pid, GameAttach, _CALIB
from vendor.shared.memory import open_process, module_base, close, Reader
from vendor.il2cpp.typeinfo import ga_module, table_base, class_by_index, class_name
from vendor.il2cpp.finder import bbwf_from_klass
from vendor.config.offsets import EAggregateType, AggregateManager

pid = _current_pid()
print("[diag3] find_pid = %s" % pid)
if pid is None:
    print("[diag3] GAME NOT RUNNING"); sys.exit(0)

try:
    h = open_process(pid)
    base = module_base(pid)
    ga, sz = ga_module(pid)
    r = Reader(h)
    tb = table_base(r, ga, _CALIB['anchor_rva'])
    K = class_by_index(r, tb, _CALIB['idx_ut'])
    inst = bbwf_from_klass(r, K)
    outer = r.rptr(inst + AggregateManager.AGGREGATES)
    print("[diag3] outer dict at %s, walking..." % hex(outer))

    # 1. Full dump
    all_keys = []
    for k, v in r.dict8b_items(outer):
        all_keys.append(k)
        name = EAggregateType(k).name if k in [m.value for m in EAggregateType] else "??"
        if v and v > 0x10000:
            sub = list(r.dict8b_items(v))
            print("[diag3] key=%d (0x%x, %s) -> inner=%s" % (k, k, name, hex(v)))
            for sk, sv in sub:
                sk_name = "?"
                if sk == 0: sk_name = "SubKey0(total?)"
                elif sk == 1: sk_name = "SubKey1"
                elif sk == 2: sk_name = "SubKey2"
                elif sk == 3: sk_name = "SubKey3"
                print("[diag3]   sk=%d (0x%x, %s) -> %d (0x%x)" % (sk, sk, sk_name, sv if sv else 0, sv if sv else 0))
        else:
            print("[diag3] key=%d (0x%x, %s) -> inner=%s" % (k, k, name, hex(v) if v else "None"))

    print("\n[diag3] all outer keys (sorted): %s" % sorted(all_keys))
    print("[diag3] BoxObtain=3 in outer? %s" % (3 in all_keys))
    print("[diag3] BoxOpen=16 in outer? %s" % (16 in all_keys))

    # 2. Specifically look for any inner dict whose SubKey=0 == 0
    #    (that would be a fresh BoxObtain-style counter that hasn't ticked yet)
    #    Actually we want to look for keys that might be chests obtained (small ints).
    print("\n[diag3] entries with SubKey=0 == 0 (potential BoxObtain-not-yet-incremented):")
    for k, v in r.dict8b_items(outer):
        if not v or v <= 0x10000: continue
        for sk, sv in r.dict8b_items(v):
            if sk == 0 and sv == 0:
                name = EAggregateType(k).name if k in [m.value for m in EAggregateType] else "??"
                print("[diag3]   key=%d (%s) SubKey=0 == 0  (inner has %d total subkeys)" % (k, name, len(list(r.dict8b_items(v)))))

    close(h)
except Exception:
    traceback.print_exc()
r"""probe_pending.py — one-shot validation of alandsamuel's ChestReader path.

Reads BoxObtain - BoxOpen from TBH process memory, prints the result,
exits. If pending comes back as a non-negative integer (not None), the
offsets are valid on this build and the chest_mvp.py vendored code is
safe to lift into our bot.

USAGE:
    cd C:\Users\thomas\tbh-bot
    py probe_pending.py
"""
import sys
import time
from pathlib import Path

# Add alandsamuel's chest_mvp.py to import path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse the GameAttach + pending_chests logic from chest_mvp.py
# (NO clicks -- pure read)
import chest_mvp as cm

print("[probe] attempting to attach to TaskBarHero.exe (read-only)...")
attach = cm.GameAttach()
ok = attach.attach()
if not ok:
    print("[probe] FAILED to attach to TaskBarHero.exe")
    print("[probe]   (is the game running? wrong build / offsets?)")
    sys.exit(1)

print(f"[probe] attached pid={attach.pid} ga_base=0x{attach.ga_base:x}")
print(f"[probe] AggregateManager klass at 0x{attach.ut_klass:x}")
print()

# Poll pending 5 times at 1Hz to see if the read is stable.
for i in range(5):
    ob = attach.read_aggregate(cm.EAggregateType.BoxObtain)
    op = attach.read_aggregate(cm.EAggregateType.BoxOpen)
    pending = (max(0, ob - op) if ob is not None and op is not None
               else None)
    print(f"[probe] t={i}s  BoxObtain={ob}  BoxOpen={op}  pending={pending}")
    time.sleep(1.0)

attach.detach()
print()
print("[probe] done. If pending is an integer (not None), vendor is valid.")

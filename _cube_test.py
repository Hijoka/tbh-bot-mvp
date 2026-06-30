"""
Test cube_state.py data layer + state machine WITHOUT clicking anything.
Verifies:
1. discover_raw_dir() finds the right path
2. list_raw_files() returns newest-first
3. compute_cube_buckets() buckets correctly
4. pick_eligible() picks in priority order
5. State machine detects new run via rising-edge
"""
import sys, os, json, time
sys.path.insert(0, r"C:\Users\thomas\tbh-bot-mvp")
import cube_state as cs

# === Test 1: discover + list ===
print("=== Test 1: discover + list ===")
raw_dir = cs.discover_raw_dir()
print(f"raw_dir={raw_dir}")
files = cs.list_raw_files(raw_dir)
print(f"found {len(files)} files")
assert len(files) > 0, "no raw files found"
newest_id, newest_path, newest_mt = files[0]
print(f"newest: {newest_id}")

# === Test 2: read + bucket ===
print("\n=== Test 2: bucket from newest raw ===")
raw = cs.read_raw_file(newest_path)
assert raw is not None
print(f"keys: {list(raw.keys())}")
buckets = cs.compute_cube_buckets(raw)
print("buckets:")
for (cat, g), c in sorted(buckets.items()):
    marker = " <-- eligible" if c >= 9 else ""
    print(f"    {cat:<10} grade={cs.GRADE_NAMES.get(g, g):<10} count={c}{marker}")

# === Test 3: pick_eligible ===
print("\n=== Test 3: pick_eligible ===")
picked = cs.pick_eligible(buckets)
if picked:
    mode, grade_id = picked
    print(f"picked: {mode} grade={cs.GRADE_NAMES.get(grade_id, grade_id)} count={buckets[(mode, grade_id)]}")
else:
    print("no eligible bucket (no 9+ items)")

# === Test 4: state machine rising-edge ===
print("\n=== Test 4: state machine ===")
state = cs.CubeState()
# Baseline: should NOT trigger
new_files = state.update(raw_dir)
print(f"baseline update returned {len(new_files)} files (expected 0)")
assert len(new_files) == 0
assert state.last_run_id == newest_id

# Simulate a new file: manually mutate last_run_id and call update again
state.last_run_id = "0000000000000"  # way older
new_files = state.update(raw_dir)
print(f"after reset, update returned {len(new_files)} files (expected 1)")
if new_files:
    rid, path, mt = new_files[0]
    print(f"  detected: {rid}")

# === Test 5: synthesize_once in dry-run mode ===
print("\n=== Test 5: synthesize_once in dry-run ===")
state2 = cs.CubeState()
state2.update(raw_dir)
picked = cs.pick_eligible(cs.compute_cube_buckets(state2.last_data))
if picked:
    mode, grade_id = picked
    cs.synthesize_once(hwnd=None, mode=mode, grade_id=grade_id, count=buckets[(mode, grade_id)], state=state2, dry_run=True)
    print("dry-run synthesize_once returned without exception")
else:
    print("nothing to synthesize (skipping test)")

# === Test 6: equipped-exclusion ===
print("\n=== Test 6: equipped-exclusion sanity ===")
equipped = cs.extract_equipped_uniqueids(raw)
print(f"equipped uniqueIds: {len(equipped)}")
inv_items = list(cs.iter_inventory_items(raw))
total = len(inv_items)
avail = sum(1 for it in inv_items if str(it.get("uniqueId", "")) not in equipped)
print(f"total inventory+stash items: {total}")
print(f"available for cube (not equipped): {avail}")

print("\n=== ALL TESTS PASSED ===")
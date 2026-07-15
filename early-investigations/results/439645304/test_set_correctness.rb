# Exercise many khash rebuilds (small->hash at 5, then repeated grows) with
# ordinary keys, and verify Set semantics are preserved by the patched rebuild.
ok = true
def check(c, msg); unless c; puts "FAIL: #{msg}"; end; c; end

# 1) Insert 500 distinct ints + 500 distinct strings, with duplicates interleaved
s = Set.new
expected = []
500.times do |i|
  s.add(i); s.add(i)            # duplicate add must not grow size
  s.add("str#{i}"); s.add("str#{i}")
  expected << i << "str#{i}"
end
ok &= check(s.size == 1000, "size after inserts: #{s.size} != 1000")
# every expected member present (survived all rebuilds)
miss = expected.reject { |e| s.include?(e) }
ok &= check(miss.empty?, "missing #{miss.size} members after rebuilds: #{miss.first(5).inspect}")
# non-members absent
ok &= check(!s.include?(99999), "false positive 99999")
ok &= check(!s.include?("nope"), "false positive 'nope'")

# 2) Deletion then re-grow
250.times { |i| s.delete(i) }
ok &= check(s.size == 750, "size after deleting 250 ints: #{s.size} != 750")
ok &= check(!s.include?(0) && s.include?(499), "deletion correctness")
250.times { |i| s.add(100000 + i) }  # force more rebuilds after deletes
ok &= check(s.size == 1000, "size after re-adding: #{s.size} != 1000")

# 3) Set algebra sanity
a = Set.new(0..99)
b = Set.new(50..149)
ok &= check((a & b).size == 50, "intersection size")
ok &= check((a | b).size == 150, "union size")
ok &= check((a - b).size == 50, "difference size")

puts ok ? "ALL_SET_TESTS_PASS" : "SET_TESTS_FAILED"

# Isolate REBUILD migration correctness from the co-resident string-GC bug.
GC.disable rescue nil
ok = true
def check(c,m); puts("FAIL: #{m}") unless c; c; end

# (A) strings, GC disabled so heap members can't be collected -> any loss is rebuild logic
s = Set.new
2000.times { |i| s.add("str#{i}"); s.add("str#{i}") }   # forces ~9 rebuilds, dup adds
ok &= check(s.size == 2000, "A size #{s.size} != 2000")
miss = (0...2000).reject { |i| s.include?("str#{i}") }
ok &= check(miss.empty?, "A missing #{miss.size}: #{miss.first(5).map{|i|"str#{i}"}.inspect}")

# (B) immediate keys (integers) never GC'd -> pure rebuild logic, even without GC.disable
GC.enable rescue nil
t = Set.new
5000.times { |i| t.add(i); t.add(i) }
ok &= check(t.size == 5000, "B size #{t.size} != 5000")
bad = (0...5000).reject { |i| t.include?(i) }
ok &= check(bad.empty?, "B missing #{bad.size}: #{bad.first(5).inspect}")
ok &= check(!t.include?(5000) && !t.include?(-1), "B false positives")
# delete half then regrow
2500.times { |i| t.delete(2*i) }
ok &= check(t.size == 2500, "B size after delete #{t.size} != 2500")
2500.times { |i| t.add(10000+i) }
ok &= check(t.size == 5000, "B size after regrow #{t.size} != 5000")

puts ok ? "REBUILD_LOGIC_OK" : "REBUILD_LOGIC_FAIL"

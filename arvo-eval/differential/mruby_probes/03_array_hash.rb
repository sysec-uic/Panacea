# Deterministic array/hash operations.
a = (1..10).to_a.map { |x| x * x }
puts a.inject(0) { |acc, x| acc + x }
h = {}
a.each_with_index { |v, i| h[i] = v }
puts h.keys.sort.join(",")
puts h.values.sort.reverse.join(",")

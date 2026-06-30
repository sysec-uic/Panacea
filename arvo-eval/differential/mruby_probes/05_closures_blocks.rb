# Closures, upvalues, and nested blocks -- exercises the env/stack machinery
# that the pool/stack-escape (SUAR) family stresses. Deterministic output.
def make_counter(start)
  count = start
  -> { count += 1 }
end

c = make_counter(10)
puts c.call
puts c.call
puts c.call

adders = (1..5).map { |n| ->(x) { x + n } }
puts adders.map { |f| f.call(100) }.join(",")

acc = []
3.times { |i| 3.times { |j| acc << i * 3 + j } }
puts acc.join(",")

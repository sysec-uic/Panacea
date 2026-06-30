# GC-stressing allocation loop. Exercises mark/sweep paths -- the family the
# mruby-set khash and write-barrier UAF bugs live in. Deterministic output.
total = 0
1000.times do |i|
  a = []
  10.times { |j| a << (i * j).to_s }
  total += a.join.length
end
GC.start
puts total
puts (1..500).map { |n| { n => n * n } }.length
GC.start
puts "gc-ok"

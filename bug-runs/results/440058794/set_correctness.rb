# GC-stress Set correctness test. Targets the missing-write-barrier UAF:
# heap keys added to an already-black Set get swept unless barriered.
$fail = 0
def check(name, ok)
  unless ok
    $fail += 1
    puts "FAIL: #{name}"
  end
end

GC.start

# 1) Many STRING keys, with GC churn forcing sweeps between inserts.
N = 2000
s = Set.new
ref = []
N.times do |i|
  k = "key-#{i}-#{'x'*(i % 7)}"
  s << k
  ref << k
  # allocate garbage + GC to provoke sweeps while set is black
  if i % 16 == 0
    100.times { "garbage#{i}" * 3 }
    GC.start
  end
end
GC.start
check("string set size", s.size == N)
missing = ref.reject { |k| s.include?(k) }
check("no string keys lost (missing=#{missing.size})", missing.empty?)

# 2) Custom objects with a Ruby #hash method => during khash rebuild the rehash
#    calls #hash (mrb_funcall) which allocates and can GC mid-rebuild. This is the
#    exact crash path in the PoC.
class K
  attr_reader :v
  def initialize(v) @v = v end
  def hash; @v.hash ^ 0x55; end
  def eql?(o); o.is_a?(K) && o.v == v; end
  def ==(o); eql?(o); end
end
cs = Set.new
keep = []
1000.times do |i|
  o = K.new(i)
  cs << o
  keep << o
  GC.start if i % 8 == 0
end
GC.start
check("custom-obj set size", cs.size == 1000)
lost = keep.reject { |o| cs.include?(o) }
check("no custom-obj keys lost (lost=#{lost.size})", lost.empty?)

# 3) Set operations under GC stress
a = Set.new; b = Set.new
500.times { |i| a << "a#{i}"; b << "b#{(i/2)}" }
200.times { |i| a << "c#{i}"; b << "c#{i}" }  # overlap
GC.start
uni = a | b
inter = a & b
diff = a - b
GC.start
check("union size", uni.size == a.size + b.size - inter.size)
check("intersection contains overlap", (0...200).all? { |i| inter.include?("c#{i}") })
check("difference excludes overlap", (0...200).none? { |i| diff.include?("c#{i}") })
check("difference keeps a-only", diff.include?("a0") && diff.include?("a499"))

# 4) Set[...] literal create path (s_create) under GC
GC.start
lit = Set[*(0...300).map { |i| "lit#{i}" }]
GC.start
check("Set[] literal size", lit.size == 300)
check("Set[] literal no loss", (0...300).all? { |i| lit.include?("lit#{i}") })

if $fail == 0
  puts "ALL PASS"
else
  puts "TOTAL FAILURES: #{$fail}"
end

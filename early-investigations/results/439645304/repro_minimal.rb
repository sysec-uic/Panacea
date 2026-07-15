class K
  def initialize(i) @i = i end
  # custom hash forces mrb_obj_hash_code default branch -> Ruby funcall,
  # and allocates heavily to trigger incremental GC *during* a Set rehash
  def hash
    a = []
    400.times { a << ("x" * 40) }
    @i
  end
  def eql?(o) o.is_a?(K) && o.instance_variable_get(:@i) == @i end
end

s = Set.new
50.times { |i| s.add(K.new(i)) }
p s.size

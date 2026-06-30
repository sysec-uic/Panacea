# Exception unwinding (raise/rescue/ensure) + symbol interning. Exercises the
# unwind path and the symbol table. Deterministic output.
def risky(n)
  raise ArgumentError, "neg" if n < 0
  n * 2
end

log = []
[-1, 2, -3, 4].each do |n|
  begin
    log << risky(n)
  rescue ArgumentError => e
    log << e.message
  ensure
    log << :done
  end
end
puts log.join(",")

syms = [:alpha, :beta, :alpha, :gamma]
puts syms.map(&:to_s).sort.uniq.join(",")
puts syms.uniq.length

#!/usr/bin/env python3
"""Generate the paper's figures from the measured data. Outputs PDFs into figs/."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams

rcParams.update({
    "font.family": "serif", "font.size": 9, "axes.edgecolor": "#555555",
    "axes.linewidth": 0.8, "xtick.color": "#333333", "ytick.color": "#333333",
    "axes.labelcolor": "#222222", "text.color": "#222222", "figure.dpi": 200,
})
CONTROL = "#9A4933"   # rust
TREAT   = "#556B2E"   # olive
LIGHT   = "#D9D7C7"   # muted "seen" backdrop
OUT = "figs/"

# ---- Fig 1: effort (attempts + tokens), matched pairs n=26 ----
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6.4, 2.7))
for ax, vals, title, ylim, fmt in [
    (ax1, [1.19, 1.00], "Mean attempts (of 5)", (0.9, 1.28), "{:.2f}"),
    (ax2, [2.491, 2.356], "Mean tokens per bug (millions)", (2.25, 2.58), "{:.2f}")]:
    bars = ax.bar(["control", "treatment"], vals, color=[CONTROL, TREAT], width=0.55)
    ax.set_ylim(*ylim); ax.set_title(title, fontsize=9, color="#555555")
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.tick_params(length=3)
    for b, v in zip(bars, vals):
        ax.text(b.get_x()+b.get_width()/2, v+(ylim[1]-ylim[0])*0.02, fmt.format(v),
                ha="center", va="bottom", fontsize=9)
fig.tight_layout()
fig.savefig(OUT+"effort.pdf", bbox_inches="tight"); plt.close(fig)

# ---- Fig 2: bug-family distribution (27 solved bugs) ----
fams = ["bigint stack/pool\nlifetime", "MSan uninitialized\nvalue",
        "bignum boxing\ntype confusion", "bigint Karatsuba\nscratch-buffer",
        "GC write-barrier /\narena", "other, one-off"]
counts = [10, 6, 3, 2, 2, 4]
order = sorted(range(len(counts)), key=lambda i: counts[i])
fig, ax = plt.subplots(figsize=(6.2, 2.9))
bars = ax.barh([fams[i] for i in order], [counts[i] for i in order],
               color=TREAT, height=0.62)
for i, b in zip(order, bars):
    ax.text(counts[i]+0.12, b.get_y()+b.get_height()/2, str(counts[i]),
            va="center", fontsize=9)
ax.set_xlim(0, 11); ax.set_xlabel("solved bugs")
ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
ax.tick_params(length=3, labelsize=8)
fig.tight_layout()
fig.savefig(OUT+"families.pdf", bbox_inches="tight"); plt.close(fig)

# ---- Fig 3: local 24B model, solved vs seen by family (single attempt) ----
lf = ["Stack use-after-return", "Invalid-free", "Stack-buffer-overflow",
      "Segfault", "Heap-buffer-overflow", "MSan uninitialized value",
      "Heap use-after-free"]
seen   = [4, 1, 1, 3, 4, 6, 1]
solved = [4, 1, 1, 2, 2, 2, 0]
y = range(len(lf))[::-1]
fig, ax = plt.subplots(figsize=(6.2, 3.0))
ax.barh(list(y), seen, color=LIGHT, height=0.66, label="seen")
ax.barh(list(y), solved, color=TREAT, height=0.66, label="solved")
ax.set_yticks(list(y)); ax.set_yticklabels(lf, fontsize=8)
for yi, s, se in zip(y, solved, seen):
    ax.text(se+0.1, yi, f"{s}/{se}", va="center", fontsize=8, color="#333333")
ax.set_xlim(0, 6.8); ax.set_xlabel("bugs")
ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
ax.tick_params(length=3)
ax.legend(frameon=False, fontsize=8, loc="lower right")
fig.tight_layout()
fig.savefig(OUT+"local.pdf", bbox_inches="tight"); plt.close(fig)

print("wrote figs/effort.pdf figs/families.pdf figs/local.pdf")

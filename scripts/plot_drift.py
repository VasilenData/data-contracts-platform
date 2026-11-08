"""Chart per-month violation rates against the contract's tolerance (reads measurements/real_months.json)."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
data = json.loads((ROOT / "measurements" / "real_months.json").read_text())
months = [m["month"][5:] for m in data["months"]]
PAPER, INK, MUTED, MINT, AMBER = "#111315", "#ECEFF1", "#A3ACB3", "#7EE0B8", "#F0B86E"
panels = [
    ("allowed:VendorID", "VendorID not in {1, 2}", 0.0, "strict, no tolerance"),
    ("min:fare_amount", "Negative fare_amount", 0.01, "tolerance 1%"),
    ("check:dropoff_not_before_pickup", "Dropoff before pickup", 0.00001, "tolerance 0.001%"),
]
fig, axes = plt.subplots(1, 3, figsize=(12, 3.6), facecolor=PAPER)
for ax, (rule, title, tol, tol_label) in zip(axes, panels, strict=True):
    rates = [m["rates"].get(rule, 0.0) * 100 for m in data["months"]]
    over = [r > tol * 100 for r in rates]
    ax.bar(months, rates, color=[AMBER if o else MINT for o in over], width=0.7)
    ax.axhline(tol * 100, color=INK, lw=1, ls="--")
    ax.text(0.02, 0.93, tol_label, transform=ax.transAxes, color=MUTED, fontsize=8, va="top")
    ax.set_title(title, color=INK, fontsize=10, loc="left")
    ax.set_facecolor(PAPER)
    ax.tick_params(colors=MUTED, labelsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#2B3035")
    ax.set_xlabel("2023 month", color=MUTED, fontsize=8)
    ax.set_ylabel("% of rows", color=MUTED, fontsize=8)
fig.suptitle(
    "Contract v1 (fitted on January) against Jan-Dec 2023 NYC yellow taxi files: amber = over tolerance",
    color=INK, fontsize=10, x=0.01, ha="left",
)  # fmt: skip
fig.tight_layout(rect=(0, 0, 1, 0.92))
out = ROOT / "reports" / "drift.png"
fig.savefig(out, dpi=150, facecolor=PAPER)
print(out)

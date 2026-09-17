# Momentum Radars: Architecture & Logic

## 1. What We Did

We significantly upgraded the Momentum engine by introducing two new "Early Warning" scanners (Radars) that sit *ahead* of the standard momentum pipeline, and we overhauled the UI to make it highly configurable but clean.

### A. Institutional Accumulation Radar (Early Entry)
We built a 3-factor scanner that hunts for "Stealth Accumulation". It evaluates stocks using a "Fast Window" (e.g., 3 days vs 10 days) to spot footprints *before* they trigger the standard 5v22 Momentum Gainer flags.

**The 3 Factors:**
1.  **Fast Heat (3v10):** The recent 3-day average turnover must be $\ge$ 1.20x its 10-day baseline.
2.  **Acceleration:** The Fast Ratio (3v10) must be aggressively outpacing the Standard Ratio (5v22). This ensures volume isn't just high, it's actively accelerating.
3.  **Broker Lock:** The exact same institutional broker must hold the #1 Top Buyer seat for at least 2 of the last 3 sessions.

*We also added a **Minimum Turnover Filter** (defaulting to 5,000,000 NRS) to immediately discard illiquid micro-caps where a single retail order can mathematically distort the ratios.*

### B. Institutional Distribution Radar (Early Exit)
We took the Accumulation logic and inverted it to build an early warning system for exhaustion and dumping.

**The 3 Factors:**
1.  **Fast Cooling:** The 3-day average turnover is dying relative to the 10-day baseline ($\le$ 0.80x).
2.  **Deceleration:** The Fast Ratio is dropping significantly faster than the Standard Ratio. (The standard 22-day trend might still look healthy to retail traders, but the last 3 days show the momentum is dead).
3.  **Broker Dump:** The exact same broker holds the #1 Top Seller seat for at least 2 of the last 3 sessions.

### C. UI & Control Bar Overhaul
-   **Fieldsets:** We broke the single messy row of inputs into organized `Standard Scanner` and `Radar Early Warning` groups.
-   **Configurability:** Added inputs to freely tweak the Fast Recent, Fast Baseline, and Min Turnover limits directly from the UI.
-   **Collapsibles & Tooltips:** Wrapped all data tables in native HTML `<details>` tags so the page remains clean and easy to scan. Added hover tooltips to all status badges (`HIGH_CONVICTION`, `STEALTH_BUILDING`, etc.) so the underlying logic is always transparent.

---

## 2. Why We Did It

The standard 5v22 Momentum scanner is highly effective, but it is fundamentally a **lagging indicator**. By the time a stock sustains enough volume over 5 days to alter a 22-day baseline, the price has usually already moved. 

Furthermore, high volume alone is ambiguous. Is it smart money buying, or is it a retail buying frenzy at the top?

We built these radars to solve both problems: **Speed** and **Intent**.

---

## 3. For What Purpose?

**The ultimate goal is to give you an asymmetric information advantage.**

*   **The Accumulation Radar** exists to get you into a trade *before* the crowd. By verifying that volume acceleration is backed by concentrated, multi-day broker buying, you are tracking smart money building a position quietly. 
*   **The Distribution Radar** exists to get you out of a trade *before* the crowd panics. It spots the exact moment a trend runs out of gas and a major player starts unloading their shares into the lingering retail hype.

Instead of just following the price chart, you are now scanning the underlying market microstructure.

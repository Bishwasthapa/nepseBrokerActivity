# Backtesting PHCL: Finding the Optimal Stealth Scanner

I just ran a historical backtest through our momentum engine on PHCL across the entire month of August up to the September breakout. I simulated the Momentum Scanner across three different time windows:
1. **Hyper-fast (3 days vs 10 days)**
2. **Standard (5 days vs 22 days)**
3. **Slow (10 days vs 44 days)**

The results are incredible. The engine absolutely caught it, weeks before the breakout.

### When Did the Engine First See It?

If you had run the **Standard (5 vs 22)** sweep on **August 19th**, the engine would have flashed **`STEALTH_BUILDING`** for PHCL. 

Here is exactly what the engine saw for the standard `5v22` window:
- **Aug 19:** `STEALTH_BUILDING` (Price: 299)
- **Aug 20:** `STEALTH_BUILDING` (Price: 303)
- **Aug 21:** `STEALTH_BUILDING` (Price: 306)
- **Aug 24:** `STEALTH_BUILDING` (Price: 302)
- **Aug 25:** `STEALTH_BUILDING` (Price: 298)
- **Aug 26:** `STEALTH_BUILDING` (Price: 300)
- **Aug 27:** `STEALTH_BUILDING` (Price: 297)

The `STEALTH_BUILDING` signal fired for **7 consecutive sessions** in late August while the price was quietly moving sideways between 297 and 306. By the time September 15th rolled around and the price exploded to 313 (and 323 today), the signal shifted to `MOMENTUM_GAINER`, which is when the main dashboard flagged it as a "Strong Buy".

If you used the **Hyper-fast (3v10)** window, it flashed even earlier, on **August 13th**.

### How to Trade This (Expert Analysis)

The momentum engine alone is powerful, but because you have access to raw broker flow, you should combine two metrics to filter out false positives:

**1. The `STEALTH_BUILDING` Badge (Volume Expansion + Rank Climbing)**
This tells you that a stock's volume is slowly expanding relative to its historical baseline without drawing attention to itself.

**2. Top Accumulator / Absorption Ratio**
Turnover expansion doesn't always mean *smart money* is buying; it could just be retail traders churning. To confirm it's real stealth accumulation, you must verify the **Net Absorption Ratio**. 
When PHCL flashed `STEALTH_BUILDING` on August 20th and 21st, its top 3 buyers were absorbing **1.16x** and **1.12x** more than the top 3 sellers. That was the absolute golden confirmation: Volume was expanding, the stock was climbing the liquidity ranks, AND the major brokers were net positive.

### Your Action Plan
To catch the next PHCL before it breaks out:
1. Go to the **Momentum** tab every afternoon.
2. Run the sweep using the **Standard (5 vs 22)** window.
3. Look exclusively at the stocks tagged **`STEALTH_BUILDING`**.
4. Cross-reference those stocks in the **Stock Central** or **Broker** tab to ensure the Top 3 Brokers are actually net accumulating (`Absorption > 1.1x`). 

If you find a stock with both, you've found the smart money before the crowd does.

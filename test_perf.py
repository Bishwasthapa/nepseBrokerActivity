import time
from src.screener import position_analysis

start = time.time()
for sym in ['NABIL', 'LEC', 'GBIME', 'NICA', 'SHIVM', 'NHPC', 'API', 'UPPER', 'HIDCL', 'NTC', 'CIT', 'CBIL', 'KBL', 'PCBL', 'SANIMA', 'SBL', 'HBL', 'MBL', 'BOKL', 'NCCB']:
    position_analysis(sym)
print(f"Took {time.time() - start:.2f} seconds")

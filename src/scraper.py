import time
from playwright.sync_api import sync_playwright

def get_top_brokers_today(num_brokers=5, side='buyer', target_date=None):
    """
    Scrapes ShareSansar Top Brokers page, and returns (top_broker_details, detected_market_date)
    """
    print(f"Scraping ShareSansar for Top {num_brokers} {side.capitalize()} Brokers{' for ' + target_date if target_date else ''}...")
    detected_market_date = target_date
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-http2", "--no-sandbox"]
        )
        page = browser.new_page()
        page.set_extra_http_headers({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"})
        page.goto("https://www.sharesansar.com/top-brokers", wait_until="networkidle")
        
        try:
            page.wait_for_selector("#date", timeout=10000)
            page_date = page.input_value("#date")
            if not target_date:
                detected_market_date = page_date
                print(f"Detected Market Date from ShareSansar: {detected_market_date}")

            page.wait_for_selector("table#myTable", timeout=15000)
            
            if target_date:
                print(f"Applying date filter: {target_date}")
                try:
                    mode_selector = page.locator("select:visible, .select2-container:visible, button:has-text('Today'), button:has-text('Daily'), .dropdown-toggle").first
                    if mode_selector.count() > 0:
                        mode_selector.click()
                        page.wait_for_timeout(500)
                        date_wise = page.locator("a:has-text('Datewise'), a:has-text('Date Wise'), option:has-text('Datewise'), li:has-text('Datewise')").first
                        if date_wise.count() > 0:
                            date_wise.click()
                            page.wait_for_timeout(1000)
                except:
                    pass

                page.click("#date")
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.type("#date", target_date, delay=100)
                
                page.click("#btn_topbrokers_submit")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(3000) 

            print("Sorting by Difference...")
            header = page.locator("th:has-text('Difference (Rs.)')")
            header.click()
            time.sleep(1)
            
            if side == 'buyer':
                header.click()
                time.sleep(1)
            
            rows = page.locator("table#myTable tbody tr").all()
            
            if not rows or len(rows) == 0:
                print(f"[WRN] No data rows found on ShareSansar for {target_date if target_date else 'today'}.")
            
            broker_details = []
            for row in rows:
                if len(broker_details) >= num_brokers:
                    break
                    
                try:
                    cells = row.locator("td").all()
                    if len(cells) < 7: continue
                    
                    broker_no = cells[1].inner_text(timeout=3000).strip()
                    broker_name = cells[2].inner_text(timeout=3000).strip()
                    buy_amt = cells[3].inner_text(timeout=3000).strip()
                    sell_amt = cells[4].inner_text(timeout=3000).strip()
                    diff_amt = cells[6].inner_text(timeout=3000).strip()

                    if broker_no.isdigit():
                        broker_details.append({
                            "id": int(broker_no),
                            "name": broker_name,
                            "buy": buy_amt,
                            "sell": sell_amt,
                            "diff": diff_amt
                        })
                except Exception:
                    continue
                    
            browser.close()
            return broker_details, detected_market_date
            
        except Exception as e:
            print(f"Error scraping ShareSansar: {e}")
            
        browser.close()
    return [], detected_market_date

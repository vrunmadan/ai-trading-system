"""
universe/screener_client.py
---------------------------
HTTP client for Screener.in — used for weekly universe discovery and validation.

Screener.in doesn't publish a documented API, but the endpoints the website itself
uses are accessible with a valid session cookie and have been stable for years.
Premium account required for full fundamental data and large screen exports.

What this does:
  1. Session-based login (CSRF token → session cookie)
  2. Screen runner — paginates through ALL companies matching a fundamental filter,
     so we can discover stocks we haven't considered yet
  3. Company ratios — detailed per-stock fundamentals for validation

Required Railway env vars:
  SCREENER_EMAIL    — your Screener.in account email
  SCREENER_PASSWORD — your Screener.in account password

Optional:
  SCREENER_QUERY    — override the default discovery screen query
"""

import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL = "https://www.screener.in"

# Default universe discovery screen.
# Runs against ALL listed companies on NSE + BSE (~5000+).
# Returns the subset that passes these quality filters.
# Tune via SCREENER_QUERY env var if you want stricter/looser criteria.
DEFAULT_DISCOVERY_QUERY = (
    "ROCE > 20 AND "
    "Debt to equity < 0.5 AND "
    "Sales growth > 15 AND "
    "Market Capitalization > 300"
)


class ScreenerClient:
    """
    Authenticated Screener.in session. Instantiate once per job run — the
    session cookie is valid for the duration of one HTTP session.
    """

    def __init__(self, email: str, password: str):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        self._login(email, password)

    # -------------------------------------------------------------------------
    # Auth
    # -------------------------------------------------------------------------

    def _login(self, email: str, password: str) -> None:
        """Log in and store session cookie. Raises ValueError on bad credentials."""
        # Step 1: GET the login page to extract the CSRF token
        try:
            resp = self.session.get(f"{BASE_URL}/login/", timeout=20)
            resp.raise_for_status()
        except Exception as e:
            raise ConnectionError(f"Could not reach Screener.in: {e}") from e

        # CSRF token is in a hidden input or meta tag
        match = re.search(r'name="csrfmiddlewaretoken"[^>]+value="([^"]+)"', resp.text)
        if not match:
            match = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content="([^"]+)"', resp.text)
        if not match:
            raise ValueError("Could not find CSRF token on Screener login page — page structure may have changed")

        csrf_token = match.group(1)

        # Step 2: POST credentials
        login_resp = self.session.post(
            f"{BASE_URL}/login/",
            data={
                "csrfmiddlewaretoken": csrf_token,
                "username": email,
                "password": password,
                "next": "/",
            },
            headers={"Referer": f"{BASE_URL}/login/"},
            timeout=20,
            allow_redirects=True,
        )

        # Login failed → still on /login/ with an error message
        if "/login/" in login_resp.url:
            raise ValueError(
                "Screener.in login failed — check SCREENER_EMAIL and SCREENER_PASSWORD. "
                "Make sure 2FA is disabled or you are using the right credentials."
            )

        log.info("Screener.in login OK")

    # -------------------------------------------------------------------------
    # Screen runner — discovers ALL companies matching fundamental criteria
    # -------------------------------------------------------------------------

    def run_screen(
        self,
        query: str = DEFAULT_DISCOVERY_QUERY,
        sort: str = "Market Capitalization",
        order: str = "desc",
        max_pages: int = 30,
    ) -> list[dict]:
        """
        Run a fundamental screen and return every matching company (all pages).

        Returns a list of dicts:
          symbol        — NSE/BSE trading symbol (from Screener URL)
          company       — company name
          cmp           — current market price (Rs)
          pe            — trailing P/E
          market_cap_cr — market cap in Cr
          roce          — ROCE % (present only if the default columns include it)
          screener_url  — link to the company's Screener page
          rank          — position in screen result (1 = highest market cap by default)
        """
        all_results: list[dict] = []
        page = 1

        while page <= max_pages:
            log.info(f"Screener screen page {page} ...")
            try:
                resp = self.session.get(
                    f"{BASE_URL}/screen/run/",
                    params={
                        "query": query,
                        "sort": sort,
                        "order": order,
                        "page": page,
                    },
                    timeout=25,
                )
                resp.raise_for_status()
            except Exception as e:
                log.warning(f"Screen page {page} request failed: {e} — stopping pagination")
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            rows, col_index = self._parse_screen_table(soup)

            if not rows:
                log.debug(f"No rows on page {page} — end of results")
                break

            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue

                name_cell = cells[1]
                link = name_cell.find("a")
                if not link:
                    continue

                href = link.get("href", "")
                symbol_match = re.search(r"/company/([^/]+)/", href)
                symbol = symbol_match.group(1).upper() if symbol_match else ""
                company = link.text.strip()

                def _num(idx):
                    if idx is None:
                        return None
                    try:
                        return float(cells[idx].text.strip().replace(",", "").replace("%", ""))
                    except (IndexError, ValueError, AttributeError):
                        return None

                all_results.append({
                    "symbol":       symbol,
                    "company":      company,
                    "cmp":          _num(col_index.get("cmp")),
                    "pe":           _num(col_index.get("pe")),
                    "market_cap_cr": _num(col_index.get("market_cap")),
                    "roce":         _num(col_index.get("roce")),
                    "rank":         len(all_results) + 1,
                    "screener_url": f"{BASE_URL}{href}",
                })

            log.info(f"  page {page}: {len(rows)} rows — running total {len(all_results)}")
            page += 1
            time.sleep(1.0)   # 1-second pause — be a polite guest

        log.info(f"Screen complete — {len(all_results)} companies matched the criteria")
        return all_results

    def _parse_screen_table(self, soup: BeautifulSoup) -> tuple[list, dict]:
        """
        Finds the results table and reads column positions from the header row.
        Returns (tbody_rows, col_index_dict).
        col_index maps 'cmp'/'pe'/'market_cap'/'roce' to 0-based <td> index.
        """
        table = soup.find("table", class_="data-table")
        if not table:
            return [], {}

        # Build col index from header row
        col_index: dict[str, int] = {}
        thead = table.find("thead")
        if thead:
            headers = [th.text.strip().lower() for th in thead.find_all("th")]
            for i, h in enumerate(headers):
                if "cmp" in h or ("price" in h and "earning" not in h):
                    col_index["cmp"] = i
                elif "p/e" in h or "price to earning" in h:
                    col_index["pe"] = i
                elif "mar cap" in h or "market cap" in h:
                    col_index["market_cap"] = i
                elif "roce" in h:
                    col_index["roce"] = i

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else []
        return rows, col_index

    # -------------------------------------------------------------------------
    # Per-company detailed ratios (for validation of existing universe stocks)
    # -------------------------------------------------------------------------

    def get_company_ratios(self, symbol: str) -> Optional[dict]:
        """
        Fetch key financial ratios for a single company via Screener's company API.
        Returns dict or None on failure. Used to validate existing universe stocks.

        Keys: roce, debt_to_equity, promoter_holding, sales_growth_3yr, pe, market_cap_cr
        """
        try:
            resp = self.session.get(
                f"{BASE_URL}/api/company/{symbol}/?format=json",
                timeout=15,
            )
            if resp.status_code == 404:
                log.debug(f"Screener: {symbol} not found (404)")
                return None
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"Company API failed for {symbol}: {e}")
            return None

        # Screener company API returns ratios in a list with name/values structure.
        # Build a flat dict for easy access.
        flat: dict = {}
        for section in data.get("ratios", []):
            name = section.get("name", "")
            values = section.get("values", [])
            if values:
                raw = values[0].get("value")
                try:
                    flat[name] = float(str(raw).replace(",", "").replace("%", ""))
                except (TypeError, ValueError):
                    flat[name] = raw

        return {
            "roce":             flat.get("Return on capital employed"),
            "debt_to_equity":   flat.get("Debt to equity"),
            "promoter_holding": flat.get("Promoter holding"),
            "sales_growth_3yr": flat.get("Sales growth 3Years"),
            "pe":               flat.get("Price to Earning"),
            "market_cap_cr":    flat.get("Market Capitalization"),
        }


def build_client_from_env() -> ScreenerClient:
    """
    Convenience factory — reads credentials from env vars and returns a ready client.
    Raises EnvironmentError if credentials not set.
    """
    import os
    email    = os.getenv("SCREENER_EMAIL", "")
    password = os.getenv("SCREENER_PASSWORD", "")
    if not email or not password:
        raise EnvironmentError(
            "SCREENER_EMAIL and SCREENER_PASSWORD must be set in Railway env vars. "
            "Sign up for a Screener.in premium account first."
        )
    return ScreenerClient(email, password)

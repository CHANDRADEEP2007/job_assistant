import time
from urllib.parse import quote_plus


STATE_ABBREVIATIONS = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "new hampshire": "nh",
    "new jersey": "nj",
    "new mexico": "nm",
    "new york": "ny",
    "north carolina": "nc",
    "north dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode island": "ri",
    "south carolina": "sc",
    "south dakota": "sd",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "west virginia": "wv",
    "wisconsin": "wi",
    "wyoming": "wy",
}


def scrape_job_urls(browser, query: str, location: str = "California", source: str = "greenhouse") -> list:
    source = (source or "greenhouse").strip().lower()

    if source == "greenhouse":
        return _scrape_my_greenhouse(browser, query, location)

    urls = _scrape_linkedin_public(browser, query, location)
    if urls:
        return urls

    print("[Navigation] LinkedIn public scraping found nothing. Trying Google...")
    urls = _scrape_google_direct(browser, query, location)
    if urls:
        return urls

    print("[Navigation] No job URLs found.")
    return []


def _dismiss_cookie_banner(browser):
    try:
        ok_button = browser.page.locator("button:has-text('Ok')")
        if ok_button.count() > 0 and ok_button.first.is_visible():
            ok_button.first.click()
            time.sleep(1)
    except Exception:
        pass


def _greenhouse_signed_in(browser) -> bool:
    try:
        if not browser.page:
            return False
        text = browser.get_page_text() or ""
        url = browser.page.url
        return (
            "enter your email address to continue" not in text.lower() and
            "/users/sign_in" not in url
        )
    except Exception:
        return False


def _wait_for_greenhouse_sign_in(browser, timeout_seconds: int = 180) -> bool:
    print(f"[Navigation] Waiting up to {timeout_seconds} seconds for MyGreenhouse sign-in...")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            if browser.page:
                browser.page.bring_to_front()
        except Exception:
            pass
        if _greenhouse_signed_in(browser):
            print("[Navigation] MyGreenhouse sign-in detected.")
            return True
        time.sleep(3)
    print("[Navigation] Timed out waiting for MyGreenhouse sign-in.")
    return False


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").lower().replace(",", " ").split())


def _location_aliases(location: str) -> set[str]:
    location_norm = _normalize_text(location)
    aliases = {location_norm}
    for state_name, abbreviation in STATE_ABBREVIATIONS.items():
        if state_name in location_norm:
            aliases.add(abbreviation)
            aliases.add(f"{state_name} {abbreviation}")
        if f" {abbreviation}" in f" {location_norm}":
            aliases.add(state_name)
    return {alias for alias in aliases if alias}


def _matches_location(card_locations: list[str], requested_location: str) -> bool:
    if not requested_location:
        return True

    haystack = _normalize_text(" ".join(card_locations))
    if not haystack:
        return False

    aliases = _location_aliases(requested_location)
    tokens = set(haystack.split())
    for alias in aliases:
        alias = alias.strip()
        if not alias:
            continue
        if len(alias) <= 3:
            if alias in tokens:
                return True
        elif alias in haystack:
            return True
    return False


def _extract_greenhouse_results(browser) -> list[dict]:
    return browser.page.evaluate(
        """() => {
            return Array.from(document.querySelectorAll('[data-provides="search-result"]')).map((card) => {
                const title = card.querySelector('h4')?.innerText?.trim() || '';
                const company = card.querySelector('p.body')?.innerText?.trim() || '';
                const tags = Array.from(card.querySelectorAll('.tag-text')).map((el) => el.innerText.trim()).filter(Boolean);
                const href = card.querySelector('a[href*="gh_"], a[href*="greenhouse"], a[href*="gh_src="]')?.href || '';
                const text = card.innerText || '';
                return { title, company, tags, href, text };
            }).filter((item) => item.href);
        }"""
    )


def _scrape_my_greenhouse(browser, query: str, location: str) -> list:
    print(f"[Navigation] Searching MyGreenhouse: '{query}' in '{location}'")

    browser.navigate("https://my.greenhouse.io/jobs")
    time.sleep(4)

    if not _greenhouse_signed_in(browser):
        print("[Navigation] MyGreenhouse is not signed in. Please complete sign-in in the opened browser.")
        if not _wait_for_greenhouse_sign_in(browser):
            return []
        browser.navigate("https://my.greenhouse.io/jobs")
        time.sleep(3)

    _dismiss_cookie_banner(browser)

    try:
        search = browser.page.locator("input[placeholder='Search for a job title']")
        search.click()
        search.fill(query)
        search.press("Enter")
        time.sleep(4)
    except Exception as e:
        print(f"[Navigation] MyGreenhouse search error: {e}")
        return []

    try:
        results = _extract_greenhouse_results(browser)
    except Exception as e:
        print(f"[Navigation] Failed to extract MyGreenhouse results: {e}")
        return []

    filtered = []
    seen = set()
    for item in results:
        href = item.get("href", "")
        if not href:
            continue
        if location and not _matches_location(item.get("tags", []), location):
            continue
        base = href.split("?")[0]
        if base in seen:
            continue
        seen.add(base)
        filtered.append(href)

    if filtered:
        print(f"[Navigation] Found {len(filtered)} MyGreenhouse job postings.")
        return filtered[:25]

    print("[Navigation] No MyGreenhouse matches after location filtering.")
    return []


def _scrape_linkedin_public(browser, query: str, location: str) -> list:
    print(f"[Navigation] Searching LinkedIn public jobs: '{query}' in '{location}'")
    safe_query = quote_plus(query)
    safe_location = quote_plus(location)
    search_url = f"https://www.linkedin.com/jobs/search?keywords={safe_query}&location={safe_location}&position=1&pageNum=0"

    try:
        browser.navigate(search_url)
        time.sleep(4)

        if not browser.page:
            return []

        for _ in range(5):
            browser.page.mouse.wheel(0, 1500)
            time.sleep(1)

        print("[Navigation] Extracting job URLs...")
        all_hrefs = browser.page.evaluate(
            """() => {
                const links = document.querySelectorAll('a[href*="/jobs/view/"]');
                return Array.from(links).map(a => a.href).filter(h => h.includes('/jobs/view/'));
            }"""
        )

        clean_urls = []
        seen = set()
        for href in all_hrefs:
            if not href.startswith("http"):
                href = "https://www.linkedin.com" + href
            base = href.split("?")[0]
            if base not in seen and "/jobs/view/" in base:
                seen.add(base)
                clean_urls.append(href)

        if clean_urls:
            print(f"[Navigation] Found {len(clean_urls)} unique job postings!")
            return clean_urls[:25]

        print("[Navigation] No LinkedIn job URLs extracted.")
    except Exception as e:
        print(f"[Navigation] LinkedIn error: {e}")

    return []


def _scrape_google_direct(browser, query: str, location: str) -> list:
    print(f"[Navigation] Google search for: '{query}' jobs in '{location}'")
    google_query = quote_plus(f'site:linkedin.com/jobs/view "{query}" "{location}"')
    search_url = f"https://www.google.com/search?q={google_query}&num=20"

    try:
        browser.navigate(search_url)
        time.sleep(3)

        if not browser.page:
            return []

        hrefs = browser.page.evaluate(
            """() => {
                const results = [];
                document.querySelectorAll('a').forEach(a => {
                    const href = a.href || '';
                    if (href.includes('linkedin.com/jobs/view/') && !href.includes('google.com')) {
                        results.push(href);
                    }
                    if (href.includes('/url?') && href.includes('linkedin.com')) {
                        try {
                            const url = new URL(href);
                            const q = url.searchParams.get('q') || url.searchParams.get('url');
                            if (q && q.includes('linkedin.com/jobs/view/')) {
                                results.push(q);
                            }
                        } catch (e) {}
                    }
                });
                return results;
            }"""
        )

        clean_urls = []
        seen = set()
        for href in hrefs:
            base = href.split("?")[0]
            if base not in seen and "linkedin.com/jobs/view/" in base:
                seen.add(base)
                if href.startswith("http"):
                    clean_urls.append(href)

        if clean_urls:
            print(f"[Navigation] Found {len(clean_urls)} job postings via Google!")
            return clean_urls[:25]
    except Exception as e:
        print(f"[Navigation] Google scraping error: {e}")

    return []

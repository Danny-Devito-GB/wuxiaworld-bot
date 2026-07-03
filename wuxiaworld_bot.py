"""
WuxiaWorld free-chapter unlock bot.

Claims the "N Free Chapters Every 23 Hrs" reward for RMJI and RMJIIR.

Login/session handling (stealth script, SPA-ready waits, identity-server
redirect handling, storage_state persistence) is carried over from the
known-working wuxiaworld_bot.py. Login state is verified by reading the
SPA's own localStorage user object rather than guessing from nav-bar
selectors or round-tripping to a protected page.

Unlock logic:
  1. For each book: land on the book page. If it opened on the "About"
     tab instead of "Chapters", click the Chapters tab.
  2. Check the wait-progress widget. If width == 0%, the free unlock is
     ready.
  3. If ready: expand every book/volume accordion, collect every chapter
     row site-wide, sort ascending by chapter number, and take the
     earliest N locked chapters (locked = has a karma-icon, not "Owned").
  4. Visit each of those N chapter pages directly to consume the unlock.
  5. Print a summary: what was claimed, what's on cooldown.

Run (Windows CMD):
    set WW_EMAIL=you@example.com
    set WW_PASSWORD=yourpassword
    python wuxiaworld_bot.py

    Or with force flag to claim even on cooldown:
    python wuxiaworld_bot.py --force


Requires:
    pip install playwright --break-system-packages
    playwright install chromium
"""

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeoutError

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
EMAIL    = os.environ.get("WW_EMAIL")
PASSWORD = os.environ.get("WW_PASSWORD")

HEADLESS = os.environ.get("CI", "false").lower() == "true"  # auto headless on GitHub Actions
SLOW_MO  = 200
TIMEOUT  = 60_000  # the SPA is slow

STATE_FILE = "wuxiaworld_state.json"
LOG_FILE   = "wuxiaworld_bot.log"

BASE_URL  = "https://www.wuxiaworld.com"

BOOKS = {
    "RMJI": {"slug": "rmji", "free_count": 4},
    "RMJIIR": {"slug": "rmjiir", "free_count": 2},
}

FORCE_CLAIM = "--force" in sys.argv
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("ww_bot")


# ══════════════════════════════════════════════════════════════
#  STEALTH — remove automation fingerprints
# ══════════════════════════════════════════════════════════════

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});
window.chrome = { runtime: {} };
"""


# ══════════════════════════════════════════════════════════════

#  SPA WAIT HELPERS
# ══════════════════════════════════════════════════════════════

async def wait_for_spa_ready(page: Page, timeout: int = 30_000):
    """Wait until WuxiaWorld's React SPA has finished rendering."""
    log.info("Waiting for SPA to finish rendering …")
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
        log.info("SPA is ready ✓")
    except PWTimeoutError:
        log.warning("SPA ready check timed out — proceeding anyway")


async def safe_goto(page: Page, url: str, wait: str = "domcontentloaded"):
    """Navigate and wait for SPA to render."""
    try:
        await page.goto(url, wait_until=wait, timeout=TIMEOUT)
        await wait_for_spa_ready(page)
    except PWTimeoutError:
        log.warning("Navigation to %s timed out, continuing …", url)


# ══════════════════════════════════════════════════════════════
#  SESSION HELPERS
# ══════════════════════════════════════════════════════════════

async def save_state(context):
    state = await context.storage_state()
    Path(STATE_FILE).write_text(json.dumps(state))
    log.info("Session saved → %s", STATE_FILE)


def load_state() -> dict | None:
    if Path(STATE_FILE).exists():
        try:
            state = json.loads(Path(STATE_FILE).read_text())
            log.info("Loaded saved session from %s ✓", STATE_FILE)
            return state
        except Exception as e:
            log.warning("Could not load session file: %s", e)
    return None


# ══════════════════════════════════════════════════════════════
#  AUTH VERIFICATION
#
#  The SPA stores the logged-in user as JSON in localStorage under the
#  key "user". Reading it directly is instant and authoritative — no
#  need to guess from nav-bar selectors or round-trip to a protected
#  page. Must be called while the page is loaded on the wuxiaworld.com
#  origin (localStorage is per-origin).
# ══════════════════════════════════════════════════════════════

async def verify_authenticated(page: Page) -> bool:
    try:
        username = await page.evaluate(
            "() => { try { return JSON.parse(localStorage.getItem('user')).userName; } "
            "catch (e) { return null; } }"
        )
    except Exception as e:
        log.warning("Could not read localStorage: %s", e)
        return False

    if username:
        log.info("localStorage shows logged-in user: %s", username)
        return True

    log.info("localStorage 'user' missing/empty → not authenticated")
    return False


# ══════════════════════════════════════════════════════════════
#  LOGIN
# ══════════════════════════════════════════════════════════════

async def login(page: Page, context):
    if not EMAIL or not PASSWORD:
        raise RuntimeError(
            "Missing credentials. Set WW_EMAIL and WW_PASSWORD environment "
            "variables before running."
        )

    log.info("Navigating to main site …")
    await safe_goto(page, BASE_URL)

    log.info("Clicking profile nav button …")
    try:
        await page.click('[aria-label="profile nav"]', timeout=10_000)
    except PWTimeoutError:
        log.error("Profile nav button not found")
        raise RuntimeError("Login failed — could not find profile nav button.")

    log.info("Waiting for login button in menu …")
    try:
        await page.wait_for_selector('button:has-text("LOG IN")', timeout=10_000, state="visible")
    except PWTimeoutError:
        log.error("Login button in menu did not appear")
        raise RuntimeError("Login failed — login button did not appear.")

    log.info("Clicking login button …")
    await page.click('button:has-text("LOG IN")')

    log.info("Waiting for identity server login form …")
    try:
        await page.wait_for_selector("#Username", timeout=15_000, state="visible")
    except PWTimeoutError:
        log.error("Login form did not appear")
        raise RuntimeError("Login failed — identity server form not found.")

    await page.fill("#Username", EMAIL)
    await page.fill("#Password", PASSWORD)

    log.info("Submitting credentials …")
    await page.click("button:has-text('Log in'), button[type='submit']")

    try:
        await page.wait_for_url(
            lambda u: "identity.wuxiaworld.com" not in u,
            timeout=TIMEOUT,
        )
        log.info("Redirected away from identity server → %s", page.url)
    except PWTimeoutError:
        log.error("Never redirected away from identity server")
        raise RuntimeError("Login failed — stuck on identity page.")

    log.info("Verifying session on main site …")
    await safe_goto(page, BASE_URL)

    if await verify_authenticated(page):
        log.info("Login verified ✓")
    else:
        log.warning("Not authenticated after first attempt, retrying …")
        await page.wait_for_timeout(5_000)
        await page.reload(wait_until="domcontentloaded")
        await wait_for_spa_ready(page)
        if await verify_authenticated(page):
            log.info("Login verified on retry ✓")
        else:
            log.error("Login failed — check WW_EMAIL / WW_PASSWORD credentials.")
            raise RuntimeError("Login failed.")

    await save_state(context)


# ══════════════════════════════════════════════════════════════
#  RMJI / RMJIIR FREE-UNLOCK LOGIC
# ══════════════════════════════════════════════════════════════

@dataclass
class Chapter:
    number: int
    href: str
    locked: bool


async def ensure_chapters_tab(page: Page) -> None:
    """The book page sometimes lands on the 'About' tab instead of
    'Chapters'. Click into Chapters if it isn't already selected."""
    tab = page.locator("#full-width-tab-0")
    if await tab.count() == 0:
        return
    selected = await tab.first.get_attribute("aria-selected")
    if selected != "true":
        log.info("Book page opened on a different tab — clicking 'Chapters'")
        await tab.first.click()
        await page.wait_for_timeout(500)


async def get_wait_progress_percent(page: Page) -> float | None:
    """Returns the wait-progress bar's width as a float (0-100), or None
    if the widget isn't present on the page."""
    locator = page.locator('[data-testid="wait-progress"]')
    if await locator.count() == 0:
        return None
    style = await locator.first.get_attribute("style") or ""
    match = re.search(r"width:\s*([\d.]+)%", style)
    return float(match.group(1)) if match else None


async def get_timer_text(page: Page) -> str | None:
    locator = page.locator('[data-testid="status-text"]')
    if await locator.count() == 0:
        return None
    text = await locator.first.inner_text()
    return text if text else None


async def expand_all_accordions(page: Page) -> None:
    """Clicks every collapsed book/volume accordion so chapter rows render."""
    try:
        await page.evaluate("""
            () => {
                const accordions = document.querySelectorAll('.MuiAccordionSummary-root[aria-expanded="false"]');
                accordions.forEach(accordion => accordion.click());
            }
        """)
        await page.wait_for_timeout(1_000)  # Wait for all accordions to render
    except Exception as e:
        log.warning("Failed to expand accordions: %s", e)


async def collect_chapters(page: Page, slug: str) -> list[Chapter]:
    """Collects every chapter row for this book, across all expanded volumes."""
    chapters_data = await page.evaluate(f"""
        () => {{
            const anchors = document.querySelectorAll('a[href*="/{slug}-chapter-"]');
            return Array.from(anchors).map(anchor => ({{
                href: anchor.getAttribute('href'),
                locked: anchor.querySelector('[data-testid="karma-icon"]') !== null
            }}));
        }}
    """)
    
    chapters: list[Chapter] = []
    for data in chapters_data:
        match = re.search(rf"{slug}-chapter-([\d]+)", data['href'])
        if not match:
            continue
        number = int(match.group(1))
        chapters.append(Chapter(number=number, href=data['href'], locked=data['locked']))
    
    chapters.sort(key=lambda c: c.number)
    return chapters


async def claim_free_chapters(page: Page, slug: str, free_count: int) -> list[int]:
    """Navigates to the earliest `free_count` locked chapters to consume
    the free unlock. Returns the chapter numbers claimed."""
    await expand_all_accordions(page)
    chapters = await collect_chapters(page, slug)
    locked_chapters = [c for c in chapters if c.locked]

    to_claim = locked_chapters[:free_count]
    if len(to_claim) < free_count:
        log.warning(
            "only %d locked chapters found (expected %d) -- may be caught up "
            "on releases.",
            len(to_claim), free_count,
        )

    claimed = []
    for chapter in to_claim:
        await safe_goto(page, f"{BASE_URL}{chapter.href}")
        
        # Wait for lock modal to actually disappear
        try:
            await page.wait_for_selector('.shadow-chapter-lock', state='detached', timeout=10_000)
            claimed.append(chapter.number)
            log.info("Chapter %d unlocked ✓", chapter.number)
        except PWTimeoutError:
            log.warning("Chapter %d still locked after first attempt — retrying...", chapter.number)
            # Retry once: navigate to the same URL again and re-wait
            try:
                await safe_goto(page, f"{BASE_URL}{chapter.href}")
                await page.wait_for_selector('.shadow-chapter-lock', state='detached', timeout=10_000)
                claimed.append(chapter.number)
                log.info("Chapter %d unlocked on retry ✓", chapter.number)
            except PWTimeoutError:
                log.error(
                    "Chapter %d FAILED to unlock after retry — skipping. "
                    "This chapter was not claimed.",
                    chapter.number,
                )
                # Emit a GitHub Actions warning annotation — visible as a
                # yellow banner on the run summary page without needing to
                # open the artifact log. No-op outside of Actions.
                print(f"::warning::Chapter {chapter.number} failed to unlock after retry and was not claimed")

    return claimed

async def unlock_chapter_with_key(page: Page) -> bool:
    """Unlocks a locked chapter by clicking the 'UNLOCK WITH' button.
    Returns True if successful, False otherwise."""
    try:
        # Find button containing the voucher-amount testid
        unlock_button = page.locator('button:has([data-testid="voucher-amount"])')
        if await unlock_button.count() == 0:
            log.warning("UNLOCK WITH button not found on page")
            return False

        log.info("Clicking UNLOCK WITH button …")
        await unlock_button.first.click()
        await page.wait_for_timeout(2_000)

        # Verify the lock modal is gone
        is_locked = await page.locator('.shadow-chapter-lock').count() > 0
        if not is_locked:
            log.info("Chapter unlocked with key ✓")
            return True
        else:
            log.warning("Chapter still locked after clicking UNLOCK WITH")
            return False
    except Exception as e:
        log.error("Failed to unlock chapter with key: %s", e)
        return False


async def claim_mission_chapters_with_keys(page: Page, slug: str, chapter_count: int) -> list[int]:
    """Claims chapters using keys from lock modal."""
    await expand_all_accordions(page)
    chapters = await collect_chapters(page, slug)
    locked_chapters = [c for c in chapters if c.locked]

    to_claim = locked_chapters[:chapter_count]
    if len(to_claim) < chapter_count:
        log.warning("only %d locked chapters found (expected %d)", len(to_claim), chapter_count)

    claimed = []
    for chapter in to_claim:
        await safe_goto(page, f"{BASE_URL}{chapter.href}")
        
        try:
            log.info("Clicking UNLOCK WITH button for chapter %d…", chapter.number)
            if not await unlock_chapter_with_key(page):
                log.warning("Key unlock failed for chapter %d, skipping", chapter.number)
                continue

            await page.wait_for_selector('.shadow-chapter-lock', state='detached', timeout=10_000)
            claimed.append(chapter.number)
            log.info("Chapter %d unlocked with key ✓", chapter.number)
        except PWTimeoutError:
            log.warning("Chapter %d still locked after key unlock", chapter.number)

    return claimed


async def process_book(page: Page, name: str, slug: str, free_count: int) -> dict:
    log.info("[%s] checking book page...", name)
    await safe_goto(page, f"{BASE_URL}/novel/{slug}")
    await ensure_chapters_tab(page)

    progress = await get_wait_progress_percent(page)
    if progress is None:
        return {"book": name, "status": "error", "detail": "wait-progress widget not found"}

    if progress > 0 and progress < 100 and not FORCE_CLAIM:
        remaining = await get_timer_text(page) or "unknown"
        log.info("[%s] on cooldown, %s remaining", name, remaining)
        return {"book": name, "status": "cooldown", "detail": remaining}

    log.info("[%s] ready! claiming %d chapters...", name, free_count)
    claimed = await claim_free_chapters(page, slug, free_count)

    status = "claimed" if len(claimed) >= free_count else "partial"
    if status == "partial":
        log.warning("[%s] only claimed %d/%d chapters", name, len(claimed), free_count)
    log.info("[%s] claimed: %s", name, claimed)
    return {"book": name, "status": status, "detail": claimed}


async def get_mission_progress(page: Page) -> dict | None:
    """Navigates to daily rewards page and returns mission progress + which are claimable."""
    log.info("Checking daily mission progress …")
    await safe_goto(page, f"{BASE_URL}/manage/subscriptions/daily-rewards")
    
    try:
        # Get page text and find mission progress patterns
        page_text = await page.inner_text('body')
        
        missions = {
            "read_2": None,
            "read_5": None,
            "read_10": None,
        }
        totals = {
            "read_2": None,
            "read_5": None,
            "read_10": None,
        }

        # Find "Read X new chapters" and capture the progress on the next line.
        # Verified against the real mission-rewards markup: between a
        # mission's label and its own "current/total" fraction there's only
        # "N keys" text (not N/M-shaped), and the non-greedy scan can't reach
        # backward into the separate Login-rewards box above it. Confirmed
        # correct for the current page structure.
        matches = re.finditer(r'Read (\d+) new chapters.*?(\d+)/(\d+)', page_text, re.DOTALL)

        for match in matches:
            chapters_required = int(match.group(1))
            current = int(match.group(2))
            total = int(match.group(3))

            key = f"read_{chapters_required}"
            if key in missions:
                missions[key] = current
                totals[key] = total

        log.info("Mission progress: 2-chapter=%s, 5-chapter=%s, 10-chapter=%s", 
                 missions["read_2"], missions["read_5"], missions["read_10"])
        
        # Auto-claim any completed missions — compare against the total we
        # actually parsed, not a value re-derived from the dict key name.
        completed = [
            k for k, v in missions.items()
            if v is not None and totals[k] is not None and v >= totals[k]
        ]
        if completed:
            log.info("Completed missions: %s", completed)
            await collect_all_mission_rewards(page)
        
        return missions
    except Exception as e:
        log.warning("Could not read mission progress: %s", e)
    
    return None


async def collect_all_mission_rewards(page: Page) -> bool:
    """Navigate to rewards page and click REDEEM on all mission rewards."""
    log.info("Collecting all mission rewards …")

    # Ensure we're on the rewards page
    current_url = page.url
    if "subscriptions/daily-rewards" not in current_url:
        log.info("Not on rewards page, navigating…")
        await safe_goto(page, f"{BASE_URL}/manage/subscriptions/daily-rewards")
    
    try:
        redeem_buttons = page.locator('button:has-text("REDEEM")')
        count = await redeem_buttons.count()
        log.info("Found %d REDEEM buttons", count)
        
        for i in range(count):
            button = redeem_buttons.nth(i)
            # Check if button is enabled
            disabled = await button.get_attribute("disabled")
            if disabled is None:
                log.info("Clicking REDEEM button %d/%d", i + 1, count)
                await button.click()
                await page.wait_for_timeout(1_000)
        
        log.info("Mission rewards collected ✓")
        return True
    except Exception as e:
        log.warning("Failed to collect mission rewards: %s", e)
        return False

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

async def run_bot() -> bool:
    """Core bot logic. Returns True on success, False if anything went
    wrong badly enough that a scheduled run should be flagged (login
    failure, unhandled exception, or any per-book "error" status)."""
    success = True
    log.info("=" * 55)
    log.info("WuxiaWorld Bot — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 55)

    saved_state = load_state()
    results = []
    start_time = datetime.now()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-extensions",
            ],
        )

        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        if saved_state:
            ctx_kwargs["storage_state"] = saved_state

        context = await browser.new_context(**ctx_kwargs)
        context.set_default_timeout(TIMEOUT)
        page = await context.new_page()
        await page.add_init_script(STEALTH_JS)

        try:
            await safe_goto(page, BASE_URL)

            if not await verify_authenticated(page):
                log.info("Not logged in — performing login …")
                await login(page, context)
            else:
                log.info("Already logged in via saved session ✓")

            for name, info in BOOKS.items():
                try:
                    result = await process_book(page, name, info["slug"], info["free_count"])
                except Exception as e:
                    log.error("%s failed: %s", name, e, exc_info=True)
                    result = {"book": name, "status": "error", "detail": str(e)}
                results.append(result)

            # Check mission progress and claim extra chapters if needed
            mission_progress = await get_mission_progress(page)
            if mission_progress and mission_progress["read_10"] is not None:
                chapters_needed = 10 - mission_progress["read_10"]
                # Only unlock with keys if 4 or fewer chapters needed (to save keys)
                if 0 < chapters_needed <= 4:
                    log.info("Need %d chapters for 10-chapter mission — unlocking with keys", chapters_needed)
                    await safe_goto(page, f"{BASE_URL}/novel/{BOOKS['RMJI']['slug']}")
                    await ensure_chapters_tab(page)
                    mission_claimed = await claim_mission_chapters_with_keys(page, BOOKS["RMJI"]["slug"], chapters_needed)
                    if mission_claimed:
                        log.info("Mission chapters claimed: %s", mission_claimed)
                        await collect_all_mission_rewards(page)
                        results.append({"book": "MISSION", "status": "claimed", "detail": mission_claimed})
                elif chapters_needed > 4:
                    log.info("Need %d chapters for 10-chapter mission (more than 4) — skipping key unlock", chapters_needed)
            else:
                log.info("10-chapter mission complete or could not read progress")

            await save_state(context)
        except Exception as e:
            log.error("Bot error: %s", e, exc_info=True)
            success = False
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            await page.screenshot(path=f"error_{ts}.png")
            log.info("Error screenshot saved → error_%s.png", ts)

        finally:
            await context.close()
            await browser.close()

    log.info("=== Summary ===")
    for r in results:
        detail = r["detail"]
        # If cooldown, calculate unlock time
        if r["status"] == "cooldown" and detail:
            try:
                time_parts = detail.split(":")
                hours = int(time_parts[0])
                minutes = int(time_parts[1])
                seconds = int(time_parts[2]) if len(time_parts) > 2 else 0
                # Calculate unlock time
                unlock_time = datetime.now() + timedelta(hours=hours, minutes=minutes, seconds=seconds)
                unlock_str = unlock_time.strftime("%H:%M:%S")
                detail = f"{detail}, {unlock_str}"
            except (ValueError, IndexError):
                pass

        log.info("%s: %s -> %s", r["book"], r["status"], detail)

    elapsed = (datetime.now() - start_time).total_seconds()
    log.info("Bot completed in %.1fs", elapsed)

    if any(r["status"] == "error" for r in results):
        success = False

    if any(r["status"] == "partial" for r in results):
        success = False
        log.error("One or more books only partially claimed — flagging for non-zero exit")

    if not success:
        log.error("Run completed with errors — flagging for non-zero exit")
 
    # Signal to GitHub Actions whether anything was actually claimed this
    # run (free chapters, key-unlocked mission chapters, or both) — covers
    # "claimed" and "partial" book statuses as well as the MISSION entry.
    # The workflow uses this to decide whether to push a heartbeat commit,
    # so the repo only gets a commit on runs that did real work.
    unlocked = any(r["status"] in ("claimed", "partial") for r in results)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"unlocked={'true' if unlocked else 'false'}\n")
 
    return success


if __name__ == "__main__":
    ok = asyncio.run(run_bot())
    sys.exit(0 if ok else 1)
"""Browser automation controller for Jarvis using Chrome DevTools Protocol (CDP).

Handles:
1. Reusing existing Chrome tabs for navigation and searches (never opening new tabs needlessly).
2. Clicking links by ordinal (first, second, third...) in the active tab:
   - For Google Search: selects first organic result (skips ads and 'People also ask').
   - For general web pages: selects first meaningful link in the main content.
3. Voice scrolling in the active tab (window.scrollBy / window.scrollTo) with
   pyautogui.scroll as fallback for non-browser windows.
4. Starting Chrome with --remote-debugging-port=9222 if not already running.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None

ORDINAL_ALIASES: dict[str, int] = {
    # first <- later, fast, worst, forst, fist, thirst, 1st, one, number one
    "first": 1, "1st": 1, "1": 1, "one": 1, "number one": 1, "number 1": 1,
    "later": 1, "fast": 1, "worst": 1, "forst": 1, "fist": 1, "thirst": 1,
    # second <- 2nd, two, number two, secund
    "second": 2, "2nd": 2, "2": 2, "two": 2, "number two": 2, "number 2": 2, "secund": 2,
    # third <- 3rd, three, number three, thurd
    "third": 3, "3rd": 3, "3": 3, "three": 3, "number three": 3, "number 3": 3, "thurd": 3,
    # fourth
    "fourth": 4, "4th": 4, "4": 4, "four": 4, "number four": 4, "number 4": 4,
    # fifth
    "fifth": 5, "5th": 5, "5": 5, "five": 5, "number five": 5, "number 5": 5,
}


def resolve_ordinal_word(word: str) -> int | None:
    """
    Resolve ordinal inside link/result commands only.
    Uses exact alias map, then rapidfuzz (>= 80) fallback.
    """
    w = word.strip().lower()
    if w in ORDINAL_ALIASES:
        return ORDINAL_ALIASES[w]
    if w in ORDINALS_MAP:
        return ORDINALS_MAP[w]

    # Rapidfuzz fallback (threshold about 80)
    if fuzz is not None:
        best_score = 0.0
        best_val = None
        for alias, val in ORDINAL_ALIASES.items():
            score = float(fuzz.ratio(w, alias))
            if score > best_score:
                best_score = score
                best_val = val
        if best_score >= 80 and best_val is not None:
            return best_val
    return None

ORDINALS_MAP: dict[str, int] = {
    "first": 1,
    "1st": 1,
    "1": 1,
    "one": 1,
    "second": 2,
    "2nd": 2,
    "2": 2,
    "two": 2,
    "third": 3,
    "3rd": 3,
    "3": 3,
    "three": 3,
    "fourth": 4,
    "4th": 4,
    "4": 4,
    "four": 4,
    "fifth": 5,
    "5th": 5,
    "5": 5,
    "five": 5,
    "sixth": 6,
    "6th": 6,
    "6": 6,
    "six": 6,
    "seventh": 7,
    "7th": 7,
    "7": 7,
    "seven": 7,
    "eighth": 8,
    "8th": 8,
    "8": 8,
    "eight": 8,
    "ninth": 9,
    "9th": 9,
    "9": 9,
    "nine": 9,
    "tenth": 10,
    "10th": 10,
    "10": 10,
    "ten": 10,
}

SCROLL_STEP = 600
SCROLL_AMOUNTS: dict[str, int] = {
    "down": SCROLL_STEP,
    "up": -SCROLL_STEP,
    "down_more": SCROLL_STEP * 2,
    "up_more": -SCROLL_STEP * 2,
    "down_little": int(SCROLL_STEP * 0.4),
    "up_little": -int(SCROLL_STEP * 0.4),
    "top": 0,
    "bottom": 0,
}


def find_chrome_executable() -> str | None:
    """Locate the Google Chrome executable on Windows."""
    candidates = [
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)

    try:
        import winreg
        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, key_path) as key:
                    val, _ = winreg.QueryValueEx(key, "")
                    if val and os.path.isfile(val):
                        return val
            except OSError:
                continue
    except Exception:
        pass

    return None


def is_chrome_running() -> bool:
    """Check if any Chrome process is currently running."""
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            shell=False,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return "chrome.exe" in output.lower()
    except Exception:
        return False


def is_cdp_available(port: int = CDP_PORT) -> bool:
    """Fast check whether Chrome DevTools Protocol port is open."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def launch_chrome_with_cdp(url: str | None = None) -> bool:
    """Launch Chrome once with remote debugging enabled if Chrome is not running."""
    exe = find_chrome_executable()
    if not exe:
        return False

    args = [exe, f"--remote-debugging-port={CDP_PORT}", "--no-first-run", "--no-default-browser-check"]
    if url:
        args.append(url)

    creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen(args, creationflags=creation_flags, close_fds=True)
    except Exception as e:
        print(f"[BrowserController] Launching Chrome failed: {e}")
        return False

    # Wait up to 3.0s for the CDP port to open
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if is_cdp_available():
            return True
        time.sleep(0.1)

    return is_cdp_available()


def ensure_chrome_running_with_cdp(target_url: str | None = None) -> bool:
    """Ensure Chrome is running with remote debugging port enabled."""
    if not is_chrome_running() or not is_cdp_available():
        launch_chrome_with_cdp(target_url)
    return is_cdp_available()


class _ChromeCDPManager:
    """Singleton CDP manager keeping Playwright connection over CDP."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._playwright: Any = None
        self._browser: Any = None

    def get_browser(self) -> Any:
        with self._lock:
            if not is_cdp_available():
                if not is_chrome_running():
                    launch_chrome_with_cdp()
                if not is_cdp_available():
                    return None

            try:
                from playwright.sync_api import sync_playwright

                if self._playwright is None:
                    self._playwright = sync_playwright().start()

                if self._browser is None or not self._browser.is_connected():
                    self._browser = self._playwright.chromium.connect_over_cdp(CDP_URL)

                return self._browser
            except Exception as e:
                print(f"[BrowserController] Error connecting over CDP: {e}")
                self._browser = None
                return None

    def get_active_page(self) -> Any:
        browser = self.get_browser()
        if not browser:
            return None

        try:
            contexts = browser.contexts
            if not contexts:
                return None
            context = contexts[0]
            pages = context.pages
            if not pages:
                return context.new_page()

            # Find the active / visible page
            for page in pages:
                try:
                    state = page.evaluate("document.visibilityState")
                    if state == "visible":
                        return page
                except Exception:
                    continue

            # Fallback to the latest page
            return pages[-1]
        except Exception as e:
            print(f"[BrowserController] Error getting active page: {e}")
            return None

    def close(self) -> None:
        with self._lock:
            if self._browser:
                try:
                    self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None


_cdp_manager = _ChromeCDPManager()


def _bring_chrome_window_to_front() -> bool:
    try:
        from tools.window_tools import find_any_window, force_foreground_window
        hwnd = find_any_window("chrome")
        if hwnd:
            return force_foreground_window(hwnd)
    except Exception:
        pass
    return False


def is_chrome_foreground() -> bool:
    """Check if Chrome is the current active/foreground window."""
    try:
        from tools.window_tools import get_foreground_window, get_window_process_name
        hwnd = get_foreground_window()
        if hwnd:
            proc = get_window_process_name(hwnd).lower()
            return proc == "chrome.exe"
    except Exception:
        pass
    return False


import urllib.parse
from urllib.parse import urlparse

# ============================================================
# TAB REUSE & SAME-SITE RULE
# ============================================================

def _extract_site_key(url: str) -> str:
    """Extract canonical domain/site key for same-site tab comparison."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        netloc = (parsed.netloc or "").lower().split(":")[0]
        if netloc.startswith("www."):
            netloc = netloc[4:]
        elif netloc.startswith("m."):
            netloc = netloc[2:]
        return netloc
    except Exception:
        return ""


def _is_same_site(url1: str, url2: str) -> bool:
    """Determine whether two URLs belong to the same site/domain."""
    k1 = _extract_site_key(url1)
    k2 = _extract_site_key(url2)
    if not k1 or not k2:
        return False
    if k1 == k2:
        return True
    if k1.endswith(f".{k2}") or k2.endswith(f".{k1}"):
        return True
    p1 = k1.split(".")
    p2 = k2.split(".")
    if len(p1) >= 2 and len(p2) >= 2 and p1[-2] == p2[-2] and p1[-2] in {"google", "youtube", "amazon", "yahoo", "bing"}:
        return True
    return False


def navigate_to_url(url: str, *, new_tab: bool = False) -> bool:
    """
    Navigate in Chrome obeying the SAME-SITE RULE:
    1. Never open a duplicate tab.
    2. Before opening any site, check all open Chrome tabs via CDP.
    3. If a tab for the same site/domain is already open, switch to that tab and navigate within it.
    4. Leave all other existing tabs untouched.
    5. Open a new tab only if no tab for that site exists.
    6. If Chrome is not running, launch it once with --remote-debugging-port=9222 and reuse that instance.
    """
    if not url:
        return False

    url_str = str(url).strip()
    if url_str.lower().startswith("file:///s:/") or url_str.lower().startswith("file:///s://"):
        clean_target = re.sub(r"^file:///[sS]:/*", "", url_str)
        url = "https://" + clean_target.lstrip("/")
    elif url_str.lower().startswith("s://"):
        url = "https://" + url_str[4:]
    elif url_str.lower().startswith("ttps://"):
        url = "https://" + url_str[7:]
    elif url_str.lower().startswith("ttp://"):
        url = "http://" + url_str[6:]

    # Ensure URL has a valid scheme
    if not url.startswith(("http://", "https://", "file://", "chrome://")):
        url = f"https://{url}"

    try:
        from core.session_memory import session
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if host:
            session.activate_context(f"site:{host}")
    except (ImportError, TypeError, ValueError):
        pass

    # If Chrome isn't running, launch it with debugging port
    if not is_chrome_running():
        launch_chrome_with_cdp(url)
        _bring_chrome_window_to_front()
        return True

    # Chrome is running: connect via CDP
    browser = _cdp_manager.get_browser()
    if browser:
        try:
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
                pages = context.pages

                # Rule: Check all open tabs for the SAME SITE/DOMAIN
                target_page = None
                if not new_tab:
                    for p in pages:
                        try:
                            p_url = p.url or ""
                            if _is_same_site(p_url, url):
                                target_page = p
                                break
                        except Exception:
                            continue

                # If tab for same site exists: switch to that tab and navigate within it
                if target_page is not None:
                    target_page.bring_to_front()
                    try:
                        if target_page.url != url:
                            target_page.goto(url)
                    except Exception as nav_err:
                        print(f"[BrowserController] Error navigating same-site tab: {nav_err}")
                    _bring_chrome_window_to_front()
                    return True

                # No tab for that site exists: open a new tab (or reuse blank tab)
                blank_tab = None
                for p in pages:
                    try:
                        p_url = (p.url or "").lower()
                        if p_url in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}:
                            blank_tab = p
                            break
                    except Exception:
                        continue

                page = blank_tab if blank_tab is not None else context.new_page()
                page.bring_to_front()
                page.goto(url)
                _bring_chrome_window_to_front()
                return True
        except Exception as e:
            print(f"[BrowserController] CDP navigation failed: {e}")

    # Fallback if CDP is not available: navigate active tab via keyboard address bar
    try:
        from tools.system_tools import _navigate_current_browser_tab
        if _navigate_current_browser_tab(url, browser="chrome"):
            return True
    except Exception:
        pass

    return False


# ============================================================
# SEARCH HELPERS (URL SANITIZING & SITE-AWARE SEARCH URL)
# ============================================================

def clean_search_query(raw_query: str) -> str:
    """
    Clean query text before searching:
    - Never pass a URL as a search query.
    - If a URL or partial URL got passed (e.g. ttps://www.youtube.com/results?search_query=bad+apple
      or file:///S://www.youtube.com/results%3Fsearch_query=Bad+Apple),
      extract the query parameter ('search_query' or 'q') if present.
    - Strip any URL fragments, http-like text, domain names, or protocols.
    """
    if not raw_query:
        return ""

    query = str(raw_query).strip()

    # If full or partial URL was passed, unquote and extract query parameter if available
    unquoted = urllib.parse.unquote(query)
    param_match = re.search(r"[?&]search_query=([^&#\s]+)", unquoted, re.IGNORECASE)
    if param_match:
        extracted = param_match.group(1).replace("+", " ").strip()
        if extracted:
            return extracted

    param_q = re.search(r"[?&]q=([^&#\s]+)", unquoted, re.IGNORECASE)
    if param_q:
        extracted = param_q.group(1).replace("+", " ").strip()
        if extracted:
            return extracted

    url_match = re.search(r"(?:https?|ttps?|file:///[sS]:/|s:)//[^\s]+", query, re.IGNORECASE)
    if url_match:
        url_str = url_match.group(0)
        url_str = urllib.parse.unquote(url_str)
        if url_str.lower().startswith("file:///s:"):
            url_str = "https://" + re.sub(r"^file:///[sS]:/*", "", url_str).lstrip("/")
        elif url_str.lower().startswith("s://"):
            url_str = "https://" + url_str[4:]
        elif url_str.startswith("ttps://"):
            url_str = "h" + url_str
        elif url_str.startswith("ttp://"):
            url_str = "h" + url_str
        try:
            parsed = urllib.parse.urlparse(url_str)
            qs = urllib.parse.parse_qs(parsed.query)
            if "search_query" in qs and qs["search_query"]:
                return qs["search_query"][0].strip()
            if "q" in qs and qs["q"]:
                return qs["q"][0].strip()
        except Exception:
            pass

    # Strip any http/https/ttps-like text, file:///s://, protocols, or URL fragments
    query = re.sub(r"file:///[sS]:/\S*", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"(?:https?|ttps?)://\S*", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\b(?:https?|ttps?)\b[:/]*", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\bwww\.[a-z0-9-]+\.[a-z]{2,}\b\S*", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\b[a-z0-9-]+\.(?:com|org|net|edu|gov|io|co|in)\b\S*", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"search_query=\S*", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"\s+", " ", query).strip()

    return query


def get_current_active_browser_site() -> str:
    """
    Determine the current active site in Chrome ('youtube', 'google', or 'other').
    Uses CDP active page url, falling back to foreground window title.
    """
    try:
        page = _cdp_manager.get_active_page()
        if page:
            p_url = (page.url or "").lower()
            if "youtube.com" in p_url:
                return "youtube"
            if "google." in p_url:
                return "google"
    except Exception:
        pass

    try:
        from tools.window_tools import get_foreground_window, get_window_title
        hwnd = get_foreground_window()
        title = (get_window_title(hwnd) or "").lower()
        if "youtube" in title:
            return "youtube"
        if "google" in title:
            return "google"
    except Exception:
        pass

    return "google"


def build_site_search_url(raw_query: str, current_site: str | None = None) -> tuple[str, str, str]:
    """
    Build target search URL directly using the site currently on:
    - YouTube -> https://www.youtube.com/results?search_query=<urlencoded query>
    - Google -> https://www.google.com/search?q=<urlencoded query>
    - Default -> Google
    Uses urllib.parse.quote_plus and validates http:// or https:// prefix.
    Returns (final_url, clean_query, site_name).
    """
    clean_q = clean_search_query(raw_query)
    if not clean_q:
        clean_q = str(raw_query).strip()

    site = current_site or get_current_active_browser_site()
    encoded = urllib.parse.quote_plus(clean_q)

    if site == "youtube":
        final_url = f"https://www.youtube.com/results?search_query={encoded}"
    else:
        final_url = f"https://www.google.com/search?q={encoded}"

    if not final_url.startswith(("http://", "https://")):
        final_url = f"https://{final_url.lstrip('/')}"

    return final_url, clean_q, site


# ============================================================
# LINK CLICKING (ORGANIC GOOGLE, YOUTUBE & MEANINGFUL MAIN CONTENT)
# ============================================================

_ORGANIC_LINK_JS = """
(targetIndex) => {
    const hostname = window.location.hostname.toLowerCase();
    const isGoogle = hostname.includes("google.") && window.location.pathname.includes("/search");
    const isYouTube = hostname.includes("youtube.com");

    if (isYouTube) {
        // Collect organic YouTube search/video results, skipping ads, Shorts shelves, playlists, channels, and 'People also watch'
        const candidates = [];
        const videoRenderers = Array.from(document.querySelectorAll("ytd-video-renderer, ytd-grid-video-renderer"));
        for (const renderer of videoRenderers) {
            // Check for ad markers
            if (renderer.querySelector("ytd-ad-slot-renderer, ytd-promoted-sparkles-web-renderer, ytd-in-feed-ad-layout-renderer, #ad-badge, [aria-label*='Sponsored' i], [aria-label*='Ad' i]")) {
                continue;
            }
            if (renderer.closest("ytd-ad-slot-renderer, [aria-label*='Sponsored' i]")) {
                continue;
            }
            // Skip Shorts shelves, playlists, channels, "People also watch" sections
            if (renderer.closest("ytd-reel-shelf-renderer, ytd-shelf-renderer, ytd-channel-renderer, ytd-playlist-renderer")) {
                continue;
            }
            if (renderer.querySelector("[aria-label*='People also watch' i], [aria-label*='Shorts' i]")) {
                continue;
            }
            const titleEl = renderer.querySelector("a#video-title, a#video-title-link, #video-title");
            if (titleEl && titleEl.href && titleEl.href.includes("/watch") && !titleEl.href.includes("/shorts/")) {
                if (!candidates.some(item => item.href === titleEl.href)) {
                    candidates.push(titleEl);
                }
            }
        }
        // Fallback for YouTube
        if (candidates.length === 0) {
            const allTitles = Array.from(document.querySelectorAll("a#video-title, a[href*='/watch?v=']"));
            for (const a of allTitles) {
                if (!a.closest("ytd-ad-slot-renderer, [aria-label*='Sponsored' i], #ad-badge, ytd-reel-shelf-renderer, ytd-shelf-renderer, ytd-channel-renderer, ytd-playlist-renderer")) {
                    if (a.href && !a.href.includes("/shorts/") && a.offsetParent !== null && !candidates.some(item => item.href === a.href)) {
                        candidates.push(a);
                    }
                }
            }
        }
        if (targetIndex >= 0 && targetIndex < candidates.length) {
            const target = candidates[targetIndex];
            const text = (target.innerText || target.getAttribute("title") || target.textContent || "").trim();
            const href = target.href;
            target.scrollIntoView({ behavior: "smooth", block: "center" });
            target.click();
            return { success: true, text: text, href: href, count: candidates.length };
        }
        return { success: false, count: candidates.length };
    }

    if (isGoogle) {
        // Collect organic Google search results, skipping ads and 'People also ask'
        const searchContainer = document.querySelector("#rso") || document.querySelector("#search") || document.body;
        const h3List = Array.from(searchContainer.querySelectorAll("h3"));
        const candidates = [];

        for (const h3 of h3List) {
            if (h3.closest("#taw, [data-text-ad], .uEierd, [aria-label*='Sponsored'], [aria-label*='Ads']")) {
                continue;
            }
            if (h3.closest("[jsname='yEVEwb'], .related-question-pair, [data-initq], [aria-label*='People also ask']")) {
                continue;
            }
            const h3Text = (h3.textContent || "").trim().toLowerCase();
            if (h3Text.includes("people also ask") || h3Text.includes("related searches") || h3Text.includes("things to know")) {
                continue;
            }

            const a = h3.closest("a") || h3.querySelector("a");
            if (a && a.href && a.href.startsWith("http")) {
                try {
                    const u = new URL(a.href);
                    if (!u.hostname.includes("google.") || (!u.pathname.startsWith("/search") && !u.pathname.startsWith("/preferences"))) {
                        if (!candidates.some(item => item.href === a.href)) {
                            candidates.push(a);
                        }
                    }
                } catch (e) {
                    candidates.push(a);
                }
            }
        }

        if (candidates.length === 0) {
            const gLinks = Array.from(searchContainer.querySelectorAll("div.g a[href^='http']"));
            for (const a of gLinks) {
                if (!a.closest("#taw, [data-text-ad], .uEierd, [aria-label*='Sponsored'], [aria-label*='People also ask']")) {
                    if (!candidates.some(item => item.href === a.href)) {
                        candidates.push(a);
                    }
                }
            }
        }

        if (targetIndex >= 0 && targetIndex < candidates.length) {
            const target = candidates[targetIndex];
            const text = (target.innerText || target.textContent || "").trim();
            const href = target.href;
            target.scrollIntoView({ behavior: "smooth", block: "center" });
            target.click();
            return { success: true, text: text, href: href, count: candidates.length };
        }
        return { success: false, count: candidates.length };
    } else {
        // General pages: find meaningful links in main content
        const mainContent = document.querySelector("main, article, #content, [role='main'], .content, .main") || document.body;
        const allAnchors = Array.from(mainContent.querySelectorAll("a[href]"));
        const candidates = [];

        for (const a of allAnchors) {
            if (a.closest("nav, header, footer, aside, .nav, .menu, .header, .footer, .sidebar, .cookie, .banner, dialog, [role='navigation'], [role='banner']")) {
                continue;
            }
            const href = (a.getAttribute("href") || "").trim();
            if (!href || href.startsWith("#") || href.startsWith("javascript:") || href.startsWith("mailto:") || href.startsWith("tel:")) {
                continue;
            }
            if (a.offsetParent === null && !a.getClientRects().length) {
                continue;
            }
            const text = (a.innerText || a.textContent || "").trim();
            if (text.length < 2 && !a.querySelector("img, svg")) {
                continue;
            }
            const lowerText = text.toLowerCase();
            if (["skip to content", "privacy policy", "terms of service", "terms of use", "cookies"].includes(lowerText)) {
                continue;
            }
            if (!candidates.some(item => item.href === a.href)) {
                candidates.push(a);
            }
        }

        if (targetIndex >= 0 && targetIndex < candidates.length) {
            const target = candidates[targetIndex];
            const text = (target.innerText || target.textContent || "").trim();
            const href = target.href;
            target.scrollIntoView({ behavior: "smooth", block: "center" });
            target.click();
            return { success: true, text: text, href: href, count: candidates.length };
        }
        return { success: false, count: candidates.length };
    }
}
"""



def click_link_by_ordinal(ordinal: int) -> tuple[bool, str]:
    """
    Click the Nth link in the active Chrome tab using CDP.
    Skips ads & 'People also ask' on Google search, selects meaningful links in main content elsewhere.
    """
    if ordinal < 1:
        return False, "Invalid link number."

    if not is_chrome_running():
        return False, "Chrome is not open."

    _bring_chrome_window_to_front()

    page = _cdp_manager.get_active_page()
    if not page:
        return False, "I couldn't connect to the active Chrome tab."

    try:
        page.bring_to_front()
        target_index = ordinal - 1
        result = page.evaluate(_ORGANIC_LINK_JS, target_index)

        if not result or not result.get("success"):
            count = result.get("count", 0) if isinstance(result, dict) else 0
            if count > 0:
                return False, f"I only found {count} link{'s' if count != 1 else ''} on this page."
            return False, f"I couldn't find link number {ordinal} on this page."

        title = result.get("text") or ""
        # Truncate title cleanly if verbose
        if len(title) > 60:
            title = title[:57].rstrip() + "..."

        if title:
            return True, f"Opening {title}."
        return True, f"Opening link number {ordinal}."

    except Exception as e:
        print(f"[BrowserController] Error clicking link: {e}")
        return False, "I encountered an error trying to click the link."


def play_visible_video() -> tuple[bool, str]:
    """Play the most relevant video currently visible in the active browser page."""
    if not is_chrome_foreground() or not is_cdp_available():
        return False, "No active browser page is available."

    page = _cdp_manager.get_active_page()
    if not page:
        return False, "I couldn't connect to the active browser tab."

    try:
        page.bring_to_front()
        result = page.evaluate(
            """() => {
                const visible = (element) => {
                    if (!element) return false;
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) > 0 &&
                        rect.width > 4 && rect.height > 4 &&
                        rect.bottom > 0 && rect.right > 0 &&
                        rect.top < window.innerHeight && rect.left < window.innerWidth;
                };
                const titleOf = (element) => {
                    const text = element.innerText || element.getAttribute('title') ||
                        element.getAttribute('aria-label') || '';
                    return text.replace(/\\s+/g, ' ').trim();
                };
                const youtube = location.hostname.endsWith('youtube.com');
                if (youtube) {
                    const candidates = Array.from(document.querySelectorAll(
                        'ytd-video-renderer, ytd-grid-video-renderer, ytd-compact-video-renderer, ytd-rich-item-renderer'
                    )).filter(visible);
                    for (const card of candidates) {
                        const link = card.querySelector('a#video-title, a#video-title-link, a[href*="/watch"]');
                        if (!link || !visible(link)) continue;
                        const href = link.href;
                        const title = titleOf(link) || 'the visible YouTube video';
                        link.scrollIntoView({behavior: 'smooth', block: 'center'});
                        link.click();
                        return {success: true, title, href, kind: 'youtube'};
                    }
                }
                const videos = Array.from(document.querySelectorAll('video')).filter(visible);
                if (videos.length) {
                    const video = videos[0];
                    const button = video.closest('figure, article, [role="main"]')?.querySelector(
                        'a[href], button[aria-label*="play" i], button[title*="play" i]'
                    );
                    if (button && visible(button)) button.click();
                    else {
                        video.scrollIntoView({behavior: 'smooth', block: 'center'});
                        video.play().catch(() => {});
                    }
                    return {success: true, title: titleOf(video) || 'the visible video', kind: 'html5'};
                }
                return {success: false};
            }"""
        )
        if result and result.get("success"):
            title = result.get("title") or "the visible video"
            if len(title) > 70:
                title = title[:67].rstrip() + "..."
            return True, f"Playing {title}."
        return False, "I couldn't find a visible video on the active page."
    except Exception as error:
        print(f"[BrowserController] Visible video playback failed: {error}")
        return False, "I couldn't inspect the active browser page."


def open_visible_link(item_kind: str = "link") -> tuple[bool, str]:
    """Open the most relevant visible link in the active browser page."""
    if not is_chrome_foreground() or not is_cdp_available():
        return False, "No active browser page is available."

    page = _cdp_manager.get_active_page()
    if not page:
        return False, "I couldn't connect to the active browser tab."

    try:
        page.bring_to_front()
        result = page.evaluate(
            """(requestedKind) => {
                const visible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) > 0 && rect.width > 4 && rect.height > 4 &&
                        rect.bottom > 0 && rect.right > 0 &&
                        rect.top < window.innerHeight && rect.left < window.innerWidth;
                };
                const filePattern = /\\.(pdf|docx?|xlsx?|pptx?|txt|csv|zip|rar|7z|png|jpe?g|gif|webp|mp3|wav|mp4|mkv|avi)(?:[?#]|$)/i;
                const links = Array.from(document.querySelectorAll('a[href]'))
                    .filter(link => visible(link))
                    .map(link => ({
                        link,
                        href: link.href,
                        text: (link.innerText || link.textContent || link.getAttribute('aria-label') || '')
                            .replace(/\\s+/g, ' ').trim(),
                        isFile: Boolean(link.hasAttribute('download') || filePattern.test(link.href))
                    }))
                    .filter(item => item.href && !item.href.startsWith('javascript:') &&
                        !item.href.startsWith('mailto:') && item.text.length > 0);
                const candidates = requestedKind === 'file'
                    ? links.filter(item => item.isFile)
                    : requestedKind === 'folder'
                        ? links.filter(item => !item.isFile)
                        : links;
                const chosen = candidates[0];
                if (!chosen) return {success: false, count: candidates.length};
                chosen.link.scrollIntoView({behavior: 'smooth', block: 'center'});
                chosen.link.click();
                return {success: true, text: chosen.text, href: chosen.href, isFile: chosen.isFile};
            }""",
            item_kind,
        )
        if result and result.get("success"):
            label = result.get("text") or result.get("href") or "the visible link"
            if len(label) > 70:
                label = label[:67].rstrip() + "..."
            prefix = "Opening file" if result.get("isFile") else "Opening"
            return True, f"{prefix} {label}."
        return False, f"I couldn't find a visible {item_kind} link on the active page."
    except Exception as error:
        print(f"[BrowserController] Visible link opening failed: {error}")
        return False, "I couldn't inspect the active browser page."


# ============================================================
# SCROLLING
# ============================================================

def scroll_page(action: str) -> tuple[bool, str]:
    """
    Scroll the active browser tab via window.scrollBy/window.scrollTo,
    with pyautogui.scroll fallback for non-browser windows.
    """
    if action not in SCROLL_AMOUNTS:
        return False, f"Unknown scroll action: {action}"

    amount = SCROLL_AMOUNTS[action]

    # Check if Chrome is active and CDP is available
    if is_chrome_foreground() and is_cdp_available():
        page = _cdp_manager.get_active_page()
        if page:
            try:
                if action == "top":
                    page.evaluate("window.scrollTo({ top: 0, behavior: 'smooth' })")
                    return True, "Scrolled to top."
                elif action == "bottom":
                    page.evaluate("window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' })")
                    return True, "Scrolled to bottom."
                else:
                    page.evaluate(f"window.scrollBy({{ top: {amount}, behavior: 'smooth' }})")
                    direction = "down" if amount > 0 else "up"
                    return True, f"Scrolled {direction}."
            except Exception as e:
                print(f"[BrowserController] CDP scroll failed, falling back to pyautogui: {e}")

    # Fallback for non-browser windows or CDP unavailability: pyautogui
    try:
        import pyautogui
        pyautogui.FAILSAFE = False

        if action == "top":
            pyautogui.hotkey("ctrl", "home")
            return True, "Scrolled to top."
        elif action == "bottom":
            pyautogui.hotkey("ctrl", "end")
            return True, "Scrolled to bottom."
        else:
            # On Windows pyautogui.scroll: negative values scroll DOWN, positive scroll UP
            pyautogui_amount = -amount
            pyautogui.scroll(pyautogui_amount)
            direction = "down" if amount > 0 else "up"
            return True, f"Scrolled {direction}."
    except Exception as e:
        print(f"[BrowserController] pyautogui.scroll failed: {e}")
        return False, "I couldn't scroll the window."


# ============================================================
# VOICE COMMAND PARSERS
# ============================================================

def parse_link_click_command(command: str) -> int | None:
    """
    Parse ordinal link click commands like:
    - 'open the first link', 'click first link', 'open second result',
    - 'click the 3rd link', 'open 1st result', 'click the second', 'open first',
    - 'open first video', 'open the first one', 'open the later link', 'open the fast link',
    - 'open number one result'
    Uses resolve_ordinal_word with exact ordinal aliases and rapidfuzz fallback (threshold 80).
    """
    cmd = command.strip().lower()
    cmd = re.sub(r"[,:;!?]+", "", cmd).strip()
    cmd = re.sub(r"\s+(?:in|on|from)\s+(?:youtube|you tube|yt)$", "", cmd).strip()

    # Pattern 0: Optional (the) + ordinal_words + (link|result|search result|video|entry|item|one)
    m0 = re.match(
        r"^(?:the\s+)?([a-z0-9\s]+?)\s+(?:link|result|search result|video|entry|item|one)$",
        cmd,
    )
    if m0:
        val = resolve_ordinal_word(m0.group(1))
        if val is not None:
            return val

    # Pattern 1: Verb + optional (the) + ordinal_words + (link|result|search result|video|entry|item|website|page|site|one)
    m1 = re.match(
        r"^(?:open|click|follow|select|press|go to|goto|play)(?:\s+on)?(?:\s+the)?\s+"
        r"([a-z0-9\s]+?)\s+(?:link|result|search result|video|entry|item|website|page|site|one)$",
        cmd,
    )
    if m1:
        val = resolve_ordinal_word(m1.group(1))
        if val is not None:
            return val

    # Pattern 2: Verb + (link|result|video) + ordinal_words
    m2 = re.match(
        r"^(?:open|click|follow|play)(?:\s+on)?(?:\s+the)?\s+(?:link|result|video)\s+([a-z0-9\s]+)$",
        cmd,
    )
    if m2:
        val = resolve_ordinal_word(m2.group(1))
        if val is not None:
            return val

    # Pattern 3: Verb + (the) + ordinal_words
    m3 = re.match(
        r"^(?:open|click|follow|play)(?:\s+on)?(?:\s+the)?\s+([a-z0-9\s]+)$",
        cmd,
    )
    if m3:
        val = resolve_ordinal_word(m3.group(1))
        if val is not None:
            return val

    return None


def play_or_resume_youtube(query: str | None = None) -> tuple[bool, str]:
    """
    Execute YouTube play command:
    - If no query: resume playback in active YouTube tab (press 'k' or click play button).
    - If query provided:
      - If active tab is already a YouTube results page:
        wait for video renderer, click first real video result in same tab.
      - If not on YouTube results page:
        navigate to YouTube search query in existing YouTube tab (or open one),
        wait for video renderer, click first real video result.
    """
    clean_q = clean_search_query(query) if query else ""
    # 1. Transport resume (no query or generic transport word)
    if not clean_q or clean_q in ("video", "music", "playback", "song", "it", "current video"):
        if is_chrome_running() and is_cdp_available():
            try:
                page = _cdp_manager.get_active_page()
                if page and "youtube.com" in page.url:
                    page.bring_to_front()
                    res = page.evaluate("""() => {
                        const playBtn = document.querySelector('button.ytp-play-button');
                        const video = document.querySelector('video');
                        if (video && video.paused) {
                            if (playBtn) playBtn.click();
                            else video.play();
                            return true;
                        }
                        if (playBtn) { playBtn.click(); return true; }
                        return false;
                    }""")
                    return True, "Resumed playback."
            except Exception as e:
                print(f"[BrowserController] Resume error: {e}")
        try:
            import pyautogui
            pyautogui.press("k")
            return True, "Resumed playback."
        except Exception:
            return False, "Could not resume playback."

    # 2. "Play <query>" -> play first real video result
    ensure_chrome_running_with_cdp(target_url=None)
    _bring_chrome_window_to_front()

    encoded = urllib.parse.quote_plus(clean_q)
    target_search_url = f"https://www.youtube.com/results?search_query={encoded}"

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0] if browser.contexts else browser.new_context()

            # Find existing YouTube tab if any
            yt_page = None
            for pg in context.pages:
                if "youtube.com" in pg.url:
                    yt_page = pg
                    break

            active_page = _cdp_manager.get_active_page() or yt_page
            is_active_yt_results = bool(active_page and "youtube.com/results" in active_page.url)

            if is_active_yt_results and active_page:
                target_page = active_page
                target_page.bring_to_front()
            else:
                if yt_page is not None:
                    target_page = yt_page
                    target_page.bring_to_front()
                    target_page.goto(target_search_url, wait_until="domcontentloaded", timeout=12000)
                else:
                    target_page = context.new_page()
                    target_page.bring_to_front()
                    target_page.goto(target_search_url, wait_until="domcontentloaded", timeout=12000)

            # Wait for video renderer selector to avoid clicking too early
            try:
                target_page.wait_for_selector(
                    "ytd-video-renderer a#video-title",
                    state="visible",
                    timeout=8000,
                )
            except Exception:
                time.sleep(1.0)

            # Click the first real organic video result (index 0)
            result = target_page.evaluate(_ORGANIC_LINK_JS, 0)
            if result and result.get("success"):
                title = result.get("text") or clean_q
                if len(title) > 60:
                    title = title[:57].rstrip() + "..."
                return True, f"Playing {title} on YouTube."
            else:
                from tools.system_tools import _fetch_first_youtube_video_url
                direct_url = _fetch_first_youtube_video_url(clean_q)
                if direct_url:
                    target_page.goto(direct_url, wait_until="domcontentloaded", timeout=12000)
                    return True, f"Playing {clean_q} on YouTube."
                return True, f"Opened YouTube search for {clean_q}."
    except Exception as e:
        err_msg = str(e)
        if "connect ECONNREFUSED" in err_msg or "9222" in err_msg:
            print("[BrowserController] Chrome CDP unavailable; falling back to direct navigation.")
        else:
            print(f"[BrowserController] Play YouTube error: {e}")
        from tools.system_tools import _fetch_first_youtube_video_url
        direct_url = _fetch_first_youtube_video_url(clean_q)
        if direct_url:
            navigate_to_url(direct_url)
            return True, f"Playing {clean_q} on YouTube."
        navigate_to_url(target_search_url)
        return True, f"Searching YouTube for {clean_q}."



def parse_scroll_command(command: str) -> str | None:
    """
    Parse scrolling voice commands:
    - 'scroll down', 'scroll up', 'scroll to top', 'scroll to bottom'
    - 'scroll a little', 'scroll a lot', 'scroll down more', 'scroll', etc.
    """
    try:
        from core.command_engine import normalize_voice_text
        text = normalize_voice_text(command)
    except Exception:
        text = (command or "").strip().lower()
    text = re.sub(r"[,:;!?]+", "", text).strip()

    # Strip conversational prefixes
    text = re.sub(r"^(?:please\s+|can\s+you\s+|could\s+you\s+|just\s+)+", "", text).strip()

    if re.search(r"\b(?:scroll|go|move)\s+(?:to\s+(?:the\s+)?)?top\b|\bscroll\s+all\s+the\s+way\s+up\b", text):
        return "top"
    if re.search(r"\b(?:scroll|go|move)\s+(?:to\s+(?:the\s+)?)?bottom\b|\bscroll\s+all\s+the\s+way\s+down\b", text):
        return "bottom"
    if re.search(r"\bscroll\s+down\s+(?:more|a lot|much|further)\b|\bscroll\s+(?:a lot|more)\s+down\b|\bscroll\s+a\s+lot\b", text):
        return "down_more"
    if re.search(r"\bscroll\s+up\s+(?:more|a lot|much|further)\b|\bscroll\s+(?:a lot|more)\s+up\b", text):
        return "up_more"
    if re.search(r"\bscroll\s+up\s+a\s+(?:little|bit)\b|\bscroll\s+a\s+(?:little|bit)\s+up\b", text):
        return "up_little"
    if re.search(r"\bscroll\s+down\s+a\s+(?:little|bit)\b|\bscroll\s+a\s+(?:little|bit)(?:\s+down)?\b", text):
        return "down_little"
    if re.search(r"\b(?:scroll|page|stroll|slow|close|sold|hold|call|go|move)\s+down\b", text):
        return "down"
    if re.search(r"\b(?:scroll|page|stroll|slow|go|move)\s+up\b", text):
        return "up"
    if text in {"scroll", "page"}:
        return "down"

    return None


def handle_browser_voice_command(command: str) -> str | None:
    """
    Top-level router hook for browser link clicking and scrolling.
    Must be called BEFORE any app-launch lookup so that phrases like
    'open the first link' or 'scroll down' never get treated as app names.
    """
    try:
        from core.command_engine import normalize_voice_text
        norm_cmd = normalize_voice_text(command)
    except Exception:
        norm_cmd = (command or "").strip()

    # 1. Check link clicking
    ordinal = parse_link_click_command(norm_cmd)
    if ordinal is not None:
        ok, msg = click_link_by_ordinal(ordinal)
        if not ok and re.search(r"\b(?:video|youtube|yt)\b", norm_cmd, re.IGNORECASE):
            return None
        return msg

    # 2. Check scrolling
    scroll_action = parse_scroll_command(norm_cmd)
    if scroll_action is not None:
        _success, msg = scroll_page(scroll_action)
        return msg

    return None

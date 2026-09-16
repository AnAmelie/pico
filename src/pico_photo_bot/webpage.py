from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

from .public_url import (
    PublicUrlError,
    Resolver,
    normalize_public_http_url,
    resolve_host,
    validate_public_http_url,
)

MAX_WEBPAGE_IMAGES = 20
MAX_CANDIDATES = 100
MAX_TEXT_LENGTH = 600
MIN_IMAGE_WIDTH = 300
MIN_IMAGE_HEIGHT = 200
MIN_IMAGE_AREA = 120_000
_ALLOWED_RESOURCE_TYPES = {"document", "script", "stylesheet", "xhr", "fetch", "image"}
_REJECTED_URL_PARTS = re.compile(
    r"(?:^|[/_.-])(logo|icon|avatar|advert|banner|sprite|placeholder|tracking|pixel)(?:[/_.-]|$)",
    re.IGNORECASE,
)
_CREDIT_RE = re.compile(
    r"(?:photo(?:graph)?(?:er)?|credit|image|©|copyright)\s*(?:by|:)?\s*(.+)",
    re.IGNORECASE,
)


class WebpageError(ValueError):
    pass


@dataclass(frozen=True)
class WebpageCandidate:
    ordinal: int
    url: str
    alt: str = ""
    caption: str = ""
    nearby_text: str = ""
    credit: str | None = None
    photographer: str | None = None


@dataclass(frozen=True)
class WebpageResult:
    canonical_url: str
    title: str
    candidates: tuple[WebpageCandidate, ...]
    total_count: int

    @property
    def omitted_count(self) -> int:
        return max(0, self.total_count - len(self.candidates))


class WebpageRenderer(Protocol):
    async def render(self, url: str) -> WebpageResult: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class _RawCandidate:
    url: str
    width: int
    height: int
    alt: str = ""
    caption: str = ""
    nearby_text: str = ""
    credit: str | None = None
    photographer: str | None = None


def _bounded_text(value: object, limit: int = MAX_TEXT_LENGTH) -> str:
    return " ".join(str(value or "").split())[:limit]


def _credit_from_text(value: object) -> str | None:
    text = _bounded_text(value, 300)
    if not text:
        return None
    match = _CREDIT_RE.search(text)
    return _bounded_text(match.group(1), 200) if match else None


def _normalized_candidate_url(value: object, base_url: str) -> str | None:
    raw = str(value or "").strip()
    if not raw or raw.casefold().startswith(("data:", "blob:", "javascript:")):
        return None
    try:
        return normalize_public_http_url(urljoin(base_url, raw))
    except PublicUrlError:
        return None


def _is_qualifying(candidate: _RawCandidate) -> bool:
    parsed = urlsplit(candidate.url)
    path = parsed.path.casefold()
    if _REJECTED_URL_PARTS.search(path):
        return False
    if path.endswith((".gif", ".svg", ".ico", ".avif")):
        return False
    return (
        candidate.width >= MIN_IMAGE_WIDTH
        and candidate.height >= MIN_IMAGE_HEIGHT
        and candidate.width * candidate.height >= MIN_IMAGE_AREA
    )


def select_candidates(
    raw_candidates: list[_RawCandidate],
    base_url: str,
    structured_images: list[dict[str, object]] | None = None,
    *,
    limit: int = MAX_WEBPAGE_IMAGES,
) -> tuple[tuple[WebpageCandidate, ...], int]:
    """Filter, deduplicate, and merge publisher-provided ImageObject metadata."""
    structured: dict[str, dict[str, object]] = {}
    for item in structured_images or []:
        if not isinstance(item, dict):
            continue
        normalized = _normalized_candidate_url(
            item.get("contentUrl") or item.get("url"), base_url
        )
        if normalized:
            structured[normalized] = item

    selected: list[WebpageCandidate] = []
    seen: set[str] = set()
    qualifying_count = 0
    for raw in raw_candidates[:MAX_CANDIDATES]:
        normalized = _normalized_candidate_url(raw.url, base_url)
        if not normalized or normalized in seen:
            continue
        candidate = replace(raw, url=normalized)
        if not _is_qualifying(candidate):
            continue
        seen.add(normalized)
        qualifying_count += 1
        if len(selected) >= limit:
            continue
        metadata = structured.get(normalized, {})
        creator = metadata.get("creator")
        if isinstance(creator, dict):
            creator = creator.get("name")
        elif isinstance(creator, list):
            creator = ", ".join(
                _bounded_text(item.get("name") if isinstance(item, dict) else item, 100)
                for item in creator
            )
        structured_credit = _bounded_text(
            creator
            or metadata.get("creditText")
            or metadata.get("copyrightNotice"),
            200,
        )
        caption = _bounded_text(metadata.get("caption") or raw.caption)
        exact_credit = (
            structured_credit
            or _bounded_text(raw.credit, 200)
            or _credit_from_text(raw.nearby_text)
        )
        selected.append(
            WebpageCandidate(
                ordinal=len(selected) + 1,
                url=normalized,
                alt=_bounded_text(raw.alt, 300),
                caption=caption,
                nearby_text=_bounded_text(raw.nearby_text),
                credit=exact_credit or None,
                photographer=exact_credit or raw.photographer,
            )
        )
    return tuple(selected), qualifying_count


class PlaywrightWebpageRenderer:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        navigation_timeout: float = 20.0,
        lifetime_timeout: float = 35.0,
        action_timeout: float = 3.0,
    ) -> None:
        self.resolver = resolver or resolve_host
        self.navigation_timeout = navigation_timeout
        self.lifetime_timeout = lifetime_timeout
        self.action_timeout = action_timeout
        self._playwright: Any = None
        self._browser: Any = None
        self._start_lock = asyncio.Lock()

    async def _start(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            return
        async with self._start_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            if self._browser is not None or self._playwright is not None:
                await self.close()
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise WebpageError(
                    "Webpage imports require Pico's optional webpage support; install "
                    "pico-photo-bot[webpage] and run `playwright install chromium`."
                ) from exc
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=["--no-proxy-server", "--disable-background-networking"],
                )
            except Exception as exc:
                await self.close()
                raise WebpageError(
                    "Pico could not launch Chromium for this webpage; install the "
                    "Playwright Chromium browser and try again."
                ) from exc

    async def close(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass

    async def render(self, url: str) -> WebpageResult:
        await self._start()
        try:
            async with asyncio.timeout(self.lifetime_timeout):
                return await self._render_in_context(url)
        except WebpageError:
            raise
        except TimeoutError as exc:
            raise WebpageError(
                "The webpage did not finish rendering before Pico's deadline."
            ) from exc
        except Exception as exc:
            if self._browser is not None and not self._browser.is_connected():
                await self.close()
            raise WebpageError(
                "The webpage could not be rendered; it may block automated browsers."
            ) from exc

    async def _render_in_context(self, url: str) -> WebpageResult:
        assert self._browser is not None
        context = await self._browser.new_context(
            accept_downloads=False,
            java_script_enabled=True,
            service_workers="block",
        )
        deadline = time.monotonic() + self.lifetime_timeout

        async def route_request(route: Any, request: Any) -> None:
            if time.monotonic() >= deadline or request.resource_type not in _ALLOWED_RESOURCE_TYPES:
                await route.abort()
                return
            try:
                normalized = normalize_public_http_url(request.url)
                await validate_public_http_url(normalized, self.resolver)
            except (PublicUrlError, OSError):
                await route.abort()
                return
            await route.continue_()

        try:
            await context.route("**/*", route_request)
            page = await context.new_page()

            def close_popup(popup: Any) -> None:
                if popup is not page:
                    asyncio.create_task(popup.close())

            context.on("page", close_popup)
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self.navigation_timeout * 1000),
            )
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=min(5000, int(self.action_timeout * 2000))
                )
            except Exception:
                pass
            canonical, title, structured = await page.evaluate(_PAGE_METADATA_SCRIPT)
            canonical_url = _normalized_candidate_url(canonical or page.url, page.url)
            if canonical_url is None:
                canonical_url = normalize_public_http_url(page.url)
            raw: list[_RawCandidate] = []
            raw.extend(await self._extract_carousels(page, canonical_url))
            raw.extend(await self._extract_content_images(page, canonical_url))
            candidates, total = select_candidates(raw, canonical_url, structured)
            if not candidates:
                fallback = await page.evaluate(_FALLBACK_IMAGE_SCRIPT)
                if fallback:
                    fallback_raw = _raw_from_dict(fallback, canonical_url)
                    if fallback_raw is not None:
                        candidates, total = select_candidates(
                            [fallback_raw], canonical_url, structured
                        )
            return WebpageResult(
                canonical_url=canonical_url,
                title=_bounded_text(title, 300),
                candidates=candidates,
                total_count=total,
            )
        finally:
            await context.close()

    async def _extract_content_images(
        self, page: Any, base_url: str
    ) -> list[_RawCandidate]:
        items = await page.evaluate(_CONTENT_IMAGES_SCRIPT)
        output: list[_RawCandidate] = []
        for item in items[:MAX_CANDIDATES]:
            candidate = _raw_from_dict(item, base_url)
            if candidate is not None:
                output.append(candidate)
        return output

    async def _extract_carousels(
        self, page: Any, base_url: str
    ) -> list[_RawCandidate]:
        selector = (
            '[aria-roledescription="carousel"], [role="region"][aria-label*="gallery" i], '
            '[class*="carousel" i], [class*="gallery" i]'
        )
        containers = page.locator(selector)
        output: list[_RawCandidate] = []
        for index in range(min(await containers.count(), 10)):
            container = containers.nth(index)
            seen_states: set[str] = set()
            for _ in range(MAX_WEBPAGE_IMAGES):
                state = await container.evaluate(_CAROUSEL_STATE_FUNCTION)
                if not state or state.get("key") in seen_states:
                    break
                seen_states.add(state["key"])
                candidate = _raw_from_dict(state, base_url)
                if candidate is not None:
                    output.append(candidate)
                next_button = container.locator(
                    'button[aria-label*="next" i], [role="button"][aria-label*="next" i], '
                    'button[title*="next" i], [data-testid*="next" i]'
                ).first
                if not await next_button.count() or not await next_button.is_visible():
                    break
                if await next_button.is_disabled():
                    break
                before = state["key"]
                await next_button.click(timeout=int(self.action_timeout * 1000))
                changed = None
                stop_at = asyncio.get_running_loop().time() + self.action_timeout
                while asyncio.get_running_loop().time() < stop_at:
                    await asyncio.sleep(0.1)
                    changed = await container.evaluate(_CAROUSEL_STATE_FUNCTION)
                    if changed and changed.get("key") != before:
                        break
                if not changed or changed.get("key") == before:
                    break
                if len(output) >= MAX_CANDIDATES:
                    return output
        return output


def _raw_from_dict(item: object, base_url: str) -> _RawCandidate | None:
    if not isinstance(item, dict):
        return None
    url = _normalized_candidate_url(item.get("url"), base_url)
    if url is None:
        return None
    try:
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
    except (TypeError, ValueError):
        return None
    return _RawCandidate(
        url=url,
        width=width,
        height=height,
        alt=_bounded_text(item.get("alt"), 300),
        caption=_bounded_text(item.get("caption")),
        nearby_text=_bounded_text(item.get("nearbyText")),
        credit=_bounded_text(item.get("credit"), 200) or None,
    )


_PAGE_METADATA_SCRIPT = """() => {
  const canonical = document.querySelector('link[rel="canonical"]')?.href || location.href;
  const structured = [];
  const add = value => {
    if (!value || typeof value !== 'object') return;
    if (Array.isArray(value)) { value.forEach(add); return; }
    if (value['@graph']) add(value['@graph']);
    const types = Array.isArray(value['@type']) ? value['@type'] : [value['@type']];
    if (types.includes('ImageObject')) structured.push(value);
    if (value.image) add(value.image);
  };
  for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { add(JSON.parse(node.textContent || 'null')); } catch (_) {}
  }
  return [canonical, document.title || '', structured.slice(0, 100)];
}"""

_CONTENT_IMAGES_SCRIPT = """() => {
  const roots = [...document.querySelectorAll('article, main, figure')];
  const images = [];
  const seen = new Set();
  for (const root of roots) {
    for (const img of root.querySelectorAll('img')) {
      if (img.closest('[aria-roledescription="carousel"], [class*="carousel" i], [class*="gallery" i]')) continue;
      const url = img.currentSrc || img.src;
      if (!url || seen.has(url)) continue;
      seen.add(url);
      const box = img.getBoundingClientRect();
      const figure = img.closest('figure');
      const captionNode = figure?.querySelector('figcaption');
      const nearby = captionNode?.innerText || img.parentElement?.innerText || '';
      const creditNode = figure?.querySelector('[class*="credit" i], [data-testid*="credit" i], [itemprop="creditText"]');
      images.push({url, width: Math.round(box.width || img.naturalWidth), height: Math.round(box.height || img.naturalHeight), alt: img.alt || '', caption: captionNode?.innerText || '', nearbyText: nearby, credit: creditNode?.innerText || ''});
    }
  }
  return images.slice(0, 100);
}"""

_CAROUSEL_STATE_FUNCTION = """node => {
  const bounds = node.getBoundingClientRect();
  const visible = element => {
    const r = element.getBoundingClientRect();
    const s = getComputedStyle(element);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
      && r.right > bounds.left && r.left < bounds.right
      && r.bottom > bounds.top && r.top < bounds.bottom;
  };
  const firstVisible = selector => [...node.querySelectorAll(selector)].find(visible);
  const img = firstVisible('img');
  if (!img) return null;
  const box = img.getBoundingClientRect();
  const captionNode = firstVisible('[class*="caption__text" i], figcaption, [data-testid*="caption" i]') || firstVisible('[aria-live] [class*="caption" i], [class*="caption" i]');
  const creditNode = firstVisible('[aria-live] [class*="credit" i], [class*="credit" i], [data-testid*="credit" i], [itemprop="creditText"]');
  const counter = firstVisible('[aria-live], [class*="counter" i], [class*="pagination" i]')?.innerText || '';
  const url = img.currentSrc || img.src;
  return {key: counter.trim() + '|' + url, url, width: Math.round(box.width || img.naturalWidth), height: Math.round(box.height || img.naturalHeight), alt: img.alt || '', caption: captionNode?.innerText || '', nearbyText: captionNode?.parentElement?.innerText || '', credit: creditNode?.innerText || ''};
}"""

_FALLBACK_IMAGE_SCRIPT = """() => {
  const node = document.querySelector('meta[property="og:image"], meta[name="twitter:image"]');
  const url = node?.content;
  if (!url) return null;
  return {url, width: Number(document.querySelector('meta[property="og:image:width"]')?.content || 1200), height: Number(document.querySelector('meta[property="og:image:height"]')?.content || 630), alt: document.querySelector('meta[property="og:image:alt"], meta[name="twitter:image:alt"]')?.content || '', caption: '', nearbyText: '', credit: ''};
}"""

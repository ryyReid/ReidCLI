"""Web search tool: free, no API key.

Two DuckDuckGo sources, tried in order:

1. The official Instant Answer JSON API (api.duckduckgo.com) — a separate,
   lightly-protected endpoint meant for programmatic use. ~10-20x faster than
   the HTML scrape (small JSON payload, no page rendering markup) but only
   populated for factual/entity queries ("what is X", "who is Y") — general
   searches come back empty.
2. The HTML-only endpoint (html.duckduckgo.com/html/) — the same page
   DuckDuckGo serves to browsers with JavaScript disabled. Slower and more
   prone to anti-bot rate limiting, but covers general search queries the
   Instant Answer API doesn't.

Falls through to (2) only when (1) has nothing useful, so most factual
lookups get the fast path. Gzip-compressed responses (smaller/faster
transfer) and a short-lived in-memory cache (repeat queries within a session
skip the network entirely) are used for both. Stdlib-only (urllib + re) to
avoid new dependencies. Gated as ActionKind.NETWORK (HIGH risk by default)
through the same policy engine as every other tool.
"""
from __future__ import annotations

import gzip
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from reidcli.policy.models import ActionKind, PermissionDecision, RiskLevel
from reidcli.tools.base import BaseTool, ToolContext, ToolDefinition, ToolResult

_INSTANT_ANSWER_URL = "https://api.duckduckgo.com/"
_HTML_SEARCH_URL = "https://html.duckduckgo.com/html/"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip"}
_TIMEOUT_SECONDS = 10
_CACHE_TTL_SECONDS = 300

# DuckDuckGo's HTML results: <a class="result__a" href="...">title</a> ... <a class="result__snippet" ...>snippet</a>
_RESULT_RE = re.compile(
    r'class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'.*?class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")

# DuckDuckGo serves this instead of results when it suspects automation
# (common from datacenter/shared IPs, or after rapid repeated queries).
_BOT_CHALLENGE_MARKER = "bots use duckduckgo too"

_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query."},
        "max_results": {
            "type": "integer",
            "description": "Maximum results to return (default 5, max 10).",
        },
    },
    "required": ["query"],
}


def _clean(html_fragment: str) -> str:
    text = _TAG_RE.sub("", html_fragment)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _resolve_href(href: str) -> str | None:
    """DuckDuckGo wraps organic result links in a `/l/?uddg=...` redirect;
    unwrap to the real URL. Sponsored results redirect through a different,
    non-unwrappable tracking endpoint (e.g. `/y.js`) — treat those as
    unresolvable and drop them rather than surface a raw ad-click URL."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com"):
        if parsed.path == "/l/":
            real = urllib.parse.parse_qs(parsed.query).get("uddg")
            if real:
                return urllib.parse.unquote(real[0])
        return None
    return href


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw


def _instant_answer(query: str, max_results: int) -> list[dict[str, str]]:
    """Fast path: DuckDuckGo's official JSON API. Empty for general searches."""
    url = f"{_INSTANT_ANSWER_URL}?{urllib.parse.urlencode({'q': query, 'format': 'json', 'no_html': '1', 'skip_disambig': '1'})}"
    data = json.loads(_fetch(url))

    results: list[dict[str, str]] = []
    abstract = (data.get("AbstractText") or "").strip()
    if abstract:
        results.append(
            {
                "title": data.get("Heading") or query,
                "url": data.get("AbstractURL") or "",
                "snippet": abstract,
            }
        )
    for topic in data.get("RelatedTopics", []):
        if len(results) >= max_results:
            break
        if not isinstance(topic, dict) or not topic.get("Text"):
            continue
        text = topic["Text"]
        title = text.split(" - ", 1)[0]
        results.append({"title": title, "url": topic.get("FirstURL") or "", "snippet": text})
    return results[:max_results]


def _html_search(query: str, max_results: int) -> list[dict[str, str]]:
    """Fallback: scrape the HTML-only search endpoint for general queries."""
    url = f"{_HTML_SEARCH_URL}?{urllib.parse.urlencode({'q': query})}"
    page_html = _fetch(url).decode("utf-8", errors="replace")

    if _BOT_CHALLENGE_MARKER in page_html.lower():
        raise _BotChallenge

    results: list[dict[str, str]] = []
    for match in _RESULT_RE.finditer(page_html):
        title = _clean(match.group("title"))
        snippet = _clean(match.group("snippet"))
        href = _resolve_href(match.group("href"))
        if not title or not href:
            continue
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


class _BotChallenge(Exception):
    pass


class WebSearchTool(BaseTool):
    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], tuple[float, list[dict[str, str]]]] = {}

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="web_search",
            description=(
                "Search the web via DuckDuckGo (free, no API key). Returns the top "
                "result titles, URLs, and snippets for a query."
            ),
            parameters=_PARAMS,
            risk=RiskLevel.HIGH,  # matches PolicyEngine's ActionKind.NETWORK default
        )

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult.fail("query is required")
        max_results = args.get("max_results") or 5
        try:
            max_results = max(1, min(int(max_results), 10))
        except (TypeError, ValueError):
            max_results = 5

        cache_key = (query.lower(), max_results)
        cached = self._cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            return self._format(query, cached[1])

        decision = ctx.policy.evaluate(ActionKind.NETWORK)
        if decision is PermissionDecision.DENY:
            return ToolResult.fail("web search blocked by policy")
        if decision is PermissionDecision.PROMPT:
            if ctx.resolve_decision(f'Search the web for "{query}"?') is PermissionDecision.DENY:
                return ToolResult.fail("web search denied by user")

        try:
            results = _instant_answer(query, max_results)
            if not results:
                results = _html_search(query, max_results)
        except _BotChallenge:
            return ToolResult.fail(
                "DuckDuckGo returned a bot-verification challenge (likely rate-limited "
                "this IP). Wait a bit before retrying, or try a more specific query."
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return ToolResult.fail(f"web search failed: {exc}")

        self._cache[cache_key] = (time.monotonic(), results)
        return self._format(query, results)

    def _format(self, query: str, results: list[dict[str, str]]) -> ToolResult:
        if not results:
            return ToolResult.ok_(f"no results for: {query}", count=0)
        lines = [f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results, 1)]
        return ToolResult.ok_("\n".join(lines), count=len(results), results=results)


def register_web_tools(registry) -> None:  # type: ignore[no-untyped-def]
    registry.register(WebSearchTool())

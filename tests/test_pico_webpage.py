import json

import httpx
import pytest

from pico_photo_bot.attribution import PhotographerFinder, resolve_attributions
from pico_photo_bot.webpage import (
    PlaywrightWebpageRenderer,
    WebpageCandidate,
    WebpageResult,
    _RawCandidate,
    select_candidates,
)


async def public_resolver(host, port):
    return ["127.0.0.1"] if host == "private.example" else ["93.184.216.34"]


def test_candidate_filtering_deduplicates_and_preserves_credit_precedence():
    raw = [
        _RawCandidate(
            "https://cdn.example/one.jpg",
            1200,
            800,
            caption="Figure caption",
            nearby_text="Photo: Nearby Credit",
            credit="Slide Credit",
        ),
        _RawCandidate("https://cdn.example/one.jpg", 1200, 800),
        _RawCandidate("https://cdn.example/logo.png", 1200, 800),
        _RawCandidate("https://cdn.example/tiny.jpg", 170, 170),
        _RawCandidate("data:image/png;base64,abc", 1200, 800),
        _RawCandidate("https://cdn.example/two.jpg", 1200, 800, nearby_text="Photo by Nearby Photographer"),
    ]
    structured = [
        {
            "@type": "ImageObject",
            "contentUrl": "https://cdn.example/one.jpg",
            "creator": {"name": "JSON-LD Photographer"},
            "caption": "Structured caption",
        }
    ]

    candidates, total = select_candidates(raw, "https://publisher.example/story", structured)

    assert total == 2
    assert [item.url for item in candidates] == [
        "https://cdn.example/one.jpg",
        "https://cdn.example/two.jpg",
    ]
    assert candidates[0].credit == "JSON-LD Photographer"
    assert candidates[0].caption == "Structured caption"
    assert candidates[1].credit == "Nearby Photographer"


class FakeButton:
    def __init__(self, container):
        self.container = container

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def is_disabled(self):
        return self.container.index == len(self.container.states) - 1

    async def click(self, **kwargs):
        self.container.index += 1


class FakeContainer:
    def __init__(self, states):
        self.states = states
        self.index = 0

    async def evaluate(self, script):
        return self.states[self.index]

    def locator(self, selector):
        return FakeButton(self)


class FakeContainers:
    def __init__(self, containers):
        self.containers = containers

    async def count(self):
        return len(self.containers)

    def nth(self, index):
        return self.containers[index]


class FakePage:
    def __init__(self, containers):
        self.containers = FakeContainers(containers)

    def locator(self, selector):
        return self.containers


@pytest.mark.asyncio
async def test_carousel_states_keep_each_live_credit_with_its_image():
    states = [
        {
            "key": f"{index}|https://cdn.example/{index}.jpg",
            "url": f"https://cdn.example/{index}.jpg",
            "width": 1000,
            "height": 700,
            "caption": f"Caption {index}",
            "credit": f"Credit {index}",
        }
        for index in range(1, 5)
    ]
    renderer = PlaywrightWebpageRenderer(action_timeout=0.2)

    raw = await renderer._extract_carousels(
        FakePage([FakeContainer(states)]), "https://publisher.example/story"
    )
    selected, total = select_candidates(raw, "https://publisher.example/story")

    assert total == 4
    assert [(item.caption, item.credit) for item in selected] == [
        (f"Caption {index}", f"Credit {index}") for index in range(1, 5)
    ]


class RecordingFinder:
    def __init__(self):
        self.calls = []

    async def find(self, page_url, page_title, candidates):
        self.calls.append((page_url, page_title, candidates))
        return {}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_page_credit_suppresses_fallback_research_for_that_candidate():
    finder = RecordingFinder()
    page = WebpageResult(
        "https://publisher.example/story",
        "Story",
        (
            WebpageCandidate(1, "https://cdn.example/one.jpg", credit="Publisher Credit"),
            WebpageCandidate(2, "https://cdn.example/two.jpg"),
        ),
        2,
    )

    attributions = await resolve_attributions(page, finder)

    assert [item.ordinal for item in finder.calls[0][2]] == [2]
    assert attributions[1].sentence == (
        "Photo credit: Publisher Credit — https://publisher.example/story"
    )
    assert attributions[2].sentence == (
        "Source: publisher.example — https://publisher.example/story"
    )


@pytest.mark.asyncio
async def test_openrouter_batches_unresolved_candidates_and_strictly_validates_results():
    requests = []

    def handler(request):
        requests.append(request)
        content = {
            "results": [
                {
                    "ordinal": 1,
                    "status": "resolved",
                    "photographer": "First Photographer",
                    "credit_line": "First Photographer / Agency",
                    "evidence_url": "https://evidence.example/one",
                    "evidence_excerpt": "Photo by First Photographer",
                },
                {
                    "ordinal": 1,
                    "status": "resolved",
                    "photographer": "Duplicate",
                    "credit_line": "Duplicate",
                    "evidence_url": "https://evidence.example/duplicate",
                    "evidence_excerpt": "Duplicate",
                },
                {
                    "ordinal": 2,
                    "status": "resolved",
                    "photographer": "Private Evidence",
                    "credit_line": "Private Evidence",
                    "evidence_url": "https://private.example/item",
                    "evidence_excerpt": "Not public",
                },
                {
                    "ordinal": 99,
                    "status": "resolved",
                    "photographer": "Unknown",
                    "credit_line": "Unknown",
                    "evidence_url": "https://evidence.example/unknown",
                    "evidence_excerpt": "Unknown ordinal",
                },
            ]
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    finder = PhotographerFinder(
        "secret", client=client, resolver=public_resolver
    )
    candidates = (
        WebpageCandidate(1, "https://cdn.example/one.jpg", alt="one"),
        WebpageCandidate(2, "https://cdn.example/two.jpg", caption="two"),
    )

    findings = await finder.find(
        "https://publisher.example/story", "Story", candidates
    )

    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert [item["ordinal"] for item in json.loads(payload["messages"][1]["content"])["candidates"]] == [1, 2]
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["tools"][0]["web_search"] == {
        "max_results": 5,
        "max_uses": 3,
        "max_total_results": 10,
    }
    assert set(findings) == {1}
    assert findings[1].credit_line == "First Photographer / Agency"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "rate-limit", "malformed"])
async def test_openrouter_failures_leave_attribution_unresolved(mode):
    def handler(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("deadline", request=request)
        if mode == "rate-limit":
            return httpx.Response(429, json={"error": "limited"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    finder = PhotographerFinder("secret", client=client, resolver=public_resolver)

    findings = await finder.find(
        "https://publisher.example/story",
        "Story",
        (WebpageCandidate(1, "https://cdn.example/one.jpg"),),
    )

    assert findings == {}
    await finder.close()
    await finder.close()
    await client.aclose()

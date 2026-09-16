import io
import shutil
from pathlib import Path

import httpx
import pytest
from PIL import Image

from pico_photo_bot.models import SourceImage, SourcePost
from pico_photo_bot.sources import (
    MAX_IMAGE_BYTES,
    PicoMediaLoader,
    PicoSourceError,
    RedditPostClient,
)
ARCHIVE_TAG = "picoarc0123456789ab"




async def public_resolver(host, port):
    return ["93.184.216.34"]


def image_bytes(image_format="PNG", *, animated=False):
    output = io.BytesIO()
    image = Image.new("RGB", (4, 3), (20, 40, 60))
    if animated:
        second = Image.new("RGB", (4, 3), (80, 100, 120))
        image.save(output, image_format, save_all=True, append_images=[second], duration=100)
    else:
        image.save(output, image_format)
    return output.getvalue()


def reddit_payload(*, gallery=False, removed=False, video=False):
    post = {
        "author": "poster",
        "permalink": "/r/pics/comments/abc/title/",
        "url_overridden_by_dest": "https://i.redd.it/single.png",
        "is_video": video,
    }
    if removed:
        post["removed_by_category"] = "moderator"
    if gallery:
        post.update(
            {
                "gallery_data": {"items": [{"media_id": "two"}, {"media_id": "one"}]},
                "media_metadata": {
                    "one": {"status": "valid", "e": "Image", "s": {"u": "https://i.redd.it/one.png"}},
                    "two": {"status": "valid", "e": "Image", "s": {"u": "https://i.redd.it/two.png"}},
                },
            }
        )
    return [{"data": {"children": [{"data": post}]}}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://www.reddit.com/r/pics/comments/AbC/title/",
        "https://old.reddit.com/r/pics/comments/AbC/title/",
        "https://redd.it/AbC",
    ],
)
async def test_reddit_canonical_and_short_urls_use_app_only_api(url):
    token_calls = 0

    def handler(request):
        nonlocal token_calls
        if request.url.path == "/api/v1/access_token":
            token_calls += 1
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.url.path == "/comments/abc"
        return httpx.Response(200, json=reddit_payload())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reddit = RedditPostClient("id", "secret", "agent", client, public_resolver)
    post = await reddit.resolve(url)

    assert post.canonical_url == "https://www.reddit.com/r/pics/comments/abc/title/"
    assert post.attribution_sentence == "Posted by u/poster on Reddit — https://www.reddit.com/r/pics/comments/abc/title/"
    assert token_calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_reddit_share_redirects_are_validated_and_token_is_cached():
    token_calls = 0
    share_calls = 0

    def handler(request):
        nonlocal token_calls, share_calls
        if request.url.path == "/r/pics/s/share-code":
            share_calls += 1
            return httpx.Response(302, headers={"Location": "/r/pics/comments/abc/title/"})
        if request.url.path == "/api/v1/access_token":
            token_calls += 1
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(200, json=reddit_payload(gallery=True))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reddit = RedditPostClient("id", "secret", "agent", client, public_resolver)
    first = await reddit.resolve("https://www.reddit.com/r/pics/s/share-code")
    second = await reddit.resolve("https://www.reddit.com/r/pics/comments/abc/title/")

    assert [image.url for image in first.images] == [
        "https://i.redd.it/two.png",
        "https://i.redd.it/one.png",
    ]
    assert second.images == first.images
    assert (share_calls, token_calls) == (1, 1)
    await client.aclose()


@pytest.mark.asyncio
async def test_reddit_token_refreshes_inside_sixty_second_window():
    tokens = []

    def handler(request):
        if request.url.path == "/api/v1/access_token":
            token = f"token-{len(tokens) + 1}"
            tokens.append(token)
            return httpx.Response(200, json={"access_token": token, "expires_in": 30})
        return httpx.Response(200, json=reddit_payload())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reddit = RedditPostClient("id", "secret", "agent", client, public_resolver)
    await reddit.resolve("https://redd.it/abc")
    await reddit.resolve("https://redd.it/abc")
    assert tokens == ["token-1", "token-2"]
    await client.aclose()


@pytest.mark.parametrize(
    "payload",
    [reddit_payload(removed=True)[0]["data"]["children"][0]["data"], reddit_payload(video=True)[0]["data"]["children"][0]["data"]],
)
def test_reddit_rejects_removed_and_video_posts(payload):
    with pytest.raises(PicoSourceError, match="no supported still images"):
        RedditPostClient._parse_post("abc", payload)


class CopyMetadata:
    def __init__(self):
        self.writes = []

    def write(
        self, source, destination, attribution, source_url, archive_search_tag
    ):
        shutil.copy2(source, destination)
        self.writes.append((attribution, source_url, archive_search_tag))


@pytest.mark.asyncio
async def test_reddit_links_are_disabled_without_credentials():
    def handler(request):
        raise AssertionError(f"disabled Reddit client made a request to {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reddit = RedditPostClient(None, None, None, client, public_resolver)

    with pytest.raises(PicoSourceError, match="unavailable until"):
        await reddit.resolve("https://redd.it/abc")

    await client.aclose()


@pytest.mark.asyncio
async def test_direct_redirect_uses_final_hostname_and_actual_image_format(tmp_path: Path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "start.example":
            return httpx.Response(302, headers={"Location": "https://cdn.example/photo"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=image_bytes("PNG"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reddit = RedditPostClient(None, None, None, client, public_resolver)
    metadata = CopyMetadata()
    loader = PicoMediaLoader(
        reddit, metadata, ARCHIVE_TAG, client, public_resolver
    )
    prepared = await loader.prepare("https://start.example/image.jpg", tmp_path)

    assert prepared.source.attribution_sentence == "Source: cdn.example — https://cdn.example/photo"
    assert prepared.files[0].filename == f"{ARCHIVE_TAG}-cdn.example-01.png"
    assert prepared.files[0].mime_type == "image/png"
    assert calls == ["https://start.example/image.jpg", "https://cdn.example/photo"]
    assert metadata.writes == [
        (
            "Source: cdn.example — https://cdn.example/photo",
            "https://cdn.example/photo",
            ARCHIVE_TAG,
        )
    ]
    assert not tuple(tmp_path.glob(".staging-*"))
    await client.aclose()


@pytest.mark.asyncio
async def test_reddit_filename_and_metadata_keep_tag_out_of_attribution(
    tmp_path: Path,
):
    attribution = (
        "Posted by u/poster on Reddit — "
        "https://www.reddit.com/r/pics/comments/abc/title/"
    )

    class SingleReddit:
        async def resolve(self, url):
            return SourcePost(
                "reddit",
                "https://www.reddit.com/r/pics/comments/abc/title/",
                "u/poster",
                attribution,
                (SourceImage(1, "https://i.redd.it/image.png"),),
                "abc",
            )

    def handler(request):
        return httpx.Response(200, content=image_bytes("PNG"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    metadata = CopyMetadata()
    loader = PicoMediaLoader(
        SingleReddit(), metadata, ARCHIVE_TAG, client, public_resolver
    )
    prepared = await loader.prepare(
        "https://reddit.com/r/pics/comments/abc/title/", tmp_path
    )

    assert prepared.files[0].filename == f"{ARCHIVE_TAG}-reddit-abc-01.png"
    assert prepared.source.attribution_sentence == attribution
    assert metadata.writes == [
        (
            attribution,
            "https://www.reddit.com/r/pics/comments/abc/title/",
            ARCHIVE_TAG,
        )
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_direct_redirect_to_private_host_is_rejected_before_request(tmp_path: Path):
    calls = []

    async def resolver(host, port):
        return ["127.0.0.1"] if host == "localhost" else ["93.184.216.34"]

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://localhost/image"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reddit = RedditPostClient(None, None, None, client, resolver)
    loader = PicoMediaLoader(
        reddit, CopyMetadata(), ARCHIVE_TAG, client, resolver
    )
    with pytest.raises(PicoSourceError, match="non-public"):
        await loader.prepare("https://start.example/image", tmp_path)
    assert calls == ["https://start.example/image"]
    assert not tuple(tmp_path.iterdir())
    await client.aclose()


@pytest.mark.asyncio
async def test_media_rejects_declared_oversize_and_animated_webp_atomically(tmp_path: Path):
    responses = [
        httpx.Response(200, headers={"content-length": str(MAX_IMAGE_BYTES + 1)}),
        httpx.Response(200, content=image_bytes("WEBP", animated=True)),
    ]

    def handler(request):
        return responses.pop(0)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reddit = RedditPostClient(None, None, None, client, public_resolver)
    loader = PicoMediaLoader(
        reddit, CopyMetadata(), ARCHIVE_TAG, client, public_resolver
    )
    with pytest.raises(PicoSourceError, match="10,000,000-byte"):
        await loader.prepare("https://images.example/large", tmp_path)
    with pytest.raises(PicoSourceError, match="Animated"):
        await loader.prepare("https://images.example/animated", tmp_path)
    assert not tuple(tmp_path.iterdir())
    await client.aclose()


@pytest.mark.asyncio
async def test_reddit_gallery_over_twenty_images_is_rejected_before_download(tmp_path: Path):
    class LargeGalleryReddit:
        async def resolve(self, url):
            images = tuple(SourceImage(index, f"https://i.redd.it/{index}.png") for index in range(1, 22))
            return SourcePost("reddit", "https://www.reddit.com/comments/abc/", "u/poster", "Posted by u/poster on Reddit — https://www.reddit.com/comments/abc/", images, "abc")

    def handler(request):
        raise AssertionError("over-count gallery must fail before download")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    loader = PicoMediaLoader(
        LargeGalleryReddit(),
        CopyMetadata(),
        ARCHIVE_TAG,
        client,
        public_resolver,
    )
    with pytest.raises(PicoSourceError, match="at most 20"):
        await loader.prepare("https://reddit.com/comments/abc/title/", tmp_path)
    assert not tuple(tmp_path.iterdir())
    await client.aclose()

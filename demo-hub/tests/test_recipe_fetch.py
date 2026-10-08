"""The guarded fetch behind recipe import (recipe_import/fetch.py): the extractor's limits, the
pinned address, the peer check, redirects, no environment proxies, and the page parsed off the
event loop. The network is a MockTransport and a fake resolver: nothing leaves the machine."""

from __future__ import annotations

import asyncio
import gzip
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from demo_hub.recipe_import import fetch as fetch_module
from demo_hub.recipe_import.fetch import (
    ImportFailure,
    Limits,
    fetch_page,
    is_public,
    limits_of,
    load_extractor,
    without_query,
)
from tests.import_fakes import PUBLIC, Net, fake_extractor, make_importer, recipe_page

LIMITS = Limits(max_bytes=5 * 1024 * 1024, timeout_s=20)


def get(net: Net, url: str, limits: Limits = LIMITS) -> Any:
    return asyncio.run(fetch_page(url, limits=limits, resolver=net.resolve,
                                  transport=httpx.MockTransport(net.handler)))


def refused(net: Net, url: str, limits: Limits = LIMITS) -> ImportFailure:
    with pytest.raises(ImportFailure) as err:
        get(net, url, limits)
    return err.value


def test_the_limits_are_the_extractors_own(tmp_path: Path) -> None:
    path = fake_extractor(tmp_path, max_bytes=1234, timeout_s=7)
    assert limits_of(load_extractor(str(path))) == Limits(1234, 7.0)
    with pytest.raises(ImportFailure) as err:
        load_extractor(str(tmp_path / "missing.py"))
    assert err.value.status == 503 and err.value.code == "import_unavailable"
    with pytest.raises(ImportFailure):
        load_extractor("")


def test_addresses_that_are_not_public() -> None:
    for address in ("127.0.0.1", "10.0.0.8", "192.168.1.1", "169.254.169.254", "::1",
                    "fe80::1", "fc00::1", "224.0.0.1", "0.0.0.0", "100.64.0.1", "240.0.0.1",
                    "::ffff:127.0.0.1", "not an ip"):
        assert not is_public(address), address
    assert is_public(PUBLIC) and is_public("2606:4700:4700::1111")


def test_the_request_goes_to_the_checked_address_with_the_real_name() -> None:
    net = Net()
    net.serve("https://blog.example/dal", "<html>dal</html>")
    page = get(net, "https://blog.example/dal")
    assert page.text == "<html>dal</html>" and page.address == PUBLIC
    assert page.url == "https://blog.example/dal"
    [sent] = net.requests
    assert sent.url.host == PUBLIC and sent.url.scheme == "https"     # pinned
    assert sent.headers["host"] == "blog.example"                     # the name, for the site
    assert sent.extensions["sni_hostname"] == "blog.example"          # and for TLS
    assert "cookie" not in sent.headers
    assert sent.headers["user-agent"].startswith("pantry-demo-hub/")


def test_a_rebinding_name_is_resolved_once_and_the_peer_is_checked() -> None:
    """A name that answers public, then loopback: the hub never asks twice, so it connects to
    the public address; a socket that ends anywhere else is dropped before the body is read."""
    net = Net()
    net.serve("https://rebind.example/r", "secret")
    net.dns["rebind.example"] = [[PUBLIC], ["127.0.0.1"]]
    page = get(net, "https://rebind.example/r")
    assert net.resolved == ["rebind.example"] and net.requests[0].url.host == PUBLIC
    assert page.address == PUBLIC
    # the socket reports 127.0.0.1 (another process racing the loopback): 403, nothing read
    net = Net()
    net.serve("https://rebind.example/r", "secret")
    net.peer["rebind.example"] = "127.0.0.1"
    err = refused(net, "https://rebind.example/r")
    assert (err.status, err.code) == (403, "not_public")


def test_private_names_and_addresses_are_refused_before_connecting() -> None:
    net = Net()
    net.dns["intranet.example"] = [["10.1.2.3"]]
    net.dns["mixed.example"] = [[PUBLIC, "127.0.0.1"]]
    for url in ("http://intranet.example/", "http://mixed.example/", "http://127.0.0.1:8090/",
                "http://169.254.169.254/latest/meta-data/", "http://[::1]/"):
        err = refused(net, url)
        assert (err.status, err.code) == (403, "not_public"), url
    assert net.requests == []


@pytest.mark.parametrize("location", ["http://169.254.169.254/latest/meta-data/",
                                      "http://localhost:8090/hub/status",
                                      "http://10.0.0.1/admin", "file:///etc/passwd",
                                      "ftp://ftp.example/x"])
def test_redirects_are_checked_again(location: str) -> None:
    net = Net()
    net.dns["localhost"] = [["127.0.0.1"]]
    net.serve("https://blog.example/old", status=301, headers={"location": location})
    err = refused(net, "https://blog.example/old")
    assert err.status == 403, location
    assert len(net.requests) == 1                  # the private target is never contacted


def test_redirects_are_followed_re_pinned_and_capped() -> None:
    net = Net()
    net.serve("https://a.example/1", status=302, headers={"location": "https://b.example/2",
                                                          "set-cookie": "s=1"})
    net.serve("https://b.example/2", "<html>here</html>", ip="151.101.1.67")
    page = get(net, "https://a.example/1")
    assert page.url == "https://b.example/2" and page.redirects == 1
    assert [r.url.host for r in net.requests] == [PUBLIC, "151.101.1.67"]
    assert "cookie" not in net.requests[1].headers         # no cookie carried to the next hop
    loop = Net()
    for i in range(7):
        loop.serve(f"https://loop.example/{i}", status=307,
                   headers={"location": f"/{i + 1}"})
    err = refused(loop, "https://loop.example/0")
    assert (err.status, err.code) == (502, "too_many_redirects")
    assert len(loop.requests) == 6                          # the first request and 5 redirects


def test_size_is_capped_at_max_bytes_including_gzip() -> None:
    limits = Limits(max_bytes=1000, timeout_s=20)
    net = Net()
    net.serve("https://big.example/a", "x" * 1001)
    assert refused(net, "https://big.example/a", limits).status == 413
    net.serve("https://big.example/b", "x" * 1000)
    assert len(get(net, "https://big.example/b", limits).text) == 1000
    # a small gzip body that inflates past the limit
    net.serve("https://big.example/c", gzip.compress(b"y" * 5000),
              headers={"content-encoding": "gzip"})
    assert refused(net, "https://big.example/c", limits).status == 413
    net.serve("https://big.example/d", gzip.compress(b"<p>ok</p>"),
              headers={"content-encoding": "gzip"})
    assert get(net, "https://big.example/d", limits).text == "<p>ok</p>"
    # a declared length over the limit is refused without reading
    net.serve("https://big.example/e", "x", headers={"content-length": "999999"})
    assert refused(net, "https://big.example/e", limits).status == 413


def test_environment_proxies_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """With HTTPS_PROXY set, a client that trusted the environment would send the request to the
    proxy, around every check; the import's client never does."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:3128")
    made: list[dict[str, Any]] = []
    real = httpx.AsyncClient

    def spy(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        made.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(fetch_module.httpx, "AsyncClient", spy)
    net = Net()
    net.serve("https://blog.example/dal", "dal")
    assert get(net, "https://blog.example/dal").text == "dal"
    assert net.requests[0].url.host == PUBLIC                     # reached the site, not a proxy
    assert made and all(k["trust_env"] is False and k["follow_redirects"] is False
                        and k["verify"] is True for k in made)


def test_a_slow_site_gives_up_at_the_extractors_timeout() -> None:
    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200)

    async def resolver(host: str, port: int) -> list[str]:
        return [PUBLIC]

    started = time.monotonic()
    with pytest.raises(ImportFailure) as err:
        asyncio.run(fetch_page("https://slow.example/", limits=Limits(1000, 0.2),
                               resolver=resolver, transport=Slow()))
    assert (err.value.status, err.value.code) == (504, "timeout")
    assert "0.2 s" in err.value.message and time.monotonic() - started < 2


def test_bad_links_are_refused() -> None:
    net = Net()
    for url in ("ftp://x.example/", "https://user:pw@blog.example/", "https:///nohost",
                "https://" + "a" * 2001 + ".example/"):
        assert refused(net, url).status == 400, url


def test_a_url_in_a_trace_has_no_query() -> None:
    assert without_query("https://blog.example/r?token=abc#step-2") == "https://blog.example/r"


def test_the_page_is_parsed_off_the_event_loop(tmp_path: Path) -> None:
    """extract() on a big page runs in a worker thread: a concurrent ping keeps ticking."""
    net = Net()
    net.serve("https://blog.example/dal", recipe_page("Dal", ["200 g red lentils"]))
    extractor = fake_extractor(tmp_path, slow_s=0.4)
    importer = make_importer(tmp_path, net, extractor)

    async def scenario() -> tuple[dict[str, Any], int]:
        ticks = 0
        done = asyncio.Event()

        async def ping() -> None:
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        pinger = asyncio.ensure_future(ping())
        try:
            return await importer.import_url("https://blog.example/dal"), ticks
        finally:
            done.set()
            await pinger

    result, ticks = asyncio.run(scenario())
    assert result["doc"]["lines"][0]["name"] == "red lentils"
    assert ticks >= 10                      # 0.4 s of parsing did not stall the loop
    calls = load_extractor(str(extractor)).CALLS
    assert calls and calls[-1] != threading.main_thread().name

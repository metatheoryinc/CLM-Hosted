import re

from fastapi.testclient import TestClient

from clm.server import create_app


PUBLIC_PAGES = [
    "/",
    "/index.html",
    "/playground",
    "/playground/",
    "/guides/claude-code",
    "/guides/claude-code/",
    "/guides/codex",
    "/guides/codex/",
    "/guides/mipmap",
    "/guides/mipmap/",
]
PUBLIC_ASSETS = ["/home.css", "/home.js", "/app.css", "/app.js"]


def client(ui=True):
    return TestClient(create_app(object(), ui=ui, turn_pick_environ={}))


def test_public_pages_support_get_and_head():
    with client() as c:
        for path in PUBLIC_PAGES:
            response = c.get(path, follow_redirects=True)
            assert response.status_code == 200, path
            assert "text/html" in response.headers["content-type"], path

            head = c.head(path, follow_redirects=True)
            assert head.status_code == 200, path
            assert head.content == b"", path


def test_home_has_shared_navigation_and_cache_busted_assets():
    with client() as c:
        html = c.get("/").text

    assert "Keep the context" in html
    assert "Match the model to the work" in html
    assert 'class="site-header"' in html
    assert 'href="/guides/claude-code"' in html
    assert 'href="/guides/codex"' in html
    assert 'href="/guides/mipmap"' in html
    assert 'href="/login"' in html
    assert 'href="/playground"' in html
    assert '<meta name="viewport"' in html
    assert re.search(r'href="/home\.css\?v=[0-9a-f]+"', html)
    assert re.search(r'src="/home\.js\?v=[0-9a-f]+"', html)


def test_guides_share_navigation_copy_controls_and_mobile_metadata():
    with client() as c:
        for path in ("/guides/claude-code", "/guides/codex", "/guides/mipmap"):
            html = c.get(path).text
            assert '<meta name="viewport"' in html, path
            assert 'class="site-header"' in html, path
            assert 'href="/login"' in html, path
            assert 'href="/playground"' in html, path
            assert 'data-copy=' in html, path
            assert re.search(r'href="/home\.css\?v=[0-9a-f]+"', html), path
            assert re.search(r'src="/home\.js\?v=[0-9a-f]+"', html), path


def test_playground_keeps_interactive_markup_and_uses_absolute_busted_assets():
    with client() as c:
        for path in ("/playground", "/playground/"):
            html = c.get(path).text
            assert "CLM Playground" in html
            assert 'id="run"' in html
            assert 'id="tpl-question"' in html
            assert re.search(r'href="/app\.css\?v=[0-9a-f]+"', html)
            assert re.search(r'src="/app\.js\?v=[0-9a-f]+"', html)


def test_assets_revalidate_and_support_head():
    with client() as c:
        for path in PUBLIC_ASSETS:
            response = c.get(path)
            assert response.status_code == 200, path
            assert response.headers["cache-control"] == "no-cache", path
            head = c.head(path)
            assert head.status_code == 200, path
            assert head.headers["cache-control"] == "no-cache", path
            assert head.content == b"", path


def test_unknown_guides_and_static_prefixes_are_not_public_pages():
    with client() as c:
        assert c.get("/guides/unknown").status_code == 404
        assert c.get("/guides/claude-code/extra").status_code == 404
        assert c.get("/home.css/extra").status_code == 404


def test_ui_false_omits_every_page_and_asset():
    with client(ui=False) as c:
        for path in PUBLIC_PAGES + PUBLIC_ASSETS:
            assert c.get(path, follow_redirects=False).status_code == 404, path
            assert c.head(path, follow_redirects=False).status_code == 404, path

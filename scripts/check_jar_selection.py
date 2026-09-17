"""Exercise the built UI with in-memory APIs; never contacts a deployment server."""
import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SERVERS = [
    {"ip": "192.0.2.8", "name": "应用服务器", "jars": ["land-es7-biz.jar", "land-file-biz.jar"]},
    {"ip": "192.0.2.9", "name": "GIS服务器", "jars": [{"name": "land-gis-biz.jar"}]},
]
NAMES = ["land-es7-biz.jar", "land-file-biz.jar", "land-gis-biz.jar"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="fixed")
    args = parser.parse_args()
    output = ROOT / "output" / "playwright"
    output.mkdir(parents=True, exist_ok=True)
    jars = {name: {"name": name, "size": 148000000} for name in NAMES}
    submitted = []
    failed_deletes = set()

    def handle(route):
        request = route.request
        path = unquote(urlsplit(request.url).path)
        status = 200
        if path == "/api/servers":
            data = SERVERS
        elif path == "/api/config":
            data = {"connection_mode": "direct"}
        elif path == "/api/jars":
            data = list(jars.values())
        elif path.startswith("/api/jars/") and request.method == "DELETE":
            name = path.removeprefix("/api/jars/")
            if name in failed_deletes:
                status, data = 500, {"error": "模拟删除失败"}
            else:
                jars.pop(name)
                data = {"success": True}
        elif path in ("/api/deploy", "/api/restart"):
            submitted.append((path, request.post_data_json))
            data = {"success": True}
        elif path in ("/api/deploy/history", "/api/scheduled-tasks", "/api/logs"):
            data = []
        elif path == "/api/deploy/status":
            data = {"status": "idle", "operation_id": None, "operation": None, "success": 0, "fail": 0, "skip": 0}
        elif path == "/static/js/socket.io.min.js":
            route.fulfill(content_type="application/javascript", body="window.io = () => ({on(){}, disconnect(){}});")
            return
        elif path == "/" or path.startswith("/static/"):
            file = ROOT / ("static/react/index.html" if path == "/" else path.lstrip("/"))
            if file.resolve().is_relative_to(ROOT / "static") and file.is_file():
                route.fulfill(content_type=mimetypes.guess_type(file)[0] or "application/octet-stream", body=file.read_bytes())
            else:
                route.fulfill(status=404)
            return
        else:
            route.fulfill(status=404)
            return
        route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())
        page.route("**/*", handle)
        page.goto("http://127.0.0.1:5011/")
        for server in SERVERS:
            page.locator(".server-item").filter(has_text=server["ip"]).locator("input[type=checkbox]").check()
        if page.get_by_role("button", name="全选", exact=True).count():
            page.get_by_role("button", name="全选", exact=True).click()
        expect(page.locator(".selected-info")).to_have_text("已选: 2 服务器, 3 JAR包")
        page.screenshot(path=output / f"{args.label}-before-delete.png", full_page=True)
        for name in NAMES[:2]:
            page.locator(".jar-item").filter(has_text=name).get_by_title("删除JAR", exact=True).click()
            expect(page.locator(".jar-item").filter(has_text=name)).to_have_count(0)
        page.screenshot(path=output / f"{args.label}-after-delete.png", full_page=True)
        print("after_delete:", page.locator(".selected-info").inner_text(), flush=True)
        expect(page.locator(".selected-info")).to_have_text("已选: 1 服务器, 1 JAR包")
        expect(page.locator(".server-item.selected")).to_have_count(1)
        expect(page.locator(".server-item.selected .ip-label")).to_have_text("192.0.2.9")

        page.locator(".deploy-actions button").filter(has_text="开始部署").click()
        expect(page.get_by_text("部署请求已提交", exact=True)).to_be_visible()
        assert submitted[-1] == ("/api/deploy", {"servers": ["192.0.2.9"], "jars": [NAMES[2]]})
        page.reload()
        page.get_by_role("button", name="全选", exact=True).click()
        page.locator(".deploy-actions button").filter(has_text="重启服务").click()
        expect(page.get_by_text("重启请求已提交", exact=True)).to_be_visible()
        assert submitted[-1] == ("/api/restart", {"items": [{"server": "192.0.2.9", "jar": NAMES[2]}]})

        jars.update({name: {"name": name, "size": 148000000} for name in NAMES})
        failed_deletes.add(NAMES[1])
        page.reload()
        page.get_by_role("button", name="全选", exact=True).click()
        page.get_by_role("button", name="删除选中(3)").click()
        expect(page.locator(".selected-info")).to_have_text("已选: 1 服务器, 1 JAR包")
        expect(page.locator(".jar-item .jar-name")).to_have_text(NAMES[1])
        expect(page.locator(".server-item.selected .ip-label")).to_have_text("192.0.2.8")
        page.get_by_role("button", name="取消全选", exact=True).click()
        expect(page.locator(".selected-info")).to_have_text("已选: 0 服务器, 0 JAR包")
        page.locator(".server-item").filter(has_text="192.0.2.8").locator("input[type=checkbox]").check()
        expect(page.locator(".selected-info")).to_have_text("已选: 1 服务器, 1 JAR包")
        assert errors == [], errors
        print(json.dumps({"result": "PASS", "requests": submitted, "page_errors": errors}, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()

import argparse
import copy
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SERVERS = [{
    "ip": "192.0.2.1", "name": "应用服务器",
    "jars": [{"name": "land-es7-biz.jar", "script": "es7.sh"}, {"name": "land-file-biz.jar", "script": "file.sh"}],
}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="health")
    args = parser.parse_args()
    output = ROOT / "output" / "playwright"
    output.mkdir(parents=True, exist_ok=True)
    servers = copy.deepcopy(SERVERS)
    requests = []
    connection_requests = []
    operation = {"operation_id": None, "operation": None, "status": "idle", "success": 0, "fail": 0, "skip": 0}
    history = [{"id": 1, "servers": ["192.0.2.1"], "jars": ["land-es7-biz.jar"], "status": "deploy:failed", "success_count": 0, "fail_count": 1}]

    def handle(route):
        request = route.request
        path = unquote(urlsplit(request.url).path)
        if path == "/api/servers":
            if request.method == "PUT":
                payload = request.post_data_json
                requests.append((path, payload))
                servers[0] = {key: value for key, value in payload.items() if key != "old_ip"}
                data = {"success": True}
            else:
                data = servers
        elif path == "/api/config":
            data = {"connection_mode": "direct"}
        elif path == "/api/jars":
            data = [{"name": jar["name"], "size": 1024} for jar in SERVERS[0]["jars"]]
        elif path == "/api/deploy/status":
            data = operation
        elif path == "/api/deploy/history":
            data = history
        elif path in ("/api/scheduled-tasks", "/api/logs"):
            data = []
        elif path == "/api/test-connection":
            connection_requests.append(request.post_data_json)
            data = {"success": True, "message": "测试中..."}
        elif path in ("/api/restart", "/api/deploy"):
            requests.append((path, request.post_data_json))
            operation.update(operation_id=f"request-{len(requests)}", operation=path.split("/")[-1], status="running")
            data = {"success": True, "operation_id": operation["operation_id"]}
        elif path == "/static/js/socket.io.min.js":
            route.fulfill(content_type="application/javascript", body="window.__handlers = {}; window.io = () => ({on(event, callback){ window.__handlers[event] = callback; }, disconnect(){}}); window.__emit = (event, data) => window.__handlers[event]?.(data);")
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
        route.fulfill(content_type="application/json", body=json.dumps(data))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []

        def record_page_error(error):
            errors.append(str(error))
            print(f"PAGEERROR: {error}", flush=True)

        page.on("pageerror", record_page_error)
        page.on("dialog", lambda dialog: dialog.accept())
        page.route("**/*", handle)
        page.goto("http://127.0.0.1:5011/")
        server = page.locator(".server-item").filter(has_text="192.0.2.1")

        page.locator(".server-search-input").fill("es7")
        expect(server.locator(".server-jar-row")).to_have_count(1)
        server.get_by_title("编辑", exact=True).click()
        expect(page.locator(".jar-config-card")).to_have_count(2)
        page.get_by_placeholder("例如：GIS服务 / 查询服务 / 批处理服务").fill("更新应用服务器")
        page.screenshot(path=output / f"{args.label}-filtered-edit.png", full_page=True)
        with page.expect_response("**/api/servers"):
            page.get_by_role("button", name="保存", exact=True).click()
        assert [jar["name"] for jar in requests[-1][1]["jars"]] == ["land-es7-biz.jar", "land-file-biz.jar"]

        with page.expect_response("**/api/test-connection"):
            server.get_by_title("测试连接", exact=True).click()
        expect(server.locator(".badge")).to_have_text("测试中")
        first = connection_requests[-1]
        with page.expect_response("**/api/test-connection"):
            server.get_by_title("测试连接", exact=True).click()
        second = connection_requests[-1]
        assert first["request_id"] != second["request_id"]
        page.evaluate("data => window.__emit('test_result', data)", {**first, "result": {"success": True, "message": "连接成功"}})
        expect(server.locator(".badge")).to_have_text("测试中")
        page.evaluate("data => window.__emit('test_result', data)", {**second, "result": {"success": False, "message": "SSH认证失败"}})
        expect(server.locator(".badge")).to_have_text("离线")
        page.screenshot(path=output / f"{args.label}-connection-failed.png", full_page=True)
        with page.expect_response("**/api/test-connection"):
            server.get_by_title("测试连接", exact=True).click()
        page.evaluate("data => window.__emit('test_result', data)", {**connection_requests[-1], "result": {"success": True, "message": "连接成功"}})
        expect(server.locator(".badge")).to_have_text("在线")

        page.get_by_role("button", name="全选", exact=True).click()
        with page.expect_response("**/api/restart"):
            server.get_by_title("重启该服务器所有服务", exact=True).click()
        assert requests[-1][1]["items"] == [{"server": "192.0.2.1", "jar": jar["name"]} for jar in SERVERS[0]["jars"]]
        deploy_button = page.locator(".deploy-actions button").filter(has_text="开始部署")
        expect(deploy_button).to_be_disabled()
        page.evaluate("window.__emit('log', {message:'land-es7-biz.jar 重启完成 (PID=123)', level:'success'})")
        expect(deploy_button).to_be_disabled()
        old_id = operation["operation_id"]
        operation.update(operation_id="other-client-task", operation="deploy", status="running")
        page.evaluate("data => window.__emit('operation_started', data)", operation)
        page.evaluate("data => window.__emit('operation_done', data)", {"operation_id": old_id, "operation": "restart", "status": "completed", "success": 2, "fail": 0, "skip": 0})
        expect(deploy_button).to_be_disabled()
        page.screenshot(path=output / f"{args.label}-running.png", full_page=True)

        page.reload()
        page.get_by_role("button", name="全选", exact=True).click()
        expect(deploy_button).to_be_disabled()
        expect(page.locator(".deploy-actions button").filter(has_text="停止部署")).to_be_visible()
        operation.update(status="failed", success=0, fail=1)
        page.evaluate("data => window.__emit('operation_done', data)", operation)
        expect(deploy_button).to_be_enabled()
        expect(page.locator(".deploy-actions button").filter(has_text="停止部署")).to_have_count(0)
        page.get_by_role("button", name="历史记录").click()
        expect(page.locator(".history-pane")).to_contain_text("成功 0，失败 1")
        page.screenshot(path=output / f"{args.label}-history.png", full_page=True)
        assert errors == [], errors
        print(json.dumps({"result": "PASS", "requests": requests, "connection_attempts": len(connection_requests), "page_errors": errors}, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()

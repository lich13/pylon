# Chiphell 本机工具

由 pylon 统一维护。腾讯云负责浏览器、抓取、比价与投递，BE7000 的 FRP 仅提供家宽出口。

- `sync-brave-cookies.command`：从本机 Brave 读取 Cookie，经 SSH 加密传输并事务导入，不落地明文导出文件。
- `status.command`：检查代理状态。
- `deploy.command`：部署 FRP，执行前必须暂停 Chiphell 并等待在途任务结束。
- `uninstall.command`：卸载专用 FRP。

远端使用 `pylon chiphell status/test/baseline/pause/resume/import-cookies`。兼容入口 `chiphell-proxyctl` 调用相同实现；Cookie 导入从标准输入接收 JSON。`baseline` 重设新帖基线，日常 Cookie 同步不需要执行。

代理固定使用 `socks5://127.0.0.1:18080`，`CHIPHELL_REQUIRE_PROXY=1`，不直连回退。`UPSTREAM_UNAVAILABLE` 表示上游站点暂不可达。

需要 Node.js 22+、macOS Keychain、sqlite3，以及可用的 `tencent-cloud` SSH 配置。测试：`npm test`。

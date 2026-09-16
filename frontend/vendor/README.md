# 第三方文件

| 文件 | 来源 | 版本 | 许可 |
|---|---|---|---|
| uPlot.iife.min.js | https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.iife.min.js | 1.6.32 | MIT |
| uPlot.min.css | https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/uPlot.min.css | 1.6.32 | MIT |

只有 uPlot 一个依赖，直接放进仓库而不是挂 CDN：控制台跑在实验机上，可能没有外网。

升级时换掉这两个文件即可，注意 1.6.x 的文件名是 `uPlot.iife.min.js`（不是 `uPlot.min.js`），
它把全局 `uPlot` 暴露给 `app.js` 使用。

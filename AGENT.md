# 项目方案

## 字幕设置与任务资源限制

- `web/templates/setting/subtitle.html` 负责字幕服务配置，提供 OpenSubtitles 官网链接和任务设置入口。
- `web/templates/setting/subtitle_task_settings.html` 为独立任务设置页，经登录保护的 `/subtitle_task_settings` 路由加载，兼容现有 `navmenu` 的 POST 加载及浏览器历史恢复。
- 子页面保留导航栏“设置 → 字幕”选中状态，提供返回字幕和共享任务中心入口。
- 资源限制按上传队列与空间、进程与任务预算、检测与记录分组；配置键、安全范围和后端执行语义保持不变。
- `web/static/js/subtitle-task-settings.js` 复用 `SubtitleTasks.request`，管理读取、保存、字段校验及失败反馈。初次读取失败禁止保存，请求期间禁止重复操作，离开页面后旧请求不得更新新页面。
- `/subtitle/tasks/settings` 继续作为唯一策略读写接口，管理员权限与新任务配置快照规则由现有后端负责。

## 验证原则

模板渲染与字段迁移完整性检查、JS 语法检查、相关字幕任务回归测试；浏览器检查页面跳转、配置和官网入口独立响应、窄屏布局、读取/保存失败与校验。预览使用模拟接口时不宣称真实后台联调通过。

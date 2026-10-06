---
version: alpha
name: NAStool
description: 中文媒体与 NAS 管理控制台，沿用 Tabler 分组卡片与表单。
colors:
  primary: "#206bc4"
  background: "#f1f5f9"
  text: "#1d273b"
  border: "#e6e7e9"
typography:
  sans:
    fontFamily: '-apple-system, BlinkMacSystemFont, San Francisco, Segoe UI, Roboto, Helvetica Neue, sans-serif'
omitted:
  - section: rounded
    reason: 沿用 Tabler card、btn、form-control 的现有圆角。
  - section: spacing
    reason: 沿用 Tabler container-xl、page-body、row、gap 工具类。
  - section: components
    reason: 组件规范及运行时映射见正文。
---

# NAStool 界面约定

## 产品与视觉方向

面向管理媒体库和 NAS 的用户，中文界面以设备控制台为参考：明确分组、适中密度、可解释的资源限制。沿用项目现有 Tabler 风格，不为单一页面引入新字体、配色或装饰动画。用户市场不由界面语言推断。

## 运行时样式来源

`web/static/css/tabler.min.css` 为基础 token 和组件的唯一来源，`web/static/css/style.css` 为项目已有覆盖；本文记录现状，不生成或复制 CSS。

| 角色 | 运行时映射 | 使用位置 |
| --- | --- | --- |
| 主操作 | `--tblr-primary` / `.btn-primary` | 保存任务限制 |
| 背景、文字、边框 | `--tblr-body-bg`、`--tblr-body-color`、`--tblr-border-color` | 页面、卡片、表单 |
| 字体 | `--tblr-font-sans-serif` | 页面标题、正文、字段标签 |
| 反馈 | `.alert-info`、`.alert-success`、`.alert-danger` | 读取、保存、错误 |

以上颜色为浅色主题基线，暗色主题由现有 Tabler 变量切换。新增模板不写死颜色。

## 页面与组件

- 使用 `container-xl`、`page-header`、`page-body`；长表单按文档自然滚动，不固定高度。
- 字幕首页仅保留服务配置卡片；字幕库页头提供等尺寸的任务中心和任务设置入口，资源设置继续使用独立子页面。
- 设置页分三张卡片，每组用 fieldset/legend；手机单列、中屏两列、宽屏三列。
- 图标复用 `macro/svg.html` 的 Tabler 图标，操作始终带文字。
- 字段标签标明单位；用途、默认值和范围通过相邻“？”按钮的 Bootstrap tooltip 展示，沿用基础设置的 `form-help` 尺寸与悬停/聚焦方式，不挤占表单布局。字幕库状态说明使用相同问号外观，但继续打开原有弹窗。去除字幕任务页重复的页面、分组及底部说明，读取完成不常驻提示。
- 原生数字输入有对应 label，`aria-describedby` 保留帮助关联；校验错误附加关联，不覆盖说明。
- 主按钮提交配置，次按钮重新读取；请求中禁用重复操作，失败保留填写内容。
- OpenSubtitles 配置弹窗的说明区仅保留 API Consumers 快捷按钮，以“获取 API Key”标题说明用途，不增加重复官网入口；首页服务卡片只打开配置。
- 外链新窗口打开，提供可访问名称和 `noopener noreferrer`，不得嵌套在配置触发器中。
- 不增加动画；继承既有按钮焦点、悬停和禁用状态。

## 范围与依据

业务依据为 README 的字幕任务说明和 `app/helper/subtitle_tasks.py` 的策略约束。持久化及管理员校验继续由 `web/main.py` 的现有接口负责。本次只整理字幕相关页面，不扩展到其他设置页的视觉改造。


## 特殊集确认对话框

后台操作的状态协议与特殊集确认共用 `UX-CONTRACT.md`。新增全局任务反馈复用 Tabler
card/list-group、按钮和 Bootstrap modal，沿用现有颜色、字体、圆角及自然文档滚动。
接收与排队使用中性文字，真实终态才显示完成；任务结果以文本展示，完成不抢焦点。

滚动表面由 `style.css` 全局基线统一：`--nt-scrollbar-thumb` 映射 Tabler muted，
track 映射 bg-surface，hover/active 映射 body-color；宽度保持可操作，forced-colors
使用系统颜色。已有原生滚动外观原先没有显式映射，本次记录并补齐这一共享 owner。

- 复用全局 `navigation.html` 的 Bootstrap modal 和 Tabler 表单/列表样式；共享实现为 `rename/special_confirmation.html` 和 `special-confirmation.js`，未识别列表及历史使用同一流程。
- 作品查询 → 明确选择作品 → 季/单集详情 → 勾选核对 → 重新识别整理；不默认选择候选，不自动保存跨文件映射。原生季下拉采用系统弹出层，作品和剧集使用原生单选。
- 窗口内查询状态用 live region；保留原失败结果供展开查看，失败可重试或暂不处理。查询词属于临时确认上下文，不进入导航 URL。长文件名和简介换行，弹窗正文滚动、操作区保持可见。
- 请求过期时忽略响应；确认时禁用重复提交和关闭，成功刷新所属列表并继续待确认队列。外链由后端依据 TMDB 类型及数字 ID 生成。

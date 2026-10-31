# 电影、电视剧与动漫识别测试

测试验证常见文件名、作品匹配和季集保护规则是否稳定。通过测试不等于任意文件都能自动识别，也不能据此推算真实资源的识别准确率。

## 运行

在安装项目依赖后，从仓库根目录运行：

```bash
python3 -m tests.run_recognition
```

该入口在导入应用之前创建临时配置和 SQLite 数据库，默认关闭 LLM，不读取已有用户配置。测试完成后删除临时目录。TMDB、LLM 和下载服务使用测试数据或模拟接口；主测试进程禁止真实 DNS、socket 连接，即使业务代码捕获连接异常，未模拟的联网尝试仍会使测试命令失败。

可以指定模块、测试类或方法进行定向检查，例如：

```bash
python3 -m tests.run_recognition tests.test_meta_recognition_matrix
python3 -m tests.run_recognition tests.test_media_identity_matrix
```

退出码为 0 表示全部通过；失败、模块加载错误或未模拟的联网尝试返回非 0。普通入口 `python3 -m tests.run` 仍需要已配置并初始化的 `NASTOOL_CONFIG`，缺少配置或测试失败都会返回非 0；完整识别验证应使用上面的隔离入口。

## 覆盖层次

| 层次 | 验证内容 | 主要测试 |
| --- | --- | --- |
| 电影文件名 | 中英文片名、年份、数字标题、资源技术标签、剪辑版本、片名与季集标记边界 | `test_metainfo`、`test_meta_recognition_matrix`、`test_meta_parser_boundaries`、`test_fractional_versions` |
| 电视剧文件名 | S/E、EP、Season/Episode、1x03、中文季集、整季、连续多集、总集数说明、文件格式变体 | `test_meta_recognition_matrix`、`test_meta_recognition_plan`、`test_meta_parser_boundaries` |
| 动漫文件名 | 字幕组、中英混合标题、数字片名、罗马数字季号、完结标记、技术标签 | `test_metainfo`、`test_meta_recognition_matrix`、`test_meta_parser_boundaries` |
| 最终作品身份 | 电影/电视剧/动漫、动画电影、作品 ID 与正式季集，公开标题入口及真实临时文件入口 | `test_media_identity_matrix`、`test_media_recognition_integrity`、`test_media_cn_fallback` |
| 发布编号映射 | 明确作品规则、LLM 季绑定、失败回滚、缓存与 IMDb 命中后仍检查编号 | `test_llm_season_binding`、`test_media_cache_numbering`、`test_media_recognition_integrity` |
| 特殊内容 | 小数集、OVA/OAD/SP/SPECIAL、S00、Extras、完整证据、重复/冲突/缺失数据、范围保护 | `test_fractional_versions`、`test_special_episodes`、`test_meta_multi_episode_guards` |
| 特殊集二次确认 | 只读查询、正式链接、明确确认、文件变化、目标消失、单文件作用域与配置冲突；模拟接口浏览器交互 | `test_special_confirmation`、`test_special_confirmation_ui.js` |
| 审查边界 | 自定义词先行、单次提取、真实搜索分页、标题归一化、无硬链接发布回退、竞争目标及失败记录 | `test_review_regressions` |
| 下游处理 | RSS/搜索、下载选集、手动季集覆盖、任务冲突、保留源文件、目标碰撞、失败重试 | `test_media_cache_numbering`、`test_meta_parser_boundaries`、`test_sync_reliability`、`test_special_episodes` |
| 手动 EP 优先级 | 数字标题与小数集、固定集数及定位偏移、作品搜索/缓存/ID 入口一致性、特殊内容目标核验与覆盖保护、确认窗口及资源字段保留 | `test_manual_episode_precedence` |
| 手动 EP 表单 | 固定 EP 无定位规则提交、区间筛选/偏移限制、失败恢复、键盘重试、桌面与窄屏；WebKit 无头运行 | `test_manual_episode_ui.js` |
| 集数与筛选保护 | 偏移后非负性、范围顺序、第 0 集定位及标题、目录过滤原因、待恢复凭据与完成去重；保留强制覆盖和普通未收录集数策略 | `test_episode_transfer_guards` |
| 剧名完整性 | 数字标题与明确集号、中文括号和短英文标题、发布组缓存隔离、同名候选重试、缺分类动漫详情核验及失败回滚 | `test_series_name_integrity` |
| 识别链路约束矩阵 | 人工编号不被改写、未知证据不扩大不降级、缺季不改选同名作品、缓存准入、编号快照、范围不压缩；覆盖名称、缓存、LLM 直出 ID、文件、绑定、下载任务、特殊集/序数季、IMDb 入口 | `test_recognition_invariants` |
| 跨分支识别回归 | 特殊标签与小数集联合约束、源剧名/源季保全、LLM 单点和独立季名证据、ID/年份及缓存冲突、外部多作品消歧、查询去重及失败重试 | `test_recognition_special_crossings`、`test_recognition_llm_crossings`、`test_recognition_query_crossings` |

文件名解析阶段的动漫常先得到电视剧类型；TMDB 电视剧详情中的动画分类才决定最终 `ANIME` 类型。动画电影仍属于 `MOVIE`。测试分别检查解析阶段和最终分类，不能用“文件名解析为 TV”直接判定动漫识别失败。

跨分支回归断言正确结果，不保留审查阶段“断言错误行为存在”的临时探针。查询用例分别检查 SDK 调用和模拟 HTTP 传输边界：同一识别/文件批次内可以复用明确的空结果和原始候选，不复用身份确认结论；失败不作为空结果缓存，下一次识别可以重试。查询键保留类型、年份、分页、语言及服务配置，名称去重仍允许正式中文兜底展开此前未展开的配置别名。

手动转移明确指定 EP（固定集数或成功匹配的集数定位规则）时，目标集数优先于文件名的小数集判断；固定集数也不会被旧定位规则在目录扫描时过滤。小数集保护路径中的目标季及范围内每一集仍须通过 TMDB 核验，并保留下载任务约束和目标文件冲突检查。空规则、未匹配规则及仅填写偏移量均不视为明确 EP；没有手动 EP 时，原有小数集确认规则保持生效。

普通文件在作品搜索、缓存命中和显式 TMDB ID 三个入口应用相同手动季集覆盖。特殊集确认可处理单个数字标题歧义，但仍拒绝多集范围只确认一端、文件作用域错误及失效目标；解析不会因该歧义吞掉分辨率和来源字段。共享表单允许固定 EP 或合并集范围不填定位规则，逗号筛选区间及单独偏移仍需要定位规则。浏览器回归使用 `node tests/test_manual_episode_ui.js`，需可用的 Playwright WebKit；截图、跟踪与报告保存到 `docs/validation/manual-episode-precedence/`。

手动集数的格式、范围顺序和偏移后非负性在识别/转移前检查；合法负偏移和第 0 集继续可用，不因普通人工集号未被 TMDB 收录而强制拒绝。目录空筛选结果按实际首个排除原因汇总路径、后缀、定位或区间、大小；监控空目录继续正常跳过，下载整理及扫描故障继续失败。字幕回归区分有待恢复凭据的失败任务与完整历史且无凭据的已完成任务，后者不重复字幕或历史写入。

已知片名解析缺口（未知发布组作片名、纯数字括号片名、中文季范围、中英文连写目录名）保留在 `tests/test_recognition_naming_gaps.py`，不在默认入口中，需单独运行：`python3 -m tests.run_recognition tests.test_recognition_naming_gaps`。

原始 `tests/cases/meta_cases.py` 有 61 条非空命名样例，期望类型为电影 17 条、电视剧 44 条。新增矩阵的同义格式变体用于检验规则一致性；它们和 unittest 方法数都不是独立真实作品数量。第三方 anitopy 的已知失败样例不计入本项目通过率。

## 保证范围与后续真实验证

- 成功路径必须同时符合已知片名、类型、作品 ID 和季集预期；无法获得可靠证据的特殊集应保持未确认，不能为了提高命中数量强行猜号。
- 模拟候选及详情可以验证匹配逻辑，不能证明实时 TMDB/Bangumi 的候选排序、数据完整性、语言差异或真实 LLM 输出质量。
- 按日期命名、任意累计集号、分割放送/Part/Cour 与正式季号关系、未知裸四位数字和缺少片名的歧义资源，不承诺自动匹配；仍受现有规则及显式绑定约束。
- 若要评估“实际资源中大部分是否识别正确”，需要一份独立真实样本：原始文件名、预期媒体类型、TMDB 作品 ID、正式季集，以及预期跳过原因。样本应覆盖使用中的站点、命名方式和同名作品，保留未用于修规则的验证集，分别统计正确、误匹配、未识别和保护性跳过。
- 本地测试不访问 NAS，也不执行真实媒体服务器联调。转移测试只操作临时文件。

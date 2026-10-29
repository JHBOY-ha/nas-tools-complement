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
python3 -m tests.run_recognition tests.test_cour_episode_recognition
```

退出码为 0 表示全部通过；失败、模块加载错误或未模拟的联网尝试返回非 0。普通入口 `python3 -m tests.run` 仍需要已配置并初始化的 `NASTOOL_CONFIG`，缺少配置或测试失败都会返回非 0；完整识别验证应使用上面的隔离入口。

## 覆盖层次

| 层次 | 验证内容 | 主要测试 |
| --- | --- | --- |
| 电影文件名 | 中英文片名、年份、数字标题、资源技术标签、剪辑版本、片名与季集标记边界 | `test_metainfo`、`test_meta_recognition_matrix`、`test_meta_parser_boundaries`、`test_fractional_versions` |
| 电视剧文件名 | S/E、EP、Season/Episode、1x03、中文季集、整季、连续多集、总集数说明、文件格式变体 | `test_meta_recognition_matrix`、`test_meta_recognition_plan`、`test_meta_parser_boundaries` |
| 动漫文件名 | 字幕组、中英混合标题、数字片名、罗马数字季号、完结标记、Cour/Part 分部编号、技术标签 | `test_metainfo`、`test_meta_recognition_matrix`、`test_meta_parser_boundaries`、`test_cour_episode_recognition` |
| 最终作品身份 | 电影/电视剧/动漫、动画电影、作品 ID 与正式季集，公开标题入口及真实临时文件入口 | `test_media_identity_matrix`、`test_media_recognition_integrity`、`test_media_cn_fallback` |
| LLM 主导 | 原始语义检索、跨语言篇章、多季合集、绑定下载、逐文件集号、完整范围、异常保留、单集元数据确认、上下文缓存隔离、RSS预筛选、搜索过滤、下载选集及待处理历史 | `test_llm_primary` |
| 发布编号映射 | 明确作品规则、LLM 季绑定、失败回滚、缓存与 IMDb 命中后仍检查编号 | `test_llm_season_binding`、`test_media_cache_numbering`、`test_media_recognition_integrity` |
| 特殊内容 | 小数集、OVA/OAD/SP/SPECIAL、S00、Extras、完整证据、重复/冲突/缺失数据、范围保护 | `test_fractional_versions`、`test_special_episodes`、`test_meta_multi_episode_guards` |
| 特殊集二次确认 | 只读查询、正式链接、明确确认、文件变化、目标消失、单文件作用域与配置冲突；模拟接口浏览器交互 | `test_special_confirmation`、`test_special_confirmation_ui.js` |
| 审查边界 | 自定义词先行、单次提取、真实搜索分页、标题归一化、无硬链接发布回退、竞争目标及失败记录 | `test_review_regressions` |
| 下游处理 | RSS/搜索、下载选集、手动季集覆盖、任务冲突、保留源文件、目标碰撞、失败重试 | `test_media_cache_numbering`、`test_meta_parser_boundaries`、`test_sync_reliability`、`test_special_episodes` |

文件名解析阶段的动漫常先得到电视剧类型；TMDB 电视剧详情中的动画分类才决定最终 `ANIME` 类型。动画电影仍属于 `MOVIE`。测试分别检查解析阶段和最终分类，不能用“文件名解析为 TV”直接判定动漫识别失败。

原始 `tests/cases/meta_cases.py` 有 61 条非空命名样例，期望类型为电影 17 条、电视剧 44 条。新增矩阵的同义格式变体用于检验规则一致性；它们和 unittest 方法数都不是独立真实作品数量。第三方 anitopy 的已知失败样例不计入本项目通过率。

## 分部编号与批次目标冲突

`Cour 3 [37]`、`Part 3 [37]` 中的分部编号不参与集号解析，独立的 `[37]` 保留为第 37 集。中日文分部标记、合集 `[25-37]`、版本集号 `[37v2]` 以及同时含 `S4` 的 RSS 标题也在回归范围内。原发布名保留供 LLM 和正式季号映射核验；单凭 `Cour 3` 不推断第 3 季，也不制造第 3 集。

普通剧集在批次首次发布前检查最终目标。不同源文件指向同一目标时，冲突组全部保留源文件并登记待处理，整批返回非完全成功；无冲突文件仍可继续。同一 inode 的源路径别名可重试，模板确实区分了文件名的不同版本也可正常处理。小数集、特殊集和 Extras 继续使用原有的更严格校验。

## 保证范围与后续真实验证

- 成功路径必须同时符合已知片名、类型、作品 ID 和季集预期；无法获得可靠证据的特殊集应保持未确认，不能为了提高命中数量强行猜号。
- 模拟候选及详情可以验证匹配逻辑，不能证明实时 TMDB/Bangumi 的候选排序、数据完整性、语言差异或真实 LLM 输出质量。
- LLM 主导模式由模型判断命名和编号语义，程序校验目标存在性及完整性；模拟测试能证明流程采用模型判断，不能证明真实模型一定判断正确。保守模式继续使用规则。跨季单文件、离散季范围与缺少足够证据的资源保持待处理。
- 若要评估“实际资源中大部分是否识别正确”，需要一份独立真实样本：原始文件名、预期媒体类型、TMDB 作品 ID、正式季集，以及预期跳过原因。样本应覆盖使用中的站点、命名方式和同名作品，保留未用于修规则的验证集，分别统计正确、误匹配、未识别和保护性跳过。
- 本地测试不访问 NAS，也不执行真实媒体服务器联调。转移测试只操作临时文件。

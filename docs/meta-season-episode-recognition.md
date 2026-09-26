# 剧集名称与季集识别

NCOP、ED、PV、SP 不再由代码强制跳过。需要过滤时，在“文件名转移忽略词”（`media.ignored_files`）配置规则；未配置时正常识别，独立名称识别不受该配置影响。

以下可选规则匹配明确括号标签，已有规则用 `;` 分隔追加：

```regex
(?i:[\[【]\s*(?:NCOP|NCED|ED|PV|SP)\d*(?:\s*[&+＋]\s*(?:NCOP|NCED|ED|PV|SP)\d*)*\s*[\]】])
```

仅匹配当前文件名（含扩展名），不会因父目录合集标记过滤正片，不匹配片名 RED 或制作组后缀 -SP。无括号标签需按实际样例另配。主整理流程在媒体识别前过滤，沿用现有忽略词日志和返回状态，不再提供专项拦截的失败状态或下载器删源保护。本次未自动修改用户配置。

`[01.5]`、`E01.5`、` - 01.5` 等明确小数集会保留原始编号。未取得具体 TMDB 单集的可靠映射时，文件跳过整理并返回失败，不默认转成 S01 或 S00。单独的 `[5.1]` 按声道信息处理。

可以在 `media.fractional_episode_mappings` 中记录人工核实过的映射，例如：

```yaml
media:
  fractional_episode_mappings:
    - tmdb_id: 42
      source_episode: "01.5"
      target_season: 0
      target_episode: 3
      episode_title: "Bonus Story"
```

也可用 `air_date: "2024-01-01"` 代替 `episode_title`，并可选填 `source_season` 限定发布季。系统会查询该作品的 TMDB 目标季，核实目标集存在且标题或日期与配置一致后，使用 TMDB 返回的正式季集号；映射重复、证据不符或查询失败均跳过。若文件名在小数集后有明确单集标题（如 `Show [01.5] - Bonus Story.mkv`），且它在 TMDB 全部季中唯一完全匹配，也可自动确认。

规则还识别 `1x03`、`Season 4`、前置 `Episode 5`、`共24集` 与 `第II季` 等格式。仅空格分隔的裸四位集号仍是已知限制，未扩展映射策略。

## 两阶段小数集识别

文件批次先识别普通整数集，再处理小数集，最后转移。小数集识别失败通过 `skip_reason` 返回，正常文件仍可入库；整批返回非完全成功，未确认文件保留原处且不写入成功黑名单。文件名转移忽略词仍在识别前执行，不自动改变其配置。

明确的 `S01E13.5.1080p`、`EP1.5`、`第6.5集/話`、`[13.5]`、` - 1.5` 和 `0.5A/B` 可进入确认流程。编号按字符串规范化，整数部分去前导零、字母转大写，小数部分保留；`01.5` 与 `1.5` 配置视为同一键，重复配置拒绝处理。声道 `[5.1]` / `[7.1]`、`AAC5.1` 及集号后分辨率、年份不作为小数集。格式不完整的明确小数标记保留待确认。

作品身份优先来自手动绑定或精确下载任务；仅按下载器任务键复用同批已确认身份，不按同目录推断。只有小数集时可独立检索作品。新下载上下文使用 `numbering: tmdb` 表明正式编号；明确的 `release` 上下文按发布编号约束校验，旧上下文语义不明且与映射冲突时保守跳过。

无人工配置时，先查特别篇，再查作品全部季；所有季成功返回完整集列表后才确认唯一性。证据为单集标题完全匹配，或 TMDB 标题/简介中的明确发布编号，例如“第一季13.5”“第13.5集”“Episode 13.5”“#0.5A”。普通小数、评分、时长不作编号证据。标题和编号冲突、多个候选、缺失季数据均不整理。查询按作品和语言在批内复用，下批可重试失败查询。

确认后使用 TMDB 的整数季集，不取整、不默认 S00。TMDB 接口的季号和集号是 `int32`；Jellyfin 特别篇命名也应遵循元数据提供商编号。2026-09-25 核实的《进击的巨人》示例：13.5 → S00E01，3.5 → S00E07，3.25 → S00E13，3.75 → S00E14，0.5A/B → S00E15/16。这里只作为回归数据和说明，不安装作品专属默认映射。

- [TMDB 单集接口](https://developer.themoviedb.org/reference/tv-episode-details)
- [进击的巨人特别篇及发布编号说明](https://www.themoviedb.org/tv/1429/season/0)
- [Jellyfin 特别篇规范](https://jellyfin.org/docs/general/server/media/shows/#show-specials)

转移前核对小数集实际目标路径；批内同名不同源文件跳过相关小数集，已有不同文件不按大小覆盖。同一硬链接可安全重试。不自动移动旧文件或迁移历史名称。

## 电影剪辑版本与命名

`{edition}` 保持来源和技术标记，如 `BluRay ReMux`、`WEB-DL DV`，继续用于既有资源过滤。新增 `{cut}` 独立输出 `Extended`、`Directors Cut`、`Theatrical`、`Unrated`、`Uncut`，支持对应中文及英文别名，按上述顺序去重组合。仅识别年份后发布信息或独立括号标签，不推断未标注的院线版；LLM 不生成 cut，误放在技术字段中的已知剪辑标记会被清理。HDR10+ 的加号不再丢失。

普通单文件电影可选模板：

```text
{title} ({year})/{title} ({year}) - {videoFormat} {edition} {videoCodec} {cut}
```

旧模板和现有文件不会自动变化。`{edition}` 已含 `{effect}` 内容，一般不应重复使用；所有命名字段相同的不同资源仍可能撞名，可额外使用制作组或原文件名区分。分段电影继续使用原有 part 规则。

Jellyfin 识别的是替换后的文件名：同一电影各版本在同目录，文件名前缀须与目录名一致，后接 ` - ` 和自定义版本标签；cut 不是 Jellyfin API 字段。[多版本命名规范](https://jellyfin.org/docs/general/server/media/movies/#multiple-versions)

剪辑版也支持“片名＋完整剪辑短语＋年份＋资源信息”的发布格式，例如 `Rambo.Extended.Cut.2008.BluRay.1080p` 解析为片名 `Rambo`、年份 `2008`、`cut=Extended`。年份前仅接受明确完整短语或中文版本标签，须有有效片名前缀及年份后的资源标记；不全局删除片名里的 Cut/Extended，也不能保证所有歧义片名均可自动区分。

审查补充：完整日期（如 `Show - 2022.08.01`）及整数集后的 `8bit/10bit/12bit/16bit`、分辨率标签不进入小数集确认。`S01E01-1080` 等四位裸数字终点保留为单集，`E01-03` 仍支持多集。新建整季任务另存发布季范围，仅该范围内已确认映射到 S00 的小数特别篇可越过主季约束；选集任务与旧上下文仍严格校验。已确认特别篇保留跨目录历史去重；预检源文件消失不会终止无关文件的处理。


## 动漫特殊集与 Extras（2026-09-26）

识别分为发布名解析、主体身份确认和具体内容确认三步。`OVA`、`OAD`、`SP`、`Special` 是发布标签，不是固定的媒体类型或正式集号。括号字段和明确发布分隔符可识别；不会全局删去片名中的 Special。`Ma10p` 等已知技术标签不进入片名，原名、发布编号、制作组和编码仍保留。

主体身份优先使用明确绑定或精确下载任务；否则用清理后的片名和可选外部候选名称查询 TMDB，核对名称及动画分类。LLM/Bangumi 只提供名称提示，不能生成正式季集。特别篇先查 Season 0，再查全部季；标题或明确标签编号必须唯一，部分列表和证据冲突均保源。`OVA01` 不等于 `S00E01`，唯一一个特别篇也不能直接猜配。文件带明确 S00 编号则校验该集存在。

独立 OVA 剧集按 TMDB `Video` 类型的自身编号核验；独立单集电影需唯一完整片名及明确年份、独立作品标题或人工绑定。无证据的 OVA 不会当作主体电影，也不会降级为 Extras。示例春物文件会解析出完整罗马字片名、`Kamigami&VCB-Studio`、1080p、X265、FLAC；作品确认后仍须核验具体 OVA，不能自动补 S00E01。

所有自动映射输出使用 TMDB 默认季集顺序。Plex/Jellyfin/Emby 必须使用匹配的元数据顺序；本工具不自动改代理、TVDB/DVD/Absolute 顺序或特别篇播放时间线。现有用户命名模板继续生效。新整季任务可按明确发布季范围收纳已确认特别篇，旧任务和选集任务仍严格校验。

### 人工映射配置

以下 ID/集号仅说明配置结构，应替换为已核实的实际目标：

```yaml
media:
  special_episode_mappings:
    - source_type: tv
      source_tmdb_id: 65676
      kind: OVA
      source_filename: "Show [OVA].mkv"
      target:
        media_type: tv
        tmdb_id: 65676
        season: 0
        episode: 2
```

有编号时可用 `source_number: "1"`，有单集标题时可用 `source_title`，并可加 `source_season` 限定发布季。若无编号且无标题，必须填写完整 `source_filename`（包含扩展名）。全部已填写来源条件必须匹配。电影目标使用 `media_type: movie` 和 `tmdb_id`，不填写季集。重复规则、无效目标或人工指定与配置冲突均停止整理；所有映射仍验证目标实际存在。现有手动识别的明确作品及单集选择也会经过此校验。

### 可选本地 Extras

```yaml
media:
  extras:
    enabled: true
    server_profile: jellyfin  # jellyfin / plex / emby，启用时必填
```

默认关闭且不修改已保存配置。启用后，独立标签 NCOP/NCED/OP/ED 归普通附加内容，PV/TRAILER 归预告，INTERVIEW 归访谈，BEHIND THE SCENES 归幕后。忽略词优先，SP/OVA/OAD 仍走正式内容确认。第一版仅支持作品级视频 Extras，不自动分配到某一集，不开启主题音乐/背景视频功能。

| 内容 | Jellyfin | Plex | Emby |
| --- | --- | --- | --- |
| 普通附加内容 | `other` | `Other` | `extras` |
| 预告 | `trailers` | `Trailers` | `trailers` |
| 访谈 | `interviews` | `Interviews` | `interviews` |
| 幕后 | `behind the scenes` | `Behind The Scenes` | `behind the scenes` |

目标为本次明确指定的库内作品目录，或该主体在正片历史中唯一有效的作品目录。多个库有同一作品、历史目录与当前模板不一致或无既有作品时，须指定目标目录，不凭源文件父目录猜测。文件名保留原发布名和标签，仅清理路径非法字符。明确附加内容可低于普通视频体积门槛，其余文件保持原过滤规则。

支持本地硬链接、软链接、复制、移动；Rclone/Minio 模式不提供本地原子发布保障，返回错误并保源。目标同名不同文件不覆盖；同 inode 可重试。复制/移动遇到记录失败会保留源和 `.extra-pending` 暂存链接，后续相同源重试校验暂存内容再补写记录；移动在记录和成功黑名单均完成后才删源。不要将正在使用的暂存链接当作视频重新整理。

并发整理对完整发布流程加锁，跨进程竞争时保源等待重试。目标旁的 `.extra-lock` 是持久锁文件，进程退出会自动释放系统锁，不应在运行中删除该文件。复制进程中断后，重试只会重建尚未发布的残缺暂存文件；已经与目标关联的暂存文件不会被覆盖。

下载任务保存的特殊集目标仅用于约束，不能覆盖成员文件自己的编号或跳过 TMDB 核验。未确认的特殊集、小数集及本地 Extras 不参与正片下载、整季去重或缺集统计；已有缺集需求保持不变。TMDB 描述中的小数及范围（如 `OVA 1.5`、`OVA 1-OVA 2`）不作为整数发布编号证据。

Extras 单独写 `EXTRA_TRANSFER_HISTORY`，不进入正片入库历史、缺集统计、正片自动字幕下载及 NFO 生成。批次未完全成功时返回失败，但正常文件继续。媒体库刷新复用既有开关；Plex 可能需要刷新整部作品，客户端 Extras 显示能力并不一致。本地未实际连接 NAS/Jellyfin/Plex/Emby，测试为离线 TMDB 样本与临时文件验证。

官方规则依据：
- [TMDB 动漫与 OVA 分类](https://www.themoviedb.org/bible/tv/59f743289251416e71000037)：附属 OVA、独立 OVA 剧集和单集电影需分别处理。
- [TMDB 特别篇](https://www.themoviedb.org/bible/tv/59f73eb49251416e71000026)：特别季为 Season 0，但正式分配了普通季集号的内容仍保留原编号。
- [Jellyfin 剧集与 Extras](https://jellyfin.org/docs/general/server/media/shows/)：目录与特别篇编号规则。
- [Plex 剧集 Extras](https://support.plex.tv/articles/local-files-for-tv-show-trailers-and-extras/) 与 [编号顺序](https://support.plex.tv/articles/naming-and-organizing-your-tv-show-files/)。
- [Emby TV Naming](https://emby.media/support/articles/TV-Naming.html)：Extras 与 Specials 的组织方式。

# 剧集名称与季集识别

文件名中明确标注 `[NCOP]`、`[NCOP&ED]`、`[ED01]`、`[PV01]`、`[SP01]` 等附加内容时，识别和整理会按**当前文件名**跳过，源文件保留，原因写入日志。转移返回失败及跳过原因，防止下载器在移动模式据此删源；上级目录的合集标记不会使正片跳过。

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

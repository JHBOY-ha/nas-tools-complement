# 单进程 NAS 稳定性与任务预算

日期：2026-10-06。适用当前单主进程、多线程部署；危险 I/O 使用独立、复用的子进程。
数据库暂停已由用户明确实施要求取代。本轮新增 WAL 安全检查、两库 FIFO 写事务、
原子版本发布、索引/统计及安全备份恢复；保留既有 Session 清理、读池和压力告警。
完整规则见 [数据库治理说明](database-governance.md)。

## 默认预算

| 工作 | 执行上限 | 等待上限 | 行为 |
| --- | ---: | ---: | --- |
| 通用后台操作 | 6 | 64 | 满时明确拒绝，重复请求/意图合并 |
| Webhook/播放检查 | 2 | 16 | 与批任务分池；配对回调原子准入 |
| Telegram 轮询 | 1 | 4 | 独立容量，旧停止 event 结束外层循环 |
| 全部站点搜索 | 4 | 64 | 共享池，超时只取消本调用未启动任务 |
| 主服务/RSS/刷流/删种 | 共用 6 | 共用 64 | 保留迟到、防重入与合并；只关闭自身任务 |
| 文件转移 | 2 | FIFO | 同目标互斥，嵌套受保护发布复用槽 |
| 元数据/暂存 I/O 子进程 | 2 | 16 | 常规操作 10 秒，目录清理 60 秒 |
| 发布文件锁子进程 | 最多 8，受转移闸门约束 | 16 | 按需创建；同目标先在线程锁等待，不占元数据池 |
| 哈希/比较/转移子进程 | 2 | 16 | 哈希/比较 300 秒；转移沿用 external_transfer_timeout |
| 网络子进程 | 4 | 16 | DNS 最多 5 秒，整次在线操作共享 90 秒 |
| 上传接收租约 | 2 | 不排队 | 接收前预占队列槽/空间，满时立即繁忙 |
| 两库写事务 | 合计 1 | 64（含预留） | FIFO、30 秒准入等待；每库一条独立写连接，读池并发 |
| 审计持久化 | 最多 1000 行/事务 | 复用写入 FIFO | 每批重新排队；building 不可见，最终原子发布 |

这不是所有 WSGI/原生线程的统一线程数。取消 Future 不释放运行中 I/O 的真实槽。
无法立即回收的进程及其进程组继续计费，防止无限补开。D 状态内核调用可能仍要等待
文件系统恢复，隔离不能保证进程立即消失。

## 配置与空间

```yaml
app:
  workload:
    background_workers: 6
    background_queue_size: 64
    interactive_workers: 2
    interactive_queue_size: 16
    scheduler_workers: 6
    scheduler_queue_size: 64
    search_workers: 4
    search_queue_size: 64
    transfer_concurrency: 2
```

缺失字段使用默认值；非法计数提示并回退，不自动改写生产配置。预算修改后重启生效。
原 rss_workers 不再控制独立池，定时服务统一使用 scheduler_workers/queue_size。
已从默认值和校验表移除失效的 rss_workers；旧配置中的该字段不再产生作用。
繁忙机械盘可将 transfer_concurrency 设为 1；队列容量不是吞吐量。

### 运行边界与诊断

- 定时任务共享容量、搜索超时后运行中的请求继续占槽，均为有意的资源约束。
  持续繁忙时应先检查慢站点、NAS I/O 和任务排队，再调整上面的 worker 配置。
- 本地转移与外部命令均受 `app.external_transfer_timeout` 限制，默认 3600 秒。
  若正常的大文件复制需要更久，按实测耗时提高该值；超时结果须先检查源、目标和
  暂存证据，不自动重放移动或删除。父进程总时限与命令内部时限分别保留。
- 暂存单目录枚举上限为 4096 项，未登记目录的递归枚举上限为 10000 项；达到上限
  会拒绝上传准入。应检查历史清理失败和残留文件；不得直接删除活动任务目录或归属标记。
  `.cleanup-markers` 实际占用的磁盘空间也计入额度，不能在计费中忽略。
- 子进程遇到不可中断 I/O 时，容量与文件锁可能延迟释放；不绕过锁启动另一轮发布。

会话清理会同时覆盖注入的任务数据库、user.db 与 media.db；其中一个全局清理失败时
仍尝试另一个。隔离 HTTP 保留 `verify` 的布尔值或私有 CA 路径；暂存流仍禁止跟随
软链接，追加流在每次实际写入时通过 `O_APPEND` 选择文件末尾。

数据库默认 `journal_mode=auto`、`synchronous=FULL`、自动 checkpoint 1000 页、
WAL 警戒/高水位 64/256 MiB、保留空闲 2048 MiB。旧运行时 DELETE 可安全回退；
不安全环境中的既有 WAL 或显式 WAL 拒绝启动。写前另检查估算预算，高水位受
长读阻挡时拒绝新写，成功回收后恢复；SQL/fsync 不因调用方超时而释放槽。
备份/完整性检查使用已有 bulk 子进程池，不增加无限独立池。恢复只暂存，返回
`restart_required=true`，重启关闭业务连接后离线替换，并保留校验过的旧库副本。

字幕策略默认暂存 **1536 MiB**、保留空闲 **2048 MiB**，仍满足 250 MiB 批次六倍额度。
管理员已保存的显式值保留，既有任务策略快照不自动改写。接收前按 Content-Length
或未知长度批次上限预占六倍空间，解析/暂存过程中检查真实字节；已解码内部调用按
已知处理模式预占。活跃请求路径不重复计费，残留 HTTP 文件和无记录的失败目录继续计费。
读取失败不能当作空间已释放；这些额度只限制本应用，其他写入者仍须配合卷监控。

## 异步 API 与前端

rename、rename_udf、re_identification、run_directory_sync、run_userrss、run_brushtask、
auto_remove_torrents、start_mediasync、sch、download_subtitle 以及 special_confirmation
的确认阶段返回 **202**，包含 async、operation_type=background_action、task_id/task/status_url。
这是接收状态，终态才是执行结果；非 HTTP 内部调用保留原同步行为。

请求支持 Idempotency-Key 或 X-Request-ID。同一编号用于不同参数会拒绝；同一用户
正在执行的相同意图合并。执行前复核最新用户/原权限或接口认证。浏览器共享
ActionTaskClient 等待真实结果后调用原回调，页面切换忽略失效回调。特殊集复核门槛、
文件所有权与发布/删除校验保留。断网时只恢复读取状态，不自动重放操作。

| 查询方式 | 用途 |
| --- | --- |
| /do：get_action_task/get_action_tasks/find_action_task | 单任务、最近 20 条、接收响应丢失后的核对 |
| /do：cancel_action_task | 仅取消尚未启动的排队工作 |
| GET /api/v1/service/task/任务编号 | 认证查询，复核归属及原权限 |
| DELETE /api/v1/service/task/任务编号 | 同样只取消排队 |
| GET /api/v1/service/tasks | 授权可见的最近操作 |

私有记录位于 /config/temp/action-tasks/UUID.json，最多 100 条、7 天，每项结果 128 KiB，
权限 0600。不保存参数、Cookie、认证头或 API 密钥。接收/开始/终态原子写入文件，
状态读取使用内存快照。重启把未完成记录标为中断，不自动重放删除/移动。
结果过大明确标记明细截断；持久化失败明确拒绝准入或标明未持久保存。

## 上传与网络总时限

multipart 不持共享提交锁，每用户提交互斥保留去重；结束、解析失败和断连释放租约。
真实 Werkzeug 连接由定时 shutdown(SHUT_RD) 限制 120 秒总时长，滴流也会结束并返回 408。
预缓冲内存/普通文件输入可解析；不暴露可中断连接的实时 WSGI 服务明确拒绝。
更换服务器时需验证这一集成，不能静默关闭时限。

在线字幕的解析、HTTP、跳转、重试、登录和下载共享 90 秒预算与字节上限。子进程的
阻塞读取也受父进程总时限控制。共享 OpenSubtitles 锁只保护 claim/发布代次，
令牌刷新合并，配额消费单独准入；同媒体重复下载等待已有结果，不重复扣配额。

## 验证与部署边界

2026-10-06 本轮本地结果：完整稳定性 **281 项**、数据库专项 **51 项**、识别 **341 项**、
在线字幕/OpenSubtitles **64 项**通过；三个 Chrome 回归入口通过。数据库专项使用
临时 SQLite 3.51.3；默认 3.37.2 的显式 DELETE 回退另有 **41 项**通过。严格 UI
静态审查无发现，Python/JS 语法与 diff 检查通过，具体证据见
[交付说明](stability-hardening-delivery.md)。

```bash
python3 -m tests.run_stability
python3 -m tests.run_database
python3 -m tests.run_recognition
python3 -m tests.run_stability tests.test_online_subtitles tests.test_opensubtitles_api
node tests/test_action_tasks_ui.js
node tests/test_special_confirmation_ui.js
node tests/test_database_restore_ui.js
python3 scripts/audit_action_ui.py
python3 scripts/verify_nas_workload.py
python3 scripts/verify_database_workload.py
```

浏览器测试需要 Playwright 与 Chrome，支持 NODE_PATH/CHROME_PATH。静态审查覆盖新增共享
操作组件，调用点另有运行时回归。测试阻止外部网络；子进程只允许数字 loopback 测试服务。

本地可丢弃 smoke 复制并核对 8×4 MiB，实际峰值并行 2、I/O 子进程 2。这是本机临时目录
测量，不是 NAS 吞吐量。脚本支持人工选择 --scratch-root，只创建/清理唯一临时子目录，
不读取应用配置或数据库。

数据库脚本使用基线 commit `8d84b13` 与当前实现、5 万条状态、10 读/10 普通写，
默认三轮；支持同卷 `--scratch-root`，不读取生产库。本机 P95 门槛已通过，但分批
FULL 的审计结果提交耗时、次数及版本空间增加；不承诺总 I/O 减少。NAS 需补测
磁盘延迟、正常高峰及完整审计，并按相同门槛复验后才确认稳定性收益。

本轮未部署、重启生产服务或写生产数据。真实 NAS/btrfs 卷与外部字幕服务尚未联调，
不据本地回归宣称真实 NAS 压力或性能改善已测量。

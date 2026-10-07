# NAS-Tools 稳定性治理交付说明

更新日期：2026-10-07。范围：B0–B4，以及本轮授权实施的 WAL、全局写事务协调、
版本化审计、索引/统计、数据库安全备份恢复及迁移保护。

2026-10-07 审查补充：修复默认空间余量、无迁移重复摘要、媒体同步部分提交、
失败重启重复备份、待恢复取消/备份管理入口及独立 `update_db()` 初始化。完整
稳定性 303 项、数据库 WAL 73 项、旧 SQLite 回退 63 项通过；新增修复回归 20 项。
具体行为及离线命令见 [数据库治理](database-governance.md)，逐项依据见
[DeepSeek 核实与修复](deepseek-audit-reproduction.md)。下方原验收数字保留为历史记录。

代码依据：[8d84b13 — 限制 NAS 后台负载并隔离阻塞 I/O](https://github.com/JHBOY-ha/nas-tools-complement/commit/8d84b135317645451110409b82916f62b3ea059b)，
父提交为 `1a4a8c8`（字幕查询、缓存与文件 I/O 性能优化）。该稳定性提交共修改
**70 个文件，新增 6025 行、删除 624 行**；这些统计只对应基线提交。本轮数据库
实现及文档在当前工作树中，尚未提交/推送，不计入该提交统计。

**状态：B0–B3 和 B4 非数据库改造已提交；后续数据库代码、迁移、测试和文档已完成
本地实现与验收，生产运行时升级及 NAS 验收仍由部署方完成。** 没有部署、重启生产
服务或修改生产数据，不能据本机结果宣称真实 NAS 稳定性已提升。

迁移测试与数据库 workload 基准使用仓库内的 `scripts/baselines/8d84b13/` 快照及
SHA-256 清单，不再依赖本地 Git 历史对象；源码包、浅克隆和 squash merge 后仍可
执行相关验收。

> 此前的数据库暂停已被本轮明确实施要求取代。WAL 默认 auto，通过实际运行时、
> 卷和并发探针才启用；本机默认 SQLite 3.37.2 不满足 WAL 条件。保留 DELETE 的
> 降级运行不等于完成 WAL 验收。具体启动、资源与回退规则见
> [数据库治理说明](database-governance.md)。

## 一、交付结果与适用边界

适用于单主进程、多线程部署。SQLite 仍由主进程内的工作线程使用；危险文件和网络
I/O 交给有上限、可复用的子进程，应用不再只由一个操作系统进程构成。

- 通用后台、交互回调、定时任务和站点搜索使用固定 worker 与有界等待队列，繁忙时
  明确拒绝准入或合并重复触发，避免每次请求新建一批线程。
- 字幕上传队列默认上限 10；接收前另预占队列槽和空间，并限制最多 2 个接收租约。
  这些限制不等于全应用线程总量限制；WSGI 请求线程仍取决于实际服务器配置。
- 不同文件可以并行转移，但默认最多 2 个转移槽，同目标仍互斥。转移闸门按 FIFO
  等待，本身没有独立的等待人数上限；后台入口的有界队列另行控制准入。
- 长 Web 操作由任务状态查询给出最终结果，HTTP 202 只表示已接收。上传滴流、在线
  字幕 DNS/HTTP 以及主要危险 NAS I/O 均增加总时限或进程隔离。
- 两库共用一个 FIFO 写入准入，读连接独立；审计分批写不可见版本，以最后一个短
  事务原子发布状态、媒体快照和任务终态。FULL 持久性、Session 清理及池告警保留。
- 已验证资源预算、故障一致性和本机并发收益。尚无真实 NAS 吞吐量、磁盘
  延迟或内存下降幅度的数据，不能据此承诺具体性能提升百分比。

此前“应用不可能引发 btrfs 报错”的表述不足以排除实际故障链路。本轮未取得真实
NAS 的内核日志、磁盘及文件系统检查结果，**不能确定 btrfs 报错的根因**。减少并发
I/O 与空间压力不能替代底层排查，参考 [btrfs 排查清单](btrfs-metadata-troubleshooting.md)。

## 二、已完成的 B0–B3

### B0 — 锁等待、索引、缓存及阻塞边界

| 问题 | 当前实现 | 主要位置 |
| --- | --- | --- |
| SQLite 默认 5 秒锁等待，短暂竞争易失败 | `user.db`、`media.db` 连接等待设为 30 秒；签出连接达到池容量 80% 时告警，每个池每 60 秒最多一条 | `app/db/main_db.py`、`app/db/media_db.py` |
| 字幕任务及历史查询缺少部分索引候选 | 主库模型中的全部显式索引是唯一运行时定义；启动阶段独立校验并补建缺失/漂移索引，不把索引缺失升级为表结构迁移 | `app/db/models.py`、`app/db/__init__.py` |
| 刷流去重缓存只增不减，列表查找开销随记录增加 | 上限 5000 的 `OrderedDict` LRU，使用字典成员判断 | `app/brushtask.py` |
| Telegram 发图请求未设置超时 | 设置连接/读取超时 `(5, 30)` | `app/message/client/telegram.py` |
| 站点分页没有页数或耗时边界 | 3 处循环增加 100 页/300 秒边界；不宣称能即时打断正在执行的单页请求 | `app/sites/siteuserinfo/_base.py` |
| 上传读取可长期占据入口 | 当前已升级为接收租约及 120 秒读取总时限，详见 B4 | `web/main.py`、`app/helper/subtitle_tasks.py` |

索引清单：

| 表 | 新增索引列 |
| --- | --- |
| `SUBTITLE_TASK` | `CREATED_AT`；`FINISHED_AT`；`TYPE, STATUS, PRIORITY, CREATED_AT` |
| `SUBTITLE_PROBE_CACHE` | `PAIR_PATH` |
| `SUBTITLE_AUDIT_STATE` | `SERVER, UPDATED_AT`；`SERVER, SUBTITLE_PATH, UPDATED_AT`；`SUBTITLE_PATH` |
| `TRANSFER_HISTORY` | `DATE` |

原 B0 索引迁移只新增索引，不删除业务记录或修改业务字段。已有 `(SERVER, PATH)`
唯一索引保留；本轮另加单列 `PATH`、两个部分领取索引及版本存储索引，详见后文。
启动时如果上述普通索引缺失或定义漂移，只在受控维护事务中按模型定义补建/拒绝启动，
不触发整库备份、表重建或业务摘要；列顺序、排序方向、唯一性及部分索引谓词不一致
会停止启动。
回归确认原索引存在，但**不能据此认为六个
索引都已证明必要，或上线后所有相关扫描和排序都会消失**。早期“四处热点命中
索引”的记录没有充分说明统计信息条件，收益判断以下面的复核为准。

**历史索引评估（本轮实施前，基线 `8d84b13`）**：使用当时模型创建可丢弃的临时库，
SQLite 版本为 3.37.2。探测缓存和任务表各 5 万行；缓存含两个 SERVER 值，任务含
10 条 queued、10 条 recovering，其余为终态。以生产查询相同的过滤和排序生成 SQL，
比较临时库执行 `ANALYZE` 前后的 `EXPLAIN QUERY PLAN`，未访问生产数据库。

| 查询 | 未运行 ANALYZE | 临时库运行 ANALYZE 后 |
| --- | --- | --- |
| `invalidate_probe_cache`：`PATH IN (…) OR PAIR_PATH IN (…)` | `SCAN SUBTITLE_PROBE_CACHE` | `MULTI-INDEX OR`；PATH 侧使用 `(SERVER, PATH)` 的 `ANY(SERVER)` skip-scan，PAIR_PATH 侧使用新增索引 |
| `_claim_next_interactive`：多个 TYPE/STATUS，按 PRIORITY 降序、CREATED_AT/ID 升序 | 队列索引过滤，另有 `USE TEMP B-TREE FOR ORDER BY` | 同样需要额外排序 |

1. 当时缓存失效查询没有 SERVER 过滤，PATH 侧利用复合索引需要 skip-scan。在基线的
   数据库初始化、迁移及相关代码中，未发现自动 `ANALYZE`/`PRAGMA optimize` 的
   统计信息维护逻辑，不能保证仅新增 PAIR_PATH 索引就消除全表扫描。生产库可能
   已有统计信息，其实际计划还受 SQLite 版本和数据分布影响。
   [SQLite skip-scan 说明](https://www.sqlite.org/optoverview.html#the_skip_scan_optimization)
2. 队列索引可以缩小过滤范围，但本次计划仍需排序。默认上传等待队列仅 10 项，
   不代表历史总表或修复队列只有 10 项；在缺少实际耗时证据时，继续调整该索引
   的优先级较低。索引命中也不等于查询无需排序或已证明真实 NAS 提速。

这些结果不支持整体撤回六个索引。本轮据此新增直接 PATH 索引、精确排序的部分
索引和统计维护，并核对实际领取 SQL 的计划；仍不把临时库计划等同于 NAS 耗时。

### B1 — 转移互斥、搜索及调度行为

| 问题 | 当前实现 | 主要位置 |
| --- | --- | --- |
| rclone/mc 或本地转移长期阻塞 | 本地转移和外部命令均使用隔离进程；沿用 `app.external_transfer_timeout`，默认 3600 秒 | `app/utils/system_utils.py`、`app/utils/isolated_worker.py` |
| 全局转移锁使不同目标互相阻塞 | 改为按目标路径互斥，并叠加默认 2 槽公平闸门；嵌套受保护发布复用槽 | `app/filetransfer.py`、`app/utils/workload.py` |
| 每次检索新建线程池，慢站点拖住调用 | 当前使用共享有界搜索池，整体收集时限 120 秒；单站失败不丢弃其他结果 | `app/indexer/indexer.py` |
| 全量同步与定时任务并发触发 | `transfer_all_sync` 非阻塞互斥，重复触发不重复执行 | `app/sync.py` |
| 字幕启动与上传提交的 ABBA 锁顺序 | 统一 `_submit_lock -> _lock`；multipart 读取已移出共享提交锁 | `app/helper/subtitle_tasks.py` |
| 定时任务稍有延迟就被丢弃 | `misfire_grace_time=60`、`coalesce=True`、`max_instances=1`；B4 统一定时任务准入 | `config.py`、5 个调度器 |

搜索超时只取消本调用尚未启动的任务，不取消其他调用的任务。正在执行的站点
请求继续占共享槽，超时不会腾出虚假的容量，也不会再新建线程池补开。
调度延迟超过 60 秒或准入队列已满仍可能无法执行，不保证所有触发都能补跑。

### B2 — Session 生命周期与连接池

旧代码的只读查询可能让存活后台线程长期保留已签出的连接。旧调度/RSS/通用池的
线程预算合计可超过连接池容量，空闲线程也能造成 `QueuePool limit reached`。
当前在工作单元结束时清理 Session，包含异常路径：

- Flask `teardown_appcontext`；
- `ThreadHelper.start_thread/start_threads` 的每个任务；
- indexer 搜索提交及 5 个调度器注册的任务；
- 字幕常驻 worker 的每轮迭代。

`app/db/session_scope.py` 同时清理注入的任务数据库、`user.db` 与 `media.db`；注入库
的清理不会替代两个全局库的清理，一个全局清理失败时仍尝试另一个。

两个数据库读引擎分别采用 `pool_size=20`、`max_overflow=30`、`pool_timeout=30`、
`pool_use_lifo=True`，**每个池的峰值容量仍为 50，不是两个数据库合计 50**。
最多保留 20 条空闲连接，溢出连接归还后关闭；LIFO 优先复用最近归还的连接，
不表示所有空闲连接会主动定时回收。`media.db` 增加 `expire_on_commit=False`，
与 `user.db` 对齐。本轮每库另保留一条独立写连接，两库共用一个活动写事务预算。

旧 `pool_size=50` 也不是启动时就创建 50 条连接：QueuePool 按需建立连接。因此，
20+30 是常驻/溢出容量的取舍，不是连接占用问题的核心修复；没有证据证明 20 是
当前部署的最佳值。[SQLAlchemy QueuePool 说明](https://docs.sqlalchemy.org/en/14/core/pooling.html#sqlalchemy.pool.QueuePool)

本地回归确认：未清理的只读 Session 会占用池连接；工作单元结束后，即使 worker
线程仍存活，连接也会归还。该结论是**连接池占用**，不能等同于“所有只读查询
都会留下永久 SQLite 读锁”。默认 pysqlite 的实际事务和游标状态需要另外区分，
参考 [SQLAlchemy 1.4 SQLite 事务说明](https://docs.sqlalchemy.org/en/14/dialects/sqlite.html#transaction-isolation-level-autocommit)。

**基础数据库治理的必要性判断**：此前评估认为 Session 清理必要性最明确，其余配置
和索引应分别判断。本轮按用户明确方案继续实施；下面基础取舍仍保留，新增方案
已有本机故障和性能证据，但实际 NAS 是否获益仍待验收。

| 项目 | 必要性与证据 | 当前建议 |
| --- | --- | --- |
| 工作单元结束清理 Session | 高；本地回归确认存活线程的连接占用及清理后的归还 | 保留，优先保障异常路径及跨任务对象使用正确 |
| 锁等待 5→30 秒 | 中；容忍短暂竞争，但会延长阻塞 | 保留既有值，30 秒是否合适需实际等待时长支持 |
| 新增 6 个索引 | 有针对性，但各索引收益不同，部分依赖统计信息 | 不整体撤回，按实际 SQL、表规模和计划逐项评估 |
| 连接池 50+0→20+30 | 低至中；属于资源参数调整 | 可保留，先观察实际签出峰值及归还情况，不继续扩大容量 |
| `expire_on_commit=False` | 对象生命周期取舍，不能替代连接归还或刷新 | 与 Session 清理配套验证，不能据此认定数据一直最新 |
| 连接池压力告警 | 有观测价值，但不覆盖所有慢 SQL/锁竞争 | 保留；没有告警不能证明数据库没有瓶颈 |

### B3 — 把 NAS I/O 移出字幕管理器全局锁

采用“锁内快照纯数据 → 锁外做 I/O → 锁内重读复核并提交”。改造覆盖输出校验、
上传暂存检查、发布结果对账、容量检查、取消、重启恢复及交互任务领取等路径；
哈希、元数据及清理随后进一步接入 B4 的隔离进程。

取消先持久化意图，锁外核对输出，再重读并写终态。排队/恢复中的上传在对账前
将状态设为 `canceling`、阶段设为 `cancel_reconciling`，避免 worker 抢先领取并删除归属证据。
对账失败保留取消意图和证据，再次取消可以重试；已完成发布的字幕予以保留。

文件发布和删除继续要求 ownership marker、inode/内容证据及相应身份复核；审计
结果与任务终态保留原子提交。探针验证的“锁内无 NAS I/O”只适用于回归覆盖的
方法与调用路径，不推及全部仓库调用。

## 三、B4 — 资源预算与剩余非数据库改造

### 默认资源预算

| 工作 | 执行上限 | 等待上限 | 准入与回收行为 |
| --- | ---: | ---: | --- |
| 通用后台操作 | 6 | 64 | 满时拒绝，重复请求/意图合并 |
| Webhook/播放检查 | 2 | 16 | 独立交互池，相关回调原子准入 |
| Telegram 轮询 | 1 | 4 | 独立服务池，配置切换使旧循环退出 |
| 全部站点搜索 | 4 | 64 | 共享池，运行中请求继续占槽 |
| 主服务/RSS/刷流/删种 | 共用 6 | 共用 64 | 单个调度器只关闭或等待自己的工作 |
| 文件转移 | 2 | FIFO 等待 | 同目标互斥，嵌套发布复用槽；无独立等待人数上限 |
| 元数据/暂存 I/O 子进程 | 2 | 16 | 常规操作 10 秒，目录清理 60 秒 |
| 哈希/比较/转移子进程 | 2 | 16 | 哈希/比较 300 秒，转移默认 3600 秒 |
| 网络子进程 | 4 | 16 | DNS 最多 5 秒，在线操作共用 90 秒预算 |
| 上传接收租约 | 2 | 不排队 | 先预占队列槽及空间，繁忙立即拒绝 |

后台队列取消会移除尚未执行的任务参数，不让取消记录继续堆积在底层线程池。
子进程超时后父进程停止等待；未回收的进程或进程组继续占用进程预算，禁止无限
补开。不可中断的内核 I/O 可能使被终止进程延迟退出，隔离不保证它立刻消失。

### 长操作异步 API 与前端配合

以下 HTTP 操作返回 202：`rename`、`rename_udf`、`re_identification`、
`run_directory_sync`、`run_userrss`、`run_brushtask`、`auto_remove_torrents`、
`start_mediasync`、`sch`、`download_subtitle`，以及 `special_confirmation` 的确认阶段。
响应包含 `async`、`operation_type=background_action`、`task_id/task/status_url`。
非 HTTP 内部调用继续沿用原同步行为。

- 共享 `ActionTaskClient` 查询真实终态后执行原回调；媒体同步进度与终态分别展示。
  页面切换后忽略失效回调，断网只恢复状态读取，不自动重放移动或删除。
- 支持 `Idempotency-Key`/`X-Request-ID`，同一编号不同参数拒绝，同用户正在执行的
  相同意图合并。执行前重查用户/原操作权限或接口密钥，状态查询也校验归属和权限。
- `/do` 提供 `get_action_task/get_action_tasks/find_action_task/cancel_action_task`；
  `/api/v1/service/task/<task_id>` 提供 GET 查询、DELETE 取消排队，
  `/api/v1/service/tasks` 查询授权可见的最近操作。
- **仅能取消尚未开始的后台操作**，运行中操作不通过 Future 强杀。字幕任务的
  安全步骤取消机制仍由原任务管理器负责。
- 私有记录位于配置暂存目录的 `action-tasks/UUID.json`（通常是
  `/config/temp/action-tasks/`），最多 100 条、保留 7 天、每项结果上限 128 KiB、
  文件权限 0600。不保存参数、Cookie、认证头或 API 密钥，不新增 SQL 表。
  持久准入失败不返回已接收；终态保存失败会标明未持久保存。重启将未完成记录
  标为 `interrupted`，不自动重新执行。

### 上传准入、暂存额度与读取总时限

接收前按 Content-Length 或未知长度批次上限预占队列槽及六倍空间，暂存过程中
继续检查真实字节。multipart 不持共享提交锁，每用户提交互斥及去重仍保留，
结束、解析失败、断连时释放接收租约。

默认暂存额度从 2048 MiB 收紧到 **1536 MiB**，保留空闲从 1024 MiB 提高到
**2048 MiB**，仍满足默认 250 MiB 批次的六倍额度。已保存的管理员显式值和既有
任务策略快照保留。活跃请求路径不重复计费；残留 HTTP 文件、无记录的失败目录
及 `.cleanup-markers` 的真实占用继续计费，读取失败不视为空间已释放。

真实 Werkzeug 连接用 120 秒定时 `shutdown(SHUT_RD)` 限制 multipart 读取总时长，
滴流也会超时并映射为 408。恢复 socket 时同步关闭定时器有效状态，防止已经启动
的迟到回调误断开后续请求。预缓冲内存/普通文件输入可解析；实时 WSGI 输入不提供
可中断连接时**明确拒绝**，不会静默取消时限。更换服务器需验证这一集成。

### 在线字幕与危险 I/O 隔离

DNS、HTTP、登录、跳转、重试和下载共用 90 秒在线操作预算。网络读取在子进程内
执行，父进程施加总时限，并保留响应字节上限。隔离 HTTP 保留 `verify=True/False`
或私有 CA 路径，不丢失原 TLS 校验参数。此治理范围不是全应用外部请求。

OpenSubtitles 共享锁只保护 claim/发布代次，令牌刷新合并、配额消费另行准入；
同媒体重复下载等待已有结果，避免重复消费配额。特殊集确认门槛、字幕发布及删除
的身份/内容校验保留。

暂存流、哈希、容量/元数据、清理、转移和受保护发布使用复用子进程。普通媒体哈希
保留软链接媒体读取语义；受保护暂存流禁止跟随软链接。追加流每次实际写入均使用
`O_APPEND` 并回传实际偏移，避免并发追加互相覆盖。外部转移超时结果可能不确定，
不自动重放移动或删除，需要核对源、目标及暂存证据。

### 本轮数据库实施

| 项目 | 当前实现与验收边界 |
| --- | --- |
| WAL | 配置目录单实例锁、实际挂载类型与同卷两进程 SHM/保留快照探针；默认 auto，旧 DELETE 检查失败保持原模式，显式 WAL 或不安全的既有 WAL 拒绝启动 |
| 持久性 | 每条连接设置并核验 FULL、外键、30 秒锁等待及自动 checkpoint；不通过降低同步级别换性能 |
| 全局写入 | 两库共用 FIFO，等待容量 64（含文件预留），准入等待 30 秒；每库一个写连接，业务重查、autoflush、DML、提交/回滚在同一范围 |
| 审计拆分 | 去重/序列化在事务外；最多 1000 行/事务，批后重新排队；building 不可见，最终短事务同时发布状态、快照、替换屏障和任务终态 |
| 读取与清理 | 单调发布序号校验缓存，多查询短读快照；最新版本后过滤删除标记；废弃版本小事务清理，不自动 VACUUM、不重放未完成发布 |
| 索引统计 | 原六索引保留；单列 PATH、精确顺序的 interactive/audit 部分索引及版本关联索引；建索引后及每天 optimize/受限 ANALYZE |
| 空间与 checkpoint | 空闲保留 2 GiB，写前增加估算预算；WAL 警戒 64 MiB/高水位 256 MiB，高水位受长读阻挡时拒绝新写，成功回收后恢复；不删除 WAL/SHM |
| 安全备份恢复 | 两库 Online Backup、schema/发布元数据/哈希清单、私有目录；拒绝恶意 ZIP，管理员恢复只暂存并返回 restart_required；重启离线替换、持久进度与旧库副本 |
| 迁移与检查 | 迁移前备份、全部既有业务表摘要核对、约束定义和 integrity/FK 检查；异常或进程退出后不开放半升级服务；SQLite 恢复 hot journal 后再校验 |
| 文件衔接 | 先预留记录准入；排他发布使用真实历史确认，MOVE 提交失败保留源/归属证据，拒绝修改外部替换的目标，未确认结果不虚报成功 |

受支持 SQLite 为 `3.51.3+`，以及修复后的 3.44.6+（3.44 分支）、3.50.7+（3.50 分支）；
本机默认 3.37.2 不满足条件。本轮用临时 SQLite 3.51.3 驱动完成 WAL 验收，没有
替换生产运行时。[官方 WAL-reset 修复说明](https://www.sqlite.org/wal.html#the_wal_reset_bug)

锁顺序为提交锁（需要时）→ 写入准入 → 管理器锁；文件和网络 I/O 放在业务事务外。
同库嵌套仅最外层提交，内部失败令整事务回滚；跨库不宣称原子性。SQL/fsync 仍在
主线程池内执行，调用方超时不释放运行中的写槽；备份/完整性检查在有界 I/O 子进程。

用户已说明配置在 NAS 本机 btrfs/ext4，但本轮没有探测生产卷。WAL 和分批 FULL
都不能证明总 I/O 减少；新增版本及索引会增加空间，分批增加同步次数。真实 NAS
的三轮对比、磁盘延迟和上线观察仍未完成。

配置、恢复文件、验收及回退步骤见 [数据库治理说明](database-governance.md)，非数据库
预算见 [NAS 任务预算](nas-workload-budget.md)。

## 四、对现有服务的影响与部署注意事项

| 变化 | 预期收益 | 需要关注的影响 |
| --- | --- | --- |
| 工作单元结束清理 Session | 防止空闲线程耗尽连接池 | 未提交修改会回滚；脱离 Session 的对象不能继续惰性加载，跨任务对象需明确重新查询 |
| 锁等待 5→30 秒 | 降低短暂锁竞争失败 | 操作可能等更久；不保证每次竞争都等待满 30 秒，也不消除锁竞争 |
| 每池保留 20、峰值 50 | 控制高峰后的常驻连接上限 | 旧池也按需建立连接；收益取决于实际签出峰值，20 未证明是最佳值 |
| `media.db` 提交后属性不过期 | 减少重新读取及清理后属性访问问题 | 长期保留对象的数据不会自动刷新，需要重新查询 |
| 启动自动建索引 | 为部分热点提供索引候选 | 扫描/排序是否减少取决于 SQL、统计信息和分布；首次建索引增加启动时间、空间及写入维护成本 |
| WAL/FULL 与 FIFO | 保留读写并发，减少应用写者争抢，异常时不误确认 | 运行时/卷检查不通过会降级 DELETE 或拒绝启动；排队及长 fsync 仍可能阻塞 |
| 分批隐形审计版本 | 缩短连续独占写槽的时间，保持整批发布 | 版本/索引占用更大；FULL 下多次提交，完整审计总耗时不预先承诺改善 |
| 恢复只暂存、重启生效 | 避免活动连接持有旧 inode 或读到半恢复状态 | 旧调用方需识别 restart_required；重启和回退须保留有效副本 |
| 有界队列与转移并发上限 | 控制同时施加到 NAS 的工作量 | 高峰可能排队或收到繁忙响应，队列容量不代表吞吐量 |
| 长操作返回 202 | HTTP 不等待整个操作完成 | 前端及外部 API 调用方必须查询终态，不能把 202 当成功结果 |
| 上传/在线字幕/转移总时限 | 限制慢客户端及慢服务占用 | 超过合法大文件操作耗时也会停止等待，需按实测调整可配置转移时限 |
| 文件/网络子进程 | 阻塞调用不再占据主工作线程无限等待 | 增加有限 IPC/进程成本；不可中断 I/O 仍可能耗尽该类隔离容量 |

1. **整体部署包含新的 schema。** `init_db()` 在业务可读前完成实例/卷/运行时检查、
   结构迁移所需备份和受控 Alembic 升级，目标版本为 `7e1c9a42b605`；缺失普通查询
   索引走独立补建事务，不创建迁移备份。旧 `run.py` 的随后 `update_db()` 幂等返回。
   部署方先核验实际 Python SQLite，再在同卷可丢弃库验收。
2. **升级前保留可恢复备份。** 首次备份、表重建、索引、统计及完整性检查增加启动
   时间和空间。schema 回退要物化当前可见数据或恢复升级前副本，WAL 回退要关连接
   并 checkpoint；不能只回退代码，不能删除恢复文件来绕过限制。
3. **预算配置重启生效。** `app.workload` 缺失字段取默认值，非法计数提示后回退；
   预算读取不自动改写生产配置。旧 `rss_workers` 已失效，RSS 等定时服务改用
   `scheduler_workers/scheduler_queue_size`。不能热切换闸门来绕过正在执行的工作。
4. **按实际负载取值。** 默认转移并发 2；繁忙机械盘可先使用 1，并观察排队和实际
   I/O 延迟。`app.external_transfer_timeout` 默认 3600 秒，长转移应按实测调整。
5. **一并交付前后端。** 新客户端等待终态，旧接口调用方需适配异步响应。任务
   状态文件需要配置暂存目录可写；重启中断结果必须先核对再决定是否重试。
6. **确认 WSGI 上传支持。** 实时上传需暴露受支持的可中断连接，否则当前入口会
   拒绝。容量读取失败、残留暂存及枚举上限导致拒绝时，应核对历史清理状态，
   不直接删除活动任务目录或归属标记。

## 五、验证方法与结果

以下为 2026-10-06 基线加本轮工作树的验证。完整 WAL 入口与主要回归使用临时
SQLite 3.51.3；旧 3.37.2 单独验证 DELETE 回退。不同入口有重叠，不相加为独立
用例总量。这里没有将未执行的 NAS 或断电验收算作通过。

| 验证入口 | 本次结果 | 主要证据 |
| --- | --- | --- |
| `python3 -m tests.run_stability` | **281 项通过** | 原稳定性回归及 41 项数据库治理/备份/迁移用例 |
| `python3 -m tests.run_database` | **51 项通过** | 真实两库事务、故障注入、备份/恢复/迁移与 WAL；含进程退出及 checkpoint 中断 |
| `python3 -m tests.run_database --legacy-only`（SQLite 3.37.2） | **41 项通过** | 明确 DELETE 回退；不能替代 WAL 验收 |
| `python3 -m tests.run_recognition` | **341 项通过** | 识别流程及下游转移/下载保护 |
| `python3 -m tests.run_stability tests.test_online_subtitles tests.test_opensubtitles_api` | **64 项通过** | 在线字幕、OpenSubtitles、隔离传输与 TLS 参数 |
| `node tests/test_action_tasks_ui.js` | **通过** | 202/终态、去重、断网恢复、失效页面、安全文本、键盘和窄屏布局 |
| `node tests/test_special_confirmation_ui.js` | **通过** | 候选选择、确认门槛、单次提交、失败重试、安全文本和布局 |
| `node tests/test_database_restore_ui.js` | **通过** | 暂存恢复、重启提示、重复提交、失败/断网、下载、busy 复位及窄屏 |
| `python3 scripts/audit_action_ui.py` | **strict 模式 0 findings** | 新增共享操作组件的限定范围静态检查 |
| 管理员/普通用户/API 认证定向回归 | **3 项通过** | 备份设置路由拒绝普通用户、敏感操作权限与合法管理 API 认证 |
| `python3 scripts/verify_database_workload.py` | **本机三轮通过** | 5 万条审计、10 读/10 普通写；审计 P95 改善、空闲 P95 未退化超 10%，完整性/FK 无异常 |
| 查询计划 | **专项覆盖** | PATH/PAIR_PATH 的 OR 两侧索引、固定部分领取索引且无临时排序 |

本机独立运行的三轮可丢弃库对比，SQLite 3.51.3、DELETE/FULL 基线对 WAL/FULL 新版，
各轮 5 万条审计、10 读/10 普通写，每调用方 20 次操作。下表取三轮指标中位数：

| 指标 | 基线 `8d84b13` | 本轮实现 |
| --- | ---: | ---: |
| 审计窗口读 P95 | 370.487 ms | 17.228 ms |
| 审计窗口普通写 P95 | 362.953 ms | 38.562 ms |
| 无审计读 P95 | 44.376 ms | 7.731 ms |
| 无审计普通写 P95 | 35.91 ms | 22.122 ms |
| 合成审计结果提交耗时（不含扫描识别） | 2.697 s | 3.309 s |
| 审计提交次数 / 窗口总提交次数 | 1 / 201 | 53 / 253 |
| 窗口结束主库 / WAL 大小 | 16.99 / 0 MiB | 21.94 / 4.54 MiB |

全部六轮 `integrity_check=ok`、外键违规 0、运行错误 0；审计读/写 P95 改善，无审计
P95 未超过基线 110%，脚本门槛通过。**提交过程更慢、提交次数及空间更多**，并发
响应改善不能写成“总 I/O 减少”。提交计数不是 fsync 次数，文件大小是窗口结束
采样，不是峰值；没有采集物理磁盘延迟。
完整轮次、环境、范围及源码摘要保存在本地 `docs/validation/database-local-2026-10-06.json`；该目录已被 Git 忽略，不随仓库分发。

运行完整数据库验收需已修复 SQLite。本机验证实际使用：

```bash
PYTHONPATH=/tmp/nas-db-supported-runtime python3 -m tests.run_database
PYTHONPATH=/tmp/nas-db-supported-runtime python3 scripts/verify_database_workload.py
```

临时驱动用官方 SQLite 3.51.3 源构建并核对官方 SHA3；没有修改系统 Python、依赖或
Docker 运行时。新增数据库文件用例分别为治理/备份 **36 项**、迁移 **5 项**、WAL
**10 项**。每个数据库故障用例仍使用真实事务和约束；没有关闭 FULL 或跳过合法失败。

Python runner 在应用导入前创建临时配置及 SQLite，阻止外部联网；网络隔离子进程
只允许测试所需的数字 loopback 服务。完整稳定性和浏览器回归在允许本机测试端口
及临时 Chrome 的环境中完成。
Chrome 回归同样在允许启动临时浏览器的环境中完成，全部业务接口由测试模拟。
运行浏览器脚本需要 Playwright 和 Chrome，可用 `NODE_PATH`/`CHROME_PATH` 指定。

基线提交说明中的 70 项定向回归属于历史记录，不是本次执行次数。早期裸 `unittest discover` 的
大量导入/配置错误不再作为“全仓库通过”的依据，应使用各模块的隔离入口。
测试桩已改为仅在真实 `webdriver_manager` 包不可用时安装，避免遮蔽真实子模块。

## 六、限制与后续验证

- **真实 NAS、本机 btrfs/ext4 卷、实际 WSGI 服务和外部字幕源尚未联调**；未做
  NAS 吞吐量、峰值内存、峰值数据库连接数或持续负载压测。
- 有界队列不保证每个触发都能执行；超时或取消不会释放仍在运行的真实资源槽。
  D 状态调用不能保证立即回收，隔离容量耗尽时仍会拒绝后续工作。
- 隔离覆盖的是接入这些工具的文件/网络路径，不是全仓库所有系统调用。数据库
  业务 SQL/fsync 仍在主进程，仍受底层磁盘影响；备份/完整性检查已隔离。
- WAL 仍仅一个写者，外部不遵循租约的进程和长读仍可能引发忙等待或资源拒绝。
  `latest_audit_snapshots` 的全量分支仍可能读取大量可见记录，未新增分页契约。
- 版本与新增索引消耗空间，FULL 分批增加提交同步；不能据本机 P95 承诺 NAS 总
  I/O 或完整审计耗时下降。真实断电和存储执行同步写入的行为未验证。
- JSON 操作记录持久化不等于可恢复执行队列；重启保留状态并中断未完成操作，
  不自动重放。已有事务和文件证据保护不能代替实际磁盘故障后的人工核对。

部署后的验证应覆盖上传总时限及 WSGI 支持、慢站点/慢盘时的队列预算、取消与重启
恢复、暂存额度及残留计费、数据库池告警/锁失败和长写事务耗时。上述验证尚未执行。

部署方须在同卷可丢弃库完成三轮对比，再在正常高峰及完整审计窗口采集以下证据：

- 两个数据库池的签出数量、峰值，以及任务结束后是否回落；
- 区分连接池耗尽与 SQLite 锁错误，记录实际 SQL 耗时、锁等待和写事务持续时间；
- 对比审计提交前后页面响应及磁盘延迟，确认是否存在稳定关联；
- 保存实际数据规模、统计信息状态和查询计划，逐项判断索引收益。

写入诊断提供有界等待/事务时长及短读快照计数，不等于全量 SQL/磁盘监控。
真实 NAS 未收集这些运行数据；未通过 NAS 门槛时不标记为“稳定性已提升”。出现
一致性问题、持续资源限制或性能退化须停止继续启用，按已验证的离线回退规则处理。

## 七、主要改动文件与提交核对

| 领域 | 主要文件 |
| --- | --- |
| 数据库与迁移 | `app/db/main_db.py`、`media_db.py`、`models.py`、`session_scope.py`、`settings.py`、`transactions.py`、`runtime.py`、`publication.py`、`backup.py`；迁移 `f3b7c1d9e204`、`7e1c9a42b605` |
| 预算与配置 | `app/utils/workload.py`、`scheduled_executor.py`；`app/helper/thread_helper.py`；`config.py`、`config/config.yaml` |
| I/O 隔离 | `app/utils/isolated_io.py`、`isolated_worker.py`、`isolated_fs.py`、`isolated_network.py`、`http_utils.py`、`system_utils.py` |
| 字幕与发布 | `app/helper/subtitle_tasks.py`、`online_subtitles.py`、`opensubtitles.py`；`app/subtitle.py`、`app/media/meta/extra_transfer.py` |
| 后台服务 | `app/scheduler.py`、`rsschecker.py`、`brushtask.py`、`torrentremover.py`、`speedlimiter.py`、`sync.py`、`filetransfer.py`、`downloader/downloader.py`、`indexer/indexer.py` |
| 其他阻塞入口 | `app/message/client/telegram.py`、`app/sites/siteuserinfo/_base.py` |
| 异步 Web | `app/helper/action_tasks.py`；`web/action.py`、`apiv1.py`、`main.py`、`backend/action_permissions.py` |
| 前端与交互契约 | `web/static/js/action-tasks/client.js`、`util.js`、`media-sync.js`、`special-confirmation.js`；`web/templates/action-tasks/panel.html`、`navigation.html`、`service.html`；相关 CSS、`DESIGN.md`、`UX-CONTRACT.md`、`premium-ui.json` |
| 验证与文档 | `tests/run_stability.py`、`run_recognition.py`、`run_database.py`、`test_database_*.py/js` 及相关回归；`scripts/audit_action_ui.py`、`verify_database_workload.py`；`AGENT.md`、`docs/database-governance.md` 与交付/预算说明 |

基线提交及当前工作树应分开核对：

```bash
git show --format=fuller --stat 8d84b13
git diff --name-status 1a4a8c8 8d84b13
git diff --stat
git status --short
```

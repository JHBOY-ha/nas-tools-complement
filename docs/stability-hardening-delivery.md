# NAS-Tools 稳定性治理交付说明

日期：2026-10-05
范围：字幕任务与全局后台服务的性能/阻塞问题治理（B0–B3）
状态：**B0–B3 已完成；B4 非数据库改造已实施，数据库相关改动暂停；真实 NAS 联调未执行**

2026-10-06 补充：有界任务队列、共享搜索/调度池、异步操作状态、上传租约和总时限、
在线字幕网络防护及危险 I/O 隔离已实施，详见 [NAS 任务预算](nas-workload-budget.md)。
数据库相关改动继续暂停，原数据库文件和受保护事务方法使用指纹核对保持一致。
下文的验证数字、改动文件和行数为 2026-10-05 交付记录，不包含这次补充。

本次补充的最终本地验证：稳定性 236 项、识别 341 项、在线字幕/OpenSubtitles 63 项，
两套真实浏览器回归、严格 UI 静态审查、设计文档 lint、语法与 diff 检查均通过。

---

## 一、交付概述

起因是一个具体疑问：**字幕库任务是否会长期挂载，导致队列堆积、NAS 卡死**；以及
**这些问题是否会导致本地元数据出错、进而引发 btrfs 报错**。

审查覆盖四个方向：字幕任务执行路径、网络/外部服务超时、数据库与锁竞争、
其余后台服务。共定位 25+ 项问题，按风险分成四批，**已完成前三批**。

两个必须先说清的结论：

1. **队列不会无限堆积。** `max_upload_queue` 默认 10，满了返回 429。
   真实症状是「队列非空但永远不动」——因为 upload/repair 共用一个 worker 线程。
2. **应用不是 btrfs 报错的原因。** NAS-Tools 是纯用户态程序，全仓库无
   `mkfs` / btrfs ioctl / `mount` / `/dev/*` / subvolume / snapshot 调用。
   btrfs 元数据完全由内核管理，用户态程序原理上无法破坏它。唯一沾边的间接
   链路是空间耗尽，排查方法见 `docs/btrfs-metadata-troubleshooting.md`。

---

## 二、已完成

### B0 — 观测、诊断与低风险修复

| 问题 | 修复 | 位置 |
|---|---|---|
| SQLite 默认 5 秒锁等待，慢盘并发写易抛 `database is locked` | `busy_timeout` 提升到 30 秒；连接池接近耗尽时告警（每 60 秒最多一条） | `app/db/main_db.py`、`app/db/media_db.py` |
| 缺索引导致持锁期间全表扫描 | 补 6 个索引：`SUBTITLE_TASK(CREATED_AT)`、`(FINISHED_AT)`、`(TYPE,STATUS,PRIORITY,CREATED_AT)`、`SUBTITLE_PROBE_CACHE(PAIR_PATH)`、`SUBTITLE_AUDIT_STATE(SUBTITLE_PATH)`、`TRANSFER_HISTORY(DATE)` | `app/db/models.py`、`subtitle_tasks.py` `_ensure_schema_columns`、迁移 `db_scripts/versions/f3b7c1d9e204_perf_indexes.py` |
| 刷流去重缓存只增不减，且 `not in list` 是 O(n) | 改为上限 5000 的 `OrderedDict` LRU，成员判断 O(1) | `app/brushtask.py` |
| 全仓库唯一漏掉 `timeout` 的请求（Telegram 发图） | 补 `timeout=(5, 30)` | `app/message/client/telegram.py` |
| 站点分页 `while next_page` 无上限，单站点可占用线程数十分钟 | 加 100 页 / 300 秒上限（3 处循环），顺带移除一处调试 `print` | `app/sites/siteuserinfo/_base.py` |
| 上传入口锁横跨 multipart body，慢客户端可钉死唯一入口槽 | body 读取加 120 秒硬超时，超时映射 408；无 `werkzeug.socket` 的服务器上自动降级 | `web/main.py` |
| 未定位的历史元数据问题 | 产出排查清单 | `docs/btrfs-metadata-troubleshooting.md` |

**验证**：`PRAGMA busy_timeout` 实测 5000→30000ms；5 万行规模下
`EXPLAIN QUERY PLAN` 确认四处热点查询命中索引；刷流缓存插入 5 万条后稳定在 5000；
上传超时的设置与恢复验证通过。

> 索引取舍说明：`invalidate_probe_cache` 的 `OR` 需要两侧都可索引。实测 SQLite
> 已能对现有 `(SERVER,PATH)` 唯一索引做 skip-scan，**刻意没有**再加单列 `PATH`
> 索引——那只会拖慢写入。

### B1 — 低风险行为修复

| 问题 | 修复 | 位置 |
|---|---|---|
| rclone/mc `subprocess.run` 无 `timeout`，远端无响应时永久挂起 | 加超时（默认 3600 秒，可用 `app.external_transfer_timeout` 覆盖），超时终止并提示目标可能残留不完整文件 | `app/utils/system_utils.py` |
| 文件转移由一把全局锁串行化，一次大文件复制或挂起的外部进程阻塞所有转移 | 改为**按目标路径**加锁：同目标仍互斥，不同文件并行，锁表无人等待时自动清理 | `app/filetransfer.py` |
| indexer 每次检索新建线程池且不 shutdown，`as_completed` 无超时 | `with` 管理线程池、整体 120 秒上限、单站点异常不再中断整次检索 | `app/indexer/indexer.py` |
| `transfer_all_sync` 无锁，可与定时任务并发导致重复识别/硬链接 | 非阻塞互斥，并发触发只执行一次 | `app/sync.py` |
| `start()` 与 `submit_upload()` 加锁顺序相反（ABBA 死锁） | 统一为 `_submit_lock -> _lock` | `app/helper/subtitle_tasks.py` |
| apscheduler 默认 `misfire_grace_time=1` 秒，进程繁忙时静默丢弃执行 | 显式设 60 秒 | `config.py` + 5 个调度器 |

**验证**：外部转移实测 1 秒超时在 1.0s 终止（而非等待 30s）；按目标加锁实测同目标
串行 0.35s、异目标并行 0.31s；indexer 实测 1 秒返回（而非被 30 秒慢站点拖住）；
全量同步 4 次并发触发只执行 1 次；加锁顺序倒置探针计数 = 0。

### B2 — 会话生命周期（最高危项）

**问题**：`pool_size=50, max_overflow=0`，且全仓库没有 `_Session.remove()` /
`close()` / Flask teardown。SQLAlchemy 1.4 的 Session 首次查询即 autobegin 并持有
连接直到事务结束，因此**每个做过查询的存活线程钉住一条连接**。潜在常驻线程约
apscheduler 20 + rsschecker 30 + ThreadHelper 50 + 字幕 3 ≈ 100+，超过池上限。

**修复**：新增 `app/db/session_scope.py`，在每个工作单元边界归还连接：

- Flask `teardown_appcontext`（覆盖 werkzeug 每请求新线程）— `web/main.py`
- `ThreadHelper.start_thread` — `app/helper/thread_helper.py`
- indexer 提交点 — `app/indexer/indexer.py`
- 5 个 `BackgroundScheduler` 的任务注册 — `app/scheduler.py`（新增 `_add_job`）
  及其余 4 个调度器
- 字幕管理器 3 个常驻 worker 的每轮迭代 — `app/helper/subtitle_tasks.py`

配套：`media_db` 补 `expire_on_commit=False`（与 user.db 对齐）；池参数
`pool_size=20, max_overflow=30, pool_use_lifo=True`（**净容量仍为 50**，刻意不缩小
上限以免突发并发下反而更容易超时）。

**验证**：实测确认机制——8 个常驻线程各做一次只读查询即打满 `pool_size=5`，
新线程抛 `QueuePool limit reached`；经包装后线程**存活时** `checkedout()` 为 0。
另外验证包装不改变 apscheduler 注册语义（内存 jobstore 的 ID 本就是随机 UUID）。

### B3 — 把 NAS I/O 移出全局锁

**问题**：字幕 manager 的全局 RLock 被大量面向 HTTP 的读操作持有，而这些路径里
又在持锁状态下做 NAS I/O（全文件哈希、`lstat`、`disk_usage`、`rmtree`）。一个挂死
的文件系统调用会同时冻结任务队列和 Web UI，连「取消」都点不动。

**修复**：统一手法「**锁内快照纯数据 → 锁外做 I/O → 锁内重读复核并提交**」。

改造的方法：`verify_upload_item_output`、`trusted_upload_item_hashes`、
`_upload_staging_valid`、`_reconcile_upload_outputs`、`ensure_task_target_space`、
`cancel_task`、`_recover_tasks`、`_claim_next_interactive`；新增
`_output_state_snapshot`（纯数据）与 `_evaluate_output_state`（哈希）拆分；
`_check_target_capacity` 拆出 `_assert_target_capacity`（纯算术）；
`_recover_tasks` 抽出 `_recover_repair_transaction`。

`cancel_task` 与 `_recover_tasks` 采用三段式：先落库取消意图（原子性锚点）→
锁外对账 → 锁内重读后写终态（对账期间 worker 若已置终态则以已落库结果为准）。

**保留的保证未变**（这些是文件系统证据驱动，不是锁驱动）：删除仍要求 ownership
marker + inode 证据；取消时保留已发布字幕；终态与发布校验一致。

**验证**：给 7 个 I/O 入口加探针，遍历上述全部方法与路径后，
**持锁期间 NAS I/O 次数 = 0**。

---

## 三、B4 当前状态（2026-10-06）

### 已实施的非数据库改造与暂停项

| 项 | 当前状态 |
|---|---|
| **WAL 模式** | 按用户要求暂停；未新增修改数据库源码、迁移、连接池或审计事务 |
| **同步长任务异步化** | 长操作返回 202/任务编号；共享前端查询终态后执行原回调，支持状态恢复与取消排队。私有 JSON 记录状态，不增加 SQL 表；重启不重放删除/移动 |
| **上传准入锁** | 接收前预占队列槽/空间，multipart 不持共享提交锁；最多两个接收租约，失败/断连释放。真实连接用 socket shutdown 定时器限制 120 秒总时长 |
| **在线字幕网络防护** | DNS、HTTP 在有界子进程执行；90 秒预算覆盖登录、重试、跳转、下载。共享 OpenSubtitles 锁仅保护 claim/发布代次，令牌刷新合并、配额消费单独准入 |
| **暂存配额默认值** | 暂存默认 1536 MiB、保留空闲 2048 MiB；仍满足 250 MiB 批次六倍空间。管理员显式配置保留；未知失败暂存继续计费 |
| **危险 I/O 隔离** | 任务暂存、哈希、容量/元数据、清理、转移和受保护发布使用复用子进程；超时未回收的进程/进程组继续占预算，不无限补开 |
| **调度统一准入** | 主服务/RSS/刷流/删种共享 6/64 有界池；播放检查复用交互容量；单一调度器关闭只影响自身任务 |

### 仍然成立的边界

- **不试图用看门狗「中断」阻塞线程。** Python 无法中断阻塞在不可中断系统调用
  （D 状态）里的线程，`threading` 没有安全强杀机制。看门狗最多把任务标记为失败，
  **救不回那个 worker 线程**，队列照样停摆。本轮的处置是在源头消除无限阻塞
  （外部命令超时、body 读取超时、站点分页上限）并把 NAS I/O 移出全局锁，
  使个别卡住的调用不再冻结整个应用。本轮已将上述主要危险 I/O 移入子进程；
  D 状态进程收到 SIGKILL 后仍可能要等内核调用结束，不能保证立即消失。
  父进程不无限等待，未回收容量保留，运维可据状态检查 NAS/文件系统。
- **不放宽既有安全约束**：任务终态原子提交、文件发布/删除前的 ownership 校验、
  `heavy_operation` 公平闸门语义均未改动。

---

## 四、验证方法与结果

```bash
python3 -m tests.run_stability      # 201 项通过
python3 -m tests.run_recognition    # 341 项通过
python3 -m unittest discover -s tests -t . -p "test_*.py"   # 与 HEAD 基线对比
```

| 检查 | 基线（HEAD） | 本次 |
|---|---|---|
| 全量 discover | 745 项 / 514 错误 | 756 项 / 507 错误 |
| 新增回归 | — | `tests/test_stability_regressions.py` 11 项 |

新增回归覆盖的不变量：连接在工作单元结束归还、锁内无 NAS I/O、加锁顺序、
按目标加锁的互斥与并行、外部命令超时、刷流缓存有界、全量同步互斥、
`busy_timeout`、新索引存在。

> discover 的错误数远高于实际失败数，是因为这些用例需要各自 runner 的网络与配置
> 隔离；两边的对比才是有效信号（测试数增加 11、错误数下降 7，无新增失败）。

### 顺带修掉的测试基建缺陷

`tests/test_media_library.py`、`test_subtitle_align.py`、`test_subtitle_upload.py`
的 `webdriver_manager` 桩原先**无条件**用普通模块对象遮蔽真实包，导致
`webdriver_manager.firefox` 无法解析，任何导入 feapder 的模块（如 `app/brushtask`）
在测试中 ImportError。真实的 `webdriver_manager.firefox` 本来是可用的。
已改为「仅在真实包不可用时才装桩」。

---

## 五、部署与运维注意事项

1. **索引迁移自动执行**：`run.py:105` 在启动时调用 `update_db()`（alembic upgrade
   head），新迁移 `f3b7c1d9e204` 会自动应用；`_ensure_schema_columns` 另外用
   `CREATE INDEX IF NOT EXISTS` 兜底。大表建索引会占用一些时间，属正常。
2. **外部转移命令现在有超时**：默认 3600 秒。合法的大文件云端传输若超过此值会被
   终止，且目标位置可能残留不完整文件。如你的场景需要更长时间，在 `config.yaml`
   的 `app` 节点下加 `external_transfer_timeout: <秒>`。
3. **文件转移不再全局串行**：不同目标的转移会并行，NAS 并发 I/O 会增加；
   同一目标仍互斥。
4. **上传 body 读取有 120 秒上限**：慢速客户端会收到 408。
5. **定时任务不再轻易丢执行**：延迟 60 秒内的执行会补跑（原先 1 秒后即静默丢弃）。
6. **连接池容量未变**（仍为 50），但空闲连接会被更积极地回收。

---

## 六、已知限制与未验证项

- **未在真实 NAS、真实 btrfs 卷、真实外部字幕服务上验证**；未做吞吐量、
  峰值内存与峰值连接数压测。所有验证基于临时 SQLite、内存库与模拟调用。
- 上述「持锁期间 NAS I/O = 0」是在探针覆盖的方法范围内成立；未加探针的裸调用
  不在结论范围内。
- 危险 I/O 仍无法被中断（见「明确不做」）。
- 存量长事务、`latest_audit_snapshots` 不带 paths 的全量分支等潜伏项未在本轮处理。

---

## 七、附：改动文件清单

**新增**

- `app/db/session_scope.py` — 工作单元边界归还数据库连接
- `db_scripts/versions/f3b7c1d9e204_perf_indexes.py` — 索引迁移
- `docs/btrfs-metadata-troubleshooting.md` — btrfs 排查清单
- `tests/test_stability_regressions.py` — 11 项稳定性回归
- `tests/run_stability.py` — 离线回归入口

**修改（24 个）**

`AGENT.md`、`config.py`、`web/main.py`、
`app/brushtask.py`、`app/filetransfer.py`、`app/sync.py`、`app/indexer/indexer.py`、
`app/scheduler.py`、`app/rsschecker.py`、`app/speedlimiter.py`、`app/torrentremover.py`、
`app/message/client/telegram.py`、`app/sites/siteuserinfo/_base.py`、
`app/utils/system_utils.py`、`app/helper/subtitle_tasks.py`、`app/helper/thread_helper.py`、
`app/db/main_db.py`、`app/db/media_db.py`、`app/db/models.py`

测试与夹具：`tests/test_media_library.py`、`tests/test_subtitle_align.py`、
`tests/test_subtitle_upload.py`、`tests/test_subtitle_tasks.py`、
`tests/test_subtitle_task_security.py`

合计：**+1034 / −335**。改动在工作区，**未提交**。

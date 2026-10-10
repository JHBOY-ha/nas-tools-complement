# btrfs 元数据问题的排查清单

本文针对「NAS 上曾出现元数据问题但未定位到源头」的场景，给出一套可执行的自查步骤，
并说明 NAS-Tools 在这条因果链上可能与不可能扮演的角色。

> 结论先行：**NAS-Tools 是纯用户态程序，不触碰任何底层卷操作**（全仓库无 `mkfs` /
> `btrfs` ioctl / `mount` / `/dev/*` / subvolume / snapshot 调用，只用
> `open/read/write/rename/link/unlink/fsync/rmtree`）。btrfs 的 extent tree、
> checksum tree、chunk tree 完全由内核管理，COW + 日志保证用户态程序**在原理上
> 无法破坏它**。因此应用代码不是元数据损坏的原因。
>
> 唯一沾边的间接链路是**空间耗尽**（见第 3 节），以及应用会**掩盖**文件系统故障
> （见第 5 节），使故障表现成「程序卡住」而掩盖真实原因。

---

## 1. 先取真实报错原文

不同报错对应完全不同的根因，必须先拿到 dmesg 原文，不要凭现象猜。

```bash
# 带时间戳的 btrfs 相关内核日志
sudo dmesg -T | grep -iE "btrfs|BTRFS" | tail -100
```

对照下表判断方向：

| 报错关键字 | 含义 | 指向 |
|---|---|---|
| `csum failed` / `checksum error` | 读到的数据与存储的校验和不符 | 硬件：内存、SATA/网线、硬盘 |
| `parent transid verify failed` / `bad tree block` | 元数据块本身损坏 | 断电/硬重启中断写、硬件、内核 bug |
| `BTRFS: error ... no space left` | 空间耗尽（**含 metadata 先耗尽**） | 容量策略，见第 3 节 |
| `BTRFS warning ... read-only` | 文件系统已切换为只读 | 上述任一故障的后果 |

## 2. 确认是否硬件层面在持续出错

```bash
# 每块 btrfs 设备的累计错误计数
sudo btrfs device stats /volume1
# 或对每个挂载点
mount | grep btrfs | awk '{print $3}' | while read m; do echo "== $m"; sudo btrfs device stats "$m"; done
```

**判读**：`write_errors` / `read_errors` / `checksum_errors` / `corruption_errors`
只要**在增长**，就是硬件或链路问题，与应用无关。记录一次，几天后再记录一次做对比。

## 3. 检查 metadata 是否先于 data 耗尽（唯一与应用沾边的路径）

btrfs 的 metadata chunk 可能先于 data chunk 用尽。此时 `df -h` 看起来还有空间，
但写入会失败并把卷切成只读——这正是「莫名其妙就坏了」的典型来源。

```bash
# 关键看 Metadata 一行的 Used / Free（不是 Data 行）
sudo btrfs filesystem usage -T /volume1
```

**判读**：Metadata 使用率接近 100% 是危险信号。

NAS-Tools 侧相关的写入量：

- 字幕暂存目录 = `Config().get_temp_path()` = **`/config/temp`**，而 `/config` 在
  Docker 里是宿主 bind mount（`docker/compose.yml:8`），可能与媒体库在**同一个
  btrfs 卷**上。
- 暂存额度 `staging_quota_mb` 默认 1536、可配到 4096；安全余量 `reserve_free_mb`
  默认 2048、配置下限仍为 256MB，以保留管理员显式策略；本轮不自动改写已保存配置。
- 把这个组合配成「暂存额度拉满 + 安全余量压到最小」是唯一可能把卷推到危险区间的
  配置方式。建议保持 `reserve_free_mb` 不低于 2048，并确认暂存卷与媒体卷是否同一个。

```bash
# 确认 /config 与媒体库是否同一卷（比较 st_dev 或挂载点）
df -h /volume1/docker/nas-tools/config /volume1/media
```

## 4. 核对是否已有静默损坏

```bash
# 只读校验，不改数据；耗时较长，建议低峰期执行
sudo btrfs scrub start -Bd /volume1
sudo btrfs scrub status -d /volume1
```

## 5. 区分「文件系统故障」与「应用卡住」

这是最容易误判的一步。NAS-Tools 代码中有约 **90 处 `except OSError`** 会静默
吞掉文件系统错误（`continue` / `pass` / `return False`）。当卷变只读后：

- 应用不会崩溃，而是**每个文件操作静默失败**；
- 表现为「任务一直不动」「界面刷新很慢」，看起来像程序性能问题；
- 真正的 `EROFS` / `ENOSPC` 从不出现在日志里。

```bash
# 直接验证卷当前是否可写（会创建一个极小的临时文件后立即删除）
touch /volume1/.nastools-write-probe && rm -f /volume1/.nastools-write-probe && echo "可写" || echo "只读或写入失败"

# 检查 NAS-Tools 进程是否卡在不可中断 I/O（D 状态）
ps -eo pid,stat,wchan:24,cmd | awk '$2 ~ /D/ {print}'
```

**判读**：出现 `D` 状态且 `wchan` 指向 NFS/CIFS/btrfs 的进程，说明它正卡在
不可中断的系统调用上——这正是本次审查中「字幕任务长期挂载」的机制。
注意 Python 无法中断这种线程，只能靠重启进程恢复。

## 6. 应用侧已做的防御（2026-10-05 稳定性治理 B0）

- SQLite `busy_timeout` 从 5 秒提升到 30 秒，减少慢盘下的 `database is locked`；
- 补齐字幕任务/探测缓存/审计状态/转移历史的缺失索引，消除持锁期间的全表扫描；
- 刷流去重缓存改为有界结构，不再长期单调增长；
- 站点抓取分页加页数与时长上限，避免单站点长期占用线程；
- 字幕上传 body 读取加硬超时，避免慢客户端钉死上传入口。

## 7. 若确认是硬件故障

应用侧的改动都无法修复 `csum failed` / `transid verify failed` 这类硬件或断电
造成的损坏。此时应：备份数据 → `btrfs scrub` 定位受影响文件 → 更换故障硬件
（内存优先，其次是线缆与硬盘）→ 再考虑 `btrfs restore` 或从备份恢复。

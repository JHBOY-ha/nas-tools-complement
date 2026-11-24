# config 目录

本目录保存程序内置资源，Git、Docker 镜像和 Windows 打包采用明确的文件白名单：

- `config.example.yaml`：默认配置模板。只保存通用默认值和空的凭据占位，不写入实际账号、Token、Cookie 或个人路径。
- `default-category.yaml`：默认媒体分类模板。
- `sites.dat`：内置站点规则。
- `init_filter.sql`、`init_userrss_v3.sql`、`reset_db_version.sql`、`update_subscribe.sql`、`update_userpris.sql`、`update_userrss.sql`：数据库初始化和升级脚本。

数据库（包括 SQLite 的 WAL、SHM 文件）、缓存、日志、备份、临时种子和测试产物均为本地运行数据，不提交、不打包。新增内置资源时，需要同步调整根目录 `.gitignore`、`.dockerignore` 和 `windows/nas-tools.spec` 的白名单；本说明只进入 Git。

## 实际运行配置

程序读取 `NASTOOL_CONFIG` 指定的实际配置文件；文件不存在时，从本目录的 `config.example.yaml` 复制生成，之后读写实际配置文件。已有配置直接加载，不覆盖、不用模板替换。

源码部署可继续使用 `NASTOOL_CONFIG=config/config.yaml`，生成的文件被 Git 忽略。也可指定仓库外的路径，例如 `/path/to/nastool-data/config.yaml`，并提前创建父目录。已有部署无需更改路径。Docker 继续使用 `/config/config.yaml` 和原有持久化挂载。

从仍跟踪 `config/config.yaml` 的旧版本升级前，应先将实际配置备份到仓库外。Git 更新移除旧的跟踪文件时，可能删除未修改的文件，或因本地修改而中止。更新后将备份恢复到原来的 `NASTOOL_CONFIG` 路径，再启动程序；保留的已有配置会直接加载。Docker 的外部 `/config` 挂载不受仓库内模板更名影响。

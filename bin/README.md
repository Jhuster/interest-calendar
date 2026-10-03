# 运行时目录

`bin/` 保存运行脚本与本机数据。源码部署需同时复制 `src/` 和 `bin/`，在目标机器重新执行 `init.sh`；容器部署使用构建后的镜像。脚本可从任意工作目录启动。

## 文件说明

- `init.sh`：首次安装 uv、Python 3.12 和运行依赖，无需预装 Python。
- `runtime-env.sh`：启动和初始化共用的运行环境配置。
- `runtime/`：本机下载的工具、Python 和缓存，不进入 Git；换机器后重新运行初始化。
- `start.sh`：前台启动服务，适合本机调试。
- `start-background.sh`：后台启动服务。日志写入 `bin/data/server.log`。启动前和运行中若超过 1 MiB 会轮转，只保留最近 7 份，权限为仅当前用户可读。运行中是复制后截断，避免已经打开的日志继续写进旧文件。
- `build-docker/`：容器构建子目录，包含 `build-image.sh`、`Dockerfile` 和容器启动入口。
- `demo.py`：按需生成隔离的虚构演示数据，不是正式数据采集程序。
- `data/`：正式数据库、备份、凭据和运行日志。该目录不会提交 Git。

## 启动

网页管理需要管理员令牌。前台启动且终端可交互时，控制台显示访问地址和当前管理令牌。后台启动只在当前终端显示一次令牌，不把令牌写入 `server.log`。令牌原文保存在权限为仅当前用户可读的 `bin/data/credentials.json`（字段 `admin_token`）。首次启动还会写入 `bin/data/first-run-credentials.txt`，请保存后删除该文件。旧安装若只保留了哈希且已删除首次凭据文件，需要按下方命令轮换一次。

如果已经运行过，在 `bin` 目录执行 `cat data/credentials.json`，使用其中的 `admin_token`。`agent_token` 用于程序上传，不用于网页登录。该原文若已不存在，使用 `uv run --directory ../src python -m app rotate --data-dir ../bin/data --role admin` 生成并打印新管理令牌，旧令牌立即失效。

首次使用先在项目根目录执行 `./bin/init.sh`，然后启动：

```sh
./bin/start-background.sh
```

首次启动时，程序会自动创建数据库、运行迁移和生成管理员/Agent 凭据，不需要手动初始化。首次凭据会写入 `bin/data/first-run-credentials.txt`（仅创建一次，权限为仅当前用户可读）；请保存后删除该文件。启动脚本会自动探测本机非回环局域网 IP 并生成 `BASE_URL`。只有需要固定地址时才覆盖：

```sh
BASE_URL=http://192.168.1.20:8787 ./bin/start-background.sh
```

如果本机没有可探测到的局域网地址，才会回退到 `127.0.0.1`，此时只能本机访问。

初始化需要网络和 curl 或 wget，使用 uv 官方安装源：https://docs.astral.sh/uv/getting-started/installation/ 。脚本可重复运行，项目依赖位于 `src/.venv/`，不会重置数据库。

## 停止

前台 `start.sh` 在运行它的终端按 Ctrl+C。后台服务执行：

```sh
uv run --directory ../src python -m app stop --data-dir ../bin/data
```

命令会结束占用该数据目录的服务进程。恢复数据前必须先停止；会话只保存在进程内存中，停止后即失效，需要重新登录。

## 备份与恢复

备份和恢复都使用数据目录里的 SQLite 文件，不包含凭据。恢复不会让已轮换的旧令牌重新生效。在 `bin` 目录执行：

```sh
uv run --directory ../src python -m app backup --data-dir ../bin/data
```

服务运行时每天会补一份当天的备份，并只保留最近 7 份，文件在 `bin/data/backups/`。上面的手工命令总会再写一份当前快照，即使当天的自动备份已经存在。迁移前用它保留一份额外副本。

恢复先停服务，再生成差异报告。报告列出备份之后新增的事件、来源别名、删除记录、已撤销兴趣和撤下差异；能读到当前库时，会把需要更新的事件 SEQUENCE 提升到已知最大值之上。核对报告后再执行，当前库会先被保留到 `bin/data/restore-preserved/`，该目录只留最近一份数据库。差异报告在 `bin/data/restore-reports/`，只留最近 7 份：

```sh
uv run --directory ../src python -m app stop --data-dir ../bin/data
uv run --directory ../src python -m app restore --data-dir ../bin/data --backup ../bin/data/backups/2026-10-03.sqlite3
uv run --directory ../src python -m app restore --data-dir ../bin/data --backup ../bin/data/backups/2026-10-03.sqlite3 --apply
```

将示例文件名换成 `backup` 命令打印的路径。`--apply` 会做完整性检查和日历快照解析，写差异报告，然后替换当前数据库。每次恢复都会生成新的 state_epoch，并增加一次数据版本以便重新发布。旧批次在运行记录中显示为「恢复前记录」，不能当作当前发布进度；未完成的旧批次需要重新采集。若当时已经没有当前库或最新快照可作为版本基线，报告只会说明「数据恢复到备份点」，不能承诺手机没有重复或状态没有倒退。这是人工核对后的恢复，不会自动回放任意历史时点。

恢复完成后重新启动服务，使用现有凭据登录。代码回滚不要用旧程序直接覆盖数据库；应先恢复兼容该数据库的程序版本。

## 容器部署

在项目根目录执行：

```sh
./bin/build-docker/build-image.sh
docker run -d --name interest-calendar --log-opt max-size=1m --log-opt max-file=7 -p 8787:8787 -e BASE_URL=http://192.168.1.20:8787 -v interest-calendar-data:/opt/interest-calendar/bin/data interest-calendar:local
```

将示例地址替换为服务器实际地址；容器不能可靠探测宿主机的局域网地址。`--log-opt` 把容器标准输出限制为 1 MiB × 7 份，镜像本身不声明这个上限。管理令牌在数据卷的 `credentials.json`（或首次的 `first-run-credentials.txt`）中，不写入容器标准输出。订阅地址在登录后的设置页，路径里带有密钥，不是固定的 `/calendar.ics`。

对公网开放 8787 之前，把 `BASE_URL` 设为 `https://…`，并用 `python -m app serve --cert … --key …` 或在反向代理上终止 TLS。进程监听 `0.0.0.0` 只表示接受本机各网卡上的连接，不代替阿里云安全组。首次启动后保存令牌，并删除 `first-run-credentials.txt`。细节见 `docs/存储与部署规格.md`。

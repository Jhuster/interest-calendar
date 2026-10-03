# 运行时目录

`bin/` 保存运行脚本与本机数据。源码部署需同时复制 `src/` 和 `bin/`，在目标机器重新执行 `init.sh`；容器部署使用构建后的镜像。脚本可从任意工作目录启动。

## 文件说明

- `init.sh`：首次安装 uv、Python 3.12 和运行依赖，无需预装 Python。
- `runtime-env.sh`：启动和初始化共用的运行环境配置。
- `runtime/`：本机下载的工具、Python 和缓存，不进入 Git；换机器后重新运行初始化。
- `start.sh`：本机与可信局域网的前台启动。不传应用的 `--dev`。
- `start-background.sh`：本机与可信局域网的后台启动，内部调用 `start.sh`。日志写入 `bin/data/server.log`。启动前和运行中若超过 1 MiB 会轮转，只保留最近 7 份，权限为仅当前用户可读。运行中是复制后截断，避免已经打开的日志继续写进旧文件。
- `start-ecs.sh`：公网 ECS 的前台启动。要求 `BASE_URL` 为 https 源，不探测局域网地址，不传 `--dev`，不改监听。
- `build-docker/`：容器构建子目录，包含 `build-image.sh`、`Dockerfile` 和容器启动入口。
- `demo.py`：按需生成隔离的虚构演示数据，不是正式数据采集程序。
- `data/`：正式数据库、备份、凭据和运行日志。该目录不会提交 Git。

## 本机启动

网页管理需要管理员令牌。前台启动且终端可交互时，控制台显示访问地址和当前管理令牌。后台启动只在当前终端显示一次令牌，不把令牌写入 `server.log`。令牌原文保存在权限为仅当前用户可读的 `bin/data/credentials.json`（字段 `admin_token`）。首次启动还会写入 `bin/data/first-run-credentials.txt`，请保存后删除该文件。旧安装若只保留了哈希且已删除首次凭据文件，需要按下方命令轮换一次。

如果已经运行过，在 `bin` 目录执行 `cat data/credentials.json`，使用其中的 `admin_token`。`agent_token` 用于程序上传，不用于网页登录。该原文若已不存在，使用 `uv run --directory ../src python -m app rotate --data-dir ../bin/data --role admin` 生成并打印新管理令牌，旧令牌立即失效。

首次使用先在项目根目录执行 `./bin/init.sh`，然后启动：

```sh
./bin/start-background.sh
```

首次启动时，程序会自动创建数据库、运行迁移和生成管理员/Agent 凭据，不需要手动初始化。首次凭据会写入 `bin/data/first-run-credentials.txt`（仅创建一次，权限为仅当前用户可读）；请保存后删除该文件。`start.sh` 和 `start-background.sh` 在未设置 `BASE_URL` 时探测本机非回环局域网 IP。只有需要固定局域网地址时才覆盖：

```sh
BASE_URL=http://192.168.1.20:8787 ./bin/start-background.sh
```

如果本机没有可探测到的局域网地址，才会回退到 `127.0.0.1`，此时只能本机访问。这两条脚本始终不传应用的 `--dev`。`--dev` 只用于开发：它要求配置里的 `BASE_URL` 是回环地址，但进程仍听 `0.0.0.0`，并不是只绑 `127.0.0.1`。公开订阅地址只在「订阅」页，形如 `http://<局域网IP>:8787/public/calendar.ics`，无需登录。`/calendar.ics` 没有内容。其他账户的地址在其登录后的「订阅」页。份数和公网边界见 `docs/存储与部署规格.md`。

初始化需要网络和 curl 或 wget，使用 uv 官方安装源：https://docs.astral.sh/uv/getting-started/installation/ 。脚本可重复运行，项目依赖位于 `src/.venv/`，不会重置数据库。

## 云主机

不要在公网机器（ECS 或其它云虚拟机）上使用 `start.sh` 或 `start-background.sh`。它们会在缺少 `BASE_URL` 时写成局域网 HTTP 地址。

```sh
BASE_URL=https://calendar.example.com ./bin/start-ecs.sh
```

直接在进程上终止 TLS 时，证书和私钥一起给出。没写端口时进程听 8787，所以公网源要带上这个端口，手机订阅才连得到：

```sh
BASE_URL=https://calendar.example.com:8787 \
CALENDAR_CERT=/etc/interest-calendar/fullchain.pem \
CALENDAR_KEY=/etc/interest-calendar/privkey.pem \
./bin/start-ecs.sh
```

- 监听仍是程序默认的 `0.0.0.0`。端口来自 `BASE_URL`，缺省 8787。脚本不另外绑定网卡，也不传 `--dev`。
- `BASE_URL` 必须是公网 HTTPS 源。未提供 `CALENDAR_CERT`/`CALENDAR_KEY` 时，由反向代理终止 TLS，再转到本机上的该端口。
- 云防火墙或安全组才是公网大门。不要把 8787 以 HTTP 对 `0.0.0.0/0` 开放。
- 公开订阅地址只在「订阅」页，形如 `https://calendar.example.com/public/calendar.ics`。不是 `/calendar.ics`。其他账户的 `/c/<token>/calendar.ics` 只在该用户登录后的「订阅」页。
- 应用进程本身仍接受 HTTP 启动，以便本机局域网用法不变。拒绝非 https 的是这条 ECS 脚本，不是程序。细节见 `docs/存储与部署规格.md`。

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

在项目根目录构建镜像：

```sh
./bin/build-docker/build-image.sh
```

容器入口不探测地址，也不传 `--dev`。未设置 `BASE_URL` 时入口使用 `http://127.0.0.1:8787`，只能当占位；实际运行必须自己设置 `BASE_URL`。进程在容器内仍听应用默认的 `0.0.0.0`，端口取自 `BASE_URL`，缺省 8787。`--log-opt` 把容器标准输出限制为 1 MiB × 7 份，镜像本身不声明这个上限。管理令牌在数据卷的 `credentials.json`（或首次的 `first-run-credentials.txt`）中，不写入容器标准输出。公开订阅地址形如 `<BASE_URL>/public/calendar.ics`，只在「订阅」页，不是 `/calendar.ics`。其他账户的地址仍是 `<BASE_URL>/c/<token>/calendar.ics`，只在该用户登录后的「订阅」页。

可信局域网可以把端口发到宿主机，地址用局域网 HTTP：

```sh
docker run -d --name interest-calendar --log-opt max-size=1m --log-opt max-file=7 -p 8787:8787 -e BASE_URL=http://192.168.1.20:8787 -v interest-calendar-data:/opt/interest-calendar/bin/data interest-calendar:local
```

公网云主机不要照搬上面这条。`BASE_URL` 必须是公网 HTTPS 源，并且不要把 8787 以 HTTP 对 `0.0.0.0/0` 开放。云防火墙或安全组才是大门。

反向代理终止 TLS 时，只把容器端口绑到宿主机回环，由代理对外提供 HTTPS：

```sh
docker run -d --name interest-calendar --log-opt max-size=1m --log-opt max-file=7 -p 127.0.0.1:8787:8787 -e BASE_URL=https://calendar.example.com -v interest-calendar-data:/opt/interest-calendar/bin/data interest-calendar:local
```

`BASE_URL` 没写端口时，容器内仍听 8787。代理把公网 443 转到 `127.0.0.1:8787`。云防火墙或安全组放行 443，不放行 8787。

在进程上终止 TLS 时，同时挂上证书和私钥。入口只有两个变量都存在才追加 `--cert`/`--key`：

```sh
docker run -d --name interest-calendar --log-opt max-size=1m --log-opt max-file=7 -p 8787:8787 -e BASE_URL=https://calendar.example.com:8787 -e CALENDAR_CERT=/certs/fullchain.pem -e CALENDAR_KEY=/certs/privkey.pem -v /etc/interest-calendar:/certs:ro -v interest-calendar-data:/opt/interest-calendar/bin/data interest-calendar:local
```

这条会在宿主机所有网卡上发布 8787，但客户端必须用 HTTPS。云防火墙或安全组不要再对 `0.0.0.0/0` 放行同一端口的明文 HTTP。首次启动后保存令牌，并删除数据卷里的 `first-run-credentials.txt`。细节见 `docs/存储与部署规格.md`。

# 运行时目录

`bin/` 保存运行脚本与本机数据。源码部署需同时复制 `src/` 和 `bin/`，在目标机器重新执行 `init.sh`；容器部署使用构建后的镜像。脚本可从任意工作目录启动。

## 文件说明

- `init.sh`：首次安装 uv、Python 3.12 和运行依赖，无需预装 Python。
- `runtime-env.sh`：启动和初始化共用的运行环境配置。
- `runtime/`：本机下载的工具、Python 和缓存，不进入 Git；换机器后重新运行初始化。
- `start.sh`：前台启动服务，适合本机调试。
- `start-background.sh`：后台启动服务，日志默认写入 `bin/data/server.log`。
- `build-docker/`：容器构建子目录，包含 `build-image.sh`、`Dockerfile` 和容器启动入口。
- `demo.py`：按需生成隔离的虚构演示数据，不是正式数据采集程序。
- `data/`：正式数据库、备份、凭据和运行日志。该目录不会提交 Git。

## 启动

网页管理需要管理员令牌。每次前台或后台启动成功，控制台都会显示访问地址和当前管理令牌；后台日志也包含令牌。令牌原文保存在权限受限的本机凭据文件中，轮换后显示新令牌。旧安装若只保留了哈希且已删除首次凭据文件，需要按下方命令轮换一次。

如果已经运行过，在 `bin` 目录执行 `cat data/first-run-credentials.txt`，取 `ADMIN_TOKEN=` 后面的内容登录。`AGENT_TOKEN` 用于程序上传，不用于网页登录。该文件若已删除，使用 `uv run --directory ../src python -m app rotate --data-dir ../bin/data --role admin` 生成并打印新管理令牌，旧令牌立即失效。

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

## 容器部署

在项目根目录执行：

```sh
./bin/build-docker/build-image.sh
docker run -d --name interest-calendar -p 8787:8787 -e BASE_URL=http://192.168.1.20:8787 -v interest-calendar-data:/opt/interest-calendar/bin/data interest-calendar:local
```

将示例地址替换为服务器实际地址；容器不能可靠探测宿主机的局域网地址。首次管理令牌见 `docker logs interest-calendar`。

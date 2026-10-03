FROM python:3.13-slim
WORKDIR /opt/interest-calendar
COPY src/pyproject.toml src/uv.lock /opt/interest-calendar/src/
COPY src/app /opt/interest-calendar/src/app
COPY src/schemas /opt/interest-calendar/src/schemas
RUN pip install --no-cache-dir uv && uv sync --locked --no-dev --project /opt/interest-calendar/src
VOLUME ["/opt/interest-calendar/bin/data"]
# json-file 日志上限不在镜像内生效，docker run 必须带 --log-opt max-size=1m --log-opt max-file=7。
# 进程监听仍是应用默认（0.0.0.0，端口来自 BASE_URL，缺省 8787）。不要把 8787 以 HTTP 发布到 0.0.0.0/0。
# 直接 TLS 时同时设置 CALENDAR_CERT 与 CALENDAR_KEY；否则由反向代理终止 TLS。
EXPOSE 8787
COPY bin/build-docker/container-entrypoint.sh /opt/interest-calendar/bin/container-entrypoint.sh
RUN chmod +x /opt/interest-calendar/bin/container-entrypoint.sh
ENTRYPOINT ["/opt/interest-calendar/bin/container-entrypoint.sh"]

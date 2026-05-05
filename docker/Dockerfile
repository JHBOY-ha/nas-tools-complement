FROM alpine:3.18

ARG TZ=Asia/Shanghai

ENV LANG="C.UTF-8" \
    TZ="${TZ}" \
    NASTOOL_CONFIG="/config/config.yaml" \
    NASTOOL_AUTO_UPDATE=false \
    NASTOOL_CN_UPDATE=false \
    NASTOOL_VERSION=master \
    PS1="\u@\h:\w \$ " \
    REPO_URL="https://github.com/JHBOY-ha/nas-tools-complement.git" \
    PYPI_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple" \
    ALPINE_MIRROR="mirrors.ustc.edu.cn" \
    PUID=0 \
    PGID=0 \
    UMASK=000 \
    WORKDIR="/nas-tools"

WORKDIR ${WORKDIR}

COPY package_list.txt /tmp/package_list.txt
COPY requirements.txt /tmp/requirements.txt

RUN apk add --no-cache libffi-dev \
    && sed -i 's/\r$//' /tmp/package_list.txt \
    && apk add --no-cache $(echo $(cat /tmp/package_list.txt)) \
    && ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo "${TZ}" > /etc/timezone \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && curl https://rclone.org/install.sh | bash \
    && if [ "$(uname -m)" = "x86_64" ]; then ARCH=amd64; elif [ "$(uname -m)" = "aarch64" ]; then ARCH=arm64; fi \
    && curl https://dl.min.io/client/mc/release/linux-${ARCH}/mc --create-dirs -o /usr/bin/mc \
    && chmod +x /usr/bin/mc \
    && pip install --break-system-packages --upgrade pip wheel \
    && pip install --break-system-packages setuptools==70.3.0 \
    && pip install --break-system-packages cython \
    && sed -i 's/fast-bencode==1.1.3/fast-bencode==1.1.8/g' /tmp/requirements.txt \
    && sed -i 's/typing_extensions==4.3.0/typing_extensions==4.15.0/g' /tmp/requirements.txt \
    && pip install --break-system-packages --use-deprecated=legacy-resolver -r /tmp/requirements.txt \
    && apk del libffi-dev \
    && npm install pm2 -g \
    && rm -rf /tmp/* /root/.cache /var/cache/apk/*

COPY . ${WORKDIR}

RUN python_ver=$(python3 -V | awk '{print $2}') \
    && sed -i 's/\r$//' ${WORKDIR}/docker/entrypoint.sh \
    && chmod +x ${WORKDIR}/docker/entrypoint.sh \
    && echo "${WORKDIR}/" > /usr/lib/python${python_ver%.*}/site-packages/nas-tools.pth \
    && echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.conf \
    && echo 'fs.inotify.max_user_instances=524288' >> /etc/sysctl.conf \
    && git config --global pull.ff only \
    && git config --global --add safe.directory ${WORKDIR}

EXPOSE 3000
VOLUME ["/config"]

ENTRYPOINT ["/nas-tools/docker/entrypoint.sh"]

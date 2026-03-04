#!/bin/sh

cd ${WORKDIR}
if [ "${NASTOOL_AUTO_UPDATE}" = "true" ]; then
    if [ ! -s /tmp/requirements.txt.sha256sum ]; then
        sha256sum requirements.txt > /tmp/requirements.txt.sha256sum
    fi
    if [ ! -s /tmp/third_party.txt.sha256sum ]; then
        sha256sum third_party.txt > /tmp/third_party.txt.sha256sum
    fi
    if [ "${NASTOOL_VERSION}" != "lite" ]; then
        if [ ! -s /tmp/package_list.txt.sha256sum ]; then
            sha256sum package_list.txt > /tmp/package_list.txt.sha256sum
        fi
    fi
    echo "更新程序..."
    branch="${NASTOOL_VERSION:-master}"
    if ! git ls-remote --exit-code --heads "${REPO_URL}" "${branch}" >/dev/null 2>&1; then
        echo "分支 ${branch} 不存在，回退到 master..."
        branch="master"
    fi
    if [ ! -d .git ]; then
        echo "未检测到.git，正在初始化仓库用于自动更新..."
        init_repo_dir="/tmp/nas-tools-init-repo"
        rm -rf "${init_repo_dir}"
        git clone --depth 1 -b "${branch}" "${REPO_URL}" "${init_repo_dir}" >/dev/null 2>&1
        if [ $? -eq 0 ]; then
            cp -a "${init_repo_dir}/.git" "${WORKDIR}/.git"
            rm -rf "${init_repo_dir}"
            echo "仓库初始化完成..."
        else
            rm -rf "${init_repo_dir}"
            echo "仓库初始化失败，继续使用旧的程序来启动..."
        fi
    fi
    if [ -d .git ]; then
        if git remote | grep -q "^origin$"; then
            git remote set-url origin "${REPO_URL}" >/dev/null 2>&1
        else
            git remote add origin "${REPO_URL}" >/dev/null 2>&1
        fi
        echo "windows/" > .gitignore
        git fetch --depth 1 origin ${branch}
        git reset --hard origin/${branch}
    fi
    if [ $? -eq 0 ] && [ -d .git ]; then
        git clean -dffx
        echo "更新成功..."
        # Python依赖包更新
        hash_old=$(cat /tmp/requirements.txt.sha256sum)
        hash_new=$(sha256sum requirements.txt)
        if [ "${hash_old}" != "${hash_new}" ]; then
            echo "检测到requirements.txt有变化，重新安装依赖..."
            runtime_requirements="/tmp/requirements.runtime.txt"
            cp requirements.txt "${runtime_requirements}"
            # fast-bencode 1.1.3 在较新 Python 环境下构建容易失败，自动更新时统一替换为兼容版本
            sed -i 's/fast-bencode==1.1.3/fast-bencode==1.1.8/g' "${runtime_requirements}"
            # openai 1.30.x 依赖 typing_extensions>=4.7，旧版本会导致导入失败
            sed -i 's/typing_extensions==4.3.0/typing_extensions==4.15.0/g' "${runtime_requirements}"
            if [ "${NASTOOL_CN_UPDATE}" = "true" ]; then
                pip install --break-system-packages --disable-pip-version-check --use-deprecated=legacy-resolver -r "${runtime_requirements}" -i "${PYPI_MIRROR}"
            else
                pip install --break-system-packages --disable-pip-version-check --use-deprecated=legacy-resolver -r "${runtime_requirements}"
            fi
            if [ $? -ne 0 ]; then
                echo "无法安装依赖，请更新镜像..."
            else
                echo "依赖安装成功..."
                sha256sum requirements.txt > /tmp/requirements.txt.sha256sum
                hash_old=$(cat /tmp/third_party.txt.sha256sum)
                hash_new=$(sha256sum third_party.txt)
                if [ "${hash_old}" != "${hash_new}" ]; then
                    echo "检测到third_party.txt有变化，更新第三方组件..."
                    git submodule update --init --recursive
                    if [ $? -ne 0 ]; then
                        echo "无法更新第三方组件，请更新镜像..."
                    else
                        echo "第三方组件安装成功..."
                        sha256sum third_party.txt > /tmp/third_party.txt.sha256sum
                    fi
                fi
            fi
        fi
        # 系统软件包更新
        if [ "${NASTOOL_VERSION}" != "lite" ]; then
            hash_old=$(cat /tmp/package_list.txt.sha256sum)
            hash_new=$(sha256sum package_list.txt)
            if [ "${hash_old}" != "${hash_new}" ]; then
                echo "检测到package_list.txt有变化，更新软件包..."
                if [ "${NASTOOL_CN_UPDATE}" = "true" ]; then
                    sed -i "s/dl-cdn.alpinelinux.org/${ALPINE_MIRROR}/g" /etc/apk/repositories
                    apk update -f
                fi
                apk add --no-cache libffi-dev
                apk add --no-cache $(echo $(cat package_list.txt))
                if [ $? -ne 0 ]; then
                    echo "无法更新软件包，请更新镜像..."
                else
                    apk del libffi-dev
                    echo "软件包安装成功..."
                    sha256sum package_list.txt > /tmp/package_list.txt.sha256sum
                fi
            fi
        fi
    else
        echo "更新失败，继续使用旧的程序来启动..."
    fi
else
    echo "程序自动升级已关闭，如需自动升级请在创建容器时设置环境变量：NASTOOL_AUTO_UPDATE=true"
fi

echo "以PUID=${PUID}，PGID=${PGID}的身份启动程序..."

if [ "${NASTOOL_VERSION}" = "lite" ]; then
    mkdir -p /.pm2
    chown -R "${PUID}":"${PGID}" "${WORKDIR}" /config /.pm2
else
    mkdir -p /.local
    mkdir -p /.pm2
    chown -R "${PUID}":"${PGID}" "${WORKDIR}" /config /usr/lib/chromium /.local /.pm2
    export PATH=${PATH}:/usr/lib/chromium
fi
umask "${UMASK}"
exec su-exec "${PUID}":"${PGID}" "$(which dumb-init)" "$(which pm2-runtime)" start run.py -n NAStool --interpreter python3

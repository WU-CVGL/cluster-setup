# The cluster's default task image (services/task-images/README.md): Determined's pytorch-ngc-dev
# image with a newer TensorBoard and pip pointed at the cluster's PyPI cache.
FROM determinedai/pytorch-ngc-dev:0736b6d

# The index that /etc/pip.conf names (the PyPI cache at its campus address, which every pool reaches).
ARG CLUSTER_INDEX_URL=http://10.0.1.68:5000/index/
ARG CLUSTER_TRUSTED_HOST=10.0.1.68
# The index of this build; elsewhere: --build-arg BUILD_INDEX_URL=https://mirrors.bfsu.edu.cn/pypi/web/simple
ARG BUILD_INDEX_URL=${CLUSTER_INDEX_URL}
ARG TENSORBOARD_VERSION=2.20.0

LABEL org.opencontainers.image.base.name="docker.io/determinedai/pytorch-ngc-dev:0736b6d" \
      lab.cvgl.tensorboard.version="${TENSORBOARD_VERSION}"

# The base image has five identical pip configuration files with a dead extra index
# (https://pypi.ngc.nvidia.com); /usr/pip.conf would override /etc/pip.conf, so all go.
# The image's numpy, protobuf and requests stay as they are (constraints).
RUN rm -f /usr/pip.conf /etc/xdg/pip/pip.conf /root/.config/pip/pip.conf /root/.pip/pip.conf \
 && printf '[global]\nno-cache-dir = true\nindex-url = %s\ntrusted-host = %s\n' \
      "${CLUSTER_INDEX_URL}" "${CLUSTER_TRUSTED_HOST}" > /etc/pip.conf \
 && pip freeze | grep -iE '^(numpy|protobuf|requests)==' > /tmp/constraints.txt \
 && test "$(wc -l < /tmp/constraints.txt)" -eq 3 \
 && PIP_CONFIG_FILE=/dev/null pip install --no-cache-dir --disable-pip-version-check \
      --index-url "${BUILD_INDEX_URL}" --trusted-host "${CLUSTER_TRUSTED_HOST}" \
      -c /tmp/constraints.txt "tensorboard==${TENSORBOARD_VERSION}" \
 && rm -rf /tmp/constraints.txt /root/.cache/pip

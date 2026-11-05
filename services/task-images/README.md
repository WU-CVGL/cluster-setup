# Task images

Images that the Determined tasks of the cluster run in, built on the supplementary services VM and pushed to Harbor (`harbor.cvgl.lab/library`, a public project). They are not compose services.

The cluster keeps its own default image because Determined's environments repository, where `determinedai/pytorch-ngc-dev` comes from, is no longer updated: its `0736b6d` image has TensorBoard 2.9.0 and a pip configuration with a dead extra index (`https://pypi.ngc.nvidia.com`), which makes every pip install wait for minutes.

## pytorch-ngc

[`pytorch-ngc/Dockerfile`](pytorch-ngc/Dockerfile): `determinedai/pytorch-ngc-dev:0736b6d` (the fork's built-in default image) with

- TensorBoard 2.20.0, installed under the image's own numpy, protobuf and requests (constraints; TensorBoard 2.21 needs protobuf 6);
- one pip configuration file, `/etc/pip.conf`, that names the [PyPI cache](../../docs/04_Setup_Supplementary_Services.md#pypi-cache) (`http://10.0.1.68:5000/index/`) and no extra index, in place of the base image's five;
- the base image's user, entrypoint and working directory unchanged.

Inside Determined tasks `/etc/pip.conf` does not apply: the master's task container defaults set `PIP_CONFIG_FILE=/dev/null` and the `PIP_INDEX_URL` of the same cache. The file is for `docker run` use of the image and for a task that sets `PIP_CONFIG_FILE=` itself (the master's `PIP_INDEX_URL` still overrides its `index-url`).

`master.yaml` makes it the default image of every task (`task_container_defaults.image`); a task that sets `environment.image` is unaffected.

### Build and push

On the supplementary services VM (the build reaches the PyPI cache; the first build pulls the base image, about 10 GB, from Docker Hub through the proxy; the first push uploads the whole image, over 10 GB, to the NAS-backed registry):

```bash
cd services/task-images/pytorch-ngc
docker login harbor.cvgl.lab
docker build -t harbor.cvgl.lab/library/pytorch-ngc-dev:0736b6d-tb2.20.0 .
docker push harbor.cvgl.lab/library/pytorch-ngc-dev:0736b6d-tb2.20.0
```

To skip that pull, load the base image from a GPU node that has it before the first build:

```bash
ssh cvgl-node01 docker save determinedai/pytorch-ngc-dev:0736b6d | docker load
```

Elsewhere, build from the BFSU mirror: `docker build --build-arg BUILD_INDEX_URL=https://mirrors.bfsu.edu.cn/pypi/web/simple ...`.

Check the result:

```bash
docker run --rm --entrypoint bash harbor.cvgl.lab/library/pytorch-ngc-dev:0736b6d-tb2.20.0 -c \
  'pip config list; python -c "import tensorboard, numpy; print(tensorboard.__version__, numpy.__version__)"'
```

### Another TensorBoard version

Change `TENSORBOARD_VERSION` in the Dockerfile, build and push with the tag `0736b6d-tb<version>`, set both images in `master.yaml` to it, and restart the master ([Changing master.yaml](../determined/README.md#changing-masteryaml)). The build fails when that version needs newer numpy, protobuf or requests than the image has.

# SPDX-License-Identifier: AGPL-3.0-only
# The broker image.
#   runtime  (the default) the pinned interpreter, its dependencies and our code, as nobody
#   test     runtime plus the unit tests, for CI only; never pushed
# Python is the official CPython 3.13 point release image, pinned by digest;
# aiohttp, cryptography and every dependency come from requirements/broker.txt,
# pinned by version and SHA-256, as wheels only.
FROM python:3.13.16-slim-trixie@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS runtime
COPY requirements/broker.txt /tmp/requirements.txt
# pip is needed only to install; removing it keeps it out of the image.
RUN pip install --no-cache-dir --disable-pip-version-check --no-deps --require-hashes \
      --only-binary :all: -r /tmp/requirements.txt \
 && pip uninstall -y --disable-pip-version-check pip \
 && rm /tmp/requirements.txt
WORKDIR /opt/rootkeel
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/rootkeel/
COPY rootkeel ./rootkeel
COPY rt ./rt
USER 65534:65534
ENTRYPOINT ["python3", "-m", "rootkeel.broker"]

FROM runtime AS test
COPY server ./server
COPY agent ./agent
COPY scripts ./scripts
COPY tests ./tests

# Last, so that a build without --target is the runtime image.
FROM runtime

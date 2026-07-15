FROM python@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir pytest==8.4.1

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY base_repo/ /app/
RUN git init -b main \
 && git config user.name "DeepSWE Test Fixture" \
 && git config user.email "test-fixture@local" \
 && git add . \
 && GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00" GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00" git commit -q -m "Initial test fixture" \
 && git config core.hooksPath /dev/null

COPY test.sh /tests/test.sh
COPY test.patch /tests/test.patch
COPY grader.py /tests/grader.py
COPY config.json /tests/config.json
RUN chmod +x /tests/test.sh

CMD ["/bin/bash"]

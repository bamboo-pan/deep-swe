FROM python@sha256:9bed8554e926c07c6f908841d5ee88c33e8df9236b191526bbce81a9062ab43a

RUN python -m pip install --no-cache-dir pytest==8.4.1

ENV PYTHONDONTWRITEBYTECODE=1
ENV UV_INSTALLER_GITHUB_BASE_URL=https://ghfast.top/https://github.com
WORKDIR /app
COPY repo/ /app/
RUN git init -b main \
 && git config user.name "DeepSWE Test Fixture" \
 && git config user.email "test-fixture@local" \
 && git add . \
 && GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00" GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00" git commit -q -m "Initial test fixture" \
 && git config core.hooksPath /dev/null

CMD ["/bin/bash"]

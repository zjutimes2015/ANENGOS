FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANENGOS_HOME=/srv/anengos \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/anengos

# 先装依赖（利用缓存层），再拷代码
COPY pyproject.toml README.md ./
COPY kernel ./kernel
COPY governance ./governance
COPY connectors ./connectors
COPY gadgets ./gadgets
COPY app.py ./
RUN pip install --no-cache-dir .

RUN mkdir -p workspace audit

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1

CMD ["python", "app.py"]

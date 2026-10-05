FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /srv

# 零三方依赖：仅复制应用代码与测试/脚本
COPY app/ ./app/
COPY tests/ ./tests/
COPY scripts/ ./scripts/

RUN chmod +x scripts/verify.sh && python3 -m compileall -q app scripts tests

EXPOSE 8080

# slim 镜像不带 curl，健康检查使用标准库
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz', timeout=2).status==200 else 1)"

CMD ["python3", "app/server.py"]

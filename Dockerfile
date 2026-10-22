FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.32.*" httpx==0.27.*
COPY app/ /app/
EXPOSE 8090
# no uvicorn proxy headers: the app reads X-Forwarded-For itself (client_ip), and only with Caddy's secret
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090", "--no-proxy-headers"]

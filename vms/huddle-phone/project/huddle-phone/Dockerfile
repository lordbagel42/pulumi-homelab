FROM node:22-bookworm-slim AS web
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY web/ web/
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium \
      ca-certificates fonts-liberation tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home bridge \
    && mkdir /state && chown bridge:bridge /state
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY huddle_phone/ huddle_phone/
COPY web/ web/
COPY --from=web /build/web/dist/ web/dist/
USER bridge
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "huddle_phone"]
CMD ["run"]

#!/bin/sh
# End-to-end test against a Docker host that cannot see this machine's files,
# e.g. DOCKER_HOST=tcp://127.0.0.1:23750 with a docker:dind container.
set -eu
: "${DOCKER_HOST:?set DOCKER_HOST to a daemon that does not share this filesystem}"
CI=${COMPOSE_INLINE:-compose-inline}
dir=$(mktemp -d)
trap 'cd /; (cd "$dir" && $CI down -t 0 >/dev/null 2>&1) || true; rm -rf "$dir"' EXIT
cd "$dir"
printf 'price: $5\n' > app.conf
printf 'user nobody;\n' > nginx.conf
chmod 640 nginx.conf
printf 's3cr3t' > pw.txt
cat > compose.yaml <<'YAML'
name: ci-e2e
services:
  app:
    image: busybox:1.36
    command: sh -c 'cat /app.conf /etc/nginx.conf /run/secrets/pw; echo; stat -c "%a %n" /etc/app.conf /etc/nginx.conf; sleep 300'
    configs:
      - app.conf
      - source: app.conf
        target: /etc/app.conf
        mode: 0600
    secrets: [pw]
    volumes:
      - ./nginx.conf:/etc/nginx.conf:ro
configs:
  app.conf:
    file: ./app.conf
secrets:
  pw:
    file: ./pw.txt
YAML

echo "plain docker compose fails on the remote host:"
if out=$(timeout 120 docker compose run --rm app 2>&1); then
  echo "expected failure, got: $out"; exit 1
fi
echo "$out" | grep -q 'bind source path does not exist'

echo "compose-inline run:"
out=$(timeout 120 $CI --bind-files run --rm --entrypoint sh app -c 'cat /app.conf /etc/nginx.conf /run/secrets/pw; echo; stat -c "%a %n" /etc/app.conf /etc/nginx.conf')
echo "$out"
echo "$out" | grep -qx 'price: \$5'
echo "$out" | grep -qx 'user nobody;'
echo "$out" | grep -qx 's3cr3t'
echo "$out" | grep -qx '600 /etc/app.conf'
echo "$out" | grep -qx '640 /etc/nginx.conf'

echo "compose-inline up recreates only on content change:"
timeout 120 $CI --bind-files up -d --quiet-pull
id1=$(docker compose ps -q app)
timeout 120 $CI --bind-files up -d
id2=$(docker compose ps -q app)
test "$id1" = "$id2"
printf 'changed' > pw.txt
timeout 120 $CI --bind-files up -d
id3=$(docker compose ps -q app)
test "$id1" != "$id3"
timeout 60 docker exec "$id3" cat /run/secrets/pw | grep -qx changed

echo "kept: a config path that only exists on the Docker host"
sed -i 's#file: ./app.conf#file: /nonexistent/on/client#' compose.yaml
timeout 60 $CI --print 2>&1 >/dev/null | grep -q 'kept config app.conf'
echo OK

# compose-inline

Run `docker compose` against a remote Docker host (`DOCKER_HOST=ssh://...`,
`docker context use ...`, `tcp://...`) when the project uses `configs` or
`secrets` with `file:`, or bind-mounts single files such as
`./nginx.conf:/etc/nginx/nginx.conf`.

Docker Compose implements `configs.*.file` and `secrets.*.file` as bind
mounts. A bind mount is resolved on the Docker host, so with a remote host
the file has to exist at the same absolute path on that machine, and
otherwise the container fails to start:

```
Error response from daemon: invalid mount config for type "bind":
bind source path does not exist: /home/me/project/secrets/db_password.txt
```

Compose already has a second code path that works remotely: configs with
`content:` and configs or secrets with `environment:` are written into the
container through the Docker API before it starts. `compose-inline` reads
the resolved model with `docker compose config`, switches every config and
secret whose file exists on your machine to that path, and then runs the
real `docker compose` command with the rewritten model.

## Why

- [docker/compose#11867](https://github.com/docker/compose/issues/11867)
  "Support remote DOCKER_HOST by copying configs and secrets files instead
  of bind mounting them": open since 05/2024, 45 reactions, 50 comments.
  The maintainer agrees with the direction but wants an opt-in syntax
  because copying changes existing behaviour. Five pull requests
  implementing it were closed without merge
  ([#11871](https://github.com/docker/compose/pull/11871),
  [#11984](https://github.com/docker/compose/pull/11984),
  [#12251](https://github.com/docker/compose/pull/12251),
  [#12448](https://github.com/docker/compose/pull/12448),
  [#12625](https://github.com/docker/compose/pull/12625)). One commenter
  asked specifically for a way to avoid "manually pre-process the
  configuration files in order to copy them into the `content` property",
  which is what this tool does.
- Earlier reports of the same failure:
  [#7857](https://github.com/docker/compose/issues/7857),
  [#11141](https://github.com/docker/compose/issues/11141),
  [#7110](https://github.com/docker/compose/issues/7110).
- Bind-mounted configs and secrets ignore `uid`, `gid` and `mode`
  ([#9648](https://github.com/docker/compose/issues/9648), 30 reactions).
  Copied files honour them.
- Copying files into containers instead of mounting them is a long-standing
  request ([#1664](https://github.com/docker/compose/issues/1664), 129
  reactions; [#5523](https://github.com/docker/compose/issues/5523), 741
  reactions, both closed).

The workarounds suggested in the thread are copying the files to the Docker
host and using absolute paths there, keeping a separate override file per
host, or baking the files into an image with `dockerfile_inline`. Deploy
scripts that `rsync` the project and run Compose over SSH on the server need
shell access to the host. `compose-inline` only needs the Docker API.

## Install

```sh
pipx install git+https://github.com/Jwrede/compose-inline
# or
pip install git+https://github.com/Jwrede/compose-inline
```

Python 3.9+ and the `docker` CLI with the Compose plugin. No other
dependencies.

## Usage

```
compose-inline [OPTIONS] COMMAND [ARGS...]
```

`COMMAND [ARGS...]` is any `docker compose` command. OPTIONS are the
Compose global options (`-f`, `-p`, `--profile`, `--env-file`,
`--project-directory`, ...), Docker's `--context`/`-c` and `--host`/`-H`,
and:

| Option | Effect |
|---|---|
| `--bind-files` | Also pass single-file bind mounts from inside the project directory by value |
| `--print` | Print the rewritten model and exit (secret values are not in it) |
| `--verbose` | List every inlined file on stderr |

## Example

```yaml
# compose.yaml
services:
  web:
    image: nginx:1.27-alpine
    configs:
      - source: site
        target: /etc/nginx/conf.d/default.conf
    secrets: [db_password]
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
configs:
  site:
    file: ./site.conf
secrets:
  db_password:
    file: ./secrets/db_password.txt
```

With `site.conf` answering `hello from compose-inline` and a Docker host
that does not have the project files (output from a test run, paths
shortened):

```console
$ export DOCKER_HOST=ssh://deploy@server
$ docker compose up -d
 Container app-web-1 Error response from daemon: invalid mount config for type "bind": bind source path does not exist: /home/me/app/secrets/db_password.txt

$ compose-inline --bind-files --verbose up -d --wait
compose-inline: inlined config site from /home/me/app/site.conf (87 bytes)
compose-inline: inlined secret db_password from /home/me/app/secrets/db_password.txt (22 bytes)
compose-inline: inlined bind mount /home/me/app/nginx.conf:/etc/nginx/nginx.conf of service web (57 bytes)
 Container app-web-1 Created
 Container app-web-1 Started
 Container app-web-1 Healthy

$ docker compose exec web sh -c 'wget -qO- 127.0.0.1; cat /run/secrets/db_password'
hello from compose-inline
correct horse battery

$ docker compose down
```

The same works with `--context prod`, `docker context use prod`, or
`DOCKER_CONTEXT=prod`. Commands that do not create containers (`ps`, `logs`,
`down`) can be run with plain `docker compose` as usual, since the project
name is the same.

## How it works

1. `docker compose [global options] config --format json` produces the
   merged and interpolated model with absolute paths.
2. For every config or secret referenced by a service whose `file` is a
   regular file on this machine:
   - configs become `content:` (with `$` escaped, because Compose
     interpolates the model again);
   - secrets, and configs that are not valid UTF-8, become
     `environment: COMPOSE_INLINE_<n>`, and the value is passed only in the
     environment of the `docker compose` child process. Secret values are
     never written to disk.
3. With `--bind-files`, a `type: bind` volume whose source is a regular file
   inside the project directory becomes a config with the same target and
   the file's permission bits as `mode`.
4. Each affected service gets a label
   `io.github.jwrede.compose-inline.hash` with a hash of the inlined
   contents. Compose does not hash config or secret contents into the
   service, so without the label an edited file would not recreate the
   container on the next `up`.
5. The model is written to a private temporary file and
   `docker compose --project-directory <dir> -p <name> -f <tmp> COMMAND ...`
   runs with it. The temporary file is deleted when the command exits.

## Limits

- Files are copied once, when the container is created. Editing a file
  locally has no effect on a running container until the next `up`, which
  recreates the affected services. A bind mount would show the change
  immediately.
- Compose refuses to copy into services with `read_only: true`
  (`cannot create secret ... in read-only service web`).
- Copied files are owned by root (`0:0`) unless the service sets `uid` and
  `gid` on the config or secret. Configs default to mode `0444`. A
  bind-mounted file keeps the owner it has on the host, so a container
  that runs as a non-root user and relied on that ownership may need
  `uid`/`gid` or a wider `mode`.
- Only regular files. Directories, and paths that do not exist on this
  machine, stay bind mounts and are then resolved on the Docker host, which
  keeps the "absolute path on the server" workaround working. A note is
  printed for each.
- `--bind-files` only converts files inside the project directory, so
  mounts like `/etc/localtime` or `/var/run/docker.sock` keep referring to
  the Docker host. Directory bind mounts are not converted.
- Files with NUL bytes are rejected. Secrets go through the environment,
  which Linux limits to 128 KiB per value.
- The containers' `com.docker.compose.project.config_files` label points
  at the deleted temporary file.
- Tested with Docker Compose v5.0.0 locally and v2.38.2 (GitHub
  `ubuntu-24.04` runner) and v2.24.7 in CI, each against a Docker 27.5.1
  host (`docker:27-dind`) reached over TCP. Not tested with Podman.

## Development

```sh
python -m unittest discover -s tests -v
# end-to-end, needs a Docker host that does not share this filesystem:
docker run -d --privileged --name remote -e DOCKER_TLS_CERTDIR= \
  -p 127.0.0.1:23750:2375 docker:27-dind --host=tcp://0.0.0.0:2375
DOCKER_HOST=tcp://127.0.0.1:23750 sh tests/e2e.sh
```

## License

MIT

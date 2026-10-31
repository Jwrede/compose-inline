"""Run docker compose against a remote Docker host with local configs and secrets.

Compose implements `configs.*.file` and `secrets.*.file` as bind mounts, so the
paths are resolved on the Docker host. With DOCKER_HOST or a docker context
pointing at another machine the files are missing there. Compose copies
configs with `content` and configs/secrets with `environment` into the
container through the API instead. This tool rewrites the resolved model
(`docker compose config`) to use those forms and then runs the real command.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile

__version__ = "0.1.0"

HASH_LABEL = "io.github.jwrede.compose-inline.hash"
ENV_PREFIX = "COMPOSE_INLINE_"
# Linux limits a single environment string (NAME=value) to 128 KiB.
MAX_ENV_VALUE = 128 * 1024 - 256

# docker compose global options that take a value.
COMPOSE_VALUE_OPTS = {
    "-f", "--file", "-p", "--project-name", "--profile", "--env-file",
    "--project-directory", "--parallel", "--ansi", "--progress", "--workdir",
}
# docker (not compose) global options we accept and turn into environment.
DOCKER_VALUE_OPTS = {"-c": "DOCKER_CONTEXT", "--context": "DOCKER_CONTEXT",
                     "-H": "DOCKER_HOST", "--host": "DOCKER_HOST"}
DEFAULT_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")

USAGE = """\
usage: compose-inline [OPTIONS] COMMAND [ARGS...]

Runs `docker compose COMMAND ARGS...` with configs and secrets whose `file`
exists on this machine passed by value, so they work on a remote Docker host.

OPTIONS are docker compose global options (-f, -p, --profile, --env-file,
--project-directory, ...), docker's --context/-c and --host/-H, and:
  --bind-files   also pass single-file bind mounts from inside the project
                 directory (e.g. ./nginx.conf:/etc/nginx/nginx.conf) by value
  --print        print the rewritten model (secret values omitted) and exit
  --verbose      list every inlined file on stderr
  --version      print the version and exit

examples:
  DOCKER_HOST=ssh://me@server compose-inline up -d
  compose-inline --context prod -f compose.yaml --bind-files up -d --wait
"""


class Error(Exception):
    pass


def split_args(argv):
    """Split argv into (options dict, compose global args, command args)."""
    opts = {"bind_files": False, "print": False, "verbose": False, "files": [],
            "project_dir": None, "env": {}}
    globals_ = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if not a.startswith("-") or a == "-":
            break
        if a == "--":
            i += 1
            break
        name, eq, val = a.partition("=")
        if name in ("-h", "--help"):
            opts["help"] = True
            return opts, globals_, []
        if name == "--version":
            opts["version"] = True
            return opts, globals_, []
        if name == "--bind-files":
            opts["bind_files"] = True
            i += 1
            continue
        if name == "--print":
            opts["print"] = True
            i += 1
            continue
        if name == "--verbose":
            opts["verbose"] = True
            i += 1
            continue
        if name in DOCKER_VALUE_OPTS or name in COMPOSE_VALUE_OPTS:
            if not eq:
                if i + 1 >= len(argv):
                    raise Error("option %s needs a value" % name)
                val = argv[i + 1]
                i += 2
            else:
                i += 1
            if name in DOCKER_VALUE_OPTS:
                opts["env"][DOCKER_VALUE_OPTS[name]] = val
            elif name in ("-f", "--file"):
                opts["files"].append(val)
                globals_ += ["--file", val]
            elif name == "--project-directory":
                opts["project_dir"] = val
                globals_ += [name, val]
            else:
                globals_ += [name, val]
            continue
        # boolean compose option (--compatibility, --dry-run, --all-resources, ...)
        globals_.append(a)
        i += 1
    return opts, globals_, argv[i:]


def find_project_dir(opts, environ, cwd):
    """The directory docker compose would use as project directory."""
    if opts["project_dir"]:
        return os.path.abspath(opts["project_dir"])
    files = opts["files"]
    if not files and environ.get("COMPOSE_FILE"):
        sep = environ.get("COMPOSE_PATH_SEPARATOR", os.pathsep)
        files = environ["COMPOSE_FILE"].split(sep)
    if files:
        if files[0] == "-":
            return cwd
        return os.path.dirname(os.path.abspath(os.path.join(cwd, files[0])))
    d = cwd
    while True:
        if any(os.path.isfile(os.path.join(d, f)) for f in DEFAULT_FILES):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return cwd
        d = parent


def read_local(path, what):
    """Bytes of a local regular file, or None if it is missing or not a file."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data:
        raise Error("%s: %s contains NUL bytes, which cannot be passed by value" % (what, path))
    return data


def escape_dollars(text):
    # The rewritten model is interpolated again by docker compose.
    return text.replace("$", "$$")


def _refs(service, key):
    out = []
    for ref in service.get(key) or []:
        out.append(ref["source"] if isinstance(ref, dict) else ref)
    return out


def _inside(path, directory):
    path = os.path.realpath(path)
    directory = os.path.realpath(directory)
    return os.path.commonpath([path, directory]) == directory


def rewrite(model, project_dir, bind_files=False):
    """Rewrite a `docker compose config --format json` model in place.

    Returns (env, report): environment variables to pass to docker compose
    (secret and non-UTF-8 config values) and a list of (verbose_only, line).
    """
    env = {}
    report = []
    services = model.get("services") or {}
    used = {"configs": set(), "secrets": set()}
    for svc in services.values():
        for key in used:
            used[key].update(_refs(svc, key))

    digests = {}  # (kind, name) -> sha256 of the passed content
    counter = [0]

    def to_env(data):
        counter[0] += 1
        name = "%s%d" % (ENV_PREFIX, counter[0])
        if len(data) > MAX_ENV_VALUE:
            raise Error("file too large to pass through the environment (%d bytes)" % len(data))
        env[name] = data
        return name

    for kind in ("configs", "secrets"):
        for name, obj in (model.get(kind) or {}).items():
            if name not in used[kind] or obj.get("external") or "file" not in obj:
                continue
            path = obj["file"]
            data = read_local(path, "%s %s" % (kind[:-1], name))
            if data is None:
                report.append((False, "kept %s %s: %s is not a local file (resolved on the Docker host)"
                              % (kind[:-1], name, path)))
                continue
            del obj["file"]
            text = None
            if kind == "configs":
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    pass
            if text is not None:
                obj["content"] = escape_dollars(text)
            else:
                obj["environment"] = to_env(data)
            digests[(kind, name)] = hashlib.sha256(data).hexdigest()
            report.append((True, "inlined %s %s from %s (%d bytes)"
                           % (kind[:-1], name, path, len(data))))

    if bind_files:
        configs = model.setdefault("configs", {})
        for sname, svc in services.items():
            kept = []
            for i, vol in enumerate(svc.get("volumes") or []):
                src = vol.get("source", "") if isinstance(vol, dict) else ""
                if (not isinstance(vol, dict) or vol.get("type") != "bind"
                        or not os.path.isfile(src) or not _inside(src, project_dir)):
                    kept.append(vol)
                    continue
                data = read_local(src, "bind mount of service %s" % sname)
                cname = "compose-inline-%s-%d" % (sname, i)
                try:
                    configs[cname] = {"content": escape_dollars(data.decode("utf-8"))}
                except UnicodeDecodeError:
                    configs[cname] = {"environment": to_env(data)}
                # An integer: older Compose (v2.24.7) parses a mode string as decimal.
                mode = stat.S_IMODE(os.stat(src).st_mode)
                svc.setdefault("configs", []).append(
                    {"source": cname, "target": vol["target"], "mode": mode})
                digests[("configs", cname)] = hashlib.sha256(data).hexdigest()
                report.append((True, "inlined bind mount %s:%s of service %s (%d bytes)"
                               % (src, vol["target"], sname, len(data))))
            if kept:
                svc["volumes"] = kept
            else:
                svc.pop("volumes", None)
        if not configs:
            del model["configs"]

    # Compose does not hash config/secret content into the service, so a
    # changed file would not recreate the container. A label does.
    for svc in services.values():
        parts = sorted("%s/%s=%s" % (k, n, digests[(k, n)])
                       for k in ("configs", "secrets") for n in _refs(svc, k)
                       if (k, n) in digests)
        if parts:
            labels = svc.get("labels")
            if isinstance(labels, list):
                labels = dict(item.split("=", 1) if "=" in item else (item, "") for item in labels)
            labels = dict(labels or {})
            labels[HASH_LABEL] = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
            svc["labels"] = labels
    return env, report


def docker_compose(environ):
    return [environ.get("COMPOSE_INLINE_DOCKER", "docker"), "compose"]


def load_model(globals_, environ):
    cmd = docker_compose(environ) + globals_ + ["config", "--format", "json"]
    proc = subprocess.run(cmd, env=environ, stdout=subprocess.PIPE)
    if proc.returncode != 0:
        raise Error("`%s` failed with exit code %d" % (" ".join(cmd), proc.returncode))
    return json.loads(proc.stdout)


def final_globals(globals_):
    """Compose global args without -f/--file, -p and --project-directory."""
    out = []
    i = 0
    while i < len(globals_):
        if globals_[i] in ("--file", "-p", "--project-name", "--project-directory"):
            i += 2
            continue
        out.append(globals_[i])
        i += 1
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        opts, globals_, command = split_args(argv)
    except Error as e:
        print("compose-inline: %s" % e, file=sys.stderr)
        return 2
    if opts.get("help"):
        print(USAGE, end="")
        return 0
    if opts.get("version"):
        print("compose-inline %s" % __version__)
        return 0
    if not command and not opts["print"]:
        print(USAGE, end="", file=sys.stderr)
        return 2

    environ = dict(os.environ)
    environ.update(opts["env"])
    try:
        project_dir = find_project_dir(opts, environ, os.getcwd())
        model = load_model(globals_, environ)
        env, report = rewrite(model, project_dir, opts["bind_files"])
    except Error as e:
        print("compose-inline: %s" % e, file=sys.stderr)
        return 1

    for verbose_only, line in report:
        if opts["verbose"] or opts["print"] or not verbose_only:
            print("compose-inline: " + line, file=sys.stderr)
    if opts["print"]:
        json.dump(model, sys.stdout, indent=2)
        print()
        return 0
    if not model.get("name"):
        print("compose-inline: model has no project name", file=sys.stderr)
        return 1

    env_b = {k.encode(): v for k, v in env.items()}
    child_env = {k.encode(): v.encode("utf-8", "surrogateescape") for k, v in environ.items()}
    child_env.update(env_b)
    with tempfile.TemporaryDirectory(prefix="compose-inline-") as tmp:
        path = os.path.join(tmp, "compose.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(model, f)
        cmd = (docker_compose(environ) + final_globals(globals_)
               + ["--project-directory", project_dir, "-p", model["name"], "-f", path]
               + command)
        try:
            return subprocess.call(cmd, env=child_env)
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    sys.exit(main())

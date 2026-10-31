import os
import tempfile
import unittest

import compose_inline as ci


class SplitArgsTest(unittest.TestCase):
    def test_globals_and_command(self):
        opts, g, cmd = ci.split_args(
            ["-f", "a.yml", "--file=b.yml", "-p", "x", "--profile", "dev", "--dry-run",
             "--context", "prod", "--bind-files", "up", "-d", "--wait"])
        self.assertEqual(opts["files"], ["a.yml", "b.yml"])
        self.assertEqual(opts["env"], {"DOCKER_CONTEXT": "prod"})
        self.assertTrue(opts["bind_files"])
        self.assertEqual(g, ["--file", "a.yml", "--file", "b.yml", "-p", "x",
                             "--profile", "dev", "--dry-run"])
        self.assertEqual(cmd, ["up", "-d", "--wait"])

    def test_host_option(self):
        opts, g, cmd = ci.split_args(["-H", "ssh://me@box", "ps"])
        self.assertEqual(opts["env"], {"DOCKER_HOST": "ssh://me@box"})
        self.assertEqual((g, cmd), ([], ["ps"]))

    def test_missing_value(self):
        with self.assertRaises(ci.Error):
            ci.split_args(["-f"])

    def test_final_globals_drops_file_project_and_dir(self):
        g = ["--file", "a.yml", "-p", "x", "--project-directory", "d", "--profile", "dev"]
        self.assertEqual(ci.final_globals(g), ["--profile", "dev"])


class ProjectDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def opts(self, **kw):
        o = {"project_dir": None, "files": []}
        o.update(kw)
        return o

    def test_explicit(self):
        self.assertEqual(ci.find_project_dir(self.opts(project_dir=self.root), {}, "/"), self.root)

    def test_first_file(self):
        d = ci.find_project_dir(self.opts(files=["sub/compose.yml", "x.yml"]), {}, self.root)
        self.assertEqual(d, os.path.join(self.root, "sub"))

    def test_compose_file_env(self):
        env = {"COMPOSE_FILE": "deploy/a.yml:b.yml"}
        self.assertEqual(ci.find_project_dir(self.opts(), env, self.root),
                         os.path.join(self.root, "deploy"))

    def test_search_upwards(self):
        open(os.path.join(self.root, "compose.yaml"), "w").close()
        sub = os.path.join(self.root, "a", "b")
        os.makedirs(sub)
        self.assertEqual(ci.find_project_dir(self.opts(), {}, sub), self.root)


class RewriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data, mode=0o644):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, mode)
        return path

    def model(self, **services):
        return {"name": "p", "services": services or {"web": {"image": "busybox"}}}

    def test_config_file_becomes_content_with_escaped_dollars(self):
        path = self.write("app.conf", b"price: $5 ${X}\n")
        m = self.model(web={"image": "busybox", "configs": [{"source": "app"}]})
        m["configs"] = {"app": {"name": "p_app", "file": path}}
        env, report = ci.rewrite(m, self.dir)
        self.assertEqual(m["configs"]["app"], {"name": "p_app", "content": "price: $$5 $${X}\n"})
        self.assertEqual(env, {})
        self.assertIn(ci.HASH_LABEL, m["services"]["web"]["labels"])

    def test_secret_file_goes_through_environment(self):
        path = self.write("pw", b"hunter2")
        m = self.model(web={"image": "busybox", "secrets": [{"source": "pw", "target": "/run/secrets/pw"}]})
        m["secrets"] = {"pw": {"name": "p_pw", "file": path}}
        env, _ = ci.rewrite(m, self.dir)
        var = m["secrets"]["pw"]["environment"]
        self.assertTrue(var.startswith(ci.ENV_PREFIX))
        self.assertEqual(env[var], b"hunter2")
        self.assertNotIn("file", m["secrets"]["pw"])
        self.assertNotIn("hunter2", repr(m))

    def test_non_utf8_config_goes_through_environment(self):
        path = self.write("latin1.conf", "caf\xe9".encode("latin-1"))
        m = self.model(web={"image": "busybox", "configs": ["c"]})
        m["configs"] = {"c": {"file": path}}
        env, _ = ci.rewrite(m, self.dir)
        self.assertEqual(env[m["configs"]["c"]["environment"]], b"caf\xe9")

    def test_nul_bytes_are_rejected(self):
        path = self.write("bin", b"a\0b")
        m = self.model(web={"image": "busybox", "secrets": ["s"]})
        m["secrets"] = {"s": {"file": path}}
        with self.assertRaises(ci.Error):
            ci.rewrite(m, self.dir)

    def test_missing_file_unused_and_external_are_kept(self):
        path = self.write("x", b"x")
        m = self.model(web={"image": "busybox", "configs": ["remote", "ext"]})
        m["configs"] = {
            "remote": {"file": "/only/on/the/docker/host"},
            "ext": {"external": True, "name": "ext"},
            "unused": {"file": path},
        }
        env, report = ci.rewrite(m, self.dir)
        self.assertEqual(m["configs"]["remote"], {"file": "/only/on/the/docker/host"})
        self.assertEqual(m["configs"]["unused"], {"file": path})
        self.assertEqual(env, {})
        self.assertEqual([v for v, _ in report], [False])
        self.assertNotIn("labels", m["services"]["web"])

    def test_directory_is_kept(self):
        m = self.model(web={"image": "busybox", "configs": ["d"]})
        m["configs"] = {"d": {"file": self.dir}}
        ci.rewrite(m, self.dir)
        self.assertEqual(m["configs"]["d"], {"file": self.dir})

    def test_large_secret_is_rejected(self):
        path = self.write("big", b"x" * (ci.MAX_ENV_VALUE + 1))
        m = self.model(web={"image": "busybox", "secrets": ["s"]})
        m["secrets"] = {"s": {"file": path}}
        with self.assertRaises(ci.Error):
            ci.rewrite(m, self.dir)

    def test_bind_files(self):
        conf = self.write("nginx.conf", b"worker_processes 1;\n", 0o640)
        outside = os.path.realpath(__file__)
        vols = [
            {"type": "bind", "source": conf, "target": "/etc/nginx/nginx.conf", "read_only": True},
            {"type": "bind", "source": self.dir, "target": "/data"},
            {"type": "bind", "source": outside, "target": "/outside"},
            {"type": "volume", "source": "db", "target": "/db"},
        ]
        m = self.model(web={"image": "nginx", "volumes": vols})
        ci.rewrite(m, self.dir, bind_files=True)
        svc = m["services"]["web"]
        self.assertEqual([v["target"] for v in svc["volumes"]], ["/data", "/outside", "/db"])
        self.assertEqual(svc["configs"], [{"source": "compose-inline-web-0",
                                           "target": "/etc/nginx/nginx.conf", "mode": 0o640}])
        self.assertEqual(m["configs"]["compose-inline-web-0"], {"content": "worker_processes 1;\n"})

    def test_bind_files_off_by_default(self):
        conf = self.write("a.conf", b"a")
        m = self.model(web={"image": "x", "volumes": [{"type": "bind", "source": conf, "target": "/a"}]})
        ci.rewrite(m, self.dir)
        self.assertEqual(len(m["services"]["web"]["volumes"]), 1)
        self.assertNotIn("configs", m)

    def test_hash_label_changes_with_content_and_keeps_labels(self):
        path = self.write("app.conf", b"one")

        def run(labels):
            m = self.model(web={"image": "x", "configs": ["app"], "labels": labels},
                           other={"image": "x"})
            m["configs"] = {"app": {"file": path}}
            ci.rewrite(m, self.dir)
            return m["services"]

        s1 = run({"keep": "me"})
        self.assertEqual(s1["web"]["labels"]["keep"], "me")
        self.assertNotIn("labels", s1["other"])
        self.write("app.conf", b"two")
        s2 = run(["keep=me"])
        self.assertEqual(s2["web"]["labels"]["keep"], "me")
        self.assertNotEqual(s1["web"]["labels"][ci.HASH_LABEL], s2["web"]["labels"][ci.HASH_LABEL])


if __name__ == "__main__":
    unittest.main()

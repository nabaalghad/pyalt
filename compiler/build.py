"""C toolchain discovery and invocation for pyalt.

Preference order: gcc / clang on PATH, then cl.exe on PATH, then MSVC located
via vswhere + vcvars64.bat. The (slow) vcvars environment is captured once and
cached to a JSON file so later compiles are fast.
"""

import json
import os
import shutil
import subprocess

VSWHERE = r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"


class Toolchain:
    def __init__(self, kind, cc, env=None):
        self.kind = kind          # 'gcc', 'clang', or 'msvc'
        self.cc = cc              # path to the compiler executable
        self.env = env            # env dict for msvc; None = inherit

    def describe(self):
        return f"{self.kind} ({self.cc})"

    def compile(self, c_path, exe_path, include_dir):
        if self.kind == "msvc":
            obj_path = os.path.splitext(exe_path)[0] + ".obj"
            cmd = [self.cc, "/nologo", "/O2", f"/I{include_dir}", c_path,
                   f"/Fe:{exe_path}", f"/Fo:{obj_path}"]
            env = self.env
        else:
            cmd = [self.cc, "-O2", "-std=c11", "-I", include_dir, c_path,
                   "-o", exe_path, "-lm"]
            env = None
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        ok = proc.returncode == 0 and os.path.exists(exe_path)
        return ok, (proc.stdout or "") + (proc.stderr or "")

    def compile_pyd(self, c_path, pyd_path, include_dir):
        """Compile a generated extension C file into a Python .pyd/.so for the
        currently-running Python."""
        import sysconfig
        py_inc = sysconfig.get_paths()["include"]
        py_libs = os.path.join(sysconfig.get_config_var("installed_base") or "",
                               "libs")
        if self.kind == "msvc":
            obj_path = os.path.splitext(pyd_path)[0] + ".obj"
            # /MT (static CRT): the .pyd carries its runtime, so importing it
            # never depends on a VC redistributable being present (CI runners
            # hit "DLL load failed" with /MD)
            cmd = [self.cc, "/nologo", "/O2", "/MT", f"/I{include_dir}",
                   f"/I{py_inc}", c_path, "/LD", f"/Fe:{pyd_path}",
                   f"/Fo:{obj_path}", "/link", f"/LIBPATH:{py_libs}"]
            env = self.env
        else:
            # gcc/clang. Linking libpython is platform-specific:
            #   Windows (MinGW): REQUIRED — extensions must link python3XX.dll
            #   Linux: forbidden-by-convention — symbols resolve at import
            #   macOS: undefined symbols allowed via dynamic_lookup
            import sys as _sys
            cmd = [self.cc, "-O2", "-std=c11", "-shared", "-fPIC",
                   "-I", include_dir, "-I", py_inc, c_path, "-o", pyd_path]
            if os.name == "nt":
                pylib = f"python{_sys.version_info.major}{_sys.version_info.minor}"
                # -static-libgcc etc.: Python's importer won't search PATH for
                # dependent DLLs, so the MinGW runtime must live inside the .pyd
                cmd += [f"-L{py_libs}", f"-l{pylib}", "-static-libgcc",
                        "-Wl,-Bstatic,-lwinpthread,-Bdynamic"]
            elif _sys.platform == "darwin":
                cmd += ["-undefined", "dynamic_lookup"]
            cmd += ["-lm"]
            env = None
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        ok = proc.returncode == 0 and os.path.exists(pyd_path)
        return ok, (proc.stdout or "") + (proc.stderr or "")


def _env_path(env):
    """Case-insensitive PATH lookup (Windows env vars use e.g. 'Path')."""
    for key, value in env.items():
        if key.upper() == "PATH":
            return value
    return ""


def _find_vcvars():
    if not os.path.exists(VSWHERE):
        return None
    try:
        proc = subprocess.run(
            [VSWHERE, "-latest", "-products", "*", "-requires",
             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            capture_output=True, text=True, timeout=30)
    except OSError:
        return None
    path = proc.stdout.strip().splitlines()
    if not path:
        return None
    vcvars = os.path.join(path[0], "VC", "Auxiliary", "Build", "vcvars64.bat")
    return vcvars if os.path.exists(vcvars) else None


def _msvc_env(vcvars, cache_path):
    """Capture the environment vcvars64.bat sets up; cache it (it's slow)."""
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if (cached.get("vcvars") == vcvars
                    and cached.get("mtime") == os.path.getmtime(vcvars)
                    and shutil.which("cl", path=_env_path(cached["env"]))):
                return cached["env"]
        except (OSError, ValueError, KeyError):
            pass
    proc = subprocess.run(
        f'cmd /s /c ""{vcvars}" >nul 2>&1 && set"',
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return None
    env = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            env[key] = value
    if not shutil.which("cl", path=_env_path(env)):
        return None
    if cache_path:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump({"vcvars": vcvars,
                           "mtime": os.path.getmtime(vcvars),
                           "env": env}, fh)
        except OSError:
            pass
    return env


def find_toolchain(cache_dir=None):
    for name, kind in (("gcc", "gcc"), ("clang", "clang")):
        cc = shutil.which(name)
        if cc:
            return Toolchain(kind, cc)
    cc = shutil.which("cl")
    if cc:
        return Toolchain("msvc", cc, env=dict(os.environ))
    vcvars = _find_vcvars()
    if vcvars:
        cache_path = os.path.join(cache_dir, ".msvc_env.json") if cache_dir else None
        env = _msvc_env(vcvars, cache_path)
        if env:
            cc = shutil.which("cl", path=_env_path(env))
            return Toolchain("msvc", cc, env=env)
    return None

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from sys import platform
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Set, Tuple

from statx import stat, stat_result

from .utils import ROOT, escape_attr, info, sh, warn


@dataclass
class Attr:
    name: str
    exists: bool
    broken: bool
    blacklisted: bool
    skipped: bool
    path: Optional[str]
    drv_path: Optional[str]
    log_url: Optional[str] = field(default=None)
    check_report: List[str] = field(default_factory=lambda: [])
    aliases: List[str] = field(default_factory=lambda: [])
    _path_verified: Optional[bool] = field(init=False, default=None)
    _err: Optional[str] = field(init=False, default=None)

    def was_build(self) -> bool:
        if self.path is None:
            return False

        if self._path_verified is not None:
            return self._path_verified

        res = subprocess.run(
            ["nix-store", "--verify-path", self.path], stderr=subprocess.DEVNULL
        )
        self._path_verified = res.returncode == 0
        return self._path_verified

    def is_test(self) -> bool:
        return self.name.startswith("nixosTests")

    def log(self) -> Optional[str]:
        def get_log(path) -> str:
            system = subprocess.run(
                ["nix", "--experimental-features", "nix-command", "log", path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return system.stdout

        if self.drv_path is None:
            return None

        if not self.was_build() and self._err is not None:
            return self._err

        return get_log(self.drv_path) or get_log(self.path)

    def log_path(self) -> Optional[str]:
        if self.drv_path is None:
            return None
        base = os.path.basename(self.drv_path)
        prefix = "/nix/var/log/nix/drvs/"
        full = os.path.join(prefix, base[:2], base[2:] + ".bz2")
        if os.path.exists(full):
            return full
        return None

    def build_time(self) -> Optional[timedelta]:
        log_path = self.log_path()
        if log_path is None:
            return None
        result = stat(log_path)
        assert isinstance(result, stat_result)
        return timedelta(
            microseconds=(result.st_mtime_ns - result.st_birthtime_ns) / 1000
        )


def nix_shell(attrs: List[str], cache_directory: Path) -> None:
    shell = cache_directory.joinpath("shell.nix")
    write_shell_expression(shell, attrs)
    sh(["nix-shell", str(shell)], cwd=cache_directory, check=False)


def _nix_eval_filter(json: Dict[str, Any]) -> List[Attr]:
    # workaround https://github.com/NixOS/ofborg/issues/269
    blacklist = set(
        [
            "tests.nixos-functions.nixos-test",
            "tests.nixos-functions.nixosTest-test",
            "tests.writers",
            "appimage-run-tests",
        ]
    )
    attr_by_path: Dict[str, Attr] = {}
    broken = []
    for name, props in json.items():
        attr = Attr(
            name=name,
            exists=props["exists"],
            broken=props["broken"],
            blacklisted=name in blacklist,
            skipped=False,
            path=props["path"],
            drv_path=props["drvPath"],
        )
        if attr.path is not None:
            other = attr_by_path.get(attr.path, None)
            if other is None:
                attr_by_path[attr.path] = attr
            else:
                if len(other.name) > len(attr.name):
                    attr_by_path[attr.path] = attr
                    attr.aliases.append(other.name)
                else:
                    other.aliases.append(attr.name)
        else:
            broken.append(attr)
    return list(attr_by_path.values()) + broken


def nix_eval(attrs: Set[str]) -> List[Attr]:
    attr_json = NamedTemporaryFile(mode="w+", delete=False)
    delete = True
    try:
        json.dump(list(attrs), attr_json)
        eval_script = str(ROOT.joinpath("nix/evalAttrs.nix"))
        attr_json.flush()
        cmd = [
            "nix",
            "--experimental-features",
            "nix-command",
            "eval",
            "--json",
            "--impure",
            "--expr",
            f"(import {eval_script} {attr_json.name})",
        ]

        try:
            nix_eval = subprocess.run(
                cmd, check=True, stdout=subprocess.PIPE, text=True
            )
        except subprocess.CalledProcessError:
            warn(
                f"{' '.join(cmd)} failed to run, {attr_json.name} was stored inspection"
            )
            delete = False
            raise

        return _nix_eval_filter(json.loads(nix_eval.stdout))
    finally:
        attr_json.close()
        if delete:
            os.unlink(attr_json.name)


def nix_build_dry(filename: str) -> Tuple[List[str], List[str]]:

    # Turn filename into a drv_path
    proc1 = subprocess.run(
        ["nix-instantiate", filename],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    )

    proc2 = subprocess.run(
        ["nix-store", "--realize", "--dry-run"] + proc1.stdout.splitlines(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    lines = proc2.stderr.splitlines()
    to_fetch = []
    to_build = []
    ignore = []

    for line in lines:
        line = line.strip()
        if "this path will be fetched" in line:
            cur = to_fetch
        elif "paths will be fetched" in line:
            cur = to_fetch
        elif "derivations will be built" in line:
            cur = to_build
        elif "derivation will be built" in line:
            cur = to_build
        elif "don't know how to build these paths" in line:
            cur = ignore
        elif line.startswith("/nix/store"):
            cur.append(line)
        elif line != "":
            raise RuntimeError(f"dry-run parsing failed: '{line}'")

    return (to_build, to_fetch)


def nix_build(attr_names: Set[str], args: str, cache_directory: Path) -> List[Attr]:
    if not attr_names:
        info("Nothing to be built.")
        return []

    attrs = nix_eval(attr_names)
    attrs = pre_build_filter(attrs, nixpkgs=cache_directory / "nixpkgs")
    filtered = []
    for attr in attrs:
        if not (attr.broken or attr.blacklisted or attr.skipped):
            filtered.append(attr.name)

    if len(filtered) == 0:
        return attrs

    build = cache_directory.joinpath("build.nix")
    write_shell_expression(build, filtered)
    drvs_to_build, _drvs_to_fetch = nix_build_dry(build)

    command = [
        "nix",
        "--experimental-features",
        "nix-command",
        "build",
        "--no-link",
        "--keep-going",
    ]

    if platform == "linux":
        command += [
            # only matters for single-user nix and trusted users
            "--option",
            "build-use-sandbox",
            "relaxed",
        ]

    command += [
        "-f",
        str(build),
    ] + shlex.split(args)

    try:
        proc = sh(command, stderr=subprocess.PIPE)
        stderr = proc.stderr
    except subprocess.CalledProcessError as e:
        stderr = e.stderr

    has_failed_dependencies = []
    for line in stderr.splitlines():
        if "dependencies couldn't be built" in line:
            has_failed_dependencies.append(
                next(item for item in line.split() if "/nix/store" in item)
                .lstrip("'")
                .rstrip(":'")
            )

    drv_path_to_attr = {a.drv_path: a for a in attrs}
    for drv_path in has_failed_dependencies:
        if drv_path in drv_path_to_attr:
            attr = drv_path_to_attr[drv_path]
            attr._err = stderr

    attrs = postprocess(attrs, drvs_to_build, nixpkgs=cache_directory / "nixpkgs")
    return attrs


def pre_build_filter(attrs: List[Attr], nixpkgs: Path) -> List[Attr]:
    for cmd in (
        cmd
        for cmd in os.environ.get("NIXPKGS_REVIEW_PRE_BUILD_FILTER", "").split(":")
        if cmd
    ):
        encoded = json.dumps(
            {
                "attrs": [attr.__dict__ for attr in attrs],
            }
        )
        p = sh([cmd], input=encoded, stdout=subprocess.PIPE, cwd=nixpkgs)
        attrs = [Attr(**arg) for arg in json.loads(p.stdout)]
    return attrs


def postprocess(
    attrs: List[Attr], drvpaths_built: List[str], nixpkgs: Path
) -> List[Attr]:
    """Run the build attributes through nixpkgs-review-checks"""
    for cmd in (
        cmd for cmd in os.environ.get("NIXPKGS_REVIEW_CHECKS", "").split(":") if cmd
    ):
        encoded = json.dumps(
            {
                "attrs": [attr.__dict__ for attr in attrs],
                "drvpaths_built": drvpaths_built,
            }
        )
        p = sh([cmd], input=encoded, stdout=subprocess.PIPE, cwd=nixpkgs)
        attrs = [Attr(**arg) for arg in json.loads(p.stdout)]
    return attrs


def write_shell_expression(filename: Path, attrs: List[str]) -> None:
    with open(filename, "w+") as f:
        f.write(
            """{ pkgs ? import ./nixpkgs {} }:
with pkgs;
let
  paths = [
"""
        )
        f.write("\n".join(f"        {escape_attr(a)}" for a in attrs))
        f.write(
            """
  ];
  env = buildEnv {
    name = "env";
    inherit paths;
    ignoreCollisions = true;
  };
in stdenv.mkDerivation rec {
  name = "review-shell";
  buildInputs = if builtins.length paths > 50 then [ env ] else paths;
  unpackPhase = ":";
  installPhase = "touch $out";
}
"""
        )

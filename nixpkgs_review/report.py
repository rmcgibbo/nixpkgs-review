import os
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Set

from .nix import Attr
from .utils import info, link, warn


def print_number(
    packages: List[Attr],
    msg: str,
    what: str = "package",
    log: Callable[[str], None] = warn,
) -> None:
    if len(packages) == 0:
        return
    plural = "s" if len(packages) > 1 else ""
    names = (a.name for a in packages)
    log(f"{len(packages)} {what}{plural} {msg}:")
    log(" ".join(names))
    log("")


def html_pkgs_section(packages: List[Attr], msg: str, what: str = "package") -> str:
    if len(packages) == 0:
        return ""
    plural = "s" if len(packages) > 1 else ""
    res = "<details>\n"
    res += f"  <summary>{len(packages)} {what}{plural} {msg}:</summary>\n  <ul>\n"
    for pkg in packages:
        if pkg.log_url is not None:
            res += f"    <li><a href=\"{pkg.log_url}\">{pkg.name}</a></li>"
        else:
            res += f"    <li>{pkg.name}"
        if len(pkg.aliases) > 0:
            res += f" ({' ,'.join(pkg.aliases)})"
        res += "</li>\n"
    res += "  </ul>\n</details>\n"
    return res


def html_check_reports(check_reports: List[str]) -> str:
    if len(check_reports) == 0:
        return ""
    plural = "s" if len(check_reports) > 1 else ""
    res = "<details>\n"
    res += f"  <summary>{len(check_reports)} suggestion{plural}:</summary>\n  <ul>\n"
    for report in check_reports:
        res += f"    <li>{report}"
        res += "</li>\n"
    res += "  </ul>\n</details>\n"
    return res


class LazyDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.created = False

    def ensure(self) -> Path:
        if not self.created:
            self.path.mkdir(exist_ok=True)
            self.created = True
        return self.path


class Report:
    def __init__(self, system: str, attrs: List[Attr], pr_rev: Optional[str] = None) -> None:
        self.system = system
        self.attrs = attrs
        self.pr_rev: Optional[str] = pr_rev
        self.broken: List[Attr] = []
        self.failed: List[Attr] = []
        self.non_existant: List[Attr] = []
        self.blacklisted: List[Attr] = []
        self.tests: List[Attr] = []
        self.built: List[Attr] = []
        self.check_reports: Set[str] = set()

        for a in attrs:
            if a.broken:
                self.broken.append(a)
            elif a.blacklisted:
                self.blacklisted.append(a)
            elif not a.exists:
                self.non_existant.append(a)
            elif a.name.startswith("nixosTests."):
                self.tests.append(a)
            elif not a.was_build():
                self.failed.append(a)
            else:
                self.built.append(a)

        for a in attrs:
            self.check_reports.update(a.check_report)

    def built_packages(self) -> List[str]:
        return [a.name for a in self.built]

    def write(self, directory: Path, pr: Optional[int]) -> None:
        with open(directory.joinpath("report.md"), "w+") as f:
            f.write(self.markdown(pr))

    def succeeded(self) -> bool:
        """Whether the report is considered a success or a failure"""
        return len(self.failed) == 0

    def markdown(self, pr: Optional[int]) -> str:
        cmd = "nixpkgs-review"
        if pr is not None:
            cmd += f" pr {pr}"

        shortcommit = f" at {self.pr_rev[:8]}" if self.pr_rev else ""
        link = "[1](https://github.com/Mic92/nixpkgs-review)"
        msg = f"Result of `{cmd}`{shortcommit} run on {self.system} {link}\n"

        msg += html_pkgs_section(self.broken, "marked as broken and skipped")
        msg += html_pkgs_section(
            self.non_existant,
            "present in ofBorgs evaluation, but not found in the checkout",
        )
        msg += html_pkgs_section(self.failed, "failed to build")
        msg += html_pkgs_section(self.tests, "built", what="test")
        msg += html_pkgs_section(self.built, "built")
        msg += html_check_reports(sorted(self.check_reports))

        return msg

    def print_console(self, pr: Optional[int]) -> None:
        if pr is not None:
            pr_url = f"https://github.com/NixOS/nixpkgs/pull/{pr}"
            info("\nLink to currently reviewing PR:")
            link(f"\u001b]8;;{pr_url}\u001b\\{pr_url}\u001b]8;;\u001b\\\n")
        print_number(self.broken, "marked as broken and skipped")
        print_number(
            self.non_existant,
            "present in ofBorgs evaluation, but not found in the checkout",
        )
        print_number(self.blacklisted, "blacklisted")
        print_number(self.failed, "failed to build")
        print_number(self.tests, "built", what="tests", log=print)
        print_number(self.built, "built", log=print)

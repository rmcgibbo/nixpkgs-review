import os
import subprocess
from itertools import islice
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .nix import Attr
from .utils import info, link, warn
from .hydracheck import fetch_hydra_build_status


def print_number(
    packages: List[Attr],
    msg: str,
    what: str = "package",
    log: Callable[[str], None] = warn,
    show: int = -1,
) -> None:
    if len(packages) == 0:
        return
    plural = "s" if len(packages) > 1 else ""
    names = (a.name for a in packages)
    colon = ":" if show else ""
    log(f"{len(packages)} {what}{plural} {msg}:")
    if show == -1 or show > len(packages):
        log(" ".join(names))
    else:
        log(" ".join(islice(names, show)) + " ...")
    log("")


def html_pkgs_section(
    packages: List[Attr],
    msg: str,
    what: str = "package",
    show: int = -1
) -> str:
    if len(packages) == 0:
        return ""
    plural = "s" if len(packages) > 1 else ""

    res = "<details>\n"
    res += f"  <summary>{len(packages)} {what}{plural} {msg}:</summary>\n  <ul>\n"
    for i, pkg in enumerate(packages):
        if show > 0 and i >= show:
            res += f"    <li>...</li>\n"
            break

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
    def __init__(self, system: str, attrs: List[Attr], pr_data: Optional[Dict] = None) -> None:
        self.system = system
        self.attrs = attrs
        self.pr_data = pr_data
        self.skipped: List[attr] = []
        self.broken: List[Attr] = []
        self.timed_out: List[Attr] = []
        failed: List[Attr] = []
        self.failed_new: List[Attr] = []
        self.failed_existing: List[Attr] = []
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
            elif a.skipped:
                self.skipped.append(a)
            elif a.timed_out:
                self.timed_out.append(a)
            elif not a.exists:
                self.non_existant.append(a)
            elif a.name.startswith("nixosTests."):
                self.tests.append(a)
            elif not a.was_build():
                failed.append(a)
            else:
                self.built.append(a)

        self.failed_new, self.failed_existing = self.get_hydra_build_status(failed, system)

        for a in attrs:
            self.check_reports.update(a.check_report)

    def get_hydra_build_status(
        self,
        failed: List[Attr],
        system: str
    ) -> Tuple[List[Attr], List[Attr]]:

        if self.pr_data is None:
            channel = "unstable"
        else:
            channel = self.pr_data["base"]["ref"]

        reports = fetch_hydra_build_status(failed, system=system, channel=channel)

        failed_new = []
        failed_existing = []
        name2attr = {a.name: a for a in failed}
        for name, report in reports.items():
            if not report["success"]:
                failed_existing.append(name2attr.pop(name))
        for name, attr in name2attr.items():
            failed_new.append(attr)
        return failed_new, failed_existing

    def built_packages(self) -> List[str]:
        return [a.name for a in self.built]

    def write(self, directory: Path) -> None:
        with open(directory.joinpath("report.md"), "w+") as f:
            f.write(self.markdown())

    def succeeded(self) -> bool:
        """Whether the report is considered a success or a failure"""
        return len(self.failed_new) == 0 and len(self.failed_existing) == 0

    def markdown(self) -> str:
        cmd = "nixpkgs-review"
        shortcommit = ""
        if self.pr_data is not None:
            cmd += f" pr {self.pr_data['number']}"
            shortcommit = f" at {self.pr_data['head']['sha'][:8]}"

        link = "[1](https://github.com/Mic92/nixpkgs-review)"
        msg = f"Result of `{cmd}`{shortcommit} run on {self.system} {link}\n"

        msg += html_pkgs_section(self.broken, "marked as broken and skipped", show=10)
        msg += html_pkgs_section(
            self.non_existant,
            "present in ofBorgs evaluation, but not found in the checkout",
        )
        msg += html_pkgs_section(self.failed_new, "failed to build (new)")
        msg += html_pkgs_section(self.failed_existing, "failed to build (existing failures)")
        msg += html_pkgs_section(self.skipped, "skipped due to time constraints", show=10)
        msg += html_pkgs_section(self.timed_out, "timed out")
        msg += html_pkgs_section(self.tests, "built", what="test")
        msg += html_pkgs_section(self.built, "built")
        msg += html_check_reports(sorted(self.check_reports))

        return msg

    def print_console(self) -> None:
        if self.pr_data is not None:
            pr_url = self.pr_data["_links"]["html"]["href"]
            info("\nLink to currently reviewing PR:")
            link(f"\u001b]8;;{pr_url}\u001b\\{pr_url}\u001b]8;;\u001b\\\n")

        print_number(self.broken, "marked as broken and skipped", show=10)
        print_number(
            self.non_existant,
            "present in ofBorgs evaluation, but not found in the checkout",
        )
        print_number(self.blacklisted, "blacklisted")
        print_number(self.skipped, "skipped due to time constraints", show=10)
        print_number(self.timed_out, "timed out", show=True)
        print_number(self.failed_new, "failed to build (new failures)")
        print_number(self.failed_existing, "failed to build (existing failures)")
        print_number(self.tests, "built", what="tests", log=print)
        print_number(self.built, "built", log=print)

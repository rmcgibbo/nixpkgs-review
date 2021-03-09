# MIT License

# Copyright (c) 2019 Felix Richter

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import concurrent.futures
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterator, Tuple, Union

from bs4 import BeautifulSoup

from .nix import Attr

BuildStatus = Dict[str, Union[str, bool]]

__all__ = ["fetch_hydra_build_status"]


def fetch_hydra_build_status(attrs: List[Attr], system: str, channel: str = "unstable") -> Dict[str, BuildStatus]:
    jobset = _guess_jobset(channel)

    response_bodies = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = (executor.submit(_fetch_data, attr.name, jobset, system) for attr in attrs)

        for future in concurrent.futures.as_completed(future_to_url):
            try:
                data = future.result()
            except Exception as exc:
                data = None
            finally:
                response_bodies.append(data)

    parsed_responses = {}
    for attr_name__data in response_bodies:
        if attr_name__data is None:
            continue
        attr_name, data = attr_name__data
        parsed_responses[attr_name] = _parse_build_html(data)

    return parsed_responses


def _parse_build_html(data: str) -> BuildStatus:
    doc = BeautifulSoup(data, features="html.parser")
    if not doc.find("tbody"):
        # Either the package was not evaluated (due to being unfree)
        # or the package does not exist
        alert_text = (
            doc.find("div", {"class": "alert"}).text.replace("\n", " ")
            or "Unknown Hydra Error, check the package with --url to find out what went wrong"
        )
        return {"success": False, "status": alert_text}

    for row in doc.find("tbody").find_all("tr"):
        try:
            status, build, timestamp, name, arch = row.find_all("td")
        except ValueError:
            if row.find("td").find("a")["href"].endswith("/all"):
                continue
            else:
                raise
        status = status.find("img")["title"]
        build_id = build.find("a").text
        build_url = build.find("a")["href"]
        timestamp = timestamp.find("time")["datetime"]
        name = name.text
        arch = arch.find("tt").text
        success = status == "Succeeded"
        return {
            "success": success,
            "status": status,
            "timestamp": timestamp,
            "build_id": build_id,
            "build_url": build_url,
            "name": name,
            "arch": arch,
        }

    raise RuntimeError()


def _fetch_data(attr_name: str, jobset: str, system: str) -> Tuple[str, str]:
    ident = f"{jobset}/nixpkgs.{attr_name}.{system}"
    url = f"https://hydra.nixos.org/job/{ident}"

    try:
        resp = urllib.request.urlopen(url, timeout=20)
        if resp.status != 200:
            return None
        return (attr_name, resp.read())
    except urllib.error.HTTPError:
        return None


def _guess_jobset(channel: str) -> str:
    # TODO guess the latest stable channel
    if channel == "master":
        return "nixpkgs/trunk"
    elif channel == "unstable":
        return "nixos/trunk-combined"
    elif channel == "staging":
        return "nixos/staging"
    elif channel[0].isdigit():
        # 19.09, 20.03 etc
        return f"nixos/release-{channel}"
    else:
        # we asume that the user knows the jobset name ( nixos/release-19.09 )
        return channel


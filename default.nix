{ pkgs ?  import <nixpkgs> {} }:

with pkgs;
let
  statx = import (pkgs.fetchFromGitHub {
    owner = "rmcgibbo";
    repo = "statx";
    rev = "ba90b5dd37fb1f5f01465015564e0a0aeb2cb5c3";
    sha256 = "0b0jrvas4rk4qvqn0pmw1v1ykzid6pzacrqmwkpn52azvmf904sr";
  }) { pkgs = pkgs; pythonPackages = python3.pkgs; };
in
python3.pkgs.buildPythonApplication rec {
  name = "nixpkgs-review";
  src = ./.;
  buildInputs = [ makeWrapper ];
  
  propagatedBuildInputs = [
    statx
    python3.pkgs.humanize
    # humanize fails to declare its dependency on septools correctly
    # https://github.com/NixOS/nixpkgs/pull/113060
    python3.pkgs.setuptools
    python3.pkgs.beautifulsoup4
  ];
  checkInputs = [
    mypy
    python3.pkgs.black
    python3.pkgs.flake8
    python3.pkgs.pytest
    glibcLocales
  ];

  doCheck = false;
  checkPhase = ''
    echo -e "\x1b[32m## run unittest\x1b[0m"
    py.test .
    ${if pkgs.lib.versionAtLeast python3.pkgs.black.version "20" then ''
      echo -e "\x1b[32m## run black\x1b[0m"
      LC_ALL=en_US.utf-8 black --check .
    '' else ''
      echo -e "\033[0;31mskip running black (version too old)\x1b[0m"
    ''}
    echo -e "\x1b[32m## run flake8\x1b[0m"
    flake8 nixpkgs_review
    echo -e "\x1b[32m## run mypy\x1b[0m"
    mypy --strict nixpkgs_review
  '';
  makeWrapperArgs = [
    "--prefix PATH : ${stdenv.lib.makeBinPath [ nixFlakes git curl gnutar gzip ]}"
    "--set NIX_SSL_CERT_FILE ${cacert}/etc/ssl/certs/ca-bundle.crt"
  ];
  shellHook = ''
    # workaround because `python setup.py develop` breaks for me
  '';

  passthru.env = buildEnv { inherit name; paths = buildInputs ++ checkInputs; };
}

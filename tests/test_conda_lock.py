import contextlib
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from glob import glob
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urldefrag, urlsplit

import pytest

from conda_lock.conda_lock import (
    DEFAULT_FILES,
    DEFAULT_LOCKFILE_NAME,
    _add_auth_to_line,
    _add_auth_to_lockfile,
    _extract_domain,
    _strip_auth_from_line,
    _strip_auth_from_lockfile,
    aggregate_lock_specs,
    create_lockfile_from_spec,
    default_virtual_package_repodata,
    determine_conda_executable,
    extract_input_hash,
    main,
    make_lock_spec,
    parse_meta_yaml_file,
    run_lock,
)
from conda_lock.conda_solver import fake_conda_environment
from conda_lock.errors import PlatformValidationError
from conda_lock.invoke_conda import _ensureconda, is_micromamba, reset_conda_pkgs_dir
from conda_lock.pypi_solver import parse_pip_requirement, solve_pypi
from conda_lock.src_parser import (
    LockedDependency,
    LockSpecification,
    VersionedDependency,
)
from conda_lock.src_parser.environment_yaml import parse_environment_file
from conda_lock.src_parser.lockfile import parse_conda_lock_file
from conda_lock.src_parser.pyproject_toml import (
    parse_pyproject_toml,
    poetry_version_to_conda_version,
)


TEST_DIR = pathlib.Path(__file__).parent


@pytest.fixture(autouse=True)
def logging_setup(caplog):
    caplog.set_level(logging.DEBUG)


@pytest.fixture
def reset_global_conda_pkgs_dir():
    reset_conda_pkgs_dir()


@pytest.fixture
def gdal_environment():
    return TEST_DIR.joinpath("gdal").joinpath("environment.yml")


@pytest.fixture
def pip_environment():
    return TEST_DIR.joinpath("test-pypi-resolve").joinpath("environment.yml")


@pytest.fixture
def pip_local_package_environment():
    return TEST_DIR.joinpath("test-local-pip").joinpath("environment.yml")


@pytest.fixture
def zlib_environment():
    return TEST_DIR.joinpath("zlib").joinpath("environment.yml")


@pytest.fixture
def input_hash_zlib_environment():
    return (
        pathlib.Path(__file__)
        .parent.joinpath("test-input-hash-zlib")
        .joinpath("environment.yml")
    )


@pytest.fixture
def meta_yaml_environment():
    return TEST_DIR.joinpath("test-recipe").joinpath("meta.yaml")


@pytest.fixture
def poetry_pyproject_toml():
    return TEST_DIR.joinpath("test-poetry").joinpath("pyproject.toml")


@pytest.fixture
def flit_pyproject_toml():
    return TEST_DIR.joinpath("test-flit").joinpath("pyproject.toml")


@pytest.fixture(
    scope="function",
    params=[
        pytest.param(True, id="--dev-dependencies"),
        pytest.param(False, id="--no-dev-dependencies"),
    ],
)
def include_dev_dependencies(request: Any) -> bool:
    return request.param


def test_parse_environment_file(gdal_environment):
    res = parse_environment_file(gdal_environment, "linux-64")
    assert all(
        x in res.dependencies
        for x in [
            VersionedDependency(
                name="python",
                version=">=3.7,<3.8",
            ),
            VersionedDependency(
                name="gdal",
                version="",
            ),
        ]
    )
    assert (
        VersionedDependency(
            name="toolz",
            manager="pip",
            version="*",
        )
        in res.dependencies
    )
    assert all(x in res.channels for x in ["conda-forge", "defaults"])


def test_parse_environment_file_with_pip(pip_environment):
    res = parse_environment_file(pip_environment, "linux-64")
    assert [dep for dep in res.dependencies if dep.manager == "pip"] == [
        VersionedDependency(
            name="requests-toolbelt",
            manager="pip",
            optional=False,
            category="main",
            extras=[],
            version="=0.9.1",
        )
    ]


def test_choose_wheel() -> None:

    solution = solve_pypi(
        {
            "fastavro": VersionedDependency(
                name="fastavro",
                manager="pip",
                optional=False,
                category="main",
                extras=[],
                version="1.4.7",
            )
        },
        use_latest=[],
        pip_locked={},
        conda_locked={
            "python": LockedDependency.parse_obj(
                {
                    "name": "python",
                    "version": "3.9.7",
                    "manager": "conda",
                    "platform": "linux-64",
                    "dependencies": {},
                    "url": "",
                    "hash": "md5:deadbeef",
                }
            )
        },
        python_version="3.9.7",
        platform="linux-64",
    )
    assert len(solution) == 1
    assert (
        solution["fastavro"].hash
        == "sha256:a111a384a786b7f1fd6a8a8307da07ccf4d4c425084e2d61bae33ecfb60de405"
    )


@pytest.mark.parametrize(
    "requirement, parsed",
    [
        (
            "package-thingie1[foo]",
            {
                "name": "package-thingie1",
                "constraint": None,
                "extras": "foo",
                "url": None,
            },
        ),
        (
            "package[extra] @ https://foo.bar/package.whl#sha1=blerp",
            {
                "name": "package",
                "constraint": None,
                "extras": "extra",
                "url": "https://foo.bar/package.whl#sha1=blerp",
            },
        ),
        (
            "package[extra] = 2.1",
            {
                "name": "package",
                "constraint": "= 2.1",
                "extras": "extra",
                "url": None,
            },
        ),
        (
            "package[extra] == 2.1",
            {
                "name": "package",
                "constraint": "== 2.1",
                "extras": "extra",
                "url": None,
            },
        ),
        (
            "package[extra]===2.1",
            {
                "name": "package",
                "constraint": "===2.1",
                "extras": "extra",
                "url": None,
            },
        ),
        (
            "package[extra] >=2.1.*, <4.0",
            {
                "name": "package",
                "constraint": ">=2.1.*, <4.0",
                "extras": "extra",
                "url": None,
            },
        ),
        (
            "package[extra] >=0.8.0-alpha.2,<1.0.0.0",
            {
                "name": "package",
                "constraint": ">=0.8.0-alpha.2,<1.0.0.0",
                "extras": "extra",
                "url": None,
            },
        ),
    ],
)
def test_parse_pip_requirement(requirement, parsed):
    assert parse_pip_requirement(requirement) == parsed


def test_parse_meta_yaml_file(meta_yaml_environment):
    res = parse_meta_yaml_file(meta_yaml_environment, ["linux-64", "osx-64"])
    specs = {dep.name: dep for dep in res.dependencies}
    assert all(x in specs for x in ["python", "numpy"])
    # Ensure that this dep specified by a python selector is ignored
    assert "enum34" not in specs
    # Ensure that this platform specific dep is included
    assert "zlib" in specs
    assert specs["pytest"].category == "dev"
    assert specs["pytest"].optional is True


def test_parse_poetry(poetry_pyproject_toml):
    res = parse_pyproject_toml(
        poetry_pyproject_toml,
    )

    specs = {dep.name: dep for dep in res.dependencies}

    assert specs["requests"].version == ">=2.13.0,<3.0.0"
    assert specs["toml"].version == ">=0.10"
    assert specs["sqlite"].version == "<3.34"
    assert specs["certifi"].version == ">=2019.11.28"
    assert specs["pytest"].version == ">=5.1.0,<5.2.0"
    assert specs["pytest"].optional is True
    assert specs["pytest"].category == "dev"
    assert specs["tomlkit"].version == ">=0.7.0,<1.0.0"
    assert specs["tomlkit"].optional is True
    assert specs["tomlkit"].category == "tomlkit"

    assert res.channels == ["defaults"]


def test_parse_flit(flit_pyproject_toml):
    res = parse_pyproject_toml(
        flit_pyproject_toml,
    )

    specs = {dep.name: dep for dep in res.dependencies}

    assert specs["requests"].version == ">=2.13.0"
    assert specs["toml"].version == ">=0.10"
    assert specs["sqlite"].version == "<3.34"
    assert specs["certifi"].version == ">=2019.11.28"
    assert specs["pytest"].version == ">=5.1.0"
    assert specs["pytest"].optional is True
    assert specs["pytest"].category == "dev"

    assert res.channels == ["defaults"]


def test_run_lock(monkeypatch, zlib_environment, conda_exe):
    monkeypatch.chdir(zlib_environment.parent)
    if is_micromamba(conda_exe):
        monkeypatch.setenv("CONDA_FLAGS", "-v")
    run_lock([zlib_environment], conda_exe=conda_exe)


@pytest.fixture
def update_environment():
    env = TEST_DIR.joinpath("test-update").joinpath("environment-postupdate.yml")
    lock = env.parent / DEFAULT_LOCKFILE_NAME
    shutil.copy(lock, lock.with_suffix(".bak"))
    yield env
    shutil.copy(lock.with_suffix(".bak"), lock)


def test_run_lock_with_update(monkeypatch, update_environment, conda_exe):
    monkeypatch.chdir(update_environment.parent)
    if is_micromamba(conda_exe):
        monkeypatch.setenv("CONDA_FLAGS", "-v")
    pre_lock = {
        p.name: p
        for p in parse_conda_lock_file(
            update_environment.parent / DEFAULT_LOCKFILE_NAME
        ).package
    }
    run_lock([update_environment], conda_exe=conda_exe, update=["pydantic"])
    post_lock = {
        p.name: p
        for p in parse_conda_lock_file(
            update_environment.parent / DEFAULT_LOCKFILE_NAME
        ).package
    }
    assert post_lock["pydantic"].version == "1.8.2"
    assert post_lock["python"].version == pre_lock["python"].version


def test_run_lock_with_locked_environment_files(
    monkeypatch, update_environment, conda_exe
):
    """run_lock() with default args uses source files from lock"""
    monkeypatch.chdir(update_environment.parent)
    make_lock_files = MagicMock()
    monkeypatch.setattr("conda_lock.conda_lock.make_lock_files", make_lock_files)
    run_lock(DEFAULT_FILES, conda_exe=conda_exe, update=["pydantic"])
    assert [p.resolve() for p in make_lock_files.call_args.kwargs["src_files"]] == [
        pathlib.Path(update_environment.parent / "environment-preupdate.yml")
    ]


def test_run_lock_with_pip(monkeypatch, pip_environment, conda_exe):
    monkeypatch.chdir(pip_environment.parent)
    if is_micromamba(conda_exe):
        monkeypatch.setenv("CONDA_FLAGS", "-v")
    run_lock([pip_environment], conda_exe=conda_exe)


def test_run_lock_with_local_package(
    monkeypatch, pip_local_package_environment, conda_exe
):
    monkeypatch.chdir(pip_local_package_environment.parent)
    if is_micromamba(conda_exe):
        monkeypatch.setenv("CONDA_FLAGS", "-v")
    virtual_package_repo = default_virtual_package_repodata()

    with virtual_package_repo:
        lock_spec = make_lock_spec(
            src_files=[pip_local_package_environment],
            virtual_package_repo=virtual_package_repo,
        )
    assert not any(
        p.manager == "pip" for p in lock_spec.dependencies
    ), "conda-lock ignores editable pip deps"


def test_run_lock_with_input_hash_check(
    monkeypatch, input_hash_zlib_environment: pathlib.Path, conda_exe, capsys
):
    monkeypatch.chdir(input_hash_zlib_environment.parent)
    if is_micromamba(conda_exe):
        monkeypatch.setenv("CONDA_FLAGS", "-v")
    lockfile = input_hash_zlib_environment.parent / "conda-linux-64.lock"
    if lockfile.exists():
        lockfile.unlink()

    run_lock(
        [input_hash_zlib_environment],
        platforms=["linux-64"],
        conda_exe=conda_exe,
        check_input_hash=True,
    )
    stat = lockfile.stat()
    created = stat.st_mtime_ns

    with open(lockfile) as f:
        previous_hash = extract_input_hash(f.read())
        assert previous_hash is not None
        assert len(previous_hash) == 64

    capsys.readouterr()
    run_lock(
        [input_hash_zlib_environment],
        platforms=["linux-64"],
        conda_exe=conda_exe,
        check_input_hash=True,
    )
    stat = lockfile.stat()
    assert stat.st_mtime_ns == created
    output = capsys.readouterr()
    assert "Spec hash already locked for" in output.err


@pytest.mark.parametrize(
    "package,version,url_pattern",
    [
        ("python", ">=3.6,<3.7", "/python-3.6"),
        ("python", "~3.6", "/python-3.6"),
        ("python", "^2.7", "/python-2.7"),
    ],
)
def test_poetry_version_parsing_constraints(package, version, url_pattern, capsys):
    _conda_exe = determine_conda_executable(None, mamba=False, micromamba=False)

    vpr = default_virtual_package_repodata()
    with vpr, capsys.disabled():
        with tempfile.NamedTemporaryFile(dir=".") as tf:
            spec = LockSpecification(
                dependencies=[
                    VersionedDependency(
                        name=package,
                        version=poetry_version_to_conda_version(version),
                        manager="conda",
                        optional=False,
                        category="main",
                        extras=[],
                    )
                ],
                channels=["conda-forge"],
                platforms=["linux-64"],
                # NB: this file must exist for relative path resolution to work
                # in create_lockfile_from_spec
                sources=[pathlib.Path(tf.name)],
                virtual_package_repo=vpr,
            )
            lockfile_contents = create_lockfile_from_spec(
                conda=_conda_exe,
                spec=spec,
                lockfile_path=pathlib.Path(DEFAULT_LOCKFILE_NAME),
            )

        python = next(p for p in lockfile_contents.package if p.name == "python")
        assert url_pattern in python.url


def _make_spec(name, constraint="*"):
    return VersionedDependency(
        name=name,
        version=constraint,
    )


def test_aggregate_lock_specs():
    gpu_spec = LockSpecification(
        dependencies=[_make_spec("pytorch")],
        channels=["pytorch", "conda-forge"],
        platforms=["linux-64"],
        sources=[pathlib.Path("ml-stuff.yml")],
    )

    base_spec = LockSpecification(
        dependencies=[_make_spec("python", "=3.7")],
        channels=["conda-forge"],
        platforms=["linux-64"],
        sources=[pathlib.Path("base-env.yml")],
    )

    # NB: content hash explicitly does not depend on the source file names
    assert (
        aggregate_lock_specs([gpu_spec, base_spec]).content_hash()
        == LockSpecification(
            dependencies=[_make_spec("pytorch"), _make_spec("python", "=3.7")],
            channels=["pytorch", "conda-forge"],
            platforms=["linux-64"],
            sources=[],
        ).content_hash()
    )

    assert (
        aggregate_lock_specs([base_spec, gpu_spec]).content_hash()
        != LockSpecification(
            dependencies=[_make_spec("pytorch"), _make_spec("python", "=3.7")],
            channels=["conda-forge"],
            platforms=["linux-64"],
            sources=[],
        ).content_hash()
    )


@pytest.fixture(
    scope="session",
    params=[
        pytest.param("conda"),
        pytest.param("mamba"),
        pytest.param("micromamba"),
        # pytest.param("conda_exe"),
    ],
)
def conda_exe(request):
    kwargs = dict(
        mamba=False,
        micromamba=False,
        conda=False,
        conda_exe=False,
    )
    kwargs[request.param] = True
    _conda_exe = _ensureconda(**kwargs)

    if _conda_exe is not None:
        return _conda_exe
    raise pytest.skip(f"{request.param} is not installed")


def _check_package_installed(package: str, prefix: str):
    import glob

    files = list(glob.glob(f"{prefix}/conda-meta/{package}-*.json"))
    assert len(files) >= 1
    # TODO: validate that all the files are in there
    for fn in files:
        data = json.load(open(fn))
        for expected_file in data["files"]:
            assert (pathlib.Path(prefix) / pathlib.Path(expected_file)).exists()
    return True


def conda_supports_env(conda_exe):
    try:
        subprocess.check_call(
            [conda_exe, "env"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError:
        return False
    return True


@pytest.mark.parametrize("kind", ["explicit", "env"])
def test_install(
    request, kind, tmp_path, conda_exe, zlib_environment, monkeypatch, capsys
):
    if is_micromamba(conda_exe):
        monkeypatch.setenv("CONDA_FLAGS", "-v")
    if kind == "env" and not conda_supports_env(conda_exe):
        pytest.skip(
            f"Standalone conda @ '{conda_exe}' does not support materializing from environment files."
        )

    package = "zlib"
    platform = "linux-64"

    lock_filename_template = (
        request.node.name + "conda-{platform}-{dev-dependencies}.lock"
    )
    lock_filename = (
        request.node.name
        + "conda-linux-64-true.lock"
        + (".yml" if kind == "env" else "")
    )
    try:
        os.remove(lock_filename)
    except OSError:
        pass

    from click.testing import CliRunner

    with capsys.disabled():
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(
            main,
            [
                "lock",
                "--conda",
                conda_exe,
                "-p",
                platform,
                "-f",
                zlib_environment,
                "-k",
                kind,
                "--filename-template",
                lock_filename_template,
            ],
            catch_exceptions=False,
        )
    print(result.stdout, file=sys.stdout)
    print(result.stderr, file=sys.stderr)
    assert result.exit_code == 0

    env_name = "test_env"

    def invoke_install(*extra_args):
        with capsys.disabled():
            return runner.invoke(
                main,
                [
                    "install",
                    "--conda",
                    conda_exe,
                    "--prefix",
                    tmp_path / env_name,
                    *extra_args,
                    lock_filename,
                ],
                catch_exceptions=False,
            )

    if sys.platform.lower().startswith("linux"):
        context = contextlib.nullcontext()
    else:
        # since by default we do platform validation we would expect this to fail
        context = pytest.raises(PlatformValidationError)

    with context:
        result = invoke_install()
    print(result.stdout, file=sys.stdout)
    print(result.stderr, file=sys.stderr)
    if pathlib.Path(lock_filename).exists:
        logging.debug(
            "lockfile contents: \n\n=======\n%s\n\n==========",
            pathlib.Path(lock_filename).read_text(),
        )

    if sys.platform.lower().startswith("linux"):
        assert _check_package_installed(
            package=package,
            prefix=str(tmp_path / env_name),
        ), f"Package {package} does not exist in {tmp_path} environment"


@pytest.mark.parametrize(
    "line,stripped",
    (
        (
            "https://conda.mychannel.cloud/mypackage",
            "https://conda.mychannel.cloud/mypackage",
        ),
        (
            "https://user:password@conda.mychannel.cloud/mypackage",
            "https://conda.mychannel.cloud/mypackage",
        ),
        (
            "http://conda.mychannel.cloud/mypackage",
            "http://conda.mychannel.cloud/mypackage",
        ),
        (
            "http://user:password@conda.mychannel.cloud/mypackage",
            "http://conda.mychannel.cloud/mypackage",
        ),
    ),
)
def test__strip_auth_from_line(line, stripped):
    assert _strip_auth_from_line(line) == stripped


@pytest.mark.parametrize(
    "line,stripped",
    (
        ("https://conda.mychannel.cloud/mypackage", "conda.mychannel.cloud"),
        ("http://conda.mychannel.cloud/mypackage", "conda.mychannel.cloud"),
    ),
)
def test__extract_domain(line, stripped):
    assert _extract_domain(line) == stripped


def _read_file(filepath):
    with open(filepath, mode="r") as file_pointer:
        return file_pointer.read()


@pytest.mark.parametrize(
    "lockfile,stripped_lockfile",
    tuple(
        (
            _read_file(
                pathlib.Path(__file__)
                .parent.joinpath("test-lockfile")
                .joinpath(f"{filename}.lock")
            ),
            _read_file(
                pathlib.Path(__file__)
                .parent.joinpath("test-stripped-lockfile")
                .joinpath(f"{filename}.lock")
            ),
        )
        for filename in ("test", "no-auth")
    ),
)
def test__strip_auth_from_lockfile(lockfile, stripped_lockfile):
    assert _strip_auth_from_lockfile(lockfile) == stripped_lockfile


@pytest.mark.parametrize(
    "line,auth,line_with_auth",
    (
        (
            "https://conda.mychannel.cloud/mypackage",
            {"conda.mychannel.cloud": "username:password"},
            "https://username:password@conda.mychannel.cloud/mypackage",
        ),
        (
            "https://conda.mychannel.cloud/mypackage",
            {},
            "https://conda.mychannel.cloud/mypackage",
        ),
        (
            "https://conda.mychannel.cloud/channel1/mypackage",
            {"conda.mychannel.cloud/channel1": "username:password"},
            "https://username:password@conda.mychannel.cloud/channel1/mypackage",
        ),
        (
            "https://conda.mychannel.cloud/channel1/mypackage",
            {
                "conda.mychannel.cloud": "username:password",
                "conda.mychannel.cloud/channel1": "username1:password1",
            },
            "https://username1:password1@conda.mychannel.cloud/channel1/mypackage",
        ),
    ),
)
def test__add_auth_to_line(line, auth, line_with_auth):
    assert _add_auth_to_line(line, auth) == line_with_auth


@pytest.fixture(name="auth")
def auth_():
    return {
        "a.mychannel.cloud": "username_a:password_a",
        "c.mychannel.cloud": "username_c:password_c",
    }


@pytest.mark.parametrize(
    "stripped_lockfile,lockfile_with_auth",
    tuple(
        (
            _read_file(TEST_DIR / "test-stripped-lockfile" / f"{filename}.lock"),
            _read_file(TEST_DIR / "test-lockfile-with-auth" / f"{filename}.lock"),
        )
        for filename in ("test",)
    ),
)
def test__add_auth_to_lockfile(stripped_lockfile, lockfile_with_auth, auth):
    assert _add_auth_to_lockfile(stripped_lockfile, auth) == lockfile_with_auth


@pytest.mark.parametrize("kind", ["explicit", "env"])
def test_virtual_packages(conda_exe, monkeypatch, kind, capsys):
    test_dir = TEST_DIR.joinpath("test-cuda")
    monkeypatch.chdir(test_dir)

    if is_micromamba(conda_exe):
        monkeypatch.setenv("CONDA_FLAGS", "-v")
    if kind == "env" and not conda_supports_env(conda_exe):
        pytest.skip(
            f"Standalone conda @ '{conda_exe}' does not support materializing from environment files."
        )

    platform = "linux-64"

    from click.testing import CliRunner, Result

    for lockfile in glob(f"conda-{platform}.*"):
        os.unlink(lockfile)

    with capsys.disabled():
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(
            main,
            [
                "lock",
                "--conda",
                conda_exe,
                "-p",
                platform,
                "-k",
                kind,
            ],
        )

    print(result.stdout, file=sys.stdout)
    print(result.stderr, file=sys.stderr)
    if result.exception:
        raise result.exception
    assert result.exit_code == 0

    for lockfile in glob(f"conda-{platform}.*"):
        os.unlink(lockfile)

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(
        main,
        [
            "lock",
            "--conda",
            conda_exe,
            "-p",
            platform,
            "-k",
            kind,
            "--virtual-package-spec",
            test_dir / "virtual-packages-old-glibc.yaml",
        ],
    )

    # micromamba doesn't respect the CONDA_OVERRIDE_XXX="" env vars appropriately so it will include the
    # system virtual packages regardless of whether they should be present.  Skip this check in that case
    if not is_micromamba(conda_exe):
        assert result.exit_code != 0


def test_virtual_package_input_hash_stability():
    from conda_lock.virtual_package import virtual_package_repo_from_specification

    test_dir = TEST_DIR.joinpath("test-cuda")
    spec = test_dir / "virtual-packages-old-glibc.yaml"

    vpr = virtual_package_repo_from_specification(spec)
    spec = LockSpecification(
        dependencies=[],
        channels=[],
        platforms=["linux-64"],
        sources=[],
        virtual_package_repo=vpr,
    )
    expected = "d8d0e556f97aed2eaa05fe9728b5a1c91c1b532d3eed409474e8a9b85b633a26"
    assert spec.content_hash() == {"linux-64": expected}


def test_default_virtual_package_input_hash_stability():
    from conda_lock.virtual_package import default_virtual_package_repodata

    vpr = default_virtual_package_repodata()

    expected = {
        "linux-64": "93c22a62ca75ed0fd7649a6c9fbac611fd42a694465841b141c91aa2d4edf1b3",
        "linux-aarch64": "e1115c4d229438be0bd3e79c3734afb1f2fb8db42cf0c20c0e2ede5405e97e25",
        "linux-ppc64le": "d980051789ba7e6374c0833bf615b060bc0c5dfa63907eb4f11ac85f4dbb80da",
        "osx-64": "8e2e62ea8061892d10606e9a10f05f4c7358c798e5a2d390b1206568bf9338a2",
        "osx-arm64": "00eb1bef60572765717bba1fd86da4527f3b69bd40eb51cd0b60cdc89c27f5a6",
        "win-64": "d97edec84c3f450ac23bd2fbac57f77c0b0bffd5313114c1fa8c28c4df8ead6e",
    }

    spec = LockSpecification(
        dependencies=[],
        channels=[],
        platforms=list(expected.keys()),
        sources=[],
        virtual_package_repo=vpr,
    )
    assert spec.content_hash() == expected


@pytest.fixture
def conda_lock_yaml():
    return (
        pathlib.Path(__file__)
        .parent.joinpath("test-lockfile")
        .joinpath(DEFAULT_LOCKFILE_NAME)
    )


def test_fake_conda_env(conda_exe, conda_lock_yaml):

    lockfile_content = parse_conda_lock_file(conda_lock_yaml)

    with fake_conda_environment(
        lockfile_content.package, platform="linux-64"
    ) as prefix:
        subprocess.call(
            [
                conda_exe,
                "list",
                "--debug",
                "-p",
                prefix,
                "--json",
            ]
        )
        packages = json.loads(
            subprocess.check_output(
                [
                    conda_exe,
                    "list",
                    "--debug",
                    "-p",
                    prefix,
                    "--json",
                ]
            )
        )
        locked = {
            p.name: p
            for p in lockfile_content.package
            if p.manager == "conda" and p.platform == "linux-64"
        }
        assert len(packages) == len(locked)
        for env_package in packages:
            locked_package = locked[env_package["name"]]

            platform = env_package["platform"]
            path = pathlib.PurePosixPath(
                urlsplit(urldefrag(locked_package.url)[0]).path
            )
            if is_micromamba(conda_exe):
                assert (
                    env_package["base_url"]
                    == f"https://conda.anaconda.org/conda-forge/{platform}"
                )
                assert env_package["channel"] == f"conda-forge/{platform}"
            else:
                assert (
                    env_package["base_url"] == "https://conda.anaconda.org/conda-forge"
                )
                assert env_package["channel"] == "conda-forge"
            assert env_package["dist_name"] == f"{path.name[:-8]}"
            assert platform == path.parent.name
